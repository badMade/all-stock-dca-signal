"""Signal 3, pair breakdown warning (shadow only): docs/adr-comovement-signals.md, open
question 6's proposed default -- a pair whose 2-year daily-return correlation is at least
0.70 is flagged when its rolling 60-session correlation sits below that pair's own 2-year
10th percentile for 5 consecutive sessions. The record suggests both names hold at 1x and
has no live effect.

Every number below is a hand-built toy (see _toy.py); none is market data. The windows
are shrunk (rolling 4, consecutive 2) where noted so the expected values can be worked
out by hand; the trigger logic itself is tested at the production defaults (5 sessions,
10th percentile) through the pure ``breakdown_run`` function.
"""

from __future__ import annotations

import math

import pytest

from comovement_signals import (
    SIGNAL_PAIR_BREAKDOWN,
    PairBreakdownConfig,
    breakdown_run,
    pair_breakdown_shadow,
    percentile,
    rolling_correlations,
)

from ._toy import day, rows_from_closes, rows_from_returns

# --- percentile: linear interpolation between order statistics (numpy's default) ------


def test_percentile_linear_interpolation():
    # Position = q * (n - 1). [0, 1] at q=0.1: position 0.1, so 0 + 0.1 * (1 - 0) = 0.1.
    assert percentile([1.0, 0.0], 0.10) == pytest.approx(0.1)
    # 11 values 1..11 (given unsorted): position 0.1 * 10 = 1.0 exactly -> the 2nd smallest.
    assert percentile([3, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11], 0.10) == 2
    # q=0 and q=1 are the min and max.
    assert percentile([5.0, 2.0, 9.0], 0.0) == 2.0
    assert percentile([5.0, 2.0, 9.0], 1.0) == 9.0


@pytest.mark.parametrize("values,q", [([], 0.1), ([1.0], 1.5), ([1.0], -0.1), ([float("nan")], 0.1)])
def test_percentile_rejects_bad_input(values, q):
    with pytest.raises(ValueError):
        percentile(values, q)


# --- breakdown_run at the production defaults (5 sessions, 10th percentile) -----------

# Reference history of 11 rolling correlations, all exact binary fractions so there is no
# float round-off: sorted it is 0.25, 0.5, then nine 0.875s. Position 0.1 * 10 = 1.0, so
# the 10th percentile is exactly the 2nd smallest value, 0.5.
REFERENCE = [0.875] * 4 + [0.25] + [0.875] * 3 + [0.5] + [0.875] * 2


def test_exactly_five_sessions_below_triggers():
    check = breakdown_run(REFERENCE + [0.375] * 5)
    assert check.threshold == 0.5
    assert check.run_length == 5
    assert check.flagged
    assert check.reference_count == 11  # the 5 sessions under test do not vote on their threshold


def test_four_sessions_below_does_not_trigger():
    # The oldest of the last five is back above the threshold: a run of 4, one short.
    check = breakdown_run(REFERENCE + [0.875] + [0.375] * 4)
    assert check.run_length == 4
    assert not check.flagged


def test_exactly_at_the_10th_percentile_is_not_below():
    # "Below" is strict: a correlation equal to the 10th percentile does not count, so a
    # last session sitting exactly on 0.5 ends the run at length 0.
    check = breakdown_run(REFERENCE + [0.375] * 4 + [0.5])
    assert check.threshold == 0.5
    assert check.run_length == 0
    assert not check.flagged
    assert not breakdown_run(REFERENCE + [0.5] * 5).flagged


def test_too_little_reference_history_gives_no_check():
    assert breakdown_run([0.9, 0.9] + [0.1] * 5, min_reference=3) is None
    assert breakdown_run([0.1] * 5) is None  # nothing left for a reference at all


# --- rolling_correlations ---------------------------------------------------------------


