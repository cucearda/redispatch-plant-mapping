"""Build the dashboard data files (dashboard/data_<LABEL>.js) from the raw
redispatch calls and a pipeline's plant-coordinate matches.

Reads:
  - The combined 2013-2026 redispatch dataset, via prep_data.prep_redispatch()
    (see ../prep_data.py — the raw-export combining, timezone correction, and
    direction-encoding cleanup live there now, shared with any pipeline).
  - results/<LABEL>/redispatch_plant_matches.csv   one row per plant key, with
                                           a lat/lon and some notion of match
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
  - mapped        -> the plant key it names has coordinates (drawn as a map
                     circle)
  - boerse        -> the raw plant name itself is "Börse" (the market
                     countertrade entry) -- checked directly on the raw name
                     so it classifies the same regardless of whether a given
                     pipeline's matches file carries that row at all
  - not_identified-> everything else without coordinates

## Multi-plant entries and the per-pipeline `explode`

A `BETROFFENE_ANLAGE` string is often a *bundle* naming several plants
dispatched together ("Boxberg, Jänschwalde, Lippendorf, Schkopau") — 382 of
the 1154 distinct raw names, carrying 10.3% of all redispatch volume. Whether
a pipeline's matches file is keyed by that whole string or by its individual
members is a per-pipeline fact, so each PIPELINES entry declares its own
`explode(raw_name) -> [key, ...]`:

  A1   keys the whole bundle string and places it at ONE centroid of its
       members, so it explodes to itself (`explode_identity`).
  AJ1  keys the exploded members (AJ1/redispatch_prep.py splits them at prep
       time), so `_aj1_segmenter` re-applies that pipeline's own segmentation
       rule and the call's volume is split EQUALLY across the members it
       names — each member circle gets 1/n of the bundle's MWh, instead of
       the bundle being drawn as one fictitious mid-point.
  J1   also keys exploded members, but with a different (looser) segmentation
       rule of its own — see the note on its PIPELINES entry.

Equal weighting is the only defensible split without per-member dispatch
volumes: the export reports one combined GESAMTE_ARBEIT_MWH for the bundle and
never says how it divided between the plants.

Only dependency is pandas (already used elsewhere in the project).

Run from anywhere:  python dashboard/build_data.py            # builds all pipelines
                     python dashboard/build_data.py --pipeline A1   # just one
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

# Resolve paths relative to the repo root (this file lives in dashboard/).
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # so `import prep_data` works when run from anywhere

import prep_data

BOERSE_NAME = "Börse"


# ── identity confidence (the "is this the right plant" axis) ────────────────
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


# ── coordinate confidence (the separate "is this the right LOCATION" axis) ──
# AJ1's coords.py scores the location independently of the identity (see its
# module docstring). The two thresholds below sit on that scorer's own
# structural boundaries rather than on round numbers:
#
#   high   >= 0.80  coord_basis == "consensus": >=2 independent datasets
#                   (OPSD / PyPSA / Wikipedia) put the plant within
#                   coords.AGREE_KM (5 km) of each other, and the coordinate
#                   is their centroid. The lowest score this basis can produce
#                   is 0.829 — a 3-source consensus whose centroid fell
#                   outside BNetzA's stated postcode, taking the 0.85 penalty.
#   medium >= 0.40  still a real plant-level point, but resting on ONE dataset
#                   (0.637-0.788), or on a disputed pick where the datasets
#                   disagreed by more than 5 km (0.486-0.552), or on a BNetzA
#                   postcode centroid (0.40).
#   low    >  0.00  no plant location at all — a town (0.25), grid-area or
#                   region (0.15) or Bundesland (0.05) centroid, which can sit
#                   tens of km from anything generating power.
#                   assemble_results.py warns never to read these as a plant
#                   pin, so the dashboard draws them with a dashed outline and
#                   lets you filter them out.
#   none   == 0.00  nothing located it; not on the map at all.
COORD_HIGH = 0.80
COORD_MEDIUM = 0.40


def _bucket_coord_score(v):
    if pd.isna(v):
        return "none"
    v = float(v)
    if v <= 0:
        return "none"
    if v >= COORD_HIGH:
        return "high"
    if v >= COORD_MEDIUM:
        return "medium"
    return "low"


# ── fuel normalisation ──────────────────────────────────────────────────────
# AJ1 carries a fuel label from up to five places (best_guess_fuel, wiki_fuel,
# and the three registries' own), each in its own vocabulary and language:
# "Natural gas" / "Natural Gas" / "Erdgas", "Hard coal" / "Steinkohle", plus
# free-text Wikipedia infobox values like "Braunkohle<br />(+ Erdgas für VGT)"
# or "Hausmüll und hausmüllähnlicher Gewerbemüll, Erdgas, Öl". Reduce all of
# them to one small canonical set so the dashboard can show a single label.
#
# Multi-fuel strings resolve to their PRIMARY fuel — the one named first,
# which is the convention both the Wikipedia infoboxes and the registries
# follow ("Gas und Steinkohle" -> Natural gas, "Kohle, leichtes Heizöl" ->
# Hard coal). Two patterns override position because they name a storage
# technology that qualifies whatever fuel is mentioned alongside it:
# "Hydro (pumped storage)" is pumped storage, not hydro, and anything
# "Batteriespeicher" is a battery.
FUEL_PATTERNS = [
    ("Lignite",     r"braunkohle|lignite|salzkohle"),
    ("Hard coal",   r"steinkohle|hard\s*coal|\bkohle\b|\bcoal\b"),
    ("Natural gas", r"erdgas|natural\s*gas|\bgas\b|gichtgas|kokereigas|blast\s*furnace"),
    ("Oil",         r"heiz[öo]l|mineral[öo]l|mineraloel|\b[öo]l\b|\boil\b|diesel"),
    ("Nuclear",     r"kernenergie|nuclear"),
    ("Hydro",       r"wasserkraft|wasser|hydro|laufwasser"),
    ("Wind",        r"windenergie|\bwind\b"),
    ("Solar",       r"solar|photovoltaik|\bpv\b"),
    ("Biomass",     r"biomasse|biomass|biogas|altholz|holzschnitzel"),
    ("Waste",       r"abfall|m[üu]ll|waste|ersatzbrennstoff|kl[äa]rschlamm"),
]
_FUEL_RE = [(name, re.compile(p, re.IGNORECASE)) for name, p in FUEL_PATTERNS]
# checked before the positional rules above — see the comment on FUEL_PATTERNS
_FUEL_OVERRIDE = [
    ("Pumped storage", re.compile(r"pumpspeicher|pumped\s*storage", re.IGNORECASE)),
    ("Battery",        re.compile(r"batterie|battery", re.IGNORECASE)),
]
# Wikipedia infobox noise: markup entities, and "ehemals X" ("formerly X")
# clauses naming a fuel the plant no longer burns.
_FUEL_MARKUP = re.compile(r"<[^>]*>|&nbsp;")
_FUEL_FORMER = re.compile(r"ehemals[^,;]*[,;]?", re.IGNORECASE)
# values that carry no fuel information at all
_FUEL_EMPTY = {"", "nan", "none", "conflict", "unknown"}
_FUEL_OTHER = ("other", "sonstige", "sonstiges")


def normalize_fuel(value):
    """One canonical fuel label for a raw fuel string, or None if the string
    carries no fuel information (blank, or a registry "conflict" marker)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s.lower() in _FUEL_EMPTY:
        return None
    if s.lower().startswith(_FUEL_OTHER):
        return "Other"
    s = _FUEL_FORMER.sub(" ", _FUEL_MARKUP.sub(" ", s))
    for name, rx in _FUEL_OVERRIDE:
        if rx.search(s):
            return name
    best = None
    for name, rx in _FUEL_RE:
        hit = rx.search(s)
        if hit and (best is None or hit.start() < best[0]):
            best = (hit.start(), name)
    return best[1] if best else "Other"


