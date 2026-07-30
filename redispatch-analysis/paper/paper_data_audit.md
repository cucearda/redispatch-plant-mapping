# Data audit: paper (latex_1_-18.pdf, 22 July 2026) vs. data & notebooks

Every number below was recomputed from `redispatch_joined.parquet`,
`redispatch_joined_high_confidence.csv`, `battery_siting_score/out/*`,
`results/*` and `intraday_prices_de_15min.csv`.

---

## A. Mismatches that need fixing

### A1. §5.1 regional figures use a different (unfiltered) dataset — CRITICAL

Paper: North ≈27,800 down / 40,000 GWh, 5,800 up / 8,500 GWh; South ≈25,900 up /
36,000 GWh, 3,400 down / 2,500 GWh; Centre in between. Sum ≈102,000 events —
but the analysis set is stated as 83,716.

Cause: `redispatch_volume_analysis.ipynb` cell 30 builds Part 2 as
`rd.merge(matcher_slim, how='inner').dropna(subset=['lat','lon'])` →
**101,793 events, no confidence filter, no reason filter**. Figure 2 is drawn on
that. The 83,716 comes from the exported CSV, which applies conf > 0.5 **and**
the reason filter.

| | events (no filter, n=101,793) | paper | conf>0.5 (86,561) | export CSV (83,716) |
|---|---|---|---|---|
| North down | 27,574 / 40,124 GWh | ~27,800 / 40,000 | 22,171 / 34,930 | 21,925 / 34,856 |
| North up | 5,875 / 8,489 | ~5,800 / 8,500 | 5,508 / 8,422 | 5,206 / 8,340 |
| Centre down/up | 22,003 / 17,203 | ~39,300 total | 17,780 / 15,578 | 17,225 / 14,587 |
| South up | 25,791 / 35,990 | ~25,900 / 36,000 | 22,695 / 33,275 | 22,014 / 32,178 |
| South down | 3,347 / 2,738 | ~3,400 / 2,500 | 2,829 / 2,303 | 2,759 / 2,288 |

The qualitative north/south asymmetry survives the filter, so only the numbers
need restating (recommended: rerun Figure 2 on the 83,716 export).

Secondary: `lat_band()` splits at lat 52.0 / 49.5 — these are **latitude bands**,
not "control areas" as §5.1 calls them.

### A2. 83,716 kept vs. 36,801 excluded do not add up — CRITICAL

123,362 − 83,716 = **39,646**, but §4.1 decomposes **36,801** exclusions
(19,107 + 15,236 + 2,316 + residual).

Cause: 123,362 − 36,801 = 86,561 = exactly the count at conf > 0.5 **without**
the reason filter. The exported CSV additionally drops 2,845 events —
Probefahrt / Testfahrt (bnBm) / Probestart (NetzRes) / Testfahrt (KapRes) /
Funktionstest and one countertrade batch — whose mean confidence is 0.97, i.e.
they were matched fine but removed as test runs.
So Figure 5 silently omits a fourth exclusion category (~2,845 events).

Also affected: §4.1 says 83,716 = 67.9% of events and 120.6 TWh = 66.3%.
Verified on the export: 83,716 events (67.86%), 120.601 TWh (66.25%) ✓ internally
consistent — it is the exclusion figure that is on the other basis.

### A3. Figure 8 uses confidence > 0.4, the analysis uses > 0.5 — CRITICAL

The reported TSO rates reproduce **exactly at > 0.4**, so the *text* is right and the
*caption* is wrong. But every other number in the paper rests on the EXPORT set
(conf > 0.5 **and** the reason filter), where the rates are 5–20 pp lower
(authoritative three-basis table: `paper_matching_figures.ipynb`, section 6):

| TSO | paper (events/plants/MWh) | conf > 0.4 | EXPORT basis |
|---|---|---|---|
| Amprion | 83 / 64 / 86 | 83 / 64 / 86 ✓ | 71 / 51 / 79 |
| TransnetBW | 86 / 71 / 91 | 86 / 71 / 91 ✓ | 83 / 62 / 88 |
| 50Hertz | plants 20% | 70 / 20 / 60 ✓ | 65 / 13 / 59 |
| TenneT DE | "66% and 60%" | 66 / 82 / 60 | 60 / 60 / 57 |

The numbers move by up to 10 pp, so this is not cosmetic. Also: the paper writes
"TenneT's comparatively low MWh and event match rates (66% and 60%)" — the labels
are swapped, 66% is events and 60% is MWh.

### A4. §5.2 geography contradicts the paper's own map and score data — CRITICAL

Paper §5.2: "the highest locational value is concentrated ... in the northern
regions of Lower Saxony and Schleswig-Holstein ... and in a central-eastern band".
§6: "yet the north still scores highest."

Score data (`battery_siting_scores_additive_20260713_134830.csv`, 910 cells,
ΔV in M€/yr):

| latitude band | cells | mean ΔV | max |
|---|---|---|---|
| Centre (49.5–52°) | 335 | **8.81** | 15.78 |
| North (≥52°) | 336 | 6.58 | 13.26 |
| South (<49.5°) | 239 | 4.47 | 14.70 |

