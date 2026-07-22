"""Build the dashboard data file (dashboard/data.js) from the raw redispatch
calls and the plant-coordinate matches.

Reads (both raw exports, covering 2013-2026 together):
  - data/Redispatch_Daten_2013_2020.csv
  - data/Redispatch_Daten_2021_2026.csv
  - results/redispatch_plant_matches.csv   one row per distinct plant name
                                           (or composed multi_plant bundle),
                                           with lat/lon/fueltype/entry_type

Writes:
  - data/Redispatch_Daten_2013_2026.csv    the two exports combined, timezone-
                                           normalized and de-duplicated. A
                                           derived cache (gitignored) — delete
                                           and re-run to regenerate.
  - dashboard/data.js                      `const REDISPATCH_DATA = {...}`
                                           loaded by index.html via <script
                                           src> (works from file:// with no
                                           server / CORS issue).

Each raw call is classified as:
  - mapped        -> the matched plant (or multi-plant bundle) has coordinates
                     (drawn as a map circle)
  - boerse        -> entry_type == "countertrade" (the "Börse" market entry)
  - not_identified-> everything else without coordinates

Two source-data quirks are corrected here, not in the raw files:
  - The 2013-2020 export timestamps are labelled UTC; the 2021-2026 export
    (and everything since) is CET/CEST local time. Left alone, calendar days
    would misalign by 1-2h across the 2020/2021 boundary — every timestamp is
    converted to Europe/Berlin local time before its "day" is taken.
  - 6 rows in Redispatch_Daten_2021_2026.csv have a corrupted byte in
    "erhöhen" (a mixed-encoding artifact in the source export). RICHTUNG is
    matched by substring ("erh" / "reduzieren") rather than exact equality so
    these still classify correctly.

Only dependency is pandas (already used elsewhere in the project).

Run from anywhere:  python dashboard/build_data.py
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd

# Resolve paths relative to the repo root (this file lives in dashboard/).
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RAW_FILES = [
    os.path.join(ROOT, "data", "Redispatch_Daten_2013_2020.csv"),
    os.path.join(ROOT, "data", "Redispatch_Daten_2021_2026.csv"),
]
COMBINED_FILE = os.path.join(ROOT, "data", "Redispatch_Daten_2013_2026.csv")
MATCHES_FILE = os.path.join(ROOT, "results", "redispatch_plant_matches.csv")
OUT_FILE = os.path.join(HERE, "data.js")

BERLIN = "Europe/Berlin"


def _num(series):
    """Parse a German comma-decimal string column into floats."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    )


def _local_day(df):
    """Europe/Berlin calendar day for BEGINN_DATUM+BEGINN_UHRZEIT, aware that
    ZEITZONE_VON is either 'UTC' (2013-2020 export) or 'CET'/'CEST' (2021-2026
    export, already Berlin wall-clock time)."""
    naive = pd.to_datetime(
        df["BEGINN_DATUM"] + " " + df["BEGINN_UHRZEIT"],
        format="%d.%m.%Y %H:%M", errors="coerce",
    )
    is_utc = df["ZEITZONE_VON"].astype(str).str.strip().str.upper().eq("UTC")

    day = pd.Series(pd.NA, index=df.index, dtype="object")
    utc_local = (naive[is_utc]
                 .dt.tz_localize("UTC", ambiguous="NaT", nonexistent="NaT")
                 .dt.tz_convert(BERLIN))
    day.loc[is_utc] = utc_local.dt.strftime("%Y-%m-%d")

    berlin_local = naive[~is_utc].dt.tz_localize(
        BERLIN, ambiguous="NaT", nonexistent="NaT")
    day.loc[~is_utc] = berlin_local.dt.strftime("%Y-%m-%d")
    return day


