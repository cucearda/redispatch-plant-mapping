"""
assemble_results.py — AJ1 pipeline, final step: merge every stage into one
tracked output, results/AJ1/redispatch_plant_matches.csv.

Deliberately does NOT consolidate each redispatch entry down to one plant.
The full candidate list every registry produced is carried through
(`opsd_ids`/`psa_ids`/`bnetza_ids`, pipe-joined, exactly as match_exact.py
wrote them), and the single-plant answer is presented alongside it as an
explicitly-labelled *best guess* — with the basis it rests on, so a consumer
can filter by how much it should be trusted:

  guess_basis = unanimous  the registries found one plant and agreed on its
                           fuel; no judgement was applied
                llm        several candidates or a fuel conflict; match_llm.py
                           picked one of them (see llm_confidence)
                wikipedia  no registry matched at all; the wiki bot found a
                           plant article carrying a real fuel field
                none       nothing resolved it

`n_candidates` is the size of the pool the guess was drawn from, and
`fuel_status` records whether the registries agreed on fuel by themselves
(`agreed`), disagreed and were resolved by the LLM (`llm_resolved`), or
disagreed unresolved (`conflict`).

Coordinates are merged in precedence order — a real plant location always
beats an area centroid — and `coord_precision` says which you got:

  plant   OPSD / PyPSA / Wikipedia   an actual plant location
  town    gazetteer / nominatim      the settlement the entry is named after
  area    grid_area / region         DSO or region centroid
  state   state_centroid             Bundesland centroid

`coord_source` names the specific resolver. **Only `plant` rows may be drawn
as a plant pin.** A `state` centroid rendered like a matched plant is the
same false-positive failure match_exact.py refuses to make about identity,
relocated into geography — filter on `coord_precision` before mapping.

Inputs (all read-only; every optional file is skipped gracefully if absent,
and the wiki file may be partial — a capped smoke-test run only covers the
first N entries):
    temp_AJ1/redispatch_entries.csv    every distinct name, incl. aggregates
    temp_AJ1/matches_exact.csv         registry candidates
    temp_AJ1/matches_llm.csv           best guess among those candidates
    temp_AJ1/matches_wikipedia.csv     independent wiki/DDG lookup
    temp_AJ1/matches_geo.csv           area-level fallback coordinates
"""

import os

import pandas as pd

from paths import INPUT_DIR, TEMP_DIR, RESULTS_DIR

ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")
EXACT   = os.path.join(TEMP_DIR, "matches_exact.csv")
LLM     = os.path.join(TEMP_DIR, "matches_llm.csv")
WIKI    = os.path.join(TEMP_DIR, "matches_wikipedia.csv")
GEO     = os.path.join(TEMP_DIR, "matches_geo.csv")
OPSD    = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_DE.csv")
PSA     = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")
OUT     = os.path.join(RESULTS_DIR, "redispatch_plant_matches.csv")

REGISTRIES = ("opsd", "psa", "bnetza")


def _split(cell):
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [p.strip() for p in str(cell).split(" | ") if p.strip()]


def _blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""


def _load_coords():
    """{registry id (as str) -> (lat, lon)} for the two registries that have
    coordinates. BNetzA's cleaned file carries none, so a BNetzA-only winner
    has no coordinate of its own and falls back to Wikipedia's."""
    coords = {}
    opsd = pd.read_csv(OPSD, encoding="utf-8-sig")
    for i, lat, lon in zip(opsd["id"], opsd["lat"], opsd["lon"]):
        coords[("opsd", str(i))] = (lat, lon)
    psa = pd.read_csv(PSA, low_memory=False)
    for i, lat, lon in zip(psa["id"], psa["lat"], psa["lon"]):
        coords[("psa", str(i))] = (lat, lon)
    return coords


def _sole_candidate(row):
    """The one plant, if every registry hit names the same one and none of
    them reports a fuel conflict. Returns (registry, id, name, fuel) or None.

    A registry may legitimately return SEVERAL ids here — match_exact.py keeps
    a whole tight geographic cluster (one wind farm listed turbine-by-turbine)
    rather than narrowing to a row. Those still count as unanimous as long as
    the names agree; the first id stands in for the cluster. This has to match
    match_llm.needs_ranking(), or entries it skips as unanimous land here with
    nothing to resolve them.
    """
    picked, names = [], set()
    for reg in REGISTRIES:
        ids = _split(row.get(f"{reg}_ids"))
        rnames = _split(row.get(f"{reg}_names"))
        fuel = row.get(f"{reg}_fuel")
        if not ids:
            continue
        if str(fuel) == "conflict":
            return None
        picked.append((reg, ids[0], rnames[0] if rnames else "", fuel))
        names.update((n or "").strip().lower() for n in rnames)
    if not picked or len(names) > 1:
        return None
    # Prefer OPSD, then PyPSA, then BNetzA — same priority order match_exact
    # uses, and it puts a coordinate-bearing registry first where one exists.
    return picked[0]


def _n_candidates(row):
    return sum(len(_split(row.get(f"{reg}_ids"))) for reg in REGISTRIES)


