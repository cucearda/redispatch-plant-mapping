"""PTDF-based electrical-distance kernel.

This is an alternative to the graph-distance kernel in network.py. The kernel
weight between two buses n and g is

    K_h( ||PTDF[:, n] - PTDF[:, g]||_2 )

where PTDF is the DC power transfer distribution factor matrix computed by
PyPSA for the DE-internal 220/380 kV network. Two buses with identical PTDF
columns shift every line by the same amount per unit injection -> a battery at
n is a perfect substitute for a redispatch action at g, electrical distance = 0.

DC PTDF is slack-invariant up to an additive constant on each row, so the
column-difference is independent of the slack bus choice within a sub_network.
Across sub_networks (disconnected components) the kernel is set to 0.

We reuse the same bus index/order as the Dijkstra graph in network.NetworkInfo
so that snapping (network.snap_points_to_buses) maps to the same bus indices
either way.
"""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
from shapely import wkt

from .network import NetworkInfo, GEO_DEG_M, kernel_function


# Bridges between co-located buses (same OSM base_id, different voltage suffix).
# We use a small reactance Line for the PTDF network -- same role as the 250 m
# transformer-bridge in the Dijkstra graph. Value chosen well below typical
# line reactance (~10s of Ohm at 220 kV) so the bridge doesn't dominate flows.
BRIDGE_X_OHM = 0.1
BRIDGE_R_OHM = 0.01
BRIDGE_S_NOM = 1e4   # nominal apparent power not used by DC PTDF, but PyPSA needs a value


def _base_id(b: str) -> str:
    return re.sub(r"-\d+$", "", str(b))


def build_pypsa_network(
    buses_csv: str | Path,
    lines_csv: str | Path,
    net: NetworkInfo,
):
    """Build a pypsa.Network from the same DE-internal AC lines used by net.

    The bus set is exactly net.bus_idx_to_id (the line endpoints), so PTDF
    columns can be indexed by the same integer bus index as the Dijkstra graph.

    Bridges between co-located voltage levels (same OSM base_id) are added as
    short low-reactance Lines, matching the transformer-bridges in network.py.

    Lines with non-positive or NaN reactance are clipped to a small positive
    value so the susceptance matrix stays well-conditioned.
    """
    import pypsa  # local import: pypsa is heavy; only needed for ptdf kernel

    gb = pd.read_csv(buses_csv)
    de_ids = set(gb[gb.country == "DE"].bus_id)
    gl = pd.read_csv(lines_csv, quotechar="'")
    internal_df = gl[gl.bus0.isin(de_ids) & gl.bus1.isin(de_ids)].reset_index(drop=True)

    # restrict to the same bus set as the Dijkstra graph
    bus_set = set(net.bus_idx_to_id)
    internal_df = internal_df[
        internal_df.bus0.isin(bus_set) & internal_df.bus1.isin(bus_set)
    ].reset_index(drop=True)

    pn = pypsa.Network()
    # buses: keep the same order as net.bus_idx_to_id so column index lines up
    bus_v = gb.set_index("bus_id")["voltage"]
    for b in net.bus_idx_to_id:
        pn.add("Bus", name=str(b), v_nom=float(bus_v.loc[b]))

    # lines: x in Ohm. Clip non-positive / NaN reactance.
    x_raw = pd.to_numeric(internal_df["x"], errors="coerce").to_numpy(dtype=float)
    x_clip = np.where(np.isfinite(x_raw) & (x_raw > 0), x_raw, 1e-3)
    r_raw = pd.to_numeric(internal_df["r"], errors="coerce").to_numpy(dtype=float)
    r_clip = np.where(np.isfinite(r_raw) & (r_raw >= 0), r_raw, 1e-3)
    s_nom = pd.to_numeric(internal_df["s_nom"], errors="coerce").fillna(0.0).to_numpy()

    # de-duplicate parallel lines by name; if duplicate names, append a counter
    seen = defaultdict(int)
    line_names = []
    for lid in internal_df["line_id"].astype(str):
        seen[lid] += 1
        line_names.append(lid if seen[lid] == 1 else f"{lid}#{seen[lid]}")
    for i in range(len(internal_df)):
        pn.add(
            "Line",
            name=line_names[i],
            bus0=str(internal_df.bus0.iloc[i]),
            bus1=str(internal_df.bus1.iloc[i]),
            x=float(x_clip[i]),
            r=float(r_clip[i]),
            s_nom=float(s_nom[i]),
        )

    # bridges between co-located voltage levels (same OSM base_id)
    groups: dict[str, list[str]] = defaultdict(list)
    for b in net.bus_idx_to_id:
        groups[_base_id(b)].append(str(b))
    n_bridge = 0
    for members in groups.values():
        if len(members) > 1:
            m0 = members[0]
            for m in members[1:]:
                pn.add(
                    "Line",
                    name=f"bridge::{m0}::{m}",
                    bus0=m0,
                    bus1=m,
                    x=BRIDGE_X_OHM,
                    r=BRIDGE_R_OHM,
                    s_nom=BRIDGE_S_NOM,
                )
                n_bridge += 1

    pn.determine_network_topology()
    n_sub = len(pn.sub_networks)
    sizes = sorted(
        (len(pn.buses[pn.buses.sub_network == s]) for s in pn.sub_networks.index),
        reverse=True,
    )
    print(
        f"[ptdf] pypsa.Network  buses {len(pn.buses)}  lines {len(pn.lines)} "
        f"(bridges {n_bridge})  sub_networks {n_sub} (largest {sizes[0]})"
    )
    return pn