def load_raw():
    frames = []
    for path in RAW_FILES:
        df = pd.read_csv(path, sep=";", encoding="utf-8-sig", low_memory=False)
        df.columns = df.columns.str.strip()
        n_before = len(df)
        df = df.drop_duplicates()
        n_dupes = n_before - len(df)
        print(f"  {os.path.basename(path)}: {n_before} rows"
              + (f" ({n_dupes} exact duplicates dropped)" if n_dupes else ""))
        frames.append(df)
    r = pd.concat(frames, ignore_index=True)

    r["name"] = r["BETROFFENE_ANLAGE"].astype(str).str.strip()
    r["day"] = _local_day(r)
    r["mwh"] = _num(r["GESAMTE_ARBEIT_MWH"]).abs()
    r = r.dropna(subset=["day", "mwh"])

    # substring match, not equality: tolerant of the corrupted "erhöhen" byte
    # in a handful of 2021-2026 rows, and of either source's exact wording
    richtung = r["RICHTUNG"].astype(str)
    is_increase = richtung.str.contains("erh", case=False, na=False)
    is_decrease = richtung.str.contains("reduzieren", case=False, na=False)
    r["inc"] = r["mwh"].where(is_increase, 0.0)
    r["dec"] = r["mwh"].where(is_decrease, 0.0)

    r.to_csv(COMBINED_FILE, sep=";", index=False, encoding="utf-8-sig")
    print(f"  -> {os.path.basename(COMBINED_FILE)}: {len(r)} combined rows "
          f"(gitignored cache, regenerate freely)")
    return r


def load_matches():
    m = pd.read_csv(MATCHES_FILE)
    m["name"] = m["betroffene_anlage"].astype(str).str.strip()
    return m.set_index("name")


def classify(row):
    """Return 'mapped' | 'boerse' | 'not_identified' for a matches row."""
    if pd.notna(row.get("lat")) and pd.notna(row.get("lon")):
        return "mapped"
    if str(row.get("entry_type", "")).strip() == "countertrade":
        return "boerse"
    return "not_identified"


def build():
    raw = load_raw()
    matches = load_matches()

    lookup = matches.to_dict("index")

    def info(name):
        return lookup.get(name)

    # ---- classify every raw call --------------------------------------------
    cats, lats, lons, fuels, etypes, matcheds, confs = [], [], [], [], [], [], []
    for name in raw["name"]:
        m = info(name)
        if m is None:
            # name present in raw data but absent from matches -> not identified
            cats.append("not_identified")
            lats.append(None); lons.append(None); fuels.append(None); etypes.append(None)
            matcheds.append(False); confs.append(None)
            continue
        cats.append(classify(m))
        lats.append(m.get("lat")); lons.append(m.get("lon"))
        fuels.append(m.get("fueltype")); etypes.append(m.get("entry_type"))
        matcheds.append(pd.notna(m.get("matched_id")))
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
        conf = first["confidence"]
        conf = str(conf).strip().lower() if pd.notna(conf) and str(conf).strip() else "none"
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

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("// Generated by build_data.py — do not edit by hand.\n")
        f.write("const REDISPATCH_DATA = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    # ---- report -------------------------------------------------------------
    size_kb = os.path.getsize(OUT_FILE) / 1024
    print(f"\nWrote {OUT_FILE} ({size_kb:.0f} KB)")
    print(f"Date range : {data['meta']['date_min']} .. {data['meta']['date_max']}")
    print(f"Plants     : {len(plants)} with coordinates")
    print(f"Total MWh  : {total_mwh:,.0f}")
    print(f"  mapped         {mapped_mwh:12,.0f}  ({mapped_mwh / total_mwh:6.1%})")
    print(f"  Börse          {boerse_mwh:12,.0f}  ({boerse_mwh / total_mwh:6.1%})")
    print(f"  not identified {ni_mwh:12,.0f}  ({ni_mwh / total_mwh:6.1%})")
    checksum = mapped_mwh + boerse_mwh + ni_mwh
    print(f"  sum check      {checksum:12,.0f}  ({checksum / total_mwh:6.1%})")


if __name__ == "__main__":
    build()
