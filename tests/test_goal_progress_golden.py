"""Golden TWR and MWR values from real fills (docs/adr-goals-and-loss-hedge-shadow.md, Phase 2).

Written FIRST. Skipped until ``dca_goal_progress.py`` lands; then these are the acceptance numbers.

The expected values below were NOT produced by the new code. They were computed with plain
Decimal arithmetic in a separate calculation (a spreadsheet-style chain, written out line by line
in the comments) from the sanitized fixture ``tests/fixtures/goal_progress/real_fills_sanitized.json``
and the daily closes in ``golden_closes.json``. The fixture holds real filled market buys from the
scheduled runs of 2026-10-26 and 2026-10-27 (CHE and HRL), with closes from the Yahoo archive.

Conventions, fixed here so the implementation cannot pick its own:

* A buy is a contribution (an external cash flow) of its dollar amount on its trading day.
* Shares bought = quantity from the fill. Holdings are valued at that day's close.
* Sub-period return for day t:  r_t = V_t / (V_(t-1) + C_t) - 1, where C_t is that day's
  contributions and V_t the end-of-day holdings value. The chain is TWR = prod(1 + r_t) - 1.
* MWR is XIRR: sum(CF_i / (1 + x) ** (days_i / 365)) = 0 with CF = -contribution on each buy
  day and +V_end on the valuation day. Days are calendar days from the first cash flow.

Window (why it is short): the closes in the Yahoo archive end on 2026-11-04, so the golden window
is 2026-10-26 to 2026-11-04 (9 calendar days). Over 9 days the annualized XIRR is a very large
number. That is arithmetically right and is pinned here on purpose; do not "fix" the test by
trimming it. The cumulative TWR is the meaningful golden figure for this window.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

dca_goal_progress = pytest.importorskip(
    "dca_goal_progress", reason="dca_goal_progress.py lands with Phase 2 (ADR tests 13-15)"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "goal_progress"
TOL_TWR = Decimal("1E-18")  # the expected values are exact decimal chains to 20 places
TOL_XIRR = 1e-9  # relative; XIRR is solved numerically


def _load():
    fills = json.loads((FIXTURES / "real_fills_sanitized.json").read_text())["fills"]
    closes = json.loads((FIXTURES / "golden_closes.json").read_text())["closes"]
    golden = [r for r in fills if r["ticker"] in ("CHE", "HRL") and r["date"] in ("2026-10-26", "2026-10-27")]
    closes = {t: {d: Decimal(v) for d, v in by_day.items()} for t, by_day in closes.items()}
    return golden, closes


def _sum_shares(rows, ticker):
    return sum(Decimal(r["quantity"]) for r in rows if r["ticker"] == ticker)


# --------------------------------------------------------------------------------------------
# Hand math, written out so a reader can check every number with a calculator.
#
# Golden subset: 4 real fills.
#   2026-10-26  CHE  qty 0.001952  amount 1.00      HRL  qty 0.088305  amount 1.85
#   2026-10-27  CHE  qty 0.002501  amount 1.28      HRL  qty 0.102608  amount 2.13
# Closes (rounded to 4 dp in the fixture):
#   CHE  2026-10-26 512.9000   2026-10-27 509.0900   2026-11-04 548.6400
#   HRL  2026-10-26  20.9500   2026-10-27  20.8700   2026-11-04  22.7600
#
# Day 1, 2026-10-26. Shares after the day: CHE 0.001952, HRL 0.088305.
#   C1  = 1.00 + 1.85                          = 2.85
#   V1  = 0.001952 x 512.90 + 0.088305 x 20.95
#       = 1.0011808 + 1.84998975               = 2.85117055
#   r1  = V1 / (0 + C1) - 1 = 2.85117055 / 2.85 - 1 = 0.00041071929824561403...
#
# Day 2, 2026-10-27. Shares after the day: CHE 0.004453, HRL 0.190913.
#   C2  = 1.28 + 2.13                          = 3.41
#   V2  = 0.004453 x 509.09 + 0.190913 x 20.87
#       = 2.26697777 + 3.98435431              = 6.25133208
#   r2  = V2 / (V1 + C2) - 1 = 6.25133208 / 6.26117055 - 1 = -0.00157134675080844108...
#
# Day 3, 2026-11-04 (valuation only, no contribution).
#   V3  = 0.004453 x 548.64 + 0.190913 x 22.76
#       = 2.44309392 + 4.34517988              = 6.78827380
#   r3  = V3 / V2 - 1 = 6.78827380 / 6.25133208 - 1 = 0.08589236871895629643...
#
# Cumulative TWR = (1 + r1)(1 + r2)(1 + r3) - 1 = 0.08463135140943175358...  (8.4631%)
#
# MWR (XIRR, actual/365), cash flows dated from 2026-10-26:
#   -1.00 on day 0, -1.85 on day 0, -1.28 on day 1, -2.13 on day 1, +6.78827380 on day 9.
#   Solve sum(CF / (1 + x) ** (days / 365)) = 0.  Root: x = 32.01149692363805
#   (annualized, i.e. 3201.15%). Check: the NPV at this root evaluates to 0 (test below).
# --------------------------------------------------------------------------------------------

EXPECTED_R1 = Decimal("0.00041071929824561403508771929824561403")
EXPECTED_R2 = Decimal("-0.00157134675080844108295372979418361316")
EXPECTED_R3 = Decimal("0.08589236871895629643146393208405591533")
EXPECTED_TWR = Decimal("0.08463135140943175357510385885807831339")
EXPECTED_XIRR = 32.01149692363805
TERMINAL_VALUE = Decimal("6.78827380")
VALUATION_DAYS = [date(2026, 10, 26), date(2026, 10, 27), date(2026, 11, 4)]


def test_subperiod_returns_match_hand_math():
    golden, closes = _load()
    returns = dca_goal_progress.subperiod_returns(fills=golden, closes=closes, valuation_days=VALUATION_DAYS)
    assert len(returns) == 3
    assert abs(returns[0] - EXPECTED_R1) < TOL_TWR
    assert abs(returns[1] - EXPECTED_R2) < TOL_TWR
    assert abs(returns[2] - EXPECTED_R3) < TOL_TWR


def test_cumulative_twr_matches_chained_hand_math():
    golden, closes = _load()
    twr = dca_goal_progress.time_weighted_return(fills=golden, closes=closes, valuation_days=VALUATION_DAYS)
    assert abs(Decimal(str(twr)) - EXPECTED_TWR) < TOL_TWR


def test_holdings_after_fills_match_the_fixture_sums():
    # Independent of the implementation: plain sums of the real quantities.
    golden, _ = _load()
    assert _sum_shares(golden, "CHE") == Decimal("0.004453")
    assert _sum_shares(golden, "HRL") == Decimal("0.190913")


def test_mwr_xirr_matches_hand_math():
    golden, _ = _load()
    flows = [(date.fromisoformat(r["date"]), -Decimal(r["amount_usd"])) for r in golden]
    flows.append((date(2026, 11, 4), TERMINAL_VALUE))
    rate = dca_goal_progress.xirr(flows)
    assert rate == pytest.approx(EXPECTED_XIRR, rel=TOL_XIRR)


def test_xirr_root_is_zero_npv_by_direct_evaluation():
    # The expected rate must satisfy the NPV equation when evaluated here, without any solver.
    golden, _ = _load()
    flows = [(date.fromisoformat(r["date"]), -Decimal(r["amount_usd"])) for r in golden]
    flows.append((date(2026, 11, 4), TERMINAL_VALUE))
    origin = date(2026, 10, 26)
    x = Decimal(str(EXPECTED_XIRR))
    npv = sum(cf / (1 + x) ** (Decimal((d - origin).days) / Decimal(365)) for d, cf in flows)
    assert abs(npv) < Decimal("1E-9")


def test_twr_fails_closed_when_a_golden_close_is_missing():
    # A missing close on a valuation day must not silently carry the prior close forward.
    golden, closes = _load()
    del closes["HRL"]["2026-10-27"]
    with pytest.raises(dca_goal_progress.MissingCloseError):
        dca_goal_progress.time_weighted_return(fills=golden, closes=closes, valuation_days=VALUATION_DAYS)


# --------------------------------------------------------------------------------------------
# Fixture hygiene. These run without the new module, so they guard the fixture from day one.
# --------------------------------------------------------------------------------------------

def test_fixture_declares_its_sources_and_sanitization():
    doc = json.loads((FIXTURES / "real_fills_sanitized.json").read_text())
    about = doc["_about"]
    assert about["source_runs"], "source run paths must be recorded"
    assert about["order_count"] == len(doc["fills"])
    assert "9-12 digit" in about["sanitization"]


def test_fixture_has_no_account_number_ids_or_long_digit_runs():
    # The real account number is never written here; the check reads it from the environment
    # when set (it is the only copy that exists) and otherwise relies on the digit-run rule.
    account = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "")
    for name in ("real_fills_sanitized.json", "golden_closes.json"):
        text = (FIXTURES / name).read_text()
        assert not re.search(r"\d{9,12}", text), f"{name}: 9-12 digit literal"
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text), f"{name}: UUID-shaped id"
        assert not (account and account in text), f"{name}: account number"
        assert "ref_id" not in text and '"id"' not in text


def test_fixture_keeps_only_the_allowed_fields():
    doc = json.loads((FIXTURES / "real_fills_sanitized.json").read_text())
    allowed = {"date", "ticker", "side", "quantity", "price", "amount_usd"}
    assert all(set(row) == allowed for row in doc["fills"])