def main() -> None:
    entries = pd.read_csv(ENTRIES)
    exact = pd.read_csv(EXACT).set_index("plant") if os.path.exists(EXACT) else pd.DataFrame()
    llm = pd.read_csv(LLM).set_index("plant") if os.path.exists(LLM) else pd.DataFrame()
    wiki = pd.read_csv(WIKI).set_index("plant") if os.path.exists(WIKI) else pd.DataFrame()
    geo = pd.read_csv(GEO).set_index("plant") if os.path.exists(GEO) else pd.DataFrame()
    coords = _load_coords()

    if not os.path.exists(LLM):
        print("note: no matches_llm.csv — ranked guesses will be missing "
              "(run match_llm.py)")
    if not os.path.exists(GEO):
        print("note: no matches_geo.csv — area-level fallback unavailable "
              "(run match_geo.py)")
    if not os.path.exists(WIKI):
        print("note: no matches_wikipedia.csv — wiki fallback unavailable "
              "(run match_wikipedia.py)")

    rows = []
    for e in entries.to_dict("records"):
        plant = e["plant"]
        ex = exact.loc[plant].to_dict() if plant in exact.index else {}
        lm = llm.loc[plant].to_dict() if plant in llm.index else {}
        wk = wiki.loc[plant].to_dict() if plant in wiki.index else {}
        gg = geo.loc[plant].to_dict() if plant in geo.index else {}

        out = {
            "plant":              plant,
            "betroffene_anlage":  e.get("betroffene_anlage"),
            "category":           e.get("category"),
            "primaerenergieart":  e.get("primaerenergieart"),
            "n_events":           e.get("n_events"),
            "max_dispatched_mw":  e.get("max_dispatched_mw"),
            "n_candidates":       _n_candidates(ex),
        }
        for reg in REGISTRIES:
            out[f"{reg}_ids"] = ex.get(f"{reg}_ids")
            out[f"{reg}_names"] = ex.get(f"{reg}_names")
            out[f"{reg}_fuel"] = ex.get(f"{reg}_fuel")

        had_conflict = any(str(ex.get(f"{reg}_fuel")) == "conflict" for reg in REGISTRIES)
        sole = _sole_candidate(ex) if ex else None

        if sole:
            reg, cid, name, fuel = sole
            basis, fuel_status = "unanimous", "agreed"
        elif not _blank(lm.get("best_guess_id")):
            reg = lm["best_guess_registry"]
            cid = str(lm["best_guess_id"])
            name = lm.get("best_guess_name")
            fuel = lm.get("best_guess_fuel")
            basis = "llm"
            fuel_status = "llm_resolved" if had_conflict else "agreed"
        elif str(wk.get("status")) == "match" and not _blank(wk.get("wiki_fuel")):
            reg, cid, name = "wikipedia", None, wk.get("title")
            fuel = wk.get("wiki_fuel")
            basis, fuel_status = "wikipedia", "wikipedia"
        else:
            reg = cid = name = fuel = None
            basis = "none"
            fuel_status = "conflict" if had_conflict else "unknown"

        # Coordinate precedence: a real plant coordinate always beats an
        # area-level one, and `coord_precision` says which you got. An `area`
        # or `state` row is a centroid that may sit tens of km from anything
        # generating power — never render it as a plant pin.
        lat, lon, coord_source, precision, area_label = None, None, None, None, None
        if cid is not None and (reg, cid) in coords:
            lat, lon = coords[(reg, cid)]
            coord_source, precision = reg, "plant"
        if _blank(lat) and not _blank(wk.get("lat")):
            lat, lon = wk.get("lat"), wk.get("lon")
            coord_source, precision = "wikipedia", "plant"
        if _blank(lat) and gg:
            lat, lon = gg.get("lat"), gg.get("lon")
            coord_source = gg.get("geo_source")
            precision = gg.get("coord_precision")
            area_label = gg.get("area_label")

        out.update({
            "best_guess_id":       cid,
            "best_guess_registry": reg,
            "best_guess_name":     name,
            "best_guess_fuel":     fuel,
            "guess_basis":         basis,
            "fuel_status":         fuel_status,
            "llm_confidence":      lm.get("llm_confidence"),
            "llm_reasoning":       lm.get("llm_reasoning"),
            "lat":                 lat,
            "lon":                 lon,
            "coord_source":        coord_source,
            "coord_precision":     precision,
            "area_label":          area_label,
            "wiki_title":          wk.get("title"),
            "wiki_url":            wk.get("url"),
        })
        rows.append(out)

    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUT, index=False, encoding="utf-8")

    # ---- report ---------------------------------------------------------------
    n = len(out_df)
    print(f"-> {OUT}: {n} rows\n")
    print("guess_basis:")
    print(out_df["guess_basis"].value_counts().to_string())
    print("\nfuel_status:")
    print(out_df["fuel_status"].value_counts().to_string())
    n_coord = int(out_df["lat"].notna().sum())
    n_guess = int(out_df["best_guess_id"].notna().sum()
                  + (out_df["guess_basis"] == "wikipedia").sum())
    print(f"\nbest guess available: {n_guess}/{n} ({100 * n_guess / n:.0f}%)")
    print(f"with coordinates:     {n_coord}/{n} ({100 * n_coord / n:.0f}%)")
    print("\ncoord_precision (plant = a real plant location; the rest are "
          "centroids):")
    print(out_df["coord_precision"].value_counts().to_string())


if __name__ == "__main__":
    main()
