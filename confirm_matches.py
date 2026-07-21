"""
confirm_matches.py — coordinate cross-check on matched rows.

For every matched row, independently geocode the redispatch name (Haiku) and compare
that coordinate to the matched plant's coordinate. Agreement within CHECK_KM confirms
the location; a large disagreement flags the row for manual review — a likely
wrong-region name collision the fuel/capacity filter didn't catch.

CHECK_KM is deliberately loose: the geocode is town/site-level and a plant can sit some
km from the town centre, so this catches *wrong region*, not small offsets.

Adds columns `coord_check` {confirmed · disagree · no_geocode} and `check_km`; sets
needs_review=yes on disagreements. Run AFTER assemble_results + geocode_backfill.
Rewrites results/redispatch_plant_matches.csv in place.
"""

import math
import os

import pandas as pd

from geocode import geocode_names

FILE     = "results/redispatch_plant_matches.csv"
CHECK_KM = 30.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def main() -> None:
    df = pd.read_csv(FILE)
    df["coord_check"] = ""                 # object column
    df["check_km"] = float("nan")          # float column (accepts the distances)

    # multi_plant is excluded: its coordinate is the centroid of plants spread across the
    # country, so geocoding the bundle string can only ever "disagree" — nothing to confirm.
    matched = df[df["matched_id"].notna() & df["lat"].notna()
                 & df["entry_type"].ne("multi_plant")]
    print(f"cross-checking {len(matched)} matched rows via Haiku geocode …")
    coords = geocode_names(list(matched["betroffene_anlage"]))

    n_conf = n_dis = n_no = 0
    for i, r in matched.iterrows():
        g = coords.get(r["betroffene_anlage"])
        if not g:
            df.at[i, "coord_check"] = "no_geocode"
            n_no += 1
            continue
        d = haversine_km(g[0], g[1], float(r["lat"]), float(r["lon"]))
        df.at[i, "check_km"] = round(d, 1)
        if d <= CHECK_KM:
            df.at[i, "coord_check"] = "confirmed"
            n_conf += 1
        else:
            df.at[i, "coord_check"] = "disagree"
            n_dis += 1
            # only flag when the match wasn't already high-confidence — a coarse geocoder
            # is less reliable than an exact/high match, so don't second-guess those.
            if r["confidence"] != "high":
                df.at[i, "needs_review"] = "yes"
                df.at[i, "reasoning"] = f"{r['reasoning']} | coord-check: name geocodes {d:.0f} km away"

    df.to_csv(FILE, index=False, encoding="utf-8")
    print(f"→ confirmed {n_conf} · disagree {n_dis} (flagged for review) · no_geocode {n_no}")
    if n_dis or n_conf:
        km = pd.to_numeric(df["check_km"], errors="coerce").dropna()
        print(f"  check_km: median {km.median():.0f}, 90th pct {km.quantile(.9):.0f}, max {km.max():.0f}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
