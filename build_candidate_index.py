"""
build_candidate_index.py  —  Pipeline step 0.

Builds ONE candidate index that every downstream match stage searches against.

  PyPSA (powerplantmatching) = spine  ─┐
  + OPSD name_bnetza (join on opsd_id) ─┤ enrich: curated names + all IDs
  + BNetzA Anzeigename (join on mastr) ─┘
  then AGGREGATE turbine-level rows into matchable units:
    entry_type = "individual"  (Pass 1: same-name + co-located turbines → one farm)
    entry_type = "cluster"     (Pass 2: geo-cluster same fuel/tech → set of plants)

Each candidate row carries all constituent IDs (mastr/opsd/eic/pypsa) so a match
emits the plant IDs the thesis needs, and up to 3 name variants to match against.

Reuses the geo-clustering from ../pypsa-redispatch-data-linkage/aggregate_plants.py;
drops that script's Pass 4 (LLM cluster naming) — the index doesn't need pretty
names, the member names ARE the fuzzy-match queries.
"""

import ast
import math
import os
import re
from collections import defaultdict

import pandas as pd

# ── files ─────────────────────────────────────────────────────────────────────
DATA   = "data/"
PYPSA  = DATA + "pypsa_unaggregated_powerplants.csv"
OPSD   = DATA + "OPSD_conventional_power_plants_DE.csv"
BNETZA = DATA + "Bundesnetzagentur_Kraftwerkliste .csv"
OUT    = DATA + "candidate_index.csv"

# ── tuning (same constants as the old aggregate_plants.py) ────────────────────
MAX_FARM_RADIUS_KM       = 50.0        # Pass 1: same-name merge tolerance
GEO_CLUSTER_RADIUS_KM    = 10.0        # Pass 2: connect plants within this distance
GEO_CLUSTER_MAX_DIAMETER = 30.0        # Pass 2: max cluster span (splits chains)
GEO_CLUSTER_MIN_PLANTS   = 3
GEO_CLUSTER_MIN_CAPACITY = 1.0         # MW — drop rooftop-solar noise from clustering
CLUSTER_ID_OFFSET        = 10_000_000
# redispatch "Cluster" entries are all renewable DSO clusters (99.4% Erneuerbar, all wind/solar);
# clustering conventional plants only produces candidates nothing can match.
# ponytail: renewables only; add Hydro/others if a non-renewable Cluster entry ever appears
CLUSTER_FUELS            = {"Wind", "Solar", "Biogas"}
RECONCILE_KM             = 0.5         # OPSD gap-fill: same plant if within this of a PyPSA plant

# OPSD energy_source → PyPSA Fueltype vocab (for gap-filled rows)
FUELMAP = {
    "Natural gas": "Natural Gas", "Hydro": "Hydro", "Hard coal": "Hard Coal",
    "Waste": "Waste", "Lignite": "Lignite", "Oil": "Oil", "Nuclear": "Nuclear",
    "Biomass and biogas": "Solid Biomass",   # ponytail: coarse — conventional biomass ≈ solid; refine if fuel filter misfires
    "Other fuels": "Other", "Other fossil fuels": "Other", "Mixed fossil fuels": "Other",
}


