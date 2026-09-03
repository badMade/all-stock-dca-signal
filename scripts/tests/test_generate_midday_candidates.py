#!/usr/bin/env python3
"""Tests for the Gate 5 (1230ET midday) candidate generator.

The midday slot differs from 0935ET in two ways that matter here, and most of
this file is about those two:

1. **Trend validation applies.** ``config/midday_dca.json``'s confidence rule
   auto-buys only when the 60-minute intraday trend can be validated; a
   qualifying drop whose trend cannot be is flagged ``manual_review_candidate``
   and must not reach the auto-placement outputs.
2. **The kill switch is the producer's job.** The 1230ET prompt's Gate 5 states
   that nothing downstream enforces it, so the trailing-7-day deployment figure
   is required and must actually cap the multiplier.

Everything else -- the payload contract, the fail-closed input rules, the
document shape -- is shared with 0935ET and covered by
``test_generate_market_open_candidates.py``; this file does not re-test it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pytest

gmc = pytest.importorskip(
    "scripts.generate_midday_candidates",
    reason="scripts/generate_midday_candidates.py not present",
)

from dca_config import load_strategy_config  # noqa: E402
from midday_dca import MiddayConfig  # noqa: E402
from near_close_dca import Action  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = REPO_ROOT / "config"
DAY = date(2026, 8, 5)
PREVIOUS_SESSION = date(2026, 8, 4)
DECISION_ET = time(12, 30)


def _bar(moment: datetime, price: float, *, low: float | None = None) -> dict:
    low = price if low is None else low
    return {
        "timestamp": moment.isoformat(),
        "open": price,
        "high": price + 0.01,
        "low": low,
        "close": price,
        "volume": 1000.0,
    }


def _previous_close_bar(close: float, low: float) -> dict:
    return _bar(datetime.combine(PREVIOUS_SESSION, time(16, 0), tzinfo=ET), close, low=low)


def _falling_bars(early_price: float, late_price: float, *, start: time = time(9, 30)) -> list:
    """5-minute bars from ``start`` to 12:30 ET, stepping down at noon.

    The 12:30 decision anchor's completed bar is 12:25 (the 12:30 bar has only
    just opened), and the trend window is the 60 minutes before 12:30. Stepping
    down at 12:00 puts the step inside that window, so the window's first bar is
    ``early_price`` and its last completed bar is ``late_price`` -- a genuinely
    falling trend with well over ``trend_min_bars`` (6) usable bars.
    """
    bars = []
    cur = datetime.combine(DAY, start, tzinfo=ET)
    stop = datetime.combine(DAY, DECISION_ET, tzinfo=ET)
    step = datetime.combine(DAY, time(12, 0), tzinfo=ET)
    while cur <= stop:
        bars.append(_bar(cur, late_price if cur >= step else early_price))
        cur += timedelta(minutes=5)
    return bars


def _ticker_payload(
    *,
    early_price: float,
    late_price: float,
    previous_close: float,
    previous_low: float,
    start: time = time(9, 30),
) -> dict:
    return {
        "previous_close_bar": _previous_close_bar(previous_close, previous_low),
        "intraday_bars": _falling_bars(early_price, late_price, start=start),
    }


def _market_data(**per_ticker: dict) -> dict:
    return {
        "sources": {"robinhood": dict(per_ticker)},
        "source_errors": [],
        "ticker_errors": {"robinhood": []},
    }


def _config(**overrides: Any) -> MiddayConfig:
    base: dict[str, Any] = dict(
        tickers=("TEST",),
        excluded_tickers=frozenset(),
        per_ticker_sizing={},
        sizing_tiers=((0.001, 1.0), (0.02, 1.5), (0.04, 2.0), (0.05, 2.5), (0.08, 5.0)),
        minimum_drop_vs_previous_close=0.001,
        base_buy_amount_usd=2.0,
        base_buy_amount_below_close_usd=1.0,
        interval_minutes=5,
        trend_min_bars=6,
        live_execution_enabled=True,
    )
    base.update(overrides)
    return MiddayConfig(**base)


def _run(
    market_data: dict,
    config: MiddayConfig | None = None,
    *,
    deployed_last_7d_usd: float = 0.0,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    check_planner_compatibility: bool = False,
) -> dict:
    return gmc.generate_midday_candidates(
        trading_day=DAY,
        config=_config() if config is None else config,
        market_data=market_data["sources"]["robinhood"],
        deployed_last_7d_usd=deployed_last_7d_usd,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=already_covered_tickers,
        config_dir=REAL_CONFIG_DIR,
        check_planner_compatibility=check_planner_compatibility,
    )


def _only(document: dict, ticker: str = "TEST") -> dict:
    matches = [row for row in document["candidates"] if row["ticker"] == ticker]
    assert len(matches) == 1, f"expected exactly one row for {ticker}, got {matches}"
    return matches[0]


def _kill_switch_threshold_usd() -> float:
    """The account's real 7-day deployment threshold, read from the checked-in
    config rather than hardcoded, so a budgets.json change moves these tests
    with it instead of silently making them assert nothing."""
    config = load_strategy_config(REAL_CONFIG_DIR)
    return config.monthly_cash_allocation_usd * config.kill_switch_7d_deploy_fraction


# --- the signal -------------------------------------------------------------


def test_qualifying_drop_with_a_validated_trend_buys():
    """5% below the prior close and below the prior low, on a falling intraday
    trend -> the $2 base at the 2.5x tier, auto-buy eligible."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    document = _run(data)
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["buy_multiplier"] == pytest.approx(2.5)
    assert row["selected_base_amount_usd"] == pytest.approx(2.0)
    assert row["suggested_amount_usd"] == pytest.approx(5.0)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["trend_validation_exempt"] is False
    assert document["buy_count"] == 1


