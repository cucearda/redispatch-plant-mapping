"""
redispatch_prep.py — matching pipeline steps 0-1.

Step 0 — reduce Redispatch_Daten.csv to the distinct BETROFFENE_ANLAGE keys, with
         per-name attributes: modal PRIMAERENERGIEART, max(MAXIMALE_LEISTUNG_MW)
         (the capacity lower bound), instructing TSOs, event count.
Step 1 — rule filter: classify each key into the 3-way router's entry_type —
         individual · cluster · control_reserve · substation · regional_renewable
         · countertrade · emergency · foreign.

Output: data/redispatch_entries.csv (one row per distinct name, the loop grain
for everything downstream). Only `individual` + `cluster` go to matching; the rest
are labelled and kept so the lookup accounts for 100% of the redispatch names.
"""

import os
import re

import pandas as pd

REDISPATCH = "data/Redispatch_Daten.csv"
OUT        = "data/redispatch_entries.csv"

# Classification patterns, checked in precedence order (first match wins).
# Order matters: countertrade/emergency/foreign before the grid aggregates,
# and Cluster (matchable) before CR_/UW/EE so "SHN Cluster …" isn't stolen.
CATEGORY_RES = [
    ("countertrade",       re.compile(r"^\s*Börse", re.I)),                  # EPEX Gegengeschäft — no plant
    ("emergency",          re.compile(r"Notfall", re.I)),                    # Notfall-RD virtual entry
    ("foreign",            re.compile(r"Vianden|K[üu]htai|illwerke|Vorarlberger|Ilwerke", re.I)),  # AT/LU pumped storage
    ("cluster",            re.compile(r"\bCluster\b", re.I)),                # DSO renewable cluster → set of plants
    ("control_reserve",    re.compile(r"_CR_|_CR\b", re.I)),                 # control-reserve grid node
    ("substation",         re.compile(r"\bUW\b|Umspannwerk", re.I)),        # transformer station node
    ("regional_renewable", re.compile(r"\bEE\b", re.I)),                     # "EE Bayern" — whole-state renewables
]


# Technology spelled out in the name (mostly the DSO CR_ buckets: `_CR_WIND` etc).
# Finer than PRIMAERENERGIEART — Wind vs Solar are both "Erneuerbar" there.
# \b treats "_" as a word char, so "_CR_WIND" has no boundary before WIND — use a
# letter-boundary lookaround (matches WIND delimited by _/space/digits, not WINDPARK).
_L = r"[A-Za-zÄÖÜäöüß]"
TECH_RES = [
    ("Wind",             re.compile(rf"(?<!{_L})WIND(?!{_L})", re.I)),
    ("Solar",            re.compile(rf"PHOTOVOLTAIK|(?<!{_L})PV(?!{_L})", re.I)),
    ("Other renewable",  re.compile(r"SONSTIGE_EE", re.I)),
    ("Conventional",     re.compile(r"KONVENTIONELL", re.I)),
]
# Offshore-wind substations: OWP = Offshore-Windpark (deterministic from the name); the plain
# `UW …` converter stations below are the onshore landing points of offshore HVDC links
# (Büttel=SylWin, Diele/Garrel=DolWin, Dörpen-West, Emden-Ost) — knowledge, hardcoded once.
_OWP = re.compile(r"\bOWP\b", re.I)
_UW  = re.compile(r"\bUW\b|Umspannwerk", re.I)
_OFFSHORE_CONVERTER = re.compile(r"büttel|buettel|diele|dörpen|dorpen|emden|garrel|baltic", re.I)


def classify(name: str) -> str:
    for label, rx in CATEGORY_RES:
        if rx.search(name):
            return label
    return "individual"


def name_technology(name: str) -> str:
    for label, rx in TECH_RES:
        if rx.search(name):
            return label
    if _OWP.search(name):                                       # Offshore-Windpark substation
        return "Wind"
    if _UW.search(name) and _OFFSHORE_CONVERTER.search(name):   # offshore HVDC converter station
        return "Wind"
    return ""


def main(redispatch_file: str = REDISPATCH) -> None:
    r = pd.read_csv(redispatch_file, sep=";", encoding="utf-8-sig", low_memory=False)
    r.columns = r.columns.str.strip()
    r["BETROFFENE_ANLAGE"] = r["BETROFFENE_ANLAGE"].astype(str).str.strip()
    r = r[r["BETROFFENE_ANLAGE"].ne("") & r["BETROFFENE_ANLAGE"].ne("nan")]
    r["MAXIMALE_LEISTUNG_MW"] = pd.to_numeric(
        r["MAXIMALE_LEISTUNG_MW"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce")

    rows = []
    for name, g in r.groupby("BETROFFENE_ANLAGE", sort=False):
        energies = g["PRIMAERENERGIEART"].dropna().astype(str).str.strip()
        energies = energies[energies.ne("")]
        modal = energies.mode().iloc[0] if len(energies) else ""
        tsos = sorted(g["ANWEISENDER_UENB"].dropna().astype(str).str.strip().unique())
        rows.append({
            "betroffene_anlage": name,
            "primaerenergieart": modal,
            "energy_conflict":   energies.nunique() > 1,   # flag inconsistent labelling
            "max_dispatched_mw": g["MAXIMALE_LEISTUNG_MW"].max(),
            "tsos":              ",".join(tsos),
            "n_events":          len(g),
            "entry_type":        classify(name),
            "name_technology":   name_technology(name),   # finer fuel where the name spells it out
        })

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"→ {OUT}: {len(out)} distinct entries\n")
    print(out["entry_type"].value_counts().to_string())
    matchable = out["entry_type"].isin(["individual", "cluster"]).sum()
    print(f"\nmatchable (individual + cluster): {matchable}")
    print(f"energy-type conflicts flagged:    {int(out['energy_conflict'].sum())}")

    # self-check
    assert out["betroffene_anlage"].is_unique, "duplicate keys"
    assert out["entry_type"].notna().all(), "unclassified entry"
    assert out.loc[out["entry_type"] == "individual", "max_dispatched_mw"].notna().any(), \
        "no dispatched-power values parsed"
    print("self-check OK")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
