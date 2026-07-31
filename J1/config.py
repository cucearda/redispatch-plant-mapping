"""Paths and tuning constants for the rule-based matcher (pipeline "J1").

Recreated from redispatch-analysis/matcher/config.py, adapted to read from
this repo's shared input/ (via paths.py) instead of a private data/ folder.

    REDISPATCH_FILE   the prepped, combined 2013-2026 redispatch export
                      (input/Redispatch_Daten_2013_2026.csv, built by
                      ../prep_data.py — run that first if it doesn't exist)
    OPSD_PATH         input/OPSD_conventional_power_plants_DE.csv
    PSA_PATH          input/pypsa_powerplants_de_at_lu.csv (prepped by
                      ../prep_data.py, DE+AT+LU PyPSA extract)
    OUTPUT_PATH       results/J1/redispatch_plant_matches.csv — same basename
                      A1 uses, for direct comparison
    CACHE_PATH        J1/temp_J1/plant_matcher_cache.json — regenerable scratch
"""

import os

from paths import INPUT_DIR, TEMP_DIR, RESULTS_DIR

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
REDISPATCH_FILE = os.path.join(INPUT_DIR, "Redispatch_Daten_2013_2026.csv")
REDISPATCH_ENC = "utf-8-sig"  # verified: prep_data.py writes this file as utf-8-sig

OPSD_PATH = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_DE.csv")
OPSD_ENC = "utf-8-sig"  # verified by direct read: no mojibake in name_bnetza

PSA_PATH = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
OUTPUT_PATH = os.path.join(RESULTS_DIR, "redispatch_plant_matches.csv")
CACHE_PATH = os.path.join(TEMP_DIR, "plant_matcher_cache.json")

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
