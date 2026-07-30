"""Redispatch network-KDE map with pre/post Redispatch-2.0 split and dual metric.

Single self-contained interactive HTML. Three live filter axes drive BOTH the
event circles AND the network-KDE heat (lines + glow):

  - period:    combined | pre 2021-10-01 | post 2021-10-01  (Redispatch 2.0)
  - direction: all | positive (erhoehen) | negative (reduzieren)
  - metric:    Anzahl (count of events) | MWh (sum of |GESAMTE_ARBEIT_MWH|)

The reasons dropdown subsets the circles only -- pre-computing 18 (period x dir
x metric) x K-reasons KDE rasters is not worth it; the heat reflects all reasons.

Method
------
1. Build a graph over the 380/220 kV transmission lines (nodes = substations,
   edge weight = real line length [m]); bridge co-located buses across voltages
   so density can cross transformers.
2. Snap each unique redispatch location onto its nearest line ONCE (invariant
   to the filters).
3. For each of the 9 (period x direction) filter combinations and each metric
   (count / MWh), accumulate a quartic kernel of bandwidth H along the network
   (shortest path on the line graph) weighted by per-location count or summed
   |MWh|.
4. For the off-grid glow, value(P) = density(nearest lixel) * Kperp(|P-G|),
   perpendicular bandwidth H / PERP_FACTOR.
5. All 18 lixel density vectors + 18 PNG rasters are embedded; the toggle in
   the UI just swaps colours / image, no recompute in the browser.

Normalisation: per metric, the heat is normalised to the 98th percentile of
the COMBINED-ALL-DIRECTION density. Switching to "pre" therefore shows dimmer
heat, honestly, because the pre period contains less.

Output:
    redispatch_kde_map_germany.html
"""
import base64
import io
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import networkx as nx
from PIL import Image
from scipy.spatial import cKDTree
from shapely import wkt, STRtree
from shapely.geometry import Point
from shapely.ops import substring

from config import JOINED_CSV, BATTERY_CSV, GRID_DIR, MAPS_OUT

SRC = JOINED_CSV

OUT = MAPS_OUT / "redispatch_kde_map_germany.html"

LAT_MIN, LAT_MAX = 46.5, 56.0
LON_MIN, LON_MAX = 3.0, 16.0
H = 100000.0       # network bandwidth [m]
DELTA = 2500.0     # lixel length [m]
SIMPLIFY = 0.002   # geometry simplification for drawing [deg]
Q = "'"
PERP_FACTOR = 2.0  # off-line perpendicular kernel attenuation
IMG_WIDTH = 760
LAT0 = 51.0

CUTOFF_DATE = pd.Timestamp("2021-10-01")  # Redispatch 2.0 hard start

DEG = 111139.0
COSLAT0 = float(np.cos(np.radians(LAT0)))


def to_m(lon, lat):
    return np.asarray(lon) * DEG * COSLAT0, np.asarray(lat) * DEG


# ============================================================================
# 1) REDISPATCH EVENTS
# ============================================================================
df = pd.read_csv(SRC, sep=";", low_memory=False)
df = df.dropna(subset=["lat", "lon"])
df = df[(df.lat.between(LAT_MIN, LAT_MAX)) & (df.lon.between(LON_MIN, LON_MAX))]
df["beg"] = pd.to_datetime(df["BEGINN_DATUM"], format="%d.%m.%Y", errors="coerce")


def direction(s):
    if not isinstance(s, str):
        return None
    if "reduzieren" in s:
        return 0
    if "erh" in s:
        return 1
    return None


df["dir"] = df["RICHTUNG"].map(direction)
df["reason"] = df["GRUND_DER_MASSNAHME"].fillna("Unbekannt").astype(str).str.strip()

# Keep *-bedingt Redispatch (Strom-, Spannungs-, Strom-und-Spannungs-),
# drop Countertrade + test runs + Unbekannt + Einspeisemanagement
n_before = len(df)
df = df[df["reason"].str.contains("bedingt", case=False, na=False)
        & ~df["reason"].str.contains("countertrade", case=False, na=False)]
