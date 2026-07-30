"""Render the redispatch SOURCE NODES ("baseline nodes") and their impact.

Companion to plot_grid_map_html.py, same interactive Leaflet style (CARTO tiles,
380/220 kV backdrop, panel + legend). Where the siting heatmap shows the RESULT
(locational value per candidate cell), this map shows the CAUSE: the redispatch
event buses that drive the wedge. Each node is drawn where it snaps onto the
grid, with

    size  = impact  = congestion volume (sum |MWh|) x electrical reach
                      (sum of the PTDF kernel weight onto all candidate cells)
    colour = net direction  = net signed energy / total energy in [-1, 1]
             red  (+): "erhoehen" dominant -> grid-short -> raises local price -> discharge value
             blue (-): "reduzieren" dominant -> surplus -> lowers local price -> charge value

Reuses the exact pipeline objects (network, snapping, PTDF kernel, window,
kernel bandwidth) so the impact is consistent with the scorer. The scorer itself
is untouched -- this only reads its inputs.

Usage:
    python -m battery_siting_score.plot_baseline_nodes_html
    python -m battery_siting_score.plot_baseline_nodes_html --config <cfg> --out <html>
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

from .data_load import load_redispatch, make_artificial_grid
from .network import build_network, snap_points_to_buses
from .plot_grid_map_html import _build_grid_backdrop
from .plot_grid_map import _latest_csv, _pick_value_col, DEFAULT_OUT_DIR

HERE = Path(__file__).resolve().parent


def _candidate_value_cells(value_col: str | None) -> dict:
    """Optional faint underlay: the siting heatmap cells from the latest scores CSV."""
    try:
        csv_path = _latest_csv(DEFAULT_OUT_DIR)
    except FileNotFoundError:
        return {"cells": [], "dlat": 0.25, "dlon": 0.25}
    df = pd.read_csv(csv_path)
    col = _pick_value_col(df, value_col)
    df = df.dropna(subset=["lat", "lon", col])
    ulon = np.unique(np.round(df["lon"].to_numpy(), 4))
    ulat = np.unique(np.round(df["lat"].to_numpy(), 4))
    dlon = float(np.median(np.diff(ulon))) if len(ulon) > 1 else 0.25
    dlat = float(np.median(np.diff(ulat))) if len(ulat) > 1 else 0.25
    vals = df[col].to_numpy(dtype=float)
    pos = vals[vals > 0]
    norm = float(np.percentile(pos, 98)) if pos.size else 1.0
    t = np.clip(vals / max(norm, 1e-9), 0.0, 1.0)
    cells = [{"lat": round(float(df["lat"].iat[i]), 5),
              "lon": round(float(df["lon"].iat[i]), 5),
              "t": round(float(t[i]), 4)} for i in range(len(df))]
    return {"cells": cells, "dlat": round(dlat, 5), "dlon": round(dlon, 5)}


def build_html(cfg_path: str, value_col: str | None = None,
               out_html: Path | None = None) -> Path:
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    paths = cfg["paths"]
    buses_csv, lines_csv = paths["buses_csv"], paths["lines_csv"]

    # 1) network + redispatch events restricted to the scorer's window
    net = build_network(buses_csv, lines_csv)
    red = load_redispatch(paths["redispatch_csv"])
    w_start = pd.Timestamp(cfg["window"]["start_utc"])
    w_end = pd.Timestamp(cfg["window"]["end_utc"])
    in_w = (red["ts_end_utc"] > w_start) & (red["ts_start_utc"] <= w_end)
    red = red[in_w].copy()
    red["energy"] = pd.to_numeric(red["GESAMTE_ARBEIT_MWH"], errors="coerce").abs().fillna(0.0)
    red["signed"] = red["sign"].astype(float) * red["energy"]
    print(f"[nodes] redispatch events in window: {len(red)}")

    # 2) snap each event to its grid bus, aggregate per bus
    ev_bus = snap_points_to_buses(red["lon"].to_numpy(), red["lat"].to_numpy(), net)
    red["ev_bus"] = ev_bus
    agg = (red.groupby("ev_bus")
              .agg(total_energy=("energy", "sum"),
                   net_energy=("signed", "sum"),
                   n_events=("energy", "size"))
              .reset_index())
    node_bus = agg["ev_bus"].to_numpy(dtype=int)
    print(f"[nodes] unique source buses: {len(node_bus)}")

    # 3) electrical reach via the SAME PTDF kernel the scorer uses, onto the
    #    artificial candidate grid (reach = how broadly a node moves the fleet).
    gcfg = cfg["battery"].get("grid", {}) or {}
    grid = make_artificial_grid(buses_csv,
                                spacing_deg=float(gcfg.get("spacing_deg", 0.25)),
                                power_mw=float(gcfg.get("power_mw", 50.0)),
                                max_dist_km=float(gcfg.get("max_dist_km", 50.0)))
    bat_bus = snap_points_to_buses(grid["lon"].to_numpy(), grid["lat"].to_numpy(), net)

    from .ptdf import build_pypsa_network, compute_ptdf_columns, ptdf_kernel_matrix
    pn = build_pypsa_network(buses_csv, lines_csv, net)
    ptdf_cols, sub_id = compute_ptdf_columns(pn, net)
    K = ptdf_kernel_matrix(ptdf_cols, sub_id,
                           ev_bus_idx=node_bus, bat_bus_idx=bat_bus,
                           kernel_kind=str(cfg["kernel"].get("ptdf_shape", "gaussian")),
                           bandwidth=float(cfg["kernel"].get("ptdf_bandwidth", 1.0)))
    reach = K.sum(axis=1)                          # (n_nodes,) sum over candidate cells

    total_energy = agg["total_energy"].to_numpy()
    net_energy = agg["net_energy"].to_numpy()
    impact = total_energy * reach                  # relative influence on the siting map
    direction = np.divide(net_energy, np.maximum(total_energy, 1e-9))  # [-1, 1]
    imp_p98 = float(np.percentile(impact[impact > 0], 98)) if (impact > 0).any() else 1.0

    nodes = []
    for k in range(len(node_bus)):
        b = int(node_bus[k])
        nodes.append({
            "lat": round(float(net.coords_lat[b]), 5),
            "lon": round(float(net.coords_lon[b]), 5),
            "imp": round(float(impact[k]), 4),
            "t": round(float(min(1.0, impact[k] / max(imp_p98, 1e-9))), 4),
            "dir": round(float(direction[k]), 3),
            "mwh": round(float(total_energy[k]), 0),
            "net": round(float(net_energy[k]), 0),
            "reach": round(float(reach[k]), 3),
            "n": int(agg["n_events"].iat[k]),
        })
    print(f"[nodes] impact p98 {imp_p98:,.1f}  "
          f"pos-dir nodes {(direction>0).sum()}  neg-dir nodes {(direction<0).sum()}")

    payload = {"nodes": nodes, "n": len(nodes), "imp_p98": round(imp_p98, 3),
               "window": [str(w_start.date()), str(w_end.date())]}
    DATA_JSON = json.dumps(payload, ensure_ascii=False)
    GRID_JSON = json.dumps(_build_grid_backdrop(), ensure_ascii=False)
    VALUE_JSON = json.dumps(_candidate_value_cells(value_col), ensure_ascii=False)

    html = (_HTML.replace("__DATA__", DATA_JSON)
                 .replace("__GRID__", GRID_JSON)
                 .replace("__VALUE__", VALUE_JSON))
    if out_html is None:
        out_html = DEFAULT_OUT_DIR / "baseline_nodes_impact_germany.html"
    out_html.parent.mkdir(parents=True, exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[html] {len(nodes)} source nodes -> {out_html}")
    return out_html


_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Redispatch-Quellknoten &ndash; Impact (Deutschland)</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; top:0; bottom:0; left:0; right:0; }
  .panel {
    position:absolute; top:12px; left:12px; z-index:1000; background:#fff;
    padding:12px 14px; border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,.3);
    width:290px; font-size:13px; max-height:94vh; overflow:auto;
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
  .legend .dbar { height:10px; width:200px; border-radius:3px;
    background:linear-gradient(to right,#2166ac,#67a9cf,#f7f7f7,#ef8a62,#b2182b); }
  .legend .ticks { display:flex; justify-content:space-between; margin-top:2px; }
  .dot { display:inline-block; border-radius:50%; background:#888; vertical-align:middle; }
</style>
</head>
<body>
<div id="map"></div>

<div class="panel">
  <h1>Redispatch-Quellknoten</h1>
  <div class="sub">Die Knoten, die den Standortwert erzeugen. Gr&ouml;&szlig;e = Impact
  (Volumen &times; elektrische Reichweite via PTDF-Kern), Farbe = Netto-Richtung.</div>

  <label>Stromnetz (&Uuml;bertragungsnetz)</label>
  <label class="chk"><input type="checkbox" id="gridChk" checked/> 380 / 220&nbsp;kV anzeigen</label>

  <label>Quellknoten</label>
  <label class="chk"><input type="checkbox" id="nodeChk" checked/> Knoten anzeigen (Gr&ouml;&szlig;e = Impact)</label>

  <label>Standortwert-Heatmap (Vergleich)</label>
  <label class="chk"><input type="checkbox" id="valChk"/> dV-Wertekarte als Unterlage</label>

  <label>Knoten-Deckkraft</label>
  <input type="range" id="opac" min="0" max="100" value="80"/>

  <div class="stat">
    <div><b id="nNodes">0</b> Quellknoten</div>
    <div id="dirSplit" style="color:#666;"></div>
    <div class="sub" style="margin-top:6px;" id="winTxt"></div>
  </div>
</div>

<div class="legend">
  <div>Netto-Richtung des Knotens</div>
  <div class="dbar"></div>
  <div class="ticks"><span>reduzieren&nbsp;(&darr;&nbsp;Laden)</span><span>erh&ouml;hen&nbsp;(&uarr;&nbsp;Entladen)</span></div>
  <div style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">Impact (Kreisgr&ouml;&szlig;e)</div>
  <div style="margin-top:4px;">
    <span class="dot" style="width:8px;height:8px;"></span>&nbsp;niedrig&nbsp;&nbsp;
    <span class="dot" style="width:16px;height:16px;"></span>&nbsp;&nbsp;
    <span class="dot" style="width:26px;height:26px;"></span>&nbsp;hoch
  </div>
  <div id="gridLegend" style="margin-top:8px; padding-top:8px; border-top:1px solid #eee;">
    <div><span style="display:inline-block;width:20px;height:3px;background:#e07a7a;vertical-align:middle;"></span> 380&nbsp;kV
    &nbsp; <span style="display:inline-block;width:20px;height:3px;background:#9ab8d6;vertical-align:middle;"></span> 220&nbsp;kV</div>
  </div>
</div>

<script>
const DATA = __DATA__;
const GRID = __GRID__;
const VALUE = __VALUE__;

const map = L.map('map', {preferCanvas:true}).setView([51.2,10.4], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:18
}).addTo(map);

map.createPane('valPane');  map.getPane('valPane').style.zIndex = 235;
map.createPane('gridPane'); map.getPane('gridPane').style.zIndex = 245;
map.createPane('nodePane'); map.getPane('nodePane').style.zIndex = 260;
const valRenderer  = L.canvas({pane:'valPane'});
const gridRenderer = L.canvas({pane:'gridPane'});
const nodeRenderer = L.canvas({pane:'nodePane'});

// diverging RdBu ramp for net direction in [-1,1]
const DST = [[33,102,172],[103,169,207],[247,247,247],[239,138,98],[178,24,43]];
function dcol(t){ const u=(Math.max(-1,Math.min(1,t))+1)/2;  // -> [0,1]
  const x=u*(DST.length-1), i=Math.floor(x), f=x-i;
  const a=DST[i], b=DST[Math.min(i+1,DST.length-1)];
  const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`;
}
// YlOrRd for the optional value underlay
const YST=[[255,255,178],[254,217,118],[254,178,76],[253,141,60],[240,59,32],[189,0,38]];
function ycol(t){ t=Math.max(0,Math.min(1,t)); const x=t*(YST.length-1),i=Math.floor(x),f=x-i;
  const a=YST[i],b=YST[Math.min(i+1,YST.length-1)]; const c=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
  return `rgb(${c.join(',')})`; }

// grid backdrop
const gridLayer = L.layerGroup();
(GRID.ac||[]).forEach(l=>{ const ehv=l.v>=380;
  L.polyline(l.p, {pane:'gridPane', renderer:gridRenderer, interactive:false,
    color: ehv?'#e07a7a':'#9ab8d6', weight: ehv?2.0:1.3, opacity:0.5}).addTo(gridLayer); });

// optional value underlay
const valLayer = L.layerGroup();
const vHalfLat=(VALUE.dlat||0.25)/2, vHalfLon=(VALUE.dlon||0.25)/2;
(VALUE.cells||[]).forEach(c=>{
  L.rectangle([[c.lat-vHalfLat,c.lon-vHalfLon],[c.lat+vHalfLat,c.lon+vHalfLon]],
    {pane:'valPane', renderer:valRenderer, interactive:false, stroke:false,
     fillColor:ycol(c.t), fillOpacity:0.45}).addTo(valLayer);
});

// source nodes
let nodeOpacity = 0.80;
const nodeLayer = L.layerGroup();
const dots = [];
DATA.nodes.forEach(nd=>{
  const radius = 3 + 24*Math.sqrt(nd.t);
  const cm = L.circleMarker([nd.lat,nd.lon], {pane:'nodePane', renderer:nodeRenderer,
    radius, color:'#333', weight:0.6, fillColor:dcol(nd.dir), fillOpacity:nodeOpacity})
    .bindPopup(`<b>Redispatch-Quellknoten</b><br>`
      + `Impact-Rang &middot; ${nd.n.toLocaleString('de-DE')} Ereignisse<br>`
      + `Volumen: <b>${nd.mwh.toLocaleString('de-DE')}</b> MWh (|Arbeit|)<br>`
      + `Netto: ${nd.net.toLocaleString('de-DE')} MWh (${nd.dir>0?'erh&ouml;hen &uarr;':'reduzieren &darr;'})<br>`
      + `Reichweite (PTDF): ${nd.reach.toLocaleString('de-DE',{maximumFractionDigits:1})}`
      + `<br><span style="color:#777;font-size:11px">${nd.lat.toFixed(3)}&deg;N, ${nd.lon.toFixed(3)}&deg;E</span>`)
    .bindTooltip(`${nd.mwh.toLocaleString('de-DE')} MWh &middot; Netto ${nd.dir>0?'+':''}${(nd.dir*100).toFixed(0)}%`);
  cm.addTo(nodeLayer);
  dots.push(cm);
});

gridLayer.addTo(map); nodeLayer.addTo(map);

// stats
document.getElementById('nNodes').textContent = DATA.n.toLocaleString('de-DE');
const npos = DATA.nodes.filter(n=>n.dir>0).length, nneg = DATA.nodes.filter(n=>n.dir<0).length;
document.getElementById('dirSplit').textContent =
  `${npos} erh&ouml;hen-dominiert (rot) · ${nneg} reduzieren-dominiert (blau)`.replace('&ouml;','ö');
document.getElementById('winTxt').textContent = `Fenster: ${DATA.window[0]} → ${DATA.window[1]}`;

// toggles
const gridChk=document.getElementById('gridChk');
gridChk.addEventListener('change',()=>{ gridChk.checked?gridLayer.addTo(map):map.removeLayer(gridLayer);
  document.getElementById('gridLegend').style.display=gridChk.checked?'block':'none'; });
const nodeChk=document.getElementById('nodeChk');
nodeChk.addEventListener('change',()=>{ nodeChk.checked?nodeLayer.addTo(map):map.removeLayer(nodeLayer); });
const valChk=document.getElementById('valChk');
valChk.addEventListener('change',()=>{ valChk.checked?valLayer.addTo(map):map.removeLayer(valLayer); });
const opac=document.getElementById('opac');
opac.addEventListener('input',()=>{ nodeOpacity=opac.value/100; dots.forEach(d=>d.setStyle({fillOpacity:nodeOpacity})); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--col", default=None, help="value column for the comparison underlay")
    ap.add_argument("--out", default=None, help="output HTML path")
    args = ap.parse_args()
    build_html(args.config, value_col=args.col,
               out_html=(Path(args.out) if args.out else None))
