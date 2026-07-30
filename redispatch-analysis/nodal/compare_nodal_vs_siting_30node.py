"""
Compare PyPSA nodal marginal prices (30-node DE network, 2025 hourly) against
the redispatch-based battery siting-score surface (the FULL fine grid in
battery_location_ranking_full.csv, 4593 cells).

Same methodology as compare_nodal_vs_siting.py (which used the 20-node
network): the siting score is a PROXY for the locational price signal that
Germany's single bidding zone hides. The PyPSA nodal run produces the real
nodal LMPs on a (still heavily clustered, but finer than 20-node) 30-node
network -- 17 of those nodes are in Germany. Each siting grid cell is
assigned to its NEAREST DE PyPSA node, aggregated, then rank-correlated.

Only RANKING is comparable, not absolute EUR: siting dV is calibrated to
BNetzA redispatch cost (~100 EUR/MWh); PyPSA LMP spreads here are only a
few EUR/MWh.
"""
import netCDF4 as nc
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

NET = "de-nodal-2025-hourly/networks/base_s_30_elec_.nc"   # 30-node 2025 hourly run
SITE = "battery_location_ranking_full.csv"                 # full fine-grid siting score (4593 cells)
PMW, DUR = 50.0, 4.0     # reference battery 50 MW / 4 h (matches siting-score meta)

ds = nc.Dataset(NET)
bus = list(ds.variables["buses_i"][:])
x = np.asarray(ds.variables["buses_x"][:], dtype=float)
y = np.asarray(ds.variables["buses_y"][:], dtype=float)
ctry = list(ds.variables["buses_country"][:])
mp = np.array(ds.variables["buses_t_marginal_price"][:])
mp_i = list(ds.variables["buses_t_marginal_price_i"][:])
ds.close()
if mp.shape[0] == len(mp_i) and mp.shape[1] != len(mp_i):
    pass  # already (n_t, n_bus)
elif mp.shape[0] == len(mp_i):
    mp = mp.T
price = pd.DataFrame(mp, columns=mp_i)

de = [b for b, c in zip(bus, ctry) if c == "DE"]
print(f"[nodal] {len(bus)} total buses, {len(de)} DE buses, {price.shape[0]} hourly steps")

T = price.shape[0]
nd = T // 24
k = int(DUR)
recs = []
for b in de:
    p = price[b].values[: nd * 24].reshape(nd, 24)
    s = np.sort(p, axis=1)
    spread = s[:, -k:].mean(1) - s[:, :k].mean(1)
    arb = (spread * PMW * DUR).sum() * (365 / nd)
    i = bus.index(b)
    recs.append(dict(bus=b, x=float(x[i]), y=float(y[i]),
                      mean_price=price[b].mean(), std_price=price[b].std(), arb_eur_yr=arb))
nod = pd.DataFrame(recs)
nod["congestion_premium"] = nod["mean_price"] - price[de].values.mean()

sit = pd.read_csv(SITE)
nx, ny = nod["x"].values, nod["y"].values
sit["pypsa_bus"] = [nod["bus"].values[((nx - lo) ** 2 + (ny - la) ** 2).argmin()]
                     for lo, la in zip(sit["lon"], sit["lat"])]
agg = (sit.groupby("pypsa_bus")
          .agg(n_cells=("lon", "size"), sit_V=("V_proxy_eur_per_year", "mean"),
               sit_dV=("dV_proxy_eur_per_year", "mean")).reset_index())
m = nod.merge(agg, left_on="bus", right_on="pypsa_bus")

print(m.sort_values("sit_dV", ascending=False).to_string(index=False))
print("\nSpearman rank correlations across DE nodes (30-node network):")
for a in ["arb_eur_yr", "std_price", "congestion_premium"]:
    rho, p = spearmanr(m[a], m["sit_dV"])
    print(f"  {a:<18} vs siting dV : rho={rho:+.3f}  p={p:.3f}")

OUT = "nodal_vs_siting_comparison_2025_30node.csv"
m.to_csv(OUT, index=False)
print(f"\nsaved -> {OUT}")
