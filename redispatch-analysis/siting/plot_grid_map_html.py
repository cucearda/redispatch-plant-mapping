"""Render the artificial-grid siting scores as an interactive HTML heatmap.

Same look as make_redispatch_kde_map.py: a single self-contained Leaflet page
with CARTO light tiles, the 380/220 kV grid backdrop, a control panel and a
YlOrRd legend. Each lattice cell is drawn as a coloured rectangle whose shade
encodes its locational value dV. Standalone -- reads a ranking CSV produced by
`battery_siting_score.run` (battery.source='grid') and never touches the scorer.

Usage:
    python -m battery_siting_score.plot_grid_map_html               # latest CSV in out/
    python -m battery_siting_score.plot_grid_map_html --csv <path>
    python -m battery_siting_score.plot_grid_map_html --col dV_proxy_eur_per_year
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Allow running as a standalone script (`python plot_grid_map_html.py`) as well
# as a package module (`python -m battery_siting_score.plot_grid_map_html`) --
# same fallback pattern as run.py. Without this, running the file directly
# fails immediately with "ImportError: attempted relative import with no
# known parent package" before any of the actual map-building code runs.
if __package__ in (None, ""):
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE.parent))
    from battery_siting_score.plot_grid_map import _latest_csv, _pick_value_col, DEFAULT_OUT_DIR
else:
    from .plot_grid_map import _latest_csv, _pick_value_col, DEFAULT_OUT_DIR

HERE = Path(__file__).resolve().parent
GRID_DIR = HERE.parent.parent / "grid_data"   # TUM/Seminar EM/grid_data
REDISPATCH_CSV = HERE.parent / "redispatch_joined_high_confidence.csv"  # ALLES_NEU/
Q = "'"


def _build_redispatch_events(since: str = "2021-10-01") -> dict:
    """Aggregate redispatch events (>= `since`) per location for an optional overlay,
    split by direction (RICHTUNG) so the map can draw up-regulation (Wirkleistungs-
    einspeisung erhoehen -> discharge signal) in red and down-regulation
    (reduzieren -> charge signal) in blue.

    Mirrors the scorer's input file and window start (Redispatch 2.0, 2021-10-01):
    keeps rows with duration_h > 0 and ts_start >= since, then groups per (lat, lon)
    x direction into event count and summed |GESAMTE_ARBEIT_MWH|. Purely a
    grid-stress backdrop -- it does NOT feed the scoring and is off by default
    in the UI.
    """
    out = {"events": [], "since": since, "n_events": 0, "mwh_up_p98": 1.0, "mwh_down_p98": 1.0}
    if not REDISPATCH_CSV.exists():
        print(f"[redispatch] overlay skipped (file not found at {REDISPATCH_CSV})")
        return out
    df = pd.read_csv(REDISPATCH_CSV, sep=";", low_memory=False, encoding="utf-8-sig")
    df = df[pd.to_numeric(df["duration_h"], errors="coerce") > 0]
    df = df.dropna(subset=["lat", "lon"])
    ts = pd.to_datetime(df["ts_start"], errors="coerce")
    df = df[ts >= pd.Timestamp(since)]
    df["mwh"] = pd.to_numeric(df["GESAMTE_ARBEIT_MWH"], errors="coerce").abs().fillna(0.0)
    r = df["RICHTUNG"].astype(str).str.strip()
    df["dir"] = np.where(r.str.contains("erh", case=False, na=False), "up",
                  np.where(r.str.contains("reduzier", case=False, na=False), "down", "other"))
    df["glat"] = df["lat"].round(5)
    df["glon"] = df["lon"].round(5)

    up = (df[df["dir"] == "up"].groupby(["glat", "glon"])
            .agg(n_up=("mwh", "size"), mwh_up=("mwh", "sum")))
    down = (df[df["dir"] == "down"].groupby(["glat", "glon"])
              .agg(n_down=("mwh", "size"), mwh_down=("mwh", "sum")))
    g = up.join(down, how="outer").fillna(0.0).reset_index()
    g["n_up"] = g["n_up"].astype(int)
    g["n_down"] = g["n_down"].astype(int)

    if "plant_name" in df.columns:
        names = (df.dropna(subset=["plant_name"])
                   .groupby(["glat", "glon"])["plant_name"].first())
    else:
        names = pd.Series(dtype=object)

    mwh_up_p98 = float(np.percentile(g["mwh_up"][g["mwh_up"] > 0], 98)) if (g["mwh_up"] > 0).any() else 1.0
    mwh_down_p98 = float(np.percentile(g["mwh_down"][g["mwh_down"] > 0], 98)) if (g["mwh_down"] > 0).any() else 1.0

    events = [{
        "lat": float(row.glat), "lon": float(row.glon),
        "n_up": int(row.n_up), "mwh_up": round(float(row.mwh_up), 1),
        "n_down": int(row.n_down), "mwh_down": round(float(row.mwh_down), 1),
        "name": str(names.get((row.glat, row.glon), "") or "Redispatch-Standort"),
    } for row in g.itertuples(index=False)]
    out.update(events=events, n_events=int(len(df)),
               mwh_up_p98=round(mwh_up_p98, 1), mwh_down_p98=round(mwh_down_p98, 1))
    print(f"[redispatch] overlay: {len(df)} events since {since} at {len(events)} locations "
          f"(up p98 {mwh_up_p98:,.0f} MWh, down p98 {mwh_down_p98:,.0f} MWh)")
    return out


def _build_grid_backdrop() -> dict:
    """AC 380/220 kV lines with both ends in DE (same backdrop as the KDE map)."""
    from shapely import wkt
    grid = {"ac": []}
    try:
        gb = pd.read_csv(GRID_DIR / "buses.csv")
        de_ids = set(gb[gb.country == "DE"].bus_id)
        gl = pd.read_csv(GRID_DIR / "lines.csv", quotechar=Q)
    except FileNotFoundError:
        print(f"[grid] backdrop skipped (grid_data not found at {GRID_DIR})")
        return grid

    def simp_path(g_wkt):
        g = wkt.loads(g_wkt).simplify(0.004, preserve_topology=False)
        return [[round(y, 5), round(x, 5)] for x, y in g.coords]

    both_de = gl.bus0.isin(de_ids) & gl.bus1.isin(de_ids)
    for _, r in gl[both_de].iterrows():
        grid["ac"].append({"v": int(r.voltage), "p": simp_path(r.geometry)})
    print(f"[grid] backdrop AC lines: {len(grid['ac'])}")
    return grid


def build_html(csv_path: Path, value_col: str | None = None,
               out_html: Path | None = None) -> Path:
    df = pd.read_csv(csv_path)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col]).reset_index(drop=True)

    # Cell size from the lattice spacing (regular grid).
    ulon = np.unique(np.round(df["lon"].to_numpy(), 4))
    ulat = np.unique(np.round(df["lat"].to_numpy(), 4))
    dlon = float(np.median(np.diff(ulon))) if len(ulon) > 1 else 0.25
    dlat = float(np.median(np.diff(ulat))) if len(ulat) > 1 else 0.25

    # Normalise to p98 of positive values (robust, like the KDE map).
    vals = df[col].to_numpy(dtype=float)
    pos = vals[vals > 0]
    norm = float(np.percentile(pos, 98)) if pos.size else 1.0
    t = np.clip(vals / max(norm, 1e-9), 0.0, 1.0)

    rank_col = "rank_by_dV" if "rank_by_dV" in df.columns else None
    name_col = "name" if "name" in df.columns else None

    cells = []
    for i in range(len(df)):
        cells.append({
            "lat": round(float(df["lat"].iat[i]), 5),
            "lon": round(float(df["lon"].iat[i]), 5),
            "v": round(float(vals[i]) / 1e6, 3),           # million EUR/yr
            "t": round(float(t[i]), 4),                     # 0..1 colour position
            "r": int(df[rank_col].iat[i]) if rank_col else i + 1,
            "n": str(df[name_col].iat[i]) if name_col else f"cell_{i}",
        })

    payload = {
        "cells": cells,
        "dlat": round(dlat, 5), "dlon": round(dlon, 5),
        "col": col, "norm_million": round(norm / 1e6, 3),
        "vmax_million": round(float(vals.max()) / 1e6, 3),
        "n": len(cells),
    }
    DATA_JSON = json.dumps(payload, ensure_ascii=False)
    GRID_JSON = json.dumps(_build_grid_backdrop(), ensure_ascii=False)
    EVENTS_JSON = json.dumps(_build_redispatch_events(), ensure_ascii=False)

    html = (_HTML.replace("__DATA__", DATA_JSON)
                 .replace("__GRID__", GRID_JSON)
                 .replace("__EVENTS__", EVENTS_JSON))
    if out_html is None:
        out_html = csv_path.with_name("battery_siting_heatmap_germany.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[html] value column: {col}")
    print(f"[html] {len(cells)} cells, cell size {dlat:.3f} x {dlon:.3f} deg, "
          f"p98 norm {norm/1e6:.2f} M EUR/yr, max {vals.max()/1e6:.2f} M EUR/yr")
    print(f"[html] -> {out_html}")
    return out_html


_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Batterie-Standortwert Deutschland (k&uuml;nstliches Gitter)</title>
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
  .chk { font-weight:400; display:flex; align-items:center; gap:6px; margin-top:6px; }
  input[type=range] { width:100%; }
  .stat { margin-top:12px; padding-top:10px; border-top:1px solid #eee; font-size:12px; }
  .stat b { font-size:15px; }
  .legend {
    position:absolute; bottom:18px; right:12px; z-index:1000; background:#fff;
    padding:8px 12px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3); font-size:11px;
  }
  .legend .kbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Batterie-Standortwert</h1>
  <div class="sub">K&uuml;nstliches Gitter aus Kandidatenstandorten. Farbe = lokaler
  Arbitragewert dV (pro 50&nbsp;MW-Kandidat) aus dem Redispatch-Keil.</div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Wertekarte (Gitterzellen)</label>
  <label class="chk"><input type="checkbox" id="cellChk" checked/> Heatmap anzeigen</label>
  <label class="chk"><input type="checkbox" id="topChk" checked/> Top-10 Standorte markieren</label>

  <label>Redispatch-Ereignisse (seit 01.10.2021)</label>
  <label class="chk"><input type="checkbox" id="evtChk"/> Ereignisse anzeigen (Kreisgr&ouml;&szlig;e = MWh)</label>

  <label>Deckkraft</label>
  <input type="range" id="opac" min="0" max="100" value="72"/>

  <div class="stat">
    <div><b id="nCells">0</b> Kandidatenzellen</div>
    <div>Max dV: <b id="vMax">0</b> Mio.&nbsp;&euro;/Jahr</div>
    <div>Normierung (p98): <span id="vNorm">0</span> Mio.&nbsp;&euro;/Jahr</div>
    <div class="sub" style="margin-top:6px;" id="colName"></div>
  </div>
</div>

<div class="legend">
  <div id="kdeLabel">Standortwert dV (Mio.&nbsp;&euro;/Jahr, pro 50&nbsp;MW)</div>
  <div class="kbar"></div>
  <div class="ticks"><span>niedrig</span><span id="legMax">hoch</span></div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Stromnetz</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
  </div>
  <div id="evtLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee; display:none;">
    <div>Redispatch-Ereignisse (seit 01.10.2021)</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#de2d26;opacity:.7;vertical-align:middle;"></span> Hochregeln (erh&ouml;hen) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#3182bd;opacity:.7;vertical-align:middle;"></span> Runterregeln (reduzieren) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
  </div>
</div>

<script>
const DATA = __DATA__;
const GRID = __GRID__;
const EVENTS = __EVENTS__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

// panes: grid under cells, top markers on top
map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 240;
map.createPane('cellPane'); map.getPane('cellPane').style.zIndex = 250;
map.createPane('evtPane');  map.getPane('evtPane').style.zIndex = 270;
const gridRenderer = L.canvas({pane:'gridPane'});
const cellRenderer = L.canvas({pane:'cellPane'});
const evtRenderer  = L.canvas({pane:'evtPane'});

// YlOrRd ramp (same palette family as the redispatch KDE map)
const STOPS = [[255,255,178],[254,217,118],[254,178,76],[253,141,60],[240,59,32],[189,0,38]];
function ramp(t){ t=Math.max(0,Math.min(1,t));
  const x=t*(STOPS.length-1), i=Math.floor(x), f=x-i;
  const a=STOPS[i], b=STOPS[Math.min(i+1,STOPS.length-1)];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}

// grid backdrop
const gridLayer = L.layerGroup();
(GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
  L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
    color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.55}).addTo(gridLayer); });

// value cells (rectangles on the regular lattice)
const halfLat = DATA.dlat/2, halfLon = DATA.dlon/2;
const cellLayer = L.layerGroup();
let fillOpacity = 0.72;
const rects = [];
DATA.cells.forEach(c=>{
  const bounds = [[c.lat-halfLat, c.lon-halfLon],[c.lat+halfLat, c.lon+halfLon]];
  const col = ramp(c.t);
  const rect = L.rectangle(bounds, {pane:'cellPane', renderer:cellRenderer, interactive:true,
    stroke:false, fillColor:col, fillOpacity:fillOpacity})
    .bindPopup(`<b>${c.n}</b> &middot; Rang ${c.r}<br><b>${c.v.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> Mio.&nbsp;&euro;/Jahr`
      + `<br><span style="color:#777;font-size:11px">${c.lat.toFixed(3)}&deg;N, ${c.lon.toFixed(3)}&deg;E</span>`)
    .bindTooltip(`${c.v.toLocaleString('de-DE',{maximumFractionDigits:1})} Mio. €/Jahr`);
  rect.addTo(cellLayer);
  rects.push(rect);
});

// top-10 markers
const topLayer = L.layerGroup();
[...DATA.cells].sort((a,b)=>a.r-b.r).slice(0,10).forEach(c=>{
  L.circleMarker([c.lat,c.lon], {pane:'cellPane', radius:7, color:'#111', weight:2,
    fill:false}).bindTooltip(`Rang ${c.r}: ${c.n}`).addTo(topLayer);
});

// redispatch events overlay (off by default) -- grid-stress backdrop, not scored
const evtLayer = L.layerGroup();
(function buildEvents(){
  const p98Down = Math.max(1e-9, EVENTS.mwh_down_p98||1);
  const p98Up = Math.max(1e-9, EVENTS.mwh_up_p98||1);
  (EVENTS.events||[]).forEach(e=>{
    if(e.mwh_down > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_down / p98Down));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#08519c', weight:0.8, fillColor:'#3182bd', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Runterregeln (reduzieren)<br><b>${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_down.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh runter (${e.n_down} Ereignisse)`)
        .addTo(evtLayer);
    }
    if(e.mwh_up > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_up / p98Up));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#a50f15', weight:0.8, fillColor:'#de2d26', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Hochregeln (erh&ouml;hen)<br><b>${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_up.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh hoch (${e.n_up} Ereignisse)`)
        .addTo(evtLayer);
    }
  });
})();

gridLayer.addTo(map); cellLayer.addTo(map); topLayer.addTo(map);

// stats
document.getElementById('nCells').textContent = DATA.n.toLocaleString('de-DE');
document.getElementById('vMax').textContent = DATA.vmax_million.toLocaleString('de-DE',{maximumFractionDigits:2});
document.getElementById('vNorm').textContent = DATA.norm_million.toLocaleString('de-DE',{maximumFractionDigits:2});
document.getElementById('legMax').textContent = '≥ ' + DATA.norm_million.toLocaleString('de-DE',{maximumFractionDigits:1});
document.getElementById('colName').textContent = 'Spalte: ' + DATA.col;

// toggles
const gridChk = document.getElementById('gridChk');
gridChk.addEventListener('change', ()=>{
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else map.removeLayer(gridLayer);
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block':'none';
});
const cellChk = document.getElementById('cellChk');
cellChk.addEventListener('change', ()=>{
  if(cellChk.checked){ if(!map.hasLayer(cellLayer)) cellLayer.addTo(map); }
  else map.removeLayer(cellLayer);
});
const topChk = document.getElementById('topChk');
topChk.addEventListener('change', ()=>{
  if(topChk.checked){ if(!map.hasLayer(topLayer)) topLayer.addTo(map); }
  else map.removeLayer(topLayer);
});
const evtChk = document.getElementById('evtChk');
evtChk.addEventListener('change', ()=>{
  if(evtChk.checked){ if(!map.hasLayer(evtLayer)) evtLayer.addTo(map); }
  else map.removeLayer(evtLayer);
  document.getElementById('evtLegend').style.display = evtChk.checked ? 'block' : 'none';
});
const opac = document.getElementById('opac');
opac.addEventListener('input', ()=>{
  fillOpacity = opac.value/100;
  rects.forEach(r=>r.setStyle({fillOpacity}));
});
</script>
</body>
</html>"""


