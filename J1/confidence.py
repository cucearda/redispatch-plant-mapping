"""Final-confidence scoring.

One table, keyed by (source, status), scaled by the fuzzy ratio and penalised
for energy-class contradictions and ambiguous PSA candidate sets.
"""


# ---------------------------------------------------------------------------
def _confidence(source, status, score, energy_match, n_candidates):
    """Final-confidence score in [0, 1]. 0 = no match, used by the
    caller as the 'no result' default."""
    if status == "none" or score is None:
        return 0.0
    # Base by source × status.
    base = {
        ("opsd", "exact"): 1.00,
        ("opsd", "fuzzy"): 0.85,
        ("opsd", "city"):  0.55,
        ("psa",  "exact"): 0.95,
        ("psa",  "fuzzy"): 0.80,
        ("wiki", "match"): 0.85,
        ("ddg",  "match"): 0.70,
        ("gaz",  "match"): 0.50,
    }.get((source, status), 0.0)
    # Fuzzy: scale by the actual ratio.
    if status == "fuzzy" and score:
        base = base * float(score)
    # Energy-type penalty: a contradiction zeroes the score outright.
    if energy_match is False:
        base *= 0.0
    # PSA ambiguity penalty: many same-named candidates -> drop confidence.
    if source == "psa" and n_candidates and n_candidates > 1:
        base *= max(0.4, 1.0 - 0.1 * (n_candidates - 1))
    return round(min(max(base, 0.0), 1.0), 3)
