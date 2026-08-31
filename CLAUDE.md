# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

Resolves each free-text plant name in the German redispatch data
(`BETROFFENE_ANLAGE`) to an identifiable power plant with registry IDs
(PyPSA/MaStR/OPSD/EIC) and coordinates, then feeds an interactive dashboard
and an analysis notebook. See [README.md](README.md) for the full layout and
setup instructions — this file only covers conventions and gotchas worth
knowing before editing code here.

`redispatch-analysis/` is a separate, parallel project living in the same
repo — out of scope; do not touch or describe it further unless explicitly
asked to work on it.

## The multi-pipeline convention

There are (currently) three independent, directly-comparable matching
pipelines: `A1/` (LLM-based disambiguation), `J1/` (deterministic rule-based
cascade, no LLM calls), and `AJ1/` (registry exact-match, then LLM ranking of
the candidate pool, then a Wikipedia bot, then an area-level geo fallback —
scoring the *coordinate* on its own axis, separately from the plant
identity). All follow the same folder convention:

- `input/` — raw + prepped source data, shared read-only by every pipeline.
  Prepped (derived) files are built by `prep_data.py` and are gitignored —
  regenerate with `python prep_data.py` rather than editing them.
- `<LABEL>/paths.py` — **byte-identical across every pipeline folder.**
  Auto-derives `INPUT_DIR`, `TEMP_DIR` (`<LABEL>/temp_<LABEL>/`), and
  `RESULTS_DIR` (`results/<LABEL>/`) purely from the folder's own name via
  `os.path.basename`. Never hand-edit a pipeline's `paths.py` — if you need
  to change the convention, change it in one pipeline and copy verbatim to
  the rest.
- `<LABEL>/temp_<LABEL>/` — that pipeline's private, regenerable scratch.
  Gitignored via the wildcard `*/temp_*/` in `.gitignore`, so a brand-new
  pipeline folder's temp dir is covered with zero `.gitignore` edits.
- `results/<LABEL>/` — that pipeline's tracked output. Every pipeline writes
  the same final-output basename (`redispatch_plant_matches.csv`) so outputs
  diff/join cleanly across pipelines despite very different internal schemas.

**Adding a new pipeline variant** (`A2/`, `K1/`, ...): copy `paths.py`
verbatim into the new folder — nothing else needs touching, and its temp/
results paths just work. Read the existing `A1/`, `J1/` and `AJ1/` folders
(and `dashboard/build_data.py`'s `PIPELINES` adapter registry, see below) as
worked examples before building the next one.

## Three pipelines, three schemas, no shared output contract

`A1`, `J1` and `AJ1` deliberately do **not** share an output schema — they
were built independently (A1 designed for this repo; J1 recreated from
`redispatch-analysis/matcher/`; AJ1 combining practices from both) and expose
very different match metadata (A1: `method`/`confidence` label/`entry_type`;
J1: per-registry `opsd_match`/`psa_match`/`wiki_match`/... columns and a
continuous `final_confidence`; AJ1: a `best_guess_*` + `guess_basis` identity
axis alongside a wholly separate `coord_*` location axis). Anything that
needs to treat the pipelines uniformly (the dashboard, a future comparison
script) should write a small adapter that normalises each pipeline's own
columns into a common shape, rather than trying to make the pipelines
themselves agree on one schema. See `dashboard/build_data.py`'s
`adapt_a1`/`adapt_j1`/`adapt_aj1` functions and its `PIPELINES` registry for
the pattern to follow.

## Comparison docs

When comparing pipeline outputs (or comparing `J1` against the original
`redispatch-analysis/matcher/` project it was recreated from), re-derive
numbers directly from the current CSVs rather than trusting older comparison
write-ups — they're snapshots, and pipeline code/data can change after they
were written:

- [a1_vs_j1_comparison.md](a1_vs_j1_comparison.md)
- [J1_vs_matcher_comparison.md](J1_vs_matcher_comparison.md)
- [opsd_eu_comparison.md](opsd_eu_comparison.md)

## Gotchas worth knowing before editing

- **Timezone**: the 2013-2020 redispatch export is UTC-labelled; 2021-2026 is
  already Europe/Berlin wall-clock (CET/CEST). `prep_data.py` converts both
  to one consistent Europe/Berlin calendar day before combining — don't
  re-derive "day" from a raw timestamp elsewhere without that correction.
- **Direction encoding**: ~6 rows in the 2021-2026 export have a mojibake-
  corrupted byte in "erhöhen". Direction is matched by substring (`erh` /
  `reduzieren`), not exact equality, to tolerate this.
- **J1's confidence is numeric** (`final_confidence`, 0..1), while **A1's is
  a text label** (`high`/`medium`/`low`/empty). Don't compare them directly —
  bucket J1's first (see `dashboard/build_data.py`'s
  `_bucket_confidence_numeric`, which uses J1's own `HIGH_CONF_SHORTCIRCUIT`
  = 0.85 as the high threshold).
- **"Börse" (market countertrade)** has no physical plant location in either
  pipeline. J1 drops it from its plant list entirely at load time
  (`_explode_plant_names`); A1 keeps it as `entry_type == "countertrade"`.
  Anything that needs to classify it consistently across both pipelines
  (e.g. the dashboard) should check the raw redispatch name itself
  (`name == "Börse"`) rather than relying on either pipeline's own tagging.
- **A pipeline's output key is not always the raw `BETROFFENE_ANLAGE`
  string.** A1 keys the *whole* raw string, including a multi-plant bundle
  like `"Boxberg, Jänschwalde, Lippendorf"`, which it then places at one
  centroid of its members. J1 and AJ1 key the *exploded member names*
  instead — and with different segmentation rules (AJ1 uses A1's stricter
  ">=2 plantish segments" gate plus an " und " split; J1 splits every comma).
  So anything joining a pipeline's matches back to the raw redispatch calls
  must apply that pipeline's own explosion first, and split the call's volume
  across the members (equally — the export gives one combined
  `GESAMTE_ARBEIT_MWH` per bundle and never says how it divided). See
  `dashboard/build_data.py`'s per-pipeline `explode`; joining on the raw
  string alone silently drops 10.3% of all redispatch volume (382 of the 1154
  distinct raw names are bundles).
- **AJ1 has two independent confidence axes, deliberately.**
  `guess_basis`/`llm_confidence` say whether the right *plant* was
  identified; `coord_score`/`coord_basis`/`coord_precision` (see
  `AJ1/coords.py`) say whether the *location* can be trusted, and a row can
  score well on one and badly on the other (a Wikipedia-identified plant with
  only a town-level gazetteer centroid, say). Never collapse them into one
  number. `coord_precision != "plant"` means the point is a town/area/state
  centroid and must not be drawn or analysed as a plant site.
- **Windows console encoding**: plain `python -c "..."` with German
  characters or special symbols (→, ·, ö) in `print()` can raise
  `UnicodeEncodeError` under the default cp1252 console. Prefix commands with
  `PYTHONIOENCODING=utf-8` when running ad-hoc scripts that print non-ASCII.
