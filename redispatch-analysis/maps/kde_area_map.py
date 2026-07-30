"""Redispatch + battery + grid map with a PLANAR (Euclidean) KERNEL DENSITY
ESTIMATE of redispatch energy -- the SAME map as make_redispatch_grid_kde_area_map.py
but WITHOUT SNAPPING events onto the transmission lines.

Difference to the network-KDE version
--------------------------------------
The network version snaps every redispatch point onto its nearest 380/220 kV line
and then spreads density ALONG the network (shortest path on the line graph). That
snapping is what anchors the heat to the corridors. Here we drop it entirely:

  * No line graph, no shortest paths, no snapping, no perpendicular kernel.
  * Density is a plain 2-D quartic KDE of |GESAMTE_ARBEIT_MWH| over geographic
    space, isotropic, bandwidth H. value(P) = sum_i w_i * Quartic(|P - event_i| / H).

Everything else is identical to the baseline/network map: the redispatch event
circles, the battery overlay, the plain transmission-grid backdrop, and every filter
(year / direction / reason / battery mode / battery size / grid on-off).

LAYER A -- grid lines coloured by the PLANAR density sampled at each lixel centre
  (so the lines still carry colour for visual parity, but the value is the Euclidean
  field at that point, not a network-distance density).

LAYER B -- area glow: the same planar field rendered as a raster everywhere.
  Because there is no network, the glow is a simple isotropic blob of bandwidth H
  around each event -- exactly what the field looks like with snapping removed.

CAVEAT (state this in the paper): redispatch is recorded per generating unit, not
per line. Without snapping the heat is NOT projected onto corridors at all -- it is
a spatial density of where redispatch energy concentrates, full stop. Compare this
map side-by-side with the network-KDE map to see what snapping contributes. The KDE
layer aggregates ALL events (all years, both directions) and is independent of the
year/direction/reason filters above it.

Output:
    redispatch_grid_kde_area_map_germany_nosnap.html
"""
import base64
import io
import json
from pathlib import Path
import numpy as np
import pandas as pd
from PIL import Image
from scipy.spatial import cKDTree
from shapely import wkt
from shapely.ops import substring

from config import JOINED_CSV, BATTERY_CSV, GRID_DIR, MAPS_OUT

SRC = JOINED_CSV
BAT_SRC = BATTERY_CSV

OUT = MAPS_OUT / "redispatch_grid_kde_area_map_germany_nosnap.html"

LAT_MIN, LAT_MAX = 46.5, 56.0
LON_MIN, LON_MAX = 3.0, 16.0

# --- planar KDE parameters ---
BANDWIDTH_MODE = "silverman"   # "silverman" (auto, weighted) or "manual"
H_MANUAL = 100000.0            # used when BANDWIDTH_MODE == "manual" [m]
KERNEL_FACTOR = 1.0            # multiplier on Silverman's Gaussian-reference h.
                               # 1.0 keeps the textbook value; raise to ~2.78 if
                               # you want to compensate for the quartic kernel
                               # being less efficient than Gaussian (Wand & Jones
                               # 1995, univariate canonical bandwidth ratio).
DELTA = 2500.0    # lixel length [m] for colouring the grid lines
SIMPLIFY = 0.002  # geometry simplification for drawing [deg] (~200 m)
Q = "'"

IMG_WIDTH = 760   # raster width [px]; height set to match the Web-Mercator aspect
LAT0 = 51.0       # reference latitude for the local metric (degrees->metres)

# ============================================================================
# 1) REDISPATCH EVENTS  (identical aggregation to the baseline map)
# ============================================================================
df = pd.read_csv(SRC, low_memory=False)
df = df.dropna(subset=["lat", "lon"])
df = df[(df["lat"].between(LAT_MIN, LAT_MAX)) & (df["lon"].between(LON_MIN, LON_MAX))]

df["year"] = pd.to_datetime(df["BEGINN_DATUM"], format="%d.%m.%Y", errors="coerce").dt.year

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
df = df.dropna(subset=["year", "dir"])
df["year"] = df["year"].astype(int)
df["dir"] = df["dir"].astype(int)

