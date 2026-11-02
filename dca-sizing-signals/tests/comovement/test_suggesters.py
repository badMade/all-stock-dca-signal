"""SizingSuggester-conforming wrappers for signals 1 and 2, including every fail-closed path.

Fail closed (ADR rule 3): a missing or stale input, or a failed check, yields an outcome
with ok=False and an explicit reason, carries no suggestions, and suggest() raises
ComovementUnavailable instead of inventing a neutral number.
"""

from __future__ import annotations

import pytest

from comovement_signals import (
    ClusterCapSuggester,
    ComovementUnavailable,
    CorrelatedSelloffSuggester,
)
from dca_shared_interfaces.forecasting import SizingSuggester

from ._inputs import AS_OF, FIXED_NOW, TOY_CONFIG, clock, toy_inputs
from ._toy import THREE_BLOCKS, day


def _cap(inputs=None, **kw):
    return ClusterCapSuggester(lambda asof: inputs or toy_inputs(), config=TOY_CONFIG, clock=clock, **kw)


def _selloff(inputs=None, **kw):
    return CorrelatedSelloffSuggester(lambda asof: inputs or toy_inputs(), config=TOY_CONFIG, clock=clock, **kw)


def test_both_conform_to_sizing_suggester_protocol():
    assert isinstance(_cap(), SizingSuggester)
    assert isinstance(_selloff(), SizingSuggester)


def test_cluster_cap_outcome():
    out = _cap().evaluate(AS_OF)
    assert out.ok, out.reason
    assert out.reason == "ok"
    assert out.signal == "cluster_cap"
    assert out.clusters == THREE_BLOCKS
    by = {s.ticker: s for s in out.suggestions}
    # One suggestion per proposed buy. Block A: multiplier 0.7 -> adjustment -30%.
    assert sorted(by) == ["A1", "A2", "A3", "B1", "C1"]
    for t in ("A1", "A2", "A3"):
        assert by[t].multiplier_adjustment_pct == pytest.approx(-30.0)
    assert by["B1"].multiplier_adjustment_pct == 0.0
    assert by["C1"].multiplier_adjustment_pct == 0.0
    assert all(s.asof == AS_OF and s.source == "comovement_signals.cluster_cap" for s in out.suggestions)
    assert all(s.generated_at == FIXED_NOW for s in out.suggestions)


def test_selloff_outcome():
    out = _selloff().evaluate(AS_OF)
    assert out.ok, out.reason
    by = {s.ticker: s.multiplier_adjustment_pct for s in out.suggestions}
    # See _inputs.toy_inputs for the hand arithmetic: 0.875, 2/3, 0.5.
    assert by["A1"] == pytest.approx(-12.5)
    assert by["A2"] == pytest.approx(-100.0 / 3.0)
    assert by["A3"] == pytest.approx(-50.0)
    assert by["B1"] == 0.0 and by["C1"] == 0.0
    assert out.detail["universe_flag"] is False  # 3 of 9 down = 33%
    assert out.detail["marked_clusters"][0]["members"] == ["A1", "A2", "A3"]


def test_suggest_returns_one_ticker():
    s = _cap().suggest("A2", AS_OF)
    assert s.ticker == "A2"
    assert s.multiplier_adjustment_pct == pytest.approx(-30.0)


def test_suggest_for_ticker_without_proposed_buy_raises():
    with pytest.raises(ComovementUnavailable) as exc:
        _cap().suggest("B2", AS_OF)
    assert exc.value.reason == "ticker_not_proposed"


def _assert_fail_closed(suggester, reason):
    out = suggester.evaluate(AS_OF)
    assert out.ok is False
    assert out.reason == reason
    assert out.suggestions == ()
    with pytest.raises(ComovementUnavailable) as exc:
        suggester.suggest("A1", AS_OF)
    assert exc.value.reason == reason


def test_loader_failure_fails_closed():
    def boom(asof):
        raise OSError("disk gone")

    _assert_fail_closed(ClusterCapSuggester(boom, config=TOY_CONFIG, clock=clock), "input_load_failed")


def test_no_price_rows_fails_closed():
    _assert_fail_closed(_cap(toy_inputs(close_rows=[])), "missing_prices")


def test_stale_prices_fail_closed():
    # Latest close is day(8); as_of day(13) is 5 calendar days later, beyond the 4-day limit.
    later = toy_inputs(day_returns_session=day(13))
    out = ClusterCapSuggester(lambda a: later, config=TOY_CONFIG, clock=clock).evaluate(day(13))
    assert (out.ok, out.reason) == (False, "stale_prices")


def test_four_day_old_prices_still_fresh():
    later = toy_inputs(day_returns_session=day(12))
    assert ClusterCapSuggester(lambda a: later, config=TOY_CONFIG, clock=clock).evaluate(day(12)).ok


def test_proposed_ticker_without_history_fails_closed():
    inputs = toy_inputs(proposed_buys_usd_by_ticker={"A1": 40.0, "ZZ": 10.0})
    _assert_fail_closed(_cap(inputs), "missing_ticker_history")


def test_bad_price_row_fails_closed():
    rows = toy_inputs().close_rows + [{"ticker": "A1", "date": day(3), "close": 1.0}]  # duplicate date
    _assert_fail_closed(_cap(toy_inputs(close_rows=rows)), "invalid_input")


def test_no_proposed_buys_fails_closed():
    _assert_fail_closed(_cap(toy_inputs(proposed_buys_usd_by_ticker={})), "missing_proposed_buys")


