"""
match_geo.py — AJ1 pipeline, step 6: AREA-level coordinates for entries no
plant-level stage could locate.

Ported from J1/match_geo.py — the resolvers and every lookup table
(STATE_BBOX, REGION_BBOX, DSO_CODE_TO_STATE, direction handling) are J1's,
verbatim, because they are hand-built reference data rather than logic and
re-deriving them would only introduce differences. Changed for AJ1: J1's
four `config` constants are inlined below, and a `main()` drives the
resolvers as a pipeline stage writing temp_AJ1/matches_geo.csv.

These coordinates locate an AREA, NOT A PLANT. A gazetteer hit is the town
the plant is named after; a grid-area or state hit is a region centroid that
may sit tens of kilometres from anything generating electricity. They exist
so aggregate entries (substations, clusters, grid codes — 220 names AJ1
otherwise drops entirely) can enter a spatial read at all.

Every row therefore carries `coord_precision`, and assemble_results.py
propagates it to the final output:

    town   gazetteer / nominatim   the named settlement
    area   grid_area / region      DSO or region centroid
    state  state_centroid          Bundesland centroid, optionally halved

Anything consuming the output must not render `area`/`state` as a plant
pin — that would reintroduce, as a location error, exactly the
false-positive class match_exact.py refuses to make about identity.

Input:  temp_AJ1/redispatch_entries.csv (every name, incl. aggregates)
        temp_AJ1/matches_exact.csv      (to skip what already has a plant)
Output: temp_AJ1/matches_geo.csv
"""

import os
import re
import time

import pandas as pd
import requests

from paths import TEMP_DIR
from normalize import norm, norm_tokens, significant_tokens

# Inlined from J1/config.py. DE_BBOX's lat_max is AJ1's widened 55.50 (see
# match_wikipedia.py) rather than J1's 55.10, so the two AJ1 stages agree on
# what counts as inside Germany.
HEADERS = {
    "User-Agent": "redispatch-thesis-research/3.0 (TUM seminar; academic use)",
    "Accept": "application/json",
}
NOM_SLEEP = 1.1                          # Nominatim asks for <=1 req/sec
DE_BBOX = (45.50, 55.50, 5.50, 17.20)    # lat_min, lat_max, lon_min, lon_max
USE_GAZ_NOM = False                      # Nominatim inside match_gazetteer


# ---------------------------------------------------------------------------
PLACE_MAP = {}

def load_gazetteer():
    try:
        import pgeocode
        print("Loading German place gazetteer (pgeocode)...")
        df = pgeocode.Nominatim("de")._data[
            ["place_name", "latitude", "longitude", "state_name"]
        ].dropna(subset=["place_name", "latitude", "longitude"])
        resolved_dense = 0
        for pname, grp in df.groupby("place_name"):
            lat, lon = grp["latitude"], grp["longitude"]
            if (len(grp) > 1
                    and (lat.max() - lat.min() > 0.6
                         or lon.max() - lon.min() > 0.9)):
                # Ambiguous (multiple cities share the name) -> pick the
                # postcode whose coordinates lie closest to the per-cluster
                # median. This biases toward the densest cluster, which is
                # the dominant city of that name (Halle -> Halle Saale).
                lat_m = lat.median(); lon_m = lon.median()
                dist = (lat - lat_m) ** 2 + (lon - lon_m) ** 2
                idx = dist.idxmin()
                PLACE_MAP[norm(pname)] = (float(lat.loc[idx]),
                                          float(lon.loc[idx]),
                                          grp["state_name"].loc[idx])
                resolved_dense += 1
            else:
                PLACE_MAP[norm(pname)] = (float(lat.mean()),
                                          float(lon.mean()),
                                          grp["state_name"].iloc[0])
        print(f"  {len(PLACE_MAP)} places  "
              f"({resolved_dense} ambiguous names resolved by densest cluster)")
    except Exception as e:
        print(f"  pgeocode unavailable ({e})")


def nominatim_forward(place):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "countrycodes": "de", "format": "json",
                    "limit": 1, "addressdetails": 1},
            headers=HEADERS, timeout=10,
        )
        if resp.ok and resp.json():
            hit = resp.json()[0]
            if hit.get("type") in {"city", "town", "village", "hamlet",
                                    "administrative", "municipality"}:
                addr = hit.get("address", {})
                return (float(hit["lat"]), float(hit["lon"]),
                        addr.get("state"))
    except Exception as e:
        print(f"    Nominatim error: {e}")
    return None


