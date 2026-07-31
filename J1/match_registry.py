"""Stages 1-2: direct matching against the OPSD and PSA registries.

Exact key equality first, then a difflib fuzzy pass above FUZZY_CUTOFF. Every
candidate is checked against the entry's energy class before it is accepted.
"""

import difflib

import pandas as pd

from config import FUZZY_CUTOFF, FUZZY_CUTOFF_OPSD
from normalize import norm
from sources import energy_ok


# ---------------------------------------------------------------------------
def _fuzzy_pick(query_key, candidate_keys, cutoff):
    if not query_key or not candidate_keys:
        return None, 0.0
    hits = difflib.get_close_matches(query_key, candidate_keys, n=3, cutoff=cutoff)
    if not hits:
        return None, 0.0
    best = hits[0]
    return best, round(difflib.SequenceMatcher(None, query_key, best).ratio(), 3)


def match_opsd(cleaned_key, plant_class, opsd):
    """Returns (status, score, row_idx_or_None, energy_match).
    Status is one of: none / exact / fuzzy. City matches are intentionally
    NOT returned here -- they are a lower-confidence fallback and run later
    via match_opsd_city, after the Wikipedia stage."""
    if not cleaned_key:
        return "none", 0.0, None, None

    # exact on cleaned key vs OPSD cleaned key
    hit = opsd[opsd["key_clean"] == cleaned_key]
    if not hit.empty:
        idx = hit.index[0]
        eok = energy_ok(plant_class, opsd.at[idx, "class"])
        return "exact", 1.0, idx, eok

    # exact against full name_bnetza norm
    hit = opsd[opsd["key_full"] == cleaned_key]
    if not hit.empty:
        idx = hit.index[0]
        eok = energy_ok(plant_class, opsd.at[idx, "class"])
        return "exact", 1.0, idx, eok

    # fuzzy on cleaned key
    keys = opsd["key_clean"].tolist()
    best, score = _fuzzy_pick(cleaned_key, keys, FUZZY_CUTOFF_OPSD)
    if best:
        idx = opsd.index[opsd["key_clean"] == best][0]
        eok = energy_ok(plant_class, opsd.at[idx, "class"])
        return "fuzzy", score, idx, eok

    return "none", 0.0, None, None


def match_opsd_city(cleaned_key, plant_class, opsd):
    """City-name exact match (lower confidence). Returns same shape as
    match_opsd. Prefers a row whose energy class agrees with the plant."""
    if not cleaned_key:
        return "none", 0.0, None, None
    hit = opsd[opsd["key_city"] == cleaned_key]
    if hit.empty:
        return "none", 0.0, None, None
    if plant_class:
        agree = hit[hit["class"] == plant_class.lower()]
        if not agree.empty:
            return "city", 1.0, agree.index[0], True
    idx = hit.index[0]
    eok = energy_ok(plant_class, opsd.at[idx, "class"])
    return "city", 1.0, idx, eok


# Tight-cluster threshold for PSA same-name disambiguation. ~0.2° ≈ 14–22 km
# (longer N-S than E-W in northern Germany), which comfortably covers an
# offshore wind farm's footprint while staying smaller than the distance
# between two genuinely different cities sharing a name.
PSA_CLUSTER_DEG = 0.2


def _psa_pick(hit, status, score):
    """Pick a row from same-name PSA candidates.

    If the candidates sit inside a tight bbox, this is a single site listed
    turbine-by-turbine (Wikinger has 70 SWT-5.0 rows, Meerwind ~80, etc.).
    Return the row closest to the cluster centroid AND report
    n_candidates=1 so `_confidence` doesn't slap on the ambiguity penalty
    that would otherwise collapse the right answer (0.95 -> 0.38 for n=70).

    Otherwise (multiple genuinely different sites sharing a name — e.g. two
    'Halle' substations in different states) keep the original behaviour:
    first row + real candidate count, ambiguity penalty applies."""
    if len(hit) == 1:
        return status, score, hit.index[0], True, 1
    lats = hit["lat"].astype(float)
    lons = hit["lon"].astype(float)
    if (lats.max() - lats.min() < PSA_CLUSTER_DEG
            and lons.max() - lons.min() < PSA_CLUSTER_DEG):
        lat_c, lon_c = lats.mean(), lons.mean()
        dist = (lats - lat_c) ** 2 + (lons - lon_c) ** 2
        idx = dist.idxmin()
        return status, score, idx, True, 1
    return status, score, hit.index[0], True, len(hit)


def match_psa(cleaned_key, plant_class, psa):
    """Returns (status, score, idx, energy_match, n_candidates).
    n_candidates = how many PSA rows share the matched key after the
    tight-cluster collapse in `_psa_pick`."""
    if not cleaned_key:
        return "none", 0.0, None, None, 0

    if plant_class:
        cand = psa[psa["class"] == plant_class.lower()]
    else:
        cand = psa
    if cand.empty:
        return "none", 0.0, None, None, 0

    hit = cand[cand["key_clean"] == cleaned_key]
    if not hit.empty:
        return _psa_pick(hit, "exact", 1.0)

    hit = cand[cand["key_full"] == cleaned_key]
    if not hit.empty:
        return _psa_pick(hit, "exact", 1.0)

    keys = cand["key_clean"].tolist()
    best, score = _fuzzy_pick(cleaned_key, keys, FUZZY_CUTOFF)
    if best:
        same = cand[cand["key_clean"] == best]
        return _psa_pick(same, "fuzzy", score)

    return "none", 0.0, None, None, 0