@pytest.mark.parametrize("budget", [0.0, -5.0, float("nan")])
def test_bad_budget_fails_closed(budget):
    _assert_fail_closed(_cap(toy_inputs(day_budget_usd=budget)), "invalid_input")


def test_too_few_clusterable_tickers_fails_closed():
    # With a 9-return minimum every toy ticker (8 returns) is a singleton: clustering has
    # nothing to work with, which is a failed check, not "no cap needed".
    from comovement_signals import ComovementConfig

    cfg = ComovementConfig(min_sessions=9, min_overlap_sessions=9)
    out = ClusterCapSuggester(lambda a: toy_inputs(), config=cfg, clock=clock).evaluate(AS_OF)
    assert (out.ok, out.reason) == (False, "failed_check")


def test_selloff_stale_day_returns_fail_closed():
    _assert_fail_closed(_selloff(toy_inputs(day_returns_session=day(8))), "stale_day_returns")


def test_selloff_missing_day_returns_fail_closed():
    _assert_fail_closed(_selloff(toy_inputs(day_returns_by_ticker={})), "missing_day_returns")


def test_selloff_escalation_above_proposed_fails_closed():
    inputs = toy_inputs(escalation_usd_by_ticker={"A1": 50.0})  # A1 proposes only $40
    _assert_fail_closed(_selloff(inputs), "invalid_input")


def test_cluster_cap_does_not_need_day_returns():
    # Signal 1 reads only prices, buys and budget; stale day returns must not block it.
    assert _cap(toy_inputs(day_returns_by_ticker={}, day_returns_session=day(1))).evaluate(AS_OF).ok


def test_lookahead_row_changes_no_suggestion():
    # A row dated after as_of (a crash in A1) must not change a single suggestion.
    base = _cap().evaluate(AS_OF)
    # day(9) is ON as_of and not marked final (a forming bar); day(10) is after it.
    rows = toy_inputs().close_rows + [{"ticker": "A1", "date": day(9), "close": 1.0},
                                      {"ticker": "A2", "date": day(10), "close": 1.0, "final": True}]
    after = _cap(toy_inputs(close_rows=rows)).evaluate(AS_OF)
    assert after.ok and after.suggestions == base.suggestions and after.clusters == base.clusters


# ---------------------------------------------------------------- review round 1 (PR #2106)


def test_same_day_new_inputs_are_not_served_from_cache():
    # Item 1: a later call on the same as-of date with different proposed buys must be
    # recomputed. First call: block A spends $100 > $70 cap (0.7). Second: block A spends
    # 30 + 20 + 20 = $70, exactly the cap, so no scale-down.
    state = {"inputs": toy_inputs()}
    s = ClusterCapSuggester(lambda a: state["inputs"], config=TOY_CONFIG, clock=clock)
    assert s.suggest("A1", AS_OF).multiplier_adjustment_pct == pytest.approx(-30.0)
    state["inputs"] = toy_inputs(proposed_buys_usd_by_ticker={"A1": 30.0, "A2": 20.0, "A3": 20.0})
    assert s.suggest("A1", AS_OF).multiplier_adjustment_pct == 0.0


def test_failure_is_never_cached():
    # Item 1: a transient loader failure must not stick for the rest of the day.
    calls = {"n": 0}

    def flaky(asof):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return toy_inputs()

    s = ClusterCapSuggester(flaky, config=TOY_CONFIG, clock=clock)
    assert s.evaluate(AS_OF).reason == "input_load_failed"
    assert s.evaluate(AS_OF).ok


def test_unscored_sentinel_can_never_win_an_mae_gate():
    # Item 2: no in-memory "perfect" MAE. The metric is +inf (worse than any real MAE) and
    # the suggestion says scored=False; the shared base publisher's own gate refuses it.
    import math

    from comovement_signals import ComovementSuggestion
    from dca_shared_interfaces.publishing import SuggestionPublisher

    s = _cap().suggest("A1", AS_OF)
    assert isinstance(s, ComovementSuggestion)
    assert s.scored is False
    assert math.isinf(s.eval_metric_mae) and s.eval_metric_mae > 0
    assert sorted([s.eval_metric_mae, 0.5, 3.0])[0] == 0.5  # any real MAE ranks ahead

    class _MaeGated(SuggestionPublisher):
        def validate_bounds(self, suggestion):
            return True

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        res = _MaeGated(Path(d) / "x.json").publish(s)
        assert (res.published, res.status) == (False, "blocked_missing_eval_metric")
        assert not (Path(d) / "x.json").exists()


@pytest.mark.parametrize("bad", [None, {"close_rows": []}, "inputs", 42])
def test_loader_returning_wrong_type_fails_closed(bad):
    # Item 4: never an AttributeError; a fail-closed outcome instead.
    _assert_fail_closed(ClusterCapSuggester(lambda a: bad, config=TOY_CONFIG, clock=clock), "invalid_input")


@pytest.mark.parametrize("field,value", [
    ("proposed_buys_usd_by_ticker", [("A1", 40.0)]),
    ("close_rows", "not rows"),
    ("day_returns_by_ticker", [0.01]),
    ("escalation_usd_by_ticker", 5),
])
def test_wrongly_typed_input_field_fails_closed(field, value):
    _assert_fail_closed(_selloff(toy_inputs(**{field: value})), "invalid_input")
