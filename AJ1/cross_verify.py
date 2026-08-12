"""
cross_verify.py — AJ1 pipeline, step 3: cross-verify OPSD and PyPSA matches
against each other using PyPSA's own `projectID` cross-reference (its
`OPSD` sub-key already holds the BNetzA/OPSD BNA code(s) for that row —
confirmed parseable with ast.literal_eval, 0 failures across all 141,928
PyPSA rows), and fill in whichever side is still missing. BNetzA/MaStR
cross-referencing is deferred to a later step.

Two directions:
  - OPSD -> PyPSA: for every entry with an opsd_id, look up which PyPSA
    row(s) reference that BNA code via projectID.OPSD. If the entry also
    already has an independently-matched psa_id, verify they agree; if not,
    fill psa_id from the reference. Multi-candidate cases (either an OPSD id
    referenced by several PyPSA rows, or a conflict between the existing
    psa_id and the xref-implied one) are resolved by a nearby+same-class
    check, reusing match_exact.py's own duplicate-resolution logic — pass
    -> treat as agreeing/fill; fail -> flag, never guess.
  - PyPSA -> OPSD: for every entry with a psa_id but NO opsd_id (entries
    that already have an opsd_id went through the direction above and are
    not re-checked here), read that PSA row's own projectID.OPSD code(s)
    directly and look them up in OPSD's own `id` column (which already IS
    the BNA code space) — same nearby+same-class handling if there's more
    than one code.

Never overwrites an existing opsd_id/psa_id, even on conflict — only flags
it (xref_status="conflict") for manual review, exactly as match_exact.py
never auto-resolves a BNetzA duplicate rather than guessing.

Input:  AJ1/temp_AJ1/matches_exact.csv (read-only)
Output: AJ1/temp_AJ1/matches_exact_filled.csv — every original column
        preserved, opsd_id/opsd_name/psa_id/psa_name filled where this step
        fills them, plus a new `xref_status` column: agree / conflict /
        filled_psa / filled_opsd / ambiguous_no_fill / no_xref / not_applicable.
"""

import ast
import os
from collections import defaultdict

import pandas as pd

from paths import INPUT_DIR, TEMP_DIR
from match_exact import opsd_class, psa_class, _agree, _tight_cluster_pick

MATCHES = os.path.join(TEMP_DIR, "matches_exact.csv")
OUT     = os.path.join(TEMP_DIR, "matches_exact_filled.csv")
OPSD    = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_DE.csv")
PSA     = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")


def _parse_opsd_codes(project_id):
    """PyPSA's projectID is a stringified dict; pull out its 'OPSD' sub-key
    (a set of BNA codes), tolerating any row that doesn't parse cleanly."""
    if pd.isna(project_id):
        return set()
    try:
        d = ast.literal_eval(project_id)
    except (ValueError, SyntaxError):
        return set()
    return set(d.get("OPSD", set()))


def _nearby_and_same_class(rows):
    """rows: a small DataFrame subset with lat/lon/class columns (candidates
    for the same entry). True if every row agrees on class AND all sit
    within a tight geographic cluster (same logic match_exact.py uses to
    auto-resolve a same-key duplicate pool)."""
    if len(rows) == 1:
        return True
    positions = list(range(len(rows)))
    if not _agree(positions, rows["class"].values):
        return False
    return _tight_cluster_pick(positions, rows["lat"].values, rows["lon"].values) is not None


def _centroid_closest(rows):
    lat_c, lon_c = rows["lat"].mean(), rows["lon"].mean()
    idx = ((rows["lat"] - lat_c) ** 2 + (rows["lon"] - lon_c) ** 2).idxmin()
    return rows.loc[idx]


