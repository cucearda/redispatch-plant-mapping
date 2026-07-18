"""
geocode_backfill.py — final coordinate backfill.

Any row in the assembled table still without coordinates (unmatchable aggregates like
control-reserve regions / substations / regional buckets, and residual failures) gets an
approximate coordinate from the name via Haiku, so it can enter the spatial analysis.
countertrade / emergency entries are skipped — they have no location.

Rewrites results/redispatch_plant_matches.csv in place.
"""

import os

import pandas as pd

from geocode import geocode_names

FILE = "results/redispatch_plant_matches.csv"
NO_PLACE = {"countertrade", "emergency"}


def main() -> None:
    df = pd.read_csv(FILE)
    coordless = df[df["lat"].isna() & ~df["entry_type"].isin(NO_PLACE)]
    names = list(coordless["betroffene_anlage"])
    print(f"backfilling coordinates for {len(names)} rows via Haiku geocode …")

    coords = geocode_names(names)
    n = 0
    for i, name in zip(coordless.index, coordless["betroffene_anlage"]):
        c = coords.get(name)
        if c:
            df.at[i, "lat"], df.at[i, "lon"] = c
            df.at[i, "coord_source"] = "geocode"
            n += 1

    df.to_csv(FILE, index=False, encoding="utf-8")
    print(f"→ filled {n} coordinates; total with coordinates: {df['lat'].notna().sum()}/{len(df)}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
