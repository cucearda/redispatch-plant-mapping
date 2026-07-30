"""Orchestrator: load data -> build graph -> wedge -> LP -> ranking CSV.

Run:  python -m battery_siting_score.run
       (or: python run.py from inside this folder, but it requires the package
       imports below — easier to run from the parent ALLES_NEU dir as a module)
"""
from __future__ import annotations
import argparse
import math
import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd
import yaml
from scipy.stats import spearmanr

# Allow running as a script from inside the package folder
if __package__ in (None, ""):
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE.parent))
    from battery_siting_score.data_load import (
        load_batteries, make_artificial_grid, load_redispatch, load_prices_quarterhourly,
        filter_to_window, expand_events_to_quarterhours,
    )
    from battery_siting_score.network import build_network, snap_points_to_buses
    from battery_siting_score.wedge import build_wedge, WedgeConfig
    from battery_siting_score.score_lp import annual_profit, STEPS_PER_YEAR
    from battery_siting_score.score_proxy import annual_proxy, proxy_copperplate_per_mw
    from battery_siting_score.rank import per_battery_diagnostics, join_with_meta
else:
    from .data_load import (
        load_batteries, make_artificial_grid, load_redispatch, load_prices_quarterhourly,
        filter_to_window, expand_events_to_quarterhours,
    )
    from .network import build_network, snap_points_to_buses
    from .wedge import build_wedge, WedgeConfig
    from .score_lp import annual_profit, STEPS_PER_YEAR
    from .score_proxy import annual_proxy, proxy_copperplate_per_mw
    from .rank import per_battery_diagnostics, join_with_meta


