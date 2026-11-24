"""ResidualReversalSuggester: direction, budget neutrality, point in time, every guard and
every fail-closed path."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from dca_shared_interfaces.forecasting import SizingSuggester
from residual_reversal import (
    UNSCORED_MAE,
    CorporateEvent,
    ResidualReversalConfig,
    ResidualReversalInputs,
    ResidualReversalSuggester,
    ResidualReversalUnavailable,
)

from . import _synthetic as S

BUYS = {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0, "DDD": 10.0}


def _setup(shocks=None, buys=None, **kw):
    rows, dates = S.universe(shocks=shocks, **kw)
    buys = buys or {t: 10.0 for t in (shocks or BUYS)}
    inputs = ResidualReversalInputs(rows, buys, {t: "XLK" for t in buys})
    return inputs, dates[-1] + timedelta(days=1), dates


def _run(inputs, asof, config=None):
    return ResidualReversalSuggester(lambda a: inputs, config=config, clock=S.clock).evaluate(asof)


def test_conforms_to_sizing_suggester_protocol():
    inputs, _, _ = _setup()
    assert isinstance(ResidualReversalSuggester(lambda a: inputs), SizingSuggester)


def test_cheap_leans_up_rich_leans_down_and_dollars_are_unchanged():
    inputs, asof, _ = _setup()
    out = _run(inputs, asof)
    assert out.ok, out.detail
    m = {s.ticker: 1.0 + s.multiplier_adjustment_pct / 100.0 for s in out.suggestions}
    assert out.detail["tickers"]["AAA"]["s_score"] < -1.25
    assert out.detail["tickers"]["BBB"]["s_score"] > 1.25
    assert m["AAA"] > 1.0 > m["BBB"]
    assert math.fsum(BUYS[t] * v for t, v in m.items()) == pytest.approx(sum(BUYS.values()), rel=1e-9)
    assert all(0.75 <= v <= 1.25 for v in m.values())
    for s in out.suggestions:
        assert s.eval_metric_mae == UNSCORED_MAE and s.scored is False
        assert s.generated_at == S.FIXED_NOW and s.asof == asof
        assert s.proposed_usd == 10.0


def test_all_quiet_day_is_exactly_neutral():
    inputs, asof, _ = _setup(shocks={"CCC": 0.0, "EEE": 0.0})
    out = _run(inputs, asof, ResidualReversalConfig(entry=50.0))
    assert out.ok
    assert all(s.multiplier_adjustment_pct == 0.0 for s in out.suggestions)


def test_future_rows_and_a_forming_asof_bar_are_never_read():
    inputs, asof, _ = _setup()
    base = _run(inputs, asof)
    junk = [{"ticker": t, "date": (asof + timedelta(days=k)).isoformat(), "close": 1.0 + k}
            for t in ("XLK", *BUYS) for k in range(0, 5)]
    out = _run(replace(inputs, close_rows=list(inputs.close_rows) + junk), asof)
    assert [s.multiplier_adjustment_pct for s in out.suggestions] == \
        [s.multiplier_adjustment_pct for s in base.suggestions]


def test_a_final_bar_on_asof_is_used():
    inputs, asof, dates = _setup()
    last = dates[-1]
    rows = [dict(r, final=True) for r in inputs.close_rows]
    out = _run(replace(inputs, close_rows=rows), last)  # as_of == last close date, marked final
    assert out.ok and out.suggestions
    rows_unfinal = list(inputs.close_rows)  # same day but not final: that session is dropped
    out2 = _run(replace(inputs, close_rows=rows_unfinal), last)
    assert out2.detail["tickers"] != out.detail["tickers"]


def test_suggest_returns_one_ticker_and_raises_for_others():
    inputs, asof, _ = _setup()
    sug = ResidualReversalSuggester(lambda a: inputs, clock=S.clock)
    assert sug.suggest("AAA", asof).ticker == "AAA"
    with pytest.raises(ResidualReversalUnavailable) as exc:
        sug.suggest("ZZZ", asof)
    assert exc.value.reason == "ticker_not_proposed"


def test_zero_buy_is_skipped_not_guarded():
    inputs, asof, _ = _setup(buys={"AAA": 10.0, "BBB": 0.0, "CCC": 10.0, "DDD": 10.0})
    out = _run(inputs, asof)
    assert out.ok and "BBB" not in {s.ticker for s in out.suggestions} and "BBB" not in out.guarded


# ---------------------------------------------------------------- relationship-break guard


def _guard(out, ticker):
    assert out.ok, out.detail
    assert ticker not in {s.ticker for s in out.suggestions}
    return out.guarded[ticker]


def test_guard_no_benchmark():
    inputs, asof, _ = _setup()
    out = _run(replace(inputs, benchmark_by_ticker={"BBB": "XLK", "CCC": "XLK", "DDD": "XLK", "AAA": "XLF"}), asof)
    assert _guard(out, "AAA") == "no_benchmark"


def test_guard_insufficient_history():
    inputs, asof, _ = _setup(n_returns=100)  # need 90 + 20
    out = _run(inputs, asof)
    assert set(out.guarded.values()) == {"insufficient_history"} and not out.suggestions


def test_guard_stale_history():
    inputs, asof, _ = _setup()
    out = _run(inputs, asof + timedelta(days=10))
    assert set(out.guarded.values()) == {"stale_history"}


def test_guard_ticker_missing_the_newest_session():
    inputs, asof, dates = _setup()
    rows = [r for r in inputs.close_rows if not (r["ticker"] == "AAA" and r["date"] == dates[-1].isoformat())]
    out = _run(replace(inputs, close_rows=rows), asof)
    assert _guard(out, "AAA") == "stale_history"


def test_guard_low_r_squared():
    inputs, asof, _ = _setup(beta=0.0)  # the ETF explains nothing
    out = _run(inputs, asof)
    assert set(out.guarded.values()) == {"low_r_squared"}


def test_guard_not_mean_reverting():
    # The fixture's fitted b values sit near 0.66-0.78; a tradability bound below all of
    # them must guard every ticker on today's fit.
    inputs, asof, _ = _setup()
    out = _run(inputs, asof, ResidualReversalConfig(b_max=0.5))
    assert set(out.guarded.values()) == {"not_mean_reverting"} and not out.suggestions


def test_guard_half_life_jump():
    inputs, asof, _ = _setup()
    base = _run(inputs, asof)
    jumped = [t for t, d in base.detail["tickers"].items()
              if d["half_life_sessions"] > 1.01 * d["baseline_half_life_sessions"]]
    assert jumped, "fixture should have at least one ticker above its own median"
    out = _run(inputs, asof, ResidualReversalConfig(half_life_jump=1.01))
    for t in jumped:
        assert _guard(out, t) == "half_life_jump"


def test_guard_no_half_life_baseline():
    # Pick a bound between today's b and the past fits' b for one ticker: today is
    # tradable, but too few past days were, so there is no median to compare with.
    inputs, asof, _ = _setup()
    base = _run(inputs, asof)
    t, d = min(base.detail["tickers"].items(), key=lambda kv: kv[1]["ou_b"])
    cfg = ResidualReversalConfig(b_max=d["ou_b"] + 1e-6, min_half_life_baseline=20)
    out = _run(inputs, asof, cfg)
    assert out.guarded[t] == "no_half_life_baseline"


@pytest.mark.parametrize("kind", ["8k_1.01", "8k_2.01", "8k_5.01", "material"])
def test_guard_material_8k_on_or_before_filed_date(kind):
    inputs, asof, _ = _setup()
    ev = CorporateEvent(kind, event_date=asof, known_at=asof)
    out = _run(replace(inputs, events_by_ticker={"AAA": [ev]}), asof)
    assert _guard(out, "AAA") == "corporate_event"


def test_an_8k_filed_after_asof_never_reaches_back():
    """Acquisition replay: the guard must not use a filing before its filed date."""
    inputs, asof, _ = _setup()
    later = CorporateEvent("8k_2.01", event_date=asof + timedelta(days=1), known_at=asof + timedelta(days=1))
    out = _run(replace(inputs, events_by_ticker={"AAA": [later]}), asof)
    assert "AAA" not in out.guarded
    out_next = _run(replace(inputs, events_by_ticker={"AAA": [later]}), asof + timedelta(days=1))
    assert out_next.guarded.get("AAA") == "corporate_event"


def test_an_old_8k_ages_out():
    inputs, asof, _ = _setup()
    old = CorporateEvent("8k_1.01", event_date=asof - timedelta(days=91), known_at=asof - timedelta(days=91))
    assert "AAA" not in _run(replace(inputs, events_by_ticker={"AAA": [old]}), asof).guarded


def test_guard_earnings_window_both_sides():
    inputs, asof, _ = _setup()
    for offset in (-3, 0, 3):
        ev = CorporateEvent("earnings", event_date=asof + timedelta(days=offset), known_at=asof - timedelta(days=30))
        assert _guard(_run(replace(inputs, events_by_ticker={"AAA": [ev]}), asof), "AAA") == "corporate_event"
    far = CorporateEvent("earnings", event_date=asof + timedelta(days=4), known_at=asof - timedelta(days=30))
    assert "AAA" not in _run(replace(inputs, events_by_ticker={"AAA": [far]}), asof).guarded
    unknown = CorporateEvent("earnings", event_date=asof, known_at=asof + timedelta(days=1))
    assert "AAA" not in _run(replace(inputs, events_by_ticker={"AAA": [unknown]}), asof).guarded


def test_guarded_ticker_drops_out_of_the_neutrality_set():
    inputs, asof, _ = _setup()
    ev = CorporateEvent("8k_1.01", event_date=asof, known_at=asof)
    out = _run(replace(inputs, events_by_ticker={"BBB": [ev]}), asof)
    active = {s.ticker: s for s in out.suggestions}
    assert "BBB" not in active
    total = math.fsum(s.proposed_usd * (1 + s.multiplier_adjustment_pct / 100) for s in active.values())
    assert total == pytest.approx(30.0, rel=1e-9)
    with pytest.raises(ResidualReversalUnavailable) as exc:
        ResidualReversalSuggester(lambda a: replace(inputs, events_by_ticker={"BBB": [ev]})).suggest("BBB", asof)
    assert exc.value.reason == "guarded:corporate_event"


# ---------------------------------------------------------------- fail closed


def _failed(out, reason):
    assert not out.ok and out.reason == reason and not out.suggestions, out


def test_loader_exception_fails_closed():
    def boom(asof):
        raise OSError("disk gone")
    out = ResidualReversalSuggester(boom).evaluate(S.START)
    _failed(out, "input_load_failed")
    with pytest.raises(ResidualReversalUnavailable):
        ResidualReversalSuggester(boom).suggest("AAA", S.START)


def test_wrong_loader_type_fails_closed():
    _failed(ResidualReversalSuggester(lambda a: {"close_rows": []}).evaluate(S.START), "invalid_input")


@pytest.mark.parametrize("bad_row", [
    {"ticker": "AAA", "date": "2025-01-06"},
    {"ticker": "AAA", "date": "nope", "close": 1.0},
    {"ticker": "AAA", "date": "2025-01-06", "close": -1.0},
    {"ticker": "AAA", "date": "2025-01-06", "close": True},
    {"ticker": "AAA", "date": "2025-01-06", "close": 1.0, "final": "yes"},
    {"ticker": "", "date": "2025-01-06", "close": 1.0},
])
def test_malformed_row_fails_closed(bad_row):
    inputs, asof, _ = _setup()
    _failed(_run(replace(inputs, close_rows=list(inputs.close_rows) + [bad_row]), asof), "invalid_input")


def test_malformed_future_row_still_fails_closed():
    inputs, asof, _ = _setup()
    bad = {"ticker": "AAA", "date": (asof + timedelta(days=30)).isoformat(), "close": math.nan}
    _failed(_run(replace(inputs, close_rows=list(inputs.close_rows) + [bad]), asof), "invalid_input")


def test_interpolated_rows_are_ignored_even_with_junk_close():
    inputs, asof, _ = _setup()
    fill = {"ticker": "AAA", "date": "2024-12-30", "close": None, "interpolated": True}
    assert _run(replace(inputs, close_rows=[fill] + list(inputs.close_rows)), asof).ok


def test_duplicate_row_fails_closed():
    inputs, asof, _ = _setup()
    _failed(_run(replace(inputs, close_rows=list(inputs.close_rows) + [inputs.close_rows[0]]), asof),
            "invalid_input")


@pytest.mark.parametrize("buys,reason", [({}, "missing_proposed_buys"), ({"AAA": -1.0}, "invalid_input"),
                                         ({"AAA": math.nan}, "invalid_input")])
def test_bad_buys_fail_closed(buys, reason):
    inputs, asof, _ = _setup()
    _failed(_run(replace(inputs, proposed_buys_usd_by_ticker=buys), asof), reason)


def test_bad_event_fails_closed():
    inputs, asof, _ = _setup()
    _failed(_run(replace(inputs, events_by_ticker={"AAA": [CorporateEvent("rumour", asof, asof)]}), asof),
            "invalid_input")
    _failed(_run(replace(inputs, events_by_ticker={"AAA": ["8k"]}), asof), "invalid_input")


def test_no_prices_fails_closed():
    inputs, asof, _ = _setup()
    _failed(_run(replace(inputs, close_rows=[]), asof), "missing_prices")


@pytest.mark.parametrize("cfg", [
    ResidualReversalConfig(ou_window=90),
    ResidualReversalConfig(b_max=1.0),
    ResidualReversalConfig(min_multiplier=1.1),
    ResidualReversalConfig(max_multiplier=0.9),
    ResidualReversalConfig(slope=math.nan),
    ResidualReversalConfig(half_life_jump=1.0),
    ResidualReversalConfig(min_half_life_baseline=21),
    ResidualReversalConfig(beta_window=True),
])
def test_bad_config_fails_closed(cfg):
    inputs, asof, _ = _setup()
    _failed(_run(inputs, asof, cfg), "invalid_config")
