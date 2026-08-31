"""
coords.py — AJ1 pipeline helper: reconcile the coordinates that different
datasets give for the same redispatch entry, and score how much the result
deserves to be trusted.

Replaces the strict precedence ladder assemble_results.py used to run
(registry -> wikipedia -> area fallback), which read only the winning
candidate's coordinate and never compared it to anything. Three concrete
problems with that:

  - 292 of 300 Wikipedia coordinates were discarded, because wiki was only
    consulted when everything else was blank. It never confirmed or
    disputed a registry point.
  - A BNetzA winner has no coordinate (its cleaned file carries none), so
    104 entries ended up with nothing even though a sibling OPSD/PyPSA
    candidate *in the same row* had a perfectly good point.
  - Whether the candidates agreed with each other was simply never asked.

What this does instead: reduce every dataset to one point, ask whether they
agree, use their centroid when they do, fall back to the best guess's own
point when they don't, and report the disagreement as a score.

AGREE_KM = 5 is calibrated, not guessed. On the 131 plants that had both a
registry and a Wikipedia coordinate, separation was median 0.13 km, p90
2.59 km, max 15.5 km — only 6 pairs above 5 km and none above 20 km. Real
agreement is tight, so 5 km flags genuine outliers without punishing the
normal block-vs-site offset between registries.

BNetzA's postcode is deliberately NOT mixed into the consensus centroid or
the spread: a PLZ centroid can sit kilometres from the actual site (see
postcodes.py — median 2.4 km, p95 8.1 km even for correct pairs) and would
drag both. It is used for exactly two things: as the coordinate of last
resort when no dataset offers a real one, and as an independent containment
check on whatever coordinate was chosen.
"""

from postcodes import PLZ_RADIUS_KM, coord_in_plz, haversine_km, plz_centroid

# Cross-dataset agreement threshold — see module docstring for calibration.
AGREE_KM = 5.0
# Disagreement at or beyond this gets the maximum score penalty.
MAX_SPREAD_KM = 50.0

# Coordinate-quality score. Deliberately independent of guess_basis /
# llm_confidence, which remain the separate *identity* axis: this number
# answers "how much do I trust this location", not "is this the right plant".
BASE_SCORE = {
    "consensus":           0.95,
    "single_source":       0.75,
    "best_guess_disputed": 0.55,
    "postcode":            0.40,
    "town":                0.25,
    "area":                0.15,
    "state":               0.05,
    "none":                0.00,
}
PLZ_FACTOR = {"inside": 1.05, "outside": 0.85}

# Sources that carry a real plant location, in the priority order used to
# fall back when the best guess's own registry has no coordinate. Matches
# match_exact.py's registry priority.
PRECISE_ORDER = ("opsd", "psa", "wikipedia")


def centroid(points):
    """Mean of (lat, lon) pairs. Fine at Germany's scale — the largest set
    this ever averages is one registry's tight same-site cluster."""
    pts = [p for p in points if p and p[0] is not None and p[1] is not None]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def max_pairwise_km(points):
    """Widest separation within a set of points, in km. 0.0 for <2 points."""
    pts = [p for p in points if p]
    if len(pts) < 2:
        return 0.0
    return max(haversine_km(a[0], a[1], b[0], b[1])
               for i, a in enumerate(pts) for b in pts[i + 1:])


def _spread_penalty(spread_km):
    """1.0 while the sources agree, decaying linearly to 0.5 at
    MAX_SPREAD_KM and floored there."""
    if spread_km <= AGREE_KM:
        return 1.0
    if spread_km >= MAX_SPREAD_KM:
        return 0.5
    frac = (spread_km - AGREE_KM) / (MAX_SPREAD_KM - AGREE_KM)
    return 1.0 - 0.5 * frac