reason_order = df["reason"].value_counts().index.tolist()
reason_idx = {r: i for i, r in enumerate(reason_order)}
df["ridx"] = df["reason"].map(reason_idx)

def mode_or_blank(s):
    s = s.dropna()
    return s.mode().iat[0] if not s.empty else ""

meta = (
    df.groupby(["lat", "lon"])
    .agg(plant=("plant_name", mode_or_blank), affected=("BETROFFENE_ANLAGE", mode_or_blank),
         city=("city", mode_or_blank), state=("state", mode_or_blank))
    .reset_index()
)
counts = df.groupby(["lat", "lon", "year", "dir", "ridx"]).size().reset_index(name="c")

locations = []
for _, row in meta.iterrows():
    lat, lon = row["lat"], row["lon"]
    sub = counts[(counts["lat"] == lat) & (counts["lon"] == lon)]
    rec = sub[["year", "dir", "ridx", "c"]].astype(int).values.tolist()
    name = row["plant"] or row["affected"] or "Unbekannte Anlage"
    locations.append({"lat": round(float(lat), 5), "lon": round(float(lon), 5),
                      "name": name, "city": row["city"], "state": row["state"], "rec": rec})

years = sorted(df["year"].unique().tolist())
payload = {"reasons": reason_order, "years": years, "locations": locations}
print(f"locations: {len(locations)}  events: {df.shape[0]}  years: {years[0]}-{years[-1]}")
DATA_JSON = json.dumps(payload, ensure_ascii=False)

# ============================================================================
# 2) BATTERIES  (identical to baseline)
# ============================================================================
batteries = []
bat = pd.read_csv(BAT_SRC, low_memory=False)
bat = bat.dropna(subset=["lat", "lon"])
bat = bat[(bat["lat"].between(LAT_MIN, LAT_MAX)) & (bat["lon"].between(LON_MIN, LON_MAX))]
bat = bat[bat["status_cat"].isin(["active", "planning"])]
scode = {"active": 1, "planning": 2}
for _, r in bat.iterrows():
    date = r.get("commissioning_date") if r["status_cat"] == "active" else r.get("planned_commissioning_date")
    yr = pd.to_datetime(date, errors="coerce")
    yr = int(yr.year) if pd.notna(yr) else None
    batteries.append({"lat": round(float(r["lat"]), 5), "lon": round(float(r["lon"]), 5),
                      "name": str(r["name"]) if pd.notna(r["name"]) else "Batteriespeicher",
                      "kw": float(r["power_kw"]) if pd.notna(r["power_kw"]) else 0.0,
                      "s": scode[r["status_cat"]], "y": yr,
                      "city": str(r["city"]) if pd.notna(r["city"]) else "",
                      "state": str(r["state"]) if pd.notna(r["state"]) else "",
                      "date": str(date) if pd.notna(date) else ""})
print(f"batteries: {len(batteries)} mapped")
BAT_JSON = json.dumps(batteries, ensure_ascii=False)

# ============================================================================
# 3) GRID backdrop (identical to baseline)
# ============================================================================
grid = {"ac": [], "xb": [], "dc": []}
gb = pd.read_csv(GRID_DIR / "buses.csv")
de_ids = set(gb[gb.country == "DE"].bus_id)
gl = pd.read_csv(GRID_DIR / "lines.csv", quotechar=Q)
gk = pd.read_csv(GRID_DIR / "links.csv", quotechar=Q)

def simp_path(geom_wkt):
    g = wkt.loads(geom_wkt).simplify(0.004, preserve_topology=False)
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
# 4) PLANAR KDE  (NO snapping, NO network graph)
# ============================================================================
print("\n[KDE] planar (Euclidean) kernel density -- no snapping ...")
DEG = 111139.0
COSLAT0 = float(np.cos(np.radians(LAT0)))

def to_m(lon, lat):
    return np.asarray(lon) * DEG * COSLAT0, np.asarray(lat) * DEG


