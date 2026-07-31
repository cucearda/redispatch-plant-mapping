# J1 vs. the original `redispatch-analysis/matcher/` pipeline — comparison notes

Comparison of `results/J1/redispatch_plant_matches.csv` (this repo's recreated
pipeline) against `redispatch-analysis/results/matcher/plant_matcher_output.xlsx`
(the original project's actual full run — not the 200-row
`redispatch-analysis/data/samples/plant_matcher_output.sample.csv`, which is
just a schema reference).

## Coverage

| | Count |
|---|---|
| Orig distinct plants | 766 |
| J1 distinct plants | 776 |
| **Exact-name overlap** | **748** |

**Orig-only names (18):** all comma-joined multi-plant bundle strings (e.g.
`"Boxberg, Jänschwalde, Lippendorf, Schkopau"`) that appear as a single
unsplit entry in orig's saved file — J1 correctly explodes these into
individual plant names via `_explode_plant_names`, consistent with the source
code both projects share. Likely an artifact of when that xlsx was generated
(possibly before/without the comma-explode being applied to that particular
run), not a logic difference between the two pipelines.

**J1-only names (28):** mostly grid-area/cluster constructs
(`50H_EDIS_CR_LV_NORD`, `AVA Cluster Dipperz`, `CROC Cluster Perleberg`, ...)
plus a few individual plants — consistent with this repo's combined
2013–2026 redispatch file having a slightly different/broader vintage than
whatever raw export the original project pulled.

## Where both resolve a coordinate — strong agreement

Of the 748 shared names, both have coordinates for **633**. Distance between
the two pipelines' answers:

| | |
|---|---|
| Within 1 km | 609 (96.2%) |
| Within 5 km | 612 (96.7%) |
| Within 25 km | 624 (98.6%) |
| **≥100 km apart** | **6** (0.9%) |

**Same source *category*** (registry/web/geo/grid-area/manual/unresolved,
collapsing e.g. `opsd_exact`/`opsd_fuzzy`/`opsd_city` → `opsd`): **678/748
(90.6%)**.

## The coverage gap is mostly human review, not pipeline logic

- **Orig has coords, J1 doesn't (40 cases):** 32 are `final_source ==
  "manual"` — a person filled these in by hand in orig's saved results, not
  the automated cascade. Only 4 are genuinely `unresolved` in both, plus 4
  `opsd_city` (minor OPSD-lookup misses on J1's side — worth a closer look if
  wanted).
- **J1 has coords, orig doesn't (2 cases):** `50H PV Witznitz` and
  `Kaunertal`, both resolved by J1 via Wikipedia — a small coverage win,
  likely just Wikipedia content/availability differing between when each
  pipeline ran.

## The 6 big (≥100 km) disagreements — spot-checked

| Plant | Orig source/coord | J1 source/coord | Distance |
|---|---|---|---|
| Mehrum | `nominatim`, 51.58/6.62 (near Düsseldorf) | `wikipedia`, 52.30/10.09 (near Peine) | 251 km |
| Reisach / Reisach 1–3 | `nominatim`, 49.74/11.56 (near Bayreuth) | `wikipedia`, 47.66/12.18 (Bavarian/Austrian border) | 235 km |
| AVA Cluster Vörden | `wikipedia`, 51.82/9.23 | `psa_fuzzy`, 52.45/8.10 | 104 km |

For **Mehrum** and **Reisach**, J1's Wikipedia-based answer is the
geographically correct one — the real Mehrum power station is near Peine
(Lower Saxony), and Kraftwerk Reisach is a hydro plant on the Bavarian/Austrian
border; orig's Nominatim fallback grabbed a wrong same-named place both times.
**Vörden** is genuinely ambiguous (multiple German places share that name) —
no clear winner without further digging.

## Bottom line

J1 is a faithful, correctly-adapted recreation: 90–96% agreement across every
metric where both pipelines actually attempt an automated match, and the
residual differences are explained (multi-plant name explosion, dataset
vintage, human manual-review entries in orig, and a handful of low-confidence
fallback cases where J1's answer looks more accurate on inspection) rather
than indicating a bug in the recreation.
