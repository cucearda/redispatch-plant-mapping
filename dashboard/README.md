# Redispatch dashboard

An interactive map of German redispatch: where it happens, how much, and how much
volume can't be placed on a map. Self-contained static HTML + [Leaflet](https://leafletjs.com/) —
no server or build tooling required.

## Files

| File | Role |
|---|---|
| `build_data.py` | Reads both raw exports + a pipeline's plant-coordinate matches, aggregates, and writes `data_<LABEL>.js`. |
| `data_A1.js` | **Generated** — `const REDISPATCH_DATA_A1 = {…}`, from `results/A1/redispatch_plant_matches.csv`. Do not edit by hand. |
| `data_J1.js` | **Generated** — `const REDISPATCH_DATA_J1 = {…}`, from `results/J1/redispatch_plant_matches.csv`. Do not edit by hand. |
| `data_AJ1.js` | **Generated** — `const REDISPATCH_DATA_AJ1 = {…}`, from `results/AJ1/redispatch_plant_matches.csv`. Do not edit by hand. |
| `index.html` | The dashboard. Loads all three `data_*.js` locally (a dropdown switches which one is shown) and Leaflet + noUiSlider + map tiles from CDNs. |

The dashboard is not tied to one matching pipeline: `build_data.py` normalises
each pipeline's own output schema (A1's `matched_id`/`confidence` label vs.
J1's `opsd_match`/`psa_match`/continuous `final_confidence`) to one common
shape via a small per-pipeline adapter (see `PIPELINES` in `build_data.py`).
Adding a fourth pipeline later means adding one adapter function plus one
`PIPELINES` entry — no changes to `index.html` beyond one more `<option>` and
`<script src>`.

Two things the adapter layer normalises beyond column names:

- **Fuel** — every pipeline's fuel labels run through `normalize_fuel()`,
  which folds the registries' English (`Natural gas` / `Natural Gas`), the
  BNetzA's German (`Erdgas`), and Wikipedia's free-text infobox values
  (`Braunkohle<br />(+ Erdgas für VGT)`) into one canonical set. For AJ1,
  `aj1_best_fuel()` also picks *which* source to believe, falling through
  the resolved identity's own fuel → Wikipedia → whichever registry produced
  a candidate → the redispatch export's coarse `PRIMAERENERGIEART` class,
  and reports that provenance as `fuel_basis` (shown in the tooltip).
- **Multi-plant bundles** — see below.

Also writes/reads `input/Redispatch_Daten_2013_2026.csv` — a gitignored, regenerate-freely
cache combining the two source exports (see below).

## Usage

1. **Build the data** (needs `pandas`; run from the repo root or anywhere):

   ```bash
   python dashboard/build_data.py
   ```

   It reads `input/Redispatch_Daten_2013_2020.csv` and `input/Redispatch_Daten_2021_2026.csv`
   (the full 2013–2026 history) plus **all three** pipelines'
   `results/<LABEL>/redispatch_plant_matches.csv`, writes `dashboard/data_A1.js`,
   `data_J1.js` and `data_AJ1.js`, and prints a sanity report per pipeline (plant
   count, date range, and the volume split mapped / Börse / not-identified, which
   sums to 100%; for AJ1 also the coordinate-confidence breakdown). Pass
   `--pipeline AJ1` to build just one.

   Combining the two exports required correcting two source-data quirks (not touched
   in the raw files themselves):
   - **Timezone**: the 2013-2020 export timestamps are labelled UTC; 2021-2026 is
     CET/CEST (German local time). Every timestamp is converted to Europe/Berlin
     local time before its calendar "day" is taken, so days line up correctly across
     the 2020/2021 boundary instead of drifting by 1-2 hours.
   - **Encoding**: 6 rows in `Redispatch_Daten_2021_2026.csv` have a corrupted byte in
     "erhöhen" (mixed-encoding artifact in the export). Direction is matched by
     substring (`erh` / `reduzieren`) rather than exact equality, so those rows still
     classify correctly instead of silently vanishing from the increase/decrease split.
   - Exact full-row duplicates in either export (568 in 2013-2020, 50 in 2021-2026 —
     apparent export artifacts) are dropped before aggregating, to avoid double-counting.

2. **Open the dashboard** — double-click `dashboard/index.html` (opens over
   `file://`; the `data_*.js` files are loaded via `<script>` tags so no local
   server is needed). Map tiles and the Leaflet/slider libraries load from
   CDNs, so an **internet connection is required**. The basemap is Esri's
   **World Light Gray Canvas** (a label-free base plus a transparent
   reference layer), chosen because it needs no API key: CARTO's keyless
   Positron endpoint that this used before still answers `200` but now
   returns tiles with an "API KEY REQUIRED" watermark burnt into the image,
   and OpenStreetMap's own tile server answers `418 Access blocked` for this
   kind of use. Swapping basemaps is one `L.tileLayer(...)` call in
   `index.html`. Use the **"Matching
   pipeline"** dropdown under the title to switch between A1, J1 and AJ1 — it
   reloads the map, size-legend scale, month list and filter panel for the
   selected pipeline's data.