def test_unvalidated_trend_is_manual_review_not_an_auto_buy():
    """The midday slot's confidence rule: price and drop qualify, but too few
    intraday bars resolve over the 60-minute window to validate the trend. That
    is a manual_review_candidate -- it must stay visible in `candidates` and
    manual_review_tickers while being kept out of every auto-placement output.

    This is the behavior 0935ET does NOT have (it is trend-exempt), and the
    reason a truncated midday fetch is dangerous: it does not error, it quietly
    moves real buys into manual review.
    """
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0,
            late_price=95.0,
            previous_close=100.0,
            previous_low=97.0,
            # Bars only from 12:15, so the 11:30-12:30 window holds 3 usable
            # bars, under the configured trend_min_bars of 6.
            start=time(12, 15),
        )
    )
    document = _run(data)
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["execution_status"] == "manual_review_candidate"
    assert row["trend_validated"] is False
    assert document["buy_count"] == 0
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []
    assert document["manual_review_tickers"] == ["TEST"]


def test_flat_tape_is_a_quiet_tape_not_an_error():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=100.0, late_price=100.0, previous_close=100.0, previous_low=99.0
        )
    )
    document = _run(data)
    assert document["buy_count"] == 0
    assert _only(document)["action"] == Action.SKIP.value


# --- the kill switch --------------------------------------------------------


def test_kill_switch_caps_the_multiplier_when_deployment_is_over_threshold():
    """The 1230ET producer contract's requirement: trailing-7-day deployment past
    the configured fraction of the monthly allocation caps every positive
    multiplier at the ladder minimum, even for a drop that would otherwise reach
    a higher tier. The cap fires inside near_close_dca.graded_buy_multiplier
    itself -- this module hands it the real inputs rather than recomputing a
    capped multiplier of its own.
    """
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=92.0, previous_close=100.0, previous_low=97.0
        )
    )
    over = _kill_switch_threshold_usd() * 2

    uncapped = _run(data, deployed_last_7d_usd=0.0)
    capped = _run(data, deployed_last_7d_usd=over)

    assert _only(uncapped)["buy_multiplier"] == pytest.approx(5.0)
    assert _only(capped)["buy_multiplier"] == pytest.approx(1.0)
    assert _only(capped)["suggested_amount_usd"] == pytest.approx(2.0)
    assert capped["kill_switch_engaged"] is True
    assert uncapped["kill_switch_engaged"] is False
    assert capped["kill_switch_evaluated"] is True
    assert capped["deployed_last_7d_usd"] == pytest.approx(over)


def test_deployment_below_threshold_does_not_cap():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=92.0, previous_close=100.0, previous_low=97.0
        )
    )
    under = _kill_switch_threshold_usd() / 2
    assert _only(_run(data, deployed_last_7d_usd=under))["buy_multiplier"] == pytest.approx(5.0)


