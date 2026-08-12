# AJ1 vs. J1 — exact-match coverage comparison

Snapshot comparison of `AJ1/temp_AJ1/matches_exact.csv` (AJ1's exact-match
step, `AJ1/match_exact.py`) against `results/J1/redispatch_plant_matches.csv`
(J1's `opsd_match`/`psa_match` columns, independent of which stage ultimately
"won" as `final_source`). As with the other comparison docs in this repo,
re-derive these numbers from the current CSVs rather than trusting this
write-up if the underlying data or code has changed since.

## Coverage isn't quite the same population

J1 processes all 776 distinct plant names directly. AJ1 first classifies
names into `unclear`/`aggregate`/`countertrade` (`AJ1/classify.py`) and only
attempts registry matching on the 555 `unclear` ones — substations, grid
clusters, and TSO/market constructs are deliberately routed to `aggregate`
and never attempted.

J1 has an exact match (`opsd_match == "exact"` or `psa_match == "exact"`) on
**333** of its 776 rows. Cross-referencing those 333 against AJ1:

| | Count |
|---|---|
| AJ1 classifies `unclear`, **and matches** it too (any registry/tier) | 295 |
| AJ1 classifies `unclear`, but **doesn't** match it | **22** |
| AJ1 classifies `aggregate` (never attempted, by design) | 15 |
| Not present in AJ1's name list at all (bundling edge case) | 1 |

The 15 "aggregate" names (`50H UW Altentreptow`, `AVA Cluster Alfstedt`,
`SHN Cluster Jardelund`, etc.) are substations/grid-clusters — J1's "exact
match" for these is a name coincidence with an unrelated plant, and AJ1
correctly never attempts them. The 1 missing name (`Knapsack GT11`) is a
bundle-explosion edge case, not investigated further here. The real
comparison set is the **22 genuine misses** below.

## The 22 misses, by root cause

| Plant | Cause | Detail |
|---|---|---|
| Goldisthal | hydro/class mismatch | entry `Konventionell`, OPSD class `sonstiges` |
| Hohenwarte 2 | hydro/class mismatch | entry `Konventionell`, OPSD class `sonstiges` |
| Kaunertal | hydro/class mismatch | entry `Konventionell`, OPSD class `sonstiges` |
| Markersbach | hydro/class mismatch | entry `Konventionell`, OPSD class `sonstiges` |
| Silz 1 (EON) | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Silz 1 (TIWAG) | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Silz 1 (Uniper) | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Silz 2 (TIWAG) | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Silz 2 (Uniper) | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Walchensee 1 | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Walchensee 2 | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Walchensee 3 | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Walchensee 4 | hydro/class mismatch | entry `Erneuerbar`, OPSD class `sonstiges` |
| Wehr | hydro/class mismatch | entry `Konventionell`, OPSD class `sonstiges` |
| Kernkraftwerk Neckarwestheim | stale shutdown | PSA's only candidate row (`Neckarwestheim 1`) has `DateOut=2011`; entry's earliest possible year is 2013 |
| Kernkraftwerk Neckarwestheim Block 2 | stale shutdown | same PSA row/`DateOut` issue — PyPSA has no separate row for the unit that actually ran until 2023 |
| BIBLIS_GT (bnBm) | stale shutdown | Biblis A and B both `DateOut=2011`; entry's earliest possible year is 2021 |
| 50H OWP Baltic 1+2 | too spread to cluster | PSA's 102 "Baltic" rows span 4.3° of longitude (mixes an unrelated Solar site in) |
| OWP Borkum Riffgrund 3 | too spread to cluster | PSA's 217 "Borkum Riffgrund" rows (all Wind) span 0.54° of longitude, above `TIGHT_CLUSTER_DEG=0.2` |
| PV_BEDBURG_A | too spread to cluster | PSA's 22 "Bedburg" rows mix Solar and Wind, spread 0.235° — just over threshold |
| UPMSchongau | tokenization | no space between "UPM" and "Schongau" in the source string, so it never splits into a droppable prefix token |
| WP Emsland | correct rejection of a likely-wrong J1 match | entry is `Erneuerbar` (wind park), OPSD's `Emsland` candidate is `konventionell` (gas) |

### 1. Hydro/pumped-storage class mismatch (14 of 22)

OPSD/PyPSA's fuel vocabulary puts every hydro/pumped-storage plant in AJ1's
third bucket, `class == "sonstiges"` (`OPSD_FUEL_TO_CLASS`/`PSA_FUEL_TO_CLASS`
in `AJ1/match_exact.py`), but the redispatch data's own `primaerenergieart`
inconsistently labels these plants `Konventionell` or `Erneuerbar` instead.
`_class_filter` treats a *known* class disagreement as a hard exclusion (by
design — otherwise an unrelated same-name solar/wind plant would slip
through for other entries), so every one of these candidates is filtered out
before geo-resolution ever runs. This is the dominant cause by far and is
systematic: any hydro-matched entry will hit it.

### 2. Stale-shutdown plants (3 of 22)

PSA's only candidate rows are nuclear units with `DateOut = 2011` (Biblis A/B
genuinely shut down in 2011; PyPSA's sole `"Neckarwestheim 1"` row is also
dated 2011, even though the actually-relevant unit — Neckarwestheim II/
Block 2 — kept running until 2023; PyPSA has no separate row for it, a gap
in PyPSA's own data, not AJ1's). `_date_filter` correctly excludes a
candidate that had already stopped before the entry's earliest possible
year; J1 has no equivalent filter and matches these anyway, to a reactor
that had been offline for 2-10+ years before the redispatch record could
exist.

### 3. Genuinely ambiguous, too-widely-spread name collisions (3 of 22)

PSA has many rows sharing these names, spread well beyond AJ1's
`TIGHT_CLUSTER_DEG = 0.2°`. AJ1 declines rather than guess. J1's own
duplicate-resolution logic (`_psa_pick` in `J1/match_registry.py`) uses the
*identical* 0.2° threshold — when that also fails, J1 explicitly falls back
to keeping the first row (with a confidence penalty) rather than declining.
So J1's "exact match" here isn't necessarily more correct, just more willing
to guess. Notably, `PV_BEDBURG_A` got matched by J1 to `Windpark Bedburg`
(wind), despite the entry name explicitly signalling PV/solar.

### 4. Tokenization defeated by missing whitespace (1 of 22)

`UPMSchongau` glues "UPM" and "Schongau" into one word with no separating
space, so word-based cleaning can never isolate "UPM" as a droppable prefix
token; the entry's heavy-cleaned key stays `upmschongau` while PSA's own
`"Upm Schongau"` (with a space) cleans down to `schongau`. The keys never
align at any tier — genuinely fixable (e.g. a camelCase-glue-splitting
pre-step) but currently a blind spot for both pipelines' tokenizers.

### 5. A likely-wrong J1 match, correctly rejected (1 of 22)

`WP Emsland` (a wind-park entry, `Erneuerbar`) was exact-matched by J1 to
OPSD's `"Emsland"`, a `konventionell` natural-gas plant, purely on the shared
regional name. AJ1's class filter blocks this cross-fuel-type coincidence.

## Takeaway

AJ1's stricter class/date filtering is catching real problems — categories
2 and 5 are arguably J1 errors, not AJ1 gaps. Category 1 (hydro labeling) is
a systematic inconsistency worth a real fix if those 14 should be recovered.
Category 3 is a deliberate "decline rather than guess" philosophy difference
between the two pipelines. Category 4 is a one-off tokenization edge case.