def _cell_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(float(lat), 5), round(float(lon), 5))


def _load_sweep_frame(csv_path: Path, value_col: str | None,
                       base_keys: list[tuple[float, float]]) -> dict:
    """Load one sweep run's CSV and reindex it onto the shared `base_keys` lattice
    (same (lat, lon) cell order for every h, so the map's rectangles can be built
    once and just re-colored on slider change instead of rebuilt per frame)."""
    df = pd.read_csv(csv_path)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col]).reset_index(drop=True)

    rank_col = "rank_by_dV" if "rank_by_dV" in df.columns else None
    by_key = {}
    for i in range(len(df)):
        k = _cell_key(df["lat"].iat[i], df["lon"].iat[i])
        by_key[k] = (float(df[col].iat[i]), int(df[rank_col].iat[i]) if rank_col else i + 1)

    n_missing = sum(1 for k in base_keys if k not in by_key)
    if n_missing:
        print(f"[sweep] warning: {csv_path.name} missing {n_missing}/{len(base_keys)} "
              f"cells from the reference lattice (values set to null)")

    v_million = []
    rank = []
    for k in base_keys:
        if k in by_key:
            val, r = by_key[k]
            v_million.append(round(val / 1e6, 4))
            rank.append(r)
        else:
            v_million.append(None)
            rank.append(None)

    finite_vals = [v for v in v_million if v is not None]
    pos = [v for v in finite_vals if v > 0]
    norm = float(np.percentile(pos, 98)) if pos else 1.0
    vmax = float(max(finite_vals)) if finite_vals else 0.0
    return {"v": v_million, "rank": rank, "norm_million": round(norm, 4), "vmax_million": round(vmax, 4)}