# ---------------------------------------------------------------------------
# Bounding boxes are approximate (administrative envelope incl. coastline).
STATE_BBOX = {
    "schleswig-holstein":     (53.36, 55.10,  7.86, 11.30),
    "hamburg":                (53.39, 53.74,  9.73, 10.33),
    "bremen":                 (53.01, 53.61,  8.48,  8.99),
    "niedersachsen":          (51.30, 53.89,  6.65, 11.60),
    "nordrhein-westfalen":    (50.32, 52.53,  5.86,  9.46),
    "mecklenburg-vorpommern": (53.10, 54.69, 10.59, 14.41),
    "brandenburg":            (51.36, 53.56, 11.27, 14.77),
    "berlin":                 (52.34, 52.68, 13.09, 13.76),
    "sachsen-anhalt":         (50.93, 53.04, 10.56, 13.18),
    "sachsen":                (50.17, 51.68, 11.87, 15.04),
    "thueringen":             (50.20, 51.65,  9.88, 12.65),
    "hessen":                 (49.39, 51.66,  7.77, 10.24),
    "rheinland-pfalz":        (48.97, 50.94,  6.11,  8.51),
    "saarland":               (49.11, 49.64,  6.36,  7.41),
    "bayern":                 (47.27, 50.57,  8.97, 13.84),
    "baden-wuerttemberg":     (47.53, 49.79,  7.51, 10.50),
}
STATE_ALIASES = {
    # common 2-letter codes
    "sh": "schleswig-holstein", "hh": "hamburg", "hb": "bremen",
    "ni": "niedersachsen", "nw": "nordrhein-westfalen", "nrw": "nordrhein-westfalen",
    "mv": "mecklenburg-vorpommern", "bb": "brandenburg", "be": "berlin",
    "st": "sachsen-anhalt", "sn": "sachsen", "th": "thueringen",
    "he": "hessen", "rp": "rheinland-pfalz", "sl": "saarland",
    "by": "bayern", "bw": "baden-wuerttemberg",
    # spelling variants the cleaner produces
    "bayern": "bayern", "bavaria": "bayern",
    "sachsen": "sachsen", "saxony": "sachsen",
    "thueringen": "thueringen", "thuringia": "thueringen",
    "hessen": "hessen", "hesse": "hessen",
    "niedersachsen": "niedersachsen", "lowersaxony": "niedersachsen",
    "brandenburg": "brandenburg", "berlin": "berlin", "bremen": "bremen",
    "hamburg": "hamburg", "saarland": "saarland",
    "schleswigholstein": "schleswig-holstein",
    "mecklenburgvorpommern": "mecklenburg-vorpommern",
    "rheinlandpfalz": "rheinland-pfalz",
    "sachsenanhalt": "sachsen-anhalt",
    "nordrheinwestfalen": "nordrhein-westfalen",
    "badenwuerttemberg": "baden-wuerttemberg",
}
DIRECTION_TOKENS = {
    "nord":  "N", "norden": "N", "north": "N",
    "sued":  "S", "sueden": "S", "south": "S",
    "ost":   "E", "osten":  "E", "east":  "E",
    "west":  "W", "westen": "W",
}
# Adjacent direction-token pairs collapse into a diagonal. The cleaner splits
# hyphens to spaces, so 'Nord-Ost' arrives as ['nord', 'ost'] and we recover
# the NE direction here.
COMPOUND_DIRECTIONS = {
    ("N", "E"): "NE", ("E", "N"): "NE",
    ("N", "W"): "NW", ("W", "N"): "NW",
    ("S", "E"): "SE", ("E", "S"): "SE",
    ("S", "W"): "SW", ("W", "S"): "SW",
}