def norm(name: str) -> str:
    """Minimal name normaliser for cluster aliases: strip TSO prefixes, parens, punctuation."""
    s = str(name).strip()
    s = re.sub(r"^\s*(50H|TTG|TNG|AMP|TBW)\s+", "", s)      # TSO abbrev prefixes
    s = re.sub(r"\([^)]*\)", " ", s)                        # parenthesised noise
    s = re.sub(r"[^\w\säöüÄÖÜß-]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ── ID extraction from PyPSA projectID dict ───────────────────────────────────
def parse_ids(project_id: str, eic_col: str) -> tuple[list, list, list]:
    """Return (mastr_ids, opsd_ids, eic_ids) from a PyPSA projectID cell + EIC column."""
    mastr, opsd, eic = [], [], []
    try:
        d = ast.literal_eval(project_id)
    except (ValueError, SyntaxError):
        d = {}
    if isinstance(d, dict):
        mastr = [str(x).replace("MASTR-", "") for x in d.get("MASTR", [])]
        opsd  = [str(x) for x in d.get("OPSD", [])]
        eic   = [str(x) for x in d.get("ENTSOE", [])]
    if isinstance(eic_col, str) and eic_col not in ("", "{nan}", "nan"):
        eic.append(eic_col)
    return mastr, opsd, eic


def uniq(series_of_lists) -> list:
    s = set()
    for lst in series_of_lists:
        if isinstance(lst, list):
            s.update(x for x in lst if x)
    return sorted(s)


# ── geo helpers (copied from aggregate_plants.py — small, stable) ──────────────
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_components(group: pd.DataFrame, radius_km: float) -> list[list[int]]:
    indices, lats, lons = group.index.tolist(), group["lat"].tolist(), group["lon"].tolist()
    n = len(indices)
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in range(n):
        buckets[(int(lats[i] / 0.1), int(lons[i] / 0.1))].append(i)
    uf = UnionFind(n)
    for (cy, cx), members in buckets.items():
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nb = (cy + dy, cx + dx)
                if nb not in buckets:
                    continue
                for i in members:
                    for j in buckets[nb]:
                        if j > i and haversine_km(lats[i], lons[i], lats[j], lons[j]) <= radius_km:
                            uf.union(i, j)
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        comps[uf.find(i)].append(indices[i])
    return list(comps.values())


def max_pairwise_km(cdf: pd.DataFrame) -> float:
    lats, lons = cdf["lat"].tolist(), cdf["lon"].tolist()
    n = len(lats)
    if n < 2:
        return 0.0
    step = 1 if n <= 50 else max(1, n // 50)        # ponytail: subsample big comps, O(n^2) is the ceiling
    idx = list(range(0, n, step))
    return max(haversine_km(lats[i], lons[i], lats[j], lons[j])
               for a, i in enumerate(idx) for j in idx[a + 1:])


def split_oversized(components, full_df, max_diameter_km, min_radius_km=2.5):
    result, queue = [], [(c, GEO_CLUSTER_RADIUS_KM) for c in components]
    while queue:
        comp, r = queue.pop(0)
        if len(comp) < 2 or max_pairwise_km(full_df.loc[comp]) <= max_diameter_km:
            result.append(comp)
            continue
        new_r = r / 2
        if new_r < min_radius_km:
            result.append(comp)
            continue
        for sub in cluster_components(full_df.loc[comp], new_r):
            queue.append((sub, new_r))
    return result


# ── Pass 1: same-name + co-located turbine merge → individual farm units ───────
def centroid_span_km(g: pd.DataFrame) -> float:
    wc = g.dropna(subset=["lat", "lon"])
    if len(wc) < 2:
        return 0.0
    clat, clon = wc["lat"].mean(), wc["lon"].mean()
    return max(haversine_km(r.lat, r.lon, clat, clon) for r in wc.itertuples())


def merge_rows(g: pd.DataFrame) -> dict:
    rep = g.iloc[0].to_dict()
    rep["Capacity"] = g["Capacity"].sum()
    wc = g.dropna(subset=["lat", "lon"])
    if len(wc):
        rep["lat"], rep["lon"] = wc["lat"].mean(), wc["lon"].mean()
    rep["turbine_count"] = len(g)
    rep["source_pypsa_ids"] = ",".join(str(i) for i in g["id"].tolist())
    rep["mastr_ids"] = uniq(g["mastr_ids"])
    rep["opsd_ids"]  = uniq(g["opsd_ids"])
    rep["eic_ids"]   = uniq(g["eic_ids"])
    rep["name_opsd"] = next((x for x in g["name_opsd"] if isinstance(x, str) and x), "")
    return rep


def pass1_individuals(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, g in df.groupby(["Name", "Fueltype", "Technology"], sort=False):
        if len(g) == 1 or centroid_span_km(g) > MAX_FARM_RADIUS_KM:
            for r in g.to_dict("records"):
                r["turbine_count"] = 1
                r["source_pypsa_ids"] = str(r["id"])
                rows.append(r)
        else:
            rows.append(merge_rows(g))
    out = pd.DataFrame(rows).reset_index(drop=True)
    out["entry_type"] = "individual"
    return out


# ── Pass 2: geographic clusters → set-of-plants candidates ─────────────────────
def pass2_clusters(indiv: pd.DataFrame) -> list[dict]:
    elig = indiv[(indiv["Capacity"] >= GEO_CLUSTER_MIN_CAPACITY)
                 & indiv["lat"].notna() & indiv["lon"].notna()].copy()
    rows, cid = [], CLUSTER_ID_OFFSET
    for (fuel, tech), g in elig.groupby([elig["Fueltype"].fillna(""),
                                         elig["Technology"].fillna("")], sort=False):
        if fuel not in CLUSTER_FUELS or len(g) < GEO_CLUSTER_MIN_PLANTS:
            continue
        comps = split_oversized(cluster_components(g, GEO_CLUSTER_RADIUS_KM), g, GEO_CLUSTER_MAX_DIAMETER)
        for comp in comps:
            if len(comp) < GEO_CLUSTER_MIN_PLANTS:
                continue
            cdf = g.loc[comp]
            aliases = sorted({norm(n) for n in cdf["Name"]} - {""})
            rows.append({
                "id": cid, "entry_type": "cluster",
                "Name": f"Cluster {cid}", "Fueltype": fuel, "Technology": tech,
                "Capacity": float(cdf["Capacity"].sum()),
                "lat": float(cdf["lat"].mean()), "lon": float(cdf["lon"].mean()),
                "turbine_count": len(cdf),
                "source_pypsa_ids": ",".join(str(i) for i in cdf["id"]),
                "mastr_ids": uniq(cdf["mastr_ids"]),
                "opsd_ids":  uniq(cdf["opsd_ids"]),
                "eic_ids":   uniq(cdf["eic_ids"]),
                "name_opsd": "",
                "aliases": ", ".join(aliases),
            })
            cid += 1
    return rows


# ── OPSD gap-fill: add the OPSD conventional plants PyPSA doesn't reference ────
def gapfill_opsd(py: pd.DataFrame, opsd: pd.DataFrame) -> pd.DataFrame:
    """OPSD plants whose id isn't in any PyPSA projectID. Reconcile by coords:
    within RECONCILE_KM of a PyPSA plant → enrich that row (attach opsd id/name);
    otherwise → return as a new index row (in py schema)."""
    referenced = set().union(*py["opsd_ids"]) if len(py) else set()
    missing = opsd[~opsd["id"].astype(str).isin(referenced)].copy()

    cell = 0.01
    lat, lon = py["lat"].to_numpy(), py["lon"].to_numpy()
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in range(len(py)):
        if pd.notna(lat[i]) and pd.notna(lon[i]):
            buckets[(int(lat[i] / cell), int(lon[i] / cell))].append(i)

    new_rows, enriched = [], 0
    for r in missing.itertuples():
        oid = str(r.id)
        best, bestd = None, RECONCILE_KM
        if pd.notna(r.lat) and pd.notna(r.lon):
            cy, cx = int(r.lat / cell), int(r.lon / cell)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for j in buckets.get((cy + dy, cx + dx), []):
                        d = haversine_km(r.lat, r.lon, lat[j], lon[j])
                        if d < bestd:
                            bestd, best = d, j
        if best is not None:                       # same plant already in PyPSA, untagged
            if oid not in py.at[best, "opsd_ids"]:
                py.at[best, "opsd_ids"].append(oid)
            if not py.at[best, "name_opsd"]:
                py.at[best, "name_opsd"] = str(r.name_bnetza)
            enriched += 1
        else:                                      # genuinely absent → new candidate
            new_rows.append({
                "id": f"OPSD-{oid}", "Name": str(r.name_bnetza).strip(),
                "Fueltype": FUELMAP.get(r.energy_source, "Other"),
                "Technology": str(r.technology) if pd.notna(r.technology) else "",
                "Capacity": float(r.capacity_net_bnetza) if pd.notna(r.capacity_net_bnetza) else 0.0,
                "lat": r.lat, "lon": r.lon,
                "mastr_ids": [], "opsd_ids": [oid],
                "eic_ids": [str(r.eic_code_plant)] if pd.notna(r.eic_code_plant) else [],
                "name_opsd": str(r.name_bnetza).strip(),
            })
    print(f"  OPSD not in PyPSA: {len(missing)} → {enriched} reconciled (untagged dup), "
          f"{len(new_rows)} added as new candidates")
    return pd.DataFrame(new_rows)


# ── standalone BNetzA exact-match lookup (NOT part of the index) ───────────────
def write_bnetza_lookup() -> None:
    kw = pd.read_csv(BNETZA, sep=";", skiprows=9, encoding="latin-1", low_memory=False)
    kw.columns = [c.strip() for c in kw.columns]
    kw = kw[kw["Datensatztyp*"].isin(["Einzelanlage", "stillgelegte Anlagen"])].copy()  # drop aggregated buckets
    kw["norm_name"] = kw["Anzeigename"].map(norm)
    kw["Nettonennleistung_MW"] = pd.to_numeric(
        kw["Nettonennleistung_MW"].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    out = kw.rename(columns={"EinheitMastrNummer": "mastr_id"})[
        ["mastr_id", "Anzeigename", "norm_name", "Energietraeger",
         "Nettonennleistung_MW", "Postleitzahl", "Ort"]]
    out.to_csv(DATA + "bnetza_lookup.csv", index=False, encoding="utf-8")
    print(f"→ {DATA}bnetza_lookup.csv: {len(out)} rows "
          f"({out['norm_name'].nunique()} distinct normalised names) for stage-1 exact match")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # 1. PyPSA spine ------------------------------------------------------------
    py = pd.read_csv(PYPSA)
    py["lat"] = pd.to_numeric(py["lat"], errors="coerce")
    py["lon"] = pd.to_numeric(py["lon"], errors="coerce")
    py["Capacity"] = pd.to_numeric(py["Capacity"], errors="coerce").fillna(0.0)
    py["Name"] = py["Name"].astype(str).str.strip()
    py["Technology"] = py["Technology"].fillna("").astype(str)
    parsed = [parse_ids(p, e) for p, e in zip(py["projectID"], py["EIC"].astype(str))]
    py["mastr_ids"] = [m for m, _, _ in parsed]
    py["opsd_ids"]  = [o for _, o, _ in parsed]
    py["eic_ids"]   = [c for _, _, c in parsed]
    print(f"PyPSA spine: {len(py)} rows")

    # 2. enrich with OPSD curated names (join on opsd_id) -----------------------
    opsd = pd.read_csv(OPSD, low_memory=False)
    opsd_name = dict(zip(opsd["id"].astype(str), opsd["name_bnetza"].astype(str)))
    py["name_opsd"] = [next((opsd_name[o] for o in ids if o in opsd_name), "") for ids in py["opsd_ids"]]
    print(f"  rows enriched with OPSD name:   {(py['name_opsd'] != '').sum()}")

    # 3. gap-fill: add OPSD conventional plants PyPSA doesn't already hold -------
    opsd_extra = gapfill_opsd(py, opsd)
    py = pd.concat([py, opsd_extra], ignore_index=True)
    print(f"  spine after gap-fill: {len(py)} rows")

    # 4. aggregate (BNetzA is NOT in the index — see write_bnetza_lookup) --------
    indiv = pass1_individuals(py)
    print(f"\nPass 1 individuals: {len(indiv)} (from {len(py)} turbine rows)")
    clusters = pass2_clusters(indiv)
    print(f"Pass 2 clusters:    {len(clusters)}")

    # 5. assemble match_names + write ------------------------------------------
    out = pd.concat([indiv, pd.DataFrame(clusters)], ignore_index=True)

    def match_names(r):
        if r["entry_type"] == "cluster":
            return r["aliases"]
        names = [str(r["Name"]), str(r.get("name_opsd", ""))]
        return " | ".join(sorted({n for n in names if n and n != "nan"}))
    out["match_names"] = out.apply(match_names, axis=1)
    for col in ("mastr_ids", "opsd_ids", "eic_ids"):
        out[col] = out[col].apply(lambda v: ",".join(v) if isinstance(v, list) else "")
    out["aliases"] = out.get("aliases", "").fillna("")

    cols = ["id", "entry_type", "Name", "match_names", "aliases",
            "Fueltype", "Technology", "Capacity", "lat", "lon",
            "mastr_ids", "opsd_ids", "eic_ids", "source_pypsa_ids", "turbine_count"]
    out[cols].to_csv(OUT, index=False, encoding="utf-8")
    print(f"\n→ {OUT}: {len(out)} candidate rows "
          f"({(out['entry_type']=='individual').sum()} individual, "
          f"{(out['entry_type']=='cluster').sum()} cluster)")
    print(f"  individuals ≥10 MW: {((out['entry_type']=='individual') & (out['Capacity']>=10)).sum()}")

    # self-check ----------------------------------------------------------------
    cl = out[out["entry_type"] == "cluster"]
    assert (cl["turbine_count"] >= GEO_CLUSTER_MIN_PLANTS).all(), "cluster below min plants"
    assert (out["Capacity"] >= 0).all(), "negative capacity"
    assert out["match_names"].str.len().gt(0).all(), "candidate with no name to match on"
    print("  self-check OK")

    # BNetzA standalone exact-match lookup (separate from the index) ------------
    write_bnetza_lookup()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