# The redispatch export's own PRIMAERENERGIEART is only a three-way class, so
# it is the fuel of last resort — informative for the renewable grid-area
# buckets no registry will ever resolve, but never a substitute for a real
# plant fuel.
COARSE_FUEL = {
    "Konventionell": "Conventional (unspecified)",
    "Erneuerbar":    "Renewable (unspecified)",
}


def aj1_best_fuel(row):
    """(fuel, basis) for one AJ1 row: the best-supported fuel label available
    and where it came from. Falls through the resolved identity's own fuel ->
    Wikipedia -> whichever registry produced a candidate -> the redispatch
    export's coarse class."""
    fuel = normalize_fuel(row.get("best_guess_fuel"))
    if fuel:
        # guess_basis already says how the identity — and with it the fuel —
        # was settled: unanimous / llm / wikipedia
        return fuel, str(row.get("guess_basis"))

    fuel = normalize_fuel(row.get("wiki_fuel"))
    if fuel:
        return fuel, "wikipedia"

    # Only fires on a partial run: assemble_results.py tolerates a missing
    # matches_llm.csv, leaving rows that DO have registry candidates with no
    # best guess. On a complete run every such row already resolved above.
    for reg in ("opsd", "psa", "bnetza"):
        fuel = normalize_fuel(row.get(f"{reg}_fuel"))
        if fuel:
            return fuel, f"candidate ({reg})"

    coarse = COARSE_FUEL.get(str(row.get("primaerenergieart")).strip())
    if coarse:
        return coarse, "redispatch class"
    return None, None