def test_missing_deployment_figure_is_refused():
    """No default: silently assuming zero trailing deployment would disable the
    guard on exactly the day it exists to catch."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    with pytest.raises(ValueError, match="deployed_last_7d_usd"):
        _run(data, deployed_last_7d_usd=None)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1.0])
def test_public_helper_rejects_a_non_finite_or_negative_deployment_figure(bad_value):
    """The CLI's argparse type is not the only entry path: this helper is public
    and directly importable, and a NaN or negative figure compares False against
    every kill-switch threshold -- disabling the guard rather than tripping it,
    while looking like the value was supplied (Codex review: "Validate deployment
    values in the public helper")."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    with pytest.raises(ValueError, match="finite, non-negative"):
        _run(data, deployed_last_7d_usd=bad_value)


def test_the_advertised_config_dir_default_is_usable():
    """config_dir=None is the documented default on every public entry path, but
    load_strategy_config's first act is Path(config_dir), which raises TypeError
    on None before any ticker is evaluated. The CLI always passes a path, so only
    a direct caller taking the advertised default hit it (Codex review: "Make the
    optional config directory usable")."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    document = gmc.generate_midday_candidates(
        trading_day=DAY,
        config=_config(),
        market_data=data["sources"]["robinhood"],
        deployed_last_7d_usd=0.0,
        check_planner_compatibility=False,
    )
    assert document["buy_count"] == 1


def test_cli_requires_the_deployment_figure():
    """argparse must refuse the run outright rather than defaulting it."""
    with pytest.raises(SystemExit):
        gmc.main(["--trading-day", DAY.isoformat(), "--market-data", "unused.json"])


# --- the cands.json producer contract ---------------------------------------


def test_cands_rows_carry_every_field_the_prompt_requires():
    """The 1230ET prompt's Gate 5 requires prior_close, prior_low and
    selected_base_amount_usd alongside the amount: its final pre-placement quote
    refresh re-checks "still below prior close" AND re-derives the dynamic
    $1-vs-$2 base, and has no other source for either. A row missing them cannot
    be revalidated at Gate 7."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    (row,) = _run(data)["cands"]
    assert row["ticker"] == "TEST"
    assert row["desired_usd"] == pytest.approx(5.0)
    assert row["current_price"] == pytest.approx(95.0)
    assert row["prior_close"] == pytest.approx(100.0)
    assert row["prior_low"] == pytest.approx(97.0)
    assert row["selected_base_amount_usd"] == pytest.approx(2.0)
    assert row["multiplier"] == pytest.approx(2.5)
    assert row["drop_pct"] == pytest.approx(5.0)


def test_uncalibrated_candidate_omits_the_calibration_fields():
    """sizing_base_usd/sizing_min_drop_pct/sizing_max_drop_pct are required only
    for a calibrated candidate. Emitting them for an uncalibrated one would
    publish calibration evidence the downstream validator would reject."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    (row,) = _run(data)["cands"]
    assert "sizing_base_usd" not in row
    assert "sizing_min_drop_pct" not in row
    assert "sizing_max_drop_pct" not in row


def test_calibrated_candidate_fields_match_the_downstream_validator():
    """For a calibrated ticker the fields must equal what dca_runner's own
    _configured_calibration_bounds reports -- they are read through that exact
    function rather than re-derived from per_ticker_sizing.json, so a row this
    module publishes as calibrated is one plan_orders() will accept."""
    import dca_runner
    from scripts.stream_candidates import _calibration_fields

    sizing = json.loads((REAL_CONFIG_DIR / "per_ticker_sizing.json").read_text(encoding="utf-8"))
    ticker = next(
        t
        for t, model in sizing["tickers"].items()
        if t not in sizing.get("fallback_tickers", ()) and len(model["tiers"]) >= 2
    )
    model = sizing["tickers"][ticker]
    tier_index = 0
    multiplier = float(model["tiers"][tier_index]["multiplier"])
    base = float(model["base_amount_usd"])

    fields = _calibration_fields(ticker, multiplier, base)
    expected_bases, expected_min, expected_max, _ = dca_runner._configured_calibration_bounds(
        ticker, multiplier
    )
    assert base in expected_bases
    assert fields == {
        "sizing_base_usd": base,
        "sizing_min_drop_pct": expected_min,
        "sizing_max_drop_pct": expected_max,
    }


def test_calibration_fields_are_withheld_when_the_base_does_not_match():
    """A base that is not one of the ticker's configured bases would fail the
    downstream check anyway; publishing fields that fail validation is worse than
    publishing none, so the row is treated as uncalibrated instead."""
    from scripts.stream_candidates import _calibration_fields

    sizing = json.loads((REAL_CONFIG_DIR / "per_ticker_sizing.json").read_text(encoding="utf-8"))
    ticker = next(t for t in sizing["tickers"] if t not in sizing.get("fallback_tickers", ()))
    multiplier = float(sizing["tickers"][ticker]["tiers"][0]["multiplier"])
    assert _calibration_fields(ticker, multiplier, 999.0) == {}


# --- identity and the Gate 6.5 handoff --------------------------------------


def test_document_identifies_the_midday_slot():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    document = _run(data)
    assert document["slot"] == "1230ET"
    assert document["runner_slot_id"] == "1230"
    assert document["stream"] == "midday"
    assert document["cron_id"] == "a6239dae"
    assert document["decision_time_et"] == "12:30"
    assert document["source"] == "robinhood"


def test_live_execution_enabled_is_reported_but_does_not_suppress_candidates():
    """Midday ships live_execution_enabled=true, but that gates placement inside
    MiddayDCAExecutor.run(), which this module never calls. The reported value
    must not change what Gate 5 produces either way."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    enabled = _run(data, _config(live_execution_enabled=True))
    disabled = _run(data, _config(live_execution_enabled=False))
    assert enabled["live_execution_enabled"] is True
    assert disabled["live_execution_enabled"] is False
    assert enabled["buy_count"] == disabled["buy_count"] == 1
    assert enabled["cands"] == disabled["cands"]


def test_top_up_rows_carry_the_shape_the_top_up_gate_requires():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    rows = _run(data)["same_day_top_up_candidates"]
    assert rows == [{"ticker": "TEST", "desired_usd": pytest.approx(5.0), "current_price": pytest.approx(95.0)}]


def test_provider_error_is_recorded_and_never_bought():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    document = _run(data, ticker_errors=("TEST",))
    row = _only(document)
    assert row["action"] == Action.NO_DATA.value
    assert row["provider_error"] is True
    assert document["buy_count"] == 0


def test_source_error_aborts_instead_of_emitting_a_quiet_tape():
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    with pytest.raises(ValueError, match="source_errors"):
        _run(data, source_errors=("robinhood",))


# --- the CLI ----------------------------------------------------------------


def _patch_config(monkeypatch, config: MiddayConfig | None = None) -> None:
    """Point the CLI at a hermetic one-ticker config.

    The loader is a field on the module's frozen StreamSpec, not a module global
    the shared core looks up by name, so swap the SPEC (which main() resolves at
    call time) rather than patching a loader attribute nothing reads.
    """
    resolved = _config() if config is None else config
    monkeypatch.setattr(
        gmc,
        "MIDDAY_SPEC",
        dataclasses.replace(gmc.MIDDAY_SPEC, config_loader=lambda *_a, **_kw: resolved),
    )


def test_cli_end_to_end(tmp_path, monkeypatch):
    path = tmp_path / "market_data_1230ET.json"
    path.write_text(
        json.dumps(
            _market_data(
                TEST=_ticker_payload(
                    early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
                )
            )
        ),
        encoding="utf-8",
    )
    _patch_config(monkeypatch)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gmc.main(
            [
                "--trading-day", DAY.isoformat(),
                "--market-data", str(path),
                "--config-dir", str(REAL_CONFIG_DIR),
                "--deployed-last-7d-usd", "0",
            ]
        )
    assert exit_code == 0
    document = json.loads(stdout.getvalue())
    assert document["slot"] == "1230ET"
    assert document["buy_count"] == 1
    assert document["cands"][0]["prior_close"] == pytest.approx(100.0)
    assert document["kill_switch_evaluated"] is True


@pytest.mark.parametrize("raw_value", ["-1", "NaN", "not-a-number"])
def test_cli_rejects_a_non_finite_or_negative_deployment_figure(tmp_path, monkeypatch, raw_value):
    """A NaN or negative figure compares False against every kill-switch
    threshold check, silently disabling the guard instead of tripping it."""
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(
            _market_data(
                TEST=_ticker_payload(
                    early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
                )
            )
        ),
        encoding="utf-8",
    )
    _patch_config(monkeypatch)
    with pytest.raises(SystemExit):
        gmc.main(
            [
                "--trading-day", DAY.isoformat(),
                "--market-data", str(path),
                "--config-dir", str(REAL_CONFIG_DIR),
                "--deployed-last-7d-usd", raw_value,
            ]
        )