def compute_ptdf_columns(pn, net: NetworkInfo) -> tuple[np.ndarray, np.ndarray]:
    """Stack per-sub_network PTDF blocks into a global (n_lines_total, n_buses) array.

    Returns
    -------
    ptdf_cols : (n_lines_total, n_buses) float
        Column n holds the concatenated PTDF entries for bus n across all
        sub_networks (zero outside its own sub_network). Bus index follows
        net.bus_idx_to_id; line ordering is sub_network blocks concatenated.
    sub_id : (n_buses,) int
        Sub-network id per bus, used to gate cross-component pairs.
    """
    bus_to_idx = net.bus_id_to_idx
    n_buses = len(net.bus_idx_to_id)

    blocks = []
    sub_id = np.full(n_buses, -1, dtype=int)
    next_sid = 0
    for s_name in pn.sub_networks.index:
        sub = pn.sub_networks.obj.loc[s_name]
        try:
            sub.calculate_PTDF()
        except Exception as e:
            # Some 1-bus sub_networks have no branches and skip silently.
            n_buses_sub = len(sub.components.buses.static)
            n_branches_sub = len(sub.components.lines.static) + len(sub.components.transformers.static)
            if n_branches_sub == 0:
                bus_names = list(sub.components.buses.static.index)
                for b in bus_names:
                    if str(b) in {str(bb) for bb in net.bus_idx_to_id}:
                        sub_id[bus_to_idx[b]] = next_sid
                next_sid += 1
                continue
            raise RuntimeError(f"PTDF failed for sub_network {s_name}: {e}")

        ptdf = np.asarray(sub.PTDF, dtype=float)  # (n_branches_sub, n_buses_sub)
        sub_bus_names = list(sub.components.buses.static.index)
        sub_bus_idx = np.array([bus_to_idx[str(b)] for b in sub_bus_names], dtype=int)

        block = np.zeros((ptdf.shape[0], n_buses), dtype=np.float32)
        block[:, sub_bus_idx] = ptdf.astype(np.float32)
        blocks.append(block)
        sub_id[sub_bus_idx] = next_sid
        next_sid += 1

    if not blocks:
        raise RuntimeError("PTDF: no sub_network produced a PTDF block")
    ptdf_cols = np.vstack(blocks)
    print(
        f"[ptdf] PTDF stack shape {ptdf_cols.shape}  "
        f"nonzero entries {(ptdf_cols != 0).sum()}/{ptdf_cols.size}"
    )
    return ptdf_cols, sub_id


def ptdf_kernel_matrix(
    ptdf_cols: np.ndarray,           # (n_lines, n_buses_total)
    sub_id: np.ndarray,              # (n_buses_total,)
    ev_bus_idx: np.ndarray,          # (n_ev,)  source buses (redispatch event)
    bat_bus_idx: np.ndarray,         # (n_bat,) target buses (batteries)
    kernel_kind: str,
    bandwidth: float,
) -> np.ndarray:
    """Return (n_ev, n_bat) kernel matrix using PTDF-column L2 distance.

    Pairs across different sub_networks are forced to zero.

    The bandwidth here is in dimensionless PTDF units, not metres. Typical PTDF
    column entries are in [-1, 1] so per-line differences are O(1); ||diff||_2
    over hundreds of lines is O(1)-O(10). Start with bandwidth ~ 0.3 - 1.0.
    """
    P_ev = ptdf_cols[:, ev_bus_idx]                 # (n_lines, n_ev)
    P_bat = ptdf_cols[:, bat_bus_idx]               # (n_lines, n_bat)

    # ||a - b||^2 = ||a||^2 + ||b||^2 - 2 a.b   (over the line dimension)
    a2 = (P_ev * P_ev).sum(axis=0)                  # (n_ev,)
    b2 = (P_bat * P_bat).sum(axis=0)                # (n_bat,)
    ab = P_ev.T @ P_bat                              # (n_ev, n_bat)
    d2 = a2[:, None] + b2[None, :] - 2.0 * ab
    d2 = np.maximum(d2, 0.0)
    d = np.sqrt(d2)                                  # (n_ev, n_bat)

    kfn = kernel_function(kernel_kind)
    K = kfn(d, bandwidth)

    # zero across sub_networks
    same_sub = sub_id[ev_bus_idx][:, None] == sub_id[bat_bus_idx][None, :]
    valid = same_sub & (sub_id[ev_bus_idx][:, None] >= 0) & (sub_id[bat_bus_idx][None, :] >= 0)
    K = np.where(valid, K, 0.0)

    print(
        f"[ptdf] kernel matrix shape {K.shape}  "
        f"d range {d[valid].min() if valid.any() else float('nan'):.3f} .. "
        f"{d[valid].max() if valid.any() else float('nan'):.3f}  "
        f"mean K {K.mean():.4f}  nonzero {(K>0).sum()}/{K.size}"
    )
    return K.astype(np.float32)