def silverman_bandwidth_2d(x, y, w=None):
    """Silverman's rule of thumb for an isotropic 2-D KDE bandwidth.

        h = (4 / (d + 2)) ** (1 / (d + 4))  *  sigma  *  n_eff ** (-1 / (d + 4))

    With d = 2 the prefactor is exactly 1, so h = sigma * n_eff ** (-1/6).
    The reference distribution is bivariate normal and the kernel is Gaussian;
    we keep that convention and let KERNEL_FACTOR rescale outside if a
    quartic-equivalent bandwidth is wanted.

    sigma:
        Geometric mean of the weighted standard deviations along x and y.
        Using the geometric mean preserves the area sigma_x * sigma_y under
        isotropisation, which is the standard convention when collapsing two
        per-axis bandwidths into one.

    n_eff:
        Kish's effective sample size, (sum w)^2 / sum(w^2). Falls back to the
        raw count when no weights are passed. This is what stops one giant
        event from acting like a thousand small ones.

    Returns (h, diagnostics).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if w is None:
        w = np.ones_like(x)
    else:
        w = np.asarray(w, dtype=float)
    if x.size < 2 or w.sum() <= 0:
        return float("nan"), {}
    W = float(w.sum())
    n_eff = (W * W) / float(np.sum(w * w))
    mx = float(np.sum(w * x) / W)
    my = float(np.sum(w * y) / W)
    sx = float(np.sqrt(max(np.sum(w * (x - mx) ** 2) / W, 0.0)))
    sy = float(np.sqrt(max(np.sum(w * (y - my) ** 2) / W, 0.0)))
    sigma = float(np.sqrt(sx * sy))
    d = 2
    prefactor = (4.0 / (d + 2)) ** (1.0 / (d + 4))  # = 1.0 for d = 2
    h = prefactor * sigma * n_eff ** (-1.0 / (d + 4))
    return float(h), dict(n_eff=n_eff, sigma_x=sx, sigma_y=sy,
                          sigma=sigma, prefactor=prefactor, n_raw=int(x.size))


# --- weighted redispatch locations (summed |MWh|), kept in geographic space ---
df["mwh"] = pd.to_numeric(df["GESAMTE_ARBEIT_MWH"], errors="coerce").abs()
wsum = df.groupby(["lat", "lon"])["mwh"].sum().reset_index()
wsum = wsum[wsum["mwh"] > 0]
ev_lat = wsum["lat"].to_numpy(dtype=float)
ev_lon = wsum["lon"].to_numpy(dtype=float)
ev_w = wsum["mwh"].to_numpy(dtype=float)
ev_x, ev_y = to_m(ev_lon, ev_lat)
print(f"[KDE] {len(ev_w)} weighted locations (no snapping; events stay at their own coordinates)")

# --- bandwidth selection ---
if BANDWIDTH_MODE == "silverman":
    h_silver, stats = silverman_bandwidth_2d(ev_x, ev_y, ev_w)
    if not np.isfinite(h_silver) or h_silver <= 0:
        raise RuntimeError("Silverman's rule returned a non-positive bandwidth; "
                           "check that ev_w has non-zero spread.")
    H = h_silver * KERNEL_FACTOR
    print(f"[KDE] Silverman: n_raw={stats['n_raw']}  n_eff={stats['n_eff']:.1f}  "
          f"sigma_x={stats['sigma_x']/1000:.1f} km  sigma_y={stats['sigma_y']/1000:.1f} km  "
          f"sigma_geom={stats['sigma']/1000:.1f} km")
    print(f"[KDE] h_silverman = {h_silver/1000:.2f} km  ->  H = {H/1000:.2f} km "
          f"(KERNEL_FACTOR={KERNEL_FACTOR})")
elif BANDWIDTH_MODE == "manual":
    H = float(H_MANUAL)
    print(f"[KDE] manual bandwidth H = {H/1000:.2f} km")
else:
    raise ValueError(f"unknown BANDWIDTH_MODE: {BANDWIDTH_MODE!r}")
BANDWIDTH_KM = H / 1000.0

# --- cut the internal lines into lixels just so the lines can carry colour ---
lengths = pd.to_numeric(internal_df["length"], errors="coerce")
geoms = internal_df["geometry"].apply(wkt.loads)
lengths = lengths.fillna(geoms.apply(lambda g: g.length * 111139.0))
line_geoms = list(geoms.values)
len_arr = lengths.to_numpy(dtype=float)

print(f"[KDE] cutting lixels (~{DELTA/1000:.1f} km) ...")
seg_coords = []
for j in range(len(internal_df)):
    Li = float(len_arr[j])
    g = line_geoms[j]
    gs = g.simplify(SIMPLIFY, preserve_topology=False)
    n = max(1, int(round(Li / DELTA)))
    for k in range(n):
        f0, f1 = k / n, (k + 1) / n
        seg = substring(gs, f0, f1, normalized=True)
        coords = [[round(y, 5), round(x, 5)] for x, y in seg.coords]
        if len(coords) < 2:
            continue
        seg_coords.append(coords)
L = len(seg_coords)
print(f"[KDE] lixels: {L}")

# --- planar density at each lixel centre (Euclidean kernel from the events) ---
centers = np.array([np.mean(np.asarray(c), axis=0) for c in seg_coords])  # [:, (lat, lon)]
cx, cy = to_m(centers[:, 1], centers[:, 0])
tree_lix = cKDTree(np.column_stack([cx, cy]))
density = np.zeros(L)
for i in range(len(ev_w)):
    nb = tree_lix.query_ball_point([ev_x[i], ev_y[i]], r=H)
    if not nb:
        continue
    nb = np.asarray(nb)
    d = np.hypot(cx[nb] - ev_x[i], cy[nb] - ev_y[i])
    x = d / H
    kvals = (1.0 - x * x) ** 2  # all within H, so x < 1
    np.add.at(density, nb, ev_w[i] * kvals)

pos = density[density > 0]
norm = np.percentile(pos, 98) if pos.size else 1.0
print(f"[KDE] lixel density max {density.max():.1f}  p98 {norm:.1f}  nonzero lixels {pos.size}/{L}")

# --- colour ramp YlOrRd ---
STOPS = np.array([[255,255,178],[254,217,118],[254,178,76],
                  [253,141,60],[240,59,32],[189,0,38]], dtype=float)
def ramp(t):
    t = min(max(t, 0.0), 1.0)
    x = t * (len(STOPS) - 1)
    i = int(np.floor(x)); f = x - i
    if i >= len(STOPS) - 1:
        c = STOPS[-1]
    else:
        c = STOPS[i] + (STOPS[i + 1] - STOPS[i]) * f
    return "#%02x%02x%02x" % (int(round(c[0])), int(round(c[1])), int(round(c[2])))

GREY = "#dcdcdc"
kde_segments = []
for k in range(L):
    dv = density[k]
    col = GREY if dv <= 0 else ramp(dv / norm)
    kde_segments.append({"p": seg_coords[k], "c": col})
KDE_JSON = json.dumps({"seg": kde_segments}, ensure_ascii=False)
print(f"[KDE] segments emitted: {len(kde_segments)}")

# ============================================================================
# 4b) AREA raster -- planar field rendered everywhere (isotropic, bandwidth H)
# ============================================================================
print("\n[AREA] rasterising planar density ...")

def merc_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))

def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)

W = IMG_WIDTH
mx_lo, mx_hi = np.radians(LON_MIN), np.radians(LON_MAX)
my_hi, my_lo = merc_y(LAT_MAX), merc_y(LAT_MIN)
Hpx = int(round(W * (my_hi - my_lo) / (mx_hi - mx_lo)))
lons = np.linspace(LON_MIN, LON_MAX, W)
lats = inv_merc_y(np.linspace(my_hi, my_lo, Hpx))          # row 0 = north
LON_g, LAT_g = np.meshgrid(lons, lats)
PXg, PYg = to_m(LON_g, LAT_g)                              # (Hpx, W) metric grids

# accumulate each event's isotropic quartic kernel onto the raster window it touches
val = np.zeros((Hpx, W))
dlat_deg = H / DEG
dlon_deg = H / (DEG * COSLAT0)
for i in range(len(ev_w)):
    rows = np.where(np.abs(lats - ev_lat[i]) <= dlat_deg)[0]
    cols = np.where(np.abs(lons - ev_lon[i]) <= dlon_deg)[0]
    if rows.size == 0 or cols.size == 0:
        continue
    r0, r1 = rows[0], rows[-1] + 1
    c0, c1 = cols[0], cols[-1] + 1
    bx = PXg[r0:r1, c0:c1]; by = PYg[r0:r1, c0:c1]
    d = np.hypot(bx - ev_x[i], by - ev_y[i])
    x = d / H
    kvals = np.where(x < 1.0, (1.0 - x * x) ** 2, 0.0)
    val[r0:r1, c0:c1] += ev_w[i] * kvals

t = np.clip(val / norm, 0.0, 1.0)

# colour LUT from the same YlOrRd stops; alpha rises with intensity, 0 where val==0
xs = np.linspace(0, 1, len(STOPS))
LUT = np.stack([np.interp(np.linspace(0, 1, 256), xs, STOPS[:, ch]) for ch in range(3)], axis=1)
ti = np.clip((t * 255).astype(int), 0, 255)
rgb = LUT[ti]                                              # (Hpx, W, 3)
ALPHA_MAX = 0.72
alpha = np.where(val <= 0, 0.0, np.clip(t, 0.0, 1.0) ** 0.7 * ALPHA_MAX)

rgba = np.zeros((Hpx, W, 4), dtype=np.uint8)
rgba[..., 0:3] = rgb.astype(np.uint8)
rgba[..., 3] = (alpha * 255).astype(np.uint8)
buf = io.BytesIO()
Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
AREA_IMG = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
AREA_BOUNDS_JSON = json.dumps([[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]])
print(f"[AREA] raster {W}x{Hpx}px  bandwidth {H/1000:.1f} km (isotropic)  "
      f"png {len(AREA_IMG)//1024} KB  covered px {(val>0).sum()}/{val.size}")

# ============================================================================
# 5) HTML  (baseline template + planar-KDE layer/control/legend)
# ============================================================================
HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Redispatch-Ereignisse in Deutschland (ohne Snapping)</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:270px; font-size:13px; max-height:94vh; overflow:auto;
  }
  .panel h1 { font-size:15px; margin:0 0 2px; }
  .panel .sub { color:#666; font-size:11px; margin-bottom:10px; }
  .panel label { font-weight:600; display:block; margin:10px 0 4px; }
  .row { display:flex; align-items:center; gap:8px; }
  .row input[type=range] { flex:1; }
  .szlab { width:30px; font-size:11px; color:#666; }
  #yearVal { font-weight:700; min-width:64px; text-align:right; }
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
  .legend .bar { height:10px; width:180px; border-radius:3px;
    background:linear-gradient(to right,#2c7bb6,#abd9e9,#ffffbf,#fdae61,#d7191c); }
  .legend .kbar { height:10px; width:180px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Redispatch-Ereignisse</h1>
  <div class="sub">Gezahlt nach Anzahl der Ereignisse (nicht MWh)</div>

  <label>Jahr</label>
  <div class="row">
    <input type="range" id="yearSlider" min="__YMIN__" max="__YMAX__" step="1" value="__YMAX__"/>
    <span id="yearVal">__YMAX__</span>
  </div>
  <label class="chk"><input type="checkbox" id="allYears" checked/> Alle Jahre</label>

  <label>Richtung</label>
  <div class="seg" id="dirSeg">
    <button data-d="all" class="active">Alle</button>
    <button data-d="pos">Positiv &uarr;</button>
    <button data-d="neg">Negativ &darr;</button>
  </div>

  <label>Grund der Ma&szlig;nahme</label>
  <select id="reasonSel"><option value="all">Alle Gr&uuml;nde</option></select>

  <label>Batteriespeicher (&gt;100&nbsp;kW)</label>
  <div class="seg" id="batSeg">
    <button data-b="off" class="active">Aus</button>
    <button data-b="active">Aktiv</button>
    <button data-b="planning">In Planung</button>
    <button data-b="all">Alle</button>
  </div>

  <label>Gr&ouml;&szlig;e (Leistung): <span id="batSizeVal" style="font-weight:700"></span></label>
  <div class="row"><span class="szlab">min</span><input type="range" id="batMin" min="0" max="100" step="1" value="0"/></div>
  <div class="row"><span class="szlab">max</span><input type="range" id="batMax" min="0" max="100" step="1" value="100"/></div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Redispatch-Dichte (planar, MWh)</label>
  <label class="chk"><input type="checkbox" id="kdeChk" checked/> Heatmap (Linien) anzeigen</label>
  <label class="chk"><input type="checkbox" id="areaChk" checked/> Fl&auml;che (planare KDE)</label>
  <div class="sub" style="margin:4px 0 0;">Statisch: alle Jahre &amp; Richtungen, MWh als planare KDE OHNE Snapping (isotrop, Bandbreite __BW_KM__&nbsp;km, Euklidisch).</div>

  <div class="stat">
    <div><b id="evtCount">0</b> Ereignisse</div>
    <div><span id="locCount">0</span> Standorte sichtbar</div>
    <div><span id="batCount">0</span> Batteriespeicher sichtbar</div>
  </div>
</div>

<div class="legend">
  <div>Ereignisse pro Standort</div>
  <div class="bar"></div>
  <div class="ticks"><span>1</span><span id="legMax">max</span></div>
  <div id="kdeLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Redispatch-Dichte (planar, MWh)</div>
    <div class="kbar"></div>
    <div class="ticks"><span>niedrig</span><span>hoch</span></div>
    <div style="margin-top:3px; color:#777;">Planare KDE ohne Snapping, isotrop (&asymp;__BW_KM__&nbsp;km, __BW_MODE__)</div>
  </div>
  <div id="batLegend" style="display:none; margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Batteriespeicher</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#1a9850;border:1.5px solid #fff;box-shadow:0 0 0 1px #1a9850;vertical-align:middle;"></span> aktiv (in Betrieb)</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#7b3294;border:1.5px solid #fff;box-shadow:0 0 0 1px #7b3294;vertical-align:middle;"></span> in Planung</div>
    <div style="margin-top:3px; color:#777;">Gr&ouml;&szlig;e &prop; Leistung (kW)</div>
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
const BATTERIES = __BATTERIES__;
const GRID = __GRID__;
const KDE = __KDE__;
const AREA_IMG = "__AREA_IMG__";
const AREA_BOUNDS = __AREA_BOUNDS__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

// --- transmission grid (low-z backdrop, non-interactive) ---
map.createPane('kdeAreaPane'); map.getPane('kdeAreaPane').style.zIndex = 240; // area glow, below the lines
map.createPane('gridPane');  map.getPane('gridPane').style.zIndex = 250;
map.createPane('kdePane');   map.getPane('kdePane').style.zIndex = 260;  // above grid, below markers
const gridRenderer = L.canvas({pane:'gridPane'});
const kdeRenderer  = L.canvas({pane:'kdePane'});
const gridLayer = L.layerGroup();
const kdeLayer  = L.layerGroup();
const areaLayer = L.imageOverlay(AREA_IMG, AREA_BOUNDS, {pane:'kdeAreaPane', interactive:false, opacity:1.0});
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
(function buildKde(){
  (KDE.seg||[]).forEach(s=>{
    L.polyline(s.p, {pane:'kdePane', renderer:kdeRenderer, interactive:false,
      color:s.c, weight:3.4, opacity:0.9, lineCap:'butt'}).addTo(kdeLayer); });
})();

const layer = L.layerGroup().addTo(map);
const batLayer = L.layerGroup().addTo(map);

const sel = document.getElementById('reasonSel');
DATA.reasons.forEach((r,i)=>{ const o=document.createElement('option'); o.value=i; o.textContent=r; sel.appendChild(o); });

let state = { year:'all', dir:'all', reason:'all' };
let batMode = 'off';
let batMinMW = 0.1, batMaxMW = 1000;
const SZ_LO = 0.1, SZ_HI = 1000, SZ_EXP = Math.log10(SZ_HI/SZ_LO);
function posToMW(p){ return SZ_LO * Math.pow(10, (p/100)*SZ_EXP); }
function fmtMW(v){ return v.toLocaleString('de-DE', {maximumFractionDigits: v<1?2:1}); }

const STOPS = [[44,123,182],[171,217,233],[255,255,191],[253,174,97],[215,25,28]];
function ramp(t){ t=Math.max(0,Math.min(1,t)); const x=t*(STOPS.length-1), i=Math.floor(x), f=x-i;
  if(i>=STOPS.length-1) return `rgb(${STOPS[STOPS.length-1].join(',')})`;
  const a=STOPS[i], b=STOPS[i+1]; const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f)); return `rgb(${c.join(',')})`; }

function locCount(loc){ let s=0;
  for(const [y,d,r,c] of loc.rec){
    if(state.year!=='all' && y!==state.year) continue;
    if(state.dir==='pos' && d!==1) continue;
    if(state.dir==='neg' && d!==0) continue;
    if(state.reason!=='all' && r!==state.reason) continue;
    s+=c; } return s; }

function render(){
  layer.clearLayers();
  const vals = DATA.locations.map(locCount);
  const maxC = Math.max(1, ...vals);
  let totalEvt=0, visLoc=0;
  DATA.locations.forEach((loc,idx)=>{
    const c = vals[idx]; if(c<=0) return;
    totalEvt += c; visLoc++;
    const t = Math.sqrt(c/maxC); const radius = 4 + 26*t; const col = ramp(c/maxC);
    const ylab = state.year==='all' ? 'alle Jahre' : state.year;
    const dlab = state.dir==='all' ? 'alle Richtungen' : (state.dir==='pos'?'positiv (erhoehen)':'negativ (reduzieren)');
    const rlab = state.reason==='all' ? 'alle Gruende' : DATA.reasons[state.reason];
    const loc_ = (loc.city||'') + ((loc.city&&loc.state)?', ':'') + (loc.state||'');
    L.circleMarker([loc.lat,loc.lon], {radius, color:col, weight:1, fillColor:col, fillOpacity:0.78})
      .bindPopup(`<b>${loc.name}</b><br>${loc_}<br><b>${c.toLocaleString('de-DE')}</b> Ereignisse`
        + `<br><span style="color:#777;font-size:11px">${ylab} &middot; ${dlab} &middot; ${rlab}</span>`)
      .bindTooltip(`${loc.name}: ${c.toLocaleString('de-DE')}`).addTo(layer);
  });
  document.getElementById('evtCount').textContent = totalEvt.toLocaleString('de-DE');
  document.getElementById('locCount').textContent = visLoc;
  document.getElementById('legMax').textContent = maxC.toLocaleString('de-DE');
}

const BAT_COL = {1:'#1a9850', 2:'#7b3294'};
function renderBatteries(){
  batLayer.clearLayers();
  document.getElementById('batLegend').style.display = (batMode==='off') ? 'none' : 'block';
  if(batMode==='off'){ document.getElementById('batCount').textContent = 0; return; }
  let shown = 0;
  BATTERIES.forEach(b=>{
    if(batMode==='active'   && b.s!==1) return;
    if(batMode==='planning' && b.s!==2) return;
    if(state.year!=='all' && (b.y===null || b.y>state.year)) return;
    const mw = b.kw/1000; if(mw < batMinMW || mw > batMaxMW) return;
    shown++;
    const radius = 4 + Math.min(16, Math.sqrt(mw)*2); const col = BAT_COL[b.s];
    const slab = b.s===1 ? 'aktiv (in Betrieb)' : 'in Planung';
    const loc_ = (b.city||'') + ((b.city&&b.state)?', ':'') + (b.state||'');
    const dlab = b.date ? `<br><span style="color:#777;font-size:11px">${b.s===1?'Inbetriebnahme':'geplant'}: ${b.date}</span>` : '';
    L.circleMarker([b.lat,b.lon], {radius, color:'#fff', weight:1.5, fillColor:col, fillOpacity:0.9})
      .bindPopup(`<b>${b.name}</b><br>${loc_}<br><b>${mw.toLocaleString('de-DE',{maximumFractionDigits:1})}</b> MW &middot; ${slab}${dlab}`)
      .bindTooltip(`${b.name}: ${mw.toLocaleString('de-DE',{maximumFractionDigits:1})} MW`).addTo(batLayer);
  });
  document.getElementById('batCount').textContent = shown.toLocaleString('de-DE');
}

const slider = document.getElementById('yearSlider');
const yearVal = document.getElementById('yearVal');
const allYears = document.getElementById('allYears');
function syncYear(){
  if(allYears.checked){ state.year='all'; slider.disabled=true; yearVal.textContent='Alle'; }
  else { slider.disabled=false; state.year=parseInt(slider.value); yearVal.textContent=slider.value; }
  render(); renderBatteries();
}
slider.addEventListener('input', ()=>{ if(!allYears.checked){ state.year=parseInt(slider.value); yearVal.textContent=slider.value; render(); renderBatteries(); } });
allYears.addEventListener('change', syncYear);
document.querySelectorAll('#dirSeg button').forEach(b=>{ b.addEventListener('click', ()=>{
  document.querySelectorAll('#dirSeg button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); state.dir=b.dataset.d; render(); }); });
sel.addEventListener('change', ()=>{ state.reason = sel.value==='all' ? 'all' : parseInt(sel.value); render(); });
document.querySelectorAll('#batSeg button').forEach(b=>{ b.addEventListener('click', ()=>{
  document.querySelectorAll('#batSeg button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); batMode=b.dataset.b; renderBatteries(); }); });
const batMin = document.getElementById('batMin'), batMax = document.getElementById('batMax');
function syncBatSize(){
  const lo = Math.min(+batMin.value, +batMax.value), hi = Math.max(+batMin.value, +batMax.value);
  batMinMW = posToMW(lo); batMaxMW = posToMW(hi);
  document.getElementById('batSizeVal').textContent = `${fmtMW(batMinMW)} – ${fmtMW(batMaxMW)} MW`;
  renderBatteries();
}
batMin.addEventListener('input', syncBatSize);
batMax.addEventListener('input', syncBatSize);

// --- grid toggle ---
const gridChk = document.getElementById('gridChk');
function syncGrid(){
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else { if(map.hasLayer(gridLayer)) map.removeLayer(gridLayer); }
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block' : 'none';
}
gridChk.addEventListener('change', syncGrid);

// --- KDE toggle ---
const kdeChk = document.getElementById('kdeChk');
function syncKde(){
  if(kdeChk.checked){ if(!map.hasLayer(kdeLayer)) kdeLayer.addTo(map); }
  else { if(map.hasLayer(kdeLayer)) map.removeLayer(kdeLayer); }
  document.getElementById('kdeLegend').style.display = kdeChk.checked ? 'block' : 'none';
}
kdeChk.addEventListener('change', syncKde);

// --- off-grid area toggle ---
const areaChk = document.getElementById('areaChk');
function syncArea(){
  if(areaChk.checked){ if(!map.hasLayer(areaLayer)) areaLayer.addTo(map); }
  else { if(map.hasLayer(areaLayer)) map.removeLayer(areaLayer); }
}
areaChk.addEventListener('change', syncArea);

syncYear(); syncBatSize(); syncGrid(); syncKde(); syncArea();
</script>
</body>
</html>"""

html = (
    HTML.replace("__DATA__", DATA_JSON)
    .replace("__BATTERIES__", BAT_JSON)
    .replace("__GRID__", GRID_JSON)
    .replace("__KDE__", KDE_JSON)
    .replace("__AREA_IMG__", AREA_IMG)
    .replace("__AREA_BOUNDS__", AREA_BOUNDS_JSON)
    .replace("__YMIN__", str(years[0]))
    .replace("__YMAX__", str(years[-1]))
    .replace("__BW_KM__", f"{BANDWIDTH_KM:.1f}")
    .replace("__BW_MODE__", "Silverman" if BANDWIDTH_MODE == "silverman" else "manuell")
)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"\nsaved -> {OUT}")
