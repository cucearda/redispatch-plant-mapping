# Battery locational-siting scorer

Scores each currently-deployed German battery by the **annual arbitrage profit** it
would earn against a local effective price π̂[n,t] formed by bending the zonal
intraday price with a kernel-weighted sum of nearby redispatch events. The result
is a per-battery ΔV[n] = V[n] − V_copperplate[n] in EUR/yr that directly tests
whether the deployed fleet sits in high-locational-value locations.

**Resolution: quarter-hourly (15-min) end to end.** Prices are used at their native
15-min grid (no hourly resampling), redispatch events are expanded onto that same
grid, and the daily arbitrage LP cycles over **96 steps/day with dt = 0.25 h**. A
4 h battery (E = 4·p̄) charges/discharges over 16 steps at full power. Annualization
uses `STEPS_PER_YEAR = 96·365 = 35 040`.

This is price-taker storage valuation (locational marginal value of storage),
with redispatch substituted for the nodal price Germany's single bidding zone
hides.

## Files

```
config.yaml          # all knobs (paths, kernel, wedge, LP, window)
data_load.py         # CSVs -> tz-aware UTC, sign-mapped, native 15-min grid, joined
network.py           # PyPSA-Eur graph, snapping, Dijkstra, quartic/gaussian kernel
                     #   (lifted from Final_Analysis/make_redispatch_grid_kde_area_map.py)
ptdf.py              # PyPSA DC PTDF kernel: K_h(||PTDF[:,n] - PTDF[:,g]||_2)
wedge.py             # build pi_hat = p_base + wedge per (15-min step, battery)
score_lp.py          # cyclic-SoC daily arbitrage LP (96 steps, dt=0.25) via scipy.linprog HiGHS
score_proxy.py       # top-k vs bottom-k pi_hat screening proxy
rank.py              # ProcessPoolExecutor LP, V/dV, diagnostics, ranked CSV
run.py               # orchestration entry point
tests/
  test_acceptance.py # the 3 prompt-mandated acceptance tests
```

## Run

```bash
cd ALLES_NEU
python -m battery_siting_score.run                    # full fleet
python -m battery_siting_score.run --sample 50        # quick subset
python -m battery_siting_score.tests.test_acceptance  # smoke tests
```

Outputs `out/battery_siting_scores.csv`, ranked by ΔV/yr.

## Pipeline

1. **Load** batteries (active only, drop NaN coords), redispatch (drop bad-duration
   rows), prices (native 15-min UTC, reindexed to a gap-free quarter-hour grid).
2. **Timezone-normalize** redispatch: `ts_start` is wall-clock per
   `ZEITZONE_VON ∈ {UTC, CET, CEST}`. Localize per-row, convert to UTC. Naive
   merge would silently misalign ~67% of rows.
3. **Restrict** to the configured window (default 2020-01-01 → 2025-12-31 23:45, 6
   full calendar years where prices and redispatch both have full coverage). Events
   are expanded into the 15-min steps they overlap.
4. **Build the graph** — PyPSA-Eur DE 380/220 kV buses + lines, bridge
   co-located voltage levels via OSM base_id (identical to the network-KDE map).
5. **Snap** each battery and each redispatch event to its nearest bus.
6. **Wedge**: for each 15-min step t and each battery n,
   `wedge[n,t] = wedge_scale · p_base[t] · Σ_g K_h(graph_dist(n, bus(g))) · sign[g]`
   over redispatch events g active in step t, clipped to ±`clip_eur_per_mwh`.

   Kernel is selectable in `config.yaml`:
   - `gaussian`: `K(d) = exp(−½(d/σ)²)`, no hard cutoff, Dijkstra cutoff
     defaults to 4 σ. Broader spatial reach than the quartic at the same bandwidth.
     Distance `d` = Dijkstra graph distance over the PyPSA-Eur DE topology, in metres.
   - `quartic`: `K(d) = (1 − (d/h)²)²` for d < h, else 0. Identical to the kernel in
     `Final_Analysis/make_redispatch_grid_kde_area_map.py`; use this for direct
     consistency with the project's existing KDE map. Same graph-distance metric.
   - `ptdf` (default): the rigorous electrical-distance version. `d_elec(n,g) =
     ‖PTDF[:,n] − PTDF[:,g]‖₂` where PTDF is the DC Power Transfer Distribution Factor
     matrix from a `pypsa.Network` built off the same DE 220/380 kV topology.
     The Gaussian/quartic *shape* is then applied to `d_elec`. DC PTDF column-difference
     is slack-invariant, so the choice of slack inside each sub-network does not matter.
     Cross-sub-network pairs (disconnected islands) are forced to zero. Bandwidth is
     dimensionless (`ptdf_bandwidth`, default 0.5) — PTDF entries are in [-1,1] so
     ‖diff‖ over the active lines is typically O(0.1)-O(10).

   The `ptdf` kernel replaces the graph-distance proxy with the actual electrical
   coupling between buses: two buses with identical PTDF columns shift every line
   by the same amount per unit injection, so a battery at one is a perfect substitute
   for a redispatch action at the other. The graph-distance kernels were a proxy for
   exactly this; the PTDF kernel is the direct version.

   Default bandwidth for graph kernels is `h = σ = 100 km`. Ranking is invariant to
   `wedge_scale` and weakly sensitive to bandwidth/kernel-shape (kernel choice mostly
   perturbs ΔV magnitude, with some reordering of close-ranked batteries).
