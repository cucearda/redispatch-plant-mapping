# Redispatch → Power-Plant Matching

Resolves each free-text plant name in the German redispatch data
(`BETROFFENE_ANLAGE`) to an identifiable power plant, producing a lookup table
with registry IDs (PyPSA / MaStR / OPSD / EIC) and coordinates. The result
feeds a dashboard (interactive map of redispatch volume by location and time)
and an analysis notebook.

## Repository layout

```
redispatch-plant-mapping/
├── input/              raw + prepped source data, shared by every pipeline
├── prep_data.py         builds the prepped datasets in input/ from raw sources
├── A1/                  matching pipeline "A1" — LLM-based disambiguation:
│   ├── paths.py          auto-derives this pipeline's own temp/results paths
│   ├── main.py           orchestrates the stages below, in order
│   ├── build_candidate_index.py, redispatch_prep.py, match_*.py, ...
│   └── temp_A1/          this pipeline's private, regenerable scratch (gitignored)
├── J1/                   matching pipeline "J1" — deterministic rule-based
│   │                     cascade (no LLM calls); recreated from the
│   │                     redispatch-analysis/matcher/ project as an
│   │                     independent pipeline for comparison against A1
│   ├── paths.py          identical convention to A1/paths.py
│   ├── main.py           orchestrates the stages below, in order
│   ├── config.py, sources.py, classify.py, match_registry.py, match_web.py,
│   │   match_geo.py, resolve.py, cache.py, normalize.py, confidence.py, ...
│   ├── test_j1.py        regression tests for the pure functions
│   └── temp_J1/          this pipeline's private, regenerable scratch (gitignored)
├── results/
│   ├── A1/               A1's output — tracked in git, so match quality is
│   │                     diffable across commits and across pipelines
│   └── J1/               J1's output, same schema-agnostic convention
├── dashboard/            static Leaflet map of redispatch volume by plant/time;
│                         a dropdown switches between any pipeline's matches
│                         (see dashboard/README.md)
├── analysis.ipynb        matched-volume statistics notebook
├── docs/                 pipeline design docs
├── a1_vs_j1_comparison.md         A1 vs. J1 match-quality comparison
├── J1_vs_matcher_comparison.md    J1 vs. its redispatch-analysis/matcher/ original
├── opsd_eu_comparison.md          OPSD-EU vs. this repo's DE-only OPSD extract
└── old/                  superseded raw files, kept for reference only
```

Each pipeline folder is self-contained and follows the same convention:
`paths.py` (byte-identical across pipelines) auto-derives `<LABEL>/temp_<LABEL>/`
and `results/<LABEL>/` purely from the folder's own name, so adding another
pipeline variant (`A2/`, `K1/`, ...) means copying `paths.py` verbatim into a
new folder — no manual path edits anywhere. Every pipeline reads the same
`input/`, so outputs stay directly comparable.

`redispatch-analysis/` is a separate, parallel project living alongside this
one in the same repo — out of scope here; see its own docs if you need it.

## Setup

Python 3.10+, and:

```bash
pip install pandas rapidfuzz anthropic python-dotenv pydantic   # A1
pip install pandas requests pgeocode openpyxl ddgs              # J1
```

(No `requirements.txt` yet — these are every third-party import across `A1/`
and `J1/`; `pandas` is shared.)

Create a `.env` file at the repo root with your Anthropic API key (needed for
`A1`'s LLM disambiguation, geocoding, and coordinate-confirmation stages — not
needed for `J1`, `prep_data.py`, or the dashboard):

```
CLAUDE_API_KEY=sk-ant-...
```

## Input data

