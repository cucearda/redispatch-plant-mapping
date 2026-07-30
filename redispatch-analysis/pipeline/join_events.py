"""Join the matcher output back onto the redispatch event rows.

This is the bridge between the two halves of the repo: `matcher/`
produces one row per distinct plant name; the maps and figures need one row per
redispatch *event*, carrying a coordinate. Ported from
`Join_redispatch_new_matcher.ipynb` so the step is reproducible from the shell.

    python pipeline/join_events.py

Reads the two raw exports plus the newest `plant_matcher_output*.xlsx|csv` in
`results/`, and writes `data/redispatch_joined_high_confidence.csv`.

Filtering, in order — each step is reported so the drop-off is auditable:
  1. explode comma-separated BETROFFENE_ANLAGE so each row holds ONE plant
  2. drop Probefahrt (test runs) and Börse (market countertrade, no plant)
  3. keep only the four accepted Maßnahme types
  4. keep only rows the matcher gave a coordinate
  5. optionally keep only rows at or above MIN_CONFIDENCE
"""

import argparse
import glob
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from matcher.config import DATA, OUT_DIR, REDISPATCH_SOURCES  # noqa: E402

OUT_PATH = DATA / "redispatch_joined_high_confidence.csv"

# Maßnahme types that represent an actual redispatch intervention. Everything
# else (Probefahrt, Netzreserve tests, …) is excluded from the spatial read.
ACCEPTED = [
    "Strombedingter Redispatch",
    "Spannungsbedingter Redispatch",
    "Gezielter Leistungsausgleich bei Einspeise",
    "Strombedingter Redispatch kurativ",
]


def _norm_key(s):
    """Merge key shared by both sides of the join."""
    if not isinstance(s, str):
        return s
    s = s.lower().strip()
    s = re.sub(r"\s*\(bnbm\)", "", s)        # strip the '(BNbM)' suffix
    return re.sub(r"\s+", " ", s)


def latest_matcher_output(results_dir):
    """Newest plant_matcher_output*.{xlsx,csv}, ignoring _partial checkpoints."""
    cands = (glob.glob(os.path.join(results_dir, "plant_matcher_output*.xlsx"))
             + glob.glob(os.path.join(results_dir, "plant_matcher_output*.csv")))
    cands = [f for f in cands if "_partial" not in os.path.basename(f)]
    if not cands:
        raise FileNotFoundError(
            f"No plant_matcher_output*.{{xlsx,csv}} in {results_dir} — "
            "run `python matcher/main.py` first.")
    return max(cands, key=os.path.getmtime)


def load_redispatch_raw():
    """Concatenate the raw exports, normalising their differing header quirks.

    The 2021+ export has a trailing ',,,' on its header line that mangles the
    last column name (`PRIMAERENERGIEART,,,`); both files may carry a BOM.
    """
    frames = []
    for path, enc in REDISPATCH_SOURCES:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found — see data/README.md")
        df = pd.read_csv(path, sep=";", encoding=enc, low_memory=False)
        df.columns = [str(c).lstrip("﻿").strip().rstrip(",").strip()
                      for c in df.columns]
        df["source_file"] = os.path.basename(path)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def main(min_confidence=0.0):
    matcher_path = latest_matcher_output(str(OUT_DIR))
    print(f"Matcher source: {os.path.basename(matcher_path)}")
    matcher = (pd.read_excel(matcher_path) if matcher_path.lower().endswith(".xlsx")
               else pd.read_csv(matcher_path, encoding="utf-8-sig"))
    print(f"Matcher rows:              {len(matcher):>9,}")
    print(f"  with a coordinate:       {matcher['final_lat'].notna().sum():>9,}")

    rd = load_redispatch_raw()
    print(f"Raw event rows:            {len(rd):>9,}")

    # One plant per row.
    rd["BETROFFENE_ANLAGE_RAW"] = rd["BETROFFENE_ANLAGE"]
    rd["BETROFFENE_ANLAGE"] = rd["BETROFFENE_ANLAGE"].astype(str).str.split(",")
    rd = rd.explode("BETROFFENE_ANLAGE", ignore_index=True)
    rd["BETROFFENE_ANLAGE"] = rd["BETROFFENE_ANLAGE"].str.strip()
    # The explode produces fragments (bare numbers, single characters) wherever
    # a plant name legitimately contained a comma — drop those.
    rd = rd[(rd["BETROFFENE_ANLAGE"].str.len() >= 2)
            & (~rd["BETROFFENE_ANLAGE"].str.fullmatch(r"\d+"))].copy()
    print(f"After explode:             {len(rd):>9,}")

    rd["merge_key"] = rd["BETROFFENE_ANLAGE"].map(_norm_key)
    matcher = matcher.assign(merge_key=matcher["plant_name"].map(_norm_key))
    full = rd.merge(matcher, on="merge_key", how="left")

    is_probe = full["GRUND_DER_MASSNAHME"].astype(str).str.strip().str.lower() == "probefahrt"
    is_boerse = full["BETROFFENE_ANLAGE"].astype(str).str.strip().str.lower() == "börse"
    filtered = full[~is_probe & ~is_boerse]

    accepted_clean = {re.sub(r"\s+", "", m) for m in ACCEPTED}
    mclean = filtered["GRUND_DER_MASSNAHME"].astype(str).str.replace(r"\s+", "", regex=True)
    filtered = filtered[mclean.isin(accepted_clean)].copy()
    print(f"After Probefahrt/Börse/Maßnahme filter: {len(filtered):>9,}")

    matched = filtered[filtered["final_lat"].notna()].copy()
    if min_confidence > 0:
        matched = matched[matched["final_confidence"] >= min_confidence]
    print(f"With a coordinate"
          f"{f' and confidence >= {min_confidence}' if min_confidence else ''}: "
          f"{len(matched):>9,}")
    if len(filtered):
        print(f"  -> mapped share of cleaned events: {len(matched) / len(filtered):.1%}")

    # The unmapped remainder is a result in its own right — report it rather
    # than letting it disappear silently from the map.
    unmatched = filtered[filtered["final_lat"].isna()]
    if len(unmatched):
        print(f"\nUnmapped events: {len(unmatched):,}. Top names:")
        print(unmatched["BETROFFENE_ANLAGE"].value_counts().head(10).to_string())

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    matched.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"\nWrote {OUT_PATH}  ({len(matched):,} rows)")
    return matched


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="drop matches below this final_confidence (default: keep all)")
    main(ap.parse_args().min_confidence)
