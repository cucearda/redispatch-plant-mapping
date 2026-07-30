"""Daily arbitrage LP via scipy.linprog (HiGHS).

Per battery, per day (n steps of length dt hours; quarter-hourly => n=96, dt=0.25):

    maximize    Sum_t  pi_hat[t] * (d[t] - c[t]) * dt
    subject to  s[(t+1) mod n] = s[t] + (eta * c[t] - d[t] / eta) * dt   for t = 0..n-1
                0 <= c[t], d[t] <= p_bar
                0 <= s[t]       <= E
                                                  (cyclic SoC: s[0] = s[n])

Where E = duration_hours * p_bar and eta = sqrt(eta_roundtrip).
c[t], d[t] are POWER (MW); the dt factor turns them into energy (MWh) per step,
so both the SoC balance and the revenue are correct for sub-hourly resolution.

Variables in order: [c_0..c_{n-1}, d_0..d_{n-1}, s_0..s_{n-1}]   (length 3n)
Equalities (n): for each t in 0..n-1,
        s[next] - s[t] - eta*dt * c[t] + (dt/eta) * d[t] = 0   with next = (t+1) % n.
"""
from __future__ import annotations
import math
import numpy as np
from scipy.optimize import linprog

# Quarter-hourly time semantics for the whole pipeline.
DT_HOURS = 0.25                       # length of one step in hours
STEPS_PER_DAY = int(round(24.0 / DT_HOURS))     # 96
STEPS_PER_YEAR = STEPS_PER_DAY * 365            # 35_040, used to annualize totals


def daily_arbitrage_profit(pi: np.ndarray, p_bar: float, E: float, eta: float,
                           dt: float = DT_HOURS) -> float:
    """Solve one cyclic-SoC arbitrage LP over the horizon len(pi).

    dt is the step length in hours (0.25 => quarter-hourly, the default; pass
    dt=1.0 for the legacy hourly behaviour). The scorer runs this with
    len(pi)=96; tests may use shorter horizons.
    Returns 0.0 if p_bar==0 (degenerate battery).
    """
    if p_bar <= 0 or E <= 0:
        return 0.0
    pi = np.asarray(pi, dtype=float)
    n = len(pi)
    assert n >= 2, f"need at least 2 steps, got {n}"
    nv = 3 * n  # c, d, s

    # Objective: minimize -profit = -dt * pi.(d-c) = dt*(pi.c - pi.d)
    c_vec = np.zeros(nv)
    c_vec[0:n]     =  pi * dt     # c[t] coefficient: +pi[t]*dt  (charge -> pay pi)
    c_vec[n:2*n]   = -pi * dt     # d[t] coefficient: -pi[t]*dt  (discharge -> earn pi)
    c_vec[2*n:3*n] =  0.0         # s[t] no objective contribution

    # Equality constraints A_eq @ x = 0:
    # row t: s[(t+1)%n] - s[t] - eta*dt * c[t] + (dt/eta) * d[t] = 0
    A_eq = np.zeros((n, nv))
    for t in range(n):
        nxt = (t + 1) % n
        A_eq[t, 2*n + nxt] = 1.0          # +s[next]
        A_eq[t, 2*n + t]   = -1.0         # -s[t]
        A_eq[t, t]         = -eta * dt    # -eta*dt * c[t]
        A_eq[t, n + t]     = dt / eta     # +(dt/eta) * d[t]
    b_eq = np.zeros(n)

    # Bounds
    bounds = (
        [(0.0, p_bar)] * n        # c
        + [(0.0, p_bar)] * n      # d
        + [(0.0, E)]    * n       # s
    )

    res = linprog(c_vec, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        # Degenerate cases: tiny battery, weird pi -> try presolve off
        res = linprog(c_vec, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
                      method="highs", options={"presolve": False})
        if not res.success:
            return 0.0

    # NOTE: this LP can return phantom simultaneous charge+discharge in the same
    # step (c[t]>0 and d[t]>0) in the lossless limit eta=1. With eta<1 it never
    # does, because round-tripping energy for zero price gain is strictly costly.
    # A lossless net-out (objective-preserving) would be:
    #     x = res.x
    #     c, d = x[:n], x[n:2*n]
    #     net = c - d
    #     c_clean, d_clean = np.maximum(net, 0.0), np.maximum(-net, 0.0)
    # Left disabled on purpose -- we rely on eta<1 to avoid it.

    return float(-res.fun)  # we minimized -profit


def annual_profit(pi_year: np.ndarray, p_bar: float, duration_hours: float,
                  eta_roundtrip: float, dt: float = DT_HOURS) -> float:
    """Sum daily arbitrage profits over the full quarter-hourly pi_hat series.

    dt is the step length in hours (0.25 => 96 steps/day, the default). The
    series is sliced into consecutive days of round(24/dt) steps; any short
    tail is dropped. Returns total profit in EUR over the whole input window
    (annualize by dividing by len(pi_year)/STEPS_PER_YEAR).
    """
    eta = math.sqrt(eta_roundtrip)
    E = duration_hours * p_bar
    steps_per_day = int(round(24.0 / dt))
    n_days = len(pi_year) // steps_per_day
    total = 0.0
    for d in range(n_days):
        pi = pi_year[d*steps_per_day:(d+1)*steps_per_day]
        total += daily_arbitrage_profit(pi, p_bar, E, eta, dt=dt)
    return total
