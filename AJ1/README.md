# AJ1 — registry exact-match → LLM ranking → Wikipedia → area fallback

The third matching pipeline in this repo, alongside [`A1/`](../A1) (LLM-based
disambiguation) and [`J1/`](../J1) (deterministic rule-based cascade, no LLM
calls). AJ1 was built after both and deliberately picks practices from each
rather than inheriting either wholesale — every stage's module docstring names
which parent a given rule came from, and why it was kept, tightened, or
dropped.

Like every pipeline here it reads the shared, read-only [`input/`](../input),
keeps its scratch in `temp_AJ1/` (gitignored, regenerable) and writes its
tracked output to
[`results/AJ1/redispatch_plant_matches.csv`](../results/AJ1/redispatch_plant_matches.csv).
See [../README.md](../README.md) for repo-wide setup and
[../CLAUDE.md](../CLAUDE.md) for the conventions shared across pipelines.

## What makes AJ1 different

Four commitments shape every stage:

**1. Candidates are never discarded.** The output carries the *full* candidate
pool each registry produced (`opsd_ids`/`psa_ids`/`bnetza_ids`, pipe-joined),
and presents the single-plant answer alongside it as an explicitly labelled
*best guess* with the basis it rests on. Nothing is consolidated away, so a
consumer can always see what the guess was drawn from and filter on how much it
deserves to be trusted.

**2. The LLM ranks; it never identifies.** `match_llm.py` may only pick one
candidate out of the list that entry's registry matches already produced. It is
never asked to name a plant from scratch, so it cannot introduce a match the
registries didn't support. This is enforced in **code**, not just in the
prompt: `_validate()` drops any `best_guess_id` outside that entry's own
candidate set, so a hallucinated id becomes a null guess rather than a wrong
match. `test_aj1.py` pins that property.

**3. Identity and location are scored on separate axes.** `guess_basis` /
`llm_confidence` say whether the right *plant* was identified; `coord_basis` /
`coord_score` / `coord_precision` say whether the *location* can be trusted. A
row can score well on one and badly on the other — a Wikipedia-identified plant
sitting on nothing better than a town centroid, say. Never collapse them into
one number.

**4. An area centroid is never a plant pin.** `coord_precision != "plant"`
means a town, postcode, grid-area or Bundesland centroid, which can sit tens of
kilometres from anything generating power. Rendering one like a matched plant
would reintroduce, as a *location* error, exactly the false-positive class
`match_exact.py` refuses to make about *identity*. Filter on `coord_precision`
before mapping or analysing spatially.

## Stages

`main.py` runs these in order; each is a module with its own `main()` writing
incrementally to `temp_AJ1/`, so a run can be resumed by commenting out
completed stages.

