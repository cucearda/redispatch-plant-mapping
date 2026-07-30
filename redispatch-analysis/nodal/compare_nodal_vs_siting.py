"""
Compare PyPSA nodal marginal prices (results/) against the redispatch-based
battery siting-score surface (battery_siting_score/out/).

The siting score is a PROXY for the nodal price signal that Germany's single
bidding zone hides. The PyPSA nodal run produces the real nodal LMPs. So this
tests whether the proxy's high-value locations line up with the PyPSA signal.

Two datasets live at different resolution (siting = 4593 cells on a 0.1 deg grid;
PyPSA hourly = 10 DE nodes), so each siting cell is assigned to its NEAREST PyPSA
DE node, aggregated, then rank-correlated. Only RANKING is comparable, not absolute
EUR: siting dV is calibrated to BNetzA redispatch cost (~100 EUR/MWh); PyPSA LMP
spreads here are ~3-5 EUR/MWh.
"""
import netCDF4 as nc, numpy as np, pandas as pd
from scipy.stats import spearmanr

NET  = "de-nodal-2025-hourly/networks/base_s_20_elec_.nc"   # 50-node 2013 run also possible
SITE = "../battery_siting_score/out/grid_siting_scores_20260629_155333.csv"
PMW, DUR = 50.0, 4.0     # reference battery 50 MW / 4 h (matches siting-score meta)

ds = nc.Dataset(NET)
rs = lambda n:(list(nc.chartostring(ds.variables[n][:]))
               if ds.variables[n][:].dtype.kind in 'SU' else ds.variables[n][:])
bus=[str(b) for b in rs('buses_i')]; x,y=rs('buses_x'),rs('buses_y')
ctry=[str(c) for c in rs('buses_country')]
mp=np.array(ds.variables['buses_t_marginal_price'][:]); mp_i=[str(b) for b in rs('buses_t_marginal_price_i')]
ds.close()
if mp.shape[0]==len(mp_i): mp=mp.T
price=pd.DataFrame(mp,columns=mp_i)
de=[b for b,c in zip(bus,ctry) if c=='DE']

T=price.shape[0]; nd=T//24; k=int(DUR); recs=[]
for b in de:
    p=price[b].values[:nd*24].reshape(nd,24); s=np.sort(p,axis=1)
    spread=s[:,-k:].mean(1)-s[:,:k].mean(1)
    arb=(spread*PMW*DUR).sum()*(365/nd)
    i=bus.index(b)
    recs.append(dict(bus=b,x=float(x[i]),y=float(y[i]),
                     mean_price=price[b].mean(),std_price=price[b].std(),arb_eur_yr=arb))
nod=pd.DataFrame(recs); nod['congestion_premium']=nod['mean_price']-price[de].values.mean()

sit=pd.read_csv(SITE); nx,ny=nod['x'].values,nod['y'].values
sit['pypsa_bus']=[nod['bus'].values[((nx-lo)**2+(ny-la)**2).argmin()]
                  for lo,la in zip(sit['lon'],sit['lat'])]
agg=(sit.groupby('pypsa_bus')
        .agg(n_cells=('lon','size'),sit_V=('V_proxy_eur_per_year','mean'),
             sit_dV=('dV_proxy_eur_per_year','mean')).reset_index())
m=nod.merge(agg,left_on='bus',right_on='pypsa_bus')
print(m.sort_values('sit_dV',ascending=False).to_string(index=False))
print("\nSpearman rank correlations across DE nodes:")
for a in ['arb_eur_yr','std_price','congestion_premium']:
    rho,p=spearmanr(m[a],m['sit_dV']); print(f"  {a:<18} vs siting dV : rho={rho:+.3f}  p={p:.3f}")
m.to_csv("nodal_vs_siting_comparison.csv",index=False)
print("\nsaved -> nodal_vs_siting_comparison.csv")