def build_sweep_html(sweep_dir: Path, value_col: str | None = None,
                      out_html: Path | None = None, baseline_param: float = 1.0) -> Path:
    """Build ONE self-contained HTML page with a slider that scrubs across every
    run of a parameter sweep (as produced by the h_sweep notebook), re-coloring
    the same lattice of candidate cells and re-marking the top-10 sites for
    whichever run is currently selected -- so you can see how the best sites
    shift as the swept parameter (e.g. kernel.ptdf_bandwidth) changes.

    Expects `sweep_dir` to contain `sweep_summary.csv` (columns: h, status,
    out_dir, ...) and, in each successful run's `out_dir`, exactly one
    `battery_siting_scores_*.csv` (written by `run.main()`).
    """
    sweep_dir = Path(sweep_dir)
    summary_path = sweep_dir / "sweep_summary.csv"
    summary = pd.read_csv(summary_path)
    ok = summary[summary["status"] == "ok"].sort_values("h").reset_index(drop=True)
    if ok.empty:
        raise RuntimeError(f"No successful runs in {summary_path}")

    run_csvs = []
    for _, row in ok.iterrows():
        matches = sorted(Path(row["out_dir"]).glob("battery_siting_scores_*.csv"))
        if not matches:
            raise FileNotFoundError(f"No battery_siting_scores_*.csv in {row['out_dir']}")
        run_csvs.append(matches[-1])

    # Reference lattice (lat/lon + cell size) from the first successful run --
    # the grid source is independent of h, so every run shares the same cells.
    ref_df = pd.read_csv(run_csvs[0])
    ref_col = _pick_value_col(ref_df, value_col)
    ref_df = ref_df.dropna(subset=["lat", "lon", ref_col]).reset_index(drop=True)
    ulon = np.unique(np.round(ref_df["lon"].to_numpy(), 4))
    ulat = np.unique(np.round(ref_df["lat"].to_numpy(), 4))
    dlon = float(np.median(np.diff(ulon))) if len(ulon) > 1 else 0.25
    dlat = float(np.median(np.diff(ulat))) if len(ulat) > 1 else 0.25

    base_keys = sorted({_cell_key(la, lo) for la, lo in zip(ref_df["lat"], ref_df["lon"])})
    cells_meta = [{"lat": k[0], "lon": k[1]} for k in base_keys]

    frames = []
    for h_val, csv_path in zip(ok["h"].tolist(), run_csvs):
        frame = _load_sweep_frame(csv_path, value_col, base_keys)
        frame["h"] = float(h_val)
        frames.append(frame)

    default_idx = int(np.argmin(np.abs(ok["h"].to_numpy() - baseline_param)))

    summary_points = [
        {"h": float(r["h"]),
         "mean_dV_million": (round(float(r["mean_dV"]) / 1e6, 4) if pd.notna(r.get("mean_dV")) else None),
         "top_dV_million": (round(float(r["top_dV"]) / 1e6, 4) if pd.notna(r.get("top_dV")) else None)}
        for _, r in ok.iterrows()
    ]

    payload = {"n_cells": len(cells_meta), "dlat": round(dlat, 5), "dlon": round(dlon, 5), "col": ref_col}

    CELLS_JSON = json.dumps(cells_meta, ensure_ascii=False)
    FRAMES_JSON = json.dumps(frames, ensure_ascii=False)
    SUMMARY_JSON = json.dumps(summary_points, ensure_ascii=False)
    META_JSON = json.dumps(payload, ensure_ascii=False)
    GRID_JSON = json.dumps(_build_grid_backdrop(), ensure_ascii=False)
    EVENTS_JSON = json.dumps(_build_redispatch_events(), ensure_ascii=False)

    html = (_SWEEP_HTML
            .replace("__CELLS__", CELLS_JSON)
            .replace("__FRAMES__", FRAMES_JSON)
            .replace("__SUMMARY__", SUMMARY_JSON)
            .replace("__META__", META_JSON)
            .replace("__GRID__", GRID_JSON)
            .replace("__EVENTS__", EVENTS_JSON)
            .replace("__DEFAULT_IDX__", str(default_idx)))

    if out_html is None:
        out_html = sweep_dir / "h_sweep_map.html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[sweep-html] value column: {ref_col}")
    print(f"[sweep-html] {len(frames)} frames (h = {frames[0]['h']:.4g} .. {frames[-1]['h']:.4g}), "
          f"{len(cells_meta)} cells each")
    print(f"[sweep-html] -> {out_html}")
    return out_html


_SWEEP_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Batterie-Standortwert &ndash; Parameter-Sweep</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:300px; font-size:13px; max-height:94vh; overflow:auto;
  }
  .panel h1 { font-size:15px; margin:0 0 2px; }
  .panel .sub { color:#666; font-size:11px; margin-bottom:10px; }
  .panel label { font-weight:600; display:block; margin:10px 0 4px; }
  .chk { font-weight:400; display:flex; align-items:center; gap:6px; margin-top:6px; }
  input[type=range] { width:100%; }
  .stat { margin-top:12px; padding-top:10px; border-top:1px solid #eee; font-size:12px; }
  .stat b { font-size:15px; }
  .sweepbox { padding:10px; background:#f7f7fb; border-radius:6px; border:1px solid #e6e6f0; }
  .sweepbox .hval { font-size:20px; font-weight:700; color:#222; }
  .sweepbox .hsub { font-size:11px; color:#777; }
  .playbar { display:flex; gap:6px; align-items:center; margin-top:8px; }
  .playbar button {
    border:1px solid #ccc; background:#fff; border-radius:5px; padding:4px 10px;
    font-size:12px; cursor:pointer;
  }
  .playbar button:hover { background:#f0f0f5; }
  #spark { width:100%; height:54px; margin-top:8px; cursor:pointer; }
  .legend {
    position:absolute; bottom:18px; right:12px; z-index:1000; background:#fff;
    padding:8px 12px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3); font-size:11px;
  }
  .legend .kbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Batterie-Standortwert</h1>
  <div class="sub">Parameter-Sweep: wie &auml;ndern sich die besten Standorte mit
  dem PTDF-Kernel-Bandbreiten-Parameter <code>h</code>? Farbe/Ranking je Zelle
  werden pro Sweep-Lauf neu eingef&auml;rbt.</div>

  <div class="sweepbox">
    <label style="margin-top:0;">Kernel-Bandbreite h</label>
    <div class="hval" id="hVal">-</div>
    <div class="hsub" id="hSub">Lauf - / -</div>
    <input type="range" id="hSlider" min="0" max="0" value="0" step="1"/>
    <div class="playbar">
      <button id="prevBtn">&laquo; zur&uuml;ck</button>
      <button id="playBtn">&#9654; Abspielen</button>
      <button id="nextBtn">weiter &raquo;</button>
    </div>
    <svg id="spark" viewBox="0 0 300 54" preserveAspectRatio="none"></svg>
    <div class="hsub">grau: mean dV je Lauf &middot; orange: top dV je Lauf</div>
  </div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Wertekarte (Gitterzellen)</label>
  <label class="chk"><input type="checkbox" id="cellChk" checked/> Heatmap anzeigen</label>
  <label class="chk"><input type="checkbox" id="topChk" checked/> Top-10 Standorte markieren</label>

  <label>Redispatch-Ereignisse (seit 01.10.2021)</label>
  <label class="chk"><input type="checkbox" id="evtChk"/> Ereignisse anzeigen (Kreisgr&ouml;&szlig;e = MWh)</label>

  <label>Deckkraft</label>
  <input type="range" id="opac" min="0" max="100" value="72"/>

  <div class="stat">
    <div><b id="nCells">0</b> Kandidatenzellen</div>
    <div>Max dV (dieser Lauf): <b id="vMax">0</b> Mio.&nbsp;&euro;/Jahr</div>
    <div>Normierung (p98, dieser Lauf): <span id="vNorm">0</span> Mio.&nbsp;&euro;/Jahr</div>
    <div class="sub" style="margin-top:6px;" id="colName"></div>
  </div>
</div>

<div class="legend">
  <div id="kdeLabel">Standortwert dV (Mio.&nbsp;&euro;/Jahr, pro 50&nbsp;MW)</div>
  <div class="kbar"></div>
  <div class="ticks"><span>niedrig</span><span id="legMax">hoch</span></div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Stromnetz</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
  </div>
  <div id="evtLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee; display:none;">
    <div>Redispatch-Ereignisse (seit 01.10.2021)</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#de2d26;opacity:.7;vertical-align:middle;"></span> Hochregeln (erh&ouml;hen) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#3182bd;opacity:.7;vertical-align:middle;"></span> Runterregeln (reduzieren) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
  </div>
</div>

<script>
const CELLS = __CELLS__;
const FRAMES = __FRAMES__;
const SUMMARY = __SUMMARY__;
const META = __META__;
const GRID = __GRID__;
const EVENTS = __EVENTS__;
const DEFAULT_IDX = __DEFAULT_IDX__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 240;
map.createPane('cellPane'); map.getPane('cellPane').style.zIndex = 250;
map.createPane('evtPane');  map.getPane('evtPane').style.zIndex = 270;
const gridRenderer = L.canvas({pane:'gridPane'});
const cellRenderer = L.canvas({pane:'cellPane'});
const evtRenderer  = L.canvas({pane:'evtPane'});

const STOPS = [[255,255,178],[254,217,118],[254,178,76],[253,141,60],[240,59,32],[189,0,38]];
function ramp(t){ t=Math.max(0,Math.min(1,t));
  const x=t*(STOPS.length-1), i=Math.floor(x), f=x-i;
  const a=STOPS[i], b=STOPS[Math.min(i+1,STOPS.length-1)];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}

const gridLayer = L.layerGroup();
(GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
  L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
    color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.55}).addTo(gridLayer); });

// value cells: build the rectangles ONCE (same lattice for every h); slider
// changes only re-color / re-tooltip them.
const halfLat = META.dlat/2, halfLon = META.dlon/2;
const cellLayer = L.layerGroup();
let fillOpacity = 0.72;
const rects = CELLS.map(c=>{
  const bounds = [[c.lat-halfLat, c.lon-halfLon],[c.lat+halfLat, c.lon+halfLon]];
  const rect = L.rectangle(bounds, {pane:'cellPane', renderer:cellRenderer, interactive:true,
    stroke:false, fillColor:'#ccc', fillOpacity}).bindTooltip('').bindPopup('');
  rect.addTo(cellLayer);
  return rect;
});

// top-10 markers: 10 persistent circleMarkers, repositioned per frame.
const topLayer = L.layerGroup();
const topMarkers = Array.from({length:10}, () =>
  L.circleMarker([0,0], {pane:'cellPane', radius:7, color:'#111', weight:2, fill:false})
   .addTo(topLayer)
);

const evtLayer = L.layerGroup();
(function buildEvents(){
  const p98Down = Math.max(1e-9, EVENTS.mwh_down_p98||1);
  const p98Up = Math.max(1e-9, EVENTS.mwh_up_p98||1);
  (EVENTS.events||[]).forEach(e=>{
    if(e.mwh_down > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_down / p98Down));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#08519c', weight:0.8, fillColor:'#3182bd', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Runterregeln (reduzieren)<br><b>${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_down.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh runter (${e.n_down} Ereignisse)`)
        .addTo(evtLayer);
    }
    if(e.mwh_up > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_up / p98Up));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#a50f15', weight:0.8, fillColor:'#de2d26', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Hochregeln (erh&ouml;hen)<br><b>${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_up.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh hoch (${e.n_up} Ereignisse)`)
        .addTo(evtLayer);
    }
  });
})();

