"""Signal 5, staples vs semis rotation (LOG ONLY): docs/adr-comovement-signals.md. A daily
regime value = mean daily log return of the caller's staples group minus that of the
caller's semis group, plus its trailing rolling mean (20 sessions by default). The groups
are always passed in by the caller; the module hard-codes no ticker.

Toy tickers S1, S2 (the "staples" group) and M1, M2 (the "semis" group); hand-built
returns, not market data.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from comovement_signals import SIGNAL_ROTATION_LOG, RotationConfig, regime_value, rotation_log_shadow

from ._toy import day, rows_from_closes, rows_from_returns

STAPLES = ("S1", "S2")
SEMIS = ("M1", "M2")
TOY = RotationConfig(rolling_sessions=3)

# Log returns on day(1), day(2), day(3):
#   S1 +1%, 0, -1%   S2 +3%, 0, +1%   -> staples mean +2%, 0, 0
#   M1 -1%, 0, +1%   M2 -3%, 0, +1%   -> semis mean   -2%, 0, +1%
# Regime (staples - semis): +4%, 0, -1%. Rolling 3-session mean: (4 + 0 - 1) / 3 = +1%.
RETURNS = {"S1": [0.01, 0.0, -0.01], "S2": [0.03, 0.0, 0.01],
           "M1": [-0.01, 0.0, 0.01], "M2": [-0.03, 0.0, 0.01]}


def _rows():
    return [r for t, rs in RETURNS.items() for r in rows_from_returns(t, rs)]


def test_regime_value_by_hand():
    day_returns = {"S1": 0.01, "S2": 0.03, "M1": -0.01, "M2": -0.03, "X": 0.5}
    assert regime_value(day_returns, STAPLES, SEMIS) == pytest.approx(0.04)
    # A missing member means no value (strict default coverage), never a partial mean.
    assert regime_value({"S1": 0.01, "S2": 0.03, "M1": -0.01}, STAPLES, SEMIS) is None


def test_rotation_record_by_hand():
    result = rotation_log_shadow(_rows(), as_of=day(3), staples=STAPLES, semis=SEMIS, config=TOY)
    assert result.ok and result.signal == SIGNAL_ROTATION_LOG
    (record,) = result.records
    p = record.payload
    assert p["session"] == day(3).isoformat()
    assert p["staples_mean"] == pytest.approx(0.0, abs=1e-12)
    assert p["semis_mean"] == pytest.approx(0.01)
    assert p["regime"] == pytest.approx(-0.01)
    assert p["rolling_mean"] == pytest.approx(0.01)
    assert p["rolling_sessions"] == 3
    assert p["staples"] == ["S1", "S2"] and p["semis"] == ["M1", "M2"]
    assert p["return_kind"] == "log"
    assert p["live_effect"] == "none"
    assert record.inputs_known_at == day(3)


def test_no_lookahead_rows_after_as_of_change_nothing():
    base = rotation_log_shadow(_rows(), as_of=day(3), staples=STAPLES, semis=SEMIS, config=TOY)
    future = rows_from_closes("S1", [1.0, 999.0], start=4) + rows_from_closes("M1", [999.0, 1.0], start=4)
    later = rotation_log_shadow(_rows() + future, as_of=day(3), staples=STAPLES, semis=SEMIS, config=TOY)
    assert later.records == base.records


def test_insufficient_history_gives_no_record():
    result = rotation_log_shadow(_rows(), as_of=day(3), staples=STAPLES, semis=SEMIS,
                                 config=RotationConfig(rolling_sessions=4))
    assert result.records == () and result.reason == "insufficient_history"


def test_missing_member_on_the_day_gives_no_record():
    rows = [r for r in _rows() if not (r["ticker"] == "M2" and r["date"] == day(3))]
    result = rotation_log_shadow(rows, as_of=day(3), staples=STAPLES, semis=SEMIS, config=TOY)
    assert result.records == () and result.reason == "incomplete_group"


def test_unknown_member_gives_no_record():
    result = rotation_log_shadow(_rows(), as_of=day(3), staples=("S1", "S2", "S9"), semis=SEMIS, config=TOY)
    assert result.records == () and result.reason == "incomplete_group"


@pytest.mark.parametrize("staples,semis", [(("S1", "M1"), SEMIS), ((), SEMIS), (STAPLES, ())])
def test_overlapping_or_empty_groups_are_refused(staples, semis):
    result = rotation_log_shadow(_rows(), as_of=day(3), staples=staples, semis=semis, config=TOY)
    assert not result.ok and result.reason == "invalid_groups" and result.records == ()


def test_stale_prices_fail_closed():
    result = rotation_log_shadow(_rows(), as_of=day(13), staples=STAPLES, semis=SEMIS, config=TOY)
    assert not result.ok and result.reason == "stale_prices" and result.records == ()


def test_default_rolling_window_is_20():
    assert RotationConfig().rolling_sessions == 20


def test_module_hard_codes_no_ticker():
    # The research's staples (KR, CL, MDLZ, CHD, HSY, MCK) and semis names must arrive from
    # the caller; none may appear as a string literal in the module.
    src = (Path(__file__).resolve().parents[2] / "comovement_signals" / "rotation_log.py").read_text()
    literals = {n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for ticker in ("KR", "CL", "MDLZ", "CHD", "HSY", "MCK", "NVDA", "TSM", "AMD", "MU", "AVGO", "AMAT", "ASML"):
        assert ticker not in literals
