"""
match_fuzzy.py — matching pipeline steps 3-4 (candidate pre-filter + fuzzy top-K).

For every matchable entry not resolved by exact match:
  Step 3 — pre-filter index candidates:
     • fuel: PRIMAERENERGIEART → allowed Fueltype set (Sonstiges = no filter)
     • capacity (individuals only): Capacity == 0 or Capacity >= max_dispatched × 0.7
     • entry_type: individual entries → individual candidates; cluster → cluster candidates
  Step 4 — fuzzy score (WRatio, heavy-normalised, MAX over each candidate's name
     variants) and keep the top-K per entry.

Fuzzy never decides — it just produces the top-K the LLM will choose from.

Output: data/fuzzy_candidates.csv — long format, one row per (entry, candidate).
"""

import os

import pandas as pd
from rapidfuzz import process, fuzz

from normalize import norm_light, norm_heavy

INDEX   = "data/candidate_index.csv"
ENTRIES = "data/redispatch_entries.csv"
EXACT   = "data/matches_exact.csv"
OUT     = "data/fuzzy_candidates.csv"

TOP_K          = 20
CAP_TOL        = 0.3    # keep candidates >= 70% of the entry's max dispatched power
MIN_CHOICE_LEN = 4      # drop name-variants shorter than this — abbrevs like "ch"/"wp"/"gen"
                        # false-inflate via WRatio's substring partial-ratio

# PRIMAERENERGIEART → allowed candidate Fueltype (the revised mapping; Sonstiges = None).
FUEL_FILTER = {
    "Konventionell": {"Natural Gas", "Hard Coal", "Lignite", "Oil", "Waste", "Other"},
    "Erneuerbar":    {"Solar", "Wind", "Hydro", "Biogas", "Solid Biomass", "Geothermal"},
    "Sonstiges":     None,
}


def heavy_or_light(s: str) -> str:
    """Heavy normalisation, falling back to light if stripping empties the string."""
    h = norm_heavy(s)
    return h if h else norm_light(s)


def main() -> None:
    idx = pd.read_csv(INDEX, low_memory=False)
    idx["id"] = idx["id"].astype(str)
    idx["Fueltype"] = idx["Fueltype"].fillna("")
    idx["Capacity"] = pd.to_numeric(idx["Capacity"], errors="coerce").fillna(0.0)
    # pre-normalise each candidate's name variants once
    idx["variants"] = idx["match_names"].fillna("").map(
        lambda s: [heavy_or_light(v) for v in str(s).split(" | ") if v.strip()])

    ent  = pd.read_csv(ENTRIES)
    done = set(pd.read_csv(EXACT)["betroffene_anlage"]) if os.path.exists(EXACT) else set()

    indiv = idx[idx["entry_type"] == "individual"]
    clust = idx[idx["entry_type"] == "cluster"]

    # id → attributes (for output)
    NAME = dict(zip(idx["id"], idx["Name"].astype(str)))
    FUEL = dict(zip(idx["id"], idx["Fueltype"]))
    CAP  = dict(zip(idx["id"], idx["Capacity"]))
    LAT  = dict(zip(idx["id"], idx["lat"]))
    LON  = dict(zip(idx["id"], idx["lon"]))

    # clusters are handled by match_clusters.py (location → set of individuals); fuzzy owns individuals
    open_ent = ent[(ent["entry_type"] == "individual") & ~ent["betroffene_anlage"].isin(done)]

    rows, no_cand = [], []
    for e in open_ent.itertuples():
        q = heavy_or_light(e.betroffene_anlage)
        allowed = FUEL_FILTER.get(e.primaerenergieart)

        if e.entry_type == "cluster":
            m = clust if allowed is None else clust[clust["Fueltype"].isin(allowed)]
        else:
            m = indiv if allowed is None else indiv[indiv["Fueltype"].isin(allowed)]
            if pd.notna(e.max_dispatched_mw):
                floor = e.max_dispatched_mw * (1 - CAP_TOL)
                m = m[(m["Capacity"] == 0) | (m["Capacity"] >= floor)]

        # flatten variants → parallel choice/id lists
        choices, cand_ids = [], []
        for cid, variants in zip(m["id"], m["variants"]):
            for v in variants:
                if len(v) < MIN_CHOICE_LEN:
                    continue
                choices.append(v)
                cand_ids.append(cid)
        if not choices:
            no_cand.append(e.betroffene_anlage)
            continue

        res = process.extract(q, choices, scorer=fuzz.WRatio, limit=200)
        best: dict[str, float] = {}
        for _c, score, i in res:
            cid = cand_ids[i]
            if score > best.get(cid, -1):
                best[cid] = score
        topk = sorted(best.items(), key=lambda x: -x[1])[:TOP_K]

        for rank, (cid, score) in enumerate(topk, 1):
            rows.append({
                "betroffene_anlage": e.betroffene_anlage,
                "entry_type":        e.entry_type,
                "primaerenergieart": e.primaerenergieart,
                "max_dispatched_mw": e.max_dispatched_mw,
                "rank":              rank,
                "score":             round(score, 1),
                "cand_id":           cid,
                "cand_name":         NAME.get(cid, ""),
                "cand_fueltype":     FUEL.get(cid, ""),
                "cand_capacity":     CAP.get(cid, ""),
                "cand_lat":          LAT.get(cid, ""),
                "cand_lon":          LON.get(cid, ""),
            })

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")

    n_entries = out["betroffene_anlage"].nunique()
    print(f"→ {OUT}: {len(out)} rows · {n_entries} entries got candidates")
    print(f"  entries with NO candidates after pre-filter: {len(no_cand)}")
    if no_cand:
        print("   ", no_cand[:10])
    print(f"  top-1 score distribution: "
          f"min {out.groupby('betroffene_anlage')['score'].max().min():.0f}, "
          f"median {out.groupby('betroffene_anlage')['score'].max().median():.0f}, "
          f"max {out.groupby('betroffene_anlage')['score'].max().max():.0f}")

    # self-check
    assert (out.groupby("betroffene_anlage")["rank"].max() <= TOP_K).all()
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