print(f"filter (bedingt without Countertrade): kept {len(df)}/{n_before} rows  "
      f"reasons remaining: {df['reason'].unique().tolist()}")

df = df.dropna(subset=["beg", "dir"])
df["dir"] = df["dir"].astype(int)
df["period"] = (df["beg"] >= CUTOFF_DATE).astype(int)  # 0=pre, 1=post
df["mwh"] = pd.to_numeric(df["GESAMTE_ARBEIT_MWH"], errors="coerce").abs().fillna(0.0)

reason_order = df["reason"].value_counts().index.tolist()
reason_idx = {r: i for i, r in enumerate(reason_order)}
df["ridx"] = df["reason"].map(reason_idx)


def mode_or_blank(s):
    s = s.dropna()
    return s.mode().iat[0] if not s.empty else ""


meta = (df.groupby(["lat", "lon"])
        .agg(plant=("plant_name", mode_or_blank),
             affected=("BETROFFENE_ANLAGE", mode_or_blank))
        .reset_index())

# per-location records for the circles: [period, dir, ridx, count, mwh]
agg = (df.groupby(["lat", "lon", "period", "dir", "ridx"])
       .agg(c=("mwh", "size"), m=("mwh", "sum"))
       .reset_index())

locations = []
for _, row in meta.iterrows():
    lat, lon = row.lat, row.lon
    sub = agg[(agg["lat"] == lat) & (agg["lon"] == lon)]
    rec = [[int(p), int(d), int(r), int(c), round(float(m), 1)]
           for p, d, r, c, m in sub[["period", "dir", "ridx", "c", "m"]].values]
    name = row.plant or row.affected or "Unbekannte Anlage"
    locations.append({
        "lat": round(float(lat), 5),
        "lon": round(float(lon), 5),
        "name": name, "rec": rec,
    })

print(f"events: {len(df)}  locations: {len(locations)}  "
      f"pre/post: {(df['period']==0).sum()}/{(df['period']==1).sum()}  "
      f"directions pos/neg: {(df['dir']==1).sum()}/{(df['dir']==0).sum()}")
DATA_JSON = json.dumps({"reasons": reason_order, "locations": locations},
                       ensure_ascii=False)


# ============================================================================
# 2) GRID backdrop
# ============================================================================
grid = {"ac": [], "xb": [], "dc": []}
gb = pd.read_csv(GRID_DIR / "buses.csv")
de_ids = set(gb[gb.country == "DE"].bus_id)
gl = pd.read_csv(GRID_DIR / "lines.csv", quotechar=Q)
gk = pd.read_csv(GRID_DIR / "links.csv", quotechar=Q)


def simp_path(g_wkt):
    g = wkt.loads(g_wkt).simplify(0.004, preserve_topology=False)
    return [[round(y, 5), round(x, 5)] for x, y in g.coords]


end_de = gl.bus0.isin(de_ids).astype(int) + gl.bus1.isin(de_ids).astype(int)
internal_df = gl[end_de == 2].reset_index(drop=True)
for _, r in internal_df.iterrows():
    grid["ac"].append({"v": int(r.voltage), "p": simp_path(r.geometry)})
for _, r in gl[end_de == 1].iterrows():
    grid["xb"].append({"v": int(r.voltage), "p": simp_path(r.geometry)})
for _, r in gk[gk.bus0.isin(de_ids) | gk.bus1.isin(de_ids)].iterrows():
    grid["dc"].append({"v": int(r.voltage), "p": simp_path(r.geometry)})
print(f"grid: AC {len(grid['ac'])}  cross-border {len(grid['xb'])}  HVDC {len(grid['dc'])}")
GRID_JSON = json.dumps(grid, ensure_ascii=False)


# ============================================================================
# 3) LINE GRAPH + SHORTEST PATHS
# ============================================================================
print("\n[NKDE] building line graph ...")


