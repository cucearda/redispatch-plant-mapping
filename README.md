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
├── A1/                  a matching pipeline ("A1" = arda1) — self-contained:
│   ├── paths.py          auto-derives this pipeline's own temp/results paths
│   ├── main.py           orchestrates the stages below, in order
│   ├── build_candidate_index.py, redispatch_prep.py, match_*.py, ...
│   └── temp_A1/          this pipeline's private, regenerable scratch (gitignored)
├── results/
│   └── A1/               A1's outputs — tracked in git, so match quality is
│                         diffable across commits and (later) across pipelines
├── dashboard/            static Leaflet map of redispatch volume by plant/time
├── analysis.ipynb        matched-volume statistics notebook
├── docs/                 pipeline design docs
└── old/                  superseded raw files, kept for reference only
```

A future second pipeline (`A2/`, etc.) would copy `A1/`'s scripts + `paths.py`
verbatim into its own folder — `paths.py` auto-derives `A2/temp_A2/` and
`results/A2/` from the folder's own name, no manual path edits needed. Every
pipeline reads the same `input/` so outputs stay comparable.

## Setup

Python 3.10+, and:

```bash
pip install pandas rapidfuzz anthropic python-dotenv pydantic
```

(No `requirements.txt` yet — these are every third-party import across `A1/`.)

Create a `.env` file at the repo root with your Anthropic API key (needed for
the LLM disambiguation, geocoding, and coordinate-confirmation stages — not
needed just to run `prep_data.py` or the dashboard):

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

2. **Run the matching pipeline:**
   ```bash
   python A1/main.py
   ```
   Produces `results/A1/redispatch_plant_matches.csv` — the final lookup table.
   See `docs/matching-pipeline.md` for what each stage does.

3. **Build and open the dashboard:**
   ```bash
   python dashboard/build_data.py
   ```
   then open `dashboard/index.html` in a browser (needs an internet connection
   for map tiles). See `dashboard/README.md` for details.

4. **Explore the results:** open `analysis.ipynb`.

## Docs

- [`docs/matching-pipeline.md`](docs/matching-pipeline.md) — what each matching
  stage does and why.
- [`docs/step0-candidate-index.md`](docs/step0-candidate-index.md) — how the
  candidate power-plant index is built.