# ── per-pipeline adapters: pipeline schema -> the common shape ──────────────
# Every adapter returns a frame indexed by the pipeline's own plant key with:
#   lat, lon      the coordinate to draw (NaN -> not mapped)
#   fueltype      canonical fuel label, or NaN
#   fuel_basis    where that fuel came from, or NaN
#   entry_kind    plant | aggregate | unclear | countertrade
#   entry_label   the pipeline's own finer-grained label (tooltip only)
#   matched       a specific plant identity was resolved
#   confidence    identity confidence: high | medium | low | none
#   coord_conf    coordinate confidence: high | medium | low | none, or NaN
#                 for a pipeline that scores no such thing
#   coord_label   one-line explanation of coord_conf (tooltip), or NaN
COMMON_COLUMNS = ["lat", "lon", "fueltype", "fuel_basis", "entry_kind",
                  "entry_label", "matched", "confidence", "coord_conf",
                  "coord_label"]

# A1's finer entry_type / J1's aggregation -> the common 4-way entry_kind.
# "aggregate" is anything with no single physical location: a cluster, a
# substation, a grid-area or control-reserve bucket — and, for A1, a
# multi_plant bundle, which A1 draws as one circle at the mean of the members
# it names rather than as those several plants.
A1_ENTRY_KIND = {
    "individual": "plant", "foreign": "plant",
    "multi_plant": "aggregate", "cluster": "aggregate",
    "substation": "aggregate", "control_reserve": "aggregate",
    "regional_renewable": "aggregate", "emergency": "aggregate",
    "countertrade": "countertrade",
}
J1_ENTRY_KIND = {
    "single": "plant",
    "grid_area": "aggregate", "cluster": "aggregate", "site_pool": "aggregate",
}


def adapt_a1(m):
    """results/A1/redispatch_plant_matches.csv -> common schema."""
    m = m.copy()
    m["name"] = m["betroffene_anlage"].astype(str).str.strip()
    m["fueltype"] = m["fueltype"].map(normalize_fuel)
    m["fuel_basis"] = None
    m["entry_label"] = m["entry_type"]
    m["entry_kind"] = m["entry_type"].map(A1_ENTRY_KIND).fillna("unclear")
    m["matched"] = m["matched_id"].notna()
    m["confidence"] = m["confidence"].map(_bucket_confidence_text)
    m["coord_conf"] = None      # A1 scores no separate coordinate confidence
    m["coord_label"] = None
    return m.set_index("name")[COMMON_COLUMNS]


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
    m["fueltype"] = (m["opsd_class"].where(m["opsd_match"] != "none", m["psa_class"])
                     .map(normalize_fuel))
    m["fuel_basis"] = None
    m["entry_label"] = m["aggregation"]
    m["entry_kind"] = m["aggregation"].map(J1_ENTRY_KIND).fillna("unclear")
    m["matched"] = (m["opsd_match"] != "none") | (m["psa_match"] != "none")
    m["confidence"] = m["final_confidence"].map(_bucket_confidence_numeric)
    m["coord_conf"] = None      # J1's final_confidence mixes identity and
    m["coord_label"] = None     # location into one number; not comparable
    return m.set_index("name")[COMMON_COLUMNS]


# AJ1's classify.py leaves every non-aggregate entry as "unclear" by design and
# says so: the label is only meant to become "plant" once a later matching
# stage actually resolves it to one. assemble_results.py never does that
# relabelling, so it happens here — an entry whose identity was settled
# (guess_basis != none) is a plant; one that was not stays unclear.
AJ1_IDENTITY_CONF = {"unanimous": "high", "wikipedia": "medium", "none": "none"}

AJ1_COORD_LABEL = {
    "consensus":           "{n} sources agree (≤{spread} km apart)",
    "single_source":       "one source only: {sources}",
    "best_guess_disputed": "sources disagree by {spread} km — kept {source}",
    "postcode":            "BNetzA postcode centroid",
    "area_fallback":       "{precision}-level centroid ({source})",
    "none":                "no coordinate",
}