def geom_length_m(g):
    coords = np.asarray(g.coords)
    if len(coords) < 2:
        return 0.0
    dlon = np.diff(coords[:, 0]) * DEG * COSLAT0
    dlat = np.diff(coords[:, 1]) * DEG
    return float(np.sum(np.hypot(dlon, dlat)))


lengths = pd.to_numeric(internal_df["length"], errors="coerce")
geoms = internal_df["geometry"].apply(wkt.loads)
lengths = lengths.fillna(geoms.apply(geom_length_m))
internal_df = internal_df.assign(_len=lengths.values, _geom=geoms.values)

nodes = pd.unique(pd.concat([internal_df.bus0, internal_df.bus1]))
nidx = {b: i for i, b in enumerate(nodes)}
N = len(nodes)
u_arr = internal_df.bus0.map(nidx).to_numpy()
v_arr = internal_df.bus1.map(nidx).to_numpy()
len_arr = internal_df["_len"].to_numpy(dtype=float)

G = nx.Graph()
G.add_nodes_from(range(N))
for i in range(len(internal_df)):
    a, b, w = int(u_arr[i]), int(v_arr[i]), float(len_arr[i])
    if G.has_edge(a, b):
        if w < G[a][b]["w"]:
            G[a][b]["w"] = w
    else:
        G.add_edge(a, b, w=w)


def base_id(b):
    return re.sub(r"-\d+$", "", str(b))


groups = defaultdict(list)
for b, i in nidx.items():
    groups[base_id(b)].append(i)
n_bridge = 0
for members in groups.values():
    if len(members) > 1:
        m0 = members[0]
        for m in members[1:]:
            if not G.has_edge(m0, m):
                G.add_edge(m0, m, w=250.0)
                n_bridge += 1
comps = sorted((len(c) for c in nx.connected_components(G)), reverse=True)
print(f"[NKDE] nodes {N}  edges {len(internal_df)}  bridges {n_bridge}  "
      f"components {len(comps)} (largest {comps[0]})")

print(f"[NKDE] all-pairs shortest paths (cutoff {H/1000:.0f} km) ...")
Dmat = np.full((N, N), np.inf)
for s in range(N):
    d = nx.single_source_dijkstra_path_length(G, s, cutoff=H, weight="w")
    for t, dist in d.items():
        Dmat[s, t] = dist


# ============================================================================
# 4) LIXELS
# ============================================================================
print(f"[NKDE] cutting lixels (~{DELTA/1000:.1f} km) ...")
line_geoms = list(internal_df["_geom"].values)
seg_p, seg_q, seg_c, seg_eid, seg_coords = [], [], [], [], []
for j in range(len(internal_df)):
    Li = float(len_arr[j])
    g = line_geoms[j]
    gs = g.simplify(SIMPLIFY, preserve_topology=False)
    n = max(1, int(round(Li / DELTA)))
    for k in range(n):
        f0, f1 = k / n, (k + 1) / n
        fmid = (k + 0.5) / n
        seg = substring(gs, f0, f1, normalized=True)
        coords = [[round(y, 5), round(x, 5)] for x, y in seg.coords]
        if len(coords) < 2:
            continue
        seg_coords.append(coords)
        seg_p.append(int(u_arr[j])); seg_q.append(int(v_arr[j]))
        seg_c.append(fmid * Li); seg_eid.append(j)
seg_p = np.array(seg_p); seg_q = np.array(seg_q)
seg_c = np.array(seg_c); seg_eid = np.array(seg_eid)
L = len(seg_c)
c_arr = seg_c
d_arr = len_arr[seg_eid] - seg_c
print(f"[NKDE] lixels: {L}")


