"""
Illustrative nodal-price (LMP) model for Germany — SciGRID-DE baseline.
=======================================================================

Purpose
-------
Germany is a single day-ahead bidding zone, so there is NO observable nodal
price to download. This script produces *model-based* locational marginal
prices (LMPs) as the shadow prices of each bus's energy-balance constraint in
a linear optimal power flow (LOPF). They serve as an independent, model-based
proxy for grid congestion, to be cross-checked against the OENB redispatch
volumes in the seminar paper.

IMPORTANT SCOPE / DEFENSIBILITY NOTES (for the limitations section)
-------------------------------------------------------------------
1. The SciGRID-DE network + load/generation snapshot reflects a 2011 topology
   and a single representative day (24 hourly snapshots, 2011-01-01). It is a
   recognised *teaching / illustrative* model, not a current market simulation.
2. Nodal prices are shadow prices of a reference network model, NOT observed
   market outcomes (the real DE market clears at one uniform zonal price).
3. Short-run marginal costs are assigned per carrier from a documented merit
   order (see MARGINAL_COST below), not from real fuel/CO2 time series.
4. Line capacities are de-rated by a contingency factor (0.7) as a simple
   proxy for N-1 security, following the standard PyPSA SciGRID tutorial.

The output is deliberately STATIC and DESCRIPTIVE (one representative day,
averaged), keeping within the paper's scope (no counterfactual/dispatch
modelling of battery placement).

Outputs (written next to this script)
-------------------------------------
- nodal_prices_bus.csv    : bus_id, lat, lon, plus per-bus mean LMP and the
                            congestion premium (LMP - system reference price).
- nodal_prices_map.png    : two-panel map — absolute LMP and congestion premium.
"""

from pathlib import Path
import warnings

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
import pypsa

warnings.filterwarnings("ignore")
OUT = Path(__file__).resolve().parent

# --- Documented short-run marginal cost assumptions [EUR/MWh] ----------------
# A simple, transparent merit order. Renewables & must-run hydro at ~0; thermal
# ordered by typical German short-run costs. Stated explicitly so the paper can
# cite the assumption rather than a black box.
MARGINAL_COST = {
    "Run of River": 0.0, "Solar": 0.0, "Wind Onshore": 0.0, "Wind Offshore": 0.0,
    "Geothermal": 0.0, "Storage Hydro": 0.0,
    "Nuclear": 8.0, "Waste": 20.0, "Brown Coal": 25.0, "Hard Coal": 35.0,
    "Gas": 55.0, "Other": 60.0, "Multiple": 60.0, "Oil": 90.0,
}
CONTINGENCY_FACTOR = 0.7      # de-rate line capacity as an N-1 proxy
VOLL = 3000.0                 # value of lost load [EUR/MWh] for load-shedding backstop


def build_network() -> pypsa.Network:
    n = pypsa.examples.scigrid_de()

    # Assign transparent per-carrier marginal costs (override example defaults).
    n.generators["marginal_cost"] = n.generators.carrier.map(MARGINAL_COST).fillna(60.0)

    # N-1 proxy: allow only a fraction of thermal line rating.
    n.lines["s_max_pu"] = CONTINGENCY_FACTOR

    # Load-shedding backstop at every bus guarantees feasibility and makes the
    # scarcity price well-defined (LMP caps at VOLL where load is shed).
    n.add(
        "Generator",
        n.buses.index + " load_shedding",
        bus=n.buses.index,
        carrier="load_shedding",
        marginal_cost=VOLL,
        p_nom=1e5,
    )
    return n


def solve(n: pypsa.Network, label: str = "nodal") -> None:
    status, condition = n.optimize(solver_name="highs")
    print(f"[{label}] solver status: {status} / {condition}")
    if status != "ok":
        raise RuntimeError(f"LOPF did not solve cleanly: {status}/{condition}")


def copperplate_reference() -> pd.Series:
    """Uniform zonal price per snapshot: same dispatch with transmission limits
    effectively removed. This is the single-bidding-zone clearing price against
    which the congestion component is measured."""
    n = build_network()
    n.lines["s_max_pu"] = 1e4          # relax line limits -> copperplate
    n.transformers["s_max_pu"] = 1e4
    solve(n, label="copperplate")
    # All buses share the same price when unconstrained; take the median per hour.
    ref = n.buses_t.marginal_price.median(axis=1)
    print("copperplate (uniform) price per hour [EUR/MWh]: "
          f"min {ref.min():.1f}, mean {ref.mean():.1f}, max {ref.max():.1f}")
    return ref


