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

There are (currently) two independent, directly-comparable matching
pipelines, `A1/` (LLM-based disambiguation) and `J1/` (deterministic
rule-based cascade, no LLM calls). Both follow the same folder convention:

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
results paths just work. Read the existing `A1/` and `J1/` folders (and
`dashboard/build_data.py`'s `PIPELINES` adapter registry, see below) as
worked examples before building the next one.

## Two pipelines, two schemas, no shared output contract

`A1` and `J1` deliberately do **not** share an output schema — they were
built independently (A1 designed for this repo; J1 recreated from
`redispatch-analysis/matcher/`) and expose very different match metadata
(A1: `method`/`confidence` label/`entry_type`; J1: per-registry
`opsd_match`/`psa_match`/`wiki_match`/... columns and a continuous
`final_confidence`). Anything that needs to treat both pipelines uniformly
(the dashboard, a future comparison script) should write a small adapter
that normalises each pipeline's own columns into a common shape, rather than
trying to make the pipelines themselves agree on one schema. See
`dashboard/build_data.py`'s `adapt_a1`/`adapt_j1` functions and its
`PIPELINES` registry for the pattern to follow.

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
- **Windows console encoding**: plain `python -c "..."` with German
  characters or special symbols (→, ·, ö) in `print()` can raise
  `UnicodeEncodeError` under the default cp1252 console. Prefix commands with
  `PYTHONIOENCODING=utf-8` when running ad-hoc scripts that print non-ASCII.
