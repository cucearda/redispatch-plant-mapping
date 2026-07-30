# PyPSA nodal-price baseline (SciGRID-DE)

A **low-effort, illustrative** locational-marginal-price (LMP) model for Germany,
built to cross-check the ÜNB redispatch congestion signal in the seminar paper.

## What it is / isn't
- **Is:** shadow prices (`buses_t.marginal_price`) of a linear optimal power flow
  on the SciGRID-DE network (585 buses, 852 lines), 24 hourly snapshots of
  2011-01-01. A recognised teaching model, run with the open HiGHS solver.
- **Isn't:** a market simulation. Germany is a single bidding zone — real prices
  are uniform, so nodal prices exist **only** as a model output. Don't validate
  against observed prices.

## Run
```bash
python pypsa_nodal_prices.py      # ~30 s, needs pypsa + highspy (already installed)
```

## Outputs
- `nodal_prices_bus.csv` — one row per bus: `bus_id, lat, lon, lmp_mean_eur_mwh,
  lmp_min/max, congestion_premium_eur_mwh, load_shed_hours_share`.
  Join on `lat/lon` (or nearest bus) to overlay on the redispatch/colocation maps.
- `nodal_prices_map.png` — left: absolute mean LMP; right: **congestion premium**
  = LMP − copperplate (uniform zonal) price. The premium panel is the one to use.

## Key results (this run)
- Copperplate / uniform reference price: **≈23 €/MWh** (marginal unit ≈ brown coal).
- Only **2 / 585** buses shed load → clean, well-posed prices.
- Spatial pattern: **north below** the uniform price (trapped wind surplus),
  **south/west above** it (import-dependent load) — the classic north→south
  congestion gradient that also drives real redispatch.

## Key assumptions (state these in the paper)
- Per-carrier short-run marginal costs from a documented merit order (`MARGINAL_COST`
  in the script), not real fuel/CO₂ time series.
- Line ratings de-rated by a 0.7 contingency factor as an N-1 proxy (standard
  PyPSA SciGRID tutorial choice).
- Load-shedding backstop at every bus (VOLL = 3000 €/MWh) guarantees feasibility.

## Limitations to acknowledge
1. 2011 topology + single representative day — illustrative, not current.
2. Shadow prices ≠ observed market outcomes.
3. Stylised marginal costs; no storage/ramping/unit-commitment detail.

## If a reviewer pushes on currency
The current-data equivalent is **PyPSA-Eur** clustered to Germany, but that is a
multi-day Snakemake + weather-data workflow — out of proportion for a 10–15 page
paper. This SciGRID snapshot is the defensible lightweight substitute.
