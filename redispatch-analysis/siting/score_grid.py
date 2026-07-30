"""Fleet-INDEPENDENT locational siting surface on a regular grid over Germany.

Motivation
----------
The per-battery scorer (`run.py`) evaluates the locational premium dV at the
*locations batteries happen to be built*. The resulting maps therefore mix two
things: (a) where good sites are, and (b) where the fleet is dense. This script
removes (b): it scores a regular lat/lon grid spanning Germany, so the output is
a pure "value of putting a 50 MW battery here" field that does not depend on the
number of batteries or where they sit.

Two design points that make it genuinely fleet-independent:

1. **Alpha is frozen from the fleet run.** In additive mode the wedge constant
   `alpha_cal` is normally calibrated from `mean|W|` over the evaluated points,
   which would make it depend on the grid resolution / extent. We instead
   calibrate it ONCE on the real battery fleet (anchored to BNetzA ~100 EUR/MWh)
   and reuse that exact constant for the grid (`WedgeConfig.alpha_additive`).

2. **Score is per electrical bus.** The PTDF kernel makes the score a function of
   the snapped transmission bus, so a fine lat/lon grid produces at most
   `n_unique_buses` distinct values (a Voronoi tessellation of bus service areas).
   We exploit this: only each unique bus is scored, then broadcast back to the
   grid cells. Cheap, and honest about the model's true spatial resolution.

Run:  python -m battery_siting_score.score_grid
      python -m battery_siting_score.score_grid --step-deg 0.1 --mask-to-de
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

if __package__ in (None, ""):
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE.parent))
    from battery_siting_score.data_load import (
        load_batteries, load_redispatch, load_prices_quarterhourly,
        filter_to_window, expand_events_to_quarterhours,
    )
    from battery_siting_score.network import build_network, snap_points_to_buses
    from battery_siting_score.wedge import build_wedge, WedgeConfig
    from battery_siting_score.score_lp import STEPS_PER_YEAR
    from battery_siting_score.score_proxy import annual_proxy, proxy_copperplate_per_mw
else:
    from .data_load import (
        load_batteries, load_redispatch, load_prices_quarterhourly,
        filter_to_window, expand_events_to_quarterhours,
    )
    from .network import build_network, snap_points_to_buses
    from .wedge import build_wedge, WedgeConfig
    from .score_lp import STEPS_PER_YEAR
    from .score_proxy import annual_proxy, proxy_copperplate_per_mw


# Mainland-Germany bounding box (deg). Generous; masked to the actual polygon if
# the Bundesländer GeoJSON is available.
DE_BBOX = dict(lon_min=5.8, lon_max=15.1, lat_min=47.2, lat_max=55.1)
GEOJSON_URL = ("https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON/"
               "main/2_bundeslaender/2_hoch.geo.json")


def _wcfg_from_cfg(cfg: dict) -> WedgeConfig:
    return WedgeConfig(
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


def _make_grid(step_deg: float) -> pd.DataFrame:
    lons = np.arange(DE_BBOX["lon_min"], DE_BBOX["lon_max"] + 1e-9, step_deg)
    lats = np.arange(DE_BBOX["lat_min"], DE_BBOX["lat_max"] + 1e-9, step_deg)
    LON, LAT = np.meshgrid(lons, lats)
    grid = pd.DataFrame({"lon": LON.ravel(), "lat": LAT.ravel()})
    print(f"[grid] bbox grid: {len(lons)} lon x {len(lats)} lat = {len(grid)} cells "
          f"@ {step_deg} deg (~{step_deg*111:.0f} km lat)")
    return grid


def _mask_to_germany(grid: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    """Keep only grid cells inside the German land polygon (needs geopandas)."""
    try:
        import urllib.request
        import geopandas as gpd
        from shapely.geometry import Point
    except Exception as e:
        print(f"[grid] mask skipped (geopandas/shapely unavailable: {e}); keeping bbox grid")
        return grid
    geo = out_dir / "bundeslaender.geojson"
    if not geo.exists():
        try:
            urllib.request.urlretrieve(GEOJSON_URL, geo)
            print(f"[grid] downloaded boundary -> {geo.name}")
        except Exception as e:
            print(f"[grid] mask skipped (no boundary, download failed: {e}); keeping bbox grid")
            return grid
    states = gpd.read_file(geo)
    de = states.union_all() if hasattr(states, "union_all") else states.unary_union
    pts = gpd.GeoSeries([Point(xy) for xy in zip(grid["lon"], grid["lat"])], crs=states.crs)
    inside = pts.within(de).to_numpy()
    out = grid[inside].reset_index(drop=True)
    print(f"[grid] masked to DE polygon: {len(grid)} -> {len(out)} cells")
    return out


def main(cfg_path: str, step_deg: float, mask_to_de: bool, sample_fleet: int | None):
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if str(cfg["wedge"].get("mode", "")).lower().strip() != "additive":
        print("[warn] wedge.mode is not 'additive'; alpha-freezing only matters for additive mode. "
              "Proceeding, but the fleet coupling this script removes is specific to additive.")

    out_dir = Path(cfg["paths"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80, "\n[1/6] LOAD")
    red = load_redispatch(cfg["paths"]["redispatch_csv"])
    prices_q = load_prices_quarterhourly(cfg["paths"]["price_csv"])
    fleet = load_batteries(
        cfg["paths"]["battery_csv"],
        status_filter=cfg["battery"]["status_filter"],
        drop_nan_coords=cfg["battery"]["drop_nan_coords"],
    )
    if sample_fleet:
        fleet = fleet.sample(n=int(sample_fleet), random_state=0).reset_index(drop=True)
        print(f"[fleet] sampled to {len(fleet)} for the alpha calibration")

    print("=" * 80, "\n[2/6] WINDOW + EXPAND")
    w_start = pd.Timestamp(cfg["window"]["start_utc"])
    w_end = pd.Timestamp(cfg["window"]["end_utc"])
    red_w, prices_w = filter_to_window(red, prices_q, w_start, w_end)
    red_steps = expand_events_to_quarterhours(red_w, prices_w)
    p_base_arr = prices_w.to_numpy()
    n_years = len(p_base_arr) / STEPS_PER_YEAR
    D_h = float(cfg["battery"]["duration_hours"])

    print("=" * 80, "\n[3/6] NETWORK + SNAP")
    net = build_network(cfg["paths"]["buses_csv"], cfg["paths"]["lines_csv"])
    fleet_bus = snap_points_to_buses(fleet["lon"].to_numpy(), fleet["lat"].to_numpy(), net)

    print("=" * 80, "\n[4/6] CALIBRATE ALPHA ON THE FLEET (then freeze)")
    wcfg = _wcfg_from_cfg(cfg)
    # Calibrate exactly as run.py does: build the fleet wedge with alpha_additive=None.
    # build_wedge writes the calibrated alpha back into wcfg.alpha_additive.
    _pi_fleet, _wedge_fleet = build_wedge(red_steps, prices_w, fleet_bus, net, wcfg)
    alpha_frozen = wcfg.alpha_additive
    print(f"[calib] frozen alpha = {alpha_frozen:.6g} EUR/MWh per (MW-kernel unit) "
          f"(calibrated on {len(fleet)} fleet points, target {wcfg.calib_price_eur_per_mwh} EUR/MWh)")
    del _pi_fleet, _wedge_fleet

    print("=" * 80, "\n[5/6] GRID + SCORE (frozen alpha, fixed 50 MW)")
    grid = _make_grid(step_deg)
    if mask_to_de:
        grid = _mask_to_germany(grid, out_dir)
    grid_bus = snap_points_to_buses(grid["lon"].to_numpy(), grid["lat"].to_numpy(), net)
    grid["bus_idx"] = grid_bus

    # Score each UNIQUE bus once (the PTDF score is a function of the bus), then
    # broadcast back to grid cells. unique_buses is the minimal point set.
    unique_buses = np.unique(grid_bus)
    print(f"[grid] {len(grid)} cells snap to {len(unique_buses)} unique buses "
          f"-> scoring {len(unique_buses)} points")

    wcfg_grid = replace(wcfg, alpha_additive=float(alpha_frozen))
    pi_hat_u, _wedge_u = build_wedge(red_steps, prices_w, unique_buses, net, wcfg_grid)

    score_mw = float(cfg.get("baseline", {}).get("fixed_mw", 50.0))
    V_cp_proxy_per_mw = proxy_copperplate_per_mw(p_base_arr, D_h)   # constant, fleet-independent
    V_cp = V_cp_proxy_per_mw * score_mw

    V_u = np.array([annual_proxy(pi_hat_u[:, j], score_mw, D_h) for j in range(len(unique_buses))])
    dV_u = V_u - V_cp

    # Map per-bus scores back to grid cells.
    pos = np.searchsorted(unique_buses, grid_bus)
    grid["V_proxy_eur"] = V_u[pos]
    grid["dV_proxy_eur"] = dV_u[pos]
    grid["V_proxy_eur_per_year"] = grid["V_proxy_eur"] / max(n_years, 1e-9)
    grid["dV_proxy_eur_per_year"] = grid["dV_proxy_eur"] / max(n_years, 1e-9)

    print("=" * 80, "\n[6/6] WRITE")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_csv = out_dir / f"grid_siting_scores_{stamp}.csv"
    grid.to_csv(out_csv, index=False)
    meta = {
        "kind": "grid_siting_surface",
        "alpha_frozen_eur_per_mwh_per_unit": float(alpha_frozen),
        "calibrated_on_fleet_points": int(len(fleet)),
        "calib_price_eur_per_mwh": float(wcfg.calib_price_eur_per_mwh),
        "score_power_mw": score_mw,
        "duration_hours": D_h,
        "window_start_utc": str(w_start),
        "window_end_utc": str(w_end),
        "n_years": float(n_years),
        "step_deg": float(step_deg),
        "masked_to_de": bool(mask_to_de),
        "n_grid_cells": int(len(grid)),
        "n_unique_buses": int(len(unique_buses)),
        "wedge_mode": wcfg.mode,
        "kernel_type": wcfg.kernel_type,
        "clip_eur_per_mwh": wcfg.clip_eur_per_mwh,
        "V_copperplate_proxy_eur_per_year": float(V_cp / max(n_years, 1e-9)),
        "source_csv": out_csv.name,
    }
    (out_dir / f"grid_siting_scores_{stamp}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[out] -> {out_csv}")
    print(f"[out] -> {out_csv.with_suffix('').name}.meta.json")
    print(f"[summary] dV/yr range over grid: {grid['dV_proxy_eur_per_year'].min():,.0f} "
          f".. {grid['dV_proxy_eur_per_year'].max():,.0f} EUR/yr @ {score_mw:g} MW "
          f"({(grid['dV_proxy_eur_per_year']<0).mean()*100:.0f}% negative)")
    return grid


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--step-deg", type=float, default=0.1,
                        help="grid spacing in degrees (~111 km per deg of latitude); default 0.1 (~11 km)")
    parser.add_argument("--mask-to-de", action="store_true", default=True,
                        help="keep only cells inside the German land polygon (needs geopandas)")
    parser.add_argument("--no-mask-to-de", dest="mask_to_de", action="store_false",
                        help="keep the full bounding-box grid (no polygon mask)")
    parser.add_argument("--sample-fleet", type=int, default=None,
                        help="sample N fleet points for the alpha calibration (debug; default all)")
    args = parser.parse_args()
    main(args.config, step_deg=args.step_deg, mask_to_de=args.mask_to_de, sample_fleet=args.sample_fleet)
