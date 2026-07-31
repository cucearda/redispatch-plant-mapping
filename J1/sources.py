"""Loading and harmonising the three inputs: the redispatch export, the OPSD
conventional-plant list, and the powerplantmatching (PSA) fleet.

Also owns the energy-class mapping that lets a match be rejected when the
redispatch entry and the candidate disagree on fuel category.

Recreated from redispatch-analysis/matcher/sources.py. Only load_redispatch()
is adapted (this repo's prep_data.py already combines the two raw redispatch
exports into one timezone-corrected, de-duplicated file — see config.py); the
energy-class mapping and load_opsd()/load_psa() are unchanged in logic.
"""

import os

import pandas as pd

from config import (REDISPATCH_FILE, REDISPATCH_ENC, OPSD_PATH, OPSD_ENC,
                     PSA_PATH, PSA_MIN_CAPACITY)
from normalize import clean_redispatch_name, fix_mojibake, norm, norm_tokens


# ---------------------------------------------------------------------------
def _explode_plant_names(series):
    out = series.dropna().astype(str).str.split(",")
    out = out.explode().map(lambda s: fix_mojibake(s.strip()))
    out = out[(out.str.len() >= 2) & (~out.str.fullmatch(r"\d+"))]
    return out[~out.isin({"Börse", "Börse"})]


def load_redispatch():
    # The prepped file already combines both raw exports (timezone-corrected,
    # de-duplicated) and carries their original BETROFFENE_ANLAGE un-exploded
    # (multi-plant entries like "Jänschwalde, Schkopau" stay one string) plus
    # an already-computed Europe/Berlin calendar 'day' column.
    df = pd.read_csv(REDISPATCH_FILE, delimiter=";", encoding=REDISPATCH_ENC)
    df.columns = [str(c).lstrip("﻿").strip().rstrip(",").strip()
                  for c in df.columns]
    df = df[df["GRUND_DER_MASSNAHME"] != "Probefahrt"]
    names = _explode_plant_names(df["BETROFFENE_ANLAGE"])
    energy = df["PRIMAERENERGIEART"].reindex(names.index)
    energy = energy.astype(str).str.replace(",", "", regex=False).str.strip()
    # informational parity with the original per-file source_map column;
    # derived from the prepped file's own precomputed 'day' column rather
    # than re-parsing BEGINN_DATUM
    day = df["day"].reindex(names.index)
    source_tag = day.map(lambda d: "2013-2020" if str(d) < "2021-01-01" else "2021-2026")
    sub = pd.DataFrame({
        "plant":  names.values,
        "energy": energy.values,
        "source": source_tag.values,
    })
    print(f"  {os.path.basename(REDISPATCH_FILE)}: "
          f"{sub['plant'].nunique()} unique plants ({len(sub)} rows)")
    return sub


# ---------------------------------------------------------------------------
# Maps source-specific labels -> {konventionell, erneuerbar, sonstiges}
OPSD_FUEL_TO_CLASS = {
    "natural gas": "konventionell", "hard coal": "konventionell",
    "lignite": "konventionell", "oil": "konventionell",
    "nuclear": "konventionell", "waste": "konventionell",
    "other fuels": "konventionell", "other fossil fuels": "konventionell",
    "mixed fossil fuels": "konventionell",
    "non-renewable waste": "konventionell",
    "biomass": "erneuerbar", "biogas": "erneuerbar",
    "geothermal": "erneuerbar", "solar": "erneuerbar",
    "wind": "erneuerbar",
    "renewable waste": "erneuerbar",
    "hydro": "sonstiges",   # default; tech override below splits pumped from run-of-river
}
OPSD_TECH_OVERRIDE = {
    "pumped storage": "sonstiges",
    "reservoir": "sonstiges",
    "storage technologies": "sonstiges",
    "run-of-river": "erneuerbar",
}

PSA_FUEL_TO_CLASS = {
    "natural gas": "konventionell", "hard coal": "konventionell",
    "lignite": "konventionell", "oil": "konventionell",
    "nuclear": "konventionell", "waste": "konventionell",
    "other": "konventionell", "other fossil fuels": "konventionell",
    "mixed fossil fuels": "konventionell",
    "solar": "erneuerbar", "wind": "erneuerbar",
    "biogas": "erneuerbar", "solid biomass": "erneuerbar",
    "biomass": "erneuerbar",
    "geothermal": "erneuerbar",
    "hydro": "sonstiges", "battery": "sonstiges",
}

def _safe_lower(s):
    if s is None:
        return ""
    if isinstance(s, float) and pd.isna(s):
        return ""
    return str(s).strip().lower()


def opsd_energy_class(source, tech):
    src = _safe_lower(source)
    t   = _safe_lower(tech)
    if t in OPSD_TECH_OVERRIDE:
        return OPSD_TECH_OVERRIDE[t]
    return OPSD_FUEL_TO_CLASS.get(src)


def psa_energy_class(fueltype, technology):
    f = _safe_lower(fueltype)
    t = _safe_lower(technology)
    if f == "hydro" and t in ("run-of-river",):
        return "erneuerbar"
    return PSA_FUEL_TO_CLASS.get(f)


def energy_ok(plant_class, source_class):
    """True if energy types are compatible. None means 'no info, allow it'."""
    if not plant_class or not source_class:
        return None
    return plant_class.lower() == source_class.lower()


# ---------------------------------------------------------------------------
def load_opsd():
    print("Loading OPSD...")
    df = pd.read_csv(OPSD_PATH, encoding=OPSD_ENC)
    for c in ("name_bnetza", "city", "state"):
        if c in df.columns:
            df[c] = df[c].map(fix_mojibake)
    df = df.dropna(subset=["lat", "lon"]).copy()
    df["class"] = [opsd_energy_class(s, t)
                   for s, t in zip(df["energy_source"], df["technology"])]
    df["key_full"]  = df["name_bnetza"].map(lambda s: norm(s or ""))
    df["key_city"]  = df["city"].map(lambda s: norm(s or ""))
    df["key_clean"] = df["name_bnetza"].map(
        lambda s: norm(clean_redispatch_name(str(s) if s else "")[0]))
    print(f"  OPSD with coords: {len(df)} entries")
    return df


# ---------------------------------------------------------------------------
def load_psa():
    print("Loading PSA...")
    df = pd.read_csv(PSA_PATH, low_memory=False)
    df = df[df["Country"].isin(["Germany", "Luxembourg", "Austria"])].copy()
    df = df.dropna(subset=["lat", "lon", "Name"]).copy()
    before = len(df)
    df = df[df["Capacity"].fillna(0) >= PSA_MIN_CAPACITY].copy()
    print(f"  PSA capacity filter (>= {PSA_MIN_CAPACITY} MW): "
          f"{before} -> {len(df)} entries")
    df["class"] = [psa_energy_class(f, t)
                   for f, t in zip(df["Fueltype"], df["Technology"])]
    df["key_full"]  = df["Name"].map(lambda s: norm(s or ""))
    df["key_clean"] = df["Name"].map(
        lambda s: norm(clean_redispatch_name(str(s) if s else "")[0]))
    df = df.reset_index(drop=True)
    unknown = df[df["class"].isna()]["Fueltype"].value_counts().to_dict()
    if unknown:
        print(f"  [warn] PSA unmapped fueltypes: {unknown}")
    print(f"  PSA DE+AT+LU usable: {len(df)} entries "
          f"({df['class'].value_counts(dropna=False).to_dict()})")
    return df