# ============================================================================
# 5) SNAP unique redispatch locations ONCE
# ============================================================================
print("[NKDE] snapping locations onto nearest line ...")
loc_df = df[["lat", "lon"]].drop_duplicates().reset_index(drop=True)
loc_lat = loc_df["lat"].to_numpy()
loc_lon = loc_df["lon"].to_numpy()
tree = STRtree(line_geoms)
snap_u = np.empty(len(loc_df), dtype=int)
snap_v = np.empty(len(loc_df), dtype=int)
snap_a = np.empty(len(loc_df), dtype=float)
snap_eid = np.empty(len(loc_df), dtype=int)
snap_d_m = np.empty(len(loc_df), dtype=float)
for i in range(len(loc_df)):
    P = Point(loc_lon[i], loc_lat[i])
    j = int(tree.nearest(P))
    g = line_geoms[j]
    snap_u[i] = int(u_arr[j])
    snap_v[i] = int(v_arr[j])
    snap_a[i] = g.project(P, normalized=True) * float(len_arr[j])
    snap_eid[i] = j
    # snap distance (approximate, metric)
    snap_d_m[i] = g.distance(P) * DEG  # ok as a relative diagnostic
print(f"[NKDE] snapped {len(loc_df)} locations  "
      f"(median {np.median(snap_d_m)/1000:.1f} km, max {snap_d_m.max()/1000:.1f} km)")


# ============================================================================
# 6) WEIGHTS per (period, dir, metric)
# ============================================================================
loc_key = {(round(la, 5), round(lo, 5)): i
           for i, (la, lo) in enumerate(zip(loc_lat, loc_lon))}
PERIODS = ["all", 0, 1]
DIRS = ["all", 0, 1]
METRICS = ["count", "mwh"]
weights = {(p, dd, mt): np.zeros(len(loc_df))
           for p in PERIODS for dd in DIRS for mt in METRICS}

gagg = (df.groupby(["lat", "lon", "period", "dir"])
        .agg(c=("mwh", "size"), m=("mwh", "sum"))
        .reset_index())
for _, row in gagg.iterrows():
    li = loc_key[(round(row.lat, 5), round(row.lon, 5))]
    p = int(row.period); dd = int(row.dir)
    cv = float(row.c); mv = float(row.m)
    for pf in ("all", p):
        for df_ in ("all", dd):
            weights[(pf, df_, "count")][li] += cv
            weights[(pf, df_, "mwh")][li] += mv


# ============================================================================
# 7) NETWORK KDE per combo
# ============================================================================
print("\n[NKDE] computing 18 densities ...")


def compute_density(w):
    density = np.zeros(L)
    nz = np.where(w > 0)[0]
    for i in nz:
        u, v, a, eid, wi = snap_u[i], snap_v[i], snap_a[i], snap_eid[i], w[i]
        r1 = a + Dmat[u, seg_p] + c_arr
        r2 = a + Dmat[u, seg_q] + d_arr
        r3 = (len_arr[eid] - a) + Dmat[v, seg_p] + c_arr
        r4 = (len_arr[eid] - a) + Dmat[v, seg_q] + d_arr
        dist = np.minimum(np.minimum(r1, r2), np.minimum(r3, r4))
        same = seg_eid == eid
        if same.any():
            dist[same] = np.minimum(dist[same], np.abs(a - c_arr[same]))
        x = dist / H
        k = np.where(x < 1.0, (1.0 - x * x) ** 2, 0.0)
        density += wi * k
    return density


densities = {}
for key, w in weights.items():
    densities[key] = compute_density(w)
    if (densities[key] > 0).any():
        print(f"  {key}: max {densities[key].max():.1f}  "
              f"nonzero {(densities[key]>0).sum()}/{L}")


# ============================================================================
# 8) NORMALISATION (per metric, anchored on combined-all reference)
# ============================================================================
def p98(arr):
    a = arr[arr > 0]
    return float(np.percentile(a, 98)) if a.size else 1.0


norm = {
    "count": p98(densities[("all", "all", "count")]),
    "mwh":   p98(densities[("all", "all", "mwh")]),
}
print(f"[NKDE] norm p98: count={norm['count']:.2f}  mwh={norm['mwh']:.2f}")


# ============================================================================
# 9) RASTER + lixel palette indices
# ============================================================================
STOPS = np.array([[255, 255, 178], [254, 217, 118], [254, 178, 76],
                  [253, 141, 60], [240, 59, 32], [189, 0, 38]], dtype=float)
