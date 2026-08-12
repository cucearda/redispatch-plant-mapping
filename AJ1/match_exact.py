"""
match_exact.py — AJ1 pipeline, step 2: exact-name matching for the 555
`unclear`-classified entries against three registries, checked separately
(no merged index), in priority order OPSD -> PyPSA -> BNetzA. BNetzA is
checked last: it has no coordinates at all, and A1's own real run
(results/A1/matches_exact.csv) shows its BNetzA fallback has never
independently resolved a single entry there (id_source is 80 "index", 0
"bnetza") — it's the weakest signal of the three, though AJ1's own results
show it isn't completely inert. All three registries are always attempted
regardless of earlier hits, so one entry can carry a match in more than one
registry — reconciling them into a single winner is a later step.

Each registry match is resolved by a quality-aware cascade of "attempts",
tried strictly in priority order, stopping as soon as one resolves cleanly:
  1. `light`      — norm_light key (A1-style: lowercase, strip TSO prefix,
                     drop parens/punctuation, no per-token filtering) against
                     the registry's own name column.
  2. `heavy_ae`    — clean_heavy key (J1-style aggressive cleaning, ae-fold)
                     against the same column.
  3. `heavy_bare`  — clean_heavy key with bare-diacritic folding, against
                     the same column.
  4. `block`       — OPSD only: entry's norm_light key against a light-only
                     index over OPSD's own `block_bnetza` column.
  5. `name_block`  — OPSD only: entry's norm_light key against a light-only
                     index over `name_bnetza + " " + block_bnetza`.
Attempts 4-5 exist because `clean_heavy`'s per-token filter drops every
digit/single-letter token wherever it occurs, including block numbers
("Ingolstadt 3"/"Ingolstadt 4" both heavy-clean to "ingolstadt") — the only
string form that keeps a block letter/digit through cleaning is norm_light
(attempt 1, already tried first), and OPSD alone has a second field
(`block_bnetza`) to combine it with; PyPSA/BNetzA have no equivalent field,
so they only ever get attempts 1-3.

Each attempt's hits are first narrowed to candidates compatible with the
entry itself (`_class_filter`/`_date_filter`, soft — unknown class/date on
either side never excludes a candidate), then resolved:
  - OPSD/PyPSA (coordinates): resolved if every survivor sits within a tight
    geographic cluster (`TIGHT_CLUSTER_DEG`, one site listed turbine-by-
    turbine) -> the *whole cluster* is returned as candidates (not narrowed
    to one row).
  - BNetzA (no coordinates): postal code stands in for "close" -> resolved
    if every survivor shares the same Postleitzahl -> the whole set is
    returned.
An attempt "resolves cleanly" if its returned candidates all share the same
exact fuel string too; if they don't, the candidates are still returned
(nothing is discarded for disagreeing on fuel) but tagged `fuel="conflict"`,
which ranks below a clean resolution but above no candidates at all. The
cascade stops at the first attempt that resolves cleanly; otherwise it keeps
whichever attempt reached the best rank (clean > conflict > none), earlier
attempts winning ties.

Output: AJ1/temp_AJ1/redispatch_entries.csv is read-only here (never
rewritten). Writes AJ1/temp_AJ1/matches_exact.csv — one row per plant name
with >=1 registry hit (a "conflict" still counts as a hit), carrying
`plant` and per registry `{reg}_match_tier` (which attempt above produced
the kept result, or "none"), `{reg}_names`/`{reg}_ids` (every candidate,
" | "-joined, same order), `{reg}_fuel` (the agreed fuel string, or
"conflict"). The three normalized string variants themselves are not
repeated here — see normalize_preview.py for a standalone, matching-free
dump of those.
"""

import os
from collections import defaultdict

import pandas as pd

from paths import INPUT_DIR, TEMP_DIR
from normalize import norm_light, clean_heavy, clean_heavy_bare

ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")
OUT     = os.path.join(TEMP_DIR, "matches_exact.csv")
BNETZA  = os.path.join(INPUT_DIR, "bnetza_kraftwerkliste_clean.csv")
OPSD    = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_DE.csv")
PSA     = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")

# Same threshold J1 uses for "these same-name rows are one site listed
# turbine-by-turbine, not genuinely different places" (~14-22 km).
TIGHT_CLUSTER_DEG = 0.2


