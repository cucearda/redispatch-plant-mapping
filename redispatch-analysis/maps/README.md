# maps

Spatial rendering of the joined redispatch events. Input is
`data/redispatch_joined_high_confidence.csv` (produced by
`pipeline/join_events.py`); output is interactive Leaflet HTML in
`results/maps/` and print-quality PNGs in `figures/maps/`.

| Script | What it produces |
|---|---|
| `kde_network_map.py` | **Network KDE.** Redispatch volume is spread along the transmission network itself rather than over open space: each event is attached to the grid, the density is evaluated along lixelised lines (bandwidth `H`, lixel length `DELTA`), and off-line contribution is attenuated by `PERP_FACTOR`. The result is a heat map that follows the corridors where congestion actually occurs. |
| `kde_area_map.py` | **Area KDE**, non-snapped. Plain 2-D kernel density with a Silverman bandwidth, plus the commercial battery-storage overlay. Use this as the naive baseline the network KDE is compared against. |
| `render_to_png.py` | Renders any of the generated HTML maps to PNG via headless Chrome — two variants each, `*_clean.png` (control panel hidden, for the paper) and `*_full.png` (as displayed, for slides). |
| `config.py` | All paths. Override with `MATCHER_DATA_DIR`, `GRID_DATA_DIR`, `MAPS_OUT_DIR`, `FIGURES_OUT`, `CHROME_BINARY`. |

## Requirements

The KDE maps need the PyPSA network export in `data/grid_data/`
(`buses.csv`, `lines.csv`, `links.csv`) — see `data/README_sources.md`. `render_to_png.py`
needs a Chrome or Chromium binary; it searches the usual locations and honours
`CHROME_BINARY`.

## Reading the output honestly

The maps are drawn from matched events only. Everything the matcher could not
place — and every aggregate entry that received a region centroid rather than a
plant location — is invisible on the map but is not zero.

`pipeline/join_events.py` prints the unmapped share at every filter step; quote
that number alongside any map, as a share of MWh rather than a share of names.
Rendering the same map twice, once unfiltered and once at
`--min-confidence 0.85`, is a cheap robustness check: features that survive both
are real, features that vanish were carried by low-confidence centroids.