xs = np.linspace(0, 1, len(STOPS))
LUT = np.stack([np.interp(np.linspace(0, 1, 256), xs, STOPS[:, ch])
                for ch in range(3)], axis=1)
ALPHA_MAX = 0.72
GREY = "#dcdcdc"


def ramp_hex(t):
    t = min(max(t, 0.0), 1.0)
    x = t * (len(STOPS) - 1)
    i = int(np.floor(x)); f = x - i
    if i >= len(STOPS) - 1:
        c = STOPS[-1]
    else:
        c = STOPS[i] + (STOPS[i + 1] - STOPS[i]) * f
    return "#%02x%02x%02x" % (int(round(c[0])), int(round(c[1])), int(round(c[2])))


# 256-entry palette: idx 0 = grey (no data), idx 1..255 = YlOrRd ramp
PALETTE = [GREY] + [ramp_hex(i / 255.0) for i in range(1, 256)]

# raster geometry (once)
print("\n[AREA] rasterising 18 combos ...")
centers = np.array([np.mean(np.asarray(c), axis=0) for c in seg_coords])
cx, cy = to_m(centers[:, 1], centers[:, 0])
tree_px = cKDTree(np.column_stack([cx, cy]))


def merc_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


W = IMG_WIDTH
mx_lo, mx_hi = np.radians(LON_MIN), np.radians(LON_MAX)
my_hi, my_lo = merc_y(LAT_MAX), merc_y(LAT_MIN)
Hpx = int(round(W * (my_hi - my_lo) / (mx_hi - mx_lo)))
lons = np.linspace(LON_MIN, LON_MAX, W)
lats = inv_merc_y(np.linspace(my_hi, my_lo, Hpx))
LON_g, LAT_g = np.meshgrid(lons, lats)
PX, PY = to_m(LON_g.ravel(), LAT_g.ravel())
dist_r, idx_r = tree_px.query(np.column_stack([PX, PY]), k=1)
xperp = PERP_FACTOR * dist_r / H
fac_r = np.where(xperp < 1.0, (1.0 - xperp * xperp) ** 2, 0.0)