def _extract_direction(tokens):
    """First direction-token hit wins. If the very next token is also a
    direction and the pair maps to a diagonal (N+E, S+W, ...), return the
    compound. Otherwise the bare cardinal."""
    if not tokens:
        return None
    for i, t in enumerate(tokens):
        d = DIRECTION_TOKENS.get(t)
        if not d:
            continue
        if i + 1 < len(tokens):
            d2 = DIRECTION_TOKENS.get(tokens[i + 1])
            if d2:
                pair = COMPOUND_DIRECTIONS.get((d, d2))
                if pair:
                    return pair
        return d
    return None


def _resolve_state(token):
    k = norm(token)
    if k in STATE_ALIASES:
        return STATE_ALIASES[k]
    return None


def state_centroid(state, direction=None):
    """Return (lat, lon) for a state, optionally biased to a directional half
    or quarter of its bounding box.

    Cardinals N/S/E/W halve the bbox along one axis; the diagonals
    NE/NW/SE/SW take the corresponding quarter. Bbox midpoints are computed
    once UP FRONT so a diagonal doesn't shrink one axis before reading the
    other (which would offset the midpoint)."""
    lat_min, lat_max, lon_min, lon_max = STATE_BBOX[state]
    lat_mid = (lat_min + lat_max) / 2
    lon_mid = (lon_min + lon_max) / 2
    if direction in ("N", "NE", "NW"):
        lat_min = lat_mid
    if direction in ("S", "SE", "SW"):
        lat_max = lat_mid
    if direction in ("E", "NE", "SE"):
        lon_min = lon_mid
    if direction in ("W", "NW", "SW"):
        lon_max = lon_mid
    return ((lat_min + lat_max) / 2, (lon_min + lon_max) / 2)


def match_state(tokens):
    """If the cleaned token list refers to a German Bundesland (optionally
    with a directional half), return centroid coords. Otherwise None.

    Scans the WHOLE token list for both a direction modifier and a state
    name, so trailing junk like 'EE' (erneuerbar marker) doesn't hide the
    direction. State resolution tries adjacent two-token concatenations
    first (covers two-word names like 'baden wuerttemberg'), then bare
    single tokens (covers 'bayern', codes like 'sh' / 'nrw').

    Examples:
      ['brandenburg']                          -> Brandenburg centroid
      ['bayern', 'nord']                       -> upper-half Bayern
      ['sachsen-anhalt']                       -> Sachsen-Anhalt centroid
      ['baden', 'wuerttemberg', 'nord', 'ee']  -> upper-half Baden-Württemberg
      ['sh', 'sued']                           -> lower-half Schleswig-Holstein
    """
    if not tokens:
        return None
    direction = _extract_direction(tokens)
    # State candidates, most specific first:
    #   1. adjacent two-token concatenations  -> 'baden'+'wuerttemberg'
    #   2. bare single tokens                 -> 'bayern', 'sh', 'nrw'
    candidates = []
    for i in range(len(tokens) - 1):
        candidates.append(tokens[i] + tokens[i + 1])
    candidates.extend(tokens)
    for tok in candidates:
        state = _resolve_state(tok)
        if state:
            lat, lon = state_centroid(state, direction)
            return dict(state=state, direction=direction, lat=lat, lon=lon)
    return None


# ---------------------------------------------------------------------------
# Major German Regionen / Landschaften that show up in DSO area names.
# Bbox -> centroid; direction optionally biases to the matching half.
REGION_BBOX = {
    # NRW
    "sauerland":       (50.70, 51.50,  7.70,  9.00),
    "muensterland":    (51.70, 52.50,  6.70,  8.00),
    "ruhrgebiet":      (51.30, 51.70,  6.60,  7.50),
    "rheinland":       (50.50, 51.50,  6.00,  7.50),
    "bergisches":      (50.90, 51.30,  7.00,  7.80),
    "niederrhein":     (51.30, 51.90,  6.10,  6.80),
    # RP
    "nahe":            (49.50, 50.00,  7.00,  7.90),
    "trier":           (49.50, 50.00,  6.30,  7.00),
    "hunsrueck":       (49.70, 50.40,  7.00,  7.80),
    "eifel":           (50.00, 50.70,  6.20,  7.20),
    "westerwald":      (50.50, 51.10,  7.50,  8.20),
    "pfalz":           (49.10, 49.80,  7.60,  8.40),
    # NI
    "emsland":         (52.40, 53.40,  6.90,  7.90),
    "ostfriesland":    (53.20, 53.70,  6.60,  7.80),
    # MV / BB
    "vorpommern":      (53.50, 54.50, 12.80, 14.40),
    "uckermark":       (52.90, 53.50, 13.50, 14.40),
    "altmark":         (52.50, 53.00, 11.00, 12.00),
    "prignitz":        (52.80, 53.30, 11.50, 12.50),
    # SN / TH
    "lausitz":         (51.00, 52.50, 13.50, 15.00),
    "erzgebirge":      (50.30, 51.00, 12.50, 13.60),
    "vogtland":        (50.20, 50.70, 12.00, 12.50),
    # central
    "harz":            (51.50, 51.90, 10.40, 11.20),
    "rheinmain":       (49.90, 50.30,  8.40,  8.90),
    # BY
    "schwaben":        (47.50, 49.00,  9.70, 11.50),
    "franken":         (49.20, 50.50,  9.50, 12.00),
    "oberpfalz":       (49.00, 50.00, 11.00, 13.00),
    "niederbayern":    (47.70, 49.40, 11.80, 13.80),
    "oberbayern":      (47.40, 48.60, 10.50, 12.70),
    "allgaeu":         (47.40, 47.90, 10.00, 10.80),
}


