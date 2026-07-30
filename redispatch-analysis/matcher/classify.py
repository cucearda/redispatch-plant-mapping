"""Aggregation classification: is this name one plant, a cluster, a pool, or a
grid-area / market construct that has no meaningful coordinate?
"""

import re

from normalize import significant_tokens


# ---------------------------------------------------------------------------
GRID_AREA_RE = re.compile(
    r"_CR_|_TEL\b|_WESTNETZ_|GESAMTEINSPEIS|VERTEILNETZ|"
    r"Abschaltbare\s*Last|Abruf.*abschaltbar|Auslaendische[-_ ]?Netzreserve|"
    r"^MRL\b|^10Y|^B[oö]rse$|Energy[-_ ]?Identification|"
    r"Amprion\s*NR\s*BK|^Netzregelverbund|"
    r"^Notfall[\-_ ]?RD|Notfallreserve",
    re.IGNORECASE,
)
# Pure market constructs / non-geographic — these stay skipped because no
# coordinate is meaningful for them.
GRID_AREA_NONGEO_RE = re.compile(
    r"Abschaltbare\s*Last|Abruf.*abschaltbar|Auslaendische[-_ ]?Netzreserve|"
    r"^MRL\b|^10Y|^B[oö]rse$|Energy[-_ ]?Identification|"
    r"Amprion\s*NR\s*BK|^Netzregelverbund|GESAMTEINSPEIS|"
    r"^Notfall[\-_ ]?RD|Notfallreserve",
    re.IGNORECASE,
)
CLUSTER_RE = re.compile(r"\bCluster\b|NWAK", re.I)
POOL_RE    = re.compile(r"\bpool\b|KW[-_ ]?Pool|KW[-_ ]?Park|\bgesamt\b|\bgruppe\b", re.I)
# Foreign filtering was previously used to skip Austrian / Luxembourg plants
# entirely. The user wants these geocoded (Vianden in LU, TIWAG / Vorarlberg /
# Kühtai in AT). Both filters are now no-ops; the region bbox is the only
# foreign-coordinate guard.
FOREIGN_MARKERS      = re.compile(r"(?!)", re.I)   # never matches
FOREIGN_PLACE_TOKENS = set()


def classify_aggregation(name):
    if GRID_AREA_RE.search(name):
        return "grid_area"
    if CLUSTER_RE.search(name):
        return "cluster"
    if POOL_RE.search(name):
        sig = significant_tokens(name)
        if not any(len(t) >= 5 for t in sig):
            return "grid_area"
        return "site_pool"
    return "single"
