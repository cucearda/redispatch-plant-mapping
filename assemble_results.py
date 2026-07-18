"""
assemble_results.py — merge every stage's output into one lookup table.

Combines the per-stage match files (exact / llm / cluster) with the full 398-entry
base and the candidate index, producing results/redispatch_plant_matches.csv:
one row per distinct BETROFFENE_ANLAGE, Layer-1 map + Layer-2 enrichment joined in.

Each entry is resolved by at most one stage (exact XOR llm XOR cluster), so there are
no conflicts; unmatched / unmatchable entries carry the classification and empty ids.
"""

import os

import pandas as pd

ENTRIES = "data/redispatch_entries.csv"
INDEX   = "data/candidate_index.csv"
EXACT   = "results/matches_exact.csv"
LLM     = "results/matches_llm.csv"
CLUSTER = "results/matches_cluster.csv"
OUT     = "results/redispatch_plant_matches.csv"

COLS = ["betroffene_anlage", "primaerenergieart", "entry_type", "name_technology",
        "matched_id", "id_source", "method", "confidence", "needs_review",
        "matched_name", "fueltype", "capacity_mw", "lat", "lon", "coord_source",
        "mastr_ids", "opsd_ids", "eic_ids", "reasoning"]

MATCHABLE = {"individual", "cluster"}


def uniq_join(series) -> str:
    s = set()
    for cell in series:
        if isinstance(cell, str):
            s.update(x for x in cell.split(",") if x)
    return ",".join(sorted(s))


def main() -> None:
    base = pd.read_csv(ENTRIES)
    idx = pd.read_csv(INDEX, low_memory=False)
    idx["id"] = idx["id"].astype(str)

    A = dict(zip(idx["id"], idx["Name"].astype(str)))
    FUEL = dict(zip(idx["id"], idx["Fueltype"].fillna("")))
    CAP  = dict(zip(idx["id"], pd.to_numeric(idx["Capacity"], errors="coerce")))
    LAT  = dict(zip(idx["id"], idx["lat"]))
    LON  = dict(zip(idx["id"], idx["lon"]))
    MAS  = dict(zip(idx["id"], idx["mastr_ids"].fillna("")))
    OPS  = dict(zip(idx["id"], idx["opsd_ids"].fillna("")))
    EIC  = dict(zip(idx["id"], idx["eic_ids"].fillna("")))

    # ── collect matches from each stage into one dict keyed by name ────────────
    match: dict[str, dict] = {}

    def put(name, matched_id, id_source, method, confidence, reasoning,
            matched_name="", fueltype="", capacity="", lat="", lon="",
            mastr="", opsd="", eic=""):
        match[name] = dict(matched_id=matched_id, id_source=id_source, method=method,
                           confidence=confidence, reasoning=reasoning,
                           matched_name=matched_name, fueltype=fueltype, capacity_mw=capacity,
                           lat=lat, lon=lon, mastr_ids=mastr, opsd_ids=opsd, eic_ids=eic)

    # exact (single index id)
    if os.path.exists(EXACT):
        for r in pd.read_csv(EXACT).fillna("").itertuples():
            mid = str(r.matched_id)
            put(r.betroffene_anlage, mid, r.id_source, "exact", "high", "exact name match",
                matched_name=A.get(mid, r.matched_name), fueltype=FUEL.get(mid, ""),
                capacity=CAP.get(mid, ""), lat=LAT.get(mid, ""), lon=LON.get(mid, ""),
                mastr=MAS.get(mid, ""), opsd=OPS.get(mid, ""), eic=EIC.get(mid, ""))

    # llm (single index id; empty matched_id = declined → stays unresolved)
    if os.path.exists(LLM):
        for r in pd.read_csv(LLM).fillna("").itertuples():
            mid = str(r.matched_id).strip()
            if mid:
                put(r.betroffene_anlage, mid, "index", "llm", r.confidence, r.reasoning,
                    matched_name=A.get(mid, ""), fueltype=FUEL.get(mid, ""),
                    capacity=CAP.get(mid, ""), lat=LAT.get(mid, ""), lon=LON.get(mid, ""),
                    mastr=MAS.get(mid, ""), opsd=OPS.get(mid, ""), eic=EIC.get(mid, ""))
            else:
                put(r.betroffene_anlage, "", "", "llm", "none", r.reasoning)  # declined → residual

    # cluster (comma-joined member ids; attrs aggregated over members)
    if os.path.exists(CLUSTER):
        for r in pd.read_csv(CLUSTER).fillna("").itertuples():
            members = str(r.matched_id).split(",")
            put(r.betroffene_anlage, r.matched_id, "index", "cluster_name", r.confidence,
                f"cluster: {r.n_members} plants @ {r.location}",
                matched_name=f"Cluster @ {r.location}",
                fueltype=uniq_join([FUEL.get(m, "") for m in members]),
                capacity=r.total_mw, lat=r.lat, lon=r.lon,
                mastr=uniq_join(MAS.get(m, "") for m in members),
                opsd=uniq_join(OPS.get(m, "") for m in members),
                eic=uniq_join(EIC.get(m, "") for m in members))

    # ── merge onto the 398-row base ────────────────────────────────────────────
    rows = []
    for e in base.itertuples():
        m = match.get(e.betroffene_anlage, {})
        matched = bool(m.get("matched_id"))
        conf = m.get("confidence", "")
        matchable = e.entry_type in MATCHABLE
        coord_source = "index" if (matched and m.get("lat") not in ("", None)) else "none"
        # needs review: matchable-but-unresolved, or a low/none-confidence match
        needs_review = "yes" if matchable and (not matched or conf in ("low", "none")) else "no"
        rows.append({
            "betroffene_anlage": e.betroffene_anlage,
            "primaerenergieart": e.primaerenergieart,
            "entry_type":        e.entry_type,
            "name_technology":   e.name_technology if isinstance(e.name_technology, str) else "",
            "matched_id":        m.get("matched_id", ""),
            "id_source":         m.get("id_source", ""),
            "method":            m.get("method", ""),
            "confidence":        conf,
            "needs_review":      needs_review,
            "matched_name":      m.get("matched_name", ""),
            "fueltype":          m.get("fueltype", ""),
            "capacity_mw":       m.get("capacity_mw", ""),
            "lat":               m.get("lat", ""),
            "lon":               m.get("lon", ""),
            "coord_source":      coord_source,
            "mastr_ids":         m.get("mastr_ids", ""),
            "opsd_ids":          m.get("opsd_ids", ""),
            "eic_ids":           m.get("eic_ids", ""),
            "reasoning":         m.get("reasoning", ""),
        })

    out = pd.DataFrame(rows)[COLS]
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"→ {OUT}: {len(out)} rows")
    matched = out["matched_id"].astype(str).str.len().gt(0)
    print(f"  matched: {matched.sum()}  ·  unresolved/unmatchable: {(~matched).sum()}")
    print("\n  by method:")
    print(out[matched]["method"].value_counts().to_string())
    print("\n  by confidence (matched):")
    print(out[matched]["confidence"].value_counts().to_string())
    print(f"\n  needs_review: {(out['needs_review']=='yes').sum()}")

    assert len(out) == len(base)
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