def region_centroid(region, direction=None):
    """Mirrors state_centroid: cardinals -> half, diagonals -> quarter."""
    lat_min, lat_max, lon_min, lon_max = REGION_BBOX[region]
    lat_mid = (lat_min + lat_max) / 2
    lon_mid = (lon_min + lon_max) / 2
    if direction in ("N", "NE", "NW"): lat_min = lat_mid
    if direction in ("S", "SE", "SW"): lat_max = lat_mid
    if direction in ("E", "NE", "SE"): lon_min = lon_mid
    if direction in ("W", "NW", "SW"): lon_max = lon_mid
    return ((lat_min + lat_max) / 2, (lon_min + lon_max) / 2)


# DSO operator codes seen in redispatch grid_area names -> dominant state.
# Confidence stays low (state-level) so users can manually refine.
DSO_CODE_TO_STATE = {
    # Amprion DSO
    "westnetz": "nordrhein-westfalen",
    # 50Hertz DSO partners (eastern Germany)
    "edis":     "brandenburg",
    "mns":      "sachsen-anhalt",
    "ava":      "sachsen-anhalt",
    "avacon":   "sachsen-anhalt",
    "wema":     "mecklenburg-vorpommern",
    "wemag":    "mecklenburg-vorpommern",
    "ten":      "brandenburg",            # 50Hertz internal area code
    "lvn":      "brandenburg",
    "swsw":     "sachsen",
    # Bayern
    "lew":      "bayern",                 # Lechwerke -> Schwaben
    # Saarland
    "vse":      "saarland",
    # other regional players
    "enbw":     "baden-wuerttemberg",
    "envia":    "sachsen",
    "ewe":      "niedersachsen",
    "syna":     "hessen",
    "stromnetz": "berlin",
    "vattenfall": "berlin",
    # specific big sites that show up in operator-pool names
    "basf":     "rheinland-pfalz",        # Ludwigshafen
    "infraleuna": "sachsen-anhalt",       # Leuna
}

# Tokens to drop when parsing a grid_area name (descriptor noise that doesn't
# carry geographic info). Note: WESTNETZ is intentionally NOT dropped — it
# both classifies the entry as grid_area AND identifies the DSO.
GRID_AREA_DROP = {
    "cr", "tel", "netz", "verteilnetz", "uebertragungsnetz",
    "ee", "sonstige", "sonstiges", "konventionell", "konv",
    "kwk", "photovoltaik", "wind", "solar", "pv",
    "amp", "amprion", "50h", "50hertz", "ttg", "tt", "tennet",
    "tbw", "transnetbw",
    "nr", "bk",
}


def _grid_area_tokens(name):
    toks = [norm(t) for t in re.split(r"[_\s/\-]+", str(name)) if t]
    return [t for t in toks
            if t and t not in GRID_AREA_DROP and not t.isdigit()]


