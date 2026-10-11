#!/usr/bin/env python3
"""Tests for the Gate 5 (1030ET morning) candidate generator.

The morning slot is the odd one in this family for two reasons, and this file is
mostly about those two:

1. **It drives the BASE class in live mode**, so its config loader is the only
   one taking ``mode`` positionally-second -- and handing the shared CLI the bare
   function would silently load the 9:45 WATCHLIST profile for a 10:30 LIVE run.
2. **The 9:45 pre-flight gate runs.** ``MorningDCAExecutor.evaluate()`` narrows
   the universe to the same-day pre-flight verdict for exactly this
   stream+mode combination, which makes this the only Gate 5 command in the
   family that reads the artifact tree -- and the only one whose output is not a
   pure function of its arguments.

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


def _config(tmp_path: Path | None = None, **overrides: Any) -> MorningConfig:
    """A morning live config whose pre-flight root points at an ISOLATED tree.

    Never the bundled production default: the gate reads that tree off disk, so a
    test that left the root unset would either pick up whatever a developer's
    machine happens to have under ``cron_tracking/`` or silently exercise the
    missing-artifact branch for the wrong reason.
    """
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
        preflight_run_state_root=str(
            (tmp_path if tmp_path is not None else Path("/nonexistent")) / "preflight"
        ),
    )
    base.update(overrides)
    return MorningConfig(**base)


def _write_preflight(root: Path, eligibility: dict[str, bool], *, overall_ok: bool = True) -> None:
    """Write a schema-shaped same-day 9:45 pre-flight artifact under ``root``."""
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


def test_document_identifies_the_slot_and_stream_it_speaks_for(tmp_path):
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["slot"] == "1030ET"
    assert document["runner_slot_id"] == "1030"
    assert document["stream"] == MORNING_STREAM
    assert document["cron_id"] == MORNING_LIVE_CRON_ID
    assert document["decision_time_et"] == "10:30"
    assert document["trading_day"] == DAY.isoformat()
    assert document["source"] == "robinhood"


# --- the 9:45 pre-flight gate -----------------------------------------------


def test_a_missing_preflight_artifact_evaluates_the_full_universe(tmp_path):
    """NON-BLOCKING by design (Geoff, 2026-08-02: "should not block any later
    runs"). The 9:45 scan is a coarse pre-screen; the 10:30 slot's own signal,
    trend, budget and top-up checks are the real safeguards. A fail-closed gate
    here would turn every missed pre-flight into a silently empty buy list."""
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["buy_count"] == 1
    gate = document["preflight_gate"]
    assert gate["preflight_found"] is False
    # Reported, not swallowed: this is how a reader tells a degraded gate apart
    # from a pre-screen that genuinely admitted everything.
    assert "no same-day 9:45 pre-flight artifact found" in gate["warning"]


def test_a_preflight_verdict_narrows_the_universe_and_says_so(tmp_path):
    """The gate's real job. GATED is dropped before scoring, and the document
    names it -- a short candidate list must be attributable."""
    _write_preflight(tmp_path / "preflight", {"TEST": True, "GATED": False})
    config = _config(tmp_path, tickers=("TEST", "GATED"))
    data = _market_data(TEST=_ticker_payload(), GATED=_ticker_payload())
    document = _run(data, config)
    assert [row["ticker"] for row in document["candidates"]] == ["TEST"]
    gate = document["preflight_gate"]
    assert gate["preflight_found"] is True
    assert gate["admitted_tickers"] == ["TEST"]
    assert gate["gated_out_tickers"] == ["GATED"]
    assert gate["eligible_to_execute_tickers"] == ["TEST"]
    assert "warning" not in gate


def test_a_valid_but_empty_preflight_stays_non_blocking(tmp_path):
    """A quiet pre-flight must not be MORE restrictive than a missing one, which
    already degrades to the full universe (Codex review, morning_dca: "Keep valid
    empty preflights non-blocking")."""
    _write_preflight(tmp_path / "preflight", {"TEST": False})
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["buy_count"] == 1
    gate = document["preflight_gate"]
    assert gate["preflight_found"] is True
    assert gate["eligible_to_execute_tickers"] == []
    assert "zero eligible tickers" in gate["warning"]


def test_a_preflight_that_flagged_itself_invalid_is_treated_as_missing(tmp_path):
    """``overall_ok=false`` means the 9:45 scan itself failed; trusting whatever
    eligibility data it happened to write would be worse than having none."""
    _write_preflight(tmp_path / "preflight", {"TEST": True}, overall_ok=False)
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["preflight_gate"]["preflight_found"] is False
    assert document["buy_count"] == 1


def test_the_gate_reads_only_the_configured_root(tmp_path):
    """Containment: a relocated/staging run must not read production evidence.
    An artifact under a DIFFERENT root than the config names is not consulted."""
    _write_preflight(tmp_path / "elsewhere", {"TEST": False, "OTHER": True})
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["preflight_gate"]["preflight_found"] is False
    assert document["buy_count"] == 1


def test_sibling_slots_report_no_preflight_gate():
    """The field is stream-scoped: it must be null everywhere the gate does not
    apply, so a reader cannot mistake "gate not applicable" for "gate found
    nothing"."""
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
    assert document["preflight_gate"] is None


# --- trend validation applies ------------------------------------------------


def test_qualifying_drop_with_a_validated_trend_buys(tmp_path):
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["buy_multiplier"] == pytest.approx(2.5)
    assert row["selected_base_amount_usd"] == pytest.approx(2.0)
    assert row["suggested_amount_usd"] == pytest.approx(5.0)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["trend_validation_exempt"] is False


def test_unvalidated_trend_is_manual_review_not_an_auto_buy(tmp_path):
    """Unlike 0935ET this slot is not trend-exempt: at 10:30 the full 60-minute
    window exists. Too few usable bars means manual review, and a truncated fetch
    does not error -- it quietly moves real buys out of ``cands``."""
    data = _market_data(TEST=_ticker_payload(start=time(10, 15)))
    document = _run(data, _config(tmp_path))
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["execution_status"] == "manual_review_candidate"
    assert document["manual_review_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


# --- the kill switch is NOT this slot's job ---------------------------------


def test_the_deployment_kill_switch_flag_is_not_accepted(tmp_path):
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
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["deployed_last_7d_usd"] is None
    assert document["kill_switch_evaluated"] is False
    assert document["kill_switch_engaged"] is False


# --- parity with the executor itself ----------------------------------------


def test_document_matches_the_executor_called_directly(tmp_path):
    """Parity check: every published row must equal what
    ``MorningDCAExecutor.evaluate()`` produces for the same inputs -- including
    the pre-flight narrowing, which is part of ``evaluate()`` and therefore part
    of what must not be reimplemented here."""
    _write_preflight(tmp_path / "preflight", {"TEST": True, "GATED": False})
    config = _config(tmp_path, tickers=("TEST", "GATED"))
    data = _market_data(TEST=_ticker_payload(), GATED=_ticker_payload())
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
    assert document["preflight_gate"] == executor.preflight_gate


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
            # Contain the pre-flight gate's filesystem read: without this the
            # bundled config's absolute production root would be consulted.
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
    # No pre-flight artifact exists under the relocated tree, so the gate must
    # degrade rather than empty the buy list -- the whole non-blocking contract,
    # exercised through the real config for once.
    assert document["preflight_gate"]["preflight_found"] is False


def test_a_truncated_trend_window_is_withheld(tmp_path):
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
    document = _run(data, _config(tmp_path))
    row = _only(document)
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["trend_validated"] is True
    assert document["withheld_incomplete_trend_window_tickers"] == ["TEST"]
    assert document["cands"] == []
    assert document["same_day_top_up_candidates"] == []


def test_a_full_trend_window_still_publishes(tmp_path):
    """The control: bars from the open, which at 10:30 is the full window."""
    document = _run(_market_data(TEST=_ticker_payload()), _config(tmp_path))
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]


# --- the pre-flight gate must not let gated-out evidence abort the run ------


def test_a_gated_out_tickers_malformed_payload_does_not_abort(tmp_path):
    """MorningDCAExecutor.evaluate() narrows to the 09:45 verdict before it
    scores anything, so a payload for a GATED-OUT ticker is evidence the executor
    never reads. Validating and parsing it anyway let one malformed gated-out
    payload abort a run the executor would have completed -- and the prompt tells
    the caller to fetch the ENTIRE active universe, so gated-out evidence is the
    normal case, not the exception (Codex review: "Apply preflight exclusions
    before parsing payloads"; reproduced before fixing)."""
    _write_preflight(tmp_path / "preflight", {"TEST": True, "GATED": False})
    config = _config(tmp_path, tickers=("TEST", "GATED"))
    data = _market_data(
        TEST=_ticker_payload(),
        # Truncated: previous_close_bar only, no intraday bars.
        GATED={"previous_close_bar": _previous_close_bar(100.0, 97.0)},
    )
    document = _run(data, config)
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]
    assert document["preflight_gate"]["gated_out_tickers"] == ["GATED"]


def test_an_admitted_tickers_malformed_payload_still_aborts(tmp_path):
    """The guard against over-correcting: narrowing must drop only what the
    executor would never look at. A truncated payload for an ADMITTED ticker is
    still a broken fetch and must still fail closed."""
    _write_preflight(tmp_path / "preflight", {"TEST": True, "GATED": False})
    config = _config(tmp_path, tickers=("TEST", "GATED"))
    data = _market_data(
        TEST={"previous_close_bar": _previous_close_bar(100.0, 97.0)},
        GATED=_ticker_payload(),
    )
    with pytest.raises(ValueError, match="incomplete payload"):
        _run(data, config)


def test_with_no_preflight_artifact_every_payload_is_still_validated(tmp_path):
    """The gate is non-blocking: a missing artifact degrades to the FULL
    universe, so nothing is gated out and every payload must still be checked.
    Narrowing must not become a way to skip validation on an ordinary run."""
    config = _config(tmp_path, tickers=("TEST", "OTHER"))
    data = _market_data(
        TEST=_ticker_payload(),
        OTHER={"previous_close_bar": _previous_close_bar(100.0, 97.0)},
    )
    with pytest.raises(ValueError, match="incomplete payload"):
        _run(data, config)


def test_the_spec_carries_the_preflight_universe_filter():
    """Pins the wiring: only this slot sets one, and it must be the shared
    implementation rather than a second definition of eligibility."""
    assert gmc.MORNING_SPEC.universe_filter is gmc.preflight_admitted_tickers
