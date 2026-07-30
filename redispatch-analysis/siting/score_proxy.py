"""Fast screening proxy for the daily LP (quarter-hourly).

V_proxy[n] ~= Sum_days (mean top-k pi_hat - mean bottom-k pi_hat) * p_bar * k * dt

with k = round(duration_hours / dt) the number of 15-min steps it takes to fill
E = duration_hours * p_bar at full power, and dt the step length in hours. Each
of the k charge/discharge steps moves p_bar * dt MWh. This is the lossless
analytic answer to the LP when k charges + k discharges fit and there are no
efficiency losses; with losses it overestimates a touch.
"""
from __future__ import annotations
import numpy as np

from .score_lp import DT_HOURS, STEPS_PER_DAY


def daily_proxy(pi: np.ndarray, p_bar: float, k: int, dt: float = DT_HOURS) -> float:
    if p_bar <= 0 or k <= 0:
        return 0.0
    pi = np.asarray(pi, dtype=float)
    n = len(pi)
    if k * 2 > n:
        k = n // 2
        if k == 0:
            return 0.0
    s = np.sort(pi)
    top = s[-k:].mean()
    bot = s[:k].mean()
    return float((top - bot) * p_bar * k * dt)


def annual_proxy(pi_year: np.ndarray, p_bar: float, duration_hours: float,
                 dt: float = DT_HOURS) -> float:
    steps_per_day = STEPS_PER_DAY
    k = int(round(duration_hours / dt))
    n_days = len(pi_year) // steps_per_day
    total = 0.0
    for d in range(n_days):
        pi = pi_year[d*steps_per_day:(d+1)*steps_per_day]
        total += daily_proxy(pi, p_bar, k, dt=dt)
    return total


def proxy_copperplate_per_mw(p_base: np.ndarray, duration_hours: float,
                             dt: float = DT_HOURS) -> float:
    """Proxy copperplate baseline: the screening proxy evaluated on the flat,
    spatially-uniform zonal price `p_base` (no wedge), per MW over the window.

    This is the correct baseline to subtract from `annual_proxy(pi_hat, ...)`:
    BOTH sides use the single-block proxy estimator, so the difference is the
    pure locational premium the wedge adds and nothing else. (Subtracting the
    *LP* copperplate instead mixes a multi-cycle, lossy LP with a single-cycle,
    lossless proxy and produces a spurious negative offset of ~25 kEUR/MW/yr.)
    """
    return annual_proxy(np.asarray(p_base, dtype=float), 1.0, duration_hours, dt=dt)
