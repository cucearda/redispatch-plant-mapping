"""Network graph + graph-distance + kernel.

These are LIFTED VERBATIM from
  Final_Analysis/make_redispatch_grid_kde_area_map.py
so the siting score and the existing KDE map use the SAME distance proxy
and the SAME kernel. The kernel is quartic (Epanechnikov-squared):

    K_h(x) = (1 - (d/h)^2) ** 2     for d/h < 1, else 0

and bandwidth h defaults to 100 km (the network-KDE map's H).

Graph: nodes = bus_id endpoints of DE-internal AC lines; edges = those lines,
weight = line length [m]. Co-located buses (same OSM object, different voltage
suffix) are bridged with a short 250 m transformer edge to keep the 220/380 kV
networks connected. This is identical to the reference map.

Caveat (state in paper): graph distance proxies ELECTRICAL distance. The
rigorous quantity is the PTDF difference on the binding line; redispatch data
in this project does not expose binding-line IDs, so we use the same proxy as
the KDE map.
"""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import networkx as nx
from scipy.spatial import cKDTree
from shapely import wkt

GEO_DEG_M = 111139.0  # metres per degree at the equator


@dataclass
class NetworkInfo:
    G: nx.Graph
    bus_id_to_idx: dict
    bus_idx_to_id: list
    coords_lat: np.ndarray         # (N,)
    coords_lon: np.ndarray         # (N,)
    coords_xy: np.ndarray          # (N,2) local-metric coordinates (m)


def _to_local_xy(lon: np.ndarray, lat: np.ndarray, lat0: float = 51.0) -> np.ndarray:
    """Local equirectangular metric for nearest-neighbour snapping. lat0=51 deg
    matches the reference map's projection."""
    coslat0 = float(np.cos(np.radians(lat0)))
    x = np.asarray(lon, dtype=float) * GEO_DEG_M * coslat0
    y = np.asarray(lat, dtype=float) * GEO_DEG_M
    return np.column_stack([x, y])


def build_network(buses_csv: str | Path, lines_csv: str | Path) -> NetworkInfo:
    """Build the DE 380/220 kV network graph. Identical construction to the KDE map."""
    gb = pd.read_csv(buses_csv)
    de_ids = set(gb[gb.country == "DE"].bus_id)
    gl = pd.read_csv(lines_csv, quotechar="'")
    internal_df = gl[gl.bus0.isin(de_ids) & gl.bus1.isin(de_ids)].reset_index(drop=True)

    # edge length [m]: dataset length column, fallback to geometry length * deg->m
    lengths = pd.to_numeric(internal_df["length"], errors="coerce")
    geoms = internal_df["geometry"].apply(wkt.loads)
    lengths = lengths.fillna(geoms.apply(lambda g: g.length * GEO_DEG_M))
    internal_df = internal_df.assign(_len=lengths.values)

    # bus_id index covers only those buses that appear as line endpoints
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

    # Bridge co-located buses across voltage levels via OSM base_id.
    def base_id(b):
        return re.sub(r"-\d+$", "", str(b))

    groups: dict[str, list[int]] = defaultdict(list)
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
    print(f"[network] nodes {N}  line-edges {len(internal_df)}  transformer-bridges {n_bridge}  "
          f"components {len(comps)} (largest {comps[0]})")

    # bus coords: take xy from buses.csv via bus_id lookup
    bus_xy = gb.set_index("bus_id")[["x", "y"]]
    coords_lon = np.array([float(bus_xy.loc[b, "x"]) for b in nodes])
    coords_lat = np.array([float(bus_xy.loc[b, "y"]) for b in nodes])
    coords_xy = _to_local_xy(coords_lon, coords_lat)

    return NetworkInfo(
        G=G,
        bus_id_to_idx=nidx,
        bus_idx_to_id=list(nodes),
        coords_lat=coords_lat,
        coords_lon=coords_lon,
        coords_xy=coords_xy,
    )


def snap_points_to_buses(lons: np.ndarray, lats: np.ndarray, net: NetworkInfo) -> np.ndarray:
    """Nearest-bus index for each (lon, lat). Planar Euclidean nearest neighbour
    in local-metric XY; from there, graph distance takes over."""
    tree = cKDTree(net.coords_xy)
    pts = _to_local_xy(lons, lats)
    _, idx = tree.query(pts, k=1)
    return idx.astype(int)


def dijkstra_distances(net: NetworkInfo, sources: np.ndarray, cutoff_m: float) -> np.ndarray:
    """Graph distances [m] from each source bus index to every bus, cutoff at cutoff_m.

    Returns dense (len(sources), N) array; unreachable entries are np.inf.
    """
    N = len(net.bus_idx_to_id)
    out = np.full((len(sources), N), np.inf)
    for k, s in enumerate(sources):
        d = nx.single_source_dijkstra_path_length(net.G, int(s), cutoff=cutoff_m, weight="w")
        for t, dist in d.items():
            out[k, t] = dist
    return out


def quartic_kernel(d_m: np.ndarray, h_m: float) -> np.ndarray:
    """K_h(d) = (1 - (d/h)^2) ** 2 for d/h < 1 else 0. Same kernel as the network-KDE map.

    Hard cutoff at d = h. Use Dijkstra cutoff_m = h.
    """
    x = np.asarray(d_m, dtype=float) / float(h_m)
    k = np.where(x < 1.0, (1.0 - x * x) ** 2, 0.0)
    k = np.where(np.isfinite(x), k, 0.0)
    return k


def gaussian_kernel(d_m: np.ndarray, sigma_m: float) -> np.ndarray:
    """K_sigma(d) = exp(-0.5 * (d/sigma)^2). No hard cutoff.

    Compared to the quartic at the same bandwidth, this kernel has BROADER effective
    spatial reach: at d = sigma it returns 0.61 (vs 0 for quartic). The AMISE-canonical
    equivalent (Wand & Jones 1995) is sigma_gauss = h_quartic / 2.78, but here we follow
    the user's preference and keep sigma = bandwidth_h_m as configured. For the Dijkstra
    cutoff, use a multiple of sigma (default 4*sigma captures ~99.99% of the weight).
    """
    x = np.asarray(d_m, dtype=float) / float(sigma_m)
    k = np.exp(-0.5 * x * x)
    k = np.where(np.isfinite(x), k, 0.0)
    return k


def kernel_function(kind: str):
    """Resolve a kernel by name. Returns a callable f(d_m, bandwidth_m)."""
    kind = kind.lower().strip()
    if kind == "quartic":
        return quartic_kernel
    if kind == "gaussian":
        return gaussian_kernel
    raise ValueError(f"unknown kernel type: {kind!r}; choose 'quartic' or 'gaussian'")


def dijkstra_cutoff_for(kind: str, bandwidth_m: float, cutoff_sigmas: float = 4.0) -> float:
    """Pick the Dijkstra cutoff [m] for a given kernel type.

    Quartic: exact zero past d=h, so cutoff = bandwidth.
    Gaussian: choose cutoff_sigmas * sigma (default 4 -> kernel ~3.4e-4 at the cutoff).
    """
    kind = kind.lower().strip()
    if kind == "quartic":
        return float(bandwidth_m)
    if kind == "gaussian":
        return float(bandwidth_m) * float(cutoff_sigmas)
    raise ValueError(f"unknown kernel type: {kind!r}")
