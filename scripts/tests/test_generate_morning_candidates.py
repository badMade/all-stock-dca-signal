#!/usr/bin/env python3
"""Tests for the Gate 5 (1030ET morning) candidate generator.

The morning slot is the odd one in this family for two reasons, and this file is
mostly about those two:

1. **It drives the BASE class in live mode**, so its config loader is the only
   one taking ``mode`` positionally-second -- and handing the shared CLI the bare
   function would silently load the 9:45 WATCHLIST profile for a 10:30 LIVE run.
2. **Nothing narrows the universe.** The retired 9:45 pre-flight used to cut the
   10:30 universe down to its verdict. Every configured, non-excluded ticker is
   now scored, and the output is a pure function of the arguments (nothing reads
   the artifact tree).

The payload contract, the fail-closed input rules and the document shape are
shared and covered by ``test_generate_market_open_candidates.py``; the
trend-validation contract is covered by ``test_generate_midday_candidates.py``.
This file does not re-test them beyond one case each.
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
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pytest

gmc = pytest.importorskip(
    "scripts.generate_morning_candidates",
    reason="scripts/generate_morning_candidates.py not present",
)

from morning_dca import (  # noqa: E402
    MORNING_LIVE_CRON_ID,
    MORNING_LIVE_MODE,
    MORNING_STREAM,
    MORNING_WATCHLIST_MODE,
    MorningConfig,
    MorningDCAExecutor,
)
from near_close_dca import EASTERN, Action, SourceName  # noqa: E402
from scripts.stream_candidates import StreamMarketData  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = REPO_ROOT / "config"
DAY = date(2026, 8, 5)
PREVIOUS_SESSION = date(2026, 8, 4)
DECISION_ET = time(10, 30)


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
    """5-minute bars from ``start`` to 10:30 ET, stepping down at 10:00.

    The 10:30 decision anchor's completed bar is 10:25, and the trend window is
    the 60 minutes before 10:30 -- which at 10:30 is the whole session so far.
    Stepping down at 10:00 puts the step inside that window with well over
    ``trend_min_bars`` (6) usable bars on either side.
    """
    bars = []
    cur = datetime.combine(DAY, start, tzinfo=ET)
    stop = datetime.combine(DAY, DECISION_ET, tzinfo=ET)
    step = datetime.combine(DAY, time(10, 0), tzinfo=ET)
    while cur <= stop:
        bars.append(_bar(cur, late_price if cur >= step else early_price))
        cur += timedelta(minutes=5)
    return bars


def _ticker_payload(
    *,
    early_price: float = 99.0,
    late_price: float = 95.0,
    previous_close: float = 100.0,
    previous_low: float = 97.0,
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


def _config(**overrides: Any) -> MorningConfig:
    """A morning live config with a one-ticker universe and the standard ladder."""
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
    return MorningConfig(**base)


def _write_preflight(root: Path, eligibility: dict[str, bool], *, overall_ok: bool = True) -> None:
    """Write a schema-shaped same-day artifact of the RETIRED 9:45 pre-flight.

    Only used to prove nothing reads it any more: the pre-flight no longer exists
    and the 10:30 stream must behave identically with or without a stale one.
    """
    run_dir = root / f"{DAY.strftime('%Y%m%d')}_0945ET"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": f"morning_preflight_{DAY.strftime('%Y%m%d')}",
                "trading_day": DAY.isoformat(),
                "overall_ok": overall_ok,
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "stream": MORNING_STREAM,
                    "run_type": "morning_dca_preflight",
                    "state_schema_version": "morning-preflight-v2-machine-complete",
                },
                "per_ticker_eligibility": [
                    {"symbol": symbol, "eligible_to_execute": eligible}
                    for symbol, eligible in eligibility.items()
                ],
            }
        ),
        encoding="utf-8",
    )


def _run(
    market_data: dict,
    config: MorningConfig | None = None,
    *,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    check_planner_compatibility: bool = False,
) -> dict:
    return gmc.generate_morning_candidates(
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


# --- the live-mode config loader --------------------------------------------


def test_the_spec_uses_the_mode_pinning_loader_not_the_bare_one():
    """The bug this wrapper exists to prevent is silent: ``load_morning_config``'s
    second POSITIONAL parameter is ``mode``, defaulting to the 9:45 watchlist
    profile, while StreamSpec calls every loader as
    ``loader(config_dir, allow_unpinned_account=True)``. Both parse the same file,
    so a 10:30 live run would have loaded watchlist policy with nothing raising."""
    assert gmc.MORNING_SPEC.config_loader is gmc.load_morning_live_config


def test_load_morning_live_config_binds_the_live_roots_not_the_watchlist_ones():
    from morning_dca import load_morning_config

    live = gmc.load_morning_live_config(REAL_CONFIG_DIR, allow_unpinned_account=True)
    watchlist = load_morning_config(
        REAL_CONFIG_DIR, MORNING_WATCHLIST_MODE, allow_unpinned_account=True
    )
    # The two modes share the ladder and caps and differ in where they write, so
    # the artifact root is what proves the mode actually reached the loader.
    assert live.morning_run_state_root != watchlist.morning_run_state_root
    # And what the shared CLI's calling convention would have produced instead.
    mistaken = load_morning_config(REAL_CONFIG_DIR, allow_unpinned_account=True)
    assert mistaken.morning_run_state_root == watchlist.morning_run_state_root
    assert live.morning_run_state_root != mistaken.morning_run_state_root


def test_the_spec_identifies_the_slot_and_stream_it_speaks_for():
    spec = gmc.MORNING_SPEC
    assert spec.slot == "1030ET"
    assert spec.runner_slot_id == "1030"
    assert spec.stream == MORNING_STREAM
    assert spec.cron_id == MORNING_LIVE_CRON_ID
    assert spec.live_mode == MORNING_LIVE_MODE
    assert spec.executor_cls is MorningDCAExecutor
    assert spec.stream_config_filename == "morning_dca.json"
    assert spec.source is SourceName.ROBINHOOD
    assert spec.executor_kwargs == {}


def test_document_identifies_the_slot_and_stream_it_speaks_for():
    document = _run(_market_data(TEST=_ticker_payload()), _config())
    assert document["slot"] == "1030ET"
    assert document["runner_slot_id"] == "1030"
    assert document["stream"] == MORNING_STREAM
    assert document["cron_id"] == MORNING_LIVE_CRON_ID
    assert document["decision_time_et"] == "10:30"
    assert document["trading_day"] == DAY.isoformat()
    assert document["source"] == "robinhood"


# --- the retired 9:45 pre-flight is not consulted ---------------------------


def test_a_stale_preflight_artifact_changes_nothing(tmp_path, monkeypatch):
    """Owner direction: "There should be nothing left that is dependent on the
    pre-flight." A leftover artifact of the retired pre-screen (here one that
    would have gated GATED out) must not narrow the universe or alter any row:
    the document is identical to the one produced with no artifact on disk."""
    config = _config(tickers=("TEST", "GATED"))
    data = _market_data(TEST=_ticker_payload(), GATED=_ticker_payload())
    baseline = _run(data, config)

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str())
    _write_preflight(tmp_path / "morning_dca_preflight", {"TEST": True, "GATED": False})
    with_stale_artifact = _run(data, config)

    assert with_stale_artifact == baseline
    assert [row["ticker"] for row in baseline["candidates"]] == ["TEST", "GATED"]


def test_the_document_has_no_preflight_gate_field():
    document = _run(_market_data(TEST=_ticker_payload()))
    assert document["buy_count"] == 1
    assert "preflight_gate" not in document


def test_sibling_slots_have_no_preflight_gate_field_either():
    """The field was removed from the shared document for every slot."""
    midday = pytest.importorskip("scripts.generate_midday_candidates")
    from midday_dca import MiddayConfig

    config = MiddayConfig(
        tickers=("TEST",),
        excluded_tickers=frozenset(),
        per_ticker_sizing={},
        sizing_tiers=((0.001, 1.0),),
        minimum_drop_vs_previous_close=0.001,
        base_buy_amount_usd=2.0,
        base_buy_amount_below_close_usd=1.0,
        interval_minutes=5,
        trend_min_bars=6,
    )
    document = midday.generate_midday_candidates(
        trading_day=DAY,
        config=config,
        market_data={},
        deployed_last_7d_usd=0.0,
        config_dir=REAL_CONFIG_DIR,
        check_planner_compatibility=False,
    )
    assert "preflight_gate" not in document


# --- trend validation applies ------------------------------------------------


def test_qualifying_drop_with_a_validated_trend_buys():
    document = _run(_market_data(TEST=_ticker_payload()), _config())
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["buy_multiplier"] == pytest.approx(2.5)
    assert row["selected_base_amount_usd"] == pytest.approx(2.0)
    assert row["suggested_amount_usd"] == pytest.approx(5.0)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["trend_validation_exempt"] is False


def test_unvalidated_trend_is_manual_review_not_an_auto_buy():
    """Unlike 0935ET this slot is not trend-exempt: at 10:30 the full 60-minute
    window exists. Too few usable bars means manual review, and a truncated fetch
    does not error -- it quietly moves real buys out of ``cands``."""
    data = _market_data(TEST=_ticker_payload(start=time(10, 15)))
    document = _run(data, _config())
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["execution_status"] == "manual_review_candidate"
    assert document["manual_review_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


# --- the kill switch is NOT this slot's job ---------------------------------


def test_the_deployment_kill_switch_flag_is_not_accepted():
    """Like 0935ET and the intraday fires, and unlike 1230ET: this slot's prompt
    does not make the trailing-7-day guard the producer's responsibility, so
    accepting the figure would size DIFFERENTLY from the executor this mirrors."""
    assert gmc.MORNING_SPEC.requires_deployed_last_7d_usd is False
    with pytest.raises(SystemExit) as excinfo:
        with contextlib.redirect_stderr(io.StringIO()):
            gmc.main(
                [
                    "--trading-day", DAY.isoformat(),
                    "--market-data", "/dev/null",
                    "--deployed-last-7d-usd", "500",
                ]
            )
    assert excinfo.value.code != 0
    document = _run(_market_data(TEST=_ticker_payload()), _config())
    assert document["deployed_last_7d_usd"] is None
    assert document["kill_switch_evaluated"] is False
    assert document["kill_switch_engaged"] is False


# --- parity with the executor itself ----------------------------------------


def test_document_matches_the_executor_called_directly():
    """Parity check: every published row must equal what
    ``MorningDCAExecutor.evaluate()`` produces for the same inputs, over the full
    configured universe (both tickers are scored; nothing narrows it)."""
    config = _config(tickers=("TEST", "OTHER"))
    data = _market_data(TEST=_ticker_payload(), OTHER=_ticker_payload())
    document = _run(data, config)

    provider = StreamMarketData(
        gmc.MORNING_SOURCE,
        data["sources"]["robinhood"],
        quote_staleness_minutes=config.interval_minutes,
        trading_day=DAY,
    )
    executor = MorningDCAExecutor(config=config, mode=MORNING_LIVE_MODE)
    now = datetime.combine(DAY, executor.decision_time, tzinfo=EASTERN)
    signals = executor.evaluate(now, provider)

    assert [row["ticker"] for row in document["candidates"]] == [s.ticker for s in signals]
    for row, signal in zip(document["candidates"], signals, strict=True):
        assert row["action"] == signal.action.value
        assert row["buy_multiplier"] == pytest.approx(signal.multiplier)
        assert row["suggested_amount_usd"] == pytest.approx(
            round(signal.hypothetical_amount_usd, 2)
        )
        assert row["target_price"] == signal.current_price
        assert row["execution_status"] == signal.execution_status
    assert [s.ticker for s in signals] == ["TEST", "OTHER"]


# --- end to end through the CLI ---------------------------------------------


def _cli(tmp_path, *extra: str):
    """Run the CLI in a FRESH process with $DCA_CONFIG_DIR pointed at a tmp config
    directory, so dca_runner imports from the same directory --config-dir names.

    In-process invocation cannot reach the CLI's own checks: any tmp directory
    differs from the dca_runner.CONFIG_DIR this test session already resolved, so
    the config-dir mismatch guard fires first. A subprocess is also how the
    executor prompt actually invokes this command.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in gmc._REQUIRED_CONFIG_FILENAMES:
        (config_dir / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
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
    path = tmp_path / "market_data_1030ET.json"
    path.write_text(json.dumps(_market_data(TEST=_ticker_payload())), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable, "-m", "scripts.generate_morning_candidates",
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(config_dir),
            *extra,
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DCA_CONFIG_DIR": str(config_dir),
            # Keep any artifact-root resolution inside the test's tmp tree.
            "DCA_CRON_TRACKING_ROOT": str(tmp_path / "cron_tracking"),
        },
        capture_output=True,
        text=True,
    )


