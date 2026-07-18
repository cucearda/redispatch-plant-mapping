"""
match_wikipedia.py — residual channel for individuals the LLM couldn't resolve.

For each LLM null/low entry, query the Wikipedia API (search → summary → coordinates),
then back-match that coordinate to the nearest index individual within 5 km (fuel-filtered):
  • LLM had a low-confidence guess and Wikipedia agrees (≤5 km) → upgrade to high
  • LLM had a guess and Wikipedia disagrees → flag for manual review
  • LLM declined, Wikipedia → a nearby plant → low
  • Wikipedia locates it but no plant within 5 km → keep the coordinate only (coord_source=wikipedia)

Output: results/matches_wikipedia.csv (overrides the LLM row for these entries).
"""

import json
import math
import os
import time
import urllib.parse
import urllib.request

import pandas as pd

LLM     = "results/matches_llm.csv"
INDEX   = "data/candidate_index.csv"
ENTRIES = "data/redispatch_entries.csv"
OUT     = "results/matches_wikipedia.csv"

UA = {"User-Agent": "redispatch-plant-matching/1.0 (thesis research; github.com/cucearda)"}
COORD_MATCH_KM = 5.0

FUEL_FILTER = {
    "Konventionell": {"Natural Gas", "Hard Coal", "Lignite", "Oil", "Waste", "Other"},
    "Erneuerbar":    {"Solar", "Wind", "Hydro", "Biogas", "Solid Biomass", "Geothermal"},
    "Sonstiges":     None,
}


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def wiki_coords(name: str):
    """Wikipedia (de) search → summary → coordinates, or None."""
    for q in (name, f"{name} Kraftwerk"):
        try:
            res = _get("https://de.wikipedia.org/w/rest.php/v1/search/page?"
                       f"q={urllib.parse.quote(q)}&limit=1")
            if not res.get("pages"):
                continue
            key = res["pages"][0]["key"]
            s = _get("https://de.wikipedia.org/api/rest_v1/page/summary/"
                     + urllib.parse.quote(key))
            c = s.get("coordinates")
            if c:
                return (c["lat"], c["lon"], res["pages"][0]["title"])
        except Exception:
            continue
    return None


def main() -> None:
    llm = pd.read_csv(LLM).fillna("")
    llm["matched_id"] = llm["matched_id"].astype(str).replace("nan", "")
    ent = pd.read_csv(ENTRIES).set_index("betroffene_anlage")
    idx = pd.read_csv(INDEX, low_memory=False)
    idx["id"] = idx["id"].astype(str)
    indiv = idx[idx["entry_type"] == "individual"].copy()
    indiv["lat"] = pd.to_numeric(indiv["lat"], errors="coerce")
    indiv["lon"] = pd.to_numeric(indiv["lon"], errors="coerce")

    residual = llm[(llm["matched_id"] == "") | (llm["confidence"] == "low")]
    print(f"residual entries (LLM null/low): {len(residual)}")

    rows = []
    for r in residual.itertuples():
        name = r.betroffene_anlage
        energy = ent.loc[name, "primaerenergieart"] if name in ent.index else ""
        wc = wiki_coords(name)
        time.sleep(0.1)                              # be polite to the API
        if wc is None:
            continue                                 # no Wikipedia hit → stays LLM-null
        wlat, wlon, title = wc

        allowed = FUEL_FILTER.get(energy)
        pool = indiv if allowed is None else indiv[indiv["Fueltype"].isin(allowed)]
        pool = pool.dropna(subset=["lat", "lon"])
        d = pool.apply(lambda p: haversine_km(wlat, wlon, p["lat"], p["lon"]), axis=1)
        nearest_id, nearest_km = ("", 1e9)
        if len(d):
            j = d.idxmin()
            nearest_id, nearest_km = pool.loc[j, "id"], d.loc[j]

        llm_had = bool(r.matched_id)
        llm_coord = (float(r.lat), float(r.lon)) if str(r.lat) not in ("", "nan") else None

        if llm_had and llm_coord and nearest_km <= COORD_MATCH_KM:
            # LLM guess + Wikipedia agree → high (keep the LLM's plant + its coords)
            rows.append(_row(name, r.matched_id, "index", "high",
                             f"LLM and Wikipedia ({title}) agree within {nearest_km:.0f} km",
                             r.lat, r.lon))
        elif llm_had and llm_coord and nearest_km > COORD_MATCH_KM:
            # disagreement → manual
            rows.append(_row(name, r.matched_id, "index", "low",
                             f"LLM guess and Wikipedia ({title}) coords disagree — needs manual check",
                             r.lat, r.lon, review=True))
        elif nearest_km <= COORD_MATCH_KM:
            # LLM declined, Wikipedia → a nearby plant → low
            p = indiv[indiv["id"] == nearest_id].iloc[0]
            rows.append(_row(name, nearest_id, "index", "low",
                             f"Wikipedia ({title}) coord → nearest plant within {nearest_km:.0f} km",
                             p["lat"], p["lon"]))
        else:
            # located but no registry plant nearby → coordinate only
            rows.append(_row(name, "", "", "none",
                             f"Wikipedia ({title}) located but no plant within 5 km",
                             wlat, wlon, coord_source="wikipedia", review=True))

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")
    print(f"→ {OUT}: {len(out)} residual rows resolved via Wikipedia")
    if len(out):
        print(out["confidence"].value_counts().to_string())


def _row(name, mid, src, conf, reason, lat, lon, coord_source="index", review=False):
    return {"betroffene_anlage": name, "matched_id": mid, "id_source": src,
            "method": "wikipedia", "confidence": conf, "needs_review": "yes" if review else "",
            "reasoning": reason, "lat": lat if lat is not None else "",
            "lon": lon if lon is not None else "", "coord_source": coord_source}


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