def make_png(dens, norm_val):
    base = dens[idx_r]
    val = (base * fac_r).reshape(Hpx, W)
    t = np.clip(val / max(norm_val, 1e-9), 0.0, 1.0)
    rgb = LUT[np.clip((t * 255).astype(int), 0, 255)]
    alpha = np.where(val <= 0, 0.0, t ** 0.7 * ALPHA_MAX)
    rgba = np.zeros((Hpx, W, 4), dtype=np.uint8)
    rgba[..., :3] = rgb.astype(np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def palette_indices(dens, norm_val):
    out = np.zeros(len(dens), dtype=int)
    nz = dens > 0
    t = np.clip(dens[nz] / max(norm_val, 1e-9), 0.0, 1.0)
    out[nz] = np.maximum(1, (t * 255).round().astype(int))
    return out.tolist()


kde_pal = {}
raster_imgs = {}
total_png_kb = 0
for key, dens in densities.items():
    norm_val = norm[key[2]]
    ks = f"{key[0]}_{key[1]}_{key[2]}"
    kde_pal[ks] = palette_indices(dens, norm_val)
    raster_imgs[ks] = make_png(dens, norm_val)
    total_png_kb += len(raster_imgs[ks]) // 1024
print(f"[AREA] rasters: {Hpx}x{W} px  total ~{total_png_kb} KB across 18 combos")


KDE_JSON = json.dumps({
    "seg": seg_coords, "pal": kde_pal, "palette": PALETTE,
}, ensure_ascii=False)
AREA_JSON = json.dumps({
    "imgs": raster_imgs,
    "bounds": [[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]],
}, ensure_ascii=False)


# ============================================================================
# 10) HTML
# ============================================================================
HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Redispatch-KDE Deutschland (Pre/Post Redispatch 2.0)</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:280px; font-size:13px; max-height:94vh; overflow:auto;
  }
  .panel h1 { font-size:15px; margin:0 0 2px; }
  .panel .sub { color:#666; font-size:11px; margin-bottom:10px; }
  .panel label { font-weight:600; display:block; margin:10px 0 4px; }
  select, .seg { width:100%; }
  .seg { display:flex; border:1px solid #ccc; border-radius:6px; overflow:hidden; }
  .seg button { flex:1; border:0; background:#f5f5f5; padding:6px 0; cursor:pointer; font-size:12px; }
  .seg button.active { background:#2c7bb6; color:#fff; }
  .chk { font-weight:400; display:flex; align-items:center; gap:6px; margin-top:6px; }
  .stat { margin-top:12px; padding-top:10px; border-top:1px solid #eee; font-size:12px; }
  .stat b { font-size:15px; }
  .legend {
    position:absolute; bottom:18px; right:12px; z-index:1000; background:#fff;
    padding:8px 12px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3); font-size:11px;
  }
  .legend .bar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#2c7bb6,#abd9e9,#ffffbf,#fdae61,#d7191c); }
  .legend .kbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Redispatch-Ereignisse</h1>
  <div class="sub">Live-Filter f&uuml;r Kreise UND KDE-Heatmap (Netz + Fl&auml;che).</div>

  <label>Zeitraum (Redispatch 2.0: 01.10.2021)</label>
  <div class="seg" id="perSeg">
    <button data-p="all" class="active">Alle</button>
    <button data-p="0">Pre</button>
    <button data-p="1">Post</button>
  </div>

  <label>Richtung</label>
  <div class="seg" id="dirSeg">
    <button data-d="all" class="active">Alle</button>
    <button data-d="pos">Positiv &uarr;</button>
    <button data-d="neg">Negativ &darr;</button>
  </div>

  <label>Grund der Ma&szlig;nahme (nur Kreise)</label>
  <select id="reasonSel"><option value="all">Alle Gr&uuml;nde</option></select>

  <label>Metrik</label>
  <div class="seg" id="metSeg">
    <button data-m="count" class="active">Anzahl</button>
    <button data-m="mwh">MWh</button>
  </div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Redispatch-Dichte (Netz-KDE)</label>
  <label class="chk"><input type="checkbox" id="kdeChk" checked/> Heatmap (Linien)</label>
  <label class="chk"><input type="checkbox" id="areaChk" checked/> Fl&auml;che (Glow um das Netz)</label>
  <div class="sub" style="margin:4px 0 0;">
    Bandbreite H = 100&nbsp;km entlang des Netzes; seitlich 2&times; schneller (effektiv 50&nbsp;km).
    Normalisierung pro Metrik (Skala = p98 der Combined-Alle-Richtungen-Dichte).
  </div>

  <div class="stat">
    <div><b id="evtCount">0</b> Ereignisse</div>
    <div><b id="mwhCount">0</b> MWh (|Arbeit|)</div>
    <div><span id="locCount">0</span> Standorte sichtbar</div>
  </div>
</div>

<div class="legend">
  <div id="circLabel">Ereignisse pro Standort</div>
  <div class="bar"></div>
  <div class="ticks"><span>1</span><span id="legMax">max</span></div>
  <div id="kdeLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div id="kdeLabel">Redispatch-Dichte am Netz</div>
    <div class="kbar"></div>
    <div class="ticks"><span>niedrig</span><span>hoch</span></div>
    <div style="margin-top:3px; color:#777;">Fl&auml;che: seitlicher Abfall 2&times; schneller (&asymp;50&nbsp;km)</div>
  </div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Stromnetz</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:0;border-top:2px dotted #41ab5d;vertical-align:middle;"></span> HVDC</div>
  </div>
</div>

<script>
const DATA = __DATA__;
const GRID = __GRID__;
const KDE  = __KDE__;
const AREA = __AREA__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

// panes
map.createPane('areaPane'); map.getPane('areaPane').style.zIndex = 240;
map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 250;
map.createPane('kdePane');  map.getPane('kdePane').style.zIndex = 260;
const gridRenderer = L.canvas({pane:'gridPane'});
const kdeRenderer  = L.canvas({pane:'kdePane'});

// grid backdrop
const gridLayer = L.layerGroup();
(function buildGrid(){
  (GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
    L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
      color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.55}).addTo(gridLayer); });
  (GRID.xb||[]).forEach(l=>{
    L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
      color:'#9e9e9e', weight:1.3, opacity:0.5, dashArray:'5,6'}).addTo(gridLayer); });
  (GRID.dc||[]).forEach(l=>{
    L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
      color:'#41ab5d', weight:2.5, opacity:0.7, dashArray:'2,7'}).addTo(gridLayer); });
})();