# ---------------------------------------------------------------------------
# Energy-class mappings: redispatch's own primaerenergieart is already
# Konventionell/Erneuerbar/Sonstiges. OPSD/PyPSA mappings ported verbatim
# from J1/sources.py (checked against the real OPSD energy_source/technology
# vocabulary during planning). BNetzA's Energietraeger has no existing
# mapping anywhere in this repo -- new, grounded in its actual vocabulary.
OPSD_FUEL_TO_CLASS = {
    "natural gas": "konventionell", "hard coal": "konventionell",
    "lignite": "konventionell", "oil": "konventionell",
    "nuclear": "konventionell", "waste": "konventionell",
    "other fuels": "konventionell", "other fossil fuels": "konventionell",
    "mixed fossil fuels": "konventionell",
    "non-renewable waste": "konventionell",
    "biomass": "erneuerbar", "biogas": "erneuerbar",
    "biomass and biogas": "erneuerbar",  # OPSD's actual combined label (46 rows)
    "geothermal": "erneuerbar", "solar": "erneuerbar",
    "wind": "erneuerbar",
    "renewable waste": "erneuerbar",
    "hydro": "sonstiges",
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
# BNetzA's own Energietraeger vocabulary -> class. Waerme and Wasserstoff
# are genuine judgment calls (see plan): Waerme treated as konventionell
# (likely heat-only/CHP), Wasserstoff as sonstiges (only 2 rows, no fossil-
# vs-renewable pathway distinguishable from the field alone).
BNETZA_FUEL_TO_CLASS = {
    "erdgas": "konventionell", "steinkohle": "konventionell",
    "braunkohle": "konventionell", "mineraloelprodukte": "konventionell",
    "kernenergie": "konventionell", "grubengas": "konventionell",
    "sonstige energietraeger (nicht erneuerbar)": "konventionell",
    "abfall": "konventionell",
    "biomasse": "erneuerbar", "solarestrahlungsenergie": "erneuerbar",
    "wind (onshore)": "erneuerbar", "geothermie": "erneuerbar",
    "wasser": "sonstiges", "pumpspeicher": "sonstiges",
    "batteriespeicher": "sonstiges",
    "waerme": "konventionell",
    "wasserstoff": "sonstiges",
}


def _safe_lower(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip().lower()


def opsd_class(source, tech):
    t = _safe_lower(tech)
    if t in OPSD_TECH_OVERRIDE:
        return OPSD_TECH_OVERRIDE[t]
    return OPSD_FUEL_TO_CLASS.get(_safe_lower(source))


def psa_class(fueltype, technology):
    f = _safe_lower(fueltype)
    t = _safe_lower(technology)
    if f == "hydro" and t == "run-of-river":
        return "erneuerbar"
    return PSA_FUEL_TO_CLASS.get(f)


def bnetza_class(energietraeger):
    return BNETZA_FUEL_TO_CLASS.get(_safe_lower(energietraeger))


def _entry_earliest_year(sources):
    """First calendar year the entry could possibly have been dispatched in,
    from redispatch_entries.csv's own `sources` column ("2013-2020",
    "2021-2026", or both, comma-joined). None if unknown."""
    s = str(sources)
    if "2013-2020" in s:
        return 2013
    if "2021-2026" in s:
        return 2021
    return None


# ---------------------------------------------------------------------------
# Name-column indices: three tiers, tried in order (1) the light-normalized
# string, (2) ae-expansion folding (Jänschwalde -> jaenschwalde), (3) bare
# diacritic-strip folding (Jänschwalde -> janschwalde) -- added because some
# registry names (e.g. PyPSA's "Janschwalde") drop the umlaut entirely
# rather than transliterating it.
TIERS = ("light", "heavy_ae", "heavy_bare")


def _build_keys(names):
    """tier name -> {key: [row positions]}, for a name Series."""
    idx = {tier: defaultdict(list) for tier in TIERS}
    for pos, name in enumerate(names):
        if not isinstance(name, str) or not name.strip():
            continue
        for tier, fn in zip(TIERS, (norm_light, clean_heavy, clean_heavy_bare)):
            k = fn(name)
            if k:
                idx[tier][k].append(pos)
    return idx


def _build_light_index(names):
    """{norm_light(name): [row positions]}, for the OPSD-only block-field
    fallback indices -- light only, since heavy cleaning's per-token filter
    would strip the very block letters/digits these indices exist to keep."""
    idx = defaultdict(list)
    for pos, name in enumerate(names):
        if not isinstance(name, str) or not name.strip():
            continue
        k = norm_light(name)
        if k:
            idx[k].append(pos)
    return idx


def _entry_keys(name):
    return {"light": norm_light(name), "heavy_ae": clean_heavy(name),
            "heavy_bare": clean_heavy_bare(name)}


def _agree(positions, values):
    """True if every candidate position shares the same non-blank value."""
    seen = {_safe_lower(values[p]) for p in positions}
    return len(seen) == 1 and "" not in seen


def _class_filter(positions, classes, entry_class):
    """Keep only candidates whose class is unknown, or agrees with the
    redispatch entry's own class (soft -- unknown class on either side never
    excludes a candidate)."""
    if not entry_class:
        return positions
    ec = _safe_lower(entry_class)
    return [p for p in positions
            if not classes[p] or _safe_lower(classes[p]) == ec]


def _date_filter(positions, out_years, earliest_year):
    """Keep only candidates with no recorded shutdown/retirement year, or
    one on/after the entry's earliest possible dispatch year -- a plant that
    had already stopped operating before the redispatch entry could exist
    can't be what it refers to."""
    if earliest_year is None or out_years is None:
        return positions
    kept = []
    for p in positions:
        y = out_years[p]
        if y is None or (isinstance(y, float) and pd.isna(y)) or y >= earliest_year:
            kept.append(p)
    return kept


def _resolve_candidates(positions, classes, entry_class, out_years, earliest_year,
                         has_coords, lats=None, lons=None, fuels=None, postcodes=None):
    """Returns (candidate_positions, fuel_status).

    First narrows the pool to candidates compatible with the entry's own
    fuel class and operational window (soft, see _class_filter/_date_filter),
    then checks whether what's left forms one coherent site: geographically
    tight (OPSD/PyPSA) or postal-code-agreeing (BNetzA, no coordinates).
    If not, there's no viable candidate at all -- ([], None), not ambiguity.
    If so, *every* surviving candidate is returned (never narrowed to one),
    tagged with fuel_status: the shared exact fuel string if they all agree,
    else the literal string "conflict" -- disagreement is reported, not
    grounds for dropping candidates."""
    positions = _class_filter(positions, classes, entry_class)
    positions = _date_filter(positions, out_years, earliest_year)

    if not positions:
        return [], None
    if len(positions) == 1:
        return positions, fuels[positions[0]]

    if has_coords:
        lat_vals = [lats[p] for p in positions]
        lon_vals = [lons[p] for p in positions]
        close = positions if (max(lat_vals) - min(lat_vals) < TIGHT_CLUSTER_DEG
                               and max(lon_vals) - min(lon_vals) < TIGHT_CLUSTER_DEG) else None
    else:
        close = positions if _agree(positions, postcodes) else None

    if close is None:
        return [], None

    fuel_status = fuels[close[0]] if _agree(close, fuels) else "conflict"
    return close, fuel_status


def _rank(candidates, fuel_status):
    """clean(2) > conflict(1) > none(0) -- the cascade stops at the first
    attempt reaching clean, and otherwise keeps whichever attempt reaches
    the best rank, earlier attempts winning ties."""
    if not candidates:
        return 0
    return 1 if fuel_status == "conflict" else 2


# ---------------------------------------------------------------------------
def main(entries_file: str = ENTRIES) -> None:
    df = pd.read_csv(entries_file)

    bn = pd.read_csv(BNETZA)
    bn["class"] = bn["Energietraeger"].map(bnetza_class)
    bn_idx = _build_keys(bn["Anzeigename"])

    opsd = pd.read_csv(OPSD, encoding="utf-8-sig")
    opsd["class"] = [opsd_class(s, t) for s, t in
                     zip(opsd["energy_source"], opsd["technology"])]
    opsd_idx = _build_keys(opsd["name_bnetza"])
    opsd_block_idx = _build_light_index(opsd["block_bnetza"])
    has_block = (opsd["block_bnetza"].notna()
                 & opsd["block_bnetza"].astype(str).str.strip().ne(""))
    opsd_name_block = pd.Series([None] * len(opsd), index=opsd.index, dtype=object)
    opsd_name_block[has_block] = (opsd.loc[has_block, "name_bnetza"].astype(str)
                                   + " " + opsd.loc[has_block, "block_bnetza"].astype(str))
    opsd_name_block_idx = _build_light_index(opsd_name_block)

    psa = pd.read_csv(PSA, low_memory=False)
    psa["class"] = [psa_class(f, t) for f, t in
                    zip(psa["Fueltype"], psa["Technology"])]
    psa_idx = _build_keys(psa["Name"])

    # Priority order OPSD -> PyPSA -> BNetzA (BNetzA last: no coordinates,
    # and the weakest independent signal of the three -- see module docstring).
    # date_col is each registry's own "year it went/goes out of operation"
    # field, where one exists: OPSD's `shutdown`, PyPSA's `DateOut`. BNetzA's
    # cleaned file carries no such field -- date_col=None skips that filter.
    # extra_idx: OPSD-only block-field fallback indices, appended to the
    # standard light/heavy_ae/heavy_bare cascade (see module docstring).
    registries = {
        "opsd":   dict(df_=opsd, idx=opsd_idx,
                       id_col="id", name_col="name_bnetza",
                       has_coords=True, fuel_col="energy_source",
                       date_col="shutdown",
                       extra_idx=[("block", opsd_block_idx),
                                  ("name_block", opsd_name_block_idx)]),
        "psa":    dict(df_=psa, idx=psa_idx,
                       id_col="id", name_col="Name",
                       has_coords=True, fuel_col="Fueltype",
                       date_col="DateOut", extra_idx=[]),
        "bnetza": dict(df_=bn, idx=bn_idx,
                       id_col="mastr_id", name_col="Anzeigename",
                       has_coords=False, fuel_col="Energietraeger",
                       postcode_col="Postleitzahl", date_col=None, extra_idx=[]),
    }

    unclear_mask = df["category"] == "unclear"
    all_tiers = ("light", "heavy_ae", "heavy_bare", "block", "name_block", "none")
    tier_counts = {reg: {t: 0 for t in all_tiers} for reg in registries}
    conflict_count = {reg: 0 for reg in registries}
    n_any = 0
    results = []

    for i in df.index[unclear_mask]:
        name = df.at[i, "plant"]
        entry_class = df.at[i, "primaerenergieart"]
        earliest_year = _entry_earliest_year(df.at[i, "sources"])
        keys = _entry_keys(name)

        row = {"plant": name}
        any_hit_this_entry = False
        for reg, r in registries.items():
            rdf = r["df_"]
            classes = rdf["class"].values
            fuels = rdf[r["fuel_col"]].values
            out_years = rdf[r["date_col"]].values if r["date_col"] else None
            lats = rdf["lat"].values if r["has_coords"] else None
            lons = rdf["lon"].values if r["has_coords"] else None
            postcodes = rdf[r["postcode_col"]].values if not r["has_coords"] else None

            attempts = [(t, r["idx"][t], keys[t]) for t in TIERS]
            attempts += [(tname, tidx, keys["light"]) for tname, tidx in r["extra_idx"]]

            best_candidates, best_fuel, best_tier, best_rank = [], None, "none", 0
            for tier_name, idx_, key in attempts:
                hits = idx_.get(key, [])
                if not hits:
                    continue
                candidates, fuel_status = _resolve_candidates(
                    hits, classes, entry_class, out_years, earliest_year,
                    r["has_coords"], lats=lats, lons=lons,
                    fuels=fuels, postcodes=postcodes)
                rank = _rank(candidates, fuel_status)
                if rank > best_rank:
                    best_candidates, best_fuel, best_tier, best_rank = \
                        candidates, fuel_status, tier_name, rank
                if rank == 2:
                    break

            tier_counts[reg][best_tier] += 1
            if best_candidates:
                row[f"{reg}_match_tier"] = best_tier
                row[f"{reg}_names"] = " | ".join(
                    str(rdf.iloc[p][r["name_col"]]) for p in best_candidates)
                row[f"{reg}_ids"] = " | ".join(
                    str(rdf.iloc[p][r["id_col"]]) for p in best_candidates)
                row[f"{reg}_fuel"] = best_fuel
                if best_fuel == "conflict":
                    conflict_count[reg] += 1
                any_hit_this_entry = True
            else:
                row[f"{reg}_match_tier"] = "none"
                row[f"{reg}_names"] = None
                row[f"{reg}_ids"] = None
                row[f"{reg}_fuel"] = None

        if any_hit_this_entry:
            n_any += 1
            results.append(row)

    out = pd.DataFrame(results)
    out.to_csv(OUT, index=False, encoding="utf-8")

    # ---- report -------------------------------------------------------------
    n_unclear = int(unclear_mask.sum())
    print(f"-> {OUT}: {len(out)} matched entries (of {n_unclear} 'unclear')\n")
    for reg in registries:
        n_hit = int(out[f"{reg}_ids"].notna().sum()) if len(out) else 0
        tc = tier_counts[reg]
        breakdown = ", ".join(f"{t}={tc[t]}" for t in all_tiers if tc[t])
        print(f"  {reg:7s}: {n_hit:4d}/{n_unclear} matched  "
              f"[{conflict_count[reg]} conflict]")
        print(f"           tiers: {breakdown}")
    print(f"\n  at least one registry hit: {n_any}/{n_unclear} "
          f"({100*n_any/n_unclear:.0f}%)")


if __name__ == "__main__":
    main()