| # | Stage | Module | Writes | What it does |
|---|---|---|---|---|
| 1 | redispatch prep | `redispatch_prep.py` | `redispatch_entries.csv` | Reduces the export to distinct plant-name keys. Drops `Probefahrt` rows (J1's practice); explodes multi-plant bundles using A1's stricter "≥2 comma segments each carrying a 4+ letter run" gate plus an " und " split; `max_dispatched_mw` comes from a plant's **solo** appearances only, since a bundle event's `MAXIMALE_LEISTUNG_MW` describes the combined dispatch, not any one member's capacity. |
| 2 | classify | `classify.py` | rewrites `redispatch_entries.csv` in place | 3-way label: `countertrade` (exactly "Börse"), `aggregate` (clusters, substations, grid codes, market constructs — no single physical location), `unclear`. **`unclear` is not "plant"** — it only means nothing ruled the entry out; whether it is a real plant is settled by a later stage. |
| 3 | exact match | `match_exact.py` | `matches_exact.csv` | The 555 `unclear` entries against three registries checked **separately** (no merged index), OPSD → PyPSA → BNetzA. Each registry runs a quality-aware 5-attempt cascade (`light` → `heavy_ae` → `heavy_bare`, plus OPSD-only `block` / `name_block`), stopping at the first attempt that resolves cleanly. Survivors must form a tight geographic cluster — or, for coordinate-less BNetzA, share a Postleitzahl. A fuel disagreement is tagged `conflict`, not discarded. All three registries are always attempted, so one entry can carry candidates in several. |
| — | cross-verify | `cross_verify.py` | — | **Disabled.** Commented out in `main.py`: it still imports `_tight_cluster_pick` (gone) and reads `opsd_id`/`psa_id` columns the rewritten `match_exact.py` no longer writes. Re-enable once ported to the pipe-joined `{reg}_ids` schema. |
| 4 | LLM candidate ranking | `match_llm.py` | `matches_llm.csv` | `claude-opus-5`, batched 20 at a time. Only entries with something to decide are sent: more than one distinct candidate, or a registry reporting `fuel == "conflict"`. Entries where every registry agrees on one plant resolve locally with no API call (`basis="unanimous"`). Written incrementally and **resumable** — a re-run skips entries already in the file. |
| 5 | wikipedia | `match_wikipedia.py` | `matches_wikipedia.csv` | An independent Wikipedia/DuckDuckGo lookup for **every** `unclear` entry, run regardless of whether stage 3 already resolved it, so its results can be compared against the registries rather than merely filling gaps. Gates per candidate title: not a blocked/category title, ≥5-char token overlap (compound-aware — "Walchensee" ↔ "Walchenseekraftwerk"), coordinates present, inside `DE_BBOX`, **a real fuel field in the article's Infobox Kraftwerk/Kernkraftwerk**, and no energy-class contradiction. The infobox-fuel requirement is what rejects the town-article-as-location-proxy failure mode found in J1's own Wikipedia matches. |
| 6 | geo fallback | `match_geo.py` | `matches_geo.csv` | **Area-level** coordinates for entries no plant-level stage could locate — resolvers and lookup tables ported verbatim from J1. Runs *last* of the matching stages so no Nominatim call is spent on an entry Wikipedia goes on to resolve. Exists mainly so the 220 `aggregate` names can enter a spatial read at all. Every row carries `coord_precision` (`town`/`area`/`state`). |
| 7 | assemble | `assemble_results.py` | `results/AJ1/redispatch_plant_matches.csv` | Merges every stage into the one tracked output. Optional inputs are skipped gracefully if absent, so a capped smoke-test wiki run is fine. Coordinates are reconciled here via `coords.py`, not taken from a precedence ladder. |

Some module docstrings still carry older step numbers from when `cross_verify`
was live; `main.py`'s `STAGES` list is the authority on run order.

## The coordinate workflow (`coords.py` + `postcodes.py`)

AJ1 originally read the coordinate off whichever candidate won the identity
contest — registry, else Wikipedia, else area fallback — and never compared it
to anything. That threw away real evidence: 292 of 300 Wikipedia coordinates
were never consulted, 104 entries whose winner was BNetzA (whose cleaned file
carries no coordinates at all) ended up unlocated despite a perfectly good
sibling OPSD/PyPSA point *in the same row*, and whether the datasets agreed was
simply never asked.

`coords.reconcile()` instead reduces every dataset to one representative point
— a registry's several candidate ids are collapsed to their own centroid first,
so a legitimate turbine-by-turbine listing can't inflate the disagreement — and
then asks whether they agree:

| `coord_basis` | Meaning |
|---|---|
| `consensus` | ≥2 datasets agreed within `AGREE_KM`; `lat`/`lon` is their centroid |
| `best_guess_disputed` | They disagreed; the best guess's own point was kept and the spread recorded |
| `single_source` | Only one dataset had a location |
| `postcode` | None did; a BNetzA Postleitzahl centroid was used |
| `area_fallback` | None did; `match_geo.py`'s area-level centroid |
| `none` | Nothing located it |

`AGREE_KM = 5` is **calibrated, not guessed**: across the 131 plants carrying
both a registry and a Wikipedia coordinate, separation ran median 0.13 km, p90
2.59 km, max 15.5 km — only 6 pairs above 5 km. Real agreement is tight, so
5 km flags genuine outliers without punishing the normal block-vs-site offset
between registries.

`postcodes.py` provides offline PLZ lookups in both directions via `pgeocode`,
already a `match_geo.py` dependency. **The containment test is a radius check
against the postcode centroid, not point-in-polygon** — `PLZ_RADIUS_KM = 10`
was calibrated against 843 OPSD plants whose coordinate is known to lie in
their stated postcode (median 2.44 km from the centroid, p95 8.08). 10 km is
the knee of that curve: it accepts 96.9% of genuine in-PLZ plants while sitting
just above the *rural* p95, so rural sites — whose postcodes are physically
much larger — aren't systematically flagged. A verdict of `outside` therefore
means "further from the centroid than 97% of genuine matches are": a smell
test, not a proof.

BNetzA's postcode is deliberately **not** mixed into the consensus centroid or
the spread — a PLZ centroid would drag both. It is used for exactly two things:
the coordinate of last resort, and an independent containment check
(`plz_check`) on whatever coordinate was chosen.

`coord_score` (0..1) starts from the basis, decays linearly with the spread
past `AGREE_KM`, and is nudged by `plz_check`. It rates the **location only** —
never the identity.

## Output schema

`results/AJ1/redispatch_plant_matches.csv`, one row per distinct plant name.

**Pass-through / candidate pool** — `plant`, `betroffene_anlage` (the untouched
raw `BETROFFENE_ANLAGE` string(s) this key was derived from, pipe-joined, so a
bundle-exploded member stays traceable), `category`, `primaerenergieart`,
`n_events`, `max_dispatched_mw`, `n_candidates`, and per registry
`{reg}_ids` / `{reg}_names` / `{reg}_fuel`.

**Identity axis**

| Column | Values |
|---|---|
| `best_guess_id`, `best_guess_registry`, `best_guess_name`, `best_guess_fuel` | The single-plant answer, or empty |
| `guess_basis` | `unanimous` (the registries found one plant and agreed on its fuel; no judgement applied) · `llm` (several candidates or a fuel conflict; `match_llm.py` picked one) · `wikipedia` (no registry matched at all; the wiki bot found an article carrying a real fuel field) · `none` |
| `llm_confidence`, `llm_reasoning` | Only for `guess_basis == "llm"` |
| `fuel_status` | `agreed` · `llm_resolved` · `wikipedia` · `conflict` · `unknown` |
| `wiki_title`, `wiki_url`, `wiki_fuel` | Only populated on an **accepted** wiki match — carrying them unconditionally let rejected hits masquerade as matches |

**Location axis**

| Column | Values |
|---|---|
| `lat`, `lon` | The reconciled coordinate |
| `coord_basis` | See the table above |
| `coord_score` | 0..1, location trust only |
| `coord_precision` | `plant` (OPSD/PyPSA/Wikipedia — a real plant location) · `postcode` · `town` · `area` · `state`. **Only `plant` may be drawn as a plant pin.** |
| `coord_source` | The specific resolver: `consensus`, `opsd`, `psa`, `wikipedia`, `bnetza_plz`, `gazetteer`, `nominatim`, `grid_area_*`, `state_centroid`, … |
| `coord_spread_km`, `coord_n_sources`, `coord_sources` | The evidence the score was computed from |
| `plz_check` | `inside` · `outside` · `no_plz` · `self` (the coordinate *is* the postcode centroid, so no bonus — the check would be circular) |
| `area_label` | What the area-level fallback resolved, when it was used |

## Current results (re-derive before quoting)

A snapshot of the tracked CSV. Pipeline code and input data both change — as
with this repo's other comparison write-ups, re-derive these numbers from the
current file rather than trusting what's written here.

776 distinct plant names: **555 `unclear`**, **220 `aggregate`**, 1
`countertrade`.

| Identity | | Location | |
|---|---|---|---|
| `unanimous` | 81 | `consensus` | 250 |
| `llm` | 233 | `single_source` | 130 |
| `wikipedia` | 8 | `best_guess_disputed` | 17 |
| `none` | 454 | `postcode` | 2 |
| | | `area_fallback` | 279 |
| | | `none` | 98 |

- **314** entries carry a registry id; **397** carry a plant-precision
  coordinate — Wikipedia locates 85 plants no registry id was resolved for.
- 678 of 776 rows are located at all; 205 of the 220 `aggregate` names get an
  area-level point.
- `coord_score` over located rows: mean 0.605, median 0.75.
- `plz_check`: 203 `inside`, 9 `outside`, 562 `no_plz`.
- Wikipedia stage over the 555 `unclear` entries: 300 `match`, 173 `rejected`,
  82 `none`.

### Known caveat in the currently-tracked CSV

The identity columns and the coordinate columns in the committed results file
come from **different runs**. The coordinate block was regenerated by
`rebuild_coords_on_results.py` against the current `matches_wikipedia.csv`,
while the identity block predates the Wikipedia gate fixes. The visible
symptom: **76 rows have `status == "match"` in the wiki stage — and carry
`wiki_title` / `wiki_fuel` and a plant-precision Wikipedia coordinate — but
still read `guess_basis == "none"`.** A full `python AJ1/main.py` re-run
relabels them `guess_basis == "wikipedia"`. Until then, treat `guess_basis` as
a lower bound on identity coverage.

## Running

```bash
python prep_data.py           # once, from the repo root — AJ1 reads input/ read-only

python AJ1/main.py            # full run; stage 5 is a long network job
python AJ1/main.py 20         # cap the wiki stage at 20 entries, for a smoke test
```

Individual stages can be run directly from inside `AJ1/` (imports are flat) —
useful when only one stage changed:

```bash
python match_llm.py                  # resumes; skips entries already in matches_llm.csv
python match_llm.py 5                # smoke test: first 5 entries
python match_wikipedia.py 20         # smoke test: first 20 entries
python match_wikipedia.py rejected   # retry only previously-rejected entries,
                                     # merging back into the existing file
python assemble_results.py           # re-merge whatever temp_AJ1/ currently holds
python postcodes.py 45711                # PLZ centroid
python postcodes.py 45711 51.656 7.345   # containment check
```

Tests — pure functions only, no API calls, no files read:

```bash
python -m pytest AJ1/test_aj1.py -q
python AJ1/test_aj1.py        # also runs without pytest
```

### Requirements

```bash
pip install pandas anthropic pydantic python-dotenv requests pgeocode ddgs
```

`CLAUDE_API_KEY` in a `.env` at the repo root, for stage 4 only. Every other
stage runs without it.

## Gotchas

- **Stages 1 and 2 are a pair.** `classify.py` rewrites
  `redispatch_entries.csv` **in place** to add the `category` column
  `match_exact.py` reads. Running them out of order or singly by hand is how
  you get a stale or missing `category`; the orchestrator is safer.
- **`temp_AJ1/` is gitignored, and stage 4 costs money to rebuild.** Deleting
  the temp tree throws away ~237 paid Opus calls. That is exactly why
  `rebuild_coords_on_results.py` exists — it applies the current coordinate
  logic to the *existing* results file without re-running the identity stages
  (`--apply` overwrites, keeping a `.bak`). It is a one-off, not part of the
  pipeline; a full `main.py` run produces the same thing through
  `assemble_results.py`.
- **`unclear` does not mean "individual plant".** It means no rule ruled the
  entry out as an aggregate. Several name families that *look* pool- or
  reserve-related (`Netzreservekraftwerk`/`Reservekraftwerk`, `Pool` entries,
  bare TSO/DSO names, and foreign plants such as Vianden and Kühtai) were
  checked against the real data and are genuine individual plants — AJ1
  deliberately does not exclude them the way A1's `foreign` label does.
- **AJ1's output key is the exploded member name**, not the raw
  `BETROFFENE_ANLAGE` — and with its own segmentation rules, which differ from
  J1's looser comma split. Anything joining these matches back to the raw
  redispatch calls must apply `redispatch_prep.py`'s bundle segmentation first
  (the dashboard imports it rather than reimplementing it) and split the call's
  volume across the members; the export gives one combined
  `GESAMTE_ARBEIT_MWH` per bundle and never says how it divided.
- **Windows console encoding**: ad-hoc `python -c "…"` printing German
  characters or symbols (→, ·, ö) can raise `UnicodeEncodeError` under the
  default cp1252 console. Prefix with `PYTHONIOENCODING=utf-8`.

## Files

| File | Role |
|---|---|
| `main.py` | Stage orchestrator; `STAGES` is the authority on run order |
| `paths.py` | Byte-identical across pipelines — derives `temp_AJ1/` and `results/AJ1/` from the folder name. Never hand-edit |
| `redispatch_prep.py`, `classify.py` | Stages 1–2 |
| `match_exact.py`, `match_llm.py`, `match_wikipedia.py`, `match_geo.py` | Stages 3–6 |
| `assemble_results.py` | Stage 7 |
| `normalize.py` | The two name-normalization tiers (`norm_light` from A1, `clean_heavy` from J1) used by the exact-match cascade |
| `normalize_preview.py` | Standalone, matching-free dump of the normalized string variants |
| `coords.py`, `postcodes.py` | Cross-dataset coordinate reconciliation + PLZ lookups |
| `rebuild_coords_on_results.py` | One-off: apply the current coordinate logic to an existing results file |
| `cross_verify.py` | Disabled — needs porting to the current `match_exact.py` schema |
| `test_aj1.py` | Regression tests for the candidate-set constraint and the unanimous/needs-ranking agreement |
| `j1_exact_match_comparison.md` | AJ1's exact-match coverage vs. J1's |
