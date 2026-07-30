"""Paths for the map and figure scripts.

Nothing here is machine-specific: everything resolves relative to the repository
root unless the corresponding environment variable is set.

    MATCHER_DATA_DIR   raw + joined data              (default: <root>/data)
    GRID_DATA_DIR      PyPSA network export           (default: <root>/data/grid_data)
    MAPS_OUT_DIR       generated interactive maps     (default: <root>/results/maps)
    FIGURES_OUT        rendered PNGs for the paper    (default: <root>/figures/maps)
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATCHER_DATA_DIR", ROOT / "data"))

# Redispatch events joined to plant coordinates — the output of the matcher,
# joined back onto the event rows (see matcher_rulebased/README.md).
JOINED_CSV = Path(os.environ.get("JOINED_CSV", DATA / "redispatch_joined_high_confidence.csv"))

# Commercial battery storage sites (MaStR extract), for the siting overlay.
BATTERY_CSV = Path(os.environ.get("BATTERY_CSV", DATA / "commercial_battery_storage.csv"))

# PyPSA network export: buses.csv / lines.csv / links.csv.
GRID_DIR = Path(os.environ.get("GRID_DATA_DIR", DATA / "grid_data"))

MAPS_OUT = Path(os.environ.get("MAPS_OUT_DIR", ROOT / "results" / "maps"))
MAPS_OUT.mkdir(parents=True, exist_ok=True)