def _aj1_coord_label(row):
    tpl = AJ1_COORD_LABEL.get(str(row.get("coord_basis")), "")
    spread = row.get("coord_spread_km")
    return tpl.format(
        n=int(row.get("coord_n_sources") or 0),
        spread="?" if pd.isna(spread) else f"{float(spread):.1f}",
        sources=row.get("coord_sources") or "?",
        source=row.get("coord_source") or "?",
        precision=row.get("coord_precision") or "?",
    )


def adapt_aj1(m):
    """results/AJ1/redispatch_plant_matches.csv -> common schema.

    AJ1 is the only pipeline that scores the coordinate on its own axis
    (coords.py's coord_score / coord_basis / coord_precision), separately from
    whether the plant identity is right (guess_basis / llm_confidence) — so it
    is the only one that fills coord_conf, and index.html shows the
    coordinate-confidence filter only for datasets that carry it.
    """
    m = m.copy()
    m["name"] = m["plant"].astype(str).str.strip()

    fuels = m.apply(aj1_best_fuel, axis=1)
    m["fueltype"] = [f for f, _ in fuels]
    m["fuel_basis"] = [b for _, b in fuels]

    resolved = m["guess_basis"].astype(str).ne("none")
    m["entry_kind"] = m["category"].where(
        m["category"] != "unclear",
        resolved.map({True: "plant", False: "unclear"}))
    m["entry_label"] = "identity: " + m["guess_basis"].astype(str)
    m["matched"] = resolved
    m["confidence"] = [
        str(lc).strip().lower() if str(gb) == "llm" and pd.notna(lc)
        else AJ1_IDENTITY_CONF.get(str(gb), "none")
        for gb, lc in zip(m["guess_basis"], m["llm_confidence"])
    ]
    m["coord_conf"] = m["coord_score"].map(_bucket_coord_score)
    m["coord_label"] = m.apply(_aj1_coord_label, axis=1)
    return m.set_index("name")[COMMON_COLUMNS]


# ── bundle explosion: one raw call -> the plant key(s) it names ─────────────
def explode_identity(name):
    """For a pipeline keyed by the whole raw BETROFFENE_ANLAGE string."""
    return [name]


def _aj1_segmenter():
    """AJ1's own bundle segmentation, imported rather than reimplemented so
    the dashboard can never drift from the keys the pipeline actually wrote.
    (AJ1/redispatch_prep.py imports its sibling `paths`, hence the sys.path
    entry; it only builds path constants at import time and reads nothing.)"""
    aj1_dir = os.path.join(ROOT, "AJ1")
    if aj1_dir not in sys.path:
        sys.path.insert(0, aj1_dir)
    from redispatch_prep import bundle_segments, fix_mojibake

    def explode(name):
        segments = bundle_segments(name)
        return [fix_mojibake(s) for s in segments] if segments else [fix_mojibake(name)]

    return explode


# label -> how to find, read and key that pipeline's matches
PIPELINES = {
    "A1": {
        "path": os.path.join("results", "A1", "redispatch_plant_matches.csv"),
        "adapt": adapt_a1,
        "explode": explode_identity,
    },
    # J1 keys exploded member names too, but with its own looser comma rule
    # (J1/sources.py::_explode_plant_names), so re-using AJ1's segmenter here
    # is not guaranteed to reproduce J1's keys. Left un-exploded until that is
    # checked: the cost is that bundle raw names match no J1 key at all and
    # their volume (10.3% of the total) shows up under "not identified".
    "J1": {
        "path": os.path.join("results", "J1", "redispatch_plant_matches.csv"),
        "adapt": adapt_j1,
        "explode": explode_identity,
    },
    "AJ1": {
        "path": os.path.join("results", "AJ1", "redispatch_plant_matches.csv"),
        "adapt": adapt_aj1,
        "explode": _aj1_segmenter,   # factory, resolved in resolve_explode()
    },
}


def load_raw():
    return prep_data.prep_redispatch()


def load_matches(label):
    path = os.path.join(ROOT, PIPELINES[label]["path"])
    return PIPELINES[label]["adapt"](pd.read_csv(path))


def resolve_explode(label):
    """A PIPELINES entry may give either an exploder or a factory that builds
    one, so a pipeline-specific import only happens for the pipeline needing
    it."""
    fn = PIPELINES[label]["explode"]
    return fn() if fn is _aj1_segmenter else fn


