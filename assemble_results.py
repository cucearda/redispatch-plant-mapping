"""
assemble_results.py — merge every stage's output into one lookup table.

Combines the per-stage match files (exact / llm / cluster / wikipedia) with the full
398-entry base and the candidate index → results/redispatch_plant_matches.csv:
one row per distinct BETROFFENE_ANLAGE, Layer-1 map + Layer-2 enrichment joined in.

Precedence: exact → llm → cluster, then wikipedia OVERRIDES the llm null/low rows it
re-resolved. Unmatched / unmatchable entries carry the classification and empty ids.
(The final coordinate backfill for coord-less rows is geocode_backfill.py.)
"""

import os

import pandas as pd

from redispatch_prep import segments

ENTRIES = "data/redispatch_entries.csv"
INDEX   = "data/candidate_index.csv"
EXACT   = "results/matches_exact.csv"
LLM     = "results/matches_llm.csv"
CLUSTER = "results/matches_cluster.csv"
WIKI    = "results/matches_wikipedia.csv"
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

    NAME = dict(zip(idx["id"], idx["Name"].astype(str)))
    FUEL = dict(zip(idx["id"], idx["Fueltype"].fillna("")))
    CAP  = dict(zip(idx["id"], pd.to_numeric(idx["Capacity"], errors="coerce")))
    LAT  = dict(zip(idx["id"], idx["lat"]))
    LON  = dict(zip(idx["id"], idx["lon"]))
    MAS  = dict(zip(idx["id"], idx["mastr_ids"].fillna("")))
    OPS  = dict(zip(idx["id"], idx["opsd_ids"].fillna("")))
    EIC  = dict(zip(idx["id"], idx["eic_ids"].fillna("")))

    match: dict[str, dict] = {}

    def put(name, matched_id, id_source, method, confidence, reasoning,
            matched_name="", fueltype="", capacity="", lat="", lon="", coord_source="",
            mastr="", opsd="", eic="", needs_review=""):
        match[name] = dict(matched_id=matched_id, id_source=id_source, method=method,
                           confidence=confidence, reasoning=reasoning, matched_name=matched_name,
                           fueltype=fueltype, capacity_mw=capacity, lat=lat, lon=lon,
                           coord_source=coord_source, mastr_ids=mastr, opsd_ids=opsd,
                           eic_ids=eic, needs_review=needs_review)

    def put_single(name, mid, method, conf, reason, src="index", **kw):
        put(name, mid, src, method, conf, reason,
            matched_name=NAME.get(mid, ""), fueltype=FUEL.get(mid, ""), capacity=CAP.get(mid, ""),
            lat=LAT.get(mid, ""), lon=LON.get(mid, ""), coord_source="index",
            mastr=MAS.get(mid, ""), opsd=OPS.get(mid, ""), eic=EIC.get(mid, ""), **kw)

    # exact
    if os.path.exists(EXACT):
        for r in pd.read_csv(EXACT).fillna("").itertuples():
            put_single(r.betroffene_anlage, str(r.matched_id), "exact", "high", "exact name match")

    # llm (empty matched_id = declined → placeholder, may be overridden by wikipedia)
    if os.path.exists(LLM):
        for r in pd.read_csv(LLM).fillna("").itertuples():
            mid = str(r.matched_id).strip()
            if mid:
                put_single(r.betroffene_anlage, mid, "llm", r.confidence, r.reasoning)
            else:
                put(r.betroffene_anlage, "", "", "llm", "none", r.reasoning)

    # cluster (name + geocode; comma-joined member ids)
    if os.path.exists(CLUSTER):
        for r in pd.read_csv(CLUSTER).fillna("").itertuples():
            members = str(r.matched_id).split(",")
            put(r.betroffene_anlage, r.matched_id, "index", r.method, r.confidence,
                f"cluster: {r.n_members} plants @ {r.location}",
                matched_name=f"Cluster @ {r.location}",
                fueltype=uniq_join(FUEL.get(m, "") for m in members),
                capacity=r.total_mw, lat=r.lat, lon=r.lon, coord_source="index",
                mastr=uniq_join(MAS.get(m, "") for m in members),
                opsd=uniq_join(OPS.get(m, "") for m in members),
                eic=uniq_join(EIC.get(m, "") for m in members))

    # wikipedia — overrides the llm null/low rows it re-resolved
    if os.path.exists(WIKI):
        for r in pd.read_csv(WIKI).fillna("").itertuples():
            mid = str(r.matched_id).strip()
            if mid:
                put_single(r.betroffene_anlage, mid, "wikipedia", r.confidence, r.reasoning,
                           needs_review=r.needs_review)
            else:                                     # coordinate-only (no plant nearby)
                put(r.betroffene_anlage, "", "", "wikipedia", "none", r.reasoning,
                    lat=r.lat, lon=r.lon, coord_source=r.coord_source, needs_review=r.needs_review)

    # multi_plant — "Boxberg, Goldisthal, Jänschwalde" bundles plants that each have an
    # entry of their own, so compose the members' resolved matches instead of matching the
    # string. Runs last: the segments' own rows must be final (incl. wikipedia overrides).
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for e in base[base["entry_type"] == "multi_plant"].itertuples():
        parts = segments(e.betroffene_anlage)
        mem = [match[p] for p in parts if match.get(p, {}).get("matched_id")]
        if not mem:
            continue
        pts  = [(num(m["lat"]), num(m["lon"])) for m in mem]
        pts  = [p for p in pts if p[0] is not None and p[1] is not None]
        caps = [c for c in (num(m["capacity_mw"]) for m in mem) if c is not None]
        whole = len(mem) == len(parts)
        put(e.betroffene_anlage, ",".join(str(m["matched_id"]) for m in mem), "index",
            "multi_plant", "high" if whole else "medium",
            f"multi-plant entry: {len(mem)}/{len(parts)} listed plants matched",
            matched_name=" + ".join(str(m["matched_name"]) for m in mem),
            fueltype=uniq_join(m["fueltype"] for m in mem),
            capacity=sum(caps) if caps else "",
            lat=round(sum(p[0] for p in pts) / len(pts), 4) if pts else "",
            lon=round(sum(p[1] for p in pts) / len(pts), 4) if pts else "",
            coord_source="index" if pts else "",
            mastr=uniq_join(m["mastr_ids"] for m in mem),
            opsd=uniq_join(m["opsd_ids"] for m in mem),
            eic=uniq_join(m["eic_ids"] for m in mem),
            needs_review="no" if whole else "yes")

    # ── merge onto the 398-row base ────────────────────────────────────────────
    rows = []
    for e in base.itertuples():
        m = match.get(e.betroffene_anlage, {})
        matched = bool(m.get("matched_id"))
        conf = m.get("confidence", "")
        matchable = e.entry_type in MATCHABLE
        coord_source = m.get("coord_source") or ("index" if (matched and m.get("lat") not in ("", None)) else "none")
        needs_review = m.get("needs_review") or (
            "yes" if matchable and (not matched or conf in ("low", "none")) else "no")
        rows.append({
            "betroffene_anlage": e.betroffene_anlage, "primaerenergieart": e.primaerenergieart,
            "entry_type": e.entry_type,
            "name_technology": e.name_technology if isinstance(e.name_technology, str) else "",
            "matched_id": m.get("matched_id", ""), "id_source": m.get("id_source", ""),
            "method": m.get("method", ""), "confidence": conf, "needs_review": needs_review,
            "matched_name": m.get("matched_name", ""), "fueltype": m.get("fueltype", ""),
            "capacity_mw": m.get("capacity_mw", ""), "lat": m.get("lat", ""), "lon": m.get("lon", ""),
            "coord_source": coord_source, "mastr_ids": m.get("mastr_ids", ""),
            "opsd_ids": m.get("opsd_ids", ""), "eic_ids": m.get("eic_ids", ""),
            "reasoning": m.get("reasoning", ""),
        })

    out = pd.DataFrame(rows)[COLS]
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"→ {OUT}: {len(out)} rows")
    matched = out["matched_id"].astype(str).str.len().gt(0)
    print(f"  matched: {matched.sum()}  ·  unresolved/unmatchable: {(~matched).sum()}")
    print("\n  by method (matched):")
    print(out[matched]["method"].value_counts().to_string())
    print("\n  by confidence (matched):")
    print(out[matched]["confidence"].value_counts().to_string())
    print(f"\n  has coordinate: {(out['lat'].astype(str).str.len() > 0).sum()} / {len(out)}")
    print(f"  needs_review: {(out['needs_review'] == 'yes').sum()}")

    assert len(out) == len(base)
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