def reconcile(sources, best_guess_registry=None, plz_list=(), geo=None):
    """Decide one entry's coordinate from everything that has an opinion.

    sources   {"opsd"|"psa"|"wikipedia": (lat, lon)} — already reduced to
              one representative point per dataset (a registry's several
              candidate ids are a tight same-site cluster, so collapsing
              them to their own centroid first stops a legitimate
              turbine-by-turbine listing from inflating the disagreement).
    best_guess_registry  which dataset the identity stage picked, used only
              to break a disagreement.
    plz_list  postcodes of the entry's BNetzA candidates.
    geo       the area-level fallback row from match_geo.py, if any:
              {"lat","lon","geo_source","coord_precision","area_label"}.

    Returns the coordinate columns as a dict.
    """
    sources = {k: v for k, v in sources.items()
               if v and v[0] is not None and v[1] is not None}
    names = [n for n in PRECISE_ORDER if n in sources]
    points = [sources[n] for n in names]

    out = {
        "lat": None, "lon": None,
        "coord_source": None, "coord_precision": None,
        "coord_basis": "none", "coord_score": 0.0,
        "coord_spread_km": None,
        "coord_n_sources": len(names),
        "coord_sources": " | ".join(names) if names else None,
        "plz_check": "no_plz", "area_label": None,
    }

    spread = max_pairwise_km(points) if len(points) > 1 else None

    if len(points) >= 2 and spread <= AGREE_KM:
        out["lat"], out["lon"] = centroid(points)
        out["coord_basis"] = "consensus"
        out["coord_source"] = "consensus"
        out["coord_precision"] = "plant"

    elif len(points) >= 2:
        # They disagree; trust the dataset the identity stage picked, and
        # fall back through the priority order if that one has no point.
        pick = best_guess_registry if best_guess_registry in sources else None
        if pick is None:
            pick = names[0]
        out["lat"], out["lon"] = sources[pick]
        out["coord_basis"] = "best_guess_disputed"
        out["coord_source"] = pick
        out["coord_precision"] = "plant"

    elif len(points) == 1:
        out["lat"], out["lon"] = points[0]
        out["coord_basis"] = "single_source"
        out["coord_source"] = names[0]
        out["coord_precision"] = "plant"

    else:
        # No dataset has a real plant location. A BNetzA postcode centroid
        # is the next best thing — coarser than a plant point but tied to
        # the matched registry row's own address, so better than a fallback
        # derived by parsing the entry's name.
        plz_pt = next((plz_centroid(p) for p in plz_list if plz_centroid(p)), None)
        if plz_pt:
            out["lat"], out["lon"] = plz_pt[0], plz_pt[1]
            out["coord_basis"] = "postcode"
            out["coord_source"] = "bnetza_plz"
            out["coord_precision"] = "postcode"
            # The coordinate IS the postcode centroid, so a containment
            # check on it would be circular — don't award the bonus.
            out["plz_check"] = "self"
        elif geo and geo.get("lat") is not None:
            out["lat"], out["lon"] = geo["lat"], geo["lon"]
            out["coord_basis"] = "area_fallback"
            out["coord_source"] = geo.get("geo_source")
            out["coord_precision"] = geo.get("coord_precision")
            out["area_label"] = geo.get("area_label")

    out["coord_spread_km"] = round(spread, 3) if spread is not None else None

    # ---- independent postcode containment check -----------------------------
    if out["plz_check"] != "self" and out["lat"] is not None:
        for p in plz_list:
            verdict, _dist = coord_in_plz(out["lat"], out["lon"], p)
            if verdict != "no_plz":
                out["plz_check"] = verdict
                if verdict == "inside":
                    break       # one confirmation is enough
        # several BNetzA candidates can carry different postcodes; "inside"
        # any of them counts, so only an all-miss stays "outside".

    # ---- score --------------------------------------------------------------
    basis = out["coord_basis"]
    base = BASE_SCORE.get(
        out["coord_precision"] if basis == "area_fallback" else basis, 0.0)
    score = base
    if basis == "consensus":
        score += 0.025 * max(0, len(points) - 2)
    if basis == "best_guess_disputed":
        score *= _spread_penalty(spread)
    score *= PLZ_FACTOR.get(out["plz_check"], 1.0)
    out["coord_score"] = round(min(1.0, max(0.0, score)), 3)

    return out


def points_for_ids(ids, lookup, registry):
    """Centroid of the coordinates of one registry's candidate ids.

    `ids` is match_exact.py's pipe-joined list already split; `lookup` is
    {(registry, str(id)): (lat, lon)}. Ids missing from the lookup are
    skipped rather than treated as (0, 0)."""
    pts = [lookup.get((registry, str(i))) for i in ids]
    return centroid([p for p in pts if p])
