"""Rank batteries by locational value: V, V_copperplate, dV; diagnostics."""
from __future__ import annotations
import os
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

from .score_lp import annual_profit, STEPS_PER_YEAR


def _worker(args):
    """Process-pool worker: compute V for a chunk of batteries.

    args = (chunk_indices_list, pi_hat_chunk_array, p_bars_chunk, duration_hours, eta_roundtrip)
    Returns dict {battery_idx: V_eur}.
    """
    idxs, pi_chunk, p_bars, D_h, eta_rt = args
    out = {}
    for k, j in enumerate(idxs):
        out[j] = annual_profit(pi_chunk[:, k], float(p_bars[k]), D_h, eta_rt)
    return out


def per_battery_diagnostics(
    bat_df: pd.DataFrame,            # battery roster, must include p_bar_mw
    pi_hat: np.ndarray,              # (n_h, n_b)
    wedge_only: np.ndarray,          # (n_h, n_b)
    duration_hours: float,
    eta_roundtrip: float,
    V_copperplate_per_mw: float,     # EUR per MW per year, computed once on p_base
    n_workers: int = 1,
    chunk_size: int = 50,
    score_power_mw: np.ndarray | None = None,   # override p_bar for scoring (e.g. fixed 50 MW)
    run_lp: bool = True,                          # False -> skip LP, V_eur/dV_eur left as NaN
) -> pd.DataFrame:
    """Compute V[n], dV[n] and per-battery diagnostics.

    The power used for the LP bounds and the copperplate is `score_power_mw` if
    given, else each battery's own `p_bar_mw`. Passing a constant array (e.g. all
    50 MW) scores every battery at a common reference size, making the copperplate
    identical across the fleet and dV a pure locational signal.

    Parallelizes the daily LP across batteries using a ProcessPoolExecutor.

    If `run_lp` is False the (expensive) LP is skipped entirely: the vectorized
    wedge diagnostics and the copperplate are still produced, but V_eur and dV_eur
    are returned as NaN. The caller is then expected to rank on the proxy instead.
    """
    n_t, n_b = pi_hat.shape
    assert n_b == len(bat_df)

    diag = {
        "battery_idx": np.arange(n_b),
        "score_power_mw": np.zeros(n_b),
        "V_eur": np.zeros(n_b),
        "V_copperplate_eur": np.zeros(n_b),
        "dV_eur": np.zeros(n_b),
        "steps_nonzero_wedge": np.zeros(n_b, dtype=int),
        "mean_abs_wedge_eur_per_mwh": np.zeros(n_b),
        "frac_pos_wedge": np.zeros(n_b),
        "frac_neg_wedge": np.zeros(n_b),
    }

    if score_power_mw is None:
        p_bars = bat_df["p_bar_mw"].to_numpy(dtype=float)
    else:
        p_bars = np.asarray(score_power_mw, dtype=float)
        assert len(p_bars) == n_b, "score_power_mw must have one entry per battery"
    diag["score_power_mw"] = p_bars
    n_years = n_t / STEPS_PER_YEAR

    # Diagnostics on wedge (vectorized, instant)
    nz_mask = wedge_only != 0
    diag["steps_nonzero_wedge"] = nz_mask.sum(axis=0).astype(int)
    diag["mean_abs_wedge_eur_per_mwh"] = np.mean(np.abs(wedge_only), axis=0)
    diag["frac_pos_wedge"] = (wedge_only > 0).sum(axis=0) / n_t
    diag["frac_neg_wedge"] = (wedge_only < 0).sum(axis=0) / n_t
    diag["V_copperplate_eur"] = V_copperplate_per_mw * p_bars

    if not run_lp:
        print("[LP] skipped (method='proxy') -> V_eur/dV_eur set to NaN; rank on the proxy")
        diag["V_eur"][:] = np.nan
        diag["dV_eur"][:] = np.nan
    else:
        # Build chunks
        all_idx = np.arange(n_b)
        chunks = [all_idx[i:i+chunk_size] for i in range(0, n_b, chunk_size)]
        print(f"[LP] {n_b} batteries -> {len(chunks)} chunks of <= {chunk_size}, {n_workers} workers")

        if n_workers > 1:
            tasks = [(chunk.tolist(), pi_hat[:, chunk], p_bars[chunk], duration_hours, eta_roundtrip)
                     for chunk in chunks]
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures = {ex.submit(_worker, t): k for k, t in enumerate(tasks)}
                done = 0
                for fut in as_completed(futures):
                    res = fut.result()
                    for j, V in res.items():
                        diag["V_eur"][j] = V
                    done += 1
                    print(f"  [LP] chunk {done}/{len(chunks)} done")
        else:
            for k, chunk in enumerate(chunks):
                res = _worker((chunk.tolist(), pi_hat[:, chunk], p_bars[chunk], duration_hours, eta_roundtrip))
                for j, V in res.items():
                    diag["V_eur"][j] = V
                print(f"  [LP] chunk {k+1}/{len(chunks)} done (serial)")

        diag["dV_eur"] = diag["V_eur"] - diag["V_copperplate_eur"]

    out = pd.DataFrame(diag)
    # annualize for reporting
    out["V_eur_per_year"] = out["V_eur"] / max(n_years, 1e-9)
    out["dV_eur_per_year"] = out["dV_eur"] / max(n_years, 1e-9)
    out["equivalent_full_cycles_per_year"] = np.nan  # filled later if SoC trajectories kept
    return out


def join_with_meta(rank_df: pd.DataFrame, bat_df: pd.DataFrame,
                   sort_col: str = "dV_eur_per_year") -> pd.DataFrame:
    """Attach battery metadata (name, state, lat/lon, p_bar) to the ranking table.

    Rows are sorted descending by `sort_col` (the LP's dV in 'lp' mode, the
    proxy's dV in 'proxy' mode).
    """
    cols = ["name", "p_bar_mw", "lat", "lon", "state", "status_cat",
            "commissioning_date", "EinheitMastrNummer", "colocated_solar_id"]
    cols = [c for c in cols if c in bat_df.columns]
    meta = bat_df[cols].reset_index(drop=True)
    meta["battery_idx"] = np.arange(len(meta))
    out = rank_df.merge(meta, on="battery_idx", how="left")
    out = out.sort_values(sort_col, ascending=False).reset_index(drop=True)
    out["rank_by_dV"] = np.arange(1, len(out) + 1)
    return out