## Multi-plant entries

382 of the 1154 distinct `BETROFFENE_ANLAGE` strings name **several plants
dispatched together** ("Boxberg, Jänschwalde, Lippendorf, Schkopau") and
carry 10.3% of all redispatch volume. The export reports one combined
`GESAMTE_ARBEIT_MWH` for the bundle and never says how it divided, so
`build_data.py` **splits a bundle's volume equally between the plants it
names** — each member circle gets 1/n — for any pipeline whose matches file
is keyed by member name. Each `PIPELINES` entry declares its own
`explode(raw_name) -> [key, …]`, because the right answer differs per
pipeline:

- **AJ1** is keyed by exploded members, and re-uses the pipeline's own
  `AJ1/redispatch_prep.py::bundle_segments` (imported, never reimplemented,
  so the dashboard can't drift from the keys AJ1 actually wrote). 4,823
  calls get split.
- **A1** is keyed by the whole bundle string and already places it at one
  centroid of its members, so it explodes to itself. That draws a bundle as a
  single circle at a mid-point where no plant stands — a deliberate A1
  property, not something the dashboard corrects.
- **J1** is keyed by exploded members too, but with its own looser comma rule
  (`J1/sources.py::_explode_plant_names`), so re-using AJ1's segmenter isn't
  guaranteed to reproduce J1's keys. Until that's checked it is left
  un-exploded, which is why 14.8% of J1's volume shows as "not identified"
  against A1's 2.1% — ~10 points of that gap is bundle raw names matching no
  J1 key, not plants J1 failed to resolve.

## What it shows

- **Circles** at each mapped plant — area ∝ total redispatch energy (MWh) in the
  selected window; colour = net direction (**blue** = net increase / *erhöhen*,
  **red** = net decrease / *reduzieren*, grey = balanced). Hover for the
  increase / decrease / net breakdown.
- **Length-based size legend** — the three reference circles depend only on how
  many days are selected, not on which days or which plants are visible: the top
  reference is (whole-dataset largest plant total ÷ total days) × window length,
  nice-rounded. So **panning the window keeps the same scale** (circles stay
  comparable as you move through time) and only **resizing** the window changes
  it — a day's scale is far smaller than a year's, as expected.
- **Date-range slider** (daily), with:
  - drag either handle to resize the window (changes the scale);
  - **drag the middle bar to pan** the window without changing its length (scale
    stays fixed);
  - a "Whole period" button and a month-jump dropdown.
- **"Not on the map"** panel — redispatch volume in the window with no location,
  split into **Börse** (market countertrade) and **not identified**, each as a
  share of the window's total. A third **"Hidden by filter"** row (hatched swatch)
  appears whenever the plant filter is hiding matched-plant volume, so the total
  always accounts for 100% of the window's redispatch.
- **Tooltip** — plant name, best-guess fuel (and where that fuel came from),
  the increase / decrease / net volume breakdown, then a footer with the entry
  kind and both confidence axes, e.g. `Coordinate: high — 3 sources agree
  (≤0.4 km apart)`.
- **"Plants shown" filter** — four groups, all folding hidden volume into
  "Hidden by filter" so the window still accounts for 100%:
  - **entry kind** — individual plant / aggregate (a cluster, substation,
    grid-area or control-reserve bucket with no single physical location) /
    unclear (nothing ruled it an aggregate, but no identity was resolved
    either);
  - **match type** — matched to a plant in the reference index vs.
    geocoded-only, no confirmed plant ID;
  - **identity confidence** (high / medium / low / none) — is this the right
    *plant*;
  - **coordinate confidence** (high / medium / low) — is this the right
    *location*. Shown only for a pipeline that scores the two separately
    (currently AJ1 alone; `meta.has_coord_conf`). **High** = ≥2 independent
    datasets (OPSD / PyPSA / Wikipedia) agree within 5 km and the point is
    their centroid; **medium** = a real plant-level point but from a single
    dataset, a disputed pick, or a BNetzA postcode centroid; **low** = a
    town / grid-area / Bundesland centroid, which is *not* a plant site and
    can sit tens of km from anything generating power. Low-confidence points
    are drawn hollow with a dashed outline so they read as area centroids
    even with the filter left on.

## Regenerating after a data refresh

Re-run `python dashboard/build_data.py` whenever either raw export or either
pipeline's `redispatch_plant_matches.csv` changes, then reload the page.