def main(cfg_path: str = "config.yaml", sample_n: int | None = None,
         method: str | None = None, wedge_mode: str | None = None):
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if method is not None:                       # CLI override of run.method
        cfg.setdefault("run", {})["method"] = method
    if wedge_mode is not None:                   # CLI override of wedge.mode
        cfg.setdefault("wedge", {})["mode"] = wedge_mode

    out_dir = Path(cfg["paths"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80, "\n[1/6] LOAD")
    source = str(cfg["battery"].get("source", "mastr")).lower()
    if source == "grid":
        gcfg = cfg["battery"].get("grid", {}) or {}
        bat = make_artificial_grid(
            cfg["paths"]["buses_csv"],
            spacing_deg=float(gcfg.get("spacing_deg", 0.25)),
            power_mw=float(gcfg.get("power_mw", 50.0)),
            max_dist_km=float(gcfg.get("max_dist_km", 25.0)),
        )
    else:
        bat = load_batteries(
            cfg["paths"]["battery_csv"],
            status_filter=cfg["battery"]["status_filter"],
            drop_nan_coords=cfg["battery"]["drop_nan_coords"],
        )
    if sample_n is None:
        sample_n = cfg["run"].get("sample_batteries")
    if sample_n:
        bat = bat.sample(n=int(sample_n), random_state=0).reset_index(drop=True)
        print(f"[batteries] sampled to {len(bat)} for this run")

    red = load_redispatch(cfg["paths"]["redispatch_csv"])
    prices_q = load_prices_quarterhourly(cfg["paths"]["price_csv"])

    print("=" * 80, "\n[2/6] WINDOW + JOIN")
    w_start = pd.Timestamp(cfg["window"]["start_utc"])
    w_end = pd.Timestamp(cfg["window"]["end_utc"])
    red_w, prices_w = filter_to_window(red, prices_q, w_start, w_end)
    red_steps = expand_events_to_quarterhours(red_w, prices_w)

    print("=" * 80, "\n[3/6] NETWORK")
    net = build_network(cfg["paths"]["buses_csv"], cfg["paths"]["lines_csv"])
    bat_bus_idx = snap_points_to_buses(bat["lon"].to_numpy(), bat["lat"].to_numpy(), net)
    bat["bus_idx"] = bat_bus_idx
    print(f"[snap] {bat['bus_idx'].nunique()} unique battery-buses out of {len(bat)} batteries")

    print("=" * 80, "\n[4/6] WEDGE")
    wcfg = WedgeConfig(
        bandwidth_h_m=float(cfg["kernel"]["bandwidth_h_m"]),
        mode=str(cfg["wedge"].get("mode", "exponential")),
        wedge_scale=float(cfg["wedge"]["scale"]),
        calib_price_eur_per_mwh=float(cfg["wedge"].get("calib_price_eur_per_mwh", 100.0)),
        clip_eur_per_mwh=(None if cfg["wedge"].get("clip_eur_per_mwh") is None
                          else float(cfg["wedge"]["clip_eur_per_mwh"])),
        kernel_type=str(cfg["kernel"].get("type", "gaussian")),
        cutoff_sigmas=float(cfg["kernel"].get("cutoff_sigmas", 4.0)),
        ptdf_bandwidth=float(cfg["kernel"].get("ptdf_bandwidth", 0.5)),
        ptdf_shape=str(cfg["kernel"].get("ptdf_shape", "gaussian")),
        ptdf_buses_csv=cfg["paths"]["buses_csv"],
        ptdf_lines_csv=cfg["paths"]["lines_csv"],
    )
    pi_hat, wedge_only = build_wedge(red_steps, prices_w, bat_bus_idx, net, wcfg)
    print(f"[pi_hat] shape {pi_hat.shape}  mean {pi_hat.mean():.2f}  std {pi_hat.std():.2f}")

    print("=" * 80, "\n[5/6] COPPERPLATE BASELINE")
    # V_copperplate is the LP profit using p_base[t] only — spatially uniform.
    eta_rt = float(cfg["storage_lp"]["eta_roundtrip"])
    D_h = float(cfg["battery"]["duration_hours"])
    eta = math.sqrt(eta_rt)
    p_base_arr = prices_w.to_numpy()
    V_cp_per_mw = annual_profit(p_base_arr, 1.0, D_h, eta_rt)
    n_years = len(p_base_arr) / STEPS_PER_YEAR
    print(f"[copperplate] V/MW over full window ({n_years:.2f} yr): {V_cp_per_mw:,.0f} EUR "
          f"= {V_cp_per_mw/n_years:,.0f} EUR/MW/yr")

    # Power used for scoring: each battery's own nameplate, or a common fixed MW.
    base_cfg = cfg.get("baseline", {}) or {}
    if bool(base_cfg.get("use_fixed_mw", False)):
        fixed_mw = float(base_cfg.get("fixed_mw", 50.0))
        score_power = np.full(len(bat), fixed_mw, dtype=float)
        print(f"[baseline] use_fixed_mw=true -> scoring ALL {len(bat)} batteries at "
              f"{fixed_mw:g} MW (copperplate identical across fleet; dV = pure locational)")
    else:
        score_power = bat["p_bar_mw"].to_numpy(dtype=float)
        print("[baseline] use_fixed_mw=false -> scoring each battery at its own nameplate p_bar_mw")

    method = str(cfg["run"].get("method", "proxy")).lower()
    if method not in ("proxy", "lp"):
        raise ValueError(f"run.method must be 'proxy' or 'lp', got {method!r}")
    run_lp = (method == "lp")
    label = "LP daily solver" if run_lp else "fast screening proxy only"
    print("=" * 80, f"\n[6/6] SCORE ({method}: {label}) + RANK")

    t0 = time.time()
    n_workers = int(cfg["run"].get("n_workers", 1))
    rank_df = per_battery_diagnostics(
        bat_df=bat,
        pi_hat=pi_hat,
        wedge_only=wedge_only,
        duration_hours=D_h,
        eta_roundtrip=eta_rt,
        V_copperplate_per_mw=V_cp_per_mw,
        n_workers=n_workers,
        score_power_mw=score_power,
        run_lp=run_lp,
    )
    if run_lp:
        print(f"[LP] all batteries done in {time.time()-t0:.1f}s")

    # Proxy: always computed (cheap, and the ranking signal when method='proxy').
    print("[proxy] computing screening proxy ...")
    V_proxy = np.zeros(len(bat))
    for j in range(len(bat)):
        V_proxy[j] = annual_proxy(pi_hat[:, j], float(score_power[j]), D_h)
    rank_df["V_proxy_eur"] = V_proxy
    rank_df["V_proxy_eur_per_year"] = V_proxy / max(n_years, 1e-9)

    # Proxy copperplate: the SAME single-block proxy estimator on the flat zonal
    # price (no wedge). Subtracting this (not the multi-cycle LP copperplate) makes
    # dV_proxy a like-for-like locational premium: positive where the wedge adds
    # price dispersion, ~0 where it does not.
    V_cp_proxy_per_mw = proxy_copperplate_per_mw(p_base_arr, D_h)
    print(f"[proxy] proxy copperplate: {V_cp_proxy_per_mw/n_years:,.0f} EUR/MW/yr "
          f"(vs LP copperplate {V_cp_per_mw/n_years:,.0f} EUR/MW/yr)")
    rank_df["V_copperplate_proxy_eur"] = V_cp_proxy_per_mw * score_power
    rank_df["dV_proxy_eur"] = V_proxy - rank_df["V_copperplate_proxy_eur"]
    rank_df["dV_proxy_eur_per_year"] = rank_df["dV_proxy_eur"] / max(n_years, 1e-9)

    if run_lp:
        rho_V, _ = spearmanr(rank_df["V_eur"], rank_df["V_proxy_eur"])
        rho_dV, _ = spearmanr(rank_df["dV_eur"], rank_df["dV_proxy_eur"])
        print(f"[proxy] Spearman rank corr  V vs V_proxy: {rho_V:.4f}    dV vs dV_proxy: {rho_dV:.4f}")
    else:
        print("[proxy] method='proxy' -> LP not run, no Spearman cross-check; ranking on proxy dV")

    sort_col = "dV_eur_per_year" if run_lp else "dV_proxy_eur_per_year"
    full = join_with_meta(rank_df, bat, sort_col=sort_col)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    wmode = str(cfg["wedge"].get("mode", "exponential")).lower().strip()
    out_csv = out_dir / f"battery_siting_scores_{wmode}_{stamp}.csv"
    full.to_csv(out_csv, index=False)
    print(f"[out] -> {out_csv}")
    print()
    print(full.head(15).to_string(index=False))
    return full


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--sample", type=int, default=None, help="sample N batteries (overrides config)")
    parser.add_argument("--method", choices=["proxy", "lp"], default=None,
                        help="scoring method (overrides config.run.method)")
    parser.add_argument("--wedge-mode", choices=["exponential", "additive"], default=None,
                        help="wedge construction (overrides config.wedge.mode)")
    args = parser.parse_args()
    main(args.config, sample_n=args.sample, method=args.method, wedge_mode=args.wedge_mode)
