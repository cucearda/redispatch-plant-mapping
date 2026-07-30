"""Render the interactive Leaflet maps to print-quality PNGs for the LaTeX paper.

Uses headless Chrome (already installed) - no extra Python packages needed.
For every map two variants are produced:
  *_clean.png : control panel hidden, legend + attribution kept  -> use in the paper
  *_full.png  : the page exactly as it looks in the browser      -> for slides / appendix

Run:  python render_maps_to_png.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("FIGURES_OUT", os.path.join(BASE, os.pardir, "figures", "maps"))

# Headless Chrome/Edge does the rendering. Set CHROME_BINARY to override.
_CANDIDATES = [
    os.environ.get("CHROME_BINARY"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
CHROME = next((c for c in _CANDIDATES if c and (os.path.exists(c) or shutil.which(c))), None)
if CHROME is None:
    sys.exit("No Chrome/Chromium found. Set CHROME_BINARY to your browser executable.")

# Germany bounding box, slightly padded
BOUNDS = "[[47.15, 5.70], [55.15, 15.20]]"

MAPS = [
    # (source html, output basename, window w, h)
    ("redispatch_kde_map_germany.html",                              "map_redispatch_kde",      1150, 1400),
    ("battery_siting_score/out/battery_siting_heatmap_germany.html", "map_siting_score",        1150, 1400),
    ("battery_siting_score/out/siting_vs_batteries_map.html",        "map_siting_vs_batteries", 1150, 1400),
    ("battery_siting_score/out/baseline_nodes_impact_germany.html",  "map_source_nodes",        1150, 1400),
    ("results/siting_vs_nodal_overlay.html",                         "map_siting_vs_pypsa",     1150, 1400),
    ("battery_siting_score/out/h_sweep_20260715_175302/h_sweep_map.html", "map_h_sweep",        1150, 1400),
    ("battery_siting_score/out/siting_heatmap.html",                 "map_siting_heatmap_old",  1150, 1400),
]

INJECT = """
<style id="printcss">
  %(hide)s
  .leaflet-control-zoom { display: none !important; }
  .leaflet-control-attribution { font-size: 11px; background: rgba(255,255,255,.85); }
</style>
<script>
(function () {
  function fit() {
    var m = null;
    try { m = map; } catch (e) {}
    if (!m) {
      for (var k in window) {
        if (k.indexOf('map_') === 0 && window[k] && window[k].fitBounds) { m = window[k]; break; }
      }
    }
    if (m) { try { m.invalidateSize(); m.fitBounds(%(bounds)s, {padding: [8, 8]}); } catch (e) {} }
  }
  if (document.readyState === 'complete') setTimeout(fit, 400);
  else window.addEventListener('load', function () { setTimeout(fit, 400); });
})();
</script>
"""

HIDE_PANEL = ".panel { display: none !important; }"


def render(src_rel, out_name, w, h, clean):
    src = os.path.join(BASE, src_rel)
    if not os.path.exists(src):
        print("  ! missing:", src_rel)
        return
    html = open(src, encoding="utf-8", errors="replace").read()
    html += INJECT % {"hide": HIDE_PANEL if clean else "", "bounds": BOUNDS}

    tmp = os.path.join(os.path.dirname(src), "_print_tmp.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)

    png = os.path.join(OUT, out_name + ("_clean.png" if clean else "_full.png"))
    url = "file:///" + urllib.parse.quote(tmp.replace("\\", "/"))
    profile = tempfile.mkdtemp(prefix="chromeshot_")
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2",              # 2x -> ~300 dpi at typical figure width
        "--window-size=%d,%d" % (w, h),
        "--virtual-time-budget=30000",                # let tiles + JS finish
        "--user-data-dir=" + profile,
        "--screenshot=" + png,
        url,
    ]
    subprocess.run(cmd, capture_output=True)
    shutil.rmtree(profile, ignore_errors=True)
    os.remove(tmp)
    if os.path.exists(png):
        print("  ok  %-42s %6.1f KB" % (os.path.basename(png), os.path.getsize(png) / 1024))
    else:
        print("  ! failed:", png)


def main():
    os.makedirs(OUT, exist_ok=True)
    only = sys.argv[1:]
    for src_rel, out_name, w, h in MAPS:
        if only and not any(o in out_name for o in only):
            continue
        print(out_name)
        render(src_rel, out_name, w, h, clean=True)
        render(src_rel, out_name, w, h, clean=False)


if __name__ == "__main__":
    main()
