"""
match_exact.py — matching pipeline step 2 (exact match, early-exit).

For each matchable entry (individual + cluster), light-normalise the name and look
for an EXACT normalised-name hit against two sources:
  • the candidate index's match_names (→ index id)
  • bnetza_lookup.Anzeigename (→ MaStR SEE id; resolved into the index where possible)

A hit short-circuits ONLY if it resolves to exactly ONE distinct plant (method=exact).
Ambiguous (>1 distinct plant) or no hit → left for the fuzzy → LLM stages.

Output: data/matches_exact.csv — the entries the exact stage resolves.
"""

import os
from collections import defaultdict

import pandas as pd

from normalize import norm_light

INDEX   = "data/candidate_index.csv"
BNETZA  = "data/bnetza_lookup.csv"
ENTRIES = "data/redispatch_entries.csv"
OUT     = "data/matches_exact.csv"


def main() -> None:
    idx = pd.read_csv(INDEX, low_memory=False)
    bn  = pd.read_csv(BNETZA)
    ent = pd.read_csv(ENTRIES)

    idx["id"] = idx["id"].astype(str)

    # normalised name variant → set of index ids
    name2ids: dict[str, set] = defaultdict(set)
    for _id, mn in zip(idx["id"], idx["match_names"].fillna("")):
        for variant in str(mn).split(" | "):
            k = norm_light(variant)
            if k:
                name2ids[k].add(_id)

    # MaStR SEE id → index id (first occurrence)
    mastr2idx: dict[str, str] = {}
    for _id, m in zip(idx["id"], idx["mastr_ids"].fillna("")):
        for mid in str(m).split(","):
            mid = mid.strip()
            if mid and mid not in mastr2idx:
                mastr2idx[mid] = _id

    # normalised BNetzA Anzeigename → set of MaStR ids
    bname2mastr: dict[str, set] = defaultdict(set)
    for mastr, an in zip(bn["mastr_id"].astype(str), bn["Anzeigename"].fillna("")):
        k = norm_light(an)
        if k:
            bname2mastr[k].add(mastr)

    # id → display name (for eyeballing)
    idx_name = dict(zip(idx["id"], idx["Name"].astype(str)))
    bn_name  = dict(zip(bn["mastr_id"].astype(str), bn["Anzeigename"].astype(str)))

    matchable = ent[ent["entry_type"].isin(["individual", "cluster"])]
    rows, ambiguous = [], 0
    for e in matchable.itertuples():
        q = norm_light(e.betroffene_anlage)
        if not q:
            continue
        idx_hits = set(name2ids.get(q, ()))       # index ids
        bn_only  = set()                          # bnetza-only MaStR ids
        for mid in bname2mastr.get(q, ()):
            if mid in mastr2idx:
                idx_hits.add(mastr2idx[mid])      # same plant already in index
            else:
                bn_only.add(mid)

        candidates = [("index", i) for i in idx_hits] + [("bnetza", m) for m in bn_only]
        if len(candidates) == 1:
            src, mid = candidates[0]
            rows.append({
                "betroffene_anlage": e.betroffene_anlage,
                "entry_type":        e.entry_type,
                "matched_id":        mid,
                "id_source":         src,
                "method":            "exact",
                "matched_name":      idx_name.get(mid, "") if src == "index" else bn_name.get(mid, ""),
            })
        elif len(candidates) > 1:
            ambiguous += 1                         # falls through to fuzzy → LLM

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")

    n_match = len(matchable)
    print(f"→ {OUT}: {len(out)} exact matches of {n_match} matchable "
          f"({100*len(out)/n_match:.0f}%)")
    print(f"  ambiguous (>1 plant, deferred to fuzzy): {ambiguous}")
    print(f"  by id_source: {out['id_source'].value_counts().to_dict() if len(out) else {}}")
    print(f"  by entry_type: {out['entry_type'].value_counts().to_dict() if len(out) else {}}")

    # self-check
    if len(out):
        assert (out["method"] == "exact").all()
        assert out["matched_id"].astype(str).str.len().gt(0).all(), "empty matched_id"
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