def build(label, raw):
    out_file = os.path.join(HERE, f"data_{label}.js")
    matches = load_matches(label)
    lookup = matches.to_dict("index")
    explode = resolve_explode(label)

    # ---- one raw call -> the plant key(s) it names, each with its weight ----
    # Cached per distinct raw name: 1154 distinct names across 124k calls.
    members_cache = {}

    def members(name):
        if name not in members_cache:
            keys = explode(name)
            members_cache[name] = [(k, 1.0 / len(keys)) for k in keys]
        return members_cache[name]

    # ---- classify + attribute every raw call --------------------------------
    # A bundle call contributes one weighted row per member it names, so its
    # volume is split equally instead of landing on a single mid-point. Börse
    # is checked on the raw name (and never exploded) so it classifies the
    # same in every pipeline.
    per_plant = {}          # plant key -> {day: [inc, dec]}
    unmapped = {"boerse": {}, "not_identified": {}}
    n_split = 0

    for name, day, inc, dec, mwh in zip(raw["name"], raw["day"], raw["inc"],
                                        raw["dec"], raw["mwh"]):
        if name == BOERSE_NAME:
            unmapped["boerse"][day] = unmapped["boerse"].get(day, 0.0) + mwh
            continue
        parts = members(name)
        if len(parts) > 1:
            n_split += 1
        for key, weight in parts:
            info = lookup.get(key)
            if info is None or pd.isna(info.get("lat")) or pd.isna(info.get("lon")):
                unmapped["not_identified"][day] = (
                    unmapped["not_identified"].get(day, 0.0) + mwh * weight)
                continue
            series = per_plant.setdefault(key, {})
            cur = series.get(day)
            if cur is None:
                series[day] = [inc * weight, dec * weight]
            else:
                cur[0] += inc * weight
                cur[1] += dec * weight

    # ---- mapped plants -------------------------------------------------------
    plants = []
    for key, series in per_plant.items():
        info = lookup[key]

        def txt(col, info=info):
            v = info.get(col)
            return None if v is None or pd.isna(v) or str(v).strip() == "" else str(v)

        plants.append({
            "name": key,
            "lat": round(float(info["lat"]), 5),
            "lon": round(float(info["lon"]), 5),
            "fueltype": txt("fueltype"),
            "fuel_basis": txt("fuel_basis"),
            "entry_kind": txt("entry_kind") or "unclear",
            "entry_label": txt("entry_label"),
            "matched": bool(info.get("matched")),
            "confidence": txt("confidence") or "none",
            "coord_conf": txt("coord_conf"),
            "coord_label": txt("coord_label"),
            "series": {d: [round(v[0], 3), round(v[1], 3)] for d, v in series.items()},
        })
    plants.sort(key=lambda p: p["name"])

    unmapped = {k: {d: round(v, 3) for d, v in sorted(s.items())}
                for k, s in unmapped.items()}

    # ---- meta + sanity totals -----------------------------------------------
    total_mwh = float(raw["mwh"].sum())
    mapped_mwh = sum(v[0] + v[1] for p in plants for v in p["series"].values())
    boerse_mwh = sum(unmapped["boerse"].values())
    ni_mwh = sum(unmapped["not_identified"].values())
    has_coord_conf = bool(matches["coord_conf"].notna().any())

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
            # drive which filter groups index.html shows for this dataset
            "has_coord_conf": has_coord_conf,
            "splits_bundles": bool(n_split),
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
    if n_split:
        print(f"[{label}] Bundles    : {n_split:,} calls split equally across the "
              f"plants they name")
    print(f"[{label}] Total MWh  : {total_mwh:,.0f}")
    print(f"[{label}]   mapped         {mapped_mwh:12,.0f}  ({mapped_mwh / total_mwh:6.1%})")
    print(f"[{label}]   Börse          {boerse_mwh:12,.0f}  ({boerse_mwh / total_mwh:6.1%})")
    print(f"[{label}]   not identified {ni_mwh:12,.0f}  ({ni_mwh / total_mwh:6.1%})")
    checksum = mapped_mwh + boerse_mwh + ni_mwh
    print(f"[{label}]   sum check      {checksum:12,.0f}  ({checksum / total_mwh:6.1%})")

    if has_coord_conf:
        vol = {}
        for p in plants:
            slot = vol.setdefault(p["coord_conf"] or "none", [0, 0.0])
            slot[0] += 1
            slot[1] += sum(v[0] + v[1] for v in p["series"].values())
        print(f"[{label}] Coordinate confidence, mapped plants only "
              f"(high >= {COORD_HIGH}, medium >= {COORD_MEDIUM}):")
        for bucket in ("high", "medium", "low", "none"):
            if bucket in vol:
                n, v = vol[bucket]
                print(f"[{label}]   {bucket:<7}{n:5d} plants {v:14,.0f} MWh "
                      f"({v / total_mwh:6.1%} of all redispatch)")


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
