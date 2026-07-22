# Redispatch dashboard

An interactive map of German redispatch: where it happens, how much, and how much
volume can't be placed on a map. Self-contained static HTML + [Leaflet](https://leafletjs.com/) —
no server or build tooling required.

## Files

| File | Role |
|---|---|
| `build_data.py` | Reads both raw exports + the plant-coordinate matches, aggregates, and writes `data.js`. |
| `data.js` | **Generated** — `const REDISPATCH_DATA = {…}`. Do not edit by hand. |
| `index.html` | The dashboard. Loads `data.js` locally and Leaflet + noUiSlider + map tiles from CDNs. |

Also writes/reads `data/Redispatch_Daten_2013_2026.csv` — a gitignored, regenerate-freely
cache combining the two source exports (see below).

## Usage

1. **Build the data** (needs `pandas`; run from the repo root or anywhere):

   ```bash
   python dashboard/build_data.py
   ```

   It reads `data/Redispatch_Daten_2013_2020.csv` and `data/Redispatch_Daten_2021_2026.csv`
   (the full 2013–2026 history) plus `results/redispatch_plant_matches.csv`, writes
   `dashboard/data.js`, and prints a sanity report (plant count, date range, and the
   volume split mapped / Börse / not-identified, which sums to 100%).

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
   `file://`; `data.js` is loaded via a `<script>` tag so no local server is
   needed). Map tiles and the Leaflet/slider libraries load from CDNs, so an
   **internet connection is required**.

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
- **"Plants shown" filter** — checkboxes for **match type** (matched to a plant in
  the reference index vs. geocoded-only, no confirmed plant ID) and **match
  confidence** (high / medium / low / none). Unticking a box removes those plants
  from the map and folds their volume into "Hidden by filter".

## Regenerating after a data refresh

Re-run `python dashboard/build_data.py` whenever either raw export or
`results/redispatch_plant_matches.csv` changes, then reload the page.
