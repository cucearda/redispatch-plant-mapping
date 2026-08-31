"""
postcodes.py — AJ1 pipeline helper: German/Austrian postcode (PLZ) lookups.

Two directions, both offline:

  plz_centroid(plz)             PLZ -> (lat, lon, place_name)
  coord_in_plz(lat, lon, plz)   "is this coordinate in this postcode?"
                                -> "inside" | "outside" | "no_plz"

Backed entirely by `pgeocode`'s GeoNames extract, which is already a
dependency of match_geo.py and already cached on disk — no download, no
extra data file, no geopandas/shapely.

**The containment test is a radius check against the postcode centroid**,
not true point-in-polygon. PLZ_RADIUS_KM was calibrated against real ground
truth rather than guessed: OPSD carries `postcode` AND `lat`/`lon` on the
same row, giving 843 plants whose coordinate is known to genuinely lie in
its stated postcode. Their distance to their own PLZ centroid runs:

    median 2.44 km | p90 6.28 | p95 8.08      (rural subset: median 2.91,
                                               p90 8.08, p95 9.83)

and the share of those genuine in-PLZ plants a threshold would accept:

    5 km -> 84.3%   7.5 km -> 94.0%   10 km -> 96.9%   15 km -> 98.2%

10 km is the knee: it accepts 96.9% of true in-PLZ plants and sits just
above the rural p95, so rural sites (whose postcodes are physically much
larger — rural PLZ centroids are ~4.9 km apart vs ~1.0 km urban) are not
systematically flagged. Past 15 km the curve plateaus at ~98.2%, because
the residual ~1.8% are genuine errors in OPSD's own data (the worst is
360 km) that no threshold can rescue.

So a verdict of "outside" means "further from the postcode centroid than
97% of genuine matches are" — a useful smell test, not a proof.

Usage:
    python postcodes.py 45711                  # centroid of a postcode
    python postcodes.py 45711 51.656 7.345     # containment check
"""

import math
import re
import sys

# Distance from a postcode's centroid beyond which a coordinate is called
# "outside" it. See the module docstring for the calibration behind 10 km.
PLZ_RADIUS_KM = 10.0

_NOMINATIM = {}     # country code -> pgeocode.Nominatim
_CENTROIDS = {}     # country code -> {plz: (lat, lon, place_name)}

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. AJ1 had no distance helper at all before
    this — match_exact.py compares squared degrees, which is fine for a
    same-site cluster test but not for reporting a real separation."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2) - math.radians(lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
def normalize_plz(plz):
    """(country, code) for a raw BNetzA `Postleitzahl`, or (None, None).

    The real column is messier than "5 digits": 62 of BNetzA's rows are
    Austrian, written either with an `A-` prefix (`A-6794`) or as a bare
    4-digit code (`9463`), and 71 distinct German codes have a leading zero
    that only survives because the column happens to read as object dtype
    (`prep_data.py` makes no guarantee, so always read it with dtype=str)."""
    if plz is None:
        return None, None
    s = str(plz).strip()
    if not s or s.lower() in ("nan", "none"):
        return None, None
    # pandas may hand back a float-ish "45711.0"
    s = re.sub(r"\.0$", "", s)
    if s.upper().startswith("A-"):
        return "at", s[2:].strip().zfill(4)
    if re.fullmatch(r"\d{5}", s):
        return "de", s
    if re.fullmatch(r"\d{4}", s):
        return "at", s          # bare 4-digit codes in this file are Austrian
    return None, None


def _centroids(country):
    """{plz: (lat, lon, place_name)} for one country, built once.

    A postcode can span several GeoNames rows (one per place name); their
    mean is the centroid, and the longest place name is kept as the label.
    """
    if country in _CENTROIDS:
        return _CENTROIDS[country]
    try:
        import pgeocode
    except Exception as e:                      # soft dependency, as in match_geo.py
        print(f"    pgeocode unavailable: {e}")
        _CENTROIDS[country] = {}
        return _CENTROIDS[country]

    if country not in _NOMINATIM:
        _NOMINATIM[country] = pgeocode.Nominatim(country)
    df = _NOMINATIM[country]._data
    df = df[["postal_code", "place_name", "latitude", "longitude"]].dropna(
        subset=["latitude", "longitude"])

    out = {}
    for code, grp in df.groupby("postal_code"):
        names = [str(n) for n in grp["place_name"].dropna().tolist()]
        out[str(code)] = (
            float(grp["latitude"].mean()),
            float(grp["longitude"].mean()),
            max(names, key=len) if names else "",
        )
    _CENTROIDS[country] = out
    return out


def plz_centroid(plz):
    """(lat, lon, place_name) for a postcode, or None if it doesn't resolve."""
    country, code = normalize_plz(plz)
    if not code:
        return None
    return _centroids(country).get(code)


def coord_in_plz(lat, lon, plz):
    """("inside" | "outside" | "no_plz", distance_km | None).

    Radius test against the postcode centroid — see the module docstring for
    why 10 km and what the verdict does and doesn't prove."""
    if lat is None or lon is None:
        return "no_plz", None
    c = plz_centroid(plz)
    if c is None:
        return "no_plz", None
    d = haversine_km(float(lat), float(lon), c[0], c[1])
    return ("inside" if d <= PLZ_RADIUS_KM else "outside"), d


def nearest_plz(lat, lon, country="de"):
    """(plz, distance_km) of the closest postcode centroid — "which postcode
    is this point in", answered as "whose centroid is nearest"."""
    best, best_d = None, float("inf")
    for code, (clat, clon, _name) in _centroids(country).items():
        d = haversine_km(float(lat), float(lon), clat, clon)
        if d < best_d:
            best, best_d = code, d
    return best, (best_d if best else None)


# ---------------------------------------------------------------------------
def main(argv):
    if len(argv) == 2:
        plz = argv[1]
        c = plz_centroid(plz)
        country, code = normalize_plz(plz)
        if c is None:
            print(f"{plz!r}: does not resolve (normalized: {country}/{code})")
            return
        print(f"{plz!r} -> {country.upper()} {code}  {c[2]}")
        print(f"   centroid: {c[0]:.5f}, {c[1]:.5f}")
    elif len(argv) == 4:
        plz, lat, lon = argv[1], float(argv[2]), float(argv[3])
        verdict, dist = coord_in_plz(lat, lon, plz)
        c = plz_centroid(plz)
        print(f"{lat:.5f}, {lon:.5f}  vs  PLZ {plz}"
              f"{' (' + c[2] + ')' if c else ''}")
        if dist is None:
            print(f"   {verdict}")
        else:
            print(f"   {verdict}  ({dist:.2f} km from centroid, "
                  f"threshold {PLZ_RADIUS_KM:.0f} km)")
    else:
        print(__doc__.strip().split("Usage:")[-1].strip())


if __name__ == "__main__":
    main(sys.argv)
