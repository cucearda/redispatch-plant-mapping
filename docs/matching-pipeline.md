# Redispatch → Power-Plant Matching Pipeline

Resolves each free-text `BETROFFENE_ANLAGE` name in the netztransparenz redispatch data
to identifiable power plants, producing a lookup table that carries each plant's registry
IDs (PyPSA / MaStR / OPSD / EIC) and coordinates.

The loop grain is the **398 distinct plant names** (not the 20,678 event rows); the final
table has one row per name, and is joined back onto the events for analysis.

## Data foundation

| Artifact | Role |
|---|---|
| `data/candidate_index.csv` | The matchable universe — PyPSA `powerplantmatching` fleet (spine) + 43 gap-filled OPSD conventional plants, each row carrying its `{pypsa, mastr, opsd, eic}` IDs, coordinates, capacity, fuel, and name variants. Built by [`build_candidate_index.py`](../build_candidate_index.py). See [step0-candidate-index.md](step0-candidate-index.md). |
| `data/bnetza_lookup.csv` | The BNetzA Kraftwerksliste as a standalone exact-match table (`norm_name → mastr_id`), kept out of the index. |

## Pipeline stages

Each name is routed once and resolved at the first stage that succeeds.

| # | Stage | Module | What it does |
|---|---|---|---|
| 0 | Redispatch prep | [`redispatch_prep.py`](../redispatch_prep.py) | Reduce to 398 distinct keys; per name compute modal `PRIMAERENERGIEART`, `max(MAXIMALE_LEISTUNG_MW)` (the capacity floor), and a `name_technology` where the name spells it out. |
| 1 | Rule filter / router | " | Classify each key into `individual` (plant-matched) or an aggregate type (`cluster` / `substation` / … — located + labelled, not plant-matched). |
| 2 | Exact match | [`match_exact.py`](../match_exact.py) | Unique normalised-name equality against BNetzA + the index → early exit. **27 resolved.** |
| 3 | Fuzzy shortlist | [`match_fuzzy.py`](../match_fuzzy.py) | Fuel- + capacity-filtered `WRatio` top-20 per individual (max over each candidate's name variants). Fuzzy never decides — it feeds the LLM. |
| 4 | LLM disambiguation | [`match_llm.py`](../match_llm.py) | `claude-sonnet-5` + adaptive thinking picks the correct candidate using fuel / capacity / coordinates / operator knowledge, or returns null. **168 of 197 matched** (130 high · 35 medium · 3 low); 29 null → residual. |
| 5 | Wikipedia residual | [`match_wikipedia.py`](../match_wikipedia.py) | LLM null/low entries: Wikipedia coordinate → nearest plant within 5 km, or coordinate-only. |
| 6 | Coordinate backfill | [`geocode_backfill.py`](../geocode_backfill.py) | Any still-coordless row (aggregates, residual failures) gets an approximate coordinate from its name via Haiku, so it can enter the spatial analysis. |
| 7 | Assemble | [`assemble_results.py`](../assemble_results.py) | Merge all stage outputs → `results/redispatch_plant_matches.csv`. |
| 8 | Coordinate confirmation | [`confirm_matches.py`](../confirm_matches.py) | Independently geocode each matched name (Haiku) and compare to the matched plant's coordinate → `coord_check`/`check_km`. Flags non-high-confidence disagreements for review. The geocode is coarse, so this is a **review aid**: it catches gross wrong-region matches but 30–55 km differences are usually just geocode imprecision on correct matches. Runs last, after assemble + backfill. |

Normalisation is shared ([`normalize.py`](../normalize.py)): `norm_light` (exact — lowercase,
strip TSO prefix / parens / punctuation, split underscores) and `norm_heavy` (fuzzy —
also strip generic type words, DSO prefixes, and turbine codes).

## Output schema

**Layer 1 — the map** (one row per distinct name):

| Column | Possible values | Meaning |
|---|---|---|
| `betroffene_anlage` | free text | The redispatch entry name — the key |
| `primaerenergieart` | `Konventionell` · `Erneuerbar` · `Sonstiges` | Energy category from the redispatch data |
| `entry_type` | `individual` · `cluster` · `control_reserve` · `substation` · `regional_renewable` · `countertrade` · `emergency` · `foreign` | Rule-filter class. Only `individual` is matched to a plant; the rest (incl. `cluster`) are aggregates — located + labelled, not plant-matched |
| `matched_id` | index id · MaStR `SEE…` id · *(empty)* | The resolved plant; empty for unmatched and for all aggregate entry types |
| `id_source` | `index` · `bnetza` · *(empty)* | Which table `matched_id` points into (`index` = candidate index; `bnetza` = a BNetzA-only MaStR id not in PyPSA) |
| `method` | `exact` · `llm` · `wikipedia` · `manual` · *(empty)* | How the match was made — auditable per entry |
| `confidence` | `high` · `medium` · `low` · *(empty)* | Match confidence; empty for structurally unmatchable entries |
| `needs_review` | `yes` · `no` | Flags entries for manual verification (low confidence or channel disagreement) |
| `lat`, `lon` | coordinates · *(empty)* | The plant's location |
| `coord_source` | `index` · `wikipedia` · `geocode` · `none` | Where the coordinate came from — lets name-geocoded unmatched entries enter a spatial read, appropriately caveated |
| `name_technology` | `Wind` · `Solar` · `Other renewable` · `Conventional` · *(empty)* | Technology parsed from the name where spelled out (mainly the DSO curtailment buckets — finer than `primaerenergieart`) |
| `coord_check` | `confirmed` · `disagree` · `no_geocode` · *(empty)* | Independent Haiku geocode of the name vs the matched plant's coordinate. A **review aid**, not authoritative — the geocode is coarse (see confirmation step) |
| `check_km` | distance · *(empty)* | Km between the name-geocode and the matched plant |
| `reasoning` | free text | One-line justification (LLM rationale, or e.g. "countertrade — no physical plant") |

**Layer 2 — enrichment** (join `matched_id → candidate_index` when `id_source = index`):

| Column | Source |
|---|---|
| `matched_name`, `fueltype`, `capacity_mw` | index |
| `mastr_ids`, `opsd_ids`, `eic_ids` | index |
| `source_pypsa_ids`, `turbine_count` | index |

So a matched **individual** entry expands to one plant's `{pypsa, mastr, opsd, eic}` IDs +
coordinates. For `id_source = bnetza`, Layer 2 comes from `bnetza_lookup` instead (name /
energy / PLZ / Ort), and `matched_id` itself is the MaStR id. Aggregate entry types
(`cluster`, `substation`, …) have no `matched_id`, so no Layer-2 join — they carry only the
Layer-1 label, a geocoded coordinate, and a `name_technology` where the name spells it out.

## How each `method` works

Every matched row records *how* it was matched, so any match can be audited and the
confidence interpreted accordingly.

- **`exact`** *(built)* — Deterministic name equality, no API. The redispatch name is
  light-normalised (lowercase; strip TSO prefix, parentheses, punctuation; split
  underscores) and compared for exact equality against both `bnetza_lookup.norm_name`
  and the index's plant-name variants (PyPSA + OPSD names). Accepted **only if exactly
  one distinct plant matches** — ambiguous ties fall through to fuzzy → LLM. Highest
  precision. e.g. `50H Berlin Mitte` → `berlin mitte` = index `Berlin Mitte`.

- **`llm`** *(built)* — For individuals not caught by exact. Fuzzy matching (`WRatio`,
  max over each candidate's name variants) builds a **top-20 shortlist** from the fuel-
  and capacity-filtered index — but fuzzy never decides. Claude Sonnet 5 (adaptive
  thinking) then reads each candidate's name / fuel / `Set` / capacity / coordinates and
  picks the correct one, applying operator knowledge fuzzy cannot (`KMW` → Kraftwerke
  Mainz-Wiesbaden), or **declines → null**. The `confidence` value is the model's own
  assessment.

- **cluster entries** *(not plant-matched)* — DSO `Cluster` entries name a substation, and
  the generation behind it is mostly sub-100 kW distributed renewables whose true membership
  is not recoverable from public data. An earlier name-gather channel produced systematic
  false positives (e.g. `BAG NWAK-Cluster 15 Pleinting`, a substation dispatching up to
  550 MW, matched to three same-named solar plants totalling 1.2 MW). Clusters are therefore
  treated like `regional_renewable`: **located** by the coordinate backfill and labelled with
  a `name_technology` where the name spells it out, but **not** matched to plant ids. See the
  thesis limitations chapter.

- **`wikipedia`** *(built)* — For individuals the LLM declined (null / low). A bot
  queries the name via the Wikipedia API (`opensearch` → `page/summary` → `coordinates`),
  then back-matches that coordinate to the **nearest index plant within 5 km**. If the
  LLM's guess and Wikipedia's point agree → high; Wikipedia-only → low. Resolves
  aliases / colloquial names that fuzzy misses.

- **`manual`** *(human step)* — The review tier for `needs_review = yes` entries (channel
  disagreement or genuinely unresolvable), where a person assigns the match. Not automated.

## `entry_type` reference

| Value | Matchable? | What it is |
|---|---|---|
| `individual` | yes → one plant | A named single plant |
| `cluster` | no → located only | A DSO substation aggregate (e.g. *SHN Cluster Handewitt*, *BAG NWAK-Cluster Pleinting*). True membership — mostly sub-100 kW distributed renewables — is not knowable from public data, so located + labelled, not plant-matched |
| `control_reserve` | no | DSO feed-in-management bucket — a whole grid-region + technology fleet (`{DSO}_{region}_CR_{tech}`) |
| `substation` | no | Umspannwerk / transformer-station node — aggregates all plants feeding it |
| `regional_renewable` | no | Whole-federal-state renewable bucket (e.g. *EE Bayern*) |
| `countertrade` | no | `Börse` — EPEX Gegengeschäft, no physical plant |
| `emergency` | no | Notfall-RD virtual entry |
| `foreign` | no | Austrian / Luxembourg plants not in the German registry (Vianden, Kühtai, illwerke) |

Structural aggregates are not thrown away — they are labelled (and, where the name allows,
carry a `name_technology` and a geocoded coordinate), so the table accounts for **100 % of
the redispatch names and volume**, which is itself a finding.

## Current status

Final lookup table: **`results/redispatch_plant_matches.csv`**.

Plant-matching is `exact` → `llm` → `wikipedia`, on `individual` entries only. Every other
entry type — including `cluster` — is located (geocoded) and labelled but not matched to a
plant. Aggregate entries account for 100% of the redispatch names and volume that carry no
single resolvable plant, which is itself a finding.

> Counts are omitted here pending a full re-run after the cluster-matching removal; regenerate
> with `python main.py` (needs API keys for the LLM / geocode / Wikipedia stages).

Run the whole thing with **`python main.py`** — it orchestrates every stage in order
(rebuilding the index only if missing) and exposes the redispatch input file as one config
constant. Or run any stage module directly (`python match_exact.py`) — each writes
incrementally, so the pipeline is resumable. Outputs live in `results/`; intermediates and
the index in `data/`.