// lixel KDE (created once, recoloured on filter change)
const kdeLayer = L.layerGroup();
const lixels = [];
const GREY_HEX = KDE.palette[0];
KDE.seg.forEach(coords=>{
  const pl = L.polyline(coords, {pane:'kdePane', renderer:kdeRenderer, interactive:false,
    color: GREY_HEX, weight:3.4, opacity:0.9, lineCap:'butt'});
  pl.addTo(kdeLayer);
  lixels.push(pl);
});

// area glow image (src swapped on filter change)
const areaLayer = L.imageOverlay(AREA.imgs['all_all_count'], AREA.bounds,
  {pane:'areaPane', interactive:false, opacity:1.0});

const layer = L.layerGroup().addTo(map);  // event circles
gridLayer.addTo(map); kdeLayer.addTo(map); areaLayer.addTo(map);

// reasons dropdown
const sel = document.getElementById('reasonSel');
DATA.reasons.forEach((r,i)=>{
  const o=document.createElement('option'); o.value=i; o.textContent=r; sel.appendChild(o);
});

// state
let state = { period:'all', dir:'all', reason:'all', metric:'count' };

// circle ramp (blue->red, distinct from KDE colours)
const CSTOPS = [[44,123,182],[171,217,233],[255,255,191],[253,174,97],[215,25,28]];
function cramp(t){ t=Math.max(0,Math.min(1,t));
  const x=t*(CSTOPS.length-1), i=Math.floor(x), f=x-i;
  if(i>=CSTOPS.length-1) return `rgb(${CSTOPS[CSTOPS.length-1].join(',')})`;
  const a=CSTOPS[i], b=CSTOPS[i+1];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}

function dirIdx(){ return state.dir==='all'?'all':(state.dir==='pos'?1:0); }
function comboKey(){ return `${state.period}_${dirIdx()}_${state.metric}`; }

function fmtNum(v, decimals){
  return v.toLocaleString('de-DE', {maximumFractionDigits: decimals});
}
function fmtVal(v){
  return state.metric==='mwh'
    ? fmtNum(v, v<10?1:0) + ' MWh'
    : fmtNum(v, 0);
}

function recMatches(p, d, r){
  if(state.period!=='all' && p!==state.period) return false;
  if(state.dir==='pos' && d!==1) return false;
  if(state.dir==='neg' && d!==0) return false;
  if(state.reason!=='all' && r!==state.reason) return false;
  return true;
}

function locStats(loc){
  let c=0, m=0;
  for(const [p,d,r,cc,mm] of loc.rec){
    if(!recMatches(p,d,r)) continue;
    c += cc; m += mm;
  }
  return [c, m];
}

