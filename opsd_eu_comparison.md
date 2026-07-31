# OPSD Europe conventional-plant file — comparison notes

Findings from adding `input/OPSD_conventional_power_plants_EU.csv` to `prep_data.py`
(new `prep_opsd_countries()` step, filtering to `input/opsd_conventional_powerplants_de_at.csv`)
and comparing it against the existing German-only OPSD file, the redispatch data's
`"foreign"`-labelled entries, and the PyPSA Europe extract.

## What changed

`prep_data.py` gained a fourth prep step:

```python
prep_opsd_countries(countries=["DE", "AT"])
```

Reads `input/OPSD_conventional_power_plants_EU.csv` (6,093 rows, all of Europe),
filters `country.isin(["DE", "AT"])`, writes `input/opsd_conventional_powerplants_de_at.csv`
(gitignored, regenerate-freely) — **1,019 rows** (879 DE + 140 AT).

## Are the German rows the same as `input/OPSD_conventional_power_plants_DE.csv`?

**Yes — identical underlying data**, once you know the trick: the EU file's
`name` column for German rows is actually the BNetzA `id` code (e.g. `BNA0012a`),
not a display name. It maps 1:1 onto the old DE file's `id` column.

- **877 IDs match** between the two files. For every one of them, `capacity`,
  `lat`/`lon`, and `energy_source` are exactly identical (floating-point equal).
- The EU file is missing **30 IDs** present in the old DE file — see below, this
  is the interesting part.
- The EU file drops the richer BNetzA-specific columns (block numbers, network
  operator, efficiency, CHP details, etc.) — it's a leaner, standardized export
  of the same source data, not an independent dataset.

## How many additional Austrian power plants?

**140 rows**, but with a data-quality catch: only **32 have coordinates**
(18 "BNA-sourced" rows, below, + 14 native ones). The other **108 are
coordinate-less** — mostly small native-Austrian hydro entries with no lat/lon
in this particular OPSD export.

| | rows | with coordinates | total capacity |
|---|---|---|---|
| Native-Austrian-sourced (real names) | 122 | 14 | 12,597 MW |
| Inherited from German BNetzA source (`BNA####`-style name, reclassified) | 18 | 18 | 3,653 MW |
| **Total** | **140** | **32** | **16,250 MW** |

## Do the redispatch "foreign"-labelled plants show up?

**Yes — and this is the key finding.** The 30 IDs missing from the EU file's
`DE` bucket are almost entirely the **Vorarlberger Illwerke fleet + Vianden** —
exactly the plants `A1/redispatch_prep.py`'s `"foreign"` entry-type regex flags
(16 redispatch entries: `Vianden`, `Kühtai`, `Vorarlberger Illwerke`/`Ilwerke`,
in various spellings).

The old DE-only OPSD file had **mislabeled them as `country=DE`** (they're
reported by German-side utilities/grid operators despite being physically
abroad). The new EU file **correctly reclassifies** them:

| Plant family | Redispatch entries referencing it | Reclassified as | Rows found |
|---|---|---|---|
| Vorarlberger Illwerke (Kopswerk I/II, Rodundwerk I/II, Vermuntwerk, Lünerseewerk, Silz, Kaunertal, Kühtai, Walgauwerk, Rellswerk, Obervermuntwerk II, Latschauwerk, Rifawerk, ...) | 14 entries | `AT` | 18 BNA-sourced rows (all with coords) + more under native AT names |
| Vianden | ~2 groups, many name variants (`VIANDEN_1`, `VIANDEN_3`, `VIANDEN_4`, `VIANDEN_10`, `VIANDEN_11`, `Vianden M1-M4`, `Vianden M5-M9`, `KURATIV_VIANDEN_POOL`, ...) | `LU` (Luxembourg) | 11 (all with coords) |
| Laufenburg (not a redispatch "foreign" entry, but the same DE-mislabeling pattern) | — | `CH` (Switzerland) | 1 |

**Consequence for the DE+AT filter as built:** it catches the Illwerke/Kühtai
side (Austria) but **misses Vianden entirely**, since Vianden is Luxembourg
(`LU`), not Austria. Full "foreign"-entry coverage from OPSD would need the
filter widened to `["DE", "AT", "LU"]` — matching the countries already used
for the PyPSA filter (`input/pypsa_powerplants_de_at_lu.csv`). Not yet changed;
flagged here for a decision.

## How does this compare to PyPSA's Austrian coverage?

PyPSA is substantially more complete for Austria:

| | rows | with coordinates | total capacity |
|---|---|---|---|
| OPSD (DE+AT filter, Austria only) | 140 | 32 (23%) | 16,250 MW |
| PyPSA Europe (Austria) | 476 | 476 (100%) | 27,057 MW |

PyPSA already includes wind (95 rows / 3,397 MW) and solar (168 rows / 926 MW)
for Austria, which the OPSD *conventional* file deliberately excludes by design
— part of the capacity gap is exactly that.

For the specific "foreign" plants: **PyPSA already has all of them, with
coordinates** — 12 Illwerke/Kühtai rows plus a single consolidated `Vianden`
entry (1,294 MW, aggregated to plant level rather than per-turbine like OPSD's
11 separate Vianden units). PyPSA is already the better/more complete source
for these specific plants; the new OPSD-AT data is useful mainly as a secondary
name-variant/cross-check source, not as a coordinate-coverage improvement for
the foreign entries specifically.

## Open follow-up

Widen `prep_opsd_countries()`'s default `countries` to `["DE", "AT", "LU"]` if
full "foreign"-entry OPSD coverage (including Vianden) is wanted — currently
left at `["DE", "AT"]` per the original request.
