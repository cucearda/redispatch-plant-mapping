"""Data loading + timezone-aware price join for the battery siting scorer.

Resolution: the whole pipeline runs at QUARTER-HOURLY (15-min) resolution. Prices
are loaded at their native 15-min UTC grid (no resampling), redispatch events are
expanded onto that same 15-min grid, and the LP cycles over 96 steps/day with
dt = 0.25 h.

Data contract (confirmed Step 0 / 0b):
  batteries:   Final_Analysis/commercial_battery_storage.csv  (active only)
  redispatch:  ALLES_NEU/redispatch_joined_high_confidence.csv  (semicolon, utf-8-sig)
  price:       ALLES_NEU/intraday_prices_de_15min.csv  (native 15-min UTC)

Cleaning policy applied here (with counts logged):
  - drop redispatch rows with duration_h <= 0  (56 rows, all pre-2018, outside price window anyway)
  - drop redispatch rows with DST-impossible timestamps  (typically ~11 rows)
  - drop batteries with NaN lat/lon  (25 sub-MW residential units, ~41 MW of ~31 GW fleet)

Timezone handling (critical):
  - redispatch ts_start is wall-clock per ZEITZONE_VON, NOT UTC. Localize per row using
    {UTC, CET, CEST} -> {UTC, Europe/Berlin} and convert to UTC before joining prices.
  - Naive merge on the string ts_start would silently misalign ~67% of rows.
  - Prices carry an explicit timestamp_utc; UTC has no DST gaps, so a continuous
    15-min UTC grid is fully populated (no spring-forward hole to interpolate).
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

TZ_MAP = {"UTC": "UTC", "CET": "Europe/Berlin", "CEST": "Europe/Berlin"}

# Time resolution of the whole pipeline.
STEP_FREQ = "15min"          # pandas offset alias for the canonical step grid
STEPS_PER_HOUR = 4
DT_HOURS = 0.25              # length of one step in hours


def load_batteries(path: str | Path, status_filter=("active",), drop_nan_coords=True) -> pd.DataFrame:
    bat = pd.read_csv(path)
    n0 = len(bat)
    bat = bat[bat["status_cat"].isin(list(status_filter))].copy()
    n1 = len(bat)
    if drop_nan_coords:
        miss = bat[bat[["lat", "lon"]].isna().any(axis=1)]
        bat = bat.dropna(subset=["lat", "lon"]).copy()
        print(f"[batteries] {n0} -> filter {status_filter}: {n1} -> drop NaN coords: {len(bat)}"
              f" (dropped {len(miss)} rows, sum power_mw = {miss['power_mw'].sum():.1f})")
    bat["p_bar_mw"] = bat["power_mw"].astype(float)
    return bat.reset_index(drop=True)


def make_artificial_grid(
    buses_csv: str | Path,
    spacing_deg: float = 0.25,
    power_mw: float = 50.0,
    max_dist_km: float = 25.0,
) -> pd.DataFrame:
    """Build a regular lattice of hypothetical batteries covering Germany.

    Drop-in replacement for `load_batteries`: returns the SAME columns the rest of
    the pipeline consumes (lat, lon, power_mw, p_bar_mw, status_cat) plus synthetic
    metadata (name, EinheitMastrNummer), so snapping, wedge, scoring and ranking
    run unchanged. Instead of the real MaStR fleet, every point is a candidate
    site, turning the ranking into a locational-value MAP of "where WOULD a battery
    be worth siting" rather than "how good is each existing battery".

    Extent is the bounding box of the DE buses in `buses_csv` (so the lattice
    covers exactly the modeled network area). Points farther than `max_dist_km`
    from any DE bus are dropped, which clips the lattice to the German grid
    footprint (removes sea / foreign-territory cells that would otherwise snap to a
    distant border bus).

    Parameters
    ----------
    spacing_deg : lattice step in degrees for both lat and lon (0.25 deg ~= 20-28 km).
    power_mw    : nameplate assigned to every candidate (p_bar_mw). With the config's
                  baseline.use_fixed_mw=true this is overridden by fixed_mw anyway,
                  so it only matters when scoring at own nameplate.
    max_dist_km : keep a cell only if a DE bus lies within this distance.
    """
    from scipy.spatial import cKDTree

    gb = pd.read_csv(buses_csv)
    de = gb[gb["country"] == "DE"]
    blon = de["x"].to_numpy(dtype=float)
    blat = de["y"].to_numpy(dtype=float)

    lon0, lon1 = blon.min(), blon.max()
    lat0, lat1 = blat.min(), blat.max()
    lon_grid = np.arange(lon0, lon1 + spacing_deg, spacing_deg)
    lat_grid = np.arange(lat0, lat1 + spacing_deg, spacing_deg)
    LON, LAT = np.meshgrid(lon_grid, lat_grid)
    cand_lon = LON.ravel()
    cand_lat = LAT.ravel()

    # Keep only cells within max_dist_km of a DE bus. Use a local equirectangular
    # metric (metres) centred at the bbox mid-latitude for the nearest-bus query.
    GEO_DEG_M = 111139.0
    lat_mid = 0.5 * (lat0 + lat1)
    coslat = float(np.cos(np.radians(lat_mid)))
    bus_xy = np.column_stack([blon * GEO_DEG_M * coslat, blat * GEO_DEG_M])
    cand_xy = np.column_stack([cand_lon * GEO_DEG_M * coslat, cand_lat * GEO_DEG_M])
    dist_m, _ = cKDTree(bus_xy).query(cand_xy, k=1)
    keep = dist_m <= max_dist_km * 1000.0
    cand_lon, cand_lat = cand_lon[keep], cand_lat[keep]

    n = len(cand_lon)
    bat = pd.DataFrame({
        "lat": cand_lat,
        "lon": cand_lon,
        "power_mw": np.full(n, float(power_mw)),
        "status_cat": "active",
        "name": [f"grid_{i:05d}" for i in range(n)],
        "EinheitMastrNummer": [f"GRID{i:05d}" for i in range(n)],
    })
    bat["p_bar_mw"] = bat["power_mw"].astype(float)
    print(f"[grid] artificial lattice: {len(lon_grid)}x{len(lat_grid)} cells over "
          f"lon[{lon0:.2f},{lon1:.2f}] lat[{lat0:.2f},{lat1:.2f}] "
          f"-> {n} kept within {max_dist_km:g} km of a DE bus (spacing {spacing_deg} deg)")
    return bat.reset_index(drop=True)


def _localize_per_tz(ts: pd.Series, tz_col: pd.Series) -> pd.Series:
    """Localize a wall-clock series to UTC, choosing tz from a parallel column per row.

    ambiguous='NaT' and nonexistent='NaT' so DST-impossible wall-clocks become NaT
    rather than silently snapping into a neighbour hour.
    """
    out = pd.Series(pd.NaT, index=ts.index, dtype="datetime64[ns, UTC]")
    for tz, idx in tz_col.groupby(tz_col).groups.items():
        loc = ts.loc[idx].dt.tz_localize(TZ_MAP[tz], ambiguous="NaT", nonexistent="NaT")
        out.loc[idx] = loc.dt.tz_convert("UTC")
    return out


def load_redispatch(path: str | Path) -> pd.DataFrame:
    """Load + timezone-normalize + sign-map redispatch events.

    Returns columns:
      ts_start_utc, ts_end_utc      : tz-aware UTC
      sign                          : +1 for "erhoehen" (up-ramp), -1 for "reduzieren" (down-ramp)
      lat, lon                      : event coordinates
      MITTLERE_LEISTUNG_MW          : mean power
      GESAMTE_ARBEIT_MWH            : total energy
      RICHTUNG                      : original string (kept for debugging)
    """
    df = pd.read_csv(path, sep=";", low_memory=False, encoding="utf-8-sig")
    n0 = len(df)

    # 1) Drop bad-duration rows (BEGINN/ENDE swap or zero-length).
    df = df[df["duration_h"] > 0].copy()
    print(f"[redispatch] {n0} -> drop duration_h<=0: {len(df)}")

    # 2) Parse datetimes (wall-clock strings, no tz info).
    df["ts_start"] = pd.to_datetime(df["ts_start"], errors="coerce")
    df["ts_end"] = pd.to_datetime(df["ts_end"], errors="coerce")

    # 3) Localize per-row using the ZEITZONE column, convert to UTC.
    df["ts_start_utc"] = _localize_per_tz(df["ts_start"], df["ZEITZONE_VON"])
    df["ts_end_utc"] = _localize_per_tz(df["ts_end"], df["ZEITZONE_BIS"])
    n_dst = df[["ts_start_utc", "ts_end_utc"]].isna().any(axis=1).sum()
    df = df.dropna(subset=["ts_start_utc", "ts_end_utc"]).copy()
    print(f"[redispatch] drop DST-impossible: {n_dst} -> {len(df)}")

    # 4) Sign from RICHTUNG. After encoding fix the strings are
    #      "Wirkleistungseinspeisung reduzieren"   -> down-ramp -> sign -1 (battery charges)
    #      "Wirkleistungseinspeisung erhoehen"     -> up-ramp   -> sign +1 (battery discharges)
    r = df["RICHTUNG"].str.strip()
    sign = np.where(r.str.contains("erh", case=False, na=False), 1,
                    np.where(r.str.contains("reduzier", case=False, na=False), -1, np.nan))
    assert not np.isnan(sign).any(), (
        f"Unmapped RICHTUNG values: {sorted(df.loc[np.isnan(sign), 'RICHTUNG'].unique())}"
    )
    df["sign"] = sign.astype(int)

    keep = ["ts_start_utc", "ts_end_utc", "sign", "lat", "lon",
            "MITTLERE_LEISTUNG_MW", "GESAMTE_ARBEIT_MWH", "RICHTUNG"]
    return df[keep].reset_index(drop=True)


def load_prices_quarterhourly(path: str | Path) -> pd.Series:
    """Load native 15-min intraday prices onto a continuous UTC quarter-hour grid.

    No resampling: the file is already 15-min. We reindex onto a gap-free 15-min
    UTC grid spanning [min, max] so every step is present, then assert no NaN.
    Because the index is UTC, there is no DST spring-forward gap to fill.
    """
    px = pd.read_csv(path)
    px["ts"] = pd.to_datetime(px["timestamp_utc"], utc=True)
    s = (px.set_index("ts")["price_eur_per_mwh"]
           .sort_index()
           .rename("p_base"))
    # collapse any accidental duplicate timestamps (mean), then reindex to a
    # continuous 15-min grid to expose gaps as NaN.
    s = s[~s.index.duplicated(keep="first")]
    full_idx = pd.date_range(s.index.min(), s.index.max(), freq=STEP_FREQ, tz="UTC")
    s = s.reindex(full_idx)
    n_nan = int(s.isna().sum())
    print(f"[price] 15-min UTC steps: {len(s)}  NaN: {n_nan}  range: {s.index.min()} -> {s.index.max()}")
    if n_nan:
        raise RuntimeError(f"Quarter-hourly price has {n_nan} NaN steps; expected 0 with continuous 15-min input.")
    return s


def filter_to_window(df_red: pd.DataFrame, prices: pd.Series,
                     window_start: pd.Timestamp, window_end: pd.Timestamp) -> tuple[pd.DataFrame, pd.Series]:
    """Restrict redispatch + prices to the [window_start, window_end] UTC window.

    window_end is inclusive (the LP uses the last step in the day window).
    """
    p = prices.loc[(prices.index >= window_start) & (prices.index <= window_end)]
    in_w = (df_red["ts_end_utc"] > window_start) & (df_red["ts_start_utc"] <= window_end)
    n_out = (~in_w).sum()
    df = df_red[in_w].copy()
    print(f"[window] {window_start} -> {window_end}: "
          f"redispatch {len(df_red)} -> {len(df)} (-{n_out} outside); "
          f"price steps: {len(p)}")
    return df, p


def expand_events_to_quarterhours(df_red: pd.DataFrame, prices: pd.Series) -> pd.DataFrame:
    """Expand each event into the 15-min UTC steps it overlaps.

    Returns long-format DataFrame: [event_idx, step_utc, sign, lat, lon, mw]
    where event_idx is the row index in df_red, step_utc is a quarter-hour bin on
    the canonical price grid, and mw is the event's mean power (MITTLERE_LEISTUNG_MW),
    carried per step so the wedge can be volume-weighted (MW of congestion-relief
    active in that step).
    """
    rows = []
    step_idx = prices.index  # the canonical 15-min grid
    s_min = step_idx.min()
    s_max = step_idx.max()
    has_mw = "MITTLERE_LEISTUNG_MW" in df_red.columns
    for i, r in df_red.iterrows():
        s0 = max(r["ts_start_utc"].floor(STEP_FREQ), s_min)
        s1 = min((r["ts_end_utc"] - pd.Timedelta(nanoseconds=1)).floor(STEP_FREQ), s_max)
        if s1 < s0:
            continue
        mw = float(r["MITTLERE_LEISTUNG_MW"]) if has_mw else 1.0
        steps = pd.date_range(s0, s1, freq=STEP_FREQ, tz="UTC")
        for s in steps:
            rows.append((i, s, int(r["sign"]), float(r["lat"]), float(r["lon"]), mw))
    out = pd.DataFrame(rows, columns=["event_idx", "step_utc", "sign", "lat", "lon", "mw"])
    print(f"[expand] events {len(df_red)} -> quarter-hour rows {len(out)}  "
          f"(mean {len(out)/max(len(df_red),1):.2f} steps/event)")
    return out