gridLayer.addTo(map); cellLayer.addTo(map); topLayer.addTo(map);
document.getElementById('nCells').textContent = CELLS.length.toLocaleString('de-DE');
document.getElementById('colName').textContent = 'Spalte: ' + META.col;

// ---- sparkline (mean dV + top dV vs h), plain SVG, click-to-seek ----
const spark = document.getElementById('spark');
const SPARK_W = 300, SPARK_H = 54, PAD = 4;
function sparkX(i){ return PAD + (SPARK_W-2*PAD) * (SUMMARY.length>1 ? i/(SUMMARY.length-1) : 0); }
function sparkPath(key){
  const vals = SUMMARY.map(p=>p[key]).filter(v=>v!=null);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = (hi-lo)||1;
  return SUMMARY.map((p,i)=>{
    const v = p[key]==null ? lo : p[key];
    const y = SPARK_H - PAD - (SPARK_H-2*PAD)*((v-lo)/span);
    return `${i===0?'M':'L'}${sparkX(i).toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
}
spark.innerHTML = `
  <path d="${sparkPath('mean_dV_million')}" fill="none" stroke="#999" stroke-width="1.5"/>
  <path d="${sparkPath('top_dV_million')}" fill="none" stroke="#f03b20" stroke-width="1.5"/>
  <line id="sparkCursor" x1="0" y1="0" x2="0" y2="${SPARK_H}" stroke="#333" stroke-width="1" stroke-dasharray="2,2"/>
`;
const sparkCursor = document.getElementById('sparkCursor');
spark.addEventListener('click', (ev)=>{
  const rect = spark.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (ev.clientX-rect.left)/rect.width));
  const idx = Math.round(frac*(FRAMES.length-1));
  slider.value = idx;
  applyFrame(idx);
});

// ---- frame application ----
const slider = document.getElementById('hSlider');
slider.min = 0; slider.max = FRAMES.length-1; slider.step = 1;

function applyFrame(idx){
  const f = FRAMES[idx];
  document.getElementById('hVal').textContent = 'h = ' + f.h.toLocaleString('de-DE',{maximumFractionDigits:4});
  document.getElementById('hSub').textContent = `Lauf ${idx+1} / ${FRAMES.length}`;
  document.getElementById('vMax').textContent = f.vmax_million.toLocaleString('de-DE',{maximumFractionDigits:2});
  document.getElementById('vNorm').textContent = f.norm_million.toLocaleString('de-DE',{maximumFractionDigits:2});
  document.getElementById('legMax').textContent = '≥ ' + f.norm_million.toLocaleString('de-DE',{maximumFractionDigits:1});

  const norm = Math.max(f.norm_million, 1e-9);
  const topIdx = [];
  for (let i=0;i<CELLS.length;i++){
    const v = f.v[i];
    const rect = rects[i];
    if (v==null){ rect.setStyle({fillColor:'#eee', fillOpacity:0.15}); rect.unbindTooltip(); rect.unbindPopup(); continue; }
    const t = Math.max(0, Math.min(1, v/norm));
    rect.setStyle({fillColor:ramp(t), fillOpacity});
    rect.setTooltipContent(`${v.toLocaleString('de-DE',{maximumFractionDigits:2})} Mio. €/Jahr (Rang ${f.rank[i]})`);
    rect.setPopupContent(`Rang ${f.rank[i]}<br><b>${v.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> Mio.&nbsp;&euro;/Jahr`
      + `<br><span style="color:#777;font-size:11px">${CELLS[i].lat.toFixed(3)}&deg;N, ${CELLS[i].lon.toFixed(3)}&deg;E</span>`);
    if (f.rank[i] != null && f.rank[i] <= 10) topIdx.push(i);
  }
  topIdx.sort((a,b)=>f.rank[a]-f.rank[b]);
  topMarkers.forEach((m,k)=>{
    if (k < topIdx.length){
      const i = topIdx[k];
      m.setLatLng([CELLS[i].lat, CELLS[i].lon]);
      m.unbindTooltip().bindTooltip(`Rang ${f.rank[i]}`);
      m.setStyle({opacity:1});
    } else {
      m.setStyle({opacity:0});
    }
  });

  sparkCursor.setAttribute('x1', sparkX(idx)); sparkCursor.setAttribute('x2', sparkX(idx));
}

slider.addEventListener('input', ()=> applyFrame(parseInt(slider.value,10)));
document.getElementById('prevBtn').addEventListener('click', ()=>{
  slider.value = Math.max(0, parseInt(slider.value,10)-1); applyFrame(parseInt(slider.value,10));
});
document.getElementById('nextBtn').addEventListener('click', ()=>{
  slider.value = Math.min(FRAMES.length-1, parseInt(slider.value,10)+1); applyFrame(parseInt(slider.value,10));
});
let playTimer = null;
document.getElementById('playBtn').addEventListener('click', (ev)=>{
  if (playTimer){ clearInterval(playTimer); playTimer=null; ev.target.textContent = '▶ Abspielen'; return; }
  ev.target.textContent = '⏸ Pause';
  playTimer = setInterval(()=>{
    let idx = parseInt(slider.value,10) + 1;
    if (idx > FRAMES.length-1){ idx = 0; }
    slider.value = idx; applyFrame(idx);
  }, 450);
});

// toggles (grid / cells / top / events / opacity) -- same behaviour as the static map
const gridChk = document.getElementById('gridChk');
gridChk.addEventListener('change', ()=>{
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else map.removeLayer(gridLayer);
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block':'none';
});
const cellChk = document.getElementById('cellChk');
cellChk.addEventListener('change', ()=>{
  if(cellChk.checked){ if(!map.hasLayer(cellLayer)) cellLayer.addTo(map); }
  else map.removeLayer(cellLayer);
});
const topChk = document.getElementById('topChk');
topChk.addEventListener('change', ()=>{
  if(topChk.checked){ if(!map.hasLayer(topLayer)) topLayer.addTo(map); }
  else map.removeLayer(topLayer);
});
const evtChk = document.getElementById('evtChk');
evtChk.addEventListener('change', ()=>{
  if(evtChk.checked){ if(!map.hasLayer(evtLayer)) evtLayer.addTo(map); }
  else map.removeLayer(evtLayer);
  document.getElementById('evtLegend').style.display = evtChk.checked ? 'block' : 'none';
});
const opac = document.getElementById('opac');
opac.addEventListener('input', ()=>{
  fillOpacity = opac.value/100;
  rects.forEach(r=>{ const c=r.options.fillColor; if(c!=='#eee') r.setStyle({fillOpacity}); });
});

// init
slider.value = DEFAULT_IDX;
applyFrame(DEFAULT_IDX);
</script>
</body>
</html>"""


def _rank_desc(values: list[float]) -> list[int]:
    """1 = highest value. Ties broken by original order (stable)."""
    order = sorted(range(len(values)), key=lambda i: -values[i])
    rank = [0] * len(values)
    for r, i in enumerate(order, start=1):
        rank[i] = r
    return rank


def build_nodal_overlay_html(siting_csv: Path, nodal_csv: Path,
                              value_col: str | None = None,
                              out_html: Path | None = None) -> Path:
    """Build ONE Germany map: the fine siting-score grid as a heatmap (same
    rendering as build_html) with the PyPSA-eur nodal-simulation buses drawn
    on top as bubbles sized/colored by their nodal arbitrage value -- so you
    can see, location by location, whether the (fast, redispatch-proxy)
    siting score agrees with the (slow, physical) nodal LP.

    `siting_csv`: the fine grid ranking CSV (e.g. battery_location_ranking_full.csv
        or any battery_siting_score run.py output with source='grid').
    `nodal_csv`: a nodal-vs-siting comparison CSV as produced by
        results/compare_nodal_vs_siting*.py -- one row per PyPSA DE bus, with
        columns bus, x, y, arb_eur_yr, congestion_premium, sit_dV (and
        optionally mean_price, std_price, n_cells).
    """
    from scipy.stats import spearmanr

    # ---- heatmap cells (same construction as build_html) ----
    df = pd.read_csv(siting_csv)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col]).reset_index(drop=True)

    ulon = np.unique(np.round(df["lon"].to_numpy(), 4))
    ulat = np.unique(np.round(df["lat"].to_numpy(), 4))
    dlon = float(np.median(np.diff(ulon))) if len(ulon) > 1 else 0.25
    dlat = float(np.median(np.diff(ulat))) if len(ulat) > 1 else 0.25

    vals = df[col].to_numpy(dtype=float)
    pos = vals[vals > 0]
    norm = float(np.percentile(pos, 98)) if pos.size else 1.0
    t = np.clip(vals / max(norm, 1e-9), 0.0, 1.0)

    rank_col = next((c for c in ("rank_by_dV", "site_rank") if c in df.columns), None)
    name_col = "name" if "name" in df.columns else None

    cells = []
    for i in range(len(df)):
        cells.append({
            "lat": round(float(df["lat"].iat[i]), 5),
            "lon": round(float(df["lon"].iat[i]), 5),
            "v": round(float(vals[i]) / 1e6, 3),
            "t": round(float(t[i]), 4),
            "r": int(df[rank_col].iat[i]) if rank_col else i + 1,
            "n": str(df[name_col].iat[i]) if name_col else f"cell_{i}",
        })

    # ---- PyPSA nodal buses (bubbles) ----
    nod = pd.read_csv(nodal_csv)
    bubble_col = "arb_eur_yr" if "arb_eur_yr" in nod.columns else "heur_value_eur_yr"
    nod = nod.dropna(subset=["x", "y", bubble_col, "sit_dV"]).reset_index(drop=True)

    arb_vals = nod[bubble_col].to_numpy(dtype=float)
    sit_vals = nod["sit_dV"].to_numpy(dtype=float)
    arb_rank = _rank_desc(arb_vals.tolist())
    sit_rank = _rank_desc(sit_vals.tolist())
    arb_norm = float(np.percentile(arb_vals[arb_vals > 0], 98)) if (arb_vals > 0).any() else 1.0

    bus_col = "bus" if "bus" in nod.columns else "busmap"
    bubbles = []
    for i in range(len(nod)):
        bubbles.append({
            "name": str(nod[bus_col].iat[i]),
            "lat": round(float(nod["y"].iat[i]), 5),
            "lon": round(float(nod["x"].iat[i]), 5),
            "arb_eur_yr": round(float(arb_vals[i]), 1),
            "arb_t": round(float(np.clip(arb_vals[i] / max(arb_norm, 1e-9), 0.0, 1.0)), 4),
            "arb_rank": arb_rank[i],
            "sit_dV_million": round(float(sit_vals[i]) / 1e6, 3),
            "sit_rank": sit_rank[i],
            "congestion_premium": (round(float(nod["congestion_premium"].iat[i]), 3)
                                    if "congestion_premium" in nod.columns else None),
            "n_cells": (int(nod["n_cells"].iat[i]) if "n_cells" in nod.columns else None),
        })

    rho, pval = spearmanr(arb_vals, sit_vals)

    payload = {
        "cells": cells, "dlat": round(dlat, 5), "dlon": round(dlon, 5),
        "col": col, "norm_million": round(norm / 1e6, 3),
        "vmax_million": round(float(vals.max()) / 1e6, 3), "n": len(cells),
        "bubbles": bubbles, "bubble_col": bubble_col,
        "n_bubbles": len(bubbles), "rho": round(float(rho), 4), "rho_p": round(float(pval), 4),
    }
    DATA_JSON = json.dumps(payload, ensure_ascii=False)
    GRID_JSON = json.dumps(_build_grid_backdrop(), ensure_ascii=False)
    EVENTS_JSON = json.dumps(_build_redispatch_events(), ensure_ascii=False)

    html = (_OVERLAY_HTML.replace("__DATA__", DATA_JSON)
                          .replace("__GRID__", GRID_JSON)
                          .replace("__EVENTS__", EVENTS_JSON))
    if out_html is None:
        out_html = Path(siting_csv).with_name("siting_vs_nodal_overlay.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[overlay-html] siting column: {col}  |  nodal column: {bubble_col}")
    print(f"[overlay-html] {len(cells)} grid cells, {len(bubbles)} PyPSA DE buses, "
          f"Spearman rho(arb, sit_dV) = {rho:+.3f} (p={pval:.3f})")
    print(f"[overlay-html] -> {out_html}")
    return out_html


_OVERLAY_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Siting-Score vs. PyPSA-Knotenmodell</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:300px; font-size:13px; max-height:94vh; overflow:auto;
  }
  .panel h1 { font-size:15px; margin:0 0 2px; }
  .panel .sub { color:#666; font-size:11px; margin-bottom:10px; }
  .panel label { font-weight:600; display:block; margin:10px 0 4px; }
  .chk { font-weight:400; display:flex; align-items:center; gap:6px; margin-top:6px; }
  input[type=range] { width:100%; }
  .stat { margin-top:12px; padding-top:10px; border-top:1px solid #eee; font-size:12px; }
  .stat b { font-size:15px; }
  .rhobox { padding:8px 10px; background:#f7f7fb; border-radius:6px; border:1px solid #e6e6f0; font-size:12px; }
  .rhobox b { font-size:16px; }
  .legend {
    position:absolute; bottom:18px; right:12px; z-index:1000; background:#fff;
    padding:8px 12px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3); font-size:11px;
  }
  .legend .kbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Siting-Score vs. PyPSA-Knotenmodell</h1>
  <div class="sub">Hintergrund: feines Kandidatengitter (Redispatch-Keil-Proxy).
  Kreise: PyPSA-eur-Knoten (physische DC-OPF-Simulation), Gr&ouml;&szlig;e/Farbe
  = nodale Arbitragewert. Popup je Knoten zeigt beide Rangfolgen nebeneinander.</div>

  <div class="rhobox">
    Spearman &rho; (Knoten-Arbitrage vs. Siting-dV): <b id="rhoVal">-</b>
    <div class="sub" id="rhoP" style="margin:2px 0 0;"></div>
  </div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Siting-Wertekarte (Gitterzellen)</label>
  <label class="chk"><input type="checkbox" id="cellChk" checked/> Heatmap anzeigen</label>
  <label class="chk"><input type="checkbox" id="topChk" checked/> Top-10 Standorte markieren</label>

  <label>PyPSA-Knoten (Nodalpreis-Simulation)</label>
  <label class="chk"><input type="checkbox" id="bubChk" checked/> Knoten-Blasen anzeigen</label>

  <label>Redispatch-Ereignisse (seit 01.10.2021)</label>
  <label class="chk"><input type="checkbox" id="evtChk"/> Ereignisse anzeigen (Kreisgr&ouml;&szlig;e = MWh)</label>

  <label>Deckkraft (Heatmap)</label>
  <input type="range" id="opac" min="0" max="100" value="60"/>

  <div class="stat">
    <div><b id="nCells">0</b> Kandidatenzellen &middot; <b id="nBub">0</b> PyPSA-Knoten</div>
    <div>Max Siting-dV: <b id="vMax">0</b> Mio.&nbsp;&euro;/Jahr</div>
    <div class="sub" style="margin-top:6px;" id="colName"></div>
  </div>
</div>

<div class="legend">
  <div id="kdeLabel">Siting-dV (Mio.&nbsp;&euro;/Jahr, pro 50&nbsp;MW)</div>
  <div class="kbar"></div>
  <div class="ticks"><span>niedrig</span><span id="legMax">hoch</span></div>
  <div style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>PyPSA-Knoten-Arbitrage</div>
    <div style="margin-top:3px; display:flex; align-items:center; gap:6px;">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#ffffb2;border:1px solid #999;"></span>
      niedrig &nbsp;&hellip;&nbsp;
      <span style="display:inline-block;width:20px;height:20px;border-radius:50%;background:#bd0026;"></span>
      hoch
    </div>
  </div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Stromnetz</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
  </div>
  <div id="evtLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee; display:none;">
    <div>Redispatch-Ereignisse (seit 01.10.2021)</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#de2d26;opacity:.7;vertical-align:middle;"></span> Hochregeln (erh&ouml;hen) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#3182bd;opacity:.7;vertical-align:middle;"></span> Runterregeln (reduzieren) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
  </div>
</div>

<script>
const DATA = __DATA__;
const GRID = __GRID__;
const EVENTS = __EVENTS__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 240;
map.createPane('cellPane'); map.getPane('cellPane').style.zIndex = 250;
map.createPane('bubPane');  map.getPane('bubPane').style.zIndex = 260;
map.createPane('evtPane');  map.getPane('evtPane').style.zIndex = 270;
const gridRenderer = L.canvas({pane:'gridPane'});
const cellRenderer = L.canvas({pane:'cellPane'});
const bubRenderer  = L.canvas({pane:'bubPane'});
const evtRenderer  = L.canvas({pane:'evtPane'});

const STOPS = [[255,255,178],[254,217,118],[254,178,76],[253,141,60],[240,59,32],[189,0,38]];
function ramp(t){ t=Math.max(0,Math.min(1,t));
  const x=t*(STOPS.length-1), i=Math.floor(x), f=x-i;
  const a=STOPS[i], b=STOPS[Math.min(i+1,STOPS.length-1)];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}

const gridLayer = L.layerGroup();
(GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
  L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
    color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.55}).addTo(gridLayer); });

const halfLat = DATA.dlat/2, halfLon = DATA.dlon/2;
const cellLayer = L.layerGroup();
let fillOpacity = 0.60;
const rects = [];
DATA.cells.forEach(c=>{
  const bounds = [[c.lat-halfLat, c.lon-halfLon],[c.lat+halfLat, c.lon+halfLon]];
  const col = ramp(c.t);
  const rect = L.rectangle(bounds, {pane:'cellPane', renderer:cellRenderer, interactive:true,
    stroke:false, fillColor:col, fillOpacity})
    .bindPopup(`<b>${c.n}</b> &middot; Rang ${c.r}<br><b>${c.v.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> Mio.&nbsp;&euro;/Jahr`
      + `<br><span style="color:#777;font-size:11px">${c.lat.toFixed(3)}&deg;N, ${c.lon.toFixed(3)}&deg;E</span>`)
    .bindTooltip(`${c.v.toLocaleString('de-DE',{maximumFractionDigits:1})} Mio. €/Jahr`);
  rect.addTo(cellLayer);
  rects.push(rect);
});

const topLayer = L.layerGroup();
[...DATA.cells].sort((a,b)=>a.r-b.r).slice(0,10).forEach(c=>{
  L.circleMarker([c.lat,c.lon], {pane:'cellPane', radius:7, color:'#111', weight:2,
    fill:false}).bindTooltip(`Rang ${c.r}: ${c.n}`).addTo(topLayer);
});

// PyPSA nodal buses -- bubbles sized/colored by nodal arbitrage value
const bubLayer = L.layerGroup();
DATA.bubbles.forEach(b=>{
  const radius = 8 + 22*b.arb_t;
  const col = ramp(b.arb_t);
  L.circleMarker([b.lat, b.lon], {pane:'bubPane', renderer:bubRenderer, radius,
    color:'#333', weight:1.5, fillColor:col, fillOpacity:0.85})
    .bindPopup(
      `<b>${b.name}</b> (PyPSA-Knoten)<br>` +
      `Nodale Arbitrage: <b>${Math.round(b.arb_eur_yr).toLocaleString('de-DE')}</b> &euro;/Jahr &middot; Rang ${b.arb_rank}/${DATA.n_bubbles}<br>` +
      `Siting-dV (Zellen im Einzugsgebiet): <b>${b.sit_dV_million.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> Mio.&nbsp;&euro;/Jahr &middot; Rang ${b.sit_rank}/${DATA.n_bubbles}` +
      (b.congestion_premium!=null ? `<br>Congestion premium: ${b.congestion_premium.toFixed(3)} €/MWh` : '') +
      (b.n_cells!=null ? `<br><span style="color:#777;font-size:11px">${b.n_cells} Gitterzellen zugeordnet</span>` : '')
    )
    .bindTooltip(`${b.name}: Arbitrage-Rang ${b.arb_rank}, Siting-Rang ${b.sit_rank}`)
    .addTo(bubLayer);
});

gridLayer.addTo(map); cellLayer.addTo(map); topLayer.addTo(map); bubLayer.addTo(map);

const evtLayer = L.layerGroup();
(function buildEvents(){
  const p98Down = Math.max(1e-9, EVENTS.mwh_down_p98||1);
  const p98Up = Math.max(1e-9, EVENTS.mwh_up_p98||1);
  (EVENTS.events||[]).forEach(e=>{
    if(e.mwh_down > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_down / p98Down));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#08519c', weight:0.8, fillColor:'#3182bd', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Runterregeln (reduzieren)<br><b>${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_down.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh runter (${e.n_down} Ereignisse)`)
        .addTo(evtLayer);
    }
    if(e.mwh_up > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_up / p98Up));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#a50f15', weight:0.8, fillColor:'#de2d26', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Hochregeln (erh&ouml;hen)<br><b>${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_up.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh hoch (${e.n_up} Ereignisse)`)
        .addTo(evtLayer);
    }
  });
})();

