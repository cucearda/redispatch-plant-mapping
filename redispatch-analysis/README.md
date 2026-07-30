# Redispatch analysis

Locating German redispatch interventions on the grid, and using that spatial
signal to reason about where grid-scale batteries should go.

The work runs in four stages: resolve each free-text plant name in the
netztransparenz redispatch exports to a real power plant and coordinate; join
those coordinates back onto the event rows; render the result as a
network-aware density map; and score candidate battery sites against it,
validated against a nodal PyPSA-Eur run.

```
raw redispatch exports  ──▶  matcher/   ──▶  results/matcher/plant_matcher_output.csv
                                              (one row per distinct plant name)
                                    │
                          pipeline/join_events.py
                                    ▼
                    data/redispatch_joined_high_confidence.csv
                              (one row per event, with coordinates)
                                    │
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
                  maps/                        siting/
          network + area KDE              battery siting score
                     │                             │
                     └──────────► nodal/ ◄─────────┘
                          PyPSA-Eur validation
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env                 # only if your data is not in ./data
python pipeline/fetch_data.py --list # what is present, what to download

python matcher/main.py               # resolve every distinct plant name
python pipeline/join_events.py       # join matches back onto events
python maps/kde_network_map.py       # network-KDE map -> results/maps/
python -m pytest tests/ -q           # or: python tests/test_matcher.py
```

Every path resolves relative to the repository root, overridable by environment
variable — see `.env.example`.

## Layout

| Folder | What lives there |
|---|---|
| `matcher/` | The staged name→plant resolver: registry lookup, then web search, then geographic fallbacks, with a confidence per stage. One module per concern — see `matcher/README.md`. |
| `pipeline/` | `fetch_data.py` (get the inputs), `join_events.py` (matcher output → event rows), `fetch_intraday_prices_smard.py`. |
| `maps/` | Network-KDE and area-KDE generation, plus headless-browser PNG rendering. See `maps/README.md`. |
| `siting/` | The battery siting score: PTDF, wedge, LP and proxy scorers, ranking, grid plots. |
| `nodal/` | PyPSA-Eur nodal-price comparison — the independent check on the siting score. |
| `notebooks/` | Analysis notebooks grouped by topic: `matching/`, `spatial/`, `siting/`, `sizing/`, `nodal/`. Outputs are committed, so results are readable without running anything. |
| `paper/` | LaTeX fragments, figure captions, the data audit, and both slide decks. |
| `data/` | Inputs. Large files are gitignored — `data/README.md` records where each comes from. `data/samples/` holds 200-row excerpts so the schemas are readable. |
| `results/` | `matcher/` (match tables), the comparison CSVs, and the PyPSA run outputs (gitignored). |
| `figures/` | Generated figures: `maps/`, `paper/`, `sizing/`. Bitmaps are gitignored — regenerate them. |
| `docs/` | `comparison.md` — how this matcher compares to the LLM-based one. |
| `tests/` | Regression tests pinning the matcher's normalisation, classification and confidence behaviour. |

## What is not in git

The working folder is 1.7 GB; the repository is about 10 MB. Excluded:
PyPSA-Eur intermediates (`resources/`, 1.2 GB), solved networks
(`results/de-nodal-*`, 124 MB — one file exceeds GitHub's 100 MB limit),
shadow-price exports (130 MB), the `pypsa-eur` clone, the collaborator's
`reference_repo` clone, siting sweep artefacts, generated bitmaps, and the raw
redispatch data.

`.gitignore` is an **allowlist**: it ignores `*` and re-includes specific
extensions. A new file type is invisible to git until a `!` line is added for
it; `git add -f <path>` overrides for one file; `git status --ignored` shows
what is being hidden.

## Caveats worth carrying into any reading of the maps

- Match confidence is calibrated by hand, not fitted. It orders matches
  sensibly; it is not a probability.
- Aggregate entries (DSO areas, federal-state renewable buckets) get a region
  centroid, not a plant location. They are flagged with a low confidence and a
  `grid_area_*` / `state_centroid*` source. A map drawn without filtering them
  out shows volume in the middle of a region where it did not physically occur.
- The maps show matched events only. `pipeline/join_events.py` prints the
  unmapped share at every filter step — quote it alongside any map, as a share
  of MWh rather than a share of names.