def test_rolling_correlation_uses_only_its_own_window():
    # A = +1, -1, +1, -1 ; B = +1, -1, +1, +1 (in 1% units) over day(1)..day(4).
    # Window ending day(3) (rolling 3): A (1,-1,1), B (1,-1,1), identical -> 1.
    # Window ending day(4): A (-1,1,-1), B (-1,1,1). Means -1/3 and 1/3.
    #   dA = (-2/3, 4/3, -2/3), dB = (-4/3, 2/3, 2/3); sum dA*dB = 8/9 + 8/9 - 4/9 = 4/3;
    #   sAA = 4/9 + 16/9 + 4/9 = 8/3 = sBB; r = (4/3) / (8/3) = 0.5.
    ra = {day(1): 1.0, day(2): -1.0, day(3): 1.0, day(4): -1.0}
    rb = {day(1): 1.0, day(2): -1.0, day(3): 1.0, day(4): 1.0}
    out = rolling_correlations(ra, rb, window=3)
    assert [d for d, _ in out] == [day(3), day(4)]
    assert out[0][1] == pytest.approx(1.0)
    assert out[1][1] == pytest.approx(0.5)


# --- the full shadow evaluation on a toy pair ------------------------------------------

# Toy pair, rolling window 4 and run length 2 so it can be worked by hand. 43 closes on
# day(0)..day(42) give 42 returns on day(1)..day(42). A1 alternates +1%, -1% (day 1 is
# +1%). B1 copies A1 exactly except where noted.
# The last toy close is day(42). as_of is the next day, so every close is final by date
# (a bar dated ON as_of counts only when the caller marks it final: PR #2106 review).
AS_OF = day(43)
TOY = PairBreakdownConfig(rolling_sessions=4, consecutive_sessions=2, min_history_sessions=40,
                          min_reference_values=10)


def _alternating(n: int) -> list[float]:
    return [0.01 if i % 2 == 0 else -0.01 for i in range(n)]


def _pair_rows(flip_last: int) -> list[dict]:
    """B1 = A1 with the last ``flip_last`` returns negated."""
    a = _alternating(42)
    b = a[: 42 - flip_last] + [-x for x in a[42 - flip_last:]]
    return rows_from_returns("A1", a) + rows_from_returns("B1", b)


def test_breakdown_flagged_and_suggests_hold_1x():
    # B1 negates A1 on day(41) and day(42).
    # 2-year correlation: both series have mean 0 (21 ups, 21 downs each); 40 sessions
    # agree and 2 disagree, so r = (40 - 2) / 42 = 19/21 = 0.905, which is >= 0.70.
    # Every rolling window ending day(4)..day(40) is two identical series: r = 1. Those 37
    # values are the reference, so the threshold is 1 (to round-off).
    # Window ending day(41) = days 38..41: A (-1,1,-1,1), B (-1,1,-1,-1); B mean -1/2,
    #   dB = (-1/2, 3/2, -1/2, -1/2); sum dA*dB = 2; sAA = 4, sBB = 3; r = 2/sqrt(12) = 1/sqrt(3).
    # Window ending day(42) = days 39..42: A (1,-1,1,-1), B (1,-1,-1,1); both mean 0; sum
    #   dA*dB = 1 + 1 - 1 - 1 = 0; r = 0.
    # Both of the last 2 sessions sit below the threshold, so the pair is flagged.
    result = pair_breakdown_shadow(_pair_rows(flip_last=2), as_of=AS_OF, config=TOY)
    assert result.ok and result.signal == SIGNAL_PAIR_BREAKDOWN
    assert result.skipped == {}
    (record,) = result.records
    p = record.payload
    assert p["pair"] == ["A1", "B1"]
    assert p["correlation_2y"] == pytest.approx(19 / 21)
    assert p["threshold"] == pytest.approx(1.0)
    assert p["rolling_correlation_recent"] == pytest.approx([1 / math.sqrt(3), 0.0], abs=1e-12)
    assert p["run_length"] == 2 and p["breakdown"] is True
    assert p["suggestion"] == {"A1": "hold_1x", "B1": "hold_1x"}
    assert p["live_effect"] == "none"
    assert record.as_of == AS_OF and record.inputs_known_at == day(42)


def test_one_session_below_is_recorded_but_not_flagged():
    # Only day(42) is negated: window ending day(41) is all-agreeing (r = 1, not below the
    # threshold of 1), window ending day(42) has r = 1/sqrt(3). A run of 1 < 2.
    result = pair_breakdown_shadow(_pair_rows(flip_last=1), as_of=AS_OF, config=TOY)
    (record,) = result.records
    assert record.payload["run_length"] == 1
    assert record.payload["breakdown"] is False
    assert record.payload["suggestion"] is None


