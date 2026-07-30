"""Acceptance tests for the battery siting scorer (QUARTER-HOURLY, dt=0.25).

The whole pipeline runs at 15-min resolution: 96 steps/day, dt=0.25 h, and a
4 h battery (E = 4*p_bar) charges/discharges over 16 steps at full power.

  1. Golden lossless LP:  p_bar=1, E=4, eta=1, dt=0.25,
     pi = [10]*16 + [90]*16  ->  charge 16 steps @10, discharge 16 steps @90.
     Energy in/out = 16 * p_bar * dt = 4 MWh = E. Profit = dt*(16*90 - 16*10)
     = 0.25*1280 = 320.  Exact equality required.
  2. Sign: a lone cheap step makes the LP charge there; a lone expensive step
     makes it discharge there. Flat pi -> 0 baseline.
  3. Three synthetic batteries with equal total nearby volume:
       A: down-ramps on windy steps only        (perfect alignment)
       B: up+down on the SAME steps (straddle)   (cancels)
       C: up+down on DIFFERENT steps             (best arbitrage)
     Assert  V_C > V_A > V_B  and  V_B ~= V_copperplate.

All horizons are built at 15-min resolution so they exercise the production
dt=0.25 path directly (no dt overrides).
"""
from __future__ import annotations
import math
import sys
from pathlib import Path
import numpy as np

# package importable from test dir
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from battery_siting_score.score_lp import daily_arbitrage_profit, STEPS_PER_DAY


def test_golden_lossless():
    # 32-step horizon: 16 cheap steps then 16 expensive steps, the quarter-hourly
    # analogue of "charge 4h@10, discharge 4h@90 -> 320".
    pi = np.concatenate([np.full(16, 10.0), np.full(16, 90.0)])
    profit = daily_arbitrage_profit(pi, p_bar=1.0, E=4.0, eta=1.0)  # dt=0.25 default
    assert math.isclose(profit, 320.0, abs_tol=1e-4), f"got {profit}"


def test_sign_down_ramp_charges_lp_via_pi_hat():
    """A near down-ramp pushes pi_hat DOWN at that step. LP should charge then.

    We sidestep the wedge wiring and feed pi_hat directly over a full 96-step day.
    """
    pi = np.full(STEPS_PER_DAY, 50.0)
    pi[48] = -200.0   # very cheap step
    pi[72] =  500.0   # very expensive step
    profit = daily_arbitrage_profit(pi, p_bar=1.0, E=4.0, eta=1.0)
    baseline = daily_arbitrage_profit(np.full(STEPS_PER_DAY, 50.0), p_bar=1.0, E=4.0, eta=1.0)
    assert baseline == 0.0
    assert profit > 0.0
    # With E=4, p_bar=1, dt=0.25 the LP charges the cheapest 16 steps and
    # discharges the most expensive 16. The 15 charge/discharge steps at 50 cancel;
    # the extreme pair clears dt*(500 - (-200)) = 0.25*700 = 175.
    assert profit >= 175.0 - 1e-6, f"profit too low: {profit}"


def test_three_batteries_ordering():
    """V_C > V_A > V_B, with V_B ~= V_copperplate, on a 96-step (15-min) day.

    Build pi_hat directly for three batteries:
      base = constant 50 EUR/MWh
      windy steps: [16:32] (= hours 4..7); calm steps: [40:48] (= hours 10..11)
      wedge_amp = +/- 40 EUR/MWh
    A: wedge=-40 on windy steps only            -> cheap charge window
    B: wedge=-40 AND +40 on the same steps      -> cancels -> ~ copperplate
    C: wedge=-40 on [16:32], +40 on [40:48]     -> charge cheap, discharge dear
    """
    base = np.full(STEPS_PER_DAY, 50.0)
    pi_A = base.copy(); pi_A[16:32] -= 40.0
    pi_B = base.copy()  # cancels -> exactly flat
    pi_C = base.copy(); pi_C[16:32] -= 40.0; pi_C[40:48] += 40.0

    V_A = daily_arbitrage_profit(pi_A, p_bar=1.0, E=4.0, eta=1.0)
    V_B = daily_arbitrage_profit(pi_B, p_bar=1.0, E=4.0, eta=1.0)
    V_C = daily_arbitrage_profit(pi_C, p_bar=1.0, E=4.0, eta=1.0)
    V_cp = daily_arbitrage_profit(base, p_bar=1.0, E=4.0, eta=1.0)

    # Sanity on the copperplate
    assert V_cp == 0.0, f"flat pi should yield 0 arbitrage; got {V_cp}"
    # B cancels -> equals copperplate
    assert V_B == V_cp == 0.0, f"B should equal copperplate; got V_B={V_B}, V_cp={V_cp}"
    # Strict ordering C > A > B
    assert V_C > V_A > V_B, f"ordering broken: V_A={V_A}, V_B={V_B}, V_C={V_C}"
    # A charges 16 steps @10, discharges 16 steps @50: dt*(16*50 - 16*10)
    #   = 0.25*640 = 160.
    assert math.isclose(V_A, 160.0, abs_tol=1e-4), f"V_A expected 160, got {V_A}"
    # C charges 16 steps @10; discharges the 8 steps @90 [40:48] then 8 more @50:
    #   dt*(8*90 + 8*50 - 16*10) = 0.25*960 = 240.
    assert math.isclose(V_C, 240.0, abs_tol=1e-4), f"V_C expected 240, got {V_C}"


if __name__ == "__main__":
    test_golden_lossless();             print("ok  test_golden_lossless")
    test_sign_down_ramp_charges_lp_via_pi_hat(); print("ok  test_sign_down_ramp_charges_lp_via_pi_hat")
    test_three_batteries_ordering();    print("ok  test_three_batteries_ordering")
    print("ALL TESTS PASS")
