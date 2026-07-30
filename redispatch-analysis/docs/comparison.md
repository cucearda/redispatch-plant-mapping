# Two pipelines, one problem

This repository now contains two independent resolvers for the same task: turn a
free-text `BETROFFENE_ANLAGE` string into a plant and a coordinate. They were
built separately and agree far more often than not, which is the useful part —
where they disagree is a shortlist of names worth looking at by hand.

## Where the decision is made

The LLM pipeline treats fuzzy matching as a *retrieval* step and the model as the
*decision* step: `match_fuzzy.py` builds a fuel- and capacity-filtered top-20
shortlist, and `match_llm.py` asks Claude Sonnet to pick from it, with a
hallucination guard rejecting any id not on the list. Fuzzy never decides.

The rule-based pipeline makes the decision at the threshold: a `difflib` ratio of
0.90 against OPSD, then PSA, then a series of web and geographic fallbacks, with
the first stage above 0.85 confidence ending the cascade. There is no
disambiguation step — the ranking *is* the decision.

Neither is strictly better. The LLM applies knowledge no string metric has
(`KMW` → Kraftwerke Mainz-Wiesbaden) and declines when unsure, which is what
makes the residual channels meaningful. The rule cascade costs nothing to re-run,
is fully deterministic, and reaches names the index simply does not contain,
because it can fall back to a gazetteer or a region centroid instead of
returning null.

## Agreement on real names

756 names are common to both. 652 have a coordinate from both.

| Metric | Value |
|---|---|
| Median disagreement | **1.5 km** |
| Within 5 km | 64% |
| Within 25 km | 77% |
| Beyond 100 km | 11% |

The disagreement is not spread evenly — it is concentrated almost entirely in one
class of entry. Broken down by which stage of the rule-based cascade produced the
coordinate:

| `final_source` | n | median km | within 5 km |
|---|---|---|---|
| `opsd_exact` | 231 | 0.24 | 94% |
| `psa_exact` | 76 | 0.19 | 80% |
| `manual` | 50 | 2.12 | 52% |
| `gazetteer` | 101 | 3.84 | 62% |
| `psa_fuzzy` | 20 | 4.69 | 55% |
| `wikipedia` | 21 | 6.64 | 33% |
| `opsd_city` | 13 | 7.63 | 46% |
| `grid_area_region` | 16 | 35.09 | 19% |
| `nominatim` | 32 | 46.18 | 34% |
| `grid_area_dso` | 54 | **118.98** | **0%** |

Registry hits agree essentially perfectly. Everything below the registry stages
degrades in exactly the order the confidence table predicts — which is a modest
validation of the hand-set scores. And `grid_area_dso` disagrees with *everything*:
a DSO area centroid is not a plant location and was never meant to be read as
one, but at 54 entries it is large enough to visibly move a density map.

## What each pipeline should take from the other

**Into the rule-based matcher:**

1. *Capacity as a filter.* The LLM pipeline requires a candidate to be at least
   70% of the entry's maximum dispatched power. Nothing in the rule cascade
   compares dispatched power to candidate rating, so an 800 MW entry can match a
   12 MW plant. Cheapest available precision gain.
2. *All name variants.* `match_fuzzy.py` scores against every name a candidate is
   known by (PyPSA, OPSD `name_bnetza`, BNetzA `Anzeigename`) and takes the max.
   The rule cascade scores against one canonical name per registry.
3. *A verification pass.* `confirm_matches.py` independently geocodes each matched
   name and reports the distance to the assigned coordinate. The rule-based
   matcher has no equivalent, so a confident-but-wrong fuzzy hit is invisible.
4. *`rapidfuzz` over `difflib`.* A 0.90 `difflib` ratio on a squashed alphanumeric
   key is strict and brittle — it misses token reorderings entirely, which is part
   of why 101 names fall all the way through to the gazetteer.

**Into the LLM pipeline:**

1. *The name cleaner.* `normalize.py:clean_redispatch_name` handles mojibake
   repair, a substantially longer TSO/operator/plant-type prefix table, and
   block/turbine suffix stripping. `norm_heavy`'s stopword set is smaller.
2. *Grid-area geography.* `match_grid_area`, `match_region` and `state_centroid`
   resolve DSO area codes and German regional names that the index has no entry
   for at all.
3. *A continuous confidence.* `high`/`medium`/`low` cannot express "0.85 scaled by
   a 0.93 fuzzy ratio", which makes threshold sweeps on the LLM output coarse.

## Open question

The 102 entries where the two disagree by more than 50 km are listed by
`pipeline/join_events.py` when both result tables are present. 46 of them are
`grid_area_dso`. Resolving those by hand would either validate the centroid
fallback as good enough for a density read, or establish that aggregate entries
should be excluded from the spatial analysis entirely — which is a finding either
way, and is currently the largest unquantified source of error in the maps.