Of the top 91 cells (top 10%): **88 Centre, 2 South, 1 North.**
Rough state boxes: Saxony 11.63 > Thuringia 10.22 > Saxony-Anhalt 9.17 >
Lower Saxony 6.87 > Schleswig-Holstein 6.13 > Bavaria 5.68 > BW/SW 3.73.
Top cell = (51.00 N, 11.17 E), eastern Thuringia.

So §6's hotspot description (Thuringian–Bavarian border + Saxony/Saxony-Anhalt)
is the correct one; §5.2's "Lower Saxony and Schleswig-Holstein" claim and §6's
"the north still scores highest" are not supported. The north is mid-field.

### A5. Date range inconsistency

- Intro / Contribution / Conclusion: "2013 to 2025"
- §4: "2013 until 22 June 2026 — 123,362 events, 182.03 TWh"

Data: `ts_start` runs **2013-04-02 → 2026-06-22**, 123,362 rows, 182.0335 TWh
absolute. So §4 is the correct statement; intro/contribution/conclusion are wrong.
The scoring window is a separate thing (2021-10-01 → 2025-12-31, 4.255 years,
149,088 quarter-hours of price data — all confirmed).

Also: the Netztransparenz citation says "Accessed: 2026-06-09", which precedes
the data end of 22 June 2026. And "31.122025" is a typo for 31.12.2025.

### A6. Smaller inconsistencies

| # | Issue |
|---|---|
| a | §4.2: "theoretical batteries placed every **XX km**" — literal placeholder. Correct: 0.25° grid = 27.8 km N–S, ~17.5 km E–W at 51°N, 910 cells over Germany. |
| b | §5.3.1 refers to "the Gaussian kernel in Equation 1" — it is Equation 3. |
| c | "Table 19" is cited both for the confidence scores (§4.1) and the PyPSA configuration (§4.3). |
| d | §4.1: "roughly three quarters of all events matched with confidence above 0.8" — 56,216 + 20,553 = 76,769, which is 62% of all 123,362 events but 73% of the 105,668 events that received any match. Denominator needs stating. |
| e | α anchor (§4.2) uses "2.8 bn / 28 TWh in 2024" ≈ 100 €/MWh, while the Intro quotes "34 TWh and roughly EUR 3 billion in 2023" (≈88 €/MWh) for the same quantity. |
| f | §5.3.1 says H is "spaced from 0.1 to 10"; the sweep is **log**-spaced (`sweep_summary.csv`). The appendix text says rankings are compared against H = 0.954 while the body says the default is H = 1. |
| g | §5.3.1: "mean locational value rises from 1.2 to 6.1 M/yr" between H≈0.3 and 0.8; table rows are 1.49 (H=0.34) and 6.06 (H=0.79). |
| h | Appendix figure path is `figures/h_sweep_summary.png`, actual file is `battery_siting_score/out/h_sweep_20260715_175302/h_sweep_summary.png`. |
| i | Two different 17-node comparison files exist: `phase1b_LP_nodal_value_vs_siting_2025hourly.csv` (ρ=0.647, the one the paper reports) and `nodal_vs_siting_comparison_2025_30node.csv` (ρ=0.625, slightly different `sit_dV`). Pick one and delete/label the other. |
| j | The 10-node robustness check (ρ=0.48) has p=0.162 — not significant. Worth saying so rather than presenting 0.48→0.65 as a clean convergence. |
| k | The §5.2 map is rendered on the 0.25° grid (910 cells); the §5.3 overlay map uses the 0.1° grid (`results/siting_vs_nodal_overlay.html`, dlat 0.1). Two different resolutions across adjacent figures. |

---

## B. Verified correct — no action needed

| Claim | Recomputed |
|---|---|
| 123,362 events, 182.03 TWh, 2013–22 Jun 2026 | 123,362 / 182.0335 TWh ✓ |
| 83,716 events = 67.9%, 120.6 TWh = 66.3% (export) | 83,716 / 67.86% / 120.601 TWh / 66.25% ✓ |
| Confidence buckets 56,216 and 20,553 | exact ✓ |
| opsd_exact 50,493, wikipedia 10,168, psa_exact 9,278 | exact ✓ |
| Market volume share: TenneT 13.7, TransnetBW 1.0, Amprion 0.6, 50Hertz 0.4 | 13.73 / 1.04 / 0.56 / 0.40 ✓ |
| Dropped volume: market 28,580 GWh | 28,594 GWh ✓ |
| ρ = 0.65, τ = 0.43, p = 0.005 (17 nodes) | 0.647 / 0.426 / 0.0050 ✓ |
| bootstrap 95% CI [0.24, 0.85] | [0.24, 0.85] ✓ |
| leave-one-out 0.58–0.71 | 0.58–0.71 ✓ |
| 10-node ρ = 0.48 | 0.479 ✓ |
| η_rt = 0.85 leaves ρ = 0.65 | 0.645 ✓ |
| siting vs. spread ρ = 0.60 | 0.598 ✓ |
| naive congestion proxy ρ = −0.17, p = 0.50 | −0.174, p = 0.504 ✓ |
| wedge clip ±500 €/MWh ≈ 99th pct of intraday prices | q0.99 = 488.5 €/MWh ✓ |
| k = 16 quarter-hour steps | 200/(50·0.25) = 16 ✓ |
| α calibrated to 100 €/MWh | meta: calib_price 100, α = 1.0909 ✓ |
| H sweep: 910 cells, 50 runs, top cell (51.00, 11.17) | ✓ |
