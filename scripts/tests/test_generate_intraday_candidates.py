#!/usr/bin/env python3
"""Tests for the Gate 5 (1130/1330/1430ET intraday) candidate generator.

The intraday stream is the only one where ONE executor class, ONE cron and ONE
config serve THREE fires, so this file is almost entirely about the thing that
makes that safe: the ``--slot`` selector actually reaching the executor, and
each fire being scored against its own decision anchor rather than another's.

The payload contract, the fail-closed input rules and the document shape are
shared with the other executor-class slots and covered by
``test_generate_market_open_candidates.py``; the trend-validation contract is
covered there and in ``test_generate_midday_candidates.py``. This file does not
re-test them beyond one anchor-sensitive case each.
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

gic = pytest.importorskip(
    "scripts.generate_intraday_candidates",
    reason="scripts/generate_intraday_candidates.py not present",
)

from intraday_dca import (  # noqa: E402
    DEFAULT_INTRADAY_SLOT,
    INTRADAY_LIVE_CRON_ID,
    INTRADAY_LIVE_MODE,
    INTRADAY_SLOTS,
    INTRADAY_STREAM,
    IntradayConfig,
    IntradayDCAExecutor,
)
from near_close_dca import EASTERN, Action, SourceName  # noqa: E402
from scripts.stream_candidates import (  # noqa: E402
    StreamMarketData,
    StreamSpec,
    run_multi_slot_cli,
)

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = REPO_ROOT / "config"
DAY = date(2026, 8, 5)
PREVIOUS_SESSION = date(2026, 8, 4)

#: The decision anchor each ``--slot`` token scores against, spelled out here
#: rather than read from the profiles so a profile edit that moved an anchor
#: fails these tests instead of silently moving them with it.
ANCHORS: dict[str, time] = {"1130": time(11, 30), "1330": time(13, 30), "1430": time(14, 30)}


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


def _falling_bars(
    anchor: time, early_price: float, late_price: float, *, start: time = time(9, 30)
) -> list:
    """5-minute bars from ``start`` to ``anchor``, stepping down 30 minutes before it.

    The step lands inside the 60-minute trend window that ends at ``anchor``, so
    the window opens at ``early_price`` and its last completed bar is
    ``late_price`` -- a genuinely falling trend with well over
    ``trend_min_bars`` (6) usable bars.
    """
    bars = []
    cur = datetime.combine(DAY, start, tzinfo=ET)
    stop = datetime.combine(DAY, anchor, tzinfo=ET)
    step = stop - timedelta(minutes=30)
    while cur <= stop:
        bars.append(_bar(cur, late_price if cur >= step else early_price))
        cur += timedelta(minutes=5)
    return bars


def _ticker_payload(
    anchor: time,
    *,
    early_price: float = 99.0,
    late_price: float = 95.0,
    previous_close: float = 100.0,
    previous_low: float = 97.0,
    start: time = time(9, 30),
) -> dict:
    return {
        "previous_close_bar": _previous_close_bar(previous_close, previous_low),
        "intraday_bars": _falling_bars(anchor, early_price, late_price, start=start),
    }


def _market_data(**per_ticker: dict) -> dict:
    return {
        "sources": {"robinhood": dict(per_ticker)},
        "source_errors": [],
        "ticker_errors": {"robinhood": []},
    }


def _config(**overrides: Any) -> IntradayConfig:
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
    )
    base.update(overrides)
    return IntradayConfig(**base)


def _run(
    slot: str,
    market_data: dict,
    config: IntradayConfig | None = None,
    *,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    check_planner_compatibility: bool = False,
) -> dict:
    return gic.generate_intraday_candidates(
        slot=slot,
        trading_day=DAY,
        config=_config() if config is None else config,
        market_data=market_data["sources"]["robinhood"],
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


# --- one cron, three fires --------------------------------------------------


def test_every_configured_intraday_slot_has_a_spec():
    """``INTRADAY_SPECS`` is derived from ``INTRADAY_SLOTS`` on purpose: a fourth
    fire added to that tuple must get an entry point, not a KeyError at 2:30."""
    assert tuple(sorted(gic.INTRADAY_SPECS)) == tuple(sorted(INTRADAY_SLOTS))


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_spec_identity_matches_the_stream_it_mirrors(slot):
    spec = gic.INTRADAY_SPECS[slot]
    assert spec.slot == f"{slot}ET"
    # dca_runner.SLOTS keys carry no "ET"; feeding it the label would abort a
    # downstream assess()/execute_plan() call that Gate 5 already succeeded for.
    assert spec.runner_slot_id == slot
    assert spec.stream == INTRADAY_STREAM
    assert spec.cron_id == INTRADAY_LIVE_CRON_ID
    assert spec.live_mode == INTRADAY_LIVE_MODE
    assert spec.executor_cls is IntradayDCAExecutor
    assert spec.stream_config_filename == "intraday_dca.json"
    assert spec.source is SourceName.ROBINHOOD
    # The whole mechanism: the constructor argument that selects the profile.
    assert spec.executor_kwargs == {"slot": slot}


def test_the_three_specs_differ_only_in_slot_identity():
    """If a fourth field ever diverges, these belong in separate modules with
    separate parsers -- ``run_multi_slot_cli`` builds ONE parser for all three."""
    varying = set()
    reference = gic.INTRADAY_SPECS[INTRADAY_SLOTS[0]]
    for slot in INTRADAY_SLOTS[1:]:
        spec = gic.INTRADAY_SPECS[slot]
        for f in dataclasses.fields(StreamSpec):
            if getattr(spec, f.name) != getattr(reference, f.name):
                varying.add(f.name)
    assert varying == {"slot", "runner_slot_id", "executor_kwargs"}


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_each_slot_is_scored_against_its_own_decision_anchor(slot):
    """The reason ``--slot`` exists. Same qualifying drop, three fires: each
    document must report its own anchor and its own slot label."""
    document = _run(slot, _market_data(TEST=_ticker_payload(ANCHORS[slot])))
    assert document["slot"] == f"{slot}ET"
    assert document["runner_slot_id"] == slot
    assert document["decision_time_et"] == ANCHORS[slot].strftime("%H:%M")
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["buy_multiplier"] == pytest.approx(2.5)
    assert row["suggested_amount_usd"] == pytest.approx(5.0)
    assert row["execution_status"] == "auto_buy_eligible"
    assert document["buy_count"] == 1


def test_an_earlier_fires_payload_does_not_score_at_a_later_anchor():
    """The concrete harm the required ``--slot`` flag prevents: an 11:30 payload
    handed to the 2:30 fire is three hours stale, and the executor rejects it
    rather than pricing the afternoon off a late-morning bar. Were the flag to
    default to ``DEFAULT_INTRADAY_SLOT`` instead, a 2:30 run that forgot it would
    do the mirror image -- score its own fresh payload against the 11:30 anchor
    and publish it under the 1130ET label."""
    stale = _market_data(TEST=_ticker_payload(ANCHORS["1130"]))
    document = _run("1430", stale)
    row = _only(document)
    assert row["action"] != Action.BUY.value
    assert document["buy_count"] == 0
    assert document["cands"] == []


def test_default_intraday_slot_is_never_silently_applied():
    """``IntradayDCAExecutor`` defaults ``slot`` for library callers. This entry
    point must not inherit that default -- guessing a fire is exactly the failure
    the test above describes."""
    assert DEFAULT_INTRADAY_SLOT in INTRADAY_SLOTS
    with pytest.raises(SystemExit) as excinfo:
        with contextlib.redirect_stderr(io.StringIO()):
            gic.main(["--trading-day", DAY.isoformat(), "--market-data", "/dev/null"])
    assert excinfo.value.code != 0


# --- spec_for_slot ----------------------------------------------------------


@pytest.mark.parametrize("given", ["1330", "1330ET"])
def test_spec_for_slot_accepts_either_spelling(given):
    """Callers hold the label ("1330ET", from the folder name and the prompts)
    or the token ("1330", from ``dca_runner.SLOTS`` and the executor)."""
    assert gic.spec_for_slot(given) is gic.INTRADAY_SPECS["1330"]


@pytest.mark.parametrize("given", ["1230", "1130E", "", "nonsense", "1530ET"])
def test_spec_for_slot_fails_loud_on_anything_else(given):
    """Never falls back to DEFAULT_INTRADAY_SLOT: a typo must not silently score
    the payload against 11:30. 1230/1530 are real slots of OTHER streams, which
    is precisely the mistake most likely to be made here."""
    with pytest.raises(ValueError, match="unknown intraday slot"):
        gic.spec_for_slot(given)


# --- trend validation applies to all three ----------------------------------


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_unvalidated_trend_is_manual_review_not_an_auto_buy(slot):
    """None of the three fires is trend-exempt (only 0935ET is): each has a full
    60-minute window available, so too few usable bars means manual review, not
    an auto-buy. A truncated fetch does not error -- it quietly moves real buys
    out of ``cands``."""
    anchor = ANCHORS[slot]
    data = _market_data(
        TEST=_ticker_payload(
            anchor,
            # Bars only from 15 minutes before the anchor: 4 usable bars in the
            # 60-minute window, under trend_min_bars (6).
            start=(datetime.combine(DAY, anchor, tzinfo=ET) - timedelta(minutes=15)).timetz(),
        )
    )
    document = _run(slot, data)
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["execution_status"] == "manual_review_candidate"
    assert row["trend_validated"] is not True
    assert document["trend_validation_exempt"] is False
    assert document["manual_review_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


# --- the kill switch is NOT this slot's job ---------------------------------


def test_the_deployment_kill_switch_flag_is_not_accepted():
    """Unlike 1230ET, none of the three intraday prompts makes the trailing-7-day
    guard the producer's responsibility, so accepting the figure would size
    DIFFERENTLY from the executor this mirrors."""
    for slot in INTRADAY_SLOTS:
        assert gic.INTRADAY_SPECS[slot].requires_deployed_last_7d_usd is False
    with pytest.raises(SystemExit) as excinfo:
        with contextlib.redirect_stderr(io.StringIO()):
            gic.main(
                [
                    "--slot",
                    "1130",
                    "--trading-day",
                    DAY.isoformat(),
                    "--market-data",
                    "/dev/null",
                    "--deployed-last-7d-usd",
                    "500",
                ]
            )
    assert excinfo.value.code != 0


def test_the_document_reports_no_kill_switch_guard():
    document = _run("1330", _market_data(TEST=_ticker_payload(ANCHORS["1330"])))
    assert document["deployed_last_7d_usd"] is None
    assert document["kill_switch_evaluated"] is False
    assert document["kill_switch_engaged"] is False


# --- the shared budget is Gate 6's, not this module's ------------------------


def test_an_earlier_fires_spend_does_not_reduce_a_later_fires_amounts():
    """All three fires draw on ONE pooled Normal-tier ceiling, and this module
    knows nothing about it: it evaluates signals only. Pooling belongs to the
    executing agent's Gate 6 / Gate 6.5, exactly where the prompt has it. This
    test pins that division so nobody 'fixes' it here by scanning the artifact
    tree from a Gate 5 producer."""
    data = _market_data(TEST=_ticker_payload(ANCHORS["1130"]))
    first = _run("1130", data)
    later = _run("1330", _market_data(TEST=_ticker_payload(ANCHORS["1330"])))
    assert first["candidates"][0]["suggested_amount_usd"] == pytest.approx(
        later["candidates"][0]["suggested_amount_usd"]
    )
    assert "remaining_daily_usd" not in first


def test_already_covered_tickers_are_never_scored():
    """The narrower Gate 4 case this module DOES handle: a compatible executor
    already placed an order for the ticker in this same window."""
    data = _market_data(TEST=_ticker_payload(ANCHORS["1430"]))
    document = _run("1430", data, already_covered_tickers=["TEST"])
    assert document["candidates"] == []
    assert document["already_covered_tickers"] == ["TEST"]


# --- parity with the executor itself ----------------------------------------


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_document_matches_the_executor_called_directly(slot):
    """Gate 5 forbids a reimplementation. Constructing the real executor for this
    fire and calling ``evaluate()`` at the same anchor must produce the same
    decision the document publishes -- if it ever does not, this module has
    become a second implementation of the signal logic."""
    anchor = ANCHORS[slot]
    config = _config()
    payload = _ticker_payload(anchor)
    document = _run(slot, _market_data(TEST=payload))

    executor = IntradayDCAExecutor(config=config, mode=INTRADAY_LIVE_MODE, slot=slot)
    provider = StreamMarketData(
        SourceName.ROBINHOOD,
        {"TEST": payload},
        quote_staleness_minutes=config.interval_minutes,
        trading_day=DAY,
        ticker_errors=[],
    )
    now = datetime.combine(DAY, executor.decision_time, tzinfo=EASTERN)
    signals = executor.evaluate(now, provider)

    assert len(signals) == 1
    signal = signals[0]
    row = _only(document)
    assert row["ticker"] == signal.ticker
    assert row["action"] == signal.action.value
    assert row["buy_multiplier"] == pytest.approx(signal.multiplier)
    assert row["suggested_amount_usd"] == pytest.approx(round(signal.hypothetical_amount_usd, 2))
    assert row["target_price"] == signal.current_price
    assert row["execution_status"] == signal.execution_status


# --- run_multi_slot_cli's own guard -----------------------------------------


def test_run_multi_slot_cli_refuses_specs_that_need_different_flags():
    """One parser serves every spec in the mapping, so a slot that disagreed on
    ``requires_deployed_last_7d_usd`` would silently inherit another slot's flag
    set -- and omitting that flag changes SIZING, not just the error message."""
    specs = dict(gic.INTRADAY_SPECS)
    specs["1330"] = dataclasses.replace(specs["1330"], requires_deployed_last_7d_usd=True)
    with pytest.raises(ValueError, match="disagree"):
        run_multi_slot_cli(specs, ["--slot", "1130"], description="x")


def test_run_multi_slot_cli_refuses_an_empty_mapping():
    with pytest.raises(ValueError, match="at least one slot spec"):
        run_multi_slot_cli({}, [], description="x")


# --- end to end through the CLI ---------------------------------------------


def _cli(tmp_path, slot: str, *extra: str):
    """Run the CLI in a FRESH process with $DCA_CONFIG_DIR pointed at a tmp config
    directory, so dca_runner imports from the same directory --config-dir names.

    In-process invocation cannot reach the CLI's own checks: any tmp directory
    differs from the dca_runner.CONFIG_DIR this test session already resolved, so
    the config-dir mismatch guard fires first. A subprocess is also how the
    executor prompt actually invokes this command.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in gic._REQUIRED_CONFIG_FILENAMES:
        (config_dir / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    # Narrow the universe to the one ticker these fixtures carry; budgets,
    # sizing_ladder and market_calendar stay VERBATIM (dca_runner cross-checks
    # them against each other in whole cents).
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
    path = tmp_path / f"market_data_{slot}ET.json"
    path.write_text(
        json.dumps(_market_data(TEST=_ticker_payload(ANCHORS[slot]))), encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable, "-m", "scripts.generate_intraday_candidates",
            "--slot", slot,
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(config_dir),
            *extra,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "DCA_CONFIG_DIR": str(config_dir)},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_cli_emits_the_selected_fires_document(tmp_path, slot):
    result = _cli(tmp_path, slot)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["slot"] == f"{slot}ET"
    assert document["runner_slot_id"] == slot
    assert document["cron_id"] == INTRADAY_LIVE_CRON_ID
    assert document["stream"] == INTRADAY_STREAM
    assert document["decision_time_et"] == ANCHORS[slot].strftime("%H:%M")
    # The real signal path, end to end: one qualifying drop through the real
    # ladder, so this asserts the wiring AND the arithmetic.
    assert document["buy_count"] == 1
    assert [row["ticker"] for row in document["cands"]] == ["TEST"]


def test_cli_rejects_an_unknown_slot(tmp_path):
    """argparse choices, not a silent fallback to DEFAULT_INTRADAY_SLOT."""
    result = _cli(tmp_path, "1130", "--slot", "1230")
    assert result.returncode != 0
    assert "invalid choice" in result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_a_truncated_trend_window_is_withheld_for_every_fire(slot):
    """Each fire's window is the 60 minutes before ITS OWN anchor, so a payload
    sized for an earlier fire's window is short for a later one. The executor's
    own rule counts bars in the window without requiring them to span it, so
    such a row grades auto_buy_eligible at full size; this entry point declines
    to publish it. See test_generate_midday_candidates.py for the reproduction."""
    anchor = ANCHORS[slot]
    # Bars covering only the final 40 minutes. _falling_bars steps down 30
    # minutes before the anchor, so this still holds a genuine decline (two bars
    # at the early price, seven at the late one) and nine usable bars, well over
    # trend_min_bars (6) -- on two thirds of the required 60-minute window.
    late_start = (datetime.combine(DAY, anchor, tzinfo=ET) - timedelta(minutes=40)).timetz()
    document = _run(slot, _market_data(TEST=_ticker_payload(anchor, start=late_start)))
    row = _only(document)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["withheld_incomplete_trend_window_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


@pytest.mark.parametrize("slot", INTRADAY_SLOTS)
def test_a_full_trend_window_still_publishes_for_every_fire(slot):
    """The control for the test above, per fire."""
    document = _run(slot, _market_data(TEST=_ticker_payload(ANCHORS[slot])))
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]
