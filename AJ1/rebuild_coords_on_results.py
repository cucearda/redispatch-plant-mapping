"""
rebuild_coords_on_results.py — ONE-OFF: apply the new coordinate workflow
(coords.py + postcodes.py) to the EXISTING results/AJ1/
redispatch_plant_matches.csv, without re-running the identity stages.

Why this exists rather than just re-running assemble_results.py:
temp_AJ1/matches_llm.csv and temp_AJ1/matches_geo.csv were deleted after
the last full run (the whole temp_*/ tree is gitignored). assemble_results
skips missing inputs silently, so re-running it would quietly drop 233 LLM
guesses — and regenerating them means re-paying ~237 Opus API calls for
answers we already have. The identity columns are already sitting in the
results file, so this script keeps everything up to `llm_reasoning`, throws
away only the coordinate block, and rebuilds that with the new logic.

Order matters here, and it is the order the pipeline itself should use
(main.py has since been fixed to match): assign plant-level coordinates
FIRST from every dataset that has one, and only then fall back to
match_geo's area-level resolvers for whatever is still unlocated — so no
Nominatim call is spent on an entry that already resolved.

    python rebuild_coords_on_results.py            # write .new.csv, diff only
    python rebuild_coords_on_results.py --apply    # overwrite, keeping .bak

After this, future full runs via main.py produce the same thing through
assemble_results.py; this script is not part of the pipeline.
"""

import os
import shutil
import sys

import pandas as pd

import coords as coords_mod
import match_geo
from assemble_results import _load_coords, _load_plz, _split, OUT as RESULTS
from paths import TEMP_DIR

WIKI = os.path.join(TEMP_DIR, "matches_wikipedia.csv")
GEO_OUT = os.path.join(TEMP_DIR, "matches_geo.csv")
NEW = RESULTS.replace(".csv", ".new.csv")

# Everything from the coordinate block onward is rebuilt; everything before
# it (identity, candidate lists, best_guess_*, llm_*) is kept as-is.
DROP = ["lat", "lon", "coord_source", "coord_precision", "area_label",
        "wiki_title", "wiki_url", "wiki_fuel",
        "coord_basis", "coord_score", "coord_spread_km",
        "coord_n_sources", "coord_sources", "plz_check"]

# match_geo.py's own labels — a previous coordinate is only safe to reuse if
# it came from one of these. If a row that used to have an opsd/psa/wikipedia
# point loses it, that is a real logic regression and must stay visible.
GEO_SOURCES = set(match_geo.PRECISION)