function render(){
  layer.clearLayers();
  // 1) compute current metric value per location + global stats
  const stats = DATA.locations.map(locStats);
  const vals = stats.map(s => state.metric==='mwh' ? s[1] : s[0]);
  const maxV = Math.max(1e-9, ...vals);
  let totalEvt=0, totalMwh=0, visLoc=0;
  DATA.locations.forEach((loc, idx)=>{
    const v = vals[idx]; if(v<=0) return;
    totalEvt += stats[idx][0]; totalMwh += stats[idx][1]; visLoc++;
    const t = Math.sqrt(v / maxV);
    const radius = 4 + 26*t;
    const col = cramp(v / maxV);
    const plab = state.period==='all'?'Alle Zeitr&auml;ume':(state.period===0?'Pre 01.10.2021':'Post 01.10.2021');
    const dlab = state.dir==='all'?'alle Richtungen':(state.dir==='pos'?'positiv (erhoehen)':'negativ (reduzieren)');
    const rlab = state.reason==='all'?'alle Gr&uuml;nde':DATA.reasons[state.reason];
    const mlab = state.metric==='mwh'?'MWh':'Anzahl';
    L.circleMarker([loc.lat,loc.lon], {radius, color:col, weight:1, fillColor:col, fillOpacity:0.78})
      .bindPopup(`<b>${loc.name}</b><br><b>${fmtVal(v)}</b> (${mlab})`
        + `<br><span style="color:#777;font-size:11px">${plab} &middot; ${dlab} &middot; ${rlab}</span>`)
      .bindTooltip(`${loc.name}: ${fmtVal(v)}`).addTo(layer);
  });
  document.getElementById('evtCount').textContent = totalEvt.toLocaleString('de-DE');
  document.getElementById('mwhCount').textContent = totalMwh.toLocaleString('de-DE', {maximumFractionDigits: 0});
  document.getElementById('locCount').textContent = visLoc;
  document.getElementById('legMax').textContent = fmtVal(maxV);
  document.getElementById('circLabel').textContent =
    state.metric==='mwh' ? 'MWh pro Standort (|Arbeit|)' : 'Ereignisse pro Standort';

  applyKde();
  applyArea();
}

function applyKde(){
  const key = comboKey();
  const pal = KDE.pal[key]; if(!pal) return;
  for(let i=0; i<lixels.length; i++){
    const idx = pal[i] || 0;
    lixels[i].setStyle({color: KDE.palette[idx]});
  }
  document.getElementById('kdeLabel').textContent =
    state.metric==='mwh' ? 'Redispatch-Dichte am Netz (MWh)' : 'Redispatch-Dichte am Netz (Anzahl)';
}

function applyArea(){
  const key = comboKey();
  const img = AREA.imgs[key]; if(!img) return;
  areaLayer.setUrl(img);
}

// segmented controls
function bindSeg(id, key, mapper){
  document.querySelectorAll(`#${id} button`).forEach(b=>{
    b.addEventListener('click', ()=>{
      document.querySelectorAll(`#${id} button`).forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      state[key] = mapper(b);
      render();
    });
  });
}
bindSeg('perSeg', 'period', b => b.dataset.p==='all' ? 'all' : parseInt(b.dataset.p));
bindSeg('dirSeg', 'dir',    b => b.dataset.d);
bindSeg('metSeg', 'metric', b => b.dataset.m);

sel.addEventListener('change', ()=>{
  state.reason = sel.value==='all' ? 'all' : parseInt(sel.value);
  render();
});

// layer toggles
const gridChk = document.getElementById('gridChk');
gridChk.addEventListener('change', ()=>{
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else { if(map.hasLayer(gridLayer)) map.removeLayer(gridLayer); }
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block' : 'none';
});
const kdeChk = document.getElementById('kdeChk');
kdeChk.addEventListener('change', ()=>{
  if(kdeChk.checked){ if(!map.hasLayer(kdeLayer)) kdeLayer.addTo(map); }
  else { if(map.hasLayer(kdeLayer)) map.removeLayer(kdeLayer); }
  document.getElementById('kdeLegend').style.display = kdeChk.checked ? 'block' : 'none';
});
const areaChk = document.getElementById('areaChk');
areaChk.addEventListener('change', ()=>{
  if(areaChk.checked){ if(!map.hasLayer(areaLayer)) areaLayer.addTo(map); }
  else { if(map.hasLayer(areaLayer)) map.removeLayer(areaLayer); }
});

render();
</script>
</body>
</html>"""

html = (
    HTML.replace("__DATA__", DATA_JSON)
    .replace("__GRID__", GRID_JSON)
    .replace("__KDE__", KDE_JSON)
    .replace("__AREA__", AREA_JSON)
)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"\nsaved -> {OUT}")
