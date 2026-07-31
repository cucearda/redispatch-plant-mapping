# A1 vs. J1 — comparison notes

Comparison of the two independent matching pipelines in this repo:
`results/A1/redispatch_plant_matches.csv` (LLM-based, `A1`) vs.
`results/J1/redispatch_plant_matches.csv` (deterministic rule-based cascade,
`J1`, recreated from `redispatch-analysis/matcher/`).

## Coverage

| | Count |
|---|---|
| A1 distinct names | 1,131 |
| J1 distinct names | 776 |
| **Exact-name overlap** | **745** |
| A1-only | 386 |
| J1-only | 31 |

**A1-only (386):** almost all comma-joined multi-plant bundle strings (e.g.
`"Boxberg, Lippendorf VE, Moorburg, Schkopau"`). A1 keeps these bundles as one
`entry_type == "multi_plant"` row; J1 explodes every `BETROFFENE_ANLAGE` on
commas via `_explode_plant_names` before matching, so the same underlying
event surfaces as several separate single-plant rows on J1's side instead of
one combined row on A1's. Structural difference in how each pipeline handles
multi-plant announcements, not a coverage gap.

**J1-only (31):** individual plant names that only exist because J1 exploded
a bundle A1 left combined (e.g. `Hohewarte II`, `Walsum 9 undWeiher 3`), plus
a handful of `PV_*`/grid-node identifiers.

## Per-pipeline match-step breakdown

**A1 `method` (1,131 rows):**

| Method | Count | Share |
|---|---|---|
| `llm` | 450 | 39.8% |
| `multi_plant` | 378 | 33.4% |
| `exact` | 80 | 7.1% |
| `cluster_name` | 38 | 3.4% |
| `cluster_geocode` | 28 | 2.5% |
| `wikipedia` | 3 | 0.3% |
| *(other/blank)* | 154 | 13.6% |

**J1 `final_source` (776 rows):**

| Source | Count | Share |
|---|---|---|
| `opsd_exact` | 237 | 30.5% |
| `gazetteer` | 116 | 15.0% |
| `unresolved` | 102 | 13.1% |
| `grid_area_dso` | 85 | 11.0% |
| `psa_exact` | 78 | 10.1% |
| `nominatim` | 36 | 4.6% |
| `psa_fuzzy` | 23 | 3.0% |
| `wikipedia` | 22 | 2.8% |
| `grid_area_region` | 17 | 2.2% |
| `skipped_grid_area_market` | 11 | 1.4% |
| `psa_exact_dir` | 9 | 1.2% |
| `opsd_city` | 8 | 1.0% |
| *(all other fallback sources)* | 32 | 4.1% |

J1's cascade is registry-first (OPSD/PSA exact matches cover ~41% of rows
before any web lookup); A1 leans on the LLM for the bulk of individual-plant
disambiguation, with exact/cluster stages handling the rest.

## Where both resolve a coordinate — agreement

Of the 745 shared names, both pipelines produce a coordinate for **603**.
Haversine distance between the two answers:

| | |
|---|---|
| Within 1 km | 277 (45.9%) |
| Within 5 km | 396 (65.7%) |
| Within 25 km | 468 (77.6%) |
| Within 100 km | 537 (89.1%) |
| **≥100 km apart** | **66** (10.9%) |

Agreement is markedly weaker here than in the [J1-vs-original-matcher
comparison](j1_vs_matcher_comparison.md) (96%+ within 5 km there vs. 65.7%
here) — expected, since A1 and J1 use fundamentally different matching
strategies (LLM judgment vs. deterministic registry/web cascade), whereas J1
vs. the original matcher are the *same* logic run on slightly different data.

## Coverage gaps (within the 745 shared names)

- **A1 has a coordinate, J1 doesn't (73 cases):** dominated by `50H_*` /
  `GERSTEINW_*`-style internal grid-node and cluster identifiers that J1's
  cascade correctly leaves `unresolved` or `skipped_grid_area_market` (no
  registry entry, no sane web search target) but that A1's LLM stage
  confidently geocodes anyway — worth treating A1's answers here with
  suspicion rather than as a J1 shortfall.