def main(apply=False):
    df = pd.read_csv(RESULTS)
    print(f"read {RESULTS}: {len(df)} rows, {len(df.columns)} cols")
    before = df[["plant", "lat", "lon", "coord_source", "coord_precision"]].copy()
    # The previous run's own area-level answers, kept so a transient
    # geocoder failure can't silently delete a coordinate we already had.
    # (First dry run lost 17 rows exactly this way: all 17 were `nominatim`
    # rows whose re-lookup came back empty under rate-limiting, even though
    # the service answers fine when queried on its own.)
    prev_geo = {
        r["plant"]: {"geo_source": r.get("coord_source"),
                     "coord_precision": r.get("coord_precision"),
                     "lat": r.get("lat"), "lon": r.get("lon"),
                     "area_label": r.get("area_label")}
        for r in df.to_dict("records")
        if pd.notna(r.get("lat"))
        and r.get("coord_source") in GEO_SOURCES
    }

    df = df.drop(columns=[c for c in DROP if c in df.columns])
    print(f"kept {len(df.columns)} identity/candidate columns")

    wiki = (pd.read_csv(WIKI).set_index("plant")
            if os.path.exists(WIKI) else pd.DataFrame())
    coord_lookup = _load_coords()
    plz_lookup = _load_plz()

    # ---- pass 1: plant-level coordinates from every dataset that has one ----
    recs = df.to_dict("records")
    results, unresolved = [], []
    for e in recs:
        plant = e["plant"]
        wk = wiki.loc[plant].to_dict() if plant in wiki.index else {}
        wiki_ok = str(wk.get("status")) == "match"

        sources = {}
        for r_ in ("opsd", "psa"):
            pt = coords_mod.points_for_ids(_split(e.get(f"{r_}_ids")),
                                           coord_lookup, r_)
            if pt:
                sources[r_] = pt
        if wiki_ok and pd.notna(wk.get("lat")):
            sources["wikipedia"] = (float(wk["lat"]), float(wk["lon"]))

        plz_list = [plz_lookup.get(str(i)) for i in _split(e.get("bnetza_ids"))]
        plz_list = [p for p in plz_list if p]

        cinfo = coords_mod.reconcile(sources, e.get("best_guess_registry"),
                                     plz_list, None)
        e.update(cinfo)
        e["wiki_title"] = wk.get("title") if wiki_ok else None
        e["wiki_url"] = wk.get("url") if wiki_ok else None
        e["wiki_fuel"] = wk.get("wiki_fuel") if wiki_ok else None
        results.append(e)
        if cinfo["lat"] is None and e.get("category") != "countertrade":
            unresolved.append(e)

    n_plant = sum(1 for e in results if e["lat"] is not None)
    print(f"\npass 1 (plant-level): {n_plant} located, "
          f"{len(unresolved)} still need an area fallback")

    # ---- pass 2: area fallback, only for what pass 1 could not locate ------
    # Reuse an already-computed matches_geo.csv when there is one, so a
    # re-run costs no network at all and is idempotent.
    existing_geo = (pd.read_csv(GEO_OUT).set_index("plant").to_dict("index")
                    if os.path.exists(GEO_OUT) else {})
    if existing_geo:
        print(f"reusing {len(existing_geo)} rows from the existing {GEO_OUT}")

    geo_rows, n_reused, n_geocoded = [], 0, 0
    gaz_loaded = False
    for i, e in enumerate(unresolved, 1):
        plant = e["plant"]
        hit = existing_geo.get(plant)
        if hit is None:
            if not gaz_loaded:      # ~2.3 s, and only needed if we geocode
                match_geo.load_gazetteer()
                gaz_loaded = True
            hit = match_geo.resolve_entry(plant, e.get("category"),
                                          allow_network=True)
            n_geocoded += 1
        if hit is None and plant in prev_geo:
            # Geocoder gave nothing this time; keep what the pipeline
            # already had rather than dropping the row to no coordinate.
            hit = prev_geo[plant]
            n_reused += 1
        if hit:
            # resolve_entry() returns no coord_precision — match_geo.main()
            # derives it separately — so derive it here too, or reconcile
            # scores the row as an unknown tier and gives it 0.
            geo_row = {
                "plant": plant, "category": e.get("category"),
                **hit,
                "coord_precision": hit.get("coord_precision")
                or match_geo.PRECISION.get(hit["geo_source"], "area"),
            }
            geo_rows.append(geo_row)
            e.update(coords_mod.reconcile({}, e.get("best_guess_registry"),
                                          [], geo_row))
        if i % 100 == 0:
            print(f"  ... {i}/{len(unresolved)} processed "
                  f"({n_geocoded} geocoded)", flush=True)

    geo_df = pd.DataFrame(geo_rows, columns=["plant", "category", "geo_source",
                                             "coord_precision", "lat", "lon",
                                             "area_label"])
    geo_df.to_csv(GEO_OUT, index=False, encoding="utf-8")
    print(f"\n-> {GEO_OUT}: {len(geo_df)} area-level coordinates "
          f"(regenerated, so future assembles have it again; "
          f"{n_reused} reused from the previous run after an empty geocode)")

    out = pd.DataFrame(results)
    out.to_csv(NEW, index=False, encoding="utf-8")
    print(f"-> {NEW}: {len(out)} rows, {len(out.columns)} cols")

    _report(out, before)

    if apply:
        bak = RESULTS.replace(".csv", ".bak.csv")
        shutil.copy2(RESULTS, bak)
        shutil.move(NEW, RESULTS)
        print(f"\napplied: {RESULTS} updated (previous kept at {bak})")
    else:
        print(f"\ndry run — {RESULTS} untouched. Re-run with --apply to overwrite.")


def _report(out, before):
    m = out.merge(before, on="plant", how="left", suffixes=("", "_old"))
    had = m["lat_old"].notna()
    has = m["lat"].notna()

    print(f"\n{'=' * 62}\ncoordinate coverage: {int(had.sum())} -> {int(has.sum())} "
          f"of {len(m)}")
    print(f"  gained a coordinate: {int((~had & has).sum())}")
    print(f"  LOST a coordinate:   {int((had & ~has).sum())}   (must be 0)")

    moved = m[had & has].copy()
    if len(moved):
        d = [coords_mod.haversine_km(a, b, c, e) for a, b, c, e in
             zip(moved["lat_old"], moved["lon_old"], moved["lat"], moved["lon"])]
        moved["moved_km"] = d
        print(f"\n  of {len(moved)} rows that already had one, "
              f"{int((moved['moved_km'] > 0.001).sum())} moved; "
              f"median {moved['moved_km'].median():.3f} km, "
              f"max {moved['moved_km'].max():.2f} km")
        far = moved[moved["moved_km"] > 5].sort_values("moved_km", ascending=False)
        if len(far):
            print(f"  moved > 5 km ({len(far)}):")
            for r in far.head(15).itertuples():
                print(f"    {r.plant!r}: {r.moved_km:.1f} km  "
                      f"{r.coord_source_old} -> {r.coord_source}")

    print("\ncoord_basis:")
    print(out["coord_basis"].value_counts().to_string())
    print("\ncoord_precision:")
    print(out["coord_precision"].value_counts(dropna=False).to_string())
    print("\nplz_check:")
    print(out["plz_check"].value_counts(dropna=False).to_string())

    scored = out[out["coord_score"] > 0]
    if len(scored):
        print(f"\ncoord_score over {len(scored)} located rows: "
              f"mean {scored['coord_score'].mean():.3f}, "
              f"median {scored['coord_score'].median():.3f}")
        print(pd.cut(scored["coord_score"],
                     [0, .25, .5, .75, .9, 1.0]).value_counts().sort_index().to_string())

    disputed = out[out["coord_basis"] == "best_guess_disputed"]
    print(f"\ndisputed (datasets disagree by > {coords_mod.AGREE_KM:.0f} km): "
          f"{len(disputed)}")
    for r in disputed.sort_values("coord_spread_km", ascending=False).itertuples():
        print(f"  {r.plant!r}: {r.coord_spread_km:.1f} km ({r.coord_sources}) "
              f"-> kept {r.coord_source}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