def collect_prices(n: pypsa.Network, ref_price: pd.Series) -> pd.DataFrame:
    lmp = n.buses_t.marginal_price                      # snapshots x buses [EUR/MWh]
    mean_lmp = lmp.mean(axis=0)                          # per-bus daily mean

    # Pure congestion component per snapshot, then averaged: LMP - uniform price.
    premium_t = lmp.sub(ref_price, axis=0)
    mean_premium = premium_t.mean(axis=0)

    # Share of hours a bus is at/above the scarcity price (load-shedding flag).
    shed_share = (lmp >= VOLL - 1.0).mean(axis=0)

    df = pd.DataFrame(
        {
            "bus_id": mean_lmp.index,
            "lat": n.buses.loc[mean_lmp.index, "y"].values,
            "lon": n.buses.loc[mean_lmp.index, "x"].values,
            "lmp_mean_eur_mwh": mean_lmp.values,
            "lmp_min_eur_mwh": lmp.min(axis=0).reindex(mean_lmp.index).values,
            "lmp_max_eur_mwh": lmp.max(axis=0).reindex(mean_lmp.index).values,
            "congestion_premium_eur_mwh": mean_premium.reindex(mean_lmp.index).values,
            "load_shed_hours_share": shed_share.reindex(mean_lmp.index).values,
        }
    )
    df.attrs["system_ref"] = float(ref_price.mean())
    print(f"reference uniform price (24h mean): {df.attrs['system_ref']:.2f} EUR/MWh")
    n_shed = int((df["load_shed_hours_share"] > 0).sum())
    print(f"buses shedding load in >=1 hour: {n_shed} / {len(df)}")
    return df


def draw_map(n: pypsa.Network, df: pd.DataFrame) -> None:
    # Line segments for context.
    b = n.buses
    segs = [
        [(b.at[l.bus0, "x"], b.at[l.bus0, "y"]), (b.at[l.bus1, "x"], b.at[l.bus1, "y"])]
        for _, l in n.lines.iterrows()
        if l.bus0 in b.index and l.bus1 in b.index
    ]

    fig, axes = plt.subplots(1, 2, figsize=(15, 8))
    panels = [
        ("lmp_mean_eur_mwh", "Nodal price (daily mean LMP)", "viridis", None),
        ("congestion_premium_eur_mwh", "Congestion premium (LMP - system ref.)", "RdBu_r", "sym"),
    ]
    for ax, (col, title, cmap, mode) in zip(axes, panels):
        ax.add_collection(LineCollection(segs, colors="0.8", linewidths=0.4, zorder=1))
        vals = df[col].values
        if mode == "sym":
            v = np.nanpercentile(np.abs(vals), 98) or 1.0
            vmin, vmax = -v, v
        else:
            vmin, vmax = np.nanpercentile(vals, 2), np.nanpercentile(vals, 98)
        sc = ax.scatter(df.lon, df.lat, c=vals, cmap=cmap, vmin=vmin, vmax=vmax,
                        s=14, zorder=2, edgecolors="none")
        fig.colorbar(sc, ax=ax, shrink=0.7, label="EUR/MWh")
        ax.set_title(title)
        ax.set_xlabel("lon"); ax.set_ylabel("lat")
        ax.set_aspect(1.4)
    fig.suptitle(
        "SciGRID-DE illustrative nodal prices — 2011-01-01, 24h mean "
        "(model-based shadow prices, not observed market data)",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(OUT / "nodal_prices_map.png", dpi=150, bbox_inches="tight")
    print(f"wrote {OUT / 'nodal_prices_map.png'}")


def main() -> None:
    ref_price = copperplate_reference()
    n = build_network()
    solve(n, label="nodal")
    df = collect_prices(n, ref_price)
    df.to_csv(OUT / "nodal_prices_bus.csv", index=False)
    print(f"wrote {OUT / 'nodal_prices_bus.csv'}  ({len(df)} buses)")
    print(df["lmp_mean_eur_mwh"].describe().round(2).to_string())
    draw_map(n, df)


if __name__ == "__main__":
    main()