- **J1 has a coordinate, A1 doesn't (33 cases):** almost entirely
  `50H_MNS_CR_*` / `50H_TEN_CR_*` grid-area/DSO codes that J1 resolves to a
  DSO-region centroid (`grid_area_dso`) — a deliberate low-precision fallback
  A1 has no equivalent stage for.

## The biggest (≥100 km) disagreements — spot-checked

| Plant | A1 method/coord | J1 source/coord | Distance |
|---|---|---|---|
| Silz 1/2 (EON/Uniper/TIWAG) | `llm`, ~47.1/10.6 (Tyrol) | `gazetteer`, wrong "Silz" | 703 km |
| SHN Cluster Krümmel | `cluster_geocode` | `gazetteer`, wrong same-named place | 371 km |
| BAG NWAK-Cluster 17 Altheim | *(unresolved on A1 side)* | `opsd_exact` | 369 km |
| 50H_* DSO grid nodes (several) | N/A | `grid_area_dso`/`grid_area_state` centroid | 330–414 km |
| München Süd GT 3 / GT 61 | `llm`, Munich area | `gazetteer`, wrong place | 320 km |

The `Silz`/`SHN Cluster Krümmel`/`München Süd` cases follow the same pattern
identified in the J1-vs-original comparison: J1's Stage 4b gazetteer
(pgeocode/city-name lookup) grabs a same-named-but-wrong town when the
registry and Wikipedia stages both miss, while A1's LLM stage correctly uses
outside knowledge of the actual plant. The `50H_*`/grid-node rows aren't
really disagreements — they're J1 supplying a deliberately coarse DSO/state
centroid where A1's exact counterpart is simply missing from the shared set.

### Reisach — traced in detail

`Reisach`, `Reisach 1`, `Reisach 2`, `Reisach 3` (all in the redispatch data)
are a real, small pumped-storage/hydro plant (Kraftwerk Reisach, on the
Tyrol/Bavaria border).

- **A1**: plain `Reisach` gets `method=exact` → **49.534, 12.276** (an exact
  OPSD/BNetzA name match — but that coordinate is actually in the Upper
  Palatinate, nowhere near the real Tyrolean plant, so the "exact" name match
  latched onto an unrelated same-named registry entry). `Reisach 1/2/3` fall
  through to the LLM stage, which explicitly writes `reasoning: "No candidate
  corresponds to the Reisach hydro plant"` and returns **47.53, 10.95** — in
  the right general area (western Tyrol) but with no matched registry ID, so
  this looks like an LLM-estimated coordinate rather than a grounded match.
- **J1**: all four variants resolve identically to `final_source=wikipedia`,
  **47.6645, 12.1758**, `wiki_title="Kloster Reisach"`,
  `wiki_reason="Pumpspeicherkraftwerk:accepted"` — i.e. J1's Wikipedia stage
  searched for `Reisach` with a "Pumpspeicherkraftwerk" (pumped-storage) fuel
  hint appended, found the article **"Kloster Reisach"** (a monastery, not
  the power plant), and its gates (title-blocklist / token-overlap / DE_BBOX /
  energy-contradiction) let it through because the article title contains the
  token "Reisach" and its coordinates sit inside the DE_BBOX.

None of the three answers is clearly correct: A1's plain `Reisach` match is a
wrong-registry-entry false positive, its `Reisach 1-3` LLM answer is
plausible but ungrounded, and J1's Wikipedia answer resolved to a monastery
article that merely shares the place name, not the actual power plant. This
also contradicts an earlier, less-informed claim (in the J1-vs-original-matcher
comparison) that J1's Reisach answer was "the geographically correct one" —
that comparison only had the *original matcher's* Nominatim answer to compare
against, not A1's, and didn't inspect J1's underlying Wikipedia article title.
**Bottom line: Reisach's true coordinates remain unresolved by both
pipelines; none of the three available answers should be trusted without
manual lookup.**

## Bottom line

A1 and J1 agree closely (registry-backed matches, ~90% within 100 km) where
both have a solid, grounded source (OPSD/PSA exact hits, clear single-plant
names), and diverge mainly in their respective low-confidence fallback
stages: A1's LLM stage will confidently answer even for internal grid-node
codes with no real-world coordinate, while J1's gazetteer/Wikipedia fallbacks
are vulnerable to same-named-place collisions. Neither pipeline's fallback
tier should be treated as ground truth without spot-checking.
