# Step 0 — Candidate Index

Builds the two lookup tables every downstream matching stage reads. Script:
[`build_candidate_index.py`](../build_candidate_index.py). Run:

```bash
python build_candidate_index.py        # writes data/candidate_index.csv + data/bnetza_lookup.csv
```

## Purpose

The redispatch dataset identifies plants only by free text (`BETROFFENE_ANLAGE`) —
no MaStR/EIC id to join on. So matching needs a single, pre-built **search space**
that (a) covers the matchable plant universe, (b) carries every plant ID we want in
the output, and (c) is aggregated to the right granularity. Step 0 produces it.

## Inputs

| File | Rows | Role |
|---|---|---|
| `pypsa_unaggregated_powerplants.csv` | 141,420 | **Spine.** Full MaStR-level fleet, all with coords; `projectID` cross-references MaStR/OPSD/EIC/JRC/GEM |
| `OPSD_conventional_power_plants_DE.csv` | 909 | Curated conventional names + `BNA` ids; gap-fill source |
| `Bundesnetzagentur_Kraftwerkliste .csv` | 2,614 | MaStR extract (Datenstand 2026); source for the standalone exact-match table |

`OPSD_renewable_power_plants_DE.csv` (1.77M rows) is **deliberately not used** — it
duplicates PyPSA's MaStR-derived renewables, is ~all rooftop-PV noise, and carries no
usable per-plant id. Renewable coverage comes through PyPSA ↔ MaStR (`mastr_id`).

## Outputs

### `data/candidate_index.csv` — the matchable universe (106,324 rows)

| entry_type | rows | what it is |
|---|---|---|
| `individual` | 104,531 | one physical plant (turbines merged into farms) — 4,498 are ≥10 MW |
| `cluster` | 1,793 | geo-clustered renewable group → set of plants (Wind 846 / Solar 804 / Biogas 143) |

Columns: `id, entry_type, Name, match_names, aliases, Fueltype, Technology, Capacity,
lat, lon, mastr_ids, opsd_ids, eic_ids, source_pypsa_ids, turbine_count`.

- **`match_names`** — pipe-joined name variants the fuzzy stage searches (PyPSA `Name`
  + OPSD `name_bnetza` for individuals; member aliases for clusters).
- **`mastr_ids / opsd_ids / eic_ids`** — comma-joined ids carried through aggregation,
  so a match emits the plant IDs directly. Coverage: mastr **99.8%**, eic 1,937 rows,
  opsd 673 rows (673 rows hold all 907 OPSD conventional ids — one row can carry several).

### `data/bnetza_lookup.csv` — standalone stage-1 exact-match table (2,323 rows, 1,963 distinct `norm_name`)

Columns: `mastr_id, Anzeigename, norm_name, Energietraeger, Nettonennleistung_MW,
Postleitzahl, Ort`. Cleaned from the raw BNetzA file (9 junk header rows dropped,
latin-1 → UTF-8, the 290 aggregated `Kleinanlagen_aggregiert` buckets removed).
**Not part of the index** — BNetzA has no coordinates; its only value is
`norm_name → mastr_id`, which bridges an exact hit back into the index for coords + other ids.

## How the index is built

1. **Spine** = PyPSA. Parse `projectID` → `mastr_ids` (strip `MASTR-`), `opsd_ids`, `eic_ids`.
2. **Enrich** with OPSD `name_bnetza` (join on `opsd_id`) → 477 rows get a second name variant.
3. **Gap-fill OPSD.** 238 OPSD plants aren't referenced in PyPSA → reconcile each by
   coordinates: within **0.5 km** of a PyPSA plant → attach the OPSD id there (untagged
   duplicate); otherwise → add as a new candidate. Result: **195 reconciled, 43 genuinely new.**
   (So the real "missing" set is 43, not 238 — 82% were dupes PyPSA already held untagged.)
4. **Aggregate.**
   - Pass 1 — merge same-name turbines within 50 km into farm-level `individual` rows (summed capacity, mean coords, unioned ids).
   - Pass 2 — geo-cluster co-located same-fuel+tech plants (10 km radius, ≥3 plants, ≥1 MW) into `cluster` rows carrying member `aliases` + constituent ids.
5. **BNetzA** written separately as the exact-match lookup.

## Key design decisions

- **Index = PyPSA (full) + OPSD gap-fill; BNetzA kept separate.** BNetzA is only the
  stage-1 exact-match accelerator; its link to everything else is `mastr_id`.
- **Clustering is renewable-only** (`CLUSTER_FUELS = {Wind, Solar, Biogas}`). Redispatch
  `Cluster` entries are 99.4% renewable (all 59 are DSO wind clusters); clustering
  conventional plants only produced junk candidates nothing could match.
- **Index is lossless.** Heavy stopword stripping (`pva`, `kraftwerk`, DSO prefixes,
  turbine suffixes) is deferred to match time — stored `norm_name`/`aliases` keep only a
  light normalisation so the list stays tunable without rebuilding.

## Contract with the matching stage

- **Same `norm()` on both sides.** Stage-1 exact match requires the redispatch
  `BETROFFENE_ANLAGE` be run through the identical `norm()` used for `norm_name`/`aliases`.
- **`Sonstiges → {Hydro-storage, Battery, Other}`** in the fuel filter — pumped storage
  (Goldisthal, Erzhausen, …) and the Neurath battery are tagged `Sonstiges`; excluding
  Hydro would block every storage match.
- **Capacity floor is per-entry**, from redispatch `max(MAXIMALE_LEISTUNG_MW)`, not a
  fixed threshold — post-Redispatch-2.0 plants match down to ~3 MW.

## Deferred / not done

- **Cluster naming** — still `Cluster <id>` (cosmetic; matcher uses `aliases`). Old
  pipeline used an LLM (Pass 4); decision pending.
- **Match-time stopword normalisation** — to be added in the fuzzy stage.
- **Foreign plants** (Vianden, Kühtai, illwerke) and **grid aggregates** (CR_/UW/EE) —
  flagged unmatchable at the redispatch-side rule filter, not here.
- **Speculative gap-fill** of the 195 reconciled dupes and of BNetzA-only plants — only
  add if a residual entry traces to a truly PyPSA-absent plant.
