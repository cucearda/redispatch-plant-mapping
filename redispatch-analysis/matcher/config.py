"""Paths and tuning constants for the rule-based matcher.

Every path is resolved relative to the repository root, or overridden by an
environment variable, so nothing here is machine-specific. Copy `.env.example`
to `.env` (or export the variables) if your data lives elsewhere.

    REDISPATCH_2013_2020   raw netztransparenz export, 2013-2020
    REDISPATCH_2021_ON     raw netztransparenz export, 2021 onwards
    OPSD_CSV               OPSD conventional_power_plants_DE.csv
    PSA_CSV                powerplantmatching fleet export
    MATCHER_OUT_DIR        where results and the cache are written
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATCHER_DATA_DIR", ROOT / "data"))
OUT_DIR = Path(os.environ.get("MATCHER_OUT_DIR", ROOT / "results" / "matcher"))
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _p(env, default):
    return str(Path(os.environ.get(env, default)))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
# (path, encoding) — the two exports use different encodings, which is why the
# encoding travels with the path rather than being a single global.
REDISPATCH_SOURCES = [
    (_p("REDISPATCH_2021_ON", DATA / "Redispatch_Daten_2021_2026.csv"), "cp1252"),
    (_p("REDISPATCH_2013_2020", DATA / "Redispatch_Daten_2013_2020.csv"), "utf-8-sig"),
]
OPSD_PATH = _p("OPSD_CSV", DATA / "conventional_power_plants_DE.csv")
OPSD_ENC = "cp1252"
PSA_PATH = _p("PSA_CSV", DATA / "powerplantsPSA.csv")

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
OUTPUT_PATH = str(OUT_DIR / "plant_matcher_output.xlsx")
CACHE_PATH = str(OUT_DIR / "plant_matcher_cache.json")

# ---------------------------------------------------------------------------
# Web lookups
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "redispatch-thesis-research/3.0 (TUM seminar; academic use)",
    "Accept": "application/json",
}
WIKI_API = "https://de.wikipedia.org/w/api.php"
WIKI_SLEEP = 0.4
DDG_SLEEP = 2.0
NOM_SLEEP = 1.1

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
# Region bbox covers Germany + Luxembourg + Austria: DE-LU is the bidding zone,
# and Austrian hydro (Vorarlberg, TIWAG, Kühtai, Kaprun) appears in the
# redispatch list and should be geocoded rather than skipped.
DE_BBOX = (45.50, 55.10, 5.50, 17.20)   # lat_min, lat_max, lon_min, lon_max

FUZZY_CUTOFF = 0.90         # difflib ratio, PSA
FUZZY_CUTOFF_OPSD = 0.90    # difflib ratio, OPSD
PSA_MIN_CAPACITY = 1.0      # MW — drop rooftop-scale renewables before matching

USE_WIKI = True
USE_DDG = True              # only fires after a Wikipedia REJECT, not after a miss
USE_GAZ_NOM = False         # Nominatim inside the Stage 4b gazetteer (kept off)
USE_NOMINATIM_LAST_RESORT = True


def unique_path(path):
    """Return `path`, or `path (2)`, `path (3)`, … if it already exists.

    Keeps a re-run from silently overwriting the previous result set.
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem} ({i}){ext}"):
        i += 1
    return f"{stem} ({i}){ext}"
