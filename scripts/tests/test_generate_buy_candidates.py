#!/usr/bin/env python3
"""Tests for the Gate 5 (1530ET final-buy) candidate generator.

Scope note: scripts/generate_buy_candidates.py deliberately covers only the
1530ET slot today (see its module docstring's "SCOPE" section) -- the other
six roster slots run through morning_dca.MorningDCAExecutor's own,
differently-implemented evaluate_signal(), not the near_close_dca functions
this module calls, and wiring them here would silently reimplement rather
than reuse that logic. These tests only exercise 1530ET.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

gbc = pytest.importorskip(
    "scripts.generate_buy_candidates",
    reason="scripts/generate_buy_candidates.py not present",
)

import dca_runner  # noqa: E402
from near_close_dca import Action, StrategyConfig  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 8, 5)
SLOT = "1530ET"


def _iso(hh: int, mm: int) -> str:
    return datetime.combine(DAY, time(hh, mm), tzinfo=ET).isoformat()


def _prev_close_bar(close: float, low: float) -> dict:
    # The prior regular-session daily bar: its close feeds previous_close, its
    # low feeds previous_low -- evaluate_source_signal reads both off this one
    # bar (near_close_dca.py: previous_close = previous_close_bar.close,
    # previous_low = previous_close_bar.low).
    return {
        "timestamp": datetime.combine(DAY - timedelta(days=1), time(16, 0), tzinfo=ET).isoformat(),
        "open": close,
        "high": close + 1,
        "low": low,
        "close": close,
        "volume": 10000.0,
    }


def _intraday_bars(early_price: float, late_price: float, *, transition_hh: int = 14, transition_mm: int = 35) -> list:
    """One bar per minute from 09:30 to 15:30 ET.

    Price is `early_price` before the transition and `late_price` from the
    transition onward. The transition sits 5 minutes after the 1530ET slot's
    60-minutes-back lookback point (14:30), so the lookback anchor reliably
    resolves to `early_price` and the 15:30 decision anchor reliably resolves
    to `late_price` regardless of exactly how the real completed-bar anchor
    rounds a target time.
    """
    bars = []
    cur = datetime.combine(DAY, time(9, 30), tzinfo=ET)
    end = datetime.combine(DAY, time(15, 30), tzinfo=ET)
    transition = datetime.combine(DAY, time(transition_hh, transition_mm), tzinfo=ET)
    while cur <= end:
        price = late_price if cur >= transition else early_price
        bars.append(
            {"timestamp": cur.isoformat(), "open": price, "high": price, "low": price, "close": price, "volume": 1000.0}
        )
        cur += timedelta(minutes=1)
    return bars


def _market_data(
    ticker: str,
    *,
    early_price: float,
    late_price: float,
    previous_close: float,
    previous_low: float,
    sources: tuple = ("finance", "robinhood"),
) -> dict:
    per_ticker = {
        "previous_close_bar": _prev_close_bar(previous_close, previous_low),
        "intraday_bars": _intraday_bars(early_price, late_price),
    }
    return {"sources": {source: {ticker: per_ticker} for source in sources}}


def _config(**overrides) -> StrategyConfig:
    base = dict(
        tickers=("TEST",),
        excluded_tickers=frozenset(),
        sizing_tiers=((0.001, 1.0), (0.02, 1.5), (0.04, 2.0), (0.05, 2.5), (0.08, 5.0)),
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _write_populated_config(config_dir) -> None:
    """Write minimal-but-meaningful content for every
    gbc._REQUIRED_CONFIG_FILENAMES file -- _require_populated_config checks
    real content (a non-empty 'active' ticker list, both base_amount_usd
    and base_amount_below_close_usd, a non-empty 'tiers' list, an active
    per_ticker_sizing that calibrates every active ticker), not just file
    existence or bare truthiness, so an empty "{}" placeholder or a
    partial budgets.json/per_ticker_sizing.json is no longer enough for a
    test to reach past config loading.

    budgets.json/sizing_ladder.json are copied VERBATIM from this repo's
    own real config/ rather than hand-built: dca_runner._validated_sizing_
    ladder()/_validated_max_per_order_hard_limit() (stricter than, and
    cross-checked against, dca_config's own load_sizing_tiers/load_budgets)
    require base_amount_usd, base_amount_below_close_usd, tiers[].
    max_drop_pct, AND budgets.json's circuit_breakers.max_per_order_usd/
    high_multiplier_threshold to all be internally consistent in whole
    cents -- a --config-dir that round-20's env-var injection makes
    dca_runner actually import from now reaches that real, stricter
    parser too, not just dca_config's. Copying the real files guarantees
    that consistency instead of hand-maintaining it. market_calendar.json
    is copied for the same reason: dca_runner.market_day_status (called by
    _previous_trading_day) reads it from dca_runner.CONFIG_DIR, the SAME
    directory this env-var injection points at, so a --config-dir missing
    it would fail as a raw FileNotFoundError deep inside dca_runner instead
    of the config-content checks these tests actually exercise. Only
    tickers.json/per_ticker_sizing.json are hand-built -- dca_runner never
    reads either at import time, so scoping them down to just "TEST" is safe.
    """
    real_config_dir = Path(__file__).resolve().parents[2] / "config"
    for name in ("budgets.json", "sizing_ladder.json", "market_calendar.json"):
        (config_dir / name).write_text((real_config_dir / name).read_text(encoding="utf-8"), encoding="utf-8")
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["TEST"]}), encoding="utf-8")
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps(
            {
                "status": "active",
                "tickers": {"TEST": {"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}},
            }
        ),
        encoding="utf-8",
    )


def _run(
    market_data: dict,
    config: StrategyConfig,
    *,
    slot: str = SLOT,
    source_errors: tuple = (),
    ticker_errors: dict = None,
    already_covered_tickers: tuple = (),
) -> list:
    now = datetime.combine(DAY, gbc.SLOT_SPECS[slot]["decision_time_et"], tzinfo=ET)
    return gbc.generate_candidates(
        slot=slot,
        now=now,
        config=config,
        market_data_by_source=market_data["sources"],
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=already_covered_tickers,
    )


def _only(candidates: list, ticker: str = "TEST") -> dict:
    matches = [c for c in candidates if c["ticker"] == ticker]
    assert len(matches) == 1, f"expected exactly one candidate for {ticker}, got {matches}"
    return matches[0]


# --- tests --------------------------------------------------------------


def test_buy_candidate_below_close_and_low():
    """4% drop below both prior close and prior low -> BUY at the $2 base, 2x tier."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value
    assert candidate["buy_multiplier"] == pytest.approx(2.0)
    assert candidate["selected_base_amount_usd"] == pytest.approx(2.0)
    assert candidate["suggested_amount_usd"] == pytest.approx(4.0)
    # Gate 6's allocation-priority tie-break (higher multiplier, then larger
    # percent drop) needs this on every candidate, not just the ticker+amount.
    assert candidate["drop_vs_previous_close_pct"] == pytest.approx(4.0)


def test_trend_filter_is_bypassed_at_1530et():
    """1530ET ignores the 60-minute trend filter: price below the prior close
    but RISING over the lookback window (recovering, not still falling) still
    buys, per near_close_dca.StrategyConfig.ignore_trend_filter /
    config/scheduled_tasks.json's final_buy_behavior.ignore_trend_filter."""
    data = _market_data("TEST", early_price=94.0, late_price=97.0, previous_close=100.0, previous_low=93.0)
    candidate = _only(_run(data, _config()))
    # Both sources still agree on BUY (reconcile_signals' own reason wins over
    # the underlying per-source signal's "trend filter bypassed" detail) -- the
    # real assertion is that a RISING intraday price still bought at all, which
    # only the ignore_trend_filter override for this slot allows.
    assert candidate["action"] == Action.BUY.value
    assert candidate["finance_action"] == Action.BUY.value
    assert candidate["robinhood_action"] == Action.BUY.value


def test_kill_switch_caps_multiplier_to_1x():
    """Deployed 7d spend past the 40%-of-monthly threshold caps every positive
    multiplier at 1x (near_close_dca.kill_switch_engaged), even for an 8% drop
    that would otherwise reach the top 5x tier."""
    data = _market_data("TEST", early_price=100.0, late_price=92.0, previous_close=100.0, previous_low=93.0)
    config = _config(monthly_cash_allocation_usd=1000.0, kill_switch_7d_deploy_fraction=0.40, deployed_last_7d_usd=500.0)
    candidate = _only(_run(data, config))
    assert candidate["action"] == Action.BUY.value
    assert candidate["buy_multiplier"] == pytest.approx(1.0)
    assert candidate["suggested_amount_usd"] == pytest.approx(2.0)


def test_source_outage_forces_manual_review_despite_other_source_buy():
    """Listing a source in source_errors means its FETCH failed, not just that
    a given ticker was unresolved on it -- every ticker scores provider_error
    on that source, so reconcile_signals must refuse to auto-buy on the
    surviving source's BUY alone (near_close_dca.reconcile_signals' errored-
    source check), even though Robinhood alone shows a clean qualifying drop."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    candidate = _only(_run(data, _config(), source_errors=("finance",)))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value
    assert candidate["action"] != Action.BUY.value


def test_missing_source_key_fails_closed_without_source_errors():
    """A top-level source key ENTIRELY ABSENT from `sources` (not merely
    present-but-empty) must fail closed exactly like an explicit
    source_errors entry, even when the caller forgot to also populate
    source_errors -- a caller whose Finance fetch failed and who simply never
    built a "finance" object must not get a silently-permitted single-source
    Robinhood auto-buy (Codex review: "Fail closed when a source is
    absent")."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    # No source_errors passed at all -- the caller simply omitted "finance".
    candidate = _only(_run(data, _config()))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value
    assert candidate["action"] != Action.BUY.value


def test_ticker_missing_from_market_data_is_not_a_buy():
    """A ticker in the active universe but absent from --market-data is still
    reported (never silently dropped), scored non-BUY on both sources."""
    config = _config(tickers=("TEST", "GHOST"))
    data = _market_data("TEST", early_price=100.0, late_price=94.0, previous_close=100.0, previous_low=93.0)
    candidates = _run(data, config)
    ghost = _only(candidates, ticker="GHOST")
    assert ghost["action"] != Action.BUY.value
    assert ghost["finance_action"] is None
    assert ghost["robinhood_action"] is None


def test_excluded_ticker_is_never_scored():
    """A ticker present in both `tickers` and `excluded_tickers` is skipped
    entirely -- defense in depth, even though a config-loaded excluded ticker
    is not normally also present in the active list."""
    config = _config(tickers=("TEST", "BANNED"), excluded_tickers=frozenset({"BANNED"}))
    data = _market_data("TEST", early_price=100.0, late_price=94.0, previous_close=100.0, previous_low=93.0)
    for source in data["sources"].values():
        source["BANNED"] = source["TEST"]
    candidates = _run(data, config)
    assert [c for c in candidates if c["ticker"] == "BANNED"] == []


def test_unknown_slot_is_rejected():
    data = _market_data("TEST", early_price=100.0, late_price=94.0, previous_close=100.0, previous_low=93.0)
    with pytest.raises(ValueError):
        gbc.generate_candidates(
            slot="0935ET",
            now=datetime.combine(DAY, time(9, 35), tzinfo=ET),
            config=_config(),
            market_data_by_source=data["sources"],
        )


def test_cli_requires_deployed_last_7d_usd(tmp_path, monkeypatch):
    """No default: omitting --deployed-last-7d-usd must fail argument parsing
    rather than silently assume zero trailing spend (which could mask a
    tripped kill-switch)."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: _config())

    with pytest.raises(SystemExit):
        gbc.main(["--slot", SLOT, "--trading-day", DAY.isoformat(), "--market-data", str(market_data_path)])


def test_cli_end_to_end(tmp_path, monkeypatch):
    """The CLI wires argument parsing -> generate_candidates -> stdout JSON.

    load_strategy_config is monkeypatched to a hermetic in-memory config so
    this test never touches the real config/ directory; it does not re-verify
    kill-switch plumbing (see test_kill_switch_caps_multiplier_to_1x for that),
    only that main() parses args, reads --market-data, and prints one valid
    JSON document.
    """
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: _config())

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["slot"] == SLOT
    assert document["buy_count"] == 1
    assert document["candidates"][0]["ticker"] == "TEST"


@pytest.mark.parametrize("raw_value", ["nan", "-1", "-inf"])
def test_cli_rejects_non_finite_or_negative_deployed_last_7d_usd(tmp_path, monkeypatch, raw_value):
    """A NaN or negative --deployed-last-7d-usd must fail argument parsing.

    near_close_dca.kill_switch_engaged's `deployed_last_7d_usd > threshold`
    check evaluates False for both a NaN and a negative value, which would
    silently disable the 7-day kill-switch instead of tripping it and let
    graded_buy_multiplier emit uncapped sizing (Codex review: "Reject
    non-finite trailing spend")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: _config())

    with pytest.raises(SystemExit):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", raw_value,
            ]
        )


def test_evaluated_count_counts_every_row_buy_count_counts_only_buys(tmp_path, monkeypatch):
    """evaluated_count is a full-universe-scan count (every action: BUY,
    NO_ACTION, NO_DATA, MANUAL_REVIEW) -- not Gate 5's "zero candidates: end
    silently" terminal condition, which is buy_count. A quiet, rising tape
    with nothing to buy must show evaluated_count > 0 while buy_count == 0
    (Codex review: "Report zero actionable candidates as zero")."""
    config = _config(tickers=("TEST", "OTHER"))
    per_ticker = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 101.0),
    }
    data = {"sources": {source: {"TEST": per_ticker, "OTHER": per_ticker} for source in ("finance", "robinhood")}}
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert all(c["action"] != Action.BUY.value for c in document["candidates"])
    assert document["evaluated_count"] == 2
    assert document["buy_count"] == 0
    assert document["planner_candidates"] == []