def test_cli_emits_the_slots_document(tmp_path):
    result = _cli(tmp_path)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["slot"] == "1030ET"
    assert document["runner_slot_id"] == "1030"
    assert document["stream"] == MORNING_STREAM
    assert document["cron_id"] == MORNING_LIVE_CRON_ID
    assert document["buy_count"] == 1
    assert [row["ticker"] for row in document["cands"]] == ["TEST"]
    assert "preflight_gate" not in document


def test_a_truncated_trend_window_is_withheld():
    """At 10:30 the 60-minute window is the whole session so far (09:30-10:30),
    so a payload starting at 10:00 is half of it. The executor's own rule counts
    bars in the window without requiring them to span it, so such a row grades
    auto_buy_eligible at full size; this entry point declines to publish it. See
    test_generate_midday_candidates.py for the reproduction."""
    # _falling_bars steps down at 10:00, so starting at 09:50 keeps a genuine
    # decline (two bars at the early price, seven at the late one) with eight
    # usable bars, well over trend_min_bars (6) -- on 40 of the required 60
    # minutes, and reaching back nowhere near the 09:30 window start.
    data = _market_data(TEST=_ticker_payload(start=time(9, 50)))
    document = _run(data, _config())
    row = _only(document)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["withheld_incomplete_trend_window_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


def test_a_full_trend_window_still_publishes():
    """The control: bars from the open, which at 10:30 is the full window."""
    document = _run(_market_data(TEST=_ticker_payload()), _config())
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]


# --- every configured ticker's payload is validated ------------------------


def test_every_configured_tickers_malformed_payload_aborts():
    """Nothing is gated out any more, so every configured ticker's payload is
    evidence the executor reads. A truncated one is a broken fetch and must still
    fail closed rather than be skipped."""
    config = _config(tickers=("TEST", "OTHER"))
    data = _market_data(
        TEST=_ticker_payload(),
        OTHER={"previous_close_bar": _previous_close_bar(100.0, 97.0)},
    )
    with pytest.raises(ValueError, match="incomplete payload"):
        _run(data, config)


def test_the_spec_has_no_universe_filter():
    """The morning spec used to be the only one carrying a universe filter."""
    assert not hasattr(gmc.MORNING_SPEC, "universe_filter")
    assert not hasattr(gmc, "preflight_admitted_tickers")