document.getElementById('nCells').textContent = DATA.n.toLocaleString('de-DE');
document.getElementById('nBub').textContent = DATA.n_bubbles.toLocaleString('de-DE');
document.getElementById('vMax').textContent = DATA.vmax_million.toLocaleString('de-DE',{maximumFractionDigits:2});
document.getElementById('legMax').textContent = '≥ ' + DATA.norm_million.toLocaleString('de-DE',{maximumFractionDigits:1});
document.getElementById('colName').textContent = 'Siting-Spalte: ' + DATA.col + '  |  Nodal-Spalte: ' + DATA.bubble_col;
document.getElementById('rhoVal').textContent = (DATA.rho>=0?'+':'') + DATA.rho.toFixed(3);
document.getElementById('rhoP').textContent = 'p = ' + DATA.rho_p.toFixed(3) + ` (n=${DATA.n_bubbles} Knoten)`;

const gridChk = document.getElementById('gridChk');
gridChk.addEventListener('change', ()=>{
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else map.removeLayer(gridLayer);
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block':'none';
});
const cellChk = document.getElementById('cellChk');
cellChk.addEventListener('change', ()=>{
  if(cellChk.checked){ if(!map.hasLayer(cellLayer)) cellLayer.addTo(map); }
  else map.removeLayer(cellLayer);
});
const topChk = document.getElementById('topChk');
topChk.addEventListener('change', ()=>{
  if(topChk.checked){ if(!map.hasLayer(topLayer)) topLayer.addTo(map); }
  else map.removeLayer(topLayer);
});
const bubChk = document.getElementById('bubChk');
bubChk.addEventListener('change', ()=>{
  if(bubChk.checked){ if(!map.hasLayer(bubLayer)) bubLayer.addTo(map); }
  else map.removeLayer(bubLayer);
});
const evtChk = document.getElementById('evtChk');
evtChk.addEventListener('change', ()=>{
  if(evtChk.checked){ if(!map.hasLayer(evtLayer)) evtLayer.addTo(map); }
  else map.removeLayer(evtLayer);
  document.getElementById('evtLegend').style.display = evtChk.checked ? 'block' : 'none';
});
const opac = document.getElementById('opac');
opac.addEventListener('input', ()=>{
  fillOpacity = opac.value/100;
  rects.forEach(r=>r.setStyle({fillOpacity}));
});
</script>
</body>
</html>"""


def build_batteries_overlay_html(siting_csv: Path, battery_csv: Path,
                                  value_col: str | None = None,
                                  out_html: Path | None = None) -> Path:
    """Build ONE Germany map: the fine siting-score grid as a heatmap (same
    rendering as build_html/build_nodal_overlay_html), with the REAL battery
    fleet (MaStR, `commercial_battery_storage.csv`) drawn on top -- active
    sites in one colour, planned sites in another, decommissioned optional
    and off by default. A capacity (MW) slider lets you filter out small
    sites so the map isn't swamped by sub-1MW residential units.

    `siting_csv`: the fine grid ranking CSV (e.g. battery_location_ranking_full.csv
        or any battery_siting_score run.py output with source='grid').
    `battery_csv`: the real fleet roster (data_load.load_batteries' source file),
        with columns name, lat, lon, power_mw, status_cat (active/planning/
        decommissioned), commissioning_date, planned_commissioning_date,
        state, city, usable_capacity_kwh.
    """
    # ---- heatmap cells (same construction as build_html) ----
    df = pd.read_csv(siting_csv)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col]).reset_index(drop=True)

    ulon = np.unique(np.round(df["lon"].to_numpy(), 4))
    ulat = np.unique(np.round(df["lat"].to_numpy(), 4))
    dlon = float(np.median(np.diff(ulon))) if len(ulon) > 1 else 0.25
    dlat = float(np.median(np.diff(ulat))) if len(ulat) > 1 else 0.25

    vals = df[col].to_numpy(dtype=float)
    pos = vals[vals > 0]
    norm = float(np.percentile(pos, 98)) if pos.size else 1.0
    t = np.clip(vals / max(norm, 1e-9), 0.0, 1.0)

    rank_col = next((c for c in ("rank_by_dV", "site_rank") if c in df.columns), None)
    name_col = "name" if "name" in df.columns else None

    cells = []
    for i in range(len(df)):
        cells.append({
            "lat": round(float(df["lat"].iat[i]), 5),
            "lon": round(float(df["lon"].iat[i]), 5),
            "v": round(float(vals[i]) / 1e6, 3),
            "t": round(float(t[i]), 4),
            "r": int(df[rank_col].iat[i]) if rank_col else i + 1,
            "n": str(df[name_col].iat[i]) if name_col else f"cell_{i}",
        })

    # ---- real battery fleet ----
    bat = pd.read_csv(battery_csv)
    bat = bat.dropna(subset=["lat", "lon", "power_mw", "status_cat"]).reset_index(drop=True)

    def _s(row, col_):
        v = row.get(col_)
        return "" if pd.isna(v) else str(v)

    batteries = []
    for _, r in bat.iterrows():
        status = str(r["status_cat"]).strip().lower()
        mw = float(r["power_mw"])
        mwh = (round(float(r["usable_capacity_kwh"]) / 1000.0, 2)
               if "usable_capacity_kwh" in bat.columns and pd.notna(r.get("usable_capacity_kwh")) else None)
        date = (r["commissioning_date"] if status == "active" and pd.notna(r.get("commissioning_date"))
                 else r.get("planned_commissioning_date"))
        batteries.append({
            "name": _s(r, "name") or "Unbenannter Standort",
            "lat": round(float(r["lat"]), 5),
            "lon": round(float(r["lon"]), 5),
            "mw": round(mw, 3),
            "mwh": mwh,
            "status": status,
            "date": ("" if pd.isna(date) else str(date)) if date is not None else "",
            "state": _s(r, "state"),
            "city": _s(r, "city"),
        })

    mw_all = [b["mw"] for b in batteries if b["mw"] > 0]
    mw_min = float(min(mw_all)) if mw_all else 0.1
    mw_max = float(max(mw_all)) if mw_all else 100.0
    n_by_status = {s: sum(1 for b in batteries if b["status"] == s) for s in set(b["status"] for b in batteries)}
    print(f"[batteries] {len(batteries)} sites loaded, by status: {n_by_status}, "
          f"MW range [{mw_min:.3f}, {mw_max:.1f}]")

    payload = {
        "cells": cells, "dlat": round(dlat, 5), "dlon": round(dlon, 5),
        "col": col, "norm_million": round(norm / 1e6, 3),
        "vmax_million": round(float(vals.max()) / 1e6, 3), "n": len(cells),
        "batteries": batteries, "n_batteries": len(batteries),
        "mw_min": round(mw_min, 3), "mw_max": round(mw_max, 1),
    }
    DATA_JSON = json.dumps(payload, ensure_ascii=False)
    GRID_JSON = json.dumps(_build_grid_backdrop(), ensure_ascii=False)
    EVENTS_JSON = json.dumps(_build_redispatch_events(), ensure_ascii=False)

    html = (_BATTERIES_HTML.replace("__DATA__", DATA_JSON)
                            .replace("__GRID__", GRID_JSON)
                            .replace("__EVENTS__", EVENTS_JSON))
    if out_html is None:
        out_html = Path(siting_csv).with_name("siting_vs_batteries_map.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[batteries-html] siting column: {col}")
    print(f"[batteries-html] {len(cells)} grid cells, {len(batteries)} real battery sites")
    print(f"[batteries-html] -> {out_html}")
    return out_html


_BATTERIES_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Siting-Score vs. reale Batteriestandorte</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:300px; font-size:13px; max-height:94vh; overflow:auto;
  }
  .panel h1 { font-size:15px; margin:0 0 2px; }
  .panel .sub { color:#666; font-size:11px; margin-bottom:10px; }
  .panel label { font-weight:600; display:block; margin:10px 0 4px; }
  .chk { font-weight:400; display:flex; align-items:center; gap:6px; margin-top:6px; }
  input[type=range] { width:100%; }
  .stat { margin-top:12px; padding-top:10px; border-top:1px solid #eee; font-size:12px; }
  .stat b { font-size:15px; }
  .capbox { padding:8px 10px; background:#f7f7fb; border-radius:6px; border:1px solid #e6e6f0; }
  .capbox .capval { font-size:16px; font-weight:700; color:#222; }
  .legend {
    position:absolute; bottom:18px; right:12px; z-index:1000; background:#fff;
    padding:8px 12px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3); font-size:11px;
  }
  .legend .kbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#ffffb2,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
  .dot { display:inline-block; width:11px; height:11px; border-radius:50%; vertical-align:middle; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Siting-Score vs. reale Batteriestandorte</h1>
  <div class="sub">Hintergrund: feines Kandidatengitter (Redispatch-Keil-Proxy).
  Punkte: reale MaStR-Batteriestandorte (aktiv / geplant). Leistungs-Schieberegler
  filtert kleine Standorte heraus.</div>

  <div class="capbox">
    <label style="margin-top:0;">Mindestleistung</label>
    <div class="capval" id="capVal">-</div>
    <input type="range" id="capSlider" min="0" max="1" step="0.001" value="0"/>
  </div>

  <label>Batteriestatus</label>
  <label class="chk"><input type="checkbox" id="stActive" checked/> <span class="dot" style="background:#1a9850;"></span> Aktiv (<span id="nActive">0</span>)</label>
  <label class="chk"><input type="checkbox" id="stPlanning" checked/> <span class="dot" style="background:#fd8d3c;"></span> Geplant (<span id="nPlanning">0</span>)</label>
  <label class="chk"><input type="checkbox" id="stDecom"/> <span class="dot" style="background:#999999;"></span> Stillgelegt (<span id="nDecom">0</span>)</label>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Siting-Wertekarte (Gitterzellen)</label>
  <label class="chk"><input type="checkbox" id="cellChk" checked/> Heatmap anzeigen</label>
  <label class="chk"><input type="checkbox" id="topChk" checked/> Top-10 Standorte markieren</label>

  <label>Redispatch-Ereignisse (seit 01.10.2021)</label>
  <label class="chk"><input type="checkbox" id="evtChk"/> Ereignisse anzeigen (Kreisgr&ouml;&szlig;e = MWh)</label>

  <label>Deckkraft (Heatmap)</label>
  <input type="range" id="opac" min="0" max="100" value="60"/>

  <div class="stat">
    <div><b id="nCells">0</b> Kandidatenzellen</div>
    <div><b id="nVisible">0</b> Batterien sichtbar &middot; <b id="mwVisible">0</b> MW gesamt</div>
    <div>Max Siting-dV: <b id="vMax">0</b> Mio.&nbsp;&euro;/Jahr</div>
    <div class="sub" style="margin-top:6px;" id="colName"></div>
  </div>
</div>

<div class="legend">
  <div id="kdeLabel">Siting-dV (Mio.&nbsp;&euro;/Jahr, pro 50&nbsp;MW)</div>
  <div class="kbar"></div>
  <div class="ticks"><span>niedrig</span><span id="legMax">hoch</span></div>
  <div style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Batteriestandorte (Gr&ouml;&szlig;e &prop; &radic;MW)</div>
    <div style="margin-top:3px;"><span class="dot" style="background:#1a9850;"></span> Aktiv</div>
    <div style="margin-top:3px;"><span class="dot" style="background:#fd8d3c;"></span> Geplant</div>
    <div style="margin-top:3px;"><span class="dot" style="background:#999999;"></span> Stillgelegt</div>
  </div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div>Stromnetz</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
  </div>
  <div id="evtLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee; display:none;">
    <div>Redispatch-Ereignisse (seit 01.10.2021)</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#de2d26;opacity:.7;vertical-align:middle;"></span> Hochregeln (erh&ouml;hen) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
    <div style="margin-top:3px;"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#3182bd;opacity:.7;vertical-align:middle;"></span> Runterregeln (reduzieren) &middot; Gr&ouml;&szlig;e &prop; MWh</div>
  </div>
</div>

<script>
const DATA = __DATA__;
const GRID = __GRID__;
const EVENTS = __EVENTS__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 240;
map.createPane('cellPane'); map.getPane('cellPane').style.zIndex = 250;
map.createPane('batPane');  map.getPane('batPane').style.zIndex = 260;
map.createPane('evtPane');  map.getPane('evtPane').style.zIndex = 270;
const gridRenderer = L.canvas({pane:'gridPane'});
const cellRenderer = L.canvas({pane:'cellPane'});
const batRenderer  = L.canvas({pane:'batPane'});
const evtRenderer  = L.canvas({pane:'evtPane'});

const STOPS = [[255,255,178],[254,217,118],[254,178,76],[253,141,60],[240,59,32],[189,0,38]];
function ramp(t){ t=Math.max(0,Math.min(1,t));
  const x=t*(STOPS.length-1), i=Math.floor(x), f=x-i;
  const a=STOPS[i], b=STOPS[Math.min(i+1,STOPS.length-1)];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}
const STATUS_COLOR = {active:'#1a9850', planning:'#fd8d3c', decommissioned:'#999999'};
const STATUS_LABEL = {active:'Aktiv', planning:'Geplant', decommissioned:'Stillgelegt'};

const gridLayer = L.layerGroup();
(GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
  L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
    color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.55}).addTo(gridLayer); });

const halfLat = DATA.dlat/2, halfLon = DATA.dlon/2;
const cellLayer = L.layerGroup();
let fillOpacity = 0.60;
const rects = [];
DATA.cells.forEach(c=>{
  const bounds = [[c.lat-halfLat, c.lon-halfLon],[c.lat+halfLat, c.lon+halfLon]];
  const col = ramp(c.t);
  const rect = L.rectangle(bounds, {pane:'cellPane', renderer:cellRenderer, interactive:true,
    stroke:false, fillColor:col, fillOpacity})
    .bindPopup(`<b>${c.n}</b> &middot; Rang ${c.r}<br><b>${c.v.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> Mio.&nbsp;&euro;/Jahr`
      + `<br><span style="color:#777;font-size:11px">${c.lat.toFixed(3)}&deg;N, ${c.lon.toFixed(3)}&deg;E</span>`)
    .bindTooltip(`${c.v.toLocaleString('de-DE',{maximumFractionDigits:1})} Mio. €/Jahr`);
  rect.addTo(cellLayer);
  rects.push(rect);
});

const topLayer = L.layerGroup();
[...DATA.cells].sort((a,b)=>a.r-b.r).slice(0,10).forEach(c=>{
  L.circleMarker([c.lat,c.lon], {pane:'cellPane', radius:7, color:'#111', weight:2,
    fill:false}).bindTooltip(`Rang ${c.r}: ${c.n}`).addTo(topLayer);
});

const evtLayer = L.layerGroup();
(function buildEvents(){
  const p98Down = Math.max(1e-9, EVENTS.mwh_down_p98||1);
  const p98Up = Math.max(1e-9, EVENTS.mwh_up_p98||1);
  (EVENTS.events||[]).forEach(e=>{
    if(e.mwh_down > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_down / p98Down));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#08519c', weight:0.8, fillColor:'#3182bd', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Runterregeln (reduzieren)<br><b>${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_down.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_down.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh runter (${e.n_down} Ereignisse)`)
        .addTo(evtLayer);
    }
    if(e.mwh_up > 0){
      const t = Math.min(1, Math.sqrt(e.mwh_up / p98Up));
      const radius = 3 + 20*t;
      L.circleMarker([e.lat,e.lon], {pane:'evtPane', renderer:evtRenderer, radius,
        color:'#a50f15', weight:0.8, fillColor:'#de2d26', fillOpacity:0.55})
        .bindPopup(`<b>${e.name}</b> &middot; Hochregeln (erh&ouml;hen)<br><b>${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})}</b> MWh &middot; ${e.n_up.toLocaleString('de-DE')} Ereignisse`
          + `<br><span style="color:#777;font-size:11px">Redispatch seit ${EVENTS.since}</span>`)
        .bindTooltip(`${e.name}: ${e.mwh_up.toLocaleString('de-DE',{maximumFractionDigits:0})} MWh hoch (${e.n_up} Ereignisse)`)
        .addTo(evtLayer);
    }
  });
})();

gridLayer.addTo(map); cellLayer.addTo(map); topLayer.addTo(map);

document.getElementById('nCells').textContent = DATA.n.toLocaleString('de-DE');
document.getElementById('vMax').textContent = DATA.vmax_million.toLocaleString('de-DE',{maximumFractionDigits:2});
document.getElementById('legMax').textContent = '≥ ' + DATA.norm_million.toLocaleString('de-DE',{maximumFractionDigits:1});
document.getElementById('colName').textContent = 'Siting-Spalte: ' + DATA.col;
['active','planning','decommissioned'].forEach(s=>{
  const el = document.getElementById(s==='active'?'nActive':s==='planning'?'nPlanning':'nDecom');
  if(el) el.textContent = DATA.batteries.filter(b=>b.status===s).length.toLocaleString('de-DE');
});

// ---- capacity slider (log scale: MW range spans orders of magnitude) ----
const MW_MIN = Math.max(DATA.mw_min, 0.01);
const MW_MAX = Math.max(DATA.mw_max, MW_MIN*10);
const LOG_MIN = Math.log10(MW_MIN), LOG_MAX = Math.log10(MW_MAX);
const capSlider = document.getElementById('capSlider');
function sliderToMw(v){ return Math.pow(10, LOG_MIN + v*(LOG_MAX-LOG_MIN)); }
function fmtMw(v){
  return v < 1 ? v.toLocaleString('de-DE',{maximumFractionDigits:2}) : v.toLocaleString('de-DE',{maximumFractionDigits:1});
}

// ---- battery markers: built once, shown/hidden per filter (cheap for ~2k points) ----
const batLayer = L.layerGroup();
const batMarkers = DATA.batteries.map(b=>{
  const radius = 3 + 14*Math.sqrt(Math.max(b.mw,0.01)/MW_MAX);
  const col = STATUS_COLOR[b.status] || '#666';
  const mwhTxt = b.mwh!=null ? `<br>Nutzbare Kapazit&auml;t: ${b.mwh.toLocaleString('de-DE',{maximumFractionDigits:1})} MWh` : '';
  const dateTxt = b.date ? `<br>${b.status==='active' ? 'Inbetriebnahme' : 'Geplante Inbetriebnahme'}: ${b.date}` : '';
  const locTxt = (b.city || b.state) ? `<br><span style="color:#777;font-size:11px">${[b.city,b.state].filter(Boolean).join(', ')}</span>` : '';
  const m = L.circleMarker([b.lat,b.lon], {pane:'batPane', renderer:batRenderer, radius,
    color:'#222', weight:0.8, fillColor:col, fillOpacity:0.85})
    .bindPopup(`<b>${b.name}</b> &middot; ${STATUS_LABEL[b.status]||b.status}<br><b>${b.mw.toLocaleString('de-DE',{maximumFractionDigits:2})}</b> MW${mwhTxt}${dateTxt}${locTxt}`)
    .bindTooltip(`${b.name}: ${b.mw.toLocaleString('de-DE',{maximumFractionDigits:1})} MW (${STATUS_LABEL[b.status]||b.status})`);
  return m;
});

function applyFilter(){
  const threshold = sliderToMw(parseFloat(capSlider.value));
  document.getElementById('capVal').textContent = '≥ ' + fmtMw(threshold) + ' MW';
  const showStatus = {
    active: document.getElementById('stActive').checked,
    planning: document.getElementById('stPlanning').checked,
    decommissioned: document.getElementById('stDecom').checked,
  };
  batLayer.clearLayers();
  let nVis = 0, mwVis = 0;
  DATA.batteries.forEach((b,i)=>{
    if(b.mw < threshold) return;
    if(!showStatus[b.status]) return;
    batMarkers[i].addTo(batLayer);
    nVis++; mwVis += b.mw;
  });
  document.getElementById('nVisible').textContent = nVis.toLocaleString('de-DE');
  document.getElementById('mwVisible').textContent = mwVis.toLocaleString('de-DE',{maximumFractionDigits:0});
}
batLayer.addTo(map);
capSlider.addEventListener('input', applyFilter);
document.getElementById('stActive').addEventListener('change', applyFilter);
document.getElementById('stPlanning').addEventListener('change', applyFilter);
document.getElementById('stDecom').addEventListener('change', applyFilter);
applyFilter();

const gridChk = document.getElementById('gridChk');
gridChk.addEventListener('change', ()=>{
  if(gridChk.checked){ if(!map.hasLayer(gridLayer)) gridLayer.addTo(map); }
  else map.removeLayer(gridLayer);
  document.getElementById('gridLegend').style.display = gridChk.checked ? 'block':'none';
});
const cellChk = document.getElementById('cellChk');
cellChk.addEventListener('change', ()=>{
  if(cellChk.checked){ if(!map.hasLayer(cellLayer)) cellLayer.addTo(map); }
  else map.removeLayer(cellLayer);
});
const topChk = document.getElementById('topChk');
topChk.addEventListener('change', ()=>{
  if(topChk.checked){ if(!map.hasLayer(topLayer)) topLayer.addTo(map); }
  else map.removeLayer(topLayer);
});
const evtChk = document.getElementById('evtChk');
evtChk.addEventListener('change', ()=>{
  if(evtChk.checked){ if(!map.hasLayer(evtLayer)) evtLayer.addTo(map); }
  else map.removeLayer(evtLayer);
  document.getElementById('evtLegend').style.display = evtChk.checked ? 'block' : 'none';
});
const opac = document.getElementById('opac');
opac.addEventListener('input', ()=>{
  fillOpacity = opac.value/100;
  rects.forEach(r=>r.setStyle({fillOpacity}));
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="scores CSV (default: latest in out/)")
    ap.add_argument("--col", default=None,
                    help="value column (default: dV_eur_per_year if populated, else dV_proxy_eur_per_year)")
    ap.add_argument("--out", default=None, help="output HTML path")
    ap.add_argument("--sweep-dir", default=None,
                    help="build the multi-run slider map instead, from a h_sweep_* folder "
                         "containing sweep_summary.csv (as produced by the h-sweep notebook)")
    ap.add_argument("--nodal-csv", default=None,
                    help="build the siting-heatmap + PyPSA-nodal-bubbles overlay map instead, "
                         "from a nodal-vs-siting comparison CSV (results/compare_nodal_vs_siting*.py output)")
    ap.add_argument("--battery-csv", default=None,
                    help="build the siting-heatmap + real-battery-fleet overlay map instead, "
                         "from commercial_battery_storage.csv (or any CSV with the same columns)")
    args = ap.parse_args()
    if args.battery_csv:
        csv_path = Path(args.csv) if args.csv else _latest_csv(DEFAULT_OUT_DIR)
        build_batteries_overlay_html(csv_path, Path(args.battery_csv), value_col=args.col,
                                      out_html=(Path(args.out) if args.out else None))
    elif args.nodal_csv:
        csv_path = Path(args.csv) if args.csv else _latest_csv(DEFAULT_OUT_DIR)
        build_nodal_overlay_html(csv_path, Path(args.nodal_csv), value_col=args.col,
                                  out_html=(Path(args.out) if args.out else None))
    elif args.sweep_dir:
        build_sweep_html(Path(args.sweep_dir), value_col=args.col,
                          out_html=(Path(args.out) if args.out else None))
    else:
        csv_path = Path(args.csv) if args.csv else _latest_csv(DEFAULT_OUT_DIR)
        build_html(csv_path, value_col=args.col,
                   out_html=(Path(args.out) if args.out else None))