7. **Daily LP** per battery, per day (96 steps of length dt = 0.25 h):
   ```
   maximize  Σ_t  π̂[t] · (d[t] − c[t]) · dt
   subject to  s[(t+1) mod 96] = s[t] + (η·c[t] − d[t]/η)·dt   (cyclic SoC)
               0 ≤ c[t], d[t] ≤ p̄          (peak power from MaStR)
               0 ≤ s[t]       ≤ E           (E = 4·p̄, duration fixed at 4 h)
   ```
   with η = √η_roundtrip ≈ 0.922. Solved via `scipy.optimize.linprog(method='highs')`.
   c[t], d[t] are power (MW); the dt factor turns them into energy (MWh) per step so
   the SoC balance and revenue are correct at 15-min resolution. Sum daily optima →
   V[n] (EUR per the configured window).
8. **Baseline V_copperplate**: solve the same LP with π̂ = p_base (spatially
   uniform → one number per MW, applied to every battery's p̄).
9. **ΔV[n] = V[n] − V_copperplate[n]**. Ranking by V and by ΔV is identical (the
   baseline is a per-battery constant `V_cp_per_mw · p̄`), but ΔV is the
   interpretable "locational value created by congestion" number.
10. **Diagnostics**: steps-with-nonzero-wedge, mean|wedge|, frac up/down nearby,
    fast top-k vs bottom-k proxy (k = duration/dt = 16 steps; Spearman vs LP).

## Sign mapping

Read empirically from `joined_high_confidence.csv` (after utf-8-sig decode +
`.str.strip()`):

| `RICHTUNG`                              | rows  | sign | interpretation       |
|-----------------------------------------|-------|------|----------------------|
| Wirkleistungseinspeisung **erhöhen**    | 41717 |  +1  | grid short → discharge |
| Wirkleistungseinspeisung **reduzieren** | 42127 |  −1  | grid surplus → charge  |

Asserted at load time: every row maps to exactly one sign or the load fails loudly.

## Cleaning policy (logged at load time)

- 56 redispatch rows with `duration_h ≤ 0` (legacy BEGINN/ENDE-swap bug, all
  pre-2018 hence outside the price window anyway) — **dropped**.
- 11 redispatch rows with DST-impossible wall-clocks (e.g. "31.10.2021 00:00 CEST"
  on a date where 00:00 CEST doesn't exist post-switch) — **dropped**.
- 25 batteries with NaN `lat/lon` (21 active + 3 planning + 1 decommissioned,
  median 0.15 MW residential MaStR entries with privacy-suppressed coords, ~41 MW
  of a ~31 GW fleet) — **dropped**.

## Modeling caveats (state these in the paper)

1. **Electrical distance.** With `kernel: type: ptdf` we use ‖PTDF[:,n] − PTDF[:,g]‖₂
   from a PyPSA DC PTDF over the DE 220/380 kV topology — the closest fully rigorous
   alternative to a `per_constraint` formulation, which would require binding-line IDs
   the redispatch dataset does not expose. The legacy `gaussian`/`quartic` options keep
   graph distance as the proxy for backwards compatibility with the KDE map kernel.
2. **`wedge_scale` affects magnitude, not ranking.** The Spearman rank correlation
   of V across batteries is invariant to any positive scalar on the wedge. The
   default `wedge_scale=0.05` keeps the wedge well within the ±200 €/MWh clip
   for all 6 years; raising it amplifies dV in absolute EUR but reorders nothing.
   The wedge magnitude uses the joined intraday price `p_base[t]` as `mc[g,t]`
   (since `joined_high_confidence.csv` has no monetary column and BNetzA does
   not publish per-event redispatch costs). Per-event MW is not used as a
   magnitude weight — an obvious extension.
3. **Redispatch is the pre-battery counterfactual on an expanding grid.** V[n]
   scores **today's** congestion, not the congestion a battery built today would
   face for its 15-year operating life. Net-grid expansion (Suedlink, Suedostlink,
   etc.) is expected to reduce redispatch volumes; ÜNB data also misses DSO-level
   redispatch, which matters most for distribution-connected batteries.

## Acceptance tests

```
python -m battery_siting_score.tests.test_acceptance
```

1. **Golden lossless LP**: `p̄=1, E=4, η=1, dt=0.25, π=[10]*16+[90]*16` →
   profit = 320 (charge 16 steps@10, discharge 16 steps@90). Exact equality required.
2. **Sign test**: feeding a 96-step π̂ with one very-cheap step and one
   very-expensive step forces the LP to charge cheap and discharge expensive,
   clearing at least `dt·spread` over the baseline `flat-π → 0`.
3. **Three-battery ordering**: synthetic 96-step π̂ for A (down-ramps only), B
   (straddle, cancels), C (up- and down-ramps in different steps) → V_C > V_A > V_B
   with V_B = V_copperplate.