def test_pairs_below_min_correlation_are_not_evaluated():
    # C1's returns are A1's negated (+1 where A1 is -1): r = -1 with A1 and -19/21 with
    # B1, both below 0.70, so only A1/B1 is evaluated.
    rows = _pair_rows(flip_last=2) + rows_from_returns("C1", [-x for x in _alternating(42)])
    result = pair_breakdown_shadow(rows, as_of=AS_OF, config=TOY)
    assert [r.payload["pair"] for r in result.records] == [["A1", "B1"]]


def test_no_lookahead_rows_after_as_of_change_nothing():
    base = pair_breakdown_shadow(_pair_rows(flip_last=2), as_of=AS_OF, config=TOY)
    # Wild closes after as_of, for both legs and for a ticker that only exists later.
    future = (rows_from_closes("A1", [500.0, 1.0, 900.0], start=44)
              + rows_from_closes("B1", [1.0, 700.0, 2.0], start=44)
              + rows_from_closes("Z9", [10.0, 20.0], start=44))
    later = pair_breakdown_shadow(_pair_rows(flip_last=2) + future, as_of=AS_OF, config=TOY)
    assert later.records == base.records
    assert later.skipped == base.skipped


def test_as_of_mid_history_uses_only_data_up_to_it():
    # At as_of = day(41), day(40) is the newest final close: the flipped days 41 and 42
    # have not happened (the unmarked day(41) bar is still forming and ignored). The last
    # two windows are all-agreeing (r = 1), so nothing is below and nothing is flagged.
    result = pair_breakdown_shadow(_pair_rows(flip_last=2), as_of=day(41),
                                   config=PairBreakdownConfig(rolling_sessions=4, consecutive_sessions=2,
                                                              min_history_sessions=38, min_reference_values=10))
    (record,) = result.records
    assert record.payload["breakdown"] is False
    assert record.inputs_known_at == day(40)


def test_insufficient_history_gives_no_record_and_a_reason():
    cfg = PairBreakdownConfig(rolling_sessions=4, consecutive_sessions=2, min_history_sessions=40,
                              min_reference_values=100)  # only 37 reference values exist
    result = pair_breakdown_shadow(_pair_rows(flip_last=2), as_of=AS_OF, config=cfg)
    assert result.ok and result.records == ()
    assert result.skipped == {"A1|B1": "insufficient_history"}


def test_stale_prices_fail_closed():
    # Newest close day(42), as_of 10 days later (day 52): beyond the 4-day staleness limit.
    result = pair_breakdown_shadow(_pair_rows(flip_last=2), as_of=day(52), config=TOY)
    assert not result.ok and result.reason == "stale_prices" and result.records == ()


def test_pair_with_a_stale_leg_is_skipped():
    # B1 is missing its last close, so the pair's newest shared return is day(41), not
    # the newest session day(42). No record for a pair that cannot be read as of today.
    rows = [r for r in _pair_rows(flip_last=2) if not (r["ticker"] == "B1" and r["date"] == day(42))]
    cfg = PairBreakdownConfig(rolling_sessions=4, consecutive_sessions=2, min_history_sessions=40,
                              min_reference_values=10)
    result = pair_breakdown_shadow(rows, as_of=AS_OF, config=cfg)
    assert result.records == ()
    assert result.skipped == {"A1|B1": "stale_pair"}


def test_malformed_rows_fail_closed():
    rows = _pair_rows(flip_last=2) + [{"ticker": "A1", "date": day(10), "close": 1.0}]  # duplicate
    result = pair_breakdown_shadow(rows, as_of=AS_OF, config=TOY)
    assert not result.ok and result.reason == "invalid_input" and result.records == ()


def test_production_defaults_match_the_adr():
    cfg = PairBreakdownConfig()
    assert (cfg.lookback_sessions, cfg.min_pair_correlation, cfg.rolling_sessions,
            cfg.percentile, cfg.consecutive_sessions) == (504, 0.70, 60, 0.10, 5)
