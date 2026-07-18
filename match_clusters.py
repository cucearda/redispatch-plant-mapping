"""
match_clusters.py — cluster matching.

A redispatch `Cluster` entry names a LOCATION and maps to the SET of individual
plants there (not to a pre-built geo-cluster — those matched 0/59).

  Name channel   — the location token appears in individual plant names →
                   gather all matching renewable individuals → the member set. [high]
  Geocode channel (pending, step 7 shared Haiku geocoder) — location in no name
                   (Süderdonn, Klixbüll, …) → geocode → individuals within R km. [medium]

Output: data/matches_cluster.csv — one row per cluster entry,
matched_id = comma-joined member index ids, coords = member centroid.
"""

import math
import os
import re
from collections import defaultdict

import pandas as pd

from normalize import norm_light

INDEX   = "data/candidate_index.csv"
ENTRIES = "data/redispatch_entries.csv"
OUT     = "data/matches_cluster.csv"

# Location extraction: drop DSO codes, scheme words, cluster/number/turbine/direction tokens.
CL_STOP = {
    "cluster", "nwak", "bag", "croc", "sc", "ee", "pool",
    "shn", "ava", "ttg", "tng", "amp", "tbw", "avk", "bwp", "wp", "windpark", "wind",
    "nord", "sud", "süd", "west", "ost",
}
_TURB = re.compile(r"^t\d+$")
MIN_TOK = 4                                    # ignore tokens shorter than this
RENEWABLE = {"Wind", "Solar", "Biogas", "Hydro", "Solid Biomass"}
COHERENCE_KM   = 25.0                          # a DSO cluster is one location
COHERENCE_FRAC = 0.6                           # dominant ball must hold this share, else it's a name collision


def cluster_location(name: str) -> list[str]:
    s = norm_light(name).replace("-", " ")
    return [t for t in s.split()
            if t not in CL_STOP and not t.isdigit() and not _TURB.match(t) and len(t) >= MIN_TOK]


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def dominant_ball(pts: list[tuple]) -> list:
    """pts = [(id, lat, lon)] with coords. Return ids in the densest COHERENCE_KM ball —
    the co-located core of a name-gather (drops nationwide same-name collisions)."""
    pts = [p for p in pts if pd.notna(p[1]) and pd.notna(p[2])]
    if len(pts) <= 1:
        return [p[0] for p in pts]
    best = []
    for _, la0, lo0 in pts:
        ball = [i for i, la, lo in pts if haversine_km(la0, lo0, la, lo) <= COHERENCE_KM]
        if len(ball) > len(best):
            best = ball
    return best


def main() -> None:
    idx = pd.read_csv(INDEX, low_memory=False)
    idx["id"] = idx["id"].astype(str)
    idx["Fueltype"] = idx["Fueltype"].fillna("")
    indiv = idx[idx["entry_type"] == "individual"]

    # location token → set of individual ids (renewable only — cluster entries are renewable)
    tok2ids: dict[str, set] = defaultdict(set)
    for cid, mn, fuel in zip(indiv["id"], indiv["match_names"].fillna(""), indiv["Fueltype"]):
        if fuel not in RENEWABLE:
            continue
        toks = set()
        for v in str(mn).split(" | "):
            toks |= set(norm_light(v).split())
        for t in toks:
            if len(t) >= MIN_TOK:
                tok2ids[t].add(cid)

    NAME = dict(zip(indiv["id"], indiv["Name"].astype(str)))
    CAP  = dict(zip(indiv["id"], pd.to_numeric(indiv["Capacity"], errors="coerce").fillna(0.0)))
    LAT  = dict(zip(indiv["id"], pd.to_numeric(indiv["lat"], errors="coerce")))
    LON  = dict(zip(indiv["id"], pd.to_numeric(indiv["lon"], errors="coerce")))

    ent = pd.read_csv(ENTRIES)
    clusters = ent[ent["entry_type"] == "cluster"]

    rows, pending = [], []
    for e in clusters.itertuples():
        toks = cluster_location(e.betroffene_anlage)
        gathered = set().union(*(tok2ids.get(t, set()) for t in toks)) if toks else set()

        # coherence: keep the co-located core; a scattered gather is a name collision → geocode
        ball = dominant_ball([(m, LAT.get(m), LON.get(m)) for m in gathered])
        members = set(ball)
        coherent = len(gathered) <= 3 or len(members) >= COHERENCE_FRAC * len(gathered)
        if not members or not coherent:
            pending.append({"betroffene_anlage": e.betroffene_anlage, "location": " ".join(toks),
                            "gathered": len(gathered), "core": len(members)})
            continue

        lats = [LAT[m] for m in members if pd.notna(LAT.get(m))]
        lons = [LON[m] for m in members if pd.notna(LON.get(m))]
        rows.append({
            "betroffene_anlage": e.betroffene_anlage,
            "location":          " ".join(toks),
            "matched_id":        ",".join(sorted(members)),
            "id_source":         "index",
            "n_members":         len(members),
            "total_mw":          round(sum(CAP.get(m, 0) for m in members), 1),
            "method":            "cluster_name",
            "confidence":        "high",
            "lat":               round(sum(lats) / len(lats), 4) if lats else "",
            "lon":               round(sum(lons) / len(lons), 4) if lons else "",
        })

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")
    # pending list for the geocode channel
    pd.DataFrame(pending).to_csv("data/matches_cluster_pending.csv", index=False, encoding="utf-8")

    print(f"→ {OUT}: {len(out)}/{len(clusters)} clusters matched via NAME channel")
    print(f"  → data/matches_cluster_pending.csv: {len(pending)} need the geocode channel")
    if len(out):
        print(f"  member-count: min {out.n_members.min()}, median {out.n_members.median():.0f}, max {out.n_members.max()}")

    assert (out["n_members"] > 0).all() if len(out) else True
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