def test_cli_aborts_on_a_config_dir_missing_the_stream_config(tmp_path):
    """Without config/midday_dca.json, load_midday_config silently returns
    MiddayConfig() defaults -- this slot's caps and policy replaced by hardcoded
    ones, with only a log line to say so."""
    incomplete = tmp_path / "config"
    incomplete.mkdir()
    for name in gmc._REQUIRED_CONFIG_FILENAMES:
        if name != "midday_dca.json":
            (incomplete / name).write_text(
                (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(
            _market_data(
                TEST=_ticker_payload(
                    early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
                )
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="midday_dca.json"):
        gmc.main(
            [
                "--trading-day", DAY.isoformat(),
                "--market-data", str(path),
                "--config-dir", str(incomplete),
                "--deployed-last-7d-usd", "0",
            ]
        )


# --- the trend WINDOW, not just the trend bar count -------------------------
#
# `_trend_validated` asks whether `trend_min_bars` usable bars sit in the 60
# minutes before the anchor and whether the last close is below the first. It
# does NOT ask whether those bars SPAN that window, so a payload carrying only
# the final 25 minutes grades a real downtrend on a quarter of the required
# evidence and still returns trend_validated=True (Codex review: "Require the
# full midday trend window before auto-buying"). That executor rule is shared by
# every trend-validating stream and is deliberately left alone; what these tests
# pin is that the entry point refuses to PUBLISH such a row.


def _bars_between(start: time, stop: time, *, early_price: float, late_price: float,
                  step_at: time) -> list:
    bars = []
    cur = datetime.combine(DAY, start, tzinfo=ET)
    end = datetime.combine(DAY, stop, tzinfo=ET)
    step = datetime.combine(DAY, step_at, tzinfo=ET)
    while cur <= end:
        bars.append(_bar(cur, late_price if cur >= step else early_price))
        cur += timedelta(minutes=5)
    return bars


def _payload_between(start: time, stop: time = DECISION_ET, *, step_at: time) -> dict:
    return {
        "previous_close_bar": _previous_close_bar(100.0, 97.0),
        "intraday_bars": _bars_between(
            start, stop, early_price=99.0, late_price=95.0, step_at=step_at
        ),
    }


def test_a_truncated_trend_window_is_withheld_from_the_auto_placement_outputs():
    """The exact reproduction: six declining 5-minute bars covering 12:00-12:25
    satisfy trend_min_bars (6) and the falling-close test for the 12:30 anchor,
    so the executor marks the row auto_buy_eligible at full 2.5x size -- on 25
    minutes of a required 60-minute window. It must not reach `cands`."""
    data = _market_data(TEST=_payload_between(time(12, 0), step_at=time(12, 5)))
    document = _run(data)
    row = _only(document)

    # The executor's own verdict is reported UNCHANGED -- this entry point does
    # not rewrite what the real signal path decided, it declines to publish it.
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["buy_count"] == 1

    assert document["withheld_incomplete_trend_window_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []
    # Not a manual-review candidate: the fix for this row is a wider re-fetch,
    # not a human judgement call, and conflating them would send it to the wrong
    # queue in Gate 9.
    assert document["manual_review_tickers"] == []


def test_a_full_trend_window_still_publishes():
    """The control. Same signal, same ladder, bars reaching back past 11:30."""
    data = _market_data(TEST=_payload_between(time(9, 30), step_at=time(12, 0)))
    document = _run(data)
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]
    assert document["cands"][0]["desired_usd"] == pytest.approx(5.0)


def test_the_withholding_is_per_ticker_not_a_whole_run_abort():
    """A thinly-traded symbol whose bars start late must not take the rest of
    the universe down with it -- unlike the payload-completeness check, where an
    absent bar list is unambiguously a broken fetch and does abort."""
    config = _config(tickers=("TRUNC", "FULL"))
    data = _market_data(
        TRUNC=_payload_between(time(12, 0), step_at=time(12, 5)),
        FULL=_payload_between(time(9, 30), step_at=time(12, 0)),
    )
    document = _run(data, config)
    assert document["withheld_incomplete_trend_window_tickers"] == ["TRUNC"]
    assert [c["ticker"] for c in document["cands"]] == ["FULL"]


def test_one_interval_of_tolerance_at_the_window_start():
    """A provider that stamps bars at interval END puts a complete window's
    first bar one interval late (11:35 for a 11:30-12:30 window). That is a
    complete payload and must still publish; 11:40 is not and must not."""
    inside = _run(_market_data(TEST=_payload_between(time(11, 35), step_at=time(12, 0))))
    assert inside["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in inside["cands"]] == ["TEST"]

    outside = _run(_market_data(TEST=_payload_between(time(11, 40), step_at=time(12, 0))))
    assert outside["withheld_incomplete_trend_window_tickers"] == ["TEST"]
    assert outside["cands"] == []


def test_trend_window_matches_the_executors_own():
    """Pins `stream_candidates.TREND_LOOKBACK_MINUTES` to the window
    `MorningDCAExecutor._trend_validated` actually uses, behaviourally rather
    than by reading the literal: with `trend_min_bars` set to exactly the number
    of 5-minute bars a full window holds, dropping the window's FIRST bar must
    flip validation off. If the executor's 60 ever moves, this fails instead of
    silently letting the two drift."""
    from scripts.stream_candidates import TREND_LOOKBACK_MINUTES

    anchor = datetime.combine(DAY, DECISION_ET, tzinfo=ET)
    window_start = (anchor - timedelta(minutes=TREND_LOOKBACK_MINUTES)).timetz()
    # completed_bar_anchor(12:30, 5) is 12:25, so a full window is 11:30..12:25.
    full_window_bars = TREND_LOOKBACK_MINUTES // 5
    config = _config(trend_min_bars=full_window_bars)

    spans = _run(
        _market_data(TEST=_payload_between(window_start, step_at=time(12, 0))), config
    )
    assert _only(spans)["trend_validated"] is True

    one_bar_short = _run(
        _market_data(
            TEST=_payload_between(
                (anchor - timedelta(minutes=TREND_LOOKBACK_MINUTES - 5)).timetz(),
                step_at=time(12, 0),
            )
        ),
        config,
    )
    assert _only(one_bar_short)["trend_validated"] is False


# --- the kill-switch figure is a dollar amount, not a truthy value ----------


@pytest.mark.parametrize("bad", [True, False])
def test_a_boolean_deployment_figure_is_rejected(bad):
    """Python makes bool a subclass of int, so math.isfinite(True) is True and
    True >= 0 is True: a mistyped deployed_last_7d_usd=True was read as $1.00 of
    trailing deployment. $1 leaves the kill switch DISENGAGED, so malformed
    evidence produced uncapped candidates while the document serialized the
    figure back as the JSON literal `true` and still claimed
    kill_switch_evaluated: true (Codex review: "Reject boolean deployment values
    in the public helper"). Reproduced before fixing."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    with pytest.raises(ValueError, match="boolean"):
        _run(data, deployed_last_7d_usd=bad)


@pytest.mark.parametrize("bad", ["850.00", None, object()])
def test_a_non_numeric_deployment_figure_is_rejected(bad):
    """Including the string a caller might pass straight from a config file.
    None is rejected here specifically because this slot REQUIRES the figure --
    the required-ness check fires first, with its own message."""
    data = _market_data(
        TEST=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        )
    )
    with pytest.raises(ValueError):
        _run(data, deployed_last_7d_usd=bad)


# --- allocation priority ----------------------------------------------------


def test_cands_are_published_in_allocation_priority_order():
    """`cands` and `same_day_top_up_candidates` are consumed IN ORDER by a Gate 6
    that stops at the first cap boundary, so publishing them in
    config/tickers.json order let a shallow candidate claim headroom a deeper one
    needed (Codex review: "Route market-open candidates through the canonical
    planner"). config/budgets.json's allocation_priority is higher multiplier
    first, then larger drop, then ticker alphabetically.

    AAAA is listed first in the config and has the SHALLOWEST drop, so config
    order and priority order disagree -- which is the whole point of the test.
    """
    config = _config(tickers=("AAAA", "BBBB", "CCCC"))
    data = _market_data(
        # 0.5% below the close -> 1x
        AAAA=_ticker_payload(
            early_price=99.9, late_price=99.5, previous_close=100.0, previous_low=99.6
        ),
        # 5% below -> 2.5x
        BBBB=_ticker_payload(
            early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
        ),
        # 2.5% below -> 1.5x
        CCCC=_ticker_payload(
            early_price=99.0, late_price=97.5, previous_close=100.0, previous_low=98.0
        ),
    )
    document = _run(data, config)

    ordered = [c["ticker"] for c in document["cands"]]
    assert ordered == ["BBBB", "CCCC", "AAAA"]
    assert [c["ticker"] for c in document["same_day_top_up_candidates"]] == ordered
    multipliers = [c["multiplier"] for c in document["cands"]]
    assert multipliers == sorted(multipliers, reverse=True)

    # `candidates` keeps EVALUATION order, not priority order: it is the parity
    # record of what the executor returned, and reordering it would make the
    # document disagree with the executor it mirrors.
    assert [row["ticker"] for row in document["candidates"]] == ["AAAA", "BBBB", "CCCC"]


def test_allocation_priority_breaks_ties_on_drop_then_ticker():
    """Equal multipliers fall through to the larger drop, then alphabetically --
    the deterministic tie-break config/budgets.json specifies, so two runs on the
    same data never disagree about which candidate gets scarce headroom."""
    config = _config(tickers=("ZZZZ", "MMMM"))
    data = _market_data(
        # Both land in the same 1x tier (0.1%-1.0%); MMMM is the deeper drop.
        ZZZZ=_ticker_payload(
            early_price=99.9, late_price=99.6, previous_close=100.0, previous_low=99.7
        ),
        MMMM=_ticker_payload(
            early_price=99.9, late_price=99.2, previous_close=100.0, previous_low=99.3
        ),
    )
    document = _run(data, config)
    assert {c["multiplier"] for c in document["cands"]} == {1.0}
    assert [c["ticker"] for c in document["cands"]] == ["MMMM", "ZZZZ"]


# --- a stream config's required blocks must have CONTENT, not just exist ----


def _stream_config_cli_text(tmp_path, stream_config_text: str):
    """Same subprocess harness the malformed/hollow/wrong-stream tests use: a
    tmp config dir with $DCA_CONFIG_DIR pointed at it, so dca_runner imports from
    the same directory --config-dir names."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in gmc._REQUIRED_CONFIG_FILENAMES:
        (config_dir / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (config_dir / "midday_dca.json").write_text(stream_config_text, encoding="utf-8")
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
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(
            _market_data(
                TEST=_ticker_payload(
                    early_price=99.0, late_price=95.0, previous_close=100.0, previous_low=97.0
                )
            )
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable, "-m", "scripts.generate_midday_candidates",
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(config_dir),
            "--deployed-last-7d-usd", "0",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "DCA_CONFIG_DIR": str(config_dir)},
        capture_output=True,
        text=True,
    )


def test_cli_rejects_present_but_empty_policy_blocks(tmp_path):
    """Codex's exact example: all four required keys present, every one hollow.
    The loader reads nothing out of the empty objects and keeps its hardcoded
    dataclass defaults, so this exited 0 and published a real candidate sized at
    1x/$1.00 where the checked-in config gives 2.5x/$5.00 -- fallback policy
    wearing the checked-in config's name (Codex review: "Validate nested stream
    policies before loading"; reproduced before fixing)."""
    result = _stream_config_cli_text(
        tmp_path,
        json.dumps(
            {"stream": "midday", "live_task": {}, "sizing_ladder": {}, "budget_ledger": {}}
        ),
    )
    assert result.returncode != 0
    assert "empty required policy block(s)" in result.stderr
    assert result.stdout.strip() == ""


def test_cli_rejects_a_half_specified_sizing_ladder(tmp_path):
    """`base amount x ladder multiplier` has two halves. A ladder supplying only
    one takes the other from defaults -- the same wrong-policy sizing reached by
    a partial edit rather than an emptied file."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["sizing_ladder"] = {"base_amount_usd": 2.0}
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert "'sizing_ladder' is missing or empty for ['tiers']" in result.stderr


def test_cli_rejects_a_budget_ledger_with_no_caps(tmp_path):
    """The exact key this stream's loader reads, derived from the stream name --
    every stream spells its caps `<stream>_daily_cap_usd`, so this needs no
    per-stream table and cannot go stale for a stream it has not learned about."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["budget_ledger"] = {"namespace": "midday"}
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert "midday_daily_cap_usd" in result.stderr


def test_cli_rejects_another_streams_cap_keys(tmp_path):
    """Suffix matching was too loose in the one direction that matters: a
    midday_dca.json whose ledger carries only market_open_* caps -- a partially
    copied or damaged file -- satisfied `*_daily_cap_usd` while
    load_midday_config() ignored those keys entirely and kept its hardcoded
    defaults, so the validation claimed the checked-in money policy had been
    applied while the caps bounding a real batch were the fallback ones. The
    `stream` identity check does not cover it: the file still declares "midday"
    (Codex review: "Honor each stream's configured cap keys"; reproduced before
    fixing)."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["budget_ledger"] = {
        "namespace": "midday",
        "market_open_daily_cap_usd": 180.0,
        "market_open_weekly_cap_usd": 900.0,
    }
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert "midday_daily_cap_usd" in result.stderr
    # The message names what WAS there, so the operator can see the mix-up
    # rather than just being told something is missing.
    assert "market_open_daily_cap_usd" in result.stderr


def test_cli_rejects_a_null_cap_value(tmp_path):
    """Present-with-a-null is the same silent fallback as absent."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["budget_ledger"]["midday_weekly_cap_usd"] = None
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert "midday_weekly_cap_usd" in result.stderr


def test_the_real_checked_in_config_passes_every_nested_check(tmp_path):
    """The control, and the guard against a check that is merely strict: the
    actual config/midday_dca.json must still produce a document."""
    result = _stream_config_cli_text(
        tmp_path, (REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8")
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["slot"] == "1230ET"


@pytest.mark.parametrize(
    "filename,stream",
    [
        ("market_open_dca.json", "market_open"),
        ("morning_dca.json", "morning"),
        ("intraday_dca.json", "intraday"),
        ("midday_dca.json", "midday"),
    ],
)
def test_every_checked_in_stream_config_satisfies_the_nested_checks(filename, stream):
    """Every slot's real config, not just midday's -- a nested check tightened
    for one stream must not quietly fail another's checked-in policy."""
    from scripts.stream_candidates import _require_parseable_stream_config

    _require_parseable_stream_config(REAL_CONFIG_DIR / filename, stream)


# --- the withholding must hold on EVERY output, and the probe must be bounded


def _real_universe_config(**overrides: Any) -> MiddayConfig:
    """A config built from the REAL checked-in ladder and a real universe ticker,
    so `check_planner_compatibility=True` actually publishes a planner row.
    With a synthetic TEST ticker every planner row is withheld as incompatible,
    which masks exactly the leak these tests are about."""
    real = load_strategy_config(REAL_CONFIG_DIR)
    ticker = real.tickers[0]
    base: dict[str, Any] = dict(
        tickers=(ticker,),
        excluded_tickers=frozenset(),
        per_ticker_sizing={k: v for k, v in real.per_ticker_sizing.items() if k == ticker},
        sizing_tiers=real.sizing_tiers,
        minimum_drop_vs_previous_close=real.minimum_drop_vs_previous_close,
        base_buy_amount_usd=real.base_buy_amount_usd,
        base_buy_amount_below_close_usd=real.base_buy_amount_below_close_usd,
        interval_minutes=5,
        trend_min_bars=6,
    )
    base.update(overrides)
    return MiddayConfig(**base)


def _run_planner(bars: list, config: MiddayConfig) -> dict:
    return gmc.generate_midday_candidates(
        trading_day=DAY,
        config=config,
        market_data={
            config.tickers[0]: {
                "previous_close_bar": _previous_close_bar(100.0, 97.0),
                "intraday_bars": bars,
            }
        },
        deployed_last_7d_usd=0.0,
        config_dir=REAL_CONFIG_DIR,
        # The documented convenience path -- the one the leak reached through.
        check_planner_compatibility=True,
    )


def test_withheld_rows_never_reach_planner_candidates():
    """planner_candidates is the documented input for a caller driving
    dca_runner.plan_orders(). Building it from buy_rows instead of placeable_rows
    let a row withheld from `cands` for a truncated trend window reappear there,
    so that caller could place the exact real-money order the withholding
    suppressed -- the check holding on one output and leaking through another
    (Codex review: "Exclude withheld rows from planner candidates"; reproduced
    before fixing)."""
    config = _real_universe_config()
    ticker = config.tickers[0]
    document = _run_planner(
        _bars_between(time(12, 0), DECISION_ET, early_price=99.0, late_price=95.0, step_at=time(12, 5)), config
    )
    assert document["withheld_incomplete_trend_window_tickers"] == [ticker]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []
    assert document["planner_candidates"] == []


def test_a_full_window_row_does_reach_planner_candidates():
    """The control, and the guard against a fix that simply empties the list:
    the same ticker on a full window must still be planner-placeable."""
    config = _real_universe_config()
    document = _run_planner(_bars_between(time(9, 30), DECISION_ET, early_price=99.0, late_price=95.0, step_at=time(12, 0)), config)
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == [config.tickers[0]]
    assert [row[0] for row in document["planner_candidates"]] == [config.tickers[0]]


def test_a_bar_older_than_the_window_does_not_prove_coverage():
    """The coverage probe originally fetched from datetime.min and took the
    minimum timestamp across everything the payload carried, so ONE bar older
    than the window -- a prior-session intraday bar, a stale cached row -- made
    it report full coverage while the in-window evidence was still only the last
    25 minutes. The truncated signal then published at full size: the very
    failure the probe exists to stop, reintroduced through its own lookup (Codex
    review: "Restrict the coverage probe to the trend window"; reproduced before
    fixing)."""
    config = _real_universe_config()
    ticker = config.tickers[0]
    prior_session_bar = _bar(
        datetime.combine(PREVIOUS_SESSION, time(10, 0), tzinfo=ET), 99.0
    )
    document = _run_planner(
        [prior_session_bar] + _bars_between(time(12, 0), DECISION_ET, early_price=99.0, late_price=95.0, step_at=time(12, 5)), config
    )
    assert document["withheld_incomplete_trend_window_tickers"] == [ticker]
    assert document["cands"] == []
    assert document["planner_candidates"] == []


def test_bars_before_the_window_still_count_as_reaching_back():
    """The counterpart: a payload that legitimately reaches back BEFORE the
    window start is complete, not suspicious. Bounding the probe to
    [window_start, anchor] must not turn full coverage into a false withhold --
    the in-window bar at 11:30 is what proves the fetch went back far enough."""
    config = _real_universe_config()
    document = _run_planner(_bars_between(time(9, 30), DECISION_ET, early_price=99.0, late_price=95.0, step_at=time(12, 0)), config)
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == [config.tickers[0]]


# --- config policy numbers must be NUMBERS -----------------------------------


@pytest.mark.parametrize(
    "path,value,expected",
    [
        (("sizing_ladder", "base_amount_usd"), True, "boolean"),
        (("sizing_ladder", "base_amount_usd"), "2.00", "finite number"),
        (("sizing_ladder", "base_amount_below_close_usd"), True, "boolean"),
        (("budget_ledger", "midday_daily_cap_usd"), True, "boolean"),
        (("budget_ledger", "midday_weekly_cap_usd"), True, "boolean"),
    ],
)
def test_cli_rejects_malformed_policy_numbers(tmp_path, path, value, expected):
    """bool subclasses int, so a JSON `true` satisfies every truthiness and
    numeric test and then converts through float(...) to 1.0. In the ladder that
    silently halves the checked-in $2.00 base; in the ledger it replaces a $180
    cap with $1.00. The earlier revision of this validator checked truthiness
    only, so `"base_amount_usd": true` passed and every full-base candidate
    published at half size with nothing reporting the substitution (Codex review:
    "Reject boolean values in the stream sizing ladder"; reproduced loading $1.00
    where the checked-in value is $2.00)."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    cursor = real
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert expected in result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "tiers,expected",
    [
        ([{"min_drop_pct": True, "multiplier": 2.0}], "boolean"),
        ([{"multiplier": 2.0}], "no 'min_drop_pct'"),
        ([{"min_drop_pct": 1.0}], "no 'multiplier'"),
        (["not-an-object"], "must be an object"),
        ({"not": "a list"}, "must be a list"),
    ],
)
def test_cli_rejects_a_malformed_ladder_tier(tmp_path, tiers, expected):
    """The tiers decide the multiplier half of `base x multiplier`, so a
    malformed rung substitutes policy exactly like a malformed base does."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["sizing_ladder"]["tiers"] = tiers
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert expected in result.stderr


def test_a_zero_minimum_drop_floor_is_still_accepted(tmp_path):
    """The guard against over-tightening: `no_buy_below_pct: 0` is a meaningful
    policy (buy on any drop), unlike a zero base amount, which is an order for
    nothing. Only the latter is refused."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["sizing_ladder"]["no_buy_below_pct"] = 0
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, "6", 6.0, None],
    ids=["true", "false", "zero", "negative", "string", "float", "null"],
)
def test_cli_rejects_a_malformed_trend_bar_minimum(tmp_path, value):
    """trend_min_bars is top-level rather than inside a policy block, and it is
    the one non-monetary field that decides whether a real order is placed: every
    loader applies int(raw["trend_min_bars"]), so `true` loads as 1 and a
    "60-minute downtrend" can be validated on a SINGLE bar -- passing both the
    executor's trend test and this module's coverage probe, against a checked-in
    six-bar minimum (Codex review: "Validate the configured trend bar minimum";
    reproduced loading 1 where the checked-in value is 6).

    The earlier revision scoped this validator to "the MONEY, not the whole
    schema". That was the wrong line: this field gates placement as directly as
    the ladder does, it just carries no dollar sign."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    real["trend_min_bars"] = value
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode != 0
    assert "trend_min_bars" in result.stderr


def test_the_checked_in_trend_bar_minimum_is_accepted(tmp_path):
    """The control: 6 is a plain positive int and must pass untouched."""
    real = json.loads((REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8"))
    assert real["trend_min_bars"] == 6
    result = _stream_config_cli_text(tmp_path, json.dumps(real))
    assert result.returncode == 0, result.stderr
