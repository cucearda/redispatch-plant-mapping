"""Effective local price pi-hat per battery, per quarter-hour step.

    pi_hat[n, t] = p_base[t] + wedge[n, t]
    wedge[n, t]  = wedge_scale * p_base[t] * Sum_g K_h(d(n, bus(g))) * sign[g]

where t indexes 15-min steps and the sum is over redispatch events g active in
step t. mc[g,t] is set to p_base[t] (the joined intraday price) per Step-0b
confirmation: the redispatch table has no monetary column, and the user
specified the joined intraday price as the cost field. The per-event MW
magnitude is NOT used; an optional volume-weighted variant can be added
trivially.

Sign mapping (printed at build time, asserted to cover 100% of rows):
    "Wirkleistungseinspeisung erhoehen"   -> +1   (grid short, battery discharges)
    "Wirkleistungseinspeisung reduzieren" -> -1   (grid surplus, battery charges)
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .network import (
    NetworkInfo, dijkstra_distances, snap_points_to_buses,
    kernel_function, dijkstra_cutoff_for,
)


@dataclass
class WedgeConfig:
    bandwidth_h_m: float = 100_000.0
    mode: str = "exponential"                # "exponential": pi_hat = p_base*exp(alpha*W_sign)
                                             # "additive":    pi_hat = p_base + alpha_cal*W_vol
    wedge_scale: float = 0.05                # exponential: alpha (dimensionless) in exp(alpha*W)
    calib_price_eur_per_mwh: float = 100.0   # additive: target mean|wedge| (EUR/MWh); alpha auto-derived.
                                             #   set = BNetzA annual redispatch cost / annual energy.
    alpha_additive: float | None = None      # additive: if set, USE this alpha directly instead of
                                             #   calibrating from mean|W| over the evaluated points. This
                                             #   makes the per-point score independent of the SET of points
                                             #   it is evaluated on (freeze the fleet's alpha, then reuse it
                                             #   to score an arbitrary grid). When None, build_wedge calibrates
                                             #   as usual AND writes the result back here so the caller can
                                             #   capture and re-use it.
    clip_eur_per_mwh: float | None = 200.0   # +/- bound on the implied additive wedge; None disables
    kernel_type: str = "gaussian"        # "quartic", "gaussian", or "ptdf"
    cutoff_sigmas: float = 4.0           # only used by gaussian (graph-dist); Dijkstra cutoff = cutoff_sigmas * sigma
    # ptdf-specific:
    ptdf_bandwidth: float = 0.5          # dimensionless; bandwidth for K_h applied to ||PTDF[:,n] - PTDF[:,g]||_2
    ptdf_shape: str = "gaussian"         # "gaussian" or "quartic" -- the K_h SHAPE for the PTDF distance
    ptdf_buses_csv: str | None = None    # required when kernel_type == "ptdf"
    ptdf_lines_csv: str | None = None


def build_wedge(
    df_red_steps: pd.DataFrame,         # long-format: event_idx, step_utc, sign, lat, lon
    prices: pd.Series,                  # 15-min UTC, index = DatetimeIndex(UTC)
    bat_bus_idx: np.ndarray,            # (n_batteries,) bus index per battery
    net: NetworkInfo,
    cfg: WedgeConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (pi_hat, wedge_only) of shape (n_steps, n_batteries).

    n_steps = len(prices) (the canonical 15-min grid)
    """
    n_t = len(prices)
    n_b = len(bat_bus_idx)
    step_index = prices.index

    # 1) snap each redispatch *location* to a bus, unique buses only
    ev_latlon = df_red_steps[["lat", "lon"]].drop_duplicates().reset_index(drop=True)
    ev_bus = snap_points_to_buses(ev_latlon["lon"].to_numpy(), ev_latlon["lat"].to_numpy(), net)
    ev_loc_key = pd.MultiIndex.from_frame(ev_latlon)
    loc_to_bus = pd.Series(ev_bus, index=ev_loc_key, name="ev_bus")

    # tag every per-step event-row with its bus
    df = df_red_steps.copy()
    df["ev_bus"] = loc_to_bus.loc[pd.MultiIndex.from_frame(df[["lat", "lon"]])].values

    # 2) unique event-buses + unique battery-buses
    unique_ev_buses = np.array(sorted(np.unique(df["ev_bus"])), dtype=int)
    unique_bat_buses = np.array(sorted(np.unique(bat_bus_idx)), dtype=int)
    print(f"[wedge] unique event buses: {len(unique_ev_buses)}  unique battery buses: {len(unique_bat_buses)}")

    # 3) Kernel matrix K[i_ev_bus, i_bat_bus] -- two paths:
    #    (a) graph-distance kernels (quartic, gaussian): Dijkstra over net.G,
    #        K_h applied to graph distance in metres.
    #    (b) PTDF kernel: PyPSA DC PTDF, K_h applied to ||P[:,n] - P[:,g]||_2
    #        in dimensionless PTDF units.
    if cfg.kernel_type.lower().strip() == "ptdf":
        from .ptdf import build_pypsa_network, compute_ptdf_columns, ptdf_kernel_matrix
        if cfg.ptdf_buses_csv is None or cfg.ptdf_lines_csv is None:
            raise ValueError("kernel_type='ptdf' requires ptdf_buses_csv and ptdf_lines_csv in WedgeConfig")
        pn = build_pypsa_network(cfg.ptdf_buses_csv, cfg.ptdf_lines_csv, net)
        ptdf_cols, sub_id = compute_ptdf_columns(pn, net)
        K = ptdf_kernel_matrix(
            ptdf_cols, sub_id,
            ev_bus_idx=unique_ev_buses,
            bat_bus_idx=unique_bat_buses,
            kernel_kind=cfg.ptdf_shape,
            bandwidth=cfg.ptdf_bandwidth,
        )                                                # (n_ev_bus, n_bat_bus)
        print(f"[wedge] kernel='ptdf'  shape={cfg.ptdf_shape!r}  bandwidth={cfg.ptdf_bandwidth} (dim-less)")
        print(f"[wedge] K matrix shape {K.shape}  nonzero entries {(K>0).sum()} / {K.size}  "
              f"mean K {K.mean():.4f}")
    else:
        cutoff_m = dijkstra_cutoff_for(cfg.kernel_type, cfg.bandwidth_h_m, cfg.cutoff_sigmas)
        D_ev_to_all = dijkstra_distances(net, unique_ev_buses, cutoff_m=cutoff_m)
        D_ev_to_bat = D_ev_to_all[:, unique_bat_buses]
        kfn = kernel_function(cfg.kernel_type)
        K = kfn(D_ev_to_bat, cfg.bandwidth_h_m)          # (n_ev_bus, n_bat_bus)
        print(f"[wedge] kernel={cfg.kernel_type!r}  bandwidth={cfg.bandwidth_h_m/1000:.1f} km  "
              f"Dijkstra cutoff={cutoff_m/1000:.1f} km")
        print(f"[wedge] K matrix shape {K.shape}  nonzero entries {(K>0).sum()} / {K.size}  "
              f"mean K {K.mean():.4f}")

    # 4) step map: step_utc -> step_idx in price grid
    step_to_idx = pd.Series(np.arange(n_t), index=step_index)
    df["step_idx"] = step_to_idx.loc[df["step_utc"]].values

    # event_bus_pos: position of ev_bus in unique_ev_buses
    ev_bus_pos = np.searchsorted(unique_ev_buses, df["ev_bus"].to_numpy())
    df["ev_bus_pos"] = ev_bus_pos

    # 5) Build signed event weight[t, i_ev] = sum_g (sign_g * w_g) for events at ev_bus i in step t.
    #    Accumulate as a sparse-ish (n_t, n_ev_bus) array. n_ev_bus is small (~hundreds).
    #    exponential mode: w_g = 1 (sign only). additive mode: w_g = mean power [MW] of the event,
    #    so the wedge is volume-weighted by the MW of congestion-relief active in that step.
    mode = cfg.mode.lower().strip()
    if mode == "additive" and "mw" in df.columns:
        weight = (df["sign"].to_numpy(dtype=np.float64) * df["mw"].to_numpy(dtype=np.float64)).astype(np.float32)
    else:
        weight = df["sign"].to_numpy(dtype=np.float32)
    n_ev = len(unique_ev_buses)
    sec = np.zeros((n_t, n_ev), dtype=np.float32)
    np.add.at(sec, (df["step_idx"].to_numpy(), df["ev_bus_pos"].to_numpy()), weight)

    # 6) Wedge weight at each (step, battery) = sec @ K_bat
    #    bat_bus_idx may map multiple batteries to the same bus -> reindex via unique_bat_buses
    bat_bus_pos = np.searchsorted(unique_bat_buses, bat_bus_idx)   # (n_batteries,)
    K_bat_full = K[:, bat_bus_pos]                                  # (n_ev_bus, n_batteries)

    print(f"[wedge] computing sec @ K  (sec {sec.shape}  K {K_bat_full.shape}) ...")
    W = sec @ K_bat_full.astype(np.float32)                         # (n_t, n_batteries)
    print(f"[wedge] W range: {W.min():.4f} .. {W.max():.4f}  mean|W| {np.mean(np.abs(W)):.4f}")

    # 7) Build pi_hat from W per the configured mode.
    #    exponential: pi_hat = p_base * exp(alpha * W_sign). Multiplicative in price -> shift scales
    #                 with the price level (biases toward peak/sell hours); strictly positive.
    #    additive:    pi_hat = p_base + alpha_cal * W_vol. Flat EUR/MWh shift independent of price
    #                 level -> a surplus neighbourhood lowers the cheap charging hours as much as the
    #                 expensive ones (symmetric peak/trough). W_vol is volume-weighted (MW). alpha_cal
    #                 is CALIBRATED so the mean absolute wedge over active steps equals
    #                 calib_price_eur_per_mwh (set this = BNetzA annual redispatch cost / annual energy),
    #                 so the local price premium is anchored to the empirical cost of congestion rather
    #                 than an assumed scalar.
    p_base = prices.to_numpy(dtype=np.float32)                      # (n_t,)
    if mode == "exponential":
        factor = np.exp(np.float32(cfg.wedge_scale) * W)            # (n_t, n_b), strictly > 0
        pi_hat = p_base[:, None] * factor
        wedge = pi_hat - p_base[:, None]                            # implied additive wedge [EUR/MWh]
        print(f"[wedge] exponential  alpha={cfg.wedge_scale}  exp-factor range "
              f"{factor.min():.3f} .. {factor.max():.3f}")
    elif mode == "additive":
        if cfg.alpha_additive is not None:
            # FROZEN alpha: use the supplied value directly. The per-point wedge is then
            # a pure function of the point's bus and the events, independent of which OTHER
            # points are scored alongside it (so a grid and the fleet stay on one scale).
            alpha_cal = float(cfg.alpha_additive)
            nz = W != 0
            mean_abs_W = float(np.abs(W[nz]).mean()) if nz.any() else float("nan")
            print(f"[wedge] additive (volume-weighted)  FROZEN alpha={alpha_cal:.4g} EUR/MWh per (MW-kernel unit)"
                  f"  | mean|W| on this point set={mean_abs_W:.3g} (NOT used for calibration)")
        else:
            nz = W != 0
            mean_abs_W = float(np.abs(W[nz]).mean()) if nz.any() else 1.0
            alpha_cal = float(cfg.calib_price_eur_per_mwh) / mean_abs_W if mean_abs_W > 0 else 0.0
            cfg.alpha_additive = alpha_cal   # expose the calibrated value so the caller can freeze + reuse it
            print(f"[wedge] additive (volume-weighted)  target mean|wedge|={cfg.calib_price_eur_per_mwh} EUR/MWh"
                  f"  -> calibrated alpha={alpha_cal:.4g} EUR/MWh per (MW-kernel unit)"
                  f"  | mean|W|={mean_abs_W:.3g}")
        wedge = (np.float32(alpha_cal) * W)                         # (n_t, n_b) [EUR/MWh]
        pi_hat = p_base[:, None] + wedge
    else:
        raise ValueError(f"wedge.mode must be 'exponential' or 'additive', got {cfg.mode!r}")

    if cfg.clip_eur_per_mwh is not None:
        wedge = np.clip(wedge, -cfg.clip_eur_per_mwh, cfg.clip_eur_per_mwh)
        pi_hat = p_base[:, None] + wedge
    return pi_hat.astype(np.float64), wedge.astype(np.float64)