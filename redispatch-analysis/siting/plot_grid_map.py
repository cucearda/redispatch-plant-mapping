"""Render the artificial-grid siting scores as a Germany heatmap (PNG).

Standalone: reads a ranking CSV produced by `battery_siting_score.run` (with
battery.source='grid') and draws the per-cell locational value dV as a heatmap
over Germany. Does NOT touch the scoring pipeline — run it after run.py.

Usage:
    python -m battery_siting_score.plot_grid_map            # latest CSV in out/
    python -m battery_siting_score.plot_grid_map --csv path/to/scores.csv
    python -m battery_siting_score.plot_grid_map --col dV_proxy_eur_per_year

Because the grid source lays cells on a regular lat/lon lattice, the points are
pivoted back into a 2D array (missing/clipped cells = NaN) and drawn with
pcolormesh, giving a clean raster heatmap rather than a scatter.
"""
from __future__ import annotations
import argparse
import math
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "out"


def _latest_csv(out_dir: Path) -> Path:
    csvs = sorted(out_dir.glob("battery_siting_scores_*.csv"),
                  key=lambda p: p.stat().st_mtime)
    if not csvs:
        raise FileNotFoundError(f"No battery_siting_scores_*.csv in {out_dir}")
    return csvs[-1]


def _pick_value_col(df: pd.DataFrame, col: str | None) -> str:
    """Choose the value column: explicit --col, else LP dV if populated, else proxy dV."""
    if col is not None:
        if col not in df.columns:
            raise KeyError(f"--col {col!r} not in CSV columns: {list(df.columns)}")
        return col
    for c in ("dV_eur_per_year", "dV_proxy_eur_per_year"):
        if c in df.columns and df[c].notna().any():
            return c
    raise KeyError("No dV_eur_per_year or dV_proxy_eur_per_year column found.")


def plot_grid_map(csv_path: Path, value_col: str | None = None,
                  out_png: Path | None = None) -> Path:
    df = pd.read_csv(csv_path)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col])

    # Reconstruct the regular lattice: unique sorted lon/lat (rounded to absorb
    # float wobble), then scatter each cell's value into a 2D grid.
    lon = np.round(df["lon"].to_numpy(), 4)
    lat = np.round(df["lat"].to_numpy(), 4)
    ulon = np.unique(lon)
    ulat = np.unique(lat)
    li = {v: i for i, v in enumerate(ulon)}
    la = {v: i for i, v in enumerate(ulat)}
    grid = np.full((len(ulat), len(ulon)), np.nan)
    vals = df[col].to_numpy()
    for k in range(len(df)):
        grid[la[lat[k]], li[lon[k]]] = vals[k]

    # Cell edges for pcolormesh (lattice is regular, so half-step out on each side).
    dlon = np.median(np.diff(ulon)) if len(ulon) > 1 else 0.25
    dlat = np.median(np.diff(ulat)) if len(ulat) > 1 else 0.25
    lon_edges = np.concatenate([ulon - dlon / 2, [ulon[-1] + dlon / 2]])
    lat_edges = np.concatenate([ulat - dlat / 2, [ulat[-1] + dlat / 2]])

    val_eur_m = grid / 1e6  # EUR/yr -> million EUR/yr for readability

    fig, ax = plt.subplots(figsize=(7.5, 8.5))
    mesh = ax.pcolormesh(lon_edges, lat_edges, val_eur_m,
                         cmap="viridis", shading="flat")
    cb = fig.colorbar(mesh, ax=ax, shrink=0.8, pad=0.02)
    cb.set_label(f"{col}\n(million EUR / yr, per 50 MW candidate)")

    # Mark the top-10 sites.
    top = df.sort_values(col, ascending=False).head(10)
    ax.scatter(top["lon"], top["lat"], s=40, facecolors="none",
               edgecolors="red", linewidths=1.4, label="top-10 sites")

    # Approximate Germany aspect ratio at mid-latitude.
    lat_mid = float(np.mean(ulat))
    ax.set_aspect(1.0 / math.cos(math.radians(lat_mid)))
    ax.set_xlabel("longitude [deg E]")
    ax.set_ylabel("latitude [deg N]")
    ax.set_title("Battery siting value across Germany\n"
                 f"(artificial grid, {len(df)} candidate cells)")
    ax.legend(loc="upper right", framealpha=0.9)
    fig.tight_layout()

    if out_png is None:
        out_png = csv_path.with_name(csv_path.stem + "_heatmap.png")
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"[plot] value column: {col}")
    print(f"[plot] {len(df)} cells, lattice {len(ulat)} x {len(ulon)}")
    print(f"[plot] -> {out_png}")
    return out_png


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="scores CSV (default: latest in out/)")
    ap.add_argument("--col", default=None,
                    help="value column (default: dV_eur_per_year if populated, else dV_proxy_eur_per_year)")
    ap.add_argument("--out", default=None, help="output PNG path")
    args = ap.parse_args()
    csv_path = Path(args.csv) if args.csv else _latest_csv(DEFAULT_OUT_DIR)
    plot_grid_map(csv_path, value_col=args.col,
                  out_png=(Path(args.out) if args.out else None))