def test_drop_vs_previous_close_pct_matches_allocation_sort_key_not_winning_source():
    """near_close_dca.allocation_sort_key tie-breaks equal multipliers on the
    MAX drop across both sources, not whichever source's price the reconciled
    decision actually targeted (the smaller-amount "winning" source per
    reconcile_signals). Finance sees a shallow 2% drop and wins reconciliation
    on its smaller $3.00 amount (1.5x tier); Robinhood sees a steeper 6% drop
    but loses on its larger $5.00 amount (2.5x tier). The emitted field must
    still reflect Robinhood's larger 6% drop, matching allocation_sort_key,
    not finance's smaller 2% (Codex review: "Emit the allocation-sort drop")."""
    market_data_by_source = {
        "finance": {
            "TEST": {
                "previous_close_bar": _prev_close_bar(100.0, 99.0),
                "intraday_bars": _intraday_bars(100.0, 98.0),
            }
        },
        "robinhood": {
            "TEST": {
                "previous_close_bar": _prev_close_bar(100.0, 99.0),
                "intraday_bars": _intraday_bars(100.0, 94.0),
            }
        },
    }
    now = datetime.combine(DAY, gbc.SLOT_SPECS[SLOT]["decision_time_et"], tzinfo=ET)
    candidates = gbc.generate_candidates(
        slot=SLOT, now=now, config=_config(), market_data_by_source=market_data_by_source
    )
    candidate = _only(candidates)
    assert candidate["finance_action"] == Action.BUY.value
    assert candidate["robinhood_action"] == Action.BUY.value
    # Finance wins reconciliation: $2.00 base * 1.5x = $3.00 < Robinhood's $2.00 * 2.5x = $5.00.
    assert candidate["action"] == Action.BUY.value
    assert candidate["suggested_amount_usd"] == pytest.approx(3.0)
    assert candidate["buy_multiplier"] == pytest.approx(1.5)
    # But the allocation tie-break drop must be Robinhood's larger 6%, not finance's 2%.
    assert candidate["drop_vs_previous_close_pct"] == pytest.approx(6.0)
    # While multiplier_drop_pct -- the value planner_candidates actually uses --
    # must stay finance's OWN 2%: it is what produced buy_multiplier=1.5, and
    # dca_runner.plan_orders() would refuse the candidate if paired with 6%
    # instead (Codex review: "Keep planner drop consistent with multiplier").
    assert candidate["multiplier_drop_pct"] == pytest.approx(2.0)


def test_planner_candidates_are_buy_only_and_parseable_by_dca_runner(tmp_path, monkeypatch):
    """planner_candidates must be directly consumable by
    dca_runner.assess()/plan_orders(), which parse raw (ticker, amount_usd,
    multiplier, drop_pct) rows -- not the debug-oriented dict rows in
    `candidates`, whose different key names would abort those functions on a
    malformed-candidate verdict instead of producing a plan (Codex review:
    "Publish planner-ready candidate rows")."""
    config = _config(tickers=("TEST", "GHOST"))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    # GHOST (missing from market data) must not leak into the BUY-only planner rows.
    assert document["buy_count"] == 1
    assert len(document["planner_candidates"]) == 1
    buy_candidate = _only(document["candidates"])
    assert document["planner_candidates"][0] == [
        buy_candidate["ticker"],
        buy_candidate["suggested_amount_usd"],
        buy_candidate["buy_multiplier"],
        buy_candidate["multiplier_drop_pct"],
    ]
    # Must not raise / abort as a malformed candidate row when fed to the shared planner.
    parsed = dca_runner._parse_candidate_rows(document["planner_candidates"])
    assert [c.ticker for c in parsed] == ["TEST"]


def test_planner_candidates_survive_dca_runner_validation_when_sources_diverge():
    """Regression: earlier code paired planner_candidates' drop_pct with the
    allocation-sort MAX (drop_vs_previous_close_pct) instead of the winning
    source's own drop (multiplier_drop_pct), which got a real BUY wrongly
    refused by dca_runner.plan_orders() as inconsistent_multiplier_notional.
    config/sizing_ladder.json's real 1.5x tier only spans [2.0%, 4.0%);
    pairing 1.5x with Robinhood's 6% (the old, buggy value) falls outside it,
    even though finance's own 2% (the fixed value) is exactly what produced
    the 1.5x/$3.00 decision (Codex review: "Keep planner drop consistent with
    multiplier"). Same divergent-source setup as
    test_drop_vs_previous_close_pct_matches_allocation_sort_key_not_winning_source."""
    market_data_by_source = {
        "finance": {
            "TEST": {
                "previous_close_bar": _prev_close_bar(100.0, 99.0),
                "intraday_bars": _intraday_bars(100.0, 98.0),
            }
        },
        "robinhood": {
            "TEST": {
                "previous_close_bar": _prev_close_bar(100.0, 99.0),
                "intraday_bars": _intraday_bars(100.0, 94.0),
            }
        },
    }
    now = datetime.combine(DAY, gbc.SLOT_SPECS[SLOT]["decision_time_et"], tzinfo=ET)
    candidate = _only(
        gbc.generate_candidates(slot=SLOT, now=now, config=_config(), market_data_by_source=market_data_by_source)
    )
    fixed_row = [
        candidate["ticker"], candidate["suggested_amount_usd"], candidate["buy_multiplier"],
        candidate["multiplier_drop_pct"],
    ]
    buggy_row = [
        candidate["ticker"], candidate["suggested_amount_usd"], candidate["buy_multiplier"],
        candidate["drop_vs_previous_close_pct"],
    ]
    (fixed_candidate,) = dca_runner._parse_candidate_rows([fixed_row])
    (buggy_candidate,) = dca_runner._parse_candidate_rows([buggy_row])

    assert dca_runner._candidate_multiplier_notional_mismatch(fixed_candidate) is False
    assert dca_runner._candidate_multiplier_notional_mismatch(buggy_candidate) is True


def test_quote_in_market_data_becomes_target_price():
    """An OPTIONAL per-ticker quote must override the decision-bar-close
    fallback for target_price, matching parity with the live
    RobinhoodDataProvider.get_quote path used by the five-cron final-buy run
    (Codex review: "Preserve signal-time quotes in static market data"). The
    quote affects only target_price -- decision_price (and so the BUY action
    and multiplier) is still driven by the completed decision bar.

    Robinhood-only, with Finance present-but-uncovering-this-ticker (not
    absent outright): the live FinanceDataProvider has no get_quote() at
    all, so a Finance quote is always ignored (see
    test_finance_quote_is_ignored) -- attaching one here too would make
    reconciliation's equal-multiplier tie-break pick Finance's own
    decision-bar close instead, defeating the point of this test. Finance
    must stay a present-but-empty source ({}) rather than an absent one:
    an absent source scores provider_error=True (Codex review: "Fail closed
    when a source is absent") and reconcile_signals then forces
    MANUAL_REVIEW instead of the single-source BUY this test wants."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    data["sources"]["finance"] = {}
    data["sources"]["robinhood"]["TEST"]["quote"] = {"price": 95.50, "as_of": _iso(15, 30)}
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value
    assert candidate["buy_multiplier"] == pytest.approx(2.0)
    assert candidate["target_price"] == pytest.approx(95.50)


def test_missing_quote_falls_back_to_decision_bar_close():
    """Without a quote, target_price stays the completed decision-bar close --
    pre-existing behavior, now asserted explicitly alongside quote support."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    candidate = _only(_run(data, _config()))
    assert candidate["target_price"] == pytest.approx(96.0)