def match_grid_area(name):
    """Try to extract a regional centroid for a grid_area name.

    Priority (most specific first):
      0. City via PLACE_MAP (pgeocode gazetteer) — point-precise
      1. Region (Sauerland, Emsland, Lausitz, ...)
      2. Bundesland (Bayern, Sachsen, ...)
      3. DSO operator code (LEW -> Bayern, EDIS -> Brandenburg, ...)
    A directional modifier (Nord/Süd/Ost/West) halves the bbox for
    region/state matches; it's recorded in the label for city matches but
    does not displace the centroid (pgeocode gives a point, not a bbox, so
    a 'half-north' of it is meaningless).
    Returns (source, confidence, lat, lon, area_label) or None.
    """
    toks = _grid_area_tokens(name)
    if not toks:
        return None

    direction = _extract_direction(toks)

    # 0. City — sometimes a grid_area name actually contains a town
    #    (e.g. '50H_EDIS_CR_WITTENBERG_NORD'). Try the joined token string
    #    first (catches multi-word names like 'frankfurt oder'), then each
    #    individual token. Skip tokens we already know are DSO codes,
    #    direction markers, or descriptor noise so we don't accidentally
    #    map 'edis' / 'nord' / 'pool' to some same-spelled village.
    place_candidates = []
    if len(toks) > 1:
        place_candidates.append(" ".join(toks))
    for t in toks:
        if (t in DIRECTION_TOKENS
                or t in DSO_CODE_TO_STATE
                or t in REGION_BBOX
                or _resolve_state(t)):
            continue
        place_candidates.append(t)
    for cand in place_candidates:
        k = norm(cand)
        if k in PLACE_MAP:
            lat, lon, state = PLACE_MAP[k]
            label = cand.title() + (f"_{direction}" if direction else "")
            # Point-precise; outranks region/state/DSO centroids.
            return ("grid_area_city", 0.45, lat, lon, label)

    # 1. region
    for t in toks:
        if t in REGION_BBOX:
            lat, lon = region_centroid(t, direction)
            label = t + (f"_{direction}" if direction else "")
            return ("grid_area_region",
                    0.30 if direction is None else 0.25,
                    lat, lon, label)

    # 2. state alias
    for t in toks:
        st = _resolve_state(t)
        if st:
            lat, lon = state_centroid(st, direction)
            label = st + (f"_{direction}" if direction else "")
            return ("grid_area_state",
                    0.25 if direction is None else 0.20,
                    lat, lon, label)

    # 3. DSO code
    for t in toks:
        if t in DSO_CODE_TO_STATE:
            st = DSO_CODE_TO_STATE[t]
            lat, lon = state_centroid(st, direction)
            label = f"{t}->{st}" + (f"_{direction}" if direction else "")
            return ("grid_area_dso",
                    0.20 if direction is None else 0.15,
                    lat, lon, label)

    return None


def match_region(tokens):
    """Like match_state but for REGION_BBOX. Used in the normal flow for
    plant names that contain a region name (e.g. 'EMSLAND_DTB0' has
    'emsland' which isn't a state or city but is a known Region)."""
    if not tokens:
        return None
    direction = _extract_direction(tokens)
    for t in tokens:
        if t in REGION_BBOX:
            lat, lon = region_centroid(t, direction)
            return dict(region=t, direction=direction, lat=lat, lon=lon)
    return None


def match_gazetteer(tokens):
    """Returns dict with status/place/state/lat/lon."""
    if not tokens:
        return dict(status="none", place=None, state=None, lat=None, lon=None)
    candidates = []
    candidates.append(" ".join(tokens))
    if len(tokens) > 1:
        candidates.append(tokens[0])
        candidates.append(tokens[-1])
    for cand in candidates:
        k = norm(cand)
        if k in PLACE_MAP:
            lat, lon, state = PLACE_MAP[k]
            return dict(status="match", place=cand.title(), state=state,
                        lat=lat, lon=lon)
    if USE_GAZ_NOM:
        hit = nominatim_forward(tokens[0])
        time.sleep(NOM_SLEEP)
        if hit:
            lat, lon, state = hit
            return dict(status="match", place=tokens[0].title(), state=state,
                        lat=lat, lon=lon)
    return dict(status="none", place=None, state=None, lat=None, lon=None)