Raw source files go in `input/`. Two are large and gitignored — obtain them
separately (see `.gitignore` for what's expected):

| File | What it is |
|---|---|
| `Redispatch_Daten_2013_2020.csv`, `Redispatch_Daten_2021_2026.csv` | Raw redispatch call exports (netztransparenz.de), semicolon-delimited |
| `pypsa_powerplants_europe.csv` | Full-Europe `powerplantmatching` power-plant export |
| `OPSD_conventional_power_plants_DE.csv` | Curated German conventional-plant names/IDs |
| `Bundesnetzagentur_Kraftwerkliste.csv` | BNetzA Kraftwerksliste (MaStR extract) |

### Renaming older/downloaded files to match what the scripts expect

If you already have these datasets from before this project's `A1` restructuring,
or under whatever name they were originally downloaded/exported as, rename them
to the exact filenames above before running anything. The scripts match on
these exact names as hardcoded constants — nothing here is fuzzy-matched.

| If your file is named... | Rename to | Why |
|---|---|---|
| `pypsa_powerplants.csv` (or any generic all-Europe PyPSA/`powerplantmatching` export) | `input/pypsa_powerplants_europe.csv` | `prep_data.py` reads this exact name to build the Germany+Austria+Luxembourg spine (`prep_pypsa_countries()`) |
| `pypsa_unaggregated_powerplants.csv` (the old Germany-only spine `A1` used to read directly) | **No longer needed** — delete or archive it | Superseded: `prep_data.py` now derives the Germany(+AT+LU) spine directly from `pypsa_powerplants_europe.csv`; `A1/build_candidate_index.py` reads the prepped result (`input/pypsa_powerplants_de_at_lu.csv`), not this file |
| `Bundesnetzagentur_Kraftwerkliste .csv` (note the trailing space — the BNetzA download's actual filename) | `input/Bundesnetzagentur_Kraftwerkliste.csv` (no trailing space) | `prep_data.py`'s `BNETZA_RAW_FILE` constant has no trailing space |
| `Redispatch Export 2013-2020.csv`, `Redispatch_Daten_2021.csv`, or other year-range/spacing variants | `input/Redispatch_Daten_2013_2020.csv`, `input/Redispatch_Daten_2021_2026.csv` | `prep_data.py`'s `REDISPATCH_RAW_FILES` constant |

`old/Redispatch_Daten.csv` and `old/Redispatch_Daten_2021_only.csv` are earlier,
now-superseded raw exports kept for reference — no script reads them anymore.

## Running the pipeline

1. **Prep the shared datasets** (once, and again whenever a raw file in
   `input/` changes):
   ```bash
   python prep_data.py
   ```
   Writes `input/Redispatch_Daten_2013_2026.csv` (combined + timezone-corrected),
   `input/pypsa_powerplants_de_at_lu.csv` (Europe extract filtered to DE+AT+LU),
   and `input/bnetza_kraftwerkliste_clean.csv` (cleaned BNetzA lookup). All
   three are gitignored, derived, regenerate-freely.

2. **Run a matching pipeline** (either or both — they're independent and
   directly comparable, since both read the same `input/` and write the same
   output basename under their own `results/<LABEL>/`):
   ```bash
   python A1/main.py    # LLM-based; produces results/A1/redispatch_plant_matches.csv
   python J1/main.py    # rule-based cascade; produces results/J1/redispatch_plant_matches.csv
   ```
   See `docs/matching-pipeline.md` for what each `A1` stage does. `J1` has no
   separate design doc yet — see the module docstrings in `J1/` (`resolve.py`
   for the stage cascade, `config.py` for tuning constants).

3. **Build and open the dashboard:**
   ```bash
   python dashboard/build_data.py
   ```
   builds `dashboard/data_A1.js` and `dashboard/data_J1.js` from
   `results/A1/redispatch_plant_matches.csv` and `results/J1/redispatch_plant_matches.csv`
   respectively (both must already exist; pass `--pipeline A1`/`--pipeline J1`
   to build just one). Then open `dashboard/index.html` in a browser (needs an
   internet connection for map tiles) — a dropdown under the title switches
   which pipeline's matches are shown. See `dashboard/README.md` for details.

4. **Explore the results:** open `analysis.ipynb`.

## Docs

- [`docs/matching-pipeline.md`](docs/matching-pipeline.md) — what each `A1`
  matching stage does and why.
- [`docs/step0-candidate-index.md`](docs/step0-candidate-index.md) — how `A1`'s
  candidate power-plant index is built.
- [`a1_vs_j1_comparison.md`](a1_vs_j1_comparison.md) — how `A1` and `J1`'s
  outputs compare: per-step match shares, coordinate agreement, coverage gaps,
  and spot-checked discrepancies.
- [`J1_vs_matcher_comparison.md`](J1_vs_matcher_comparison.md) — validates `J1`
  as a faithful recreation of `redispatch-analysis/matcher/`'s original output.
- [`opsd_eu_comparison.md`](opsd_eu_comparison.md) — the Europe-wide OPSD
  extract vs. this repo's original Germany-only OPSD dataset.