def test_same_day_top_up_candidates_are_buy_only_and_include_current_price(tmp_path, monkeypatch):
    """same_day_top_up_candidates must match scripts/same_day_top_up_gate.py's
    --candidates row shape ({"ticker", "desired_usd", "current_price"}) for
    BUY-only rows. current_price must be present and positive -- that gate's
    evaluator treats a missing/non-positive current_price as
    MISSING_CURRENT_PRICE and scores $0/manual review before ever applying
    the top-up arithmetic, so omitting it made every row fail closed instead
    of being top-up-gate ready (Codex review: "Include current_price in
    top-up rows"). It is seeded from this decision's own target_price -- a
    live executor is still expected to overwrite it with a freshly
    re-fetched quote immediately before invoking Gate 6.5."""
    config = _config(tickers=("TEST", "GHOST"))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    buy_candidate = _only(document["candidates"])
    assert document["same_day_top_up_candidates"] == [
        {
            "ticker": buy_candidate["ticker"],
            "desired_usd": buy_candidate["suggested_amount_usd"],
            "current_price": buy_candidate["target_price"],
        }
    ]
    assert all(row["current_price"] > 0 for row in document["same_day_top_up_candidates"])


def test_non_finite_bar_price_is_rejected():
    """A NaN/Infinity bar price must fail closed at parse time -- both JSON and
    Python's float() accept those tokens silently, and every comparison in
    evaluate_source_signal would otherwise evaluate False, reading as a quiet
    NO_ACTION tape instead of a malformed market-data snapshot (Codex review:
    "Reject non-finite market prices")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["intraday_bars"][-1]["close"] = float("nan")
    with pytest.raises(ValueError):
        _run(data, _config())


def test_non_finite_quote_price_is_rejected():
    """Same fail-closed requirement as bar prices, for the optional quote."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": float("inf"), "as_of": _iso(15, 30)}
    with pytest.raises(ValueError):
        _run(data, _config())


def test_missing_sources_key_is_rejected(tmp_path, monkeypatch):
    """--market-data omitting "sources" entirely is malformed input, not "both
    providers returned nothing" -- it must raise rather than silently produce
    a schema-valid, all-NO_DATA/buy_count=0 document a caller could read as a
    legitimate quiet tape (Codex review: "Require the top-level sources
    payload")."""
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: _config())

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


@pytest.mark.parametrize("bad_price", [0.0, -1.0])
def test_non_positive_bar_price_is_rejected(bad_price):
    """A 0/-1 placeholder close (e.g. from a failed fetch) is finite but must
    still fail closed -- compared against a normal positive previous_close it
    grades as a 100%+ drop, reconciling to a max-size 5x BUY at a
    non-positive target_price instead of being treated as malformed data
    (Codex review: "Reject non-positive market prices")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["intraday_bars"][-1]["close"] = bad_price
    with pytest.raises(ValueError):
        _run(data, _config())


def test_non_positive_quote_price_is_rejected():
    """Same fail-closed requirement as bar prices, for the optional quote."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": 0.0, "as_of": _iso(15, 30)}
    with pytest.raises(ValueError):
        _run(data, _config())


