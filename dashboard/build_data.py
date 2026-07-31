"""Build the dashboard data files (dashboard/data_<LABEL>.js) from the raw
redispatch calls and a pipeline's plant-coordinate matches.

Reads:
  - The combined 2013-2026 redispatch dataset, via prep_data.prep_redispatch()
    (see ../prep_data.py — the raw-export combining, timezone correction, and
    direction-encoding cleanup live there now, shared with any pipeline).
  - results/<LABEL>/redispatch_plant_matches.csv   one row per distinct plant
                                           name (or composed bundle), with a
                                           lat/lon and some notion of match
                                           confidence/type. Each pipeline's
                                           raw schema is normalised to a common
                                           shape by the PIPELINES adapters
                                           below before the rest of build()
                                           runs unchanged.

Writes, per pipeline in PIPELINES:
  - dashboard/data_<LABEL>.js               `const REDISPATCH_DATA_<LABEL> = {...}`
                                           loaded by index.html via <script
                                           src> (works from file:// with no
                                           server / CORS issue).

Each raw call is classified as:
  - mapped        -> the matched plant (or multi-plant bundle) has coordinates
                     (drawn as a map circle)
  - boerse        -> the raw plant name itself is "Börse" (the market
                     countertrade entry) -- checked directly on the raw name
                     so it classifies the same regardless of whether a given
                     pipeline's matches file carries that row at all
  - not_identified-> everything else without coordinates

Only dependency is pandas (already used elsewhere in the project).

Run from anywhere:  python dashboard/build_data.py            # builds all pipelines
                     python dashboard/build_data.py --pipeline A1   # just one
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

# Resolve paths relative to the repo root (this file lives in dashboard/).
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # so `import prep_data` works when run from anywhere

import prep_data

BOERSE_NAME = "Börse"


def _bucket_confidence_numeric(v):
    """0..1 float -> high/medium/low/none, using J1's own HIGH_CONF_SHORTCIRCUIT
    (0.85) as the high threshold."""
    if pd.isna(v):
        return "none"
    v = float(v)
    if v <= 0:
        return "none"
    if v >= 0.85:
        return "high"
    if v >= 0.5:
        return "medium"
    return "low"


def _bucket_confidence_text(v):
    """A1 already writes a high/medium/low/none-ish label; normalise blanks."""
    s = str(v).strip().lower() if pd.notna(v) and str(v).strip() else "none"
    return s if s in ("high", "medium", "low", "none") else "none"


def adapt_a1(m):
    """results/A1/redispatch_plant_matches.csv -> common schema."""
    m = m.copy()
    m["name"] = m["betroffene_anlage"].astype(str).str.strip()
    m["entry_type"] = m["entry_type"]
    m["matched"] = m["matched_id"].notna()
    m["confidence"] = m["confidence"].map(_bucket_confidence_text)
    return m.set_index("name")[["lat", "lon", "fueltype", "entry_type", "matched", "confidence"]]


def adapt_j1(m):
    """results/J1/redispatch_plant_matches.csv -> common schema.

    J1 has no fine-grained fueltype column (only a coarse energy CLASS from
    whichever registry matched), no boolean "matched to a specific plant ID"
    flag (derived here from opsd_match/psa_match being an actual registry
    hit vs. every other fallback source), and a continuous 0..1
    final_confidence instead of a label (bucketed via
    _bucket_confidence_numeric using J1's own HIGH_CONF_SHORTCIRCUIT=0.85).
    """
    m = m.copy()
    m["name"] = m["plant_name"].astype(str).str.strip()
    m["lat"] = m["final_lat"]
    m["lon"] = m["final_lon"]
    m["fueltype"] = m["opsd_class"].where(m["opsd_match"] != "none", m["psa_class"])
    # "single" (an ordinary individual plant) carries no special tag, matching
    # A1's convention where "individual" rows leave nothing distinctive to show
    m["entry_type"] = m["aggregation"].where(m["aggregation"] != "single", None)
    m["matched"] = (m["opsd_match"] != "none") | (m["psa_match"] != "none")
    m["confidence"] = m["final_confidence"].map(_bucket_confidence_numeric)
    return m.set_index("name")[["lat", "lon", "fueltype", "entry_type", "matched", "confidence"]]


# label -> (matches file relative to repo root, adapter function)
PIPELINES = {
    "A1": (os.path.join("results", "A1", "redispatch_plant_matches.csv"), adapt_a1),
    "J1": (os.path.join("results", "J1", "redispatch_plant_matches.csv"), adapt_j1),
}


def load_raw():
    return prep_data.prep_redispatch()


def load_matches(label):
    rel_path, adapter = PIPELINES[label]
    path = os.path.join(ROOT, rel_path)
    m = pd.read_csv(path)
    return adapter(m)


def classify(name, row):
    """Return 'mapped' | 'boerse' | 'not_identified' for one raw call."""
    if name == BOERSE_NAME:
        return "boerse"
    if row is not None and pd.notna(row.get("lat")) and pd.notna(row.get("lon")):
        return "mapped"
    return "not_identified"


def build(label, raw):
    rel_path, _ = PIPELINES[label]
    out_file = os.path.join(HERE, f"data_{label}.js")
    matches = load_matches(label)
    lookup = matches.to_dict("index")

    def info(name):
        return lookup.get(name)

    # ---- classify every raw call --------------------------------------------
    cats, lats, lons, fuels, etypes, matcheds, confs = [], [], [], [], [], [], []
    for name in raw["name"]:
        m = info(name)
        cats.append(classify(name, m))
        if m is None:
            lats.append(None); lons.append(None); fuels.append(None); etypes.append(None)
            matcheds.append(False); confs.append(None)
        else:
            lats.append(m.get("lat")); lons.append(m.get("lon"))
            fuels.append(m.get("fueltype")); etypes.append(m.get("entry_type"))
            matcheds.append(bool(m.get("matched")))
            confs.append(m.get("confidence"))
    raw = raw.assign(category=cats, lat=lats, lon=lons,
                     fueltype=fuels, entry_type=etypes,
                     matched=matcheds, confidence=confs)

    # ---- mapped plants (incl. composed multi-plant bundles): per (name, day)
    # volume split ------------------------------------------------------------
    mapped = raw[raw["category"] == "mapped"]
    plants = []
    grp = mapped.groupby("name", sort=False)
    for name, sub in grp:
        first = sub.iloc[0]
        daily = sub.groupby("day")[["inc", "dec"]].sum()
        series = {
            day: [round(float(v.inc), 3), round(float(v.dec), 3)]
            for day, v in daily.iterrows()
        }
        fuel = first["fueltype"]
        etype = first["entry_type"]
        conf = first["confidence"] if pd.notna(first["confidence"]) else "none"
        plants.append({
            "name": name,
            "lat": round(float(first["lat"]), 5),
            "lon": round(float(first["lon"]), 5),
            "fueltype": None if pd.isna(fuel) else str(fuel),
            "entry_type": None if pd.isna(etype) else str(etype),
            "matched": bool(first["matched"]),
            "confidence": conf,
            "series": series,
        })

    # ---- unmapped daily totals ----------------------------------------------
    def daily_totals(cat):
        sub = raw[raw["category"] == cat]
        if sub.empty:
            return {}
        s = sub.groupby("day")["mwh"].sum()
        return {day: round(float(v), 3) for day, v in s.items()}

    unmapped = {
        "boerse": daily_totals("boerse"),
        "not_identified": daily_totals("not_identified"),
    }

    # ---- meta + sanity totals -----------------------------------------------
    total_mwh = float(raw["mwh"].sum())
    mapped_mwh = float(mapped["mwh"].sum())
    boerse_mwh = float(raw.loc[raw["category"] == "boerse", "mwh"].sum())
    ni_mwh = float(raw.loc[raw["category"] == "not_identified", "mwh"].sum())

    data = {
        "meta": {
            "pipeline": label,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "date_min": raw["day"].min(),
            "date_max": raw["day"].max(),
            "n_plants": len(plants),
            "total_mwh": round(total_mwh, 1),
            "mapped_mwh": round(mapped_mwh, 1),
            "boerse_mwh": round(boerse_mwh, 1),
            "not_identified_mwh": round(ni_mwh, 1),
        },
        "plants": plants,
        "unmapped": unmapped,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(f"// Generated by build_data.py --pipeline {label} — do not edit by hand.\n")
        f.write(f"const REDISPATCH_DATA_{label} = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    # ---- report -------------------------------------------------------------
    size_kb = os.path.getsize(out_file) / 1024
    print(f"\n[{label}] Wrote {out_file} ({size_kb:.0f} KB)")
    print(f"[{label}] Date range : {data['meta']['date_min']} .. {data['meta']['date_max']}")
    print(f"[{label}] Plants     : {len(plants)} with coordinates")
    print(f"[{label}] Total MWh  : {total_mwh:,.0f}")
    print(f"[{label}]   mapped         {mapped_mwh:12,.0f}  ({mapped_mwh / total_mwh:6.1%})")
    print(f"[{label}]   Börse          {boerse_mwh:12,.0f}  ({boerse_mwh / total_mwh:6.1%})")
    print(f"[{label}]   not identified {ni_mwh:12,.0f}  ({ni_mwh / total_mwh:6.1%})")
    checksum = mapped_mwh + boerse_mwh + ni_mwh
    print(f"[{label}]   sum check      {checksum:12,.0f}  ({checksum / total_mwh:6.1%})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", choices=sorted(PIPELINES), default=None,
                        help="build only this pipeline's data file (default: build all)")
    args = parser.parse_args()

    raw = load_raw()
    labels = [args.pipeline] if args.pipeline else sorted(PIPELINES)
    for label in labels:
        build(label, raw)


if __name__ == "__main__":
    main()
