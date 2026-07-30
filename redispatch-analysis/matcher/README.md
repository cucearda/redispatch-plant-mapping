# matcher

Resolves each distinct `BETROFFENE_ANLAGE` string in the redispatch exports to a
coordinate, by routing it through a cascade of increasingly permissive stages and
recording which one succeeded.

Run it:

```bash
python matcher/main.py
```

Output is `results/plant_matcher_output.{csv,xlsx}` — one row per distinct name,
with the outcome of *every* stage kept side by side, not just the winner. That
makes a wrong match diagnosable: you can see that OPSD fuzzy fired at 0.91 while
Wikipedia rejected the same name on an energy contradiction.

## The cascade

Each name enters at Stage 0 and leaves at the first stage that produces a
coordinate above the short-circuit threshold (0.85). Below that, candidates
accumulate and the highest-confidence one wins.

| Stage | Module | What it does |
|---|---|---|
| 0 | `classify.py` | Route the name: `single` / `cluster` / `site_pool` / `grid_area`. Pure market constructs (Börse, MRL, 10Y EICs, abschaltbare Last, Netzregelverbund) have no meaningful coordinate and exit here. |
| 0b | `match_geo.py` | Renewable state aggregates (`EE Bayern`) intercept early to a state centroid — a two-token renewable name is a bucket, not a plant. |
| 1 | `match_registry.py` | OPSD: exact key equality, then `difflib` fuzzy at ≥ 0.90, energy-class checked. A city-name fallback is kept separate and scored much lower. |
| 2 | `match_registry.py` | PSA (powerplantmatching) fleet, same exact→fuzzy shape. Same-named candidates within 0.2° are collapsed; a spread-out candidate set is penalised as ambiguous. |
| 3 | `match_web.py` | Konventionell / Sonstiges: German Wikipedia API search. Gated on title blocklist, token overlap with the plant name, the DE/AT/LU bounding box, and an energy-contradiction check. A *rejection* (not a miss) falls through to DuckDuckGo. |
| 4 | `match_geo.py` | Place-name extraction into the pgeocode German gazetteer. |
| 5 | `match_web.py` | Renewables that got no gazetteer hit: Wikipedia/DDG again with `Windpark`/`Solarpark` hints and stricter gates. |
| 6 | `match_geo.py` | Region and federal-state centroids, DSO area codes, and finally a Nominatim forward lookup. |

`resolve.py` owns the cascade itself; `main.py` only pools the inputs, loops, and
checkpoints every 25 names.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Every path and tuning constant. Paths resolve relative to the repo root or from environment variables. |
| `normalize.py` | Mojibake repair, ASCII folding, and `clean_redispatch_name` — the TSO/operator/plant-type/block/turbine stripper that turns `TTG_KW-Pool Irsching Block 4` into the place tokens that carry signal. |
| `classify.py` | Aggregation classification and the grid-area regexes. |
| `sources.py` | Loading the redispatch exports, OPSD and PSA; the energy-class mapping shared by all matchers. |
| `confidence.py` | The single scoring function. One place to retune. |
| `match_registry.py` | Stages 1–2. |
| `match_web.py` | Stages 3 and 5, plus the gate predicates. |
| `match_geo.py` | Stage 4 and the geographic fallbacks: gazetteer, Nominatim, state/region centroids, DSO grid areas. |
| `cache.py` | JSON cache for every network lookup, so a re-run is free. |
| `resolve.py` | The cascade. |
| `main.py` | Orchestration, checkpointing, summary report. |

## Confidence

`confidence.py` maps `(source, status)` to a base score, scales fuzzy matches by
the actual ratio, zeroes anything with a contradicted energy class, and penalises
ambiguous PSA candidate sets:

| Source | exact | fuzzy | other |
|---|---|---|---|
| OPSD | 1.00 | 0.85 × ratio | 0.55 (city) |
| PSA | 0.95 | 0.80 × ratio | |
| Wikipedia | | | 0.85 |
| DuckDuckGo | | | 0.70 |
| Gazetteer | | | 0.50 |
| State / region centroid | | | 0.30–0.35 |

These are hand-set, not fitted. They order matches sensibly and the 0.85
short-circuit is chosen so that only a registry hit can end the cascade early —
but they are not probabilities, and any downstream filter should be justified on
its own terms rather than by treating 0.7 as "70% likely correct".

## Re-running

The web lookups are cached in `results/plant_matcher_cache.json`, so a second run
touches the network only for names it has not seen. Delete the cache to force a
full re-resolve. Partial results are written every 25 names to
`*_partial.csv`, so an interrupted run loses at most 25 names of work.