def test_calibrated_ticker_mismatching_global_ladder_is_planner_incompatible(tmp_path, monkeypatch):
    """A per-ticker-calibrated multiplier/drop pairing that graded_buy_multiplier
    correctly honors can legitimately fall outside dca_runner's GLOBAL
    config/sizing_ladder.json range for that multiplier -- dca_runner has no
    awareness of config/per_ticker_sizing.json calibration, which covers most
    of the active universe. Such a row must be held back into
    planner_incompatible_tickers, not silently shipped in planner_candidates
    where dca_runner.plan_orders() would refuse it as
    inconsistent_multiplier_notional and drop a real BUY (Codex review: "Keep
    planner rows compatible with calibrated ladders"). A second, uncalibrated
    ticker on the plain global ladder demonstrates the two are correctly
    partitioned, not that every row is suddenly held back.
    """
    config = _config(
        tickers=("TEST", "OTHER"),
        per_ticker_sizing={
            "TEST": {"tiers": ((0.001, 5.0),), "base_amount_usd": 2.0, "base_amount_below_close_usd": 1.0},
        },
    )
    # TEST: calibrated ladder maps ANY qualifying drop straight to 5.0x: a 1%
    # drop here is real and correct per that calibration, but the GLOBAL
    # ladder's 5.0x tier requires an 8%+ drop.
    data = _market_data("TEST", early_price=100.0, late_price=99.0, previous_close=100.0, previous_low=99.5)
    # OTHER: plain global ladder, 4% drop -> 2.0x, matching
    # config/sizing_ladder.json's real [4.0, 5.0) tier exactly.
    other = _market_data("OTHER", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source in ("finance", "robinhood"):
        data["sources"][source].update(other["sources"][source])
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    test_candidate = _only(document["candidates"], ticker="TEST")
    # The real, calibration-respecting decision is still fully correct in `candidates`.
    assert test_candidate["action"] == Action.BUY.value
    assert test_candidate["buy_multiplier"] == pytest.approx(5.0)
    assert test_candidate["suggested_amount_usd"] == pytest.approx(10.0)
    assert document["buy_count"] == 2
    # But TEST must be held back from planner_candidates and flagged instead,
    # while OTHER (plain global ladder) sails through normally.
    assert document["planner_incompatible_tickers"] == ["TEST"]
    assert [row[0] for row in document["planner_candidates"]] == ["OTHER"]
    assert len(document["planner_candidates"]) + len(document["planner_incompatible_tickers"]) == document["buy_count"]
    # same_day_top_up_candidates feeds Gate 6.5 (scripts/same_day_top_up_gate.py),
    # a SEPARATE auto-placement path from planner_candidates' Gate 6 -- TEST's
    # real BUY is still in `candidates`, but it must not appear here either:
    # Gate 6 was explicitly unable to approve it, and Gate 6.5 sharing the same
    # ticker unfiltered would let a caller auto-place via the path that
    # skipped the ladder-compatibility check entirely (Codex review: "Withhold
    # top-up rows that Gate 6 cannot place").
    assert [row["ticker"] for row in document["same_day_top_up_candidates"]] == ["OTHER"]


def test_boolean_bar_price_is_rejected():
    """Python's bool is an int subclass, so float(True) == 1.0 -- a corrupted
    decision-bar close of JSON true would otherwise silently become a
    finite, positive $1.00 and grade as a ~99% drop against a normal
    previous close instead of failing closed on malformed provider evidence
    (Codex review: "Reject boolean market prices")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["intraday_bars"][-1]["close"] = True
    with pytest.raises(ValueError):
        _run(data, _config())


def test_boolean_volume_is_rejected():
    """Volume goes through the same boolean guard as prices (_finite_float)."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["intraday_bars"][-1]["volume"] = False
    with pytest.raises(ValueError):
        _run(data, _config())


def test_boolean_quote_price_is_rejected():
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": True, "as_of": _iso(15, 30)}
    with pytest.raises(ValueError):
        _run(data, _config())


def test_mis_cased_source_error_is_rejected(tmp_path, monkeypatch):
    """A misspelled/mis-cased source_errors entry (e.g. "Finance") is
    silently NOT "finance" for the exact-string membership check
    finance_unavailable uses -- if sources.finance is also present as {} (a
    legitimate "no data" marker), finance_unavailable would stay False,
    finance would score a plain NO_DATA instead of provider_error=True, and
    reconciliation could allow a Robinhood-only auto-buy instead of blocking
    on the provider outage this entry was meant to record. Must fail closed
    instead (Codex review: "Validate source_errors entries")."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"] = {}
    data["source_errors"] = ["Finance"]
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_unhashable_source_error_entry_is_rejected_not_crashed(tmp_path, monkeypatch):
    """An unhashable source_errors entry (a nested object, from badly
    malformed input) must fail with the same clean ValueError as any other
    invalid entry, not an uncaught TypeError from a hash-based membership
    check."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["source_errors"] = [{"unexpected": "object"}]
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_previous_close_bar_zero_low_is_treated_as_unknown_not_rejected():
    """The live Robinhood adapter's quote-derived previous-close bar has no
    real prior-session low and deliberately stamps low=0.0 as "unknown" --
    select_first_allocation_base_usd treats any non-positive low
    conservatively as the smaller $1 base rather than upgrading to $2.
    Rejecting this live-equivalent shape would abort a live-parity payload,
    or push a caller to fabricate a positive low that could wrongly upgrade
    the base (Codex review: "Preserve unknown prior-low sentinels")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=0.0)
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value
    # previous_low=0.0 means "unknown" -> conservative $1 base, NOT upgraded
    # to $2 even though decision_price(96) is numerically "below" 0.
    assert candidate["selected_base_amount_usd"] == pytest.approx(1.0)
    assert candidate["suggested_amount_usd"] == pytest.approx(2.0)


def test_previous_close_bar_non_finite_low_still_rejected():
    """allow_unknown_low relaxes positivity only, not finiteness/type -- a
    NaN low must still fail closed same as any other malformed price."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["previous_close_bar"]["low"] = float("nan")
    with pytest.raises(ValueError):
        _run(data, _config())


def test_intraday_bar_zero_low_is_still_rejected():
    """allow_unknown_low only applies to previous_close_bar -- an intraday
    bar has no live-equivalent "unknown low" sentinel and must keep the
    strict positive check."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["intraday_bars"][-1]["low"] = 0.0
    with pytest.raises(ValueError):
        _run(data, _config())


def test_manual_review_tickers_are_reported(tmp_path, monkeypatch):
    """A source outage forcing MANUAL_REVIEW must be visible in a dedicated
    summary field -- otherwise buy_count==0 reads as "quiet day, nothing to
    do" even when a ticker urgently needs a human/agent look (Codex review:
    "Report manual-review rows before zero-candidate exits")."""
    config = _config(tickers=("TEST",))
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    data["source_errors"] = ["finance"]
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["buy_count"] == 0
    assert document["manual_review_count"] == 1
    assert document["manual_review_tickers"] == ["TEST"]
    assert _only(document["candidates"])["action"] == Action.MANUAL_REVIEW.value


def test_ticker_error_forces_manual_review_over_present_data():
    """A per-ticker fetch exception (ticker_errors) must win over any data
    also present for that ticker in sources, and must not collapse into the
    benign "this source doesn't cover this ticker" path that permits a
    single-source auto-buy (Codex review: "Preserve per-ticker provider
    errors")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    candidate = _only(_run(data, _config(), ticker_errors={"finance": ["TEST"]}))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value


def test_ticker_not_in_error_list_is_unaffected():
    """ticker_errors is per-ticker, not a blunt whole-source instrument --
    a ticker absent from the error list must still resolve normally even
    when OTHER tickers on the same source are flagged."""
    config = _config(tickers=("TEST", "OTHER"))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    other = _market_data("OTHER", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source in ("finance", "robinhood"):
        data["sources"][source].update(other["sources"][source])
    candidates = _run(data, config, ticker_errors={"finance": ["TEST"]})
    assert _only(candidates, ticker="TEST")["action"] == Action.MANUAL_REVIEW.value
    other_candidate = _only(candidates, ticker="OTHER")
    assert other_candidate["finance_action"] == Action.BUY.value
    assert other_candidate["action"] == Action.BUY.value


def test_invalid_ticker_errors_shape_is_rejected(tmp_path, monkeypatch):
    """ticker_errors must be an object keyed by known source names with list
    values -- malformed shapes must fail closed at the input boundary rather
    than surfacing a confusing error deeper in generate_candidates."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["ticker_errors"] = {"Finance": ["TEST"]}
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_null_ticker_errors_payload_is_rejected(tmp_path, monkeypatch):
    """An explicit JSON "ticker_errors": null (key PRESENT, value None) is a
    malformed shape, not a genuinely omitted key -- payload.get("ticker_
    errors", {}) only substitutes the default for an ABSENT key, so this
    null survived unchanged into generate_candidates()'s own None-means-
    "not provided" coercion and was silently treated as "no per-ticker
    errors" instead of the malformed evidence a failed serializer actually
    produced (Codex review: "Reject null ticker_errors payloads")."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["ticker_errors"] = None
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError, match="ticker_errors"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_omitted_ticker_errors_key_is_still_tolerated(tmp_path, monkeypatch):
    """Regression guard: the null-check above must not also start rejecting
    a genuinely OMITTED ticker_errors key -- that is the existing, separate
    "no per-ticker error evidence" shape."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    exit_code = gbc.main(
        [
            "--slot", SLOT,
            "--trading-day", DAY.isoformat(),
            "--market-data", str(market_data_path),
            "--deployed-last-7d-usd", "0",
        ]
    )
    assert exit_code == 0


def test_ticker_errors_rejects_unrecognized_ticker(tmp_path, monkeypatch):
    """A ticker_errors entry that is lowercase, misspelled, or otherwise not
    in the configured universe would silently never match config.tickers'
    uppercase symbols in generate_candidates' set-membership check -- the
    error marker would just be dropped with no indication anything went
    wrong. Validating membership up front turns that silent bypass into a
    loud, fail-closed error (Codex review: "Validate ticker_errors
    values")."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["ticker_errors"] = {"finance": ["test"]}
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_ticker_errors_rejects_non_string_entry(tmp_path, monkeypatch):
    """A non-string ticker_errors entry (e.g. a stray int) must fail closed
    at the input boundary, not be silently ignored downstream."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["ticker_errors"] = {"finance": [123]}
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_ticker_errors_tolerates_excluded_ticker(tmp_path, monkeypatch):
    """A ticker_errors entry naming a currently-excluded (but real) symbol is
    a guaranteed no-op -- excluded tickers are skipped before ticker_errors
    is ever consulted -- and must be tolerated, not treated the same as a
    genuinely unrecognized entry."""
    config = _config(tickers=("TEST",), excluded_tickers=frozenset({"EXCLUDED"}))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["ticker_errors"] = {"finance": ["EXCLUDED"]}
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert _only(document["candidates"])["action"] == Action.BUY.value


def test_stale_intraday_bars_force_manual_review():
    """A source whose intraday bars stop hours before the decision anchor (a
    truncated/partial fetch) must not have its last available bar treated as
    the fresh decision reading -- since 1530ET bypasses the trend filter, a
    stale bar could otherwise still reconcile to a fresh-looking BUY instead
    of failing the market-data evidence gate (Codex review: "Reject stale
    intraday bars")."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    cutoff = datetime.combine(DAY, time(10, 0), tzinfo=ET)
    stale_bars = [b for b in _intraday_bars(100.0, 96.0) if datetime.fromisoformat(b["timestamp"]) <= cutoff]
    assert stale_bars, "test setup: expected at least one bar at/before the cutoff"
    data["sources"]["finance"] = {
        "TEST": {"previous_close_bar": _prev_close_bar(100.0, 97.0), "intraday_bars": stale_bars}
    }
    candidate = _only(_run(data, _config()))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["finance_action"] == Action.NO_DATA.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value


def test_stale_quote_falls_back_to_decision_bar_close():
    """A quote timestamped hours away from the decision moment cannot be
    verified to reflect it, and must fall back to the completed decision-bar
    close exactly like a missing quote -- otherwise a stale saved quote could
    become target_price (and so same_day_top_up_candidates' current_price),
    letting Gate 6.5 compare a same-day top-up against a stale price (Codex
    review: "Reject stale quote timestamps")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": 95.50, "as_of": _iso(9, 31)}
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value
    assert candidate["target_price"] == pytest.approx(96.0)


def test_future_quote_falls_back_to_decision_bar_close():
    """A quote timestamped after the decision moment is just as unverifiable
    as a stale one and must also fall back."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": 95.50, "as_of": _iso(16, 0)}
    candidate = _only(_run(data, _config()))
    assert candidate["target_price"] == pytest.approx(96.0)


def test_quote_missing_as_of_falls_back_to_decision_bar_close():
    """A quote with no as_of at all cannot be verified fresh and must fall
    back the same as a stale or future one."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["quote"] = {"price": 95.50}
    candidate = _only(_run(data, _config()))
    assert candidate["target_price"] == pytest.approx(96.0)


def test_missing_previous_close_bar_forces_manual_review():
    """A ticker attempted on a source (has intraday bars) but missing
    previous_close_bar entirely is an incomplete fetch, not "not covered" --
    evaluate_source_signal's own None-check defaults to provider_error=False,
    which would otherwise let a clean Robinhood BUY sail through alone
    (Codex review: "Treat incomplete attempted source payloads as
    errors")."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    data["sources"]["finance"] = {"TEST": {"intraday_bars": _intraday_bars(100.0, 96.0)}}
    candidate = _only(_run(data, _config()))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["finance_action"] == Action.NO_DATA.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value


def test_missing_lookback_bar_forces_manual_review():
    """A ticker whose intraday bars start too late to cover the 60-minute
    lookback point -- but do cover a fresh decision moment -- is also an
    incomplete attempt: evaluate_source_signal requires the lookback bar to
    be present just to score at all, even though 1530ET's bypassed trend
    filter never uses its value."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    cutoff = datetime.combine(DAY, time(15, 0), tzinfo=ET)
    late_bars = [b for b in _intraday_bars(100.0, 96.0) if datetime.fromisoformat(b["timestamp"]) >= cutoff]
    assert late_bars, "test setup: expected at least one bar at/after the cutoff"
    data["sources"]["finance"] = {
        "TEST": {"previous_close_bar": _prev_close_bar(100.0, 97.0), "intraday_bars": late_bars}
    }
    candidate = _only(_run(data, _config()))
    assert candidate["robinhood_action"] == Action.BUY.value
    assert candidate["finance_action"] == Action.NO_DATA.value
    assert candidate["action"] == Action.MANUAL_REVIEW.value


def test_previous_close_bar_dated_today_is_rejected():
    """previous_close_bar must be dated strictly before the trading day -- a
    same-day (or future) close is not "the prior completed regular session"
    and would otherwise silently corrupt BUY grading and $1/$2 base
    selection instead of failing the malformed evidence (Codex review:
    "Reject stale previous-close bars")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    for source_payload in data["sources"].values():
        source_payload["TEST"]["previous_close_bar"]["timestamp"] = datetime.combine(
            DAY, time(9, 30), tzinfo=ET
        ).isoformat()
    with pytest.raises(ValueError):
        _run(data, _config())


def test_previous_close_bar_from_weeks_earlier_is_rejected():
    """previous_close_bar must be dated the immediately previous market
    session, not merely any date before trading_day. The old check only
    rejected same-day-or-future bars, so a reused/cached bar from days or
    weeks earlier passed just as easily as a genuinely fresh one, and would
    be scored as if it were the real prior close/low baseline instead of
    failing the malformed evidence (Codex review: "Reject stale
    previous-close bars")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    stale_timestamp = datetime.combine(DAY - timedelta(days=7), time(16, 0), tzinfo=ET).isoformat()
    for source_payload in data["sources"].values():
        source_payload["TEST"]["previous_close_bar"]["timestamp"] = stale_timestamp
    with pytest.raises(ValueError, match="immediately previous market session"):
        _run(data, _config())


def test_previous_trading_day_skips_the_weekend():
    """The previous trading day is computed from config/market_calendar.json
    (dca_runner's own Gate 0 calendar), not a fixed day-count offset -- for
    a Monday trading day, the correct previous session is the preceding
    Friday, not Sunday. A fixed "N days back" rule would get this wrong for
    some N regardless of which N is picked (Codex review: "Reject stale
    previous-close bars")."""
    assert gbc._previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)


def test_previous_trading_day_wraps_calendar_coverage_error():
    """A trading_day whose previous session falls outside
    market_calendar.json's years_covered must surface as this module's
    usual ValueError, not an unhandled dca_runner.CalendarCoverageError."""
    with pytest.raises(ValueError, match="cannot determine the previous trading day"):
        gbc._previous_trading_day(date(2026, 1, 1))


def test_generate_candidates_validates_source_errors_directly():
    """generate_candidates is itself a public, exported API -- a caller that
    invokes it directly (bypassing main()'s CLI) must still get the same
    fail-closed validation, not a silent bypass (Codex review: "Validate
    error markers in the public helper")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    with pytest.raises(ValueError):
        _run(data, _config(), source_errors=("Finance",))


def test_generate_candidates_validates_ticker_errors_directly():
    """Same as above, for ticker_errors -- a direct generate_candidates()
    caller must not bypass the ticker-universe membership check either."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    with pytest.raises(ValueError):
        _run(data, _config(), ticker_errors={"finance": ["test"]})


def test_provider_error_without_manual_review_is_surfaced(tmp_path, monkeypatch):
    """A provider error only escalates the overall action to MANUAL_REVIEW
    when the surviving source would otherwise BUY. When neither source
    qualifies as BUY anyway (price above the previous close here), the
    ticker reconciles as plain NO_DATA/NO_ACTION with buy_count and
    manual_review_count both 0 -- but the outage still happened and must be
    visible somewhere, or a caller cannot distinguish this from a genuinely
    clean quiet day (Codex review: "Surface provider-error runs before
    zero-candidate exits")."""
    config = _config(tickers=("TEST",))
    data = _market_data(
        "TEST", early_price=100.0, late_price=101.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    data["source_errors"] = ["finance"]
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    candidate = _only(document["candidates"])
    assert candidate["action"] != Action.BUY.value
    assert candidate["action"] != Action.MANUAL_REVIEW.value
    assert candidate["provider_error"] is True
    assert document["buy_count"] == 0
    assert document["manual_review_count"] == 0
    assert document["provider_error_count"] == 1
    assert document["provider_error_tickers"] == ["TEST"]


def test_clean_run_has_no_provider_errors(tmp_path, monkeypatch):
    """A normal run with no source_errors/ticker_errors/incomplete payloads
    must report provider_error_count=0 and provider_error=False on every
    row -- the new field must not false-positive on ordinary data."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["provider_error_count"] == 0
    assert document["provider_error_tickers"] == []
    assert _only(document["candidates"])["provider_error"] is False


def test_finance_quote_is_ignored():
    """The live FinanceDataProvider (dca_live_adapters.py) has no
    get_quote() method at all -- Finance-sourced target prices always fall
    back to the completed decision bar's close. A caller-supplied "quote"
    field for Finance must not be surfaced, or reconciliation's equal-
    multiplier tie-break could publish a target_price the live path could
    never produce (Codex review: "Ignore Finance quotes to preserve live
    target prices")."""
    as_of = datetime.combine(DAY, time(15, 30), tzinfo=ET)
    finance_data = gbc._StaticMarketData(
        gbc.SourceName.FINANCE,
        {"TEST": {"quote": {"price": 123.45, "as_of": as_of.isoformat()}}},
        quote_staleness_minutes=5,
        trading_day=DAY,
    )
    assert finance_data.get_quote("TEST", as_of) is None


def test_robinhood_quote_is_still_used():
    """Regression guard: the Finance-specific quote suppression above must
    not also silently swallow Robinhood's real, live quote capability."""
    as_of = datetime.combine(DAY, time(15, 30), tzinfo=ET)
    robinhood_data = gbc._StaticMarketData(
        gbc.SourceName.ROBINHOOD,
        {"TEST": {"quote": {"price": 123.45, "as_of": as_of.isoformat()}}},
        quote_staleness_minutes=5,
        trading_day=DAY,
    )
    assert robinhood_data.get_quote("TEST", as_of) == (123.45, as_of)


def test_document_publishes_runner_slot_id(tmp_path, monkeypatch):
    """dca_runner.SLOTS uses "1530", not "1530ET" -- dca_runner.assess()/
    execute_plan() intentionally fail loud on an unrecognized slot_id, so an
    executor feeding this document into that shared Gate 6/placement path
    needs the runner-compatible id directly rather than deriving its own
    translation (Codex review: "Publish the runner slot id")."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["runner_slot_id"] == "1530"
    assert document["runner_slot_id"] in dca_runner.SLOTS


def test_mismatched_case_ticker_key_is_rejected():
    """A differently-cased per-source ticker key ("test" for active "TEST")
    must not silently read as "this source doesn't cover this ticker" -- the
    same benign shape as a genuinely uncovered ticker. With only one source
    affected, the other source's BUY would auto-buy with no independent
    cross-check; with both affected, the run would report a quiet NO_DATA
    instead of the data problem that actually occurred (Codex review:
    "Normalize market-data ticker keys before matching")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"] = {"test": data["sources"]["finance"].pop("TEST")}
    with pytest.raises(ValueError):
        _run(data, _config())


def test_excluded_ticker_key_in_market_data_is_tolerated():
    """Regression guard: the same-universe check above must not reject a
    per-source key for a ticker that is real but currently excluded --
    that is a tolerated no-op (config.tickers's own main-loop `continue`
    skips it before any source lookup), the same tolerance
    _validate_ticker_errors already gives ticker_errors entries."""
    data = _market_data("EXCLUDED", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"]["TEST"] = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 96.0),
    }
    data["sources"]["robinhood"]["TEST"] = data["sources"]["finance"]["TEST"]
    config = _config(tickers=("TEST",), excluded_tickers=frozenset({"EXCLUDED"}))
    candidates = _run(data, config)
    assert [c["ticker"] for c in candidates] == ["TEST"]


def test_stale_lookback_bar_treated_as_incomplete():
    """A fresh decision bar with only a far-stale lookback bar (nothing near
    the 60-minute lookback anchor, 14:30 ET here) must be treated as an
    incomplete attempted payload, the same as a missing lookback bar
    entirely -- otherwise a stale morning bar silently stands in for the
    trend-lookback read and the source scores as if it were fully covered
    (Codex review: "Reject stale lookback bars before scoring")."""
    config = _config(tickers=("TEST",))
    sparse_bars = []
    cur = datetime.combine(DAY, time(9, 30), tzinfo=ET)
    morning_end = datetime.combine(DAY, time(10, 0), tzinfo=ET)
    while cur <= morning_end:
        sparse_bars.append(
            {"timestamp": cur.isoformat(), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0}
        )
        cur += timedelta(minutes=1)
    # Dense coverage right at the 15:30 decision anchor only, so the
    # decision bar itself stays unambiguously fresh regardless of exactly
    # where completed_bar_anchor rounds -- isolating the lookback gap as the
    # only source of staleness being tested here.
    cur = datetime.combine(DAY, time(15, 15), tzinfo=ET)
    decision_end = datetime.combine(DAY, time(15, 30), tzinfo=ET)
    while cur <= decision_end:
        sparse_bars.append(
            {"timestamp": cur.isoformat(), "open": 90.0, "high": 90.0, "low": 90.0, "close": 90.0, "volume": 1000.0}
        )
        cur += timedelta(minutes=1)
    data = {
        "sources": {
            "robinhood": {
                "TEST": {
                    "previous_close_bar": _prev_close_bar(100.0, 97.0),
                    "intraday_bars": sparse_bars,
                }
            }
        }
    }
    candidate = _only(_run(data, config))
    assert candidate["provider_error"] is True
    assert candidate["robinhood_action"] == Action.NO_DATA.value


def test_naive_previous_close_timestamp_is_rejected():
    """A previous_close_bar timestamp with no UTC offset must be rejected
    outright, not silently reinterpreted in the host's local system
    timezone by the later same-day .astimezone(ET) guard -- on a UTC host a
    malformed same-day bar can convert to the prior ET date, pass that
    guard, and its close/low then drive an automatic BUY from evidentially
    ambiguous data (Codex review: "Reject naive previous-close
    timestamps")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    naive_timestamp = datetime.combine(DAY - timedelta(days=1), time(16, 0)).isoformat()
    data["sources"]["finance"]["TEST"]["previous_close_bar"]["timestamp"] = naive_timestamp
    with pytest.raises(ValueError):
        _run(data, _config())


def test_planner_candidates_withheld_on_config_dir_mismatch(tmp_path, monkeypatch):
    """dca_runner's own multiplier/ladder validation (_planner_incompatible)
    reads dca_runner.CONFIG_DIR's sizing ladder, frozen at dca_runner's own
    import time -- it has no parameter to accept a different config
    directory. When --config-dir points elsewhere, that check would
    validate a real BUY's sizing against the WRONG ladder, so
    planner_candidates must be withheld rather than publish a row whose
    compatibility was never actually verified (Codex review: "Use the
    selected config for planner filtering")."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    other_config_dir = tmp_path / "other-config"
    other_config_dir.mkdir()
    _write_populated_config(other_config_dir)
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(other_config_dir),
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["buy_count"] == 1
    assert document["planner_config_mismatch"] is True
    assert document["planner_candidates"] == []
    assert document["planner_incompatible_tickers"] == []
    # The whole compatibility check is untrustworthy under a config-dir
    # mismatch, not just this one row's own multiplier/drop pairing -- the
    # same withholding must extend to same_day_top_up_candidates (Gate 6.5's
    # input), not just planner_candidates (Gate 6's), or a caller could
    # auto-place a same-day top-up for a BUY Gate 6 never verified (Codex
    # review: "Withhold top-up rows that Gate 6 cannot place").
    assert document["same_day_top_up_candidates"] == []


def test_planner_candidates_populated_with_default_config_dir(tmp_path, monkeypatch):
    """Regression guard: the mismatch check above must not also suppress
    planner_candidates on the ordinary, default --config-dir path."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["planner_config_mismatch"] is False
    assert len(document["planner_candidates"]) == 1
    assert document["planner_incompatible_tickers"] == []
    # Regression guard: withholding top-up rows for a mismatch/incompatible
    # ticker (see the tests above) must not also suppress a normal,
    # planner-approved ticker's top-up row.
    assert [row["ticker"] for row in document["same_day_top_up_candidates"]] == ["TEST"]


def test_ticker_errors_wrong_shape_list_is_rejected():
    """"ticker_errors": [] is the wrong top-level shape (must be a {source:
    [ticker]} object), but `ticker_errors or {}` previously replaced it with
    {} before _validate_ticker_errors ever saw it -- an empty list is falsy,
    so the malformed payload silently read as "no per-ticker error
    evidence" instead of failing closed on the schema violation (Codex
    review: "Validate falsy ticker_errors payloads")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    with pytest.raises(ValueError):
        _run(data, _config(), ticker_errors=[])


def test_duplicate_keys_in_market_data_json_are_rejected(tmp_path, monkeypatch):
    """A saved market-data file with a literal duplicate object key (e.g.
    two sources.finance.TEST entries from a malformed merge) must be
    rejected outright -- plain json.loads silently keeps only the LAST
    occurrence, so conflicting provider evidence could be overwritten
    before validation/scoring ever sees the discarded row (Codex review:
    "Reject duplicate keys in market-data JSON")."""
    config = _config(tickers=("TEST",))
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(
        '{"sources": {"finance": {"TEST": {"previous_close_bar": null, "intraday_bars": []}, '
        '"TEST": {"previous_close_bar": null, "intraday_bars": []}}, "robinhood": {}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)
    with pytest.raises(ValueError, match="duplicate key"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )


def test_missing_config_dir_aborts(tmp_path):
    """A mistyped --config-dir, or a mounted repo missing config/, must
    abort rather than silently score buy_count/planner_candidates against
    load_strategy_config's hardcoded StrategyConfig defaults instead of the
    real checked-in budgets/tickers/per-ticker calibration (Codex review:
    "Fail closed when the config directory is unavailable")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    missing_config_dir = tmp_path / "does-not-exist"

    with pytest.raises(ValueError, match="does not exist"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(missing_config_dir),
            ]
        )


def test_ticker_error_marked_malformed_payload_does_not_crash():
    """A ticker already marked in ticker_errors is unconditionally scored
    via _unavailable_signal in the main loop regardless of its raw payload
    -- so a genuinely malformed leftover from the SAME failed fetch that
    produced the error marker (e.g. close: 0) must never reach _parse_bar
    and crash candidate generation for every ticker, instead of producing
    the intended single-ticker provider-error row (Codex review: "Apply
    error markers before parsing errored payloads")."""
    config = _config(tickers=("TEST",))
    malformed_bar = _prev_close_bar(100.0, 97.0)
    malformed_bar["close"] = 0  # non-positive -- _finite_positive_float would raise
    data = {
        "sources": {
            "finance": {"TEST": {"previous_close_bar": malformed_bar, "intraday_bars": []}},
            "robinhood": {
                "TEST": {
                    "previous_close_bar": _prev_close_bar(100.0, 97.0),
                    "intraday_bars": _intraday_bars(100.0, 96.0),
                }
            },
        }
    }
    candidate = _only(_run(data, config, ticker_errors={"finance": ["TEST"]}))
    assert candidate["provider_error"] is True
    assert candidate["finance_action"] == Action.NO_DATA.value


def test_naive_quote_as_of_is_rejected():
    """A quote.as_of with no UTC offset must be rejected outright at parse
    time, not left to crash later as an unhandled TypeError when
    _StaticMarketData.get_quote subtracts it from an aware decision anchor
    (Antigravity review: "Unhandled TypeError on offset-naive quote.as_of
    timestamps")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    naive_as_of = datetime.combine(DAY, time(15, 30)).isoformat()
    data["sources"]["finance"]["TEST"]["quote"] = {"price": 95.50, "as_of": naive_as_of}
    with pytest.raises(ValueError, match="timezone-aware"):
        _run(data, _config())


def test_empty_quote_object_is_rejected():
    """A present-but-empty quote object ({}) must be rejected as malformed,
    not silently treated as though the field were absent -- the same shape
    a caller's `quote = fetch_quote_or_none() or {}` idiom (or a stray JSON
    "quote": {}) could produce. Falling back to the decision-bar close here
    instead of raising would hide the caller's malformed saved-quote
    evidence, even though a quote missing only price ({"as_of": ...} with
    no "price") already raised via the same KeyError path (Codex review:
    "Reject empty quote payloads explicitly")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["robinhood"]["TEST"]["quote"] = {}
    with pytest.raises(ValueError, match="missing required key"):
        _run(data, _config())


def test_non_object_quote_is_rejected():
    """A non-Mapping-but-truthy quote value (a list) must be rejected with a
    descriptive ValueError, not left to crash later as an unhandled
    AttributeError when raw.get(...) is called on it."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["robinhood"]["TEST"]["quote"] = ["invalid"]
    with pytest.raises(ValueError, match="must be an object"):
        _run(data, _config())


def test_non_numeric_market_data_value_raises_value_error():
    """A non-numeric-shaped JSON value (null, a nested list/object) for a
    price field must raise ValueError like every other malformed-input
    rejection in this module, not leak float()'s own TypeError -- a caller
    catching ValueError specifically to handle malformed market-data
    gracefully would otherwise miss this shape of corruption (Antigravity
    review: "Convert TypeError to ValueError in _finite_float")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"]["TEST"]["previous_close_bar"]["close"] = None
    with pytest.raises(ValueError):
        _run(data, _config())


def test_bar_missing_required_key_raises_value_error():
    """A bar object missing a required key entirely (not just present with
    the wrong type) must raise ValueError like every other malformed-input
    rejection in this module, not leak a raw KeyError (Antigravity review:
    "Unhandled KeyError in Bar/Quote Parsing")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    del data["sources"]["finance"]["TEST"]["previous_close_bar"]["open"]
    with pytest.raises(ValueError, match="missing required key"):
        _run(data, _config())


def test_quote_missing_price_key_raises_value_error():
    """A quote object missing "price" entirely must raise ValueError, not a
    raw KeyError (Antigravity review: "Unhandled KeyError in Bar/Quote
    Parsing")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["robinhood"]["TEST"]["quote"] = {"as_of": _iso(15, 30)}
    with pytest.raises(ValueError, match="missing required key"):
        _run(data, _config())


def test_non_mapping_source_payload_is_rejected():
    """A present-but-non-Mapping source payload (a list instead of a
    per-ticker object) must be rejected with a descriptive ValueError, not
    silently skipped and left to crash later as an unhandled AttributeError
    when generate_candidates calls .items() on it (Antigravity review:
    "Non-Mapping Provider Payload Validation")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"] = ["invalid"]
    with pytest.raises(ValueError, match="must be an object"):
        _run(data, _config())


def test_null_source_payload_is_rejected():
    """An explicit JSON null for a source is a malformed shape, not a
    tolerated absence: finance_unavailable/robinhood_unavailable check KEY
    presence ("finance" not in market_data_by_source), not value-is-None,
    so a present "finance": null key still reaches the normal-source code
    path and would otherwise crash on .items() the same as any other
    non-Mapping value."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"] = None
    with pytest.raises(ValueError, match="must be an object"):
        _run(data, _config())


def test_null_ticker_payload_is_rejected():
    """A per-ticker payload of null (sources.finance.TEST: null), rather
    than the whole source, is a malformed shape too -- _validate_market_
    data_ticker_keys only validates that per_ticker's KEYS are known
    tickers, not that each ticker's VALUE is itself an object, so this
    previously reached the plain payload.get(...) call inside
    _StaticMarketData and crashed with an unhandled AttributeError,
    aborting the entire run instead of failing on just this ticker's
    malformed evidence (Codex review: "Validate per-ticker payload objects
    before parsing")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"]["TEST"] = None
    with pytest.raises(ValueError, match="must be an object"):
        _run(data, _config())


def test_list_ticker_payload_is_rejected():
    """A per-ticker payload that is a list (e.g. from a failed serializer),
    rather than an object, must be rejected the same way as null."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"]["TEST"] = ["invalid"]
    with pytest.raises(ValueError, match="must be an object"):
        _run(data, _config())


def test_ticker_error_marked_null_payload_does_not_crash():
    """Regression guard: the new per-ticker payload-shape check above must
    not fire for a ticker already marked in ticker_errors -- generate_
    candidates' own finance_ticker_errors/robinhood_ticker_errors filtering
    removes it from finance_inputs/robinhood_inputs BEFORE _StaticMarketData
    is ever constructed, so a null (or otherwise malformed-shaped) leftover
    payload from the SAME failed fetch must still produce the intended
    single-ticker provider-error row, not a crash."""
    config = _config(tickers=("TEST",))
    data = {
        "sources": {
            "finance": {"TEST": None},
            "robinhood": {
                "TEST": {
                    "previous_close_bar": _prev_close_bar(100.0, 97.0),
                    "intraday_bars": _intraday_bars(100.0, 96.0),
                }
            },
        }
    }
    candidate = _only(_run(data, config, ticker_errors={"finance": ["TEST"]}))
    assert candidate["provider_error"] is True
    assert candidate["finance_action"] == Action.NO_DATA.value


def test_absent_source_key_is_still_tolerated():
    """Regression guard: the stricter non-Mapping/null checks above must not
    also start rejecting a genuinely ABSENT source key -- that is the
    existing, separate "whole source unavailable" shape, handled by
    finance_unavailable/robinhood_unavailable forcing a provider-error row,
    not a validation failure."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    del data["sources"]["finance"]
    candidate = _only(_run(data, _config()))
    assert candidate["provider_error"] is True
    assert candidate["finance_action"] == Action.NO_DATA.value


def test_unknown_market_data_source_key_is_rejected():
    """A mis-cased or otherwise unrecognized top-level 'sources' key (e.g.
    "Finance" alongside the canonical "finance") must be rejected outright.
    The per-source loop only ever reads market_data_by_source[name] for the
    two canonical _VALID_SOURCE_NAMES -- it never enumerates
    market_data_by_source's own keys -- so a stray key's evidence was
    previously dropped silently instead of being scored or rejected,
    letting the surviving canonical source's BUY through with no
    cross-check against the discarded, potentially contradictory data
    (Codex review: "Reject unknown market-data source keys")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["Finance"] = data["sources"]["finance"]
    with pytest.raises(ValueError, match="unknown key"):
        _run(data, _config())


def test_incomplete_config_dir_aborts(tmp_path):
    """An existing --config-dir missing one of the required config files
    (tickers.json, budgets.json, sizing_ladder.json, per_ticker_sizing.json,
    market_calendar.json) must abort just like a missing directory --
    load_strategy_config only
    warns and falls back to hardcoded defaults for the missing piece(s),
    which this live entry point must not silently score against (Codex
    review: "Require config files before scoring")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    incomplete_config_dir = tmp_path / "incomplete-config"
    incomplete_config_dir.mkdir()
    (incomplete_config_dir / "tickers.json").write_text("{}", encoding="utf-8")
    # budgets.json, sizing_ladder.json, per_ticker_sizing.json deliberately absent.

    with pytest.raises(ValueError, match="missing required file"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(incomplete_config_dir),
            ]
        )


def test_excluded_ticker_malformed_payload_does_not_crash():
    """A market-data snapshot that includes one of the configured excluded
    symbols (as a normal full-universe fetch would) must not let a stale or
    malformed bar for that excluded symbol abort scoring for the active
    universe -- excluded tickers are a tolerated no-op, so their payload
    must never reach _parse_bar at all (Codex review: "Drop excluded
    tickers before eager parsing")."""
    config = _config(tickers=("TEST",), excluded_tickers=frozenset({"EXCLUDED"}))
    malformed_bar = _prev_close_bar(100.0, 97.0)
    malformed_bar["close"] = 0  # non-positive -- _finite_positive_float would raise
    excluded_payload = {"previous_close_bar": malformed_bar, "intraday_bars": []}
    active_payload = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 96.0),
    }
    data = {
        "sources": {
            "finance": {"TEST": active_payload, "EXCLUDED": excluded_payload},
            "robinhood": {"TEST": active_payload, "EXCLUDED": excluded_payload},
        }
    }
    candidates = _run(data, config)
    assert [c["ticker"] for c in candidates] == ["TEST"]


def test_generate_candidates_rejects_non_finite_deployed_last_7d_usd():
    """A caller invoking generate_candidates() directly (bypassing the CLI's
    argparse type validation) with a NaN deployed_last_7d_usd on its
    StrategyConfig must still be rejected -- otherwise
    kill_switch_engaged's `deployed_last_7d_usd > threshold` comparison
    silently evaluates False for NaN, disabling the kill switch instead of
    tripping it (Codex review: "Validate helper trailing spend before
    scoring")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    config = _config(deployed_last_7d_usd=float("nan"))
    with pytest.raises(ValueError, match="deployed_last_7d_usd"):
        _run(data, config)


def test_generate_candidates_rejects_non_finite_monthly_cash_allocation():
    """A caller invoking generate_candidates() directly with a NaN
    monthly_cash_allocation_usd on its StrategyConfig must still be
    rejected: kill_switch_engaged's own `allocation <= 0` escape hatch is
    NaN-comparison-blind (NaN <= 0 is False), so a NaN allocation falls
    through to `threshold = fraction * allocation` = NaN, where
    `deployed_last_7d_usd > NaN` is always False regardless of how large
    the real trailing spend is -- silently disabling the kill switch instead
    of failing closed (Codex review: "Validate risk-guard inputs in the
    helper")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    config = _config(monthly_cash_allocation_usd=float("nan"))
    with pytest.raises(ValueError, match="monthly_cash_allocation_usd"):
        _run(data, config)


def test_generate_candidates_rejects_out_of_range_kill_switch_fraction():
    """A caller invoking generate_candidates() directly with a
    kill_switch_7d_deploy_fraction outside [0, 1] must still be rejected --
    dca_config.load_budgets already rejects this at the config-loading
    boundary, but a directly-constructed StrategyConfig bypasses that
    entirely, and kill_switch_engaged's own out-of-range check silently
    disables the guard rather than failing closed on the malformed value
    (Codex review: "Validate risk-guard inputs in the helper")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    config = _config(kill_switch_7d_deploy_fraction=1.5)
    with pytest.raises(ValueError, match="kill_switch_7d_deploy_fraction"):
        _run(data, config)


def test_help_does_not_crash_on_missing_dca_config_dir(tmp_path):
    """dca_runner reads CONFIG_DIR = get_config_dir() and eagerly loads
    sizing_ladder.json at ITS OWN import time -- if this module imported
    dca_runner at module level, a stale/missing $DCA_CONFIG_DIR crashed even
    a plain --help, which needs no config at all, before argparse ever got a
    chance to honor --config-dir (Codex review: "Defer dca_runner import
    until after config selection"). Run in a fresh subprocess: this test
    file's own module-level `import dca_runner` would otherwise mask a
    regression, since the module is already cached by the time any
    in-process test here runs."""
    missing_config_dir = tmp_path / "does-not-exist"
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, DCA_CONFIG_DIR=str(missing_config_dir))
    result = subprocess.run(
        [sys.executable, "-m", "scripts.generate_buy_candidates", "--help"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_config_dir_recovers_from_missing_dca_config_dir_env(tmp_path):
    """A valid --config-dir must let a run succeed even when the ambient
    $DCA_CONFIG_DIR is stale/missing. dca_runner reads CONFIG_DIR =
    get_config_dir() (i.e. $DCA_CONFIG_DIR) at ITS OWN import time with no
    per-call override -- so merely deferring that import past parse_args
    (the --help test above) does not, by itself, let --config-dir recover
    a run: dca_runner would still import using the bad ambient value
    regardless of what --config-dir says (Codex review: "Defer dca_runner
    until after honoring --config-dir"). Run in a fresh subprocess for the
    same reason as the --help test -- this test file's own module-level
    `import dca_runner` would otherwise mask a regression, since the
    module is already cached by the time any in-process test here runs."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "valid-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    missing_dca_config_dir = tmp_path / "does-not-exist"
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, DCA_CONFIG_DIR=str(missing_dca_config_dir))

    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.generate_buy_candidates",
            "--slot", SLOT,
            "--trading-day", DAY.isoformat(),
            "--market-data", str(market_data_path),
            "--deployed-last-7d-usd", "0",
            "--config-dir", str(config_dir),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["buy_count"] == 1
    assert document["planner_config_mismatch"] is False


def test_already_covered_ticker_excluded_from_buy_output(tmp_path, monkeypatch):
    """A ticker a compatible executor already placed an order for in THIS
    SAME window (Gate 4's compatible-stand-down case, live-mirror prompt)
    must be treated exactly like an excluded ticker: skipped before
    scoring, so it never reaches candidates/planner_candidates/
    same_day_top_up_candidates/buy_count at all, and a stale or malformed
    payload for it cannot abort the rest of the run (Codex review: "Exclude
    same-window tickers before scoring"). Echoed back in the
    already_covered_tickers field for traceability."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--already-covered-tickers", "TEST",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["buy_count"] == 0
    assert document["evaluated_count"] == 0
    assert document["candidates"] == []
    assert document["planner_candidates"] == []
    assert document["same_day_top_up_candidates"] == []
    assert document["already_covered_tickers"] == ["TEST"]


def test_already_covered_ticker_with_malformed_payload_does_not_crash():
    """The scenario the fix above specifically targets: a stale/malformed
    payload for an already-covered ticker must not abort scoring for the
    rest of the universe -- it is never parsed at all (Codex review:
    "Exclude same-window tickers before scoring")."""
    config = _config(tickers=("TEST", "COVERED"))
    malformed_bar = _prev_close_bar(100.0, 97.0)
    malformed_bar["close"] = 0  # non-positive -- _finite_positive_float would raise
    covered_payload = {"previous_close_bar": malformed_bar, "intraday_bars": []}
    active_payload = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 96.0),
    }
    data = {
        "sources": {
            "finance": {"TEST": active_payload, "COVERED": covered_payload},
            "robinhood": {"TEST": active_payload, "COVERED": covered_payload},
        }
    }
    candidates = _run(data, config, already_covered_tickers=["COVERED"])
    assert [c["ticker"] for c in candidates] == ["TEST"]


def test_already_covered_tickers_rejects_unrecognized_ticker(tmp_path, monkeypatch):
    """A typo'd/unrecognized --already-covered-tickers entry must fail
    loud, not silently never match anything and let a same-window
    duplicate through uncaught."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError, match="already_covered_tickers"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--already-covered-tickers", "NOTREAL",
            ]
        )


def test_no_already_covered_tickers_is_still_the_default(tmp_path, monkeypatch):
    """Regression guard: the new exclusion machinery above must not affect
    the ordinary path where --already-covered-tickers is omitted entirely."""
    config = _config(tickers=("TEST",))
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(gbc, "load_strategy_config", lambda *_args, **_kwargs: config)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["buy_count"] == 1
    assert len(document["planner_candidates"]) == 1
    assert document["already_covered_tickers"] == []


def test_malformed_finance_quote_does_not_crash():
    """A malformed Finance "quote" field (missing price) must not abort
    scoring -- get_quote() already unconditionally ignores a Finance quote
    (the live FinanceDataProvider has no quote path at all), so that data
    should never even reach _parse_quote in the first place (Codex review:
    "Skip Finance quotes before parsing").

    Builds separate finance/robinhood payload dicts rather than using
    _market_data's default sources=("finance", "robinhood") -- that helper
    shares a single per_ticker dict object across every requested source,
    so mutating data["sources"]["finance"][...]["quote"] in place would
    also mutate robinhood's payload and defeat the point of this test."""
    finance_payload = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 96.0),
        "quote": {"as_of": _iso(15, 30)},  # missing "price"
    }
    robinhood_payload = {
        "previous_close_bar": _prev_close_bar(100.0, 97.0),
        "intraday_bars": _intraday_bars(100.0, 96.0),
    }
    data = {"sources": {"finance": {"TEST": finance_payload}, "robinhood": {"TEST": robinhood_payload}}}
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value


def test_future_quote_timestamp_is_rejected():
    """A quote timestamped AFTER the decision moment must be rejected
    outright, not accepted merely because it is within the staleness
    window by absolute distance -- a 15:34 quote for a 15:30 decision is
    within Robinhood's 5-minute interval by abs() alone, but it is
    signal-time evidence that could not have existed yet at the decision
    moment (Codex review: "Reject near-future quote timestamps")."""
    data = _market_data(
        "TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0, sources=("robinhood",)
    )
    data["sources"]["finance"] = {}
    future_as_of = datetime.combine(DAY, time(15, 34), tzinfo=ET).isoformat()
    data["sources"]["robinhood"]["TEST"]["quote"] = {"price": 95.50, "as_of": future_as_of}
    candidate = _only(_run(data, _config()))
    assert candidate["action"] == Action.BUY.value
    # Rejected quote -> falls back to the decision bar close (96.0), not the future quote price (95.50).
    assert candidate["target_price"] == pytest.approx(96.0)


def test_config_dir_with_empty_files_aborts(tmp_path):
    """A --config-dir with every required FILE present but EMPTY content
    must abort just like a missing file -- load_strategy_config's load_*
    helpers tolerate empty/inactive content the exact same way they
    tolerate a missing file, silently falling back to hardcoded defaults
    (Codex review: "Validate required config contents, not just
    filenames"). match= targets _require_populated_config's own message
    specifically: with every file present-but-empty and no fix, main()
    still raises, but from _validate_market_data_ticker_keys instead (the
    real StrategyConfig() default ticker universe doesn't include "TEST"),
    a coincidental failure this test must not mistake for the real one."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    empty_config_dir = tmp_path / "empty-config"
    empty_config_dir.mkdir()
    for name in gbc._REQUIRED_CONFIG_FILENAMES:
        (empty_config_dir / name).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="has no non-empty 'active' ticker list"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(empty_config_dir),
            ]
        )


def test_config_dir_with_populated_files_succeeds(tmp_path):
    """Regression guard: the stricter content check above must not also
    reject a genuinely populated config dir."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    populated_config_dir = tmp_path / "populated-config"
    populated_config_dir.mkdir()
    _write_populated_config(populated_config_dir)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(populated_config_dir),
            ]
        )
    assert exit_code == 0


def test_errored_source_with_malformed_payload_skips_key_validation():
    """When source_errors already marks a whole source as failed, a
    malformed leftover payload for that source (an unrecognized ticker
    key) must not abort Gate 5 before the marker gets a chance to force
    provider-error rows -- the surviving source's own evidence would
    otherwise never be scored either (Codex review: "Skip errored source
    payload validation")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    data["sources"]["finance"] = {"NOTREAL": {"previous_close_bar": None, "intraday_bars": []}}
    candidate = _only(_run(data, _config(), source_errors=["finance"]))
    assert candidate["action"] == Action.MANUAL_REVIEW.value
    assert candidate["provider_error"] is True
    assert candidate["finance_action"] == Action.NO_DATA.value
    assert candidate["robinhood_action"] == Action.BUY.value


def test_duplicate_keys_in_selected_config_are_rejected(tmp_path):
    """A duplicate JSON key in one of the required config files (e.g. two
    "TEST" calibration entries in per_ticker_sizing.json) must be
    rejected -- dca_config._read_json uses plain json.loads with no
    duplicate-key protection, unlike main()'s own --market-data read and
    dca_runner's own config/*.json reads, neither of which covers this
    SELECTED --config-dir (Codex review: "Reject duplicate keys in
    selected config")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    dup_config_dir = tmp_path / "dup-config"
    dup_config_dir.mkdir()
    _write_populated_config(dup_config_dir)
    (dup_config_dir / "per_ticker_sizing.json").write_text(
        '{"status": "active", "tickers": {"TEST": {"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}, '
        '"TEST": {"tiers": [{"min_drop_pct": 2.0, "multiplier": 2.0}]}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(dup_config_dir),
            ]
        )


def test_config_dir_with_incomplete_budgets_aborts(tmp_path):
    """A budgets.json with an unrelated field present but base_amount_usd/
    base_amount_below_close_usd both absent must abort -- bare truthiness
    (the round-17 check) doesn't prove those specific live-sizing fields
    survived (Codex review: "Require the actual live budget keys")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "incomplete-budgets-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "budgets.json").write_text(json.dumps({"weekly_rollover_reserve_cap_usd": 500.0}), encoding="utf-8")

    with pytest.raises(ValueError, match="base_amount_usd"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_incomplete_calibration_coverage_aborts(tmp_path):
    """A per_ticker_sizing.json calibrating only SOME of tickers.json's
    active tickers must abort -- at-least-one-calibrated-ticker (the
    round-17 check) doesn't prove COVERAGE; the uncalibrated ticker would
    silently fall back to the global ladder via _ticker_sizing() while
    Gate 5 reports the selected calibrated config was accepted (Codex
    review: "Require calibration coverage for active tickers"). The
    validation runs before market-data is ever read, so a minimal
    single-ticker market-data payload is enough here."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "partial-calibration-config"
    config_dir.mkdir()
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["TEST", "OTHER"]}), encoding="utf-8")
    (config_dir / "budgets.json").write_text(
        json.dumps({"base_amount_usd": 2.0, "base_amount_below_close_usd": 1.0}), encoding="utf-8"
    )
    (config_dir / "sizing_ladder.json").write_text(
        json.dumps({"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}), encoding="utf-8"
    )
    # "OTHER" is active per tickers.json above but has no entry here.
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps({"status": "active", "tickers": {"TEST": {"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}}}),
        encoding="utf-8",
    )
    (config_dir / "market_calendar.json").write_text(
        (Path(__file__).resolve().parents[2] / "config" / "market_calendar.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="OTHER"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_nan_monthly_allocation_aborts(tmp_path):
    """A budgets.json risk_guard.monthly_cash_allocation_usd of NaN must
    abort -- kill_switch_engaged()'s own `allocation <= 0` guard does not
    catch NaN (NaN compares False against everything), so it would
    otherwise silently disable the kill switch regardless of trailing
    spend (Codex review: "Reject non-finite risk-guard allocation")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "nan-allocation-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "budgets.json").write_text(
        json.dumps(
            {
                "base_amount_usd": 2.0,
                "base_amount_below_close_usd": 1.0,
                "risk_guard": {"monthly_cash_allocation_usd": float("nan")},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="monthly_cash_allocation_usd"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_duplicate_active_tickers_aborts(tmp_path):
    """A tickers.json 'active' list with a duplicate entry must abort --
    the main loop would otherwise iterate and score that ticker twice,
    potentially publishing duplicate BUY/top-up rows for the same symbol
    (Codex review: "Validate active tickers before scoring")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "dup-ticker-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["TEST", "TEST"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate ticker"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_non_canonical_active_ticker_aborts(tmp_path):
    """A tickers.json 'active' entry that isn't upper-case/stripped must
    abort -- it would silently join known_tickers for every validator in
    this module without ever matching dca_runner's own canonical
    whitelist if --config-dir differs from dca_runner.CONFIG_DIR (Codex
    review: "Validate active tickers before scoring")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "lowercase-ticker-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["test"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="non-canonical"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_non_canonical_excluded_ticker_aborts(tmp_path):
    """A tickers.json 'excluded' entry that isn't upper-case/stripped must
    abort, exactly like a non-canonical 'active' entry above --
    config.excluded_tickers is matched with exact `in` membership
    everywhere it's consulted, so an excluded "test" would never match a
    canonical active "TEST" and the exclusion would silently become a
    no-op: the ticker is scored, and can be bought, despite the config
    author's intent to exclude it (Codex review: "Reject non-canonical
    excluded tickers")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "lowercase-excluded-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "tickers.json").write_text(
        json.dumps({"active": ["TEST"], "excluded": ["test"]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="'excluded' list has non-canonical"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_config_dir_with_oversized_calibration_base_aborts(tmp_path):
    """A per-ticker calibration base whose amount (base x multiplier)
    exceeds dca_runner.MAX_PER_ORDER_USD (the live $10 per-order ceiling)
    must abort -- load_per_ticker_sizing validates SHAPE but has no
    ceiling on the base, so an oversized calibrated base would otherwise
    reach graded_buy_multiplier/select_first_allocation_base_usd unchecked
    (Codex review: "Validate calibration numeric bounds")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "oversized-base-config"
    config_dir.mkdir()
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["TEST"]}), encoding="utf-8")
    (config_dir / "budgets.json").write_text(
        json.dumps({"base_amount_usd": 2.0, "base_amount_below_close_usd": 1.0}), encoding="utf-8"
    )
    (config_dir / "sizing_ladder.json").write_text(
        json.dumps({"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}), encoding="utf-8"
    )
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps(
            {
                "status": "active",
                "tickers": {
                    "TEST": {
                        "tiers": [{"min_drop_pct": 1.0, "multiplier": 5.0}],
                        "base_amount_usd": 1000.0,  # 1000 * 5 = $5000, far above MAX_PER_ORDER_USD
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "market_calendar.json").write_text(
        (Path(__file__).resolve().parents[2] / "config" / "market_calendar.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MAX_PER_ORDER_USD"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_duplicate_intraday_bar_timestamps_are_rejected():
    """Two intraday bars for the same timestamp must be rejected --
    latest_bar_at_or_before (near_close_dca.py) keeps whichever duplicate
    it visits first (strict `>` comparison never lets a later bar replace
    an equal-timestamp earlier one), so a malformed merge could otherwise
    turn a no-buy bar into an automatic BUY (or vice versa) purely from
    array order (Codex review: "Reject duplicate intraday bar
    timestamps")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    duplicate_bar = dict(data["sources"]["finance"]["TEST"]["intraday_bars"][0])
    data["sources"]["finance"]["TEST"]["intraday_bars"] = data["sources"]["finance"]["TEST"][
        "intraday_bars"
    ] + [duplicate_bar]
    with pytest.raises(ValueError, match="duplicate timestamp"):
        _run(data, _config())


def test_inconsistent_ohlc_bar_is_rejected():
    """A bar whose low is above its own open/close is impossible for a
    real bar but passes every individual-field positivity check -- both
    sources could otherwise reconcile to a max-size BUY from evidence
    that could never have actually occurred (Codex review: "Reject
    internally inconsistent OHLC bars")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    inconsistent_bar = dict(data["sources"]["finance"]["TEST"]["intraday_bars"][0])
    inconsistent_bar["low"] = 200.0  # above both this bar's open and close
    data["sources"]["finance"]["TEST"]["intraday_bars"] = [inconsistent_bar]
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        _run(data, _config())


def test_case_colliding_per_ticker_sizing_keys_are_rejected(tmp_path):
    """Two per_ticker_sizing.json ticker keys that both normalize to the
    same upper-case symbol ("TEST" and "test") must be rejected --
    load_per_ticker_sizing's own ticker.upper() would otherwise silently
    collapse them into whichever entry iterates last, discarding the
    other calibration model with no signal anything was dropped (Codex
    review: "Validate raw per-ticker sizing before normalization")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "case-collision-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps(
            {
                "status": "active",
                "tickers": {
                    "TEST": {"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]},
                    "test": {"tiers": [{"min_drop_pct": 2.0, "multiplier": 2.0}]},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="case-colliding"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )


def test_negative_raw_per_ticker_base_is_rejected(tmp_path):
    """A negative raw base_amount_usd in per_ticker_sizing.json must be
    rejected -- load_per_ticker_sizing's max(1.0, ...) floor would
    otherwise silently coerce it into a safe-looking $1.00 with no trace
    of the corruption it papered over (Codex review: "Validate raw
    per-ticker sizing before normalization")."""
    data = _market_data("TEST", early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=97.0)
    market_data_path = tmp_path / "market_data.json"
    market_data_path.write_text(json.dumps(data), encoding="utf-8")
    config_dir = tmp_path / "negative-base-config"
    config_dir.mkdir()
    _write_populated_config(config_dir)
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps(
            {
                "status": "active",
                "tickers": {
                    "TEST": {
                        "tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}],
                        "base_amount_usd": -5.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="finite, positive"):
        gbc.main(
            [
                "--slot", SLOT,
                "--trading-day", DAY.isoformat(),
                "--market-data", str(market_data_path),
                "--deployed-last-7d-usd", "0",
                "--config-dir", str(config_dir),
            ]
        )