# ---------------------------------------------------------------------------
def main(matches_file: str = MATCHES) -> None:
    m = pd.read_csv(matches_file)

    psa = pd.read_csv(PSA, low_memory=False)
    psa["class"] = [psa_class(f, t) for f, t in zip(psa["Fueltype"], psa["Technology"])]
    psa_by_id = psa.set_index("id", drop=False)

    opsd = pd.read_csv(OPSD, encoding="utf-8-sig")
    opsd["class"] = [opsd_class(s, t) for s, t in zip(opsd["energy_source"], opsd["technology"])]
    opsd_by_id = opsd.set_index("id", drop=False)

    # bna_to_psa_ids: BNA code -> set of PSA ids referencing it (reverse index).
    # psa_id_to_opsd_codes: PSA id -> set of BNA codes it references (forward,
    # read straight off that row's own projectID).
    bna_to_psa_ids = defaultdict(set)
    psa_id_to_opsd_codes = {}
    for _, row in psa.iterrows():
        codes = _parse_opsd_codes(row["projectID"])
        if codes:
            psa_id_to_opsd_codes[row["id"]] = codes
            for code in codes:
                bna_to_psa_ids[code].add(row["id"])

    # NOT "n/a" -- pandas' default read_csv na_values list treats that
    # literal string as NaN, silently corrupting this column on any later
    # read of the output file.
    m["xref_status"] = "not_applicable"
    conflicts, ambiguous = [], []
    filled_psa_n = filled_opsd_n = 0

    # ---- Direction 1: OPSD -> PyPSA ------------------------------------------
    has_opsd = m["opsd_id"].notna()
    for i in m.index[has_opsd]:
        opsd_id = m.at[i, "opsd_id"]
        candidates = bna_to_psa_ids.get(opsd_id, set())
        if not candidates:
            m.at[i, "xref_status"] = "no_xref"
            continue

        existing_psa = m.at[i, "psa_id"]
        if pd.notna(existing_psa):
            existing_psa = int(existing_psa)
            if existing_psa in candidates:
                m.at[i, "xref_status"] = "agree"
                continue
            ids_to_check = [existing_psa] + [int(c) for c in candidates]
            rows = psa_by_id.loc[psa_by_id.index.isin(ids_to_check)]
            if _nearby_and_same_class(rows):
                m.at[i, "xref_status"] = "agree"
            else:
                m.at[i, "xref_status"] = "conflict"
                conflicts.append((m.at[i, "plant"], opsd_id, m.at[i, "opsd_name"],
                                   existing_psa, m.at[i, "psa_name"], candidates))
            continue

        # psa_id missing -- fill from the cross-reference
        if len(candidates) == 1:
            row = psa_by_id.loc[int(next(iter(candidates)))]
            m.at[i, "psa_id"] = row["id"]
            m.at[i, "psa_name"] = row["Name"]
            m.at[i, "xref_status"] = "filled_psa"
            filled_psa_n += 1
        else:
            rows = psa_by_id.loc[psa_by_id.index.isin(int(c) for c in candidates)]
            if _nearby_and_same_class(rows):
                row = _centroid_closest(rows)
                m.at[i, "psa_id"] = row["id"]
                m.at[i, "psa_name"] = row["Name"]
                m.at[i, "xref_status"] = "filled_psa"
                filled_psa_n += 1
            else:
                m.at[i, "xref_status"] = "ambiguous_no_fill"
                ambiguous.append((m.at[i, "plant"], "psa", candidates))

    # ---- Direction 2: PyPSA -> OPSD (only entries still missing opsd_id) -----
    needs_opsd = m["opsd_id"].isna() & m["psa_id"].notna()
    for i in m.index[needs_opsd]:
        psa_id = int(m.at[i, "psa_id"])
        codes = psa_id_to_opsd_codes.get(psa_id, set())
        found_codes = [c for c in codes if c in opsd_by_id.index]
        if not found_codes:
            m.at[i, "xref_status"] = "no_xref"
            continue

        if len(found_codes) == 1:
            row = opsd_by_id.loc[found_codes[0]]
            m.at[i, "opsd_id"] = row["id"]
            m.at[i, "opsd_name"] = row["name_bnetza"]
            m.at[i, "xref_status"] = "filled_opsd"
            filled_opsd_n += 1
        else:
            rows = opsd_by_id.loc[opsd_by_id.index.isin(found_codes)]
            if _nearby_and_same_class(rows):
                row = _centroid_closest(rows)
                m.at[i, "opsd_id"] = row["id"]
                m.at[i, "opsd_name"] = row["name_bnetza"]
                m.at[i, "xref_status"] = "filled_opsd"
                filled_opsd_n += 1
            else:
                m.at[i, "xref_status"] = "ambiguous_no_fill"
                ambiguous.append((m.at[i, "plant"], "opsd", set(found_codes)))

    m.to_csv(OUT, index=False, encoding="utf-8")

    # ---- report ---------------------------------------------------------------
    print(f"-> {OUT}: {len(m)} rows\n")
    print(m["xref_status"].value_counts().to_string())
    print(f"\nfilled psa_id:  {filled_psa_n}")
    print(f"filled opsd_id: {filled_opsd_n}")

    if conflicts:
        print("\nconflicts (flagged, not resolved):")
        for plant, oid, oname, pid, pname, xref in conflicts:
            print(f"  {plant!r}: opsd={oid}/{oname!r}  psa={pid}/{pname!r}  "
                  f"xref-implied psa={xref}")

    if ambiguous:
        print("\nambiguous (candidates found, but not nearby+same-class -- not filled):")
        for plant, side, cands in ambiguous:
            print(f"  {plant!r} ({side}): {cands}")


if __name__ == "__main__":
    main()