# ---------------------------------------------------------------------------
# AJ1 stage driver
# ---------------------------------------------------------------------------
ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")
EXACT   = os.path.join(TEMP_DIR, "matches_exact.csv")
OUT     = os.path.join(TEMP_DIR, "matches_geo.csv")

# Which resolver produced the coordinate -> how precise it actually is.
# Everything below "town" is a centroid; see the module docstring.
PRECISION = {
    "gazetteer":        "town",
    "nominatim":        "town",
    "grid_area_city":   "town",
    "grid_area_region": "area",
    "grid_area_dso":    "area",
    "region_centroid":  "area",
    "grid_area_state":  "state",
    "state_centroid":   "state",
}


def _has_plant_coords(ex_row):
    """True if a registry that carries lat/lon already matched this entry.
    OPSD and PyPSA both have coordinates; BNetzA does not, so a BNetzA-only
    match still needs an area-level fallback."""
    if not ex_row:
        return False
    for reg in ("opsd", "psa"):
        v = ex_row.get(f"{reg}_ids")
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
            return True
    return False


def resolve_entry(name, category, allow_network=False):
    """Best area-level coordinate for one entry, or None.

    Aggregates go through match_grid_area first — it is built for exactly
    these names (substations, DSO codes, cluster labels) and already tries
    city -> region -> state -> operator internally. Everything else starts
    at the gazetteer, which is the most precise of these fallbacks.
    """
    tokens = significant_tokens(name)

    if category == "aggregate":
        hit = match_grid_area(name)
        if hit:
            source, _conf, lat, lon, label = hit
            return dict(geo_source=source, lat=lat, lon=lon, area_label=label)

    gaz = match_gazetteer(tokens)
    if gaz["status"] == "match":
        return dict(geo_source="gazetteer", lat=gaz["lat"], lon=gaz["lon"],
                    area_label=gaz["place"])

    rres = match_region(tokens)
    if rres:
        return dict(geo_source="region_centroid", lat=rres["lat"],
                    lon=rres["lon"], area_label=rres["region"])

    sres = match_state(tokens)
    if sres:
        return dict(geo_source="state_centroid", lat=sres["lat"],
                    lon=sres["lon"], area_label=sres["state"])

    # Network fallback last: one request per unresolved entry, rate-limited.
    if allow_network and tokens:
        hit = nominatim_forward(" ".join(tokens)) or nominatim_forward(tokens[0])
        time.sleep(NOM_SLEEP)
        if hit:
            lat, lon, _state = hit
            lat_min, lat_max, lon_min, lon_max = DE_BBOX
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return dict(geo_source="nominatim", lat=lat, lon=lon,
                            area_label=" ".join(tokens).title())
    return None


def main(entries_file: str = ENTRIES, use_nominatim: bool = True) -> None:
    entries = pd.read_csv(entries_file)
    exact = (pd.read_csv(EXACT).set_index("plant").to_dict("index")
             if os.path.exists(EXACT) else {})

    load_gazetteer()

    rows, n_skipped = [], 0
    todo = [e for e in entries.to_dict("records") if e.get("category") != "countertrade"]

    for i, e in enumerate(todo, 1):
        name = e["plant"]
        # An OPSD/PyPSA match already gives a real plant coordinate; spending a
        # geocode on it would only produce a worse one nothing would use.
        if _has_plant_coords(exact.get(name)):
            n_skipped += 1
            continue
        hit = resolve_entry(name, e.get("category"), allow_network=use_nominatim)
        if hit:
            rows.append({"plant": name, "category": e.get("category"),
                         "coord_precision": PRECISION.get(hit["geo_source"], "area"),
                         **hit})
        if i % 100 == 0:
            print(f"  ... {i}/{len(todo)} processed", flush=True)

    out = pd.DataFrame(rows, columns=["plant", "category", "geo_source",
                                       "coord_precision", "lat", "lon",
                                       "area_label"])
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"\n-> {OUT}: {len(out)} area-level coordinates "
          f"({n_skipped} skipped — already have plant coordinates)\n")
    if len(out):
        print("coord_precision:")
        print(out["coord_precision"].value_counts().to_string())
        print("\ngeo_source:")
        print(out["geo_source"].value_counts().to_string())
        print("\nby category:")
        print(out["category"].value_counts().to_string())


if __name__ == "__main__":
    main()
