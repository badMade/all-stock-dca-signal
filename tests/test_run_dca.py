"""Tests for the orchestrator + CLI (run_dca)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from dca_config import load_strategy_config
from near_close_dca import EASTERN, DryRunBroker, SourceName, StrategyConfig, StrategyState, build_broker
from run_dca import (
    DemoDataProvider,
    _demo_runtime,
    _fills_from_orders_csv,
    _final_buy_trend_override_window_start,
    _is_within,
    _resolve_now,
    _slot_time_et,
    configure_logging,
    main,
    run_pipeline,
)


def _now(day: tuple[int, int, int] = (2026, 7, 6)):
    return datetime.combine(date(*day), time(15, 45), tzinfo=cast(Any, EASTERN))


def _now_for_slot(slot, day: tuple[int, int, int] = (2026, 7, 6)):
    """``now`` at exactly ``slot``'s own scheduled ET time, so a run_slot() call
    under test passes _validate_slot_schedule's +/-5-minute window check."""
    return datetime.combine(date(*day), _slot_time_et(slot), tzinfo=cast(Any, EASTERN))


def _demo_config():
    # Approve the demo trading day regardless of the bundled window.
    return load_strategy_config(approved_test_dates=frozenset({_now().date()}))


def test_demo_runtime():
    config = StrategyConfig(decision_time_et=time(15, 45))
    finance, robinhood, broker = _demo_runtime(config)

    assert isinstance(finance, DemoDataProvider)
    assert finance.source == SourceName.FINANCE

    assert isinstance(robinhood, DemoDataProvider)
    assert robinhood.source == SourceName.ROBINHOOD

    assert isinstance(broker, DryRunBroker)


def test_resolve_now_with_naive_string():
    config = StrategyConfig(decision_time_et=time(15, 45))
    dt = _resolve_now("2026-07-06T12:00:00", config)
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 6
    assert dt.hour == 12
    assert dt.minute == 0
    assert dt.tzinfo == EASTERN


def test_resolve_now_with_aware_string():
    config = StrategyConfig(decision_time_et=time(15, 45))
    dt = _resolve_now("2026-07-06T12:00:00+00:00", config)
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 6
    assert dt.hour == 12
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.tzinfo == timezone.utc


def test_resolve_now_with_zulu_suffix():
    """``--now`` must accept an RFC3339 'Z' suffix.

    Every scheduled DCA task passes ``--now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"``,
    so the CLI has to normalise a trailing 'Z' the same way the rest of the
    codebase already does (``dca_live_adapters._parse_rfc3339``,
    ``scripts/run_classification._parse_iso_datetime``). ``fromisoformat`` only
    learned 'Z' in Python 3.11, so calling it bare makes the entry point
    silently interpreter-version dependent.
    """
    config = StrategyConfig(decision_time_et=time(15, 45))
    dt = _resolve_now("2026-07-06T12:00:00Z", config)
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 6
    assert dt.hour == 12
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.utcoffset() == timedelta(0)
    # Identical instant to the explicit-offset spelling.
    assert dt == _resolve_now("2026-07-06T12:00:00+00:00", config)


def test_resolve_now_with_zulu_suffix_and_fractional_seconds():
    """A 'Z' timestamp carrying fractional seconds parses to the same instant."""
    config = StrategyConfig(decision_time_et=time(15, 45))
    dt = _resolve_now("2026-07-06T12:00:00.250000Z", config)
    assert dt.utcoffset() == timedelta(0)
    assert dt.microsecond == 250000


def test_resolve_now_rejects_malformed_value():
    """A malformed ``--now`` still fails loudly rather than being coerced."""
    config = StrategyConfig(decision_time_et=time(15, 45))
    with pytest.raises(ValueError):
        _resolve_now("not-a-timestamp", config)


@patch("run_dca.datetime")
def test_resolve_now_without_arg(mock_datetime):
    # Setup mock to return a specific datetime for now()
    mock_now = datetime(2026, 7, 6, 12, 0, tzinfo=EASTERN)
    mock_datetime.now.return_value = mock_now
    # We also need to mock combine since we patched datetime
    mock_datetime.combine = datetime.combine

    config = StrategyConfig(decision_time_et=time(15, 45))
    dt = _resolve_now(None, config)

    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 6
    assert dt.hour == 15
    assert dt.minute == 45
    assert dt.tzinfo == EASTERN


def test_is_within():
    """Test _is_within correctly identifies path hierarchy."""
    base = Path("/a/b")

    # Path is the same as ancestor
    assert _is_within(Path("/a/b"), base) is True

    # Path is nested under ancestor
    assert _is_within(Path("/a/b/c"), base) is True
    assert _is_within(Path("/a/b/c/d"), base) is True

    # Path is disjoint from ancestor
    assert _is_within(Path("/a/c"), base) is False
    assert _is_within(Path("/d/e"), base) is False

    # Path is parent of ancestor
    assert _is_within(Path("/a"), base) is False
    assert _is_within(Path("/"), base) is False


def test_slot_time_et():
    from run_dca import _slot_time_et
    assert _slot_time_et("1030ET") == time(10, 30)
    assert _slot_time_et("1530ET") == time(15, 30)
    assert _slot_time_et("0935ET") == time(9, 35)

    # without ET
    assert _slot_time_et("1030") == time(10, 30)
    assert _slot_time_et("0935") == time(9, 35)


def test_demo_provider_produces_downtrend_bars():
    prov = DemoDataProvider(SourceName.FINANCE)
    start = datetime.combine(datetime(2026, 7, 6).date(), time(9, 30), tzinfo=EASTERN)
    bars = prov.get_intraday_bars("NVDA", start, _now(), 5)
    assert len(bars) > 1
    assert bars[-1].close < bars[0].close  # trend down


def test_run_pipeline_writes_artifacts_and_reports(tmp_path):
    config = _demo_config()
    finance = DemoDataProvider(SourceName.FINANCE)
    robinhood = DemoDataProvider(SourceName.ROBINHOOD)
    broker = build_broker(config)
    result = run_pipeline(
        _now(), config, StrategyState(), finance, robinhood, broker,
        artifacts_dir=tmp_path, generate_full_reports=True,
    )
    assert result["blocked"] is False
    for key in ("run_state_path", "signals_path", "buy_candidates_path", "orders_csv_path"):
        assert (tmp_path / result[key].split("/")[-1]).exists()
    assert "reports" in result and "notion_sync" in result
    assert (tmp_path / "latest_run_summary.json").exists()
    assert (tmp_path / "slippage_and_coverage_report.md").exists()


def test_run_pipeline_blocked_day_skips_reports(tmp_path):
    # A day outside the approved set trips the date gate and writes only the stub.
    config = load_strategy_config(approved_test_dates=frozenset())
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path, generate_full_reports=True,
    )
    assert result["blocked"] is True
    assert "reports" not in result


def test_run_pipeline_does_not_write_model_usage_log_by_default(tmp_path):
    # model_usage_log_root defaults to None -- run_pipeline() must not touch
    # any model-usage path unless a caller opts in explicitly.
    config = _demo_config()
    run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )
    assert not (tmp_path / "model_usage").exists()


def test_run_pipeline_appends_model_usage_log_entry_when_root_configured(tmp_path):
    import json

    from scripts.model_usage_log import resolve_log_path

    config = _demo_config()
    log_root = tmp_path / "model_usage"
    now = _now()
    result = run_pipeline(
        now, config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path / "artifacts",
        model_usage_log_root=log_root,
    )

    log_path = resolve_log_path(now, root=log_root)
    assert log_path.exists()
    entry = json.loads(log_path.read_bytes().splitlines()[0])
    assert entry["cron_id"] == "10ce362c"
    assert entry["run_origin"] == "scheduled"
    assert entry["artifact_path"] == result["run_state_path"]
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert entry["run_id"] == run_state["run_id"]


def test_run_pipeline_records_premium_routing_metadata_when_supplied(tmp_path):
    import json

    from scripts.model_usage_log import resolve_log_path

    config = _demo_config()
    log_root = tmp_path / "model_usage"
    now = _now()
    run_pipeline(
        now, config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path / "artifacts",
        model_usage_log_root=log_root,
        model_usage_model_requested="claude-opus-4-8",
        model_usage_model_actual="claude-opus-4-8",
        model_usage_model_routing_mode="premium",
        model_usage_reason_for_premium_model="safety_validation",
    )

    log_path = resolve_log_path(now, root=log_root)
    entry = json.loads(log_path.read_bytes().splitlines()[0])
    assert entry["model_routing_mode"] == "premium"
    assert entry["reason_for_premium_model"] == "safety_validation"


def test_run_pipeline_date_gate_blocked_run_has_no_run_id_to_log(tmp_path):
    # The date-gate-blocked stub (_check_execution_gates) only ever writes
    # {"trading_day": ..., "status": "blocked"} -- no run_id was ever assigned,
    # so there is nothing to attribute a usage entry to. log_model_usage must
    # skip cleanly (not raise) rather than write a malformed entry.
    from scripts.model_usage_log import resolve_log_path

    config = load_strategy_config(approved_test_dates=frozenset())
    log_root = tmp_path / "model_usage"
    now = _now()
    result = run_pipeline(
        now, config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path / "artifacts",
        model_usage_log_root=log_root,
    )
    assert result["blocked"] is True
    log_path = resolve_log_path(now, root=log_root)
    assert not log_path.exists()


def test_run_pipeline_scheduled_run_auto_loads_prior_allocations(tmp_path):
    import json

    # Seed a manual same-day allocation under the cron task root's manual_runs/,
    # mirroring the real production layout from config/scheduled_tasks.json:
    # <task_root>/scheduled_runs/YYYYMMDD_1530ET/ (artifacts_dir) as a sibling of
    # <task_root>/manual_runs/<timestamp>/. This is nested two levels below the
    # task root, unlike a flat "scheduled" directory -- a naive
    # artifacts_dir.parent would land on scheduled_runs/ itself and never see
    # manual_runs/, so the auto-derived root must walk up past scheduled_runs/.
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    manual_dir = task_root / "manual_runs" / "0900"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T09:00:00-04:00",
            "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
            # A very low reference price guarantees the afternoon price is never
            # below it, so this ticker's guard result proves the allocation was
            # loaded (top_up_guard_result != "no_prior_run") without depending
            # on the demo provider's exact drop-derived price.
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"
    assert not (task_root / "latest_run_summary.json").exists()


def test_run_pipeline_scheduled_run_loads_intraday_recovery_top_up_context(tmp_path):
    import json

    # Jul 24 2026 intraday_1430 recovery (config/scheduled_tasks.json
    # canonical_first_buy_and_topup_rule -> intraday_dca_scheduled_recovery_2026_07_24,
    # Codex review: "Include the intraday recovery in later top-up context").
    # An ad hoc intraday scheduled-recovery stream has no dedicated *_dca.py
    # module, but writes to the same cron_tracking/<stream>_dca/ sibling
    # layout as morning_dca/midday_dca -- the afternoon runner must fold its
    # fills in as prior-allocation context even though its run_origin is
    # 'scheduled' (it is scheduled evidence for ITS OWN stream only).
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    intraday_dir = tmp_path / "cron_tracking" / "intraday_dca" / "20260706_1430ET"
    intraday_dir.mkdir(parents=True)
    (intraday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T14:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "intraday_1430",
                "run_id": "intraday_live_20260706_1430ET",
            },
            # A very low reference price guarantees the afternoon price is
            # never below it, so this ticker's guard result proves the
            # recovery allocation was loaded without depending on the demo
            # provider's exact drop-derived price.
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_run_pipeline_scheduled_run_loads_market_open_top_up_context(tmp_path):
    import json

    # Codex review discussion_r3653976738 ("Include market-open fills in later
    # top-up context"): the new 9:35 AM market-open task (2ba19526, stream
    # 'market_open') is the first buy of the day, and its fills must count as
    # prior-allocation context for every later same-day run -- including the
    # 3:30 afternoon final. Before the patch, _prior_run_is_scheduled classified
    # a market_open-stream scheduled run the same as the excluded
    # scheduled-afternoon run, and market_open_dca was absent from
    # _CROSS_STREAM_PRIOR_ALLOCATION_ROOTS, so this fill was never loaded and
    # the afternoon run could re-submit AAPL's full first allocation.
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    market_open_dir = tmp_path / "cron_tracking" / "market_open_dca" / "20260706_0935ET"
    market_open_dir.mkdir(parents=True)
    (market_open_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T09:35:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "market_open",
                "run_id": "market_open_live_20260706_0935ET",
            },
            # A very low reference price guarantees the afternoon price is
            # never below it, so this ticker's guard result proves the
            # market-open allocation was loaded without depending on the demo
            # provider's exact drop-derived price.
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_run_pipeline_scheduled_run_loads_manual_midday_top_up_context(tmp_path):
    import json

    # Jul 21 2026 incident regression (PR #157 review: "Wire the manual context
    # loader into the 3:20 pipeline"). A manual midday run whose fills live ONLY
    # in a sibling submitted_orders.csv -- no embedded buy_candidates, no
    # recorded artifacts.orders_csv_path -- is a shape
    # load_prior_same_day_allocations[_multi] cannot read a fill from (no
    # buy_candidates and no artifacts block to fall back to). Only
    # scripts.run_classification.load_same_day_top_up_context, wired in via
    # _merge_manual_top_up_context, can see it.
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    manual_midday_dir = (
        tmp_path / "cron_tracking" / "midday_dca" / "manual_runs" / "20260706_1345ET"
    )
    manual_midday_dir.mkdir(parents=True)
    (manual_midday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T13:45:00-04:00",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "run_id": "manual_midday_20260706_1345ET",
                "stream": "midday_manual",
                "trading_day": _now().date().isoformat(),
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
                "valid_afternoon_scheduled_evidence": False,
            },
        }),
        encoding="utf-8",
    )
    # A very low reference price guarantees the afternoon price is never below
    # it, so this ticker's guard result proves the allocation was loaded
    # (top_up_guard_result != "no_prior_run") without depending on the demo
    # provider's exact drop-derived price.
    (manual_midday_dir / "submitted_orders.csv").write_text(
        "symbol,amount,order_id,status,average_price,quantity\n"
        "AAPL,3.0,MID-AAPL-1,filled,0.01,0.1\n",
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_run_pipeline_honors_configured_morning_artifact_root(tmp_path):
    # Codex/Copilot review: the cross-stream root derivation used to assume the
    # morning/midday roots are siblings of the afternoon task root sharing the
    # DEFAULT_*_STATE_ROOT constant's directory name. A relocated morning root
    # (a supported override via artifact_paths.live_run_state_root in
    # config/morning_dca.json) would be silently missed, so the afternoon
    # top-up guard would never see that day's morning fills. Seed a morning
    # run_state.json under a deliberately non-sibling relocated root and point
    # config_dir at a morning_dca.json that overrides live_run_state_root there.
    import json

    relocated_morning_root = tmp_path / "elsewhere" / "morning_live"
    morning_run_dir = relocated_morning_root / "20260706_1030ET"
    morning_run_dir.mkdir(parents=True)
    (morning_run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T10:30:00-04:00",
            "run_classification": {"run_origin": "scheduled", "run_id": "morning_am", "stream": "morning"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(relocated_morning_root)}}),
        encoding="utf-8",
    )

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir, config_dir=str(config_dir),
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_run_pipeline_preserves_sibling_root_fallback_when_tree_is_relocated(tmp_path):
    # Codex follow-up review: correcting the above (honoring a stream's configured
    # root) regressed the OTHER case -- a caller that relocates the WHOLE
    # cron-tracking tree via a non-production artifacts_dir (tests, demo,
    # staging) without also pointing config_dir at a matching relocated
    # config. Reading the configured root alone would resolve to the bundled
    # production default (e.g. /home/user/workspace/...) instead of the real
    # sibling morning root under this relocated tree, silently missing it.
    # No config_dir is passed here (mirrors a caller that never wires one),
    # and the seeded morning artifact lives at the sibling-of-afternoon-root
    # convention path, not the production default.
    import json

    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    # Sibling of task_root's parent (cron_base), matching DEFAULT_MORNING_LIVE_STATE_ROOT's
    # directory name ("morning_dca") under this relocated tree.
    sibling_morning_root = tmp_path / "cron_tracking" / "morning_dca"
    morning_run_dir = sibling_morning_root / "20260706_1030ET"
    morning_run_dir.mkdir(parents=True)
    (morning_run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T10:30:00-04:00",
            "run_classification": {"run_origin": "scheduled", "run_id": "morning_am", "stream": "morning"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_resolve_cross_stream_prior_allocation_roots_excludes_mismatched_default(tmp_path):
    # Codex follow-up review ("Keep relocated runs from scanning production
    # roots"): when config_dir is left unset (None) and this run's
    # cron-tracking tree is relocated (tests/demo/staging), load_morning_config/
    # load_midday_config still resolve to the bundled production defaults
    # (DEFAULT_MORNING_LIVE_STATE_ROOT / DEFAULT_MIDDAY_LIVE_STATE_ROOT) since
    # that's what ships in config/morning_dca.json and config/midday_dca.json.
    # Folding those in verbatim would let a relocated run offset its own
    # orders against a totally different (real production) environment's
    # same-day fills. They must be excluded here; the sibling-of-afternoon-root
    # fallback still covers this relocated tree's own morning/midday siblings.
    from pathlib import Path

    from midday_dca import DEFAULT_MIDDAY_LIVE_STATE_ROOT
    from morning_dca import DEFAULT_MORNING_LIVE_STATE_ROOT
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    # afternoon_root is the already-resolved cron TASK root (as produced by
    # _default_prior_allocation_state_root), not the raw artifacts_dir.
    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert Path(DEFAULT_MORNING_LIVE_STATE_ROOT).resolve() not in resolved
    assert Path(DEFAULT_MIDDAY_LIVE_STATE_ROOT).resolve() not in resolved
    assert (afternoon_root.parent / "morning_dca").resolve() in resolved
    assert (afternoon_root.parent / "midday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_env_dir_without_override_stays_contained(
    tmp_path, monkeypatch
):
    # Codex review ("Keep relocated runs isolated after env-config fallback"):
    # DCA_CONFIG_DIR pointing at a real, existing directory is not by itself
    # proof that a stream's root was actually customized for this
    # environment -- if that directory has no morning_dca.json/midday_dca.json
    # (or the file doesn't override live_run_state_root), load_morning_config/
    # load_midday_config still fall back to the bundled production default
    # roots. Trusting DCA_CONFIG_DIR unconditionally would admit those
    # production defaults for a relocated demo/staging tree exactly like the
    # no-config-at-all case above; the env var must only grant trust for a
    # stream whose own config actually set an explicit root.
    from pathlib import Path

    from midday_dca import DEFAULT_MIDDAY_LIVE_STATE_ROOT
    from morning_dca import DEFAULT_MORNING_LIVE_STATE_ROOT
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    env_config_dir = tmp_path / "env_config"
    env_config_dir.mkdir()  # exists, but ships no morning_dca.json/midday_dca.json
    monkeypatch.setenv("DCA_CONFIG_DIR", str(env_config_dir))

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert Path(DEFAULT_MORNING_LIVE_STATE_ROOT).resolve() not in resolved
    assert Path(DEFAULT_MIDDAY_LIVE_STATE_ROOT).resolve() not in resolved
    assert (afternoon_root.parent / "morning_dca").resolve() in resolved
    assert (afternoon_root.parent / "midday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_env_dir_with_override_is_trusted(
    tmp_path, monkeypatch
):
    # Mirror image of the above: when DCA_CONFIG_DIR's morning_dca.json DOES
    # set an explicit live_run_state_root, that root is this stream's real
    # configured location and must be trusted (included) even though it lives
    # outside the afternoon run's cron_base tree.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    env_config_dir = tmp_path / "env_config"
    env_config_dir.mkdir()
    custom_morning_root = tmp_path / "elsewhere" / "morning_live"
    (env_config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(custom_morning_root)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(env_config_dir))

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert custom_morning_root.resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_env_dir_empty_override_stays_contained(
    tmp_path, monkeypatch
):
    # Codex review: an explicit but EMPTY live_run_state_root ("") is not None,
    # so a `loader_root is not None` trust check alone would treat it as an
    # explicit override -- while `Path(loader_root or default_root)` (loader_root
    # falsy) actually resolves to the untouched production default root, exactly
    # like the no-override case above. An empty string must be trusted the same
    # as no override at all: still subject to containment.
    import json
    from pathlib import Path

    from midday_dca import DEFAULT_MIDDAY_LIVE_STATE_ROOT
    from morning_dca import DEFAULT_MORNING_LIVE_STATE_ROOT
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    env_config_dir = tmp_path / "env_config"
    env_config_dir.mkdir()
    (env_config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": ""}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(env_config_dir))

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert Path(DEFAULT_MORNING_LIVE_STATE_ROOT).resolve() not in resolved
    assert Path(DEFAULT_MIDDAY_LIVE_STATE_ROOT).resolve() not in resolved
    assert (afternoon_root.parent / "morning_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_prunes_nested_configured_root(tmp_path):
    # Codex review ("Avoid double-counting nested stream roots"): a configured
    # live_run_state_root that is a CHILD of the sibling-of-afternoon-root
    # convention path (e.g. <cron_base>/morning_dca/live) used to be kept
    # alongside its parent because dedup only compared exact resolved paths.
    # load_prior_same_day_allocations{,_multi} scan every root recursively, so
    # keeping both means the same run_state.json under the child is loaded
    # twice, doubling the afternoon top-up guard's prior-allocation amount and
    # shrinking or suppressing valid afternoon buys. Only the ancestor root
    # should survive.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    # afternoon_root is the already-resolved cron TASK root (as produced by
    # _default_prior_allocation_state_root), not the raw artifacts_dir.
    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    nested_morning_root = cron_base / "morning_dca" / "live"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(nested_morning_root)}}),
        encoding="utf-8",
    )

    roots = _resolve_cross_stream_prior_allocation_roots(str(config_dir), afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert nested_morning_root.resolve() not in resolved
    assert (cron_base / "morning_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_excludes_afternoon_tree(tmp_path):
    # Codex review ("Exclude the afternoon tree from cross-stream roots"): a
    # configured morning/midday root that is an ancestor of afternoon_root
    # (e.g. the shared cron_base itself) must be dropped, since run_pipeline
    # scans afternoon_root separately and sums that result with this
    # function's roots -- keeping an ancestor would recursively re-scan (and
    # double-count) the same afternoon tree.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(cron_base)}}),
        encoding="utf-8",
    )

    roots = _resolve_cross_stream_prior_allocation_roots(str(config_dir), afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert cron_base.resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_afternoon_containing_root_keeps_sibling_fallback(
    tmp_path,
):
    # Codex follow-up review ("Filter afternoon-containing roots before
    # descendant pruning"): a residual case after the previous adjustment. When a
    # stream is configured to the shared cron_base itself, the sibling
    # cron_base/morning_dca root is a genuine DESCENDANT of cron_base, so the
    # descendant-pruning pass would discard it as "redundant" with cron_base
    # as its surviving ancestor -- while the afternoon-containment pass
    # separately discards cron_base itself for containing afternoon_root.
    # Run in the wrong order (or combined into one comparison), BOTH roots
    # are lost and morning fills go completely unscanned. The
    # afternoon-containment filter must run first so the sibling fallback
    # never sees cron_base as a surviving ancestor to be pruned against.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(cron_base)}}),
        encoding="utf-8",
    )

    roots = _resolve_cross_stream_prior_allocation_roots(str(config_dir), afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert cron_base.resolve() not in resolved
    assert (cron_base / "morning_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_sibling_fallback(tmp_path):
    # With no DCA_INTRADAY_LIVE_STATE_ROOT override, the intraday recovery
    # root still falls back to the sibling-of-afternoon-root convention, same
    # as morning/midday, so the bundled production layout keeps working.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (afternoon_root.parent / "intraday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_env_override_is_trusted(
    tmp_path, monkeypatch
):
    # Codex review ("Make the intraday recovery root configurable"): unlike
    # morning/midday, there is no IntradayConfig loader to source a
    # deployment-specific root from, so a deployment that relocates its
    # afternoon and intraday artifact trees independently has no way to
    # override DEFAULT_INTRADAY_LIVE_STATE_ROOT's hard-coded basename-under-
    # afternoon_root.parent convention. DCA_INTRADAY_LIVE_STATE_ROOT must be
    # trusted directly even though it lives entirely outside afternoon_root's
    # cron_base -- that's the whole point of a relocated tree.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    custom_intraday_root = tmp_path / "elsewhere" / "intraday_live"
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(custom_intraday_root))

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert custom_intraday_root.resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_env_nested_under_afternoon_excluded(
    tmp_path, monkeypatch
):
    # Codex review ("Exclude intraday roots nested under the afternoon
    # tree"): _compute_auto_derived_prior_allocations already scans
    # afternoon_root recursively on its own. If DCA_INTRADAY_LIVE_STATE_ROOT
    # is set to a path NESTED under afternoon_root (an operator
    # misconfiguration), keeping it as an ALSO-scanned cross-stream root
    # would load every run_state.json beneath it twice and double the
    # afternoon top-up guard's prior-allocation amount for those tickers.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    nested_intraday_root = afternoon_root / "intraday_subdir"
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(nested_intraday_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert nested_intraday_root.resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_env_nested_under_afternoon_drops_fallback(
    tmp_path, monkeypatch
):
    # Codex review ("Drop the legacy fallback for covered nested
    # overrides"): DCA_INTRADAY_LIVE_STATE_ROOT is nested under
    # afternoon_root, so the caller's own recursive afternoon scan already
    # covers it (the test above confirms it's correctly excluded as a
    # separate scan entry). But that's a genuinely CONFIGURED override, just
    # one that doesn't need its own scan entry -- not the absence of an
    # override. Treating "doesn't need its own entry" as "no override was
    # configured" incorrectly resurrected the conventional
    # cron_base/intraday_dca fallback; if that stale legacy tree still held
    # artifacts from before the relocation, they'd be aggregated on top of
    # the (already-covered) real data, double-counting and potentially
    # suppressing valid top-ups.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    nested_intraday_root = afternoon_root / "intraday_subdir"
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(nested_intraday_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "intraday_dca").resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_override_replaces_sibling_fallback(
    tmp_path, monkeypatch
):
    # Codex review ("Replace the intraday fallback when an override is
    # set"): once DCA_INTRADAY_LIVE_STATE_ROOT points at a genuinely
    # relocated tree, the old cron_base/intraday_dca sibling-convention
    # path is very likely stale leftover data, not a legitimate second
    # source. Keeping both would double-count whatever each location still
    # holds -- the override must REPLACE the sibling fallback, not add to it.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    custom_intraday_root = tmp_path / "elsewhere" / "intraday_live"
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(custom_intraday_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert custom_intraday_root.resolve() in resolved
    assert (afternoon_root.parent / "intraday_dca").resolve() not in resolved
    # Morning/midday sibling fallbacks are untouched by the intraday override.
    assert (afternoon_root.parent / "morning_dca").resolve() in resolved
    assert (afternoon_root.parent / "midday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_conflicting_intraday_override_keeps_sibling_fallback(
    tmp_path, monkeypatch
):
    # Codex review ("Keep the intraday fallback when filtering the
    # override"): when DCA_INTRADAY_LIVE_STATE_ROOT is set to a path that
    # CONFLICTS with afternoon_root (here, the shared cron_base itself --
    # an ancestor of afternoon_root), the bidirectional afternoon-
    # containment filter correctly drops the override. Deciding to suppress
    # the conventional cron_base/intraday_dca sibling fallback BEFORE that
    # filtering runs (purely because the env var was set) would leave
    # NEITHER root scanned. The fallback must survive since the override
    # never actually made it into the returned roots.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(cron_base))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert cron_base.resolve() not in resolved  # the conflicting override is dropped
    assert (cron_base / "intraday_dca").resolve() in resolved  # fallback survives


def test_resolve_cross_stream_prior_allocation_roots_intraday_override_equals_afternoon_drops_fallback(
    tmp_path, monkeypatch
):
    # Codex review ("Treat an override equal to the afternoon root as
    # covered"): DCA_INTRADAY_LIVE_STATE_ROOT is set to EXACTLY
    # afternoon_root. The prior ancestor-or-equal check treated this the
    # same as the genuine cron_base-ancestor misconfiguration above,
    # resurrecting the legacy cron_base/intraday_dca fallback -- but an
    # override equal to afternoon_root is trivially covered by the
    # caller's own recursive afternoon scan, exactly like a nested
    # override, not a meaningless whole-tree misconfiguration like an
    # ancestor. If the legacy fallback location still holds stale
    # artifacts, they'd be aggregated on top of the already-covered data.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(afternoon_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "intraday_dca").resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_override_covered_by_ancestor_drops_fallback(
    tmp_path, monkeypatch
):
    # Codex review ("Drop the intraday fallback when an ancestor covers the
    # override"): DCA_INTRADAY_LIVE_STATE_ROOT is set to a path NESTED under
    # the morning stream's own (relocated) configured root. Pass 3 correctly
    # prunes the override itself as redundant -- its ancestor already scans
    # it recursively -- but that ancestor's ongoing coverage means the
    # override's real intent (a relocated tree) still holds, so the now-
    # genuinely-redundant conventional cron_base/intraday_dca fallback must
    # still be dropped. An exact-path "did the override survive" check would
    # wrongly conclude it didn't (only its ancestor remains) and keep the
    # stale fallback.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    relocated_root = cron_base / "relocated_all_streams"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(relocated_root)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(relocated_root / "intraday_dca"))

    roots = _resolve_cross_stream_prior_allocation_roots(str(config_dir), afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert relocated_root.resolve() in resolved  # the ancestor covering the override
    assert (cron_base / "intraday_dca").resolve() not in resolved  # stale fallback dropped


def test_resolve_cross_stream_prior_allocation_roots_intraday_override_equals_fallback_survives(
    tmp_path, monkeypatch
):
    # Codex review ("Preserve an override that equals the intraday
    # fallback"): DCA_INTRADAY_LIVE_STATE_ROOT is explicitly set to the exact
    # conventional cron_base/intraday_dca path. Pass 1 dedupes the override
    # and the sibling fallback into ONE surviving entry (they're the same
    # path); removing "every entry equal to the fallback key" would delete
    # that sole entry too, leaving NOTHING scanned for intraday at all.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(cron_base / "intraday_dca"))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "intraday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_intraday_override_nested_under_fallback_scans_only_override(
    tmp_path, monkeypatch
):
    # Codex review ("Scan only the nested intraday override"): when
    # DCA_INTRADAY_LIVE_STATE_ROOT is set to a path NESTED under the
    # conventional cron_base/intraday_dca fallback (e.g. a "relocated"
    # subdirectory), the override must be scanned INSTEAD of the broader
    # fallback, not merely alongside/covered-by it. An earlier iteration kept the
    # broader fallback in this case (to avoid losing coverage entirely), but
    # that scans every legacy artifact elsewhere under cron_base/intraday_dca
    # too -- not just the relocated tree the operator configured -- which can
    # double-count or pull in stale data during a migration where old and
    # relocated copies coexist. The upfront override-vs-fallback decision
    # means the fallback is never a candidate at all once the override is
    # active, so the narrower override survives on its own.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    nested_override = cron_base / "intraday_dca" / "relocated"
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(nested_override))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert nested_override.resolve() in resolved
    assert (cron_base / "intraday_dca").resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_sibling_fallback(tmp_path):
    # With no DCA_MARKET_OPEN_LIVE_STATE_ROOT override, the market-open root
    # still falls back to the sibling-of-afternoon-root convention, same as
    # morning/midday/intraday, so the bundled production layout keeps
    # working (Codex review: "Feed market-open fills into later top-up
    # guards").
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (afternoon_root.parent / "market_open_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_env_override_is_trusted(
    tmp_path, monkeypatch
):
    # Mirrors the intraday env-override test: there is no MarketOpenConfig
    # loader, so DCA_MARKET_OPEN_LIVE_STATE_ROOT must be trusted directly
    # even when it lives entirely outside afternoon_root's cron_base.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    custom_root = tmp_path / "elsewhere" / "market_open_live"
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(custom_root))

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert custom_root.resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_env_nested_under_afternoon_excluded(
    tmp_path, monkeypatch
):
    # Mirrors the intraday nested-under-afternoon test: a market-open root
    # nested under afternoon_root would otherwise be double-scanned since
    # the caller already recursively scans afternoon_root itself.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    nested_root = afternoon_root / "market_open_subdir"
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(nested_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert nested_root.resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_env_nested_under_afternoon_drops_fallback(
    tmp_path, monkeypatch
):
    # Mirrors the intraday test of the same name: a genuinely configured but
    # already-covered (nested) override must still suppress the legacy
    # cron_base/market_open_dca fallback, not resurrect it.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    nested_root = afternoon_root / "market_open_subdir"
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(nested_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "market_open_dca").resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_override_replaces_sibling_fallback(
    tmp_path, monkeypatch
):
    # Mirrors the intraday override-replaces-fallback test: once
    # DCA_MARKET_OPEN_LIVE_STATE_ROOT points at a genuinely relocated tree,
    # the override must REPLACE the sibling fallback, not add to it, and
    # morning/midday/intraday fallbacks are untouched.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    custom_root = tmp_path / "elsewhere" / "market_open_live"
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(custom_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert custom_root.resolve() in resolved
    assert (afternoon_root.parent / "market_open_dca").resolve() not in resolved
    assert (afternoon_root.parent / "morning_dca").resolve() in resolved
    assert (afternoon_root.parent / "midday_dca").resolve() in resolved
    assert (afternoon_root.parent / "intraday_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_conflicting_market_open_override_keeps_sibling_fallback(
    tmp_path, monkeypatch
):
    # Mirrors the intraday conflicting-override test: when
    # DCA_MARKET_OPEN_LIVE_STATE_ROOT conflicts with afternoon_root (set to
    # the shared cron_base, an ancestor of afternoon_root), the fallback
    # must survive since the override never makes it into the returned roots.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(cron_base))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert cron_base.resolve() not in resolved  # the conflicting override is dropped
    assert (cron_base / "market_open_dca").resolve() in resolved  # fallback survives


def test_resolve_cross_stream_prior_allocation_roots_market_open_override_equals_afternoon_drops_fallback(
    tmp_path, monkeypatch
):
    # Mirrors the intraday override-equals-afternoon test: an override equal
    # to afternoon_root is trivially covered by the caller's own recursive
    # afternoon scan, so the legacy sibling fallback must still be dropped.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(afternoon_root))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "market_open_dca").resolve() not in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_override_covered_by_ancestor_drops_fallback(
    tmp_path, monkeypatch
):
    # Mirrors the intraday override-covered-by-ancestor test: a market-open
    # override nested under the morning stream's own (relocated) configured
    # root is pruned as redundant in Pass 3, but the now-genuinely-redundant
    # legacy cron_base/market_open_dca fallback must still be dropped.
    import json

    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    relocated_root = cron_base / "relocated_all_streams"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"live_run_state_root": str(relocated_root)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(relocated_root / "market_open_dca"))

    roots = _resolve_cross_stream_prior_allocation_roots(str(config_dir), afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert relocated_root.resolve() in resolved  # the ancestor covering the override
    assert (cron_base / "market_open_dca").resolve() not in resolved  # stale fallback dropped


def test_resolve_cross_stream_prior_allocation_roots_market_open_override_equals_fallback_survives(
    tmp_path, monkeypatch
):
    # Mirrors the intraday override-equals-fallback test: Pass 1 dedupes the
    # override and the sibling fallback into ONE surviving entry since
    # they're the same path.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(cron_base / "market_open_dca"))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert (cron_base / "market_open_dca").resolve() in resolved


def test_resolve_cross_stream_prior_allocation_roots_market_open_override_nested_under_fallback_scans_only_override(
    tmp_path, monkeypatch
):
    # Mirrors the intraday override-nested-under-fallback test: the override
    # must be scanned INSTEAD of the broader fallback, not merely
    # alongside/covered-by it.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    afternoon_root = tmp_path / "cron_tracking" / "ce16ba6b"
    cron_base = afternoon_root.parent
    nested_override = cron_base / "market_open_dca" / "relocated"
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", str(nested_override))

    roots = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    resolved = {r.resolve() for r in roots}
    assert nested_override.resolve() in resolved
    assert (cron_base / "market_open_dca").resolve() not in resolved


def test_run_pipeline_explicit_prior_root_uses_run_time_boundary(tmp_path):
    import json

    prior_root = tmp_path / "custom_prior_root"
    manual_dir = prior_root / "manual_runs" / "1505"
    manual_dir.mkdir(parents=True)
    now = _now()
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": now.date().isoformat(),
            "generated_at": datetime.combine(now.date(), time(15, 5), tzinfo=EASTERN).isoformat(),
            "run_classification": {"run_origin": "manual", "run_id": "manual_1505"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    result = run_pipeline(
        now, config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path / "scheduled",
        prior_allocation_state_root=prior_root,
    )

    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"


def test_run_pipeline_explicit_prior_root_does_not_leak_sibling_manual_context(tmp_path):
    import json

    # Codex review r3626083298 ("Respect explicit prior-allocation root
    # isolation"): when a caller passes prior_allocation_state_root explicitly,
    # manual top-up context discovery must not reach outside that root. Seed a
    # manual midday run as a SIBLING of the explicit root's parent -- exactly
    # the shape scripts.run_classification.load_same_day_top_up_context would
    # find if it were (incorrectly) invoked with base=prior_allocation_state_root.
    # No buy_candidates/artifacts.orders_csv_path, so near_close_dca's own
    # loader can't see it either -- only a leak through the new loader would.
    explicit_root = tmp_path / "custom_prior_root"
    explicit_root.mkdir(parents=True)
    sibling_manual_dir = tmp_path / "midday_dca" / "manual_runs" / "20260706_1345ET"
    sibling_manual_dir.mkdir(parents=True)
    now = _now()
    (sibling_manual_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": now.date().isoformat(),
            "generated_at": f"{now.date().isoformat()}T13:45:00-04:00",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "run_id": "manual_midday_20260706_1345ET",
                "trading_day": now.date().isoformat(),
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
                "valid_afternoon_scheduled_evidence": False,
            },
        }),
        encoding="utf-8",
    )
    (sibling_manual_dir / "submitted_orders.csv").write_text(
        "symbol,amount,order_id,status,average_price,quantity\n"
        "IBM,2.0,SIBLING-IBM-1,filled,0.01,0.1\n",
        encoding="utf-8",
    )

    config = _demo_config()
    result = run_pipeline(
        now, config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path / "scheduled",
        prior_allocation_state_root=explicit_root,
    )

    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["IBM"]["top_up_guard_result"] == "no_prior_run"


def test_run_pipeline_scheduled_run_loads_sibling_stream_roots_to_run_time(tmp_path):
    import json

    # The afternoon scheduled artifacts can live under a non-standard cron_tracking
    # base. Sibling morning/midday roots must be derived from that local base, and
    # scans must include manual/live fills recorded after the historical 14:30 ET
    # cutoff but before the actual afternoon run time.
    cron_base = tmp_path / "custom_cron_tracking"
    afternoon_root = cron_base / "ce16ba6b"
    midday_root = cron_base / "midday_dca"
    prior_dir = midday_root / "manual_reruns" / "1505"
    prior_dir.mkdir(parents=True)
    (prior_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T15:05:00-04:00",
            "run_classification": {"run_origin": "manual", "run_id": "midday_manual_1505"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = afternoon_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )

    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    aapl = by_ticker["AAPL"]
    # Prove the midday sibling root was scanned: the run_id must match the seeded run.
    assert aapl["prior_same_day_run_id"] == "midday_manual_1505"
    assert aapl["prior_same_day_amount"] == 3.0
    # Prove the run-time boundary (15:45) was used, not the historical 14:30 cutoff:
    # the seeded fill is at 15:05, which is after 14:30 but before 15:45.  With the
    # old 14:30 boundary the fill would have been excluded and guard_result would be
    # "no_prior_run"; with the correct run-time boundary it is found and the guard
    # blocks or reduces the afternoon buy.
    assert aapl["top_up_guard_result"] != "no_prior_run"


def test_default_prior_allocation_state_root_walks_past_scheduled_runs():
    from pathlib import Path

    from run_dca import _default_prior_allocation_state_root

    task_root = Path("/cron_tracking/ce16ba6b")
    artifacts_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    assert _default_prior_allocation_state_root(artifacts_dir) == task_root


def test_default_prior_allocation_state_root_falls_back_to_parent():
    from pathlib import Path

    from run_dca import _default_prior_allocation_state_root

    # No scheduled_runs/ segment in the path (e.g. a flat test/demo layout) ->
    # fall back to the immediate parent.
    artifacts_dir = Path("/tmp/some_root/scheduled")
    assert _default_prior_allocation_state_root(artifacts_dir) == Path("/tmp/some_root")


def test_tickers_with_unresolved_same_day_context():
    from near_close_dca import PriorSameDayAllocation
    from run_dca import _tickers_with_unresolved_same_day_context

    allocations = {
        # Known prior amount but no resolvable reference price -> unresolved.
        "AAPL": PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=None),
        # Both reference price and base resolved -> not unresolved.
        "NVDA": PriorSameDayAllocation(
            "NVDA", amount_usd=2.0, reference_price=97.0, selected_base_amount_usd=1.0
        ),
        # No prior amount at all -- not "unresolved same-day context", just no context.
        "MSFT": PriorSameDayAllocation("MSFT", amount_usd=0.0, reference_price=None),
        # Known prior amount and reference price, but no resolvable base (e.g.
        # a legacy artifact predating selected_base_amount_usd) -> unresolved,
        # same as a missing reference price (Codex review: "Fail closed when
        # the first allocation base is unknown").
        "TSLA": PriorSameDayAllocation(
            "TSLA", amount_usd=1.0, reference_price=250.0, selected_base_amount_usd=None
        ),
    }
    assert _tickers_with_unresolved_same_day_context(allocations) == ["AAPL", "TSLA"]


def test_run_pipeline_bounded_reload_resolves_unresolved_same_day_context(tmp_path, monkeypatch):
    # Canonical rule item 13: "if same-day context cannot be resolved for one
    # ticker, perform one bounded same-day context reload, then fail only
    # that ticker closed." Simulate an artifact that only becomes resolvable
    # on the second scan (e.g. a sibling stream's writer completing between
    # the first and second pass) by stubbing
    # _compute_auto_derived_prior_allocations to return an unresolved AAPL on
    # the first call and a resolved one on the second, then assert
    # run_pipeline actually used the RELOADED result rather than failing
    # AAPL's top-up closed on the first, unresolved pass.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    # **_kwargs absorbs the real _compute_auto_derived_prior_allocations()'s
    # keyword-only max_per_order_usd/unreconstructable_sink parameters (the
    # "fail same-day unknown submission amounts closed globally" fix) that
    # this and every other bounded-reload fake in this module were written
    # before -- the reload behavior under test here is orthogonal to those
    # two, so the fakes only need to keep accepting the call, not use them.
    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0
                    )
                },
                False,
            )
        # Resolved on reload: an extremely low reference price guarantees the
        # afternoon decision price is never below it, so the guard result
        # unambiguously reflects this (second-call) data being used.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=3.0, reference_price=0.01, run_id="am", source="manual",
                    selected_base_amount_usd=2.0,
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2  # exactly one bounded reload, not an unbounded retry loop
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # If the stale (unresolved) first-call result had been used instead of the
    # reload, this would be "missing_prior_price" with top_up_manual_review=True.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "same_day_topup_not_lower_or_no_delta"
    assert by_ticker["AAPL"]["top_up_manual_review"] is False


def test_compute_auto_derived_prior_allocations_reports_scan_had_failures(tmp_path):
    # Codex review ("Detect artifacts skipped during the initial context
    # scan"): a same-day artifact still being flushed mid-write parses to
    # nothing at all -- AAPL never appears in the returned mapping, not
    # merely with a missing price -- so `_tickers_with_unresolved_same_day_context`
    # alone would see an empty allocations dict and never trigger a reload.
    # Only the scan_had_failures flag this function now returns lets the
    # caller tell that apart from a genuine no-prior-run day.
    from run_dca import _compute_auto_derived_prior_allocations

    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    manual_dir = task_root / "manual_runs" / "0900"
    manual_dir.mkdir(parents=True)
    trading_day = _now().date()
    # Contains the trading_day substring (passes the fast-path filter) but is
    # truncated -- not valid JSON -- simulating a writer caught mid-flush.
    malformed_path = manual_dir / "run_state.json"
    malformed_path.write_text(
        f'{{"trading_day": "{trading_day.isoformat()}", "buy_candidates": [',
        encoding="utf-8",
    )
    # skip_sink is scoped to plausibly same-day artifacts by mtime (Codex
    # review: "Exclude historical parse failures from reload completeness");
    # stamp it onto trading_day so this synthetic fixture is recognized as one.
    import os as _os

    _stamp = datetime.combine(trading_day, time(12, 0), tzinfo=EASTERN).timestamp()
    _os.utime(malformed_path, (_stamp, _stamp))

    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    allocations, scan_had_failures = _compute_auto_derived_prior_allocations(
        _now(), trading_day, scheduled_dir, None
    )

    assert allocations == {}
    assert scan_had_failures is True


def test_compute_auto_derived_prior_allocations_sums_attempt_count_across_the_afternoon_merge(
    tmp_path,
):
    """Codex review: "Carry attempt counts through the final cross-stream
    merge". _compute_auto_derived_prior_allocations's own afternoon-root vs
    cross-stream merge (run_dca.py, not near_close_dca's
    load_prior_same_day_allocations_multi) used to construct a bare
    PriorSameDayAllocation(...) without attempt_count, silently resetting
    the aggregate to the dataclass default of 1 -- even when both sides
    genuinely contributed real, distinct attempts."""
    import json

    from run_dca import _compute_auto_derived_prior_allocations

    trading_day = _now().date()
    root = tmp_path / "cron_tracking"
    afternoon_task_root = root / "ce16ba6b"
    afternoon_dir = afternoon_task_root / "manual_runs" / "0900"
    afternoon_dir.mkdir(parents=True)
    (afternoon_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": trading_day.isoformat(),
                "generated_at": f"{trading_day.isoformat()}T09:00:00-04:00",
                "run_classification": {"run_origin": "manual", "run_id": "afternoon_manual_am"},
                "buy_candidates": [
                    {
                        "ticker": "AAPL",
                        "status": "filled",
                        "planned_amount_usd": 2.0,
                        "unapproved_execution_basis": "executed_unbounded",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    morning_dir = root / "morning_dca" / "20260706_1030ET"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": trading_day.isoformat(),
                "generated_at": f"{trading_day.isoformat()}T10:30:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "morning_dca_live",
                    "stream": "morning",
                    "manual_run": False,
                    "trading_day": trading_day.isoformat(),
                    "run_id": "morning_live_20260706_1030ET",
                },
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    scheduled_dir = afternoon_task_root / "scheduled_runs" / "20260706_1520ET"
    allocations, scan_had_failures = _compute_auto_derived_prior_allocations(
        _now(), trading_day, scheduled_dir, None
    )

    assert scan_had_failures is False
    assert allocations["AAPL"].amount_usd == pytest.approx(5.0)
    assert allocations["AAPL"].attempt_count == 2
    assert allocations["AAPL"].unresolved_live_exposure is True


def test_compute_auto_derived_prior_allocations_deduplicates_a_mirrored_run_across_the_merge(
    tmp_path,
):
    """Codex review: "Apply mirrored-run dedup across the afternoon merge
    boundary". The SAME manual override mirrored under the afternoon root
    (ce16ba6b/manual_runs) and a sibling root (morning_dca) is one run, not
    two -- the final afternoon-vs-cross-stream merge must recognize the
    duplicate the same way load_prior_same_day_allocations_multi's own
    cross-root merge does, not silently sum it twice."""
    import json

    from run_dca import _compute_auto_derived_prior_allocations

    trading_day = _now().date()
    root = tmp_path / "cron_tracking"
    mirrored_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": f"{trading_day.isoformat()}T09:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "mirrored_override_0900"},
        "buy_candidates": [
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 5.0},
        ],
    }

    afternoon_task_root = root / "ce16ba6b"
    afternoon_dir = afternoon_task_root / "manual_runs" / "0900"
    afternoon_dir.mkdir(parents=True)
    (afternoon_dir / "run_state.json").write_text(json.dumps(mirrored_state), encoding="utf-8")

    morning_dir = root / "morning_dca" / "manual_runs" / "0900"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(json.dumps(mirrored_state), encoding="utf-8")

    scheduled_dir = afternoon_task_root / "scheduled_runs" / "20260706_1520ET"
    allocations, scan_had_failures = _compute_auto_derived_prior_allocations(
        _now(), trading_day, scheduled_dir, None
    )

    assert scan_had_failures is False
    # The SAME $5.00 order, mirrored under two roots, must count once.
    assert allocations["AAPL"].amount_usd == pytest.approx(5.0)
    assert allocations["AAPL"].attempt_count == 1


def test_compute_auto_derived_prior_allocations_full_duplicate_merge_unions_order_ids(
    tmp_path,
):
    """Codex review: "Preserve uncertain order IDs across duplicate-root
    merges". Same mirrored-run scenario as the dollar-dedup test above
    (the afternoon root and a cross-stream sibling share a run_id, so the
    final merge treats it as a full duplicate and keeps `existing`'s
    dollars/run_ids), but the sibling root's copy of that run also exposes
    a SECOND, uncertain order (A) for the same ticker that the afternoon
    root's copy never saw. The full-duplicate branch must still union
    contributing_order_ids -- discarding A's identity there would let a
    later reload whose order-id set happens to match `existing`'s alone
    look, by set equality, like proof A was resolved."""
    import json

    from run_dca import _compute_auto_derived_prior_allocations

    trading_day = _now().date()
    root = tmp_path / "cron_tracking"
    same_run_id = "mirrored_override_0900"
    afternoon_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": f"{trading_day.isoformat()}T09:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": same_run_id},
        "buy_candidates": [
            {"ticker": "AAPL", "order_id": "order-B", "status": "filled", "planned_amount_usd": 5.0},
        ],
    }
    sibling_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": f"{trading_day.isoformat()}T09:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": same_run_id},
        "buy_candidates": [
            {"ticker": "AAPL", "order_id": "order-B", "status": "filled", "planned_amount_usd": 5.0},
            {"ticker": "AAPL", "order_id": "order-A", "status": "filled", "planned_amount_usd": -1.0},
        ],
    }

    afternoon_task_root = root / "ce16ba6b"
    afternoon_dir = afternoon_task_root / "manual_runs" / "0900"
    afternoon_dir.mkdir(parents=True)
    (afternoon_dir / "run_state.json").write_text(json.dumps(afternoon_state), encoding="utf-8")

    morning_dir = root / "morning_dca" / "manual_runs" / "0900"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(json.dumps(sibling_state), encoding="utf-8")

    scheduled_dir = afternoon_task_root / "scheduled_runs" / "20260706_1520ET"
    allocations, scan_had_failures = _compute_auto_derived_prior_allocations(
        _now(), trading_day, scheduled_dir, None
    )

    assert scan_had_failures is False
    assert allocations["AAPL"].amount_usd == pytest.approx(5.0)
    assert "order-A" in allocations["AAPL"].contributing_order_ids
    assert "order-B" in allocations["AAPL"].contributing_order_ids


def test_compute_auto_derived_prior_allocations_deduplicates_a_mirrored_zero_notional_attempt(
    tmp_path,
):
    """Codex review: "Deduplicate mirrored zero-notional attempts". Same
    mirrored-run scenario as the dollar-dedup test above, but for a REJECTED
    order that contributed no dollars. contributing_run_ids is only
    populated once a row contributes a positive dollar amount, so this
    run's id never lands in it and the dollar-dedup check in this merge
    could never recognize the duplicate -- the additive branch counted the
    SAME broker attempt twice against attempt_count."""
    import json

    from run_dca import _compute_auto_derived_prior_allocations

    trading_day = _now().date()
    root = tmp_path / "cron_tracking"
    mirrored_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": f"{trading_day.isoformat()}T09:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "mirrored_override_0900"},
        "buy_candidates": [
            {"ticker": "AAPL", "status": "failed", "planned_amount_usd": 5.0},
        ],
    }

    afternoon_task_root = root / "ce16ba6b"
    afternoon_dir = afternoon_task_root / "manual_runs" / "0900"
    afternoon_dir.mkdir(parents=True)
    (afternoon_dir / "run_state.json").write_text(json.dumps(mirrored_state), encoding="utf-8")

    morning_dir = root / "morning_dca" / "manual_runs" / "0900"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(json.dumps(mirrored_state), encoding="utf-8")

    scheduled_dir = afternoon_task_root / "scheduled_runs" / "20260706_1520ET"
    allocations, scan_had_failures = _compute_auto_derived_prior_allocations(
        _now(), trading_day, scheduled_dir, None
    )

    assert scan_had_failures is False
    # The SAME rejected attempt, mirrored under two roots, must count once.
    assert allocations["AAPL"].amount_usd == 0.0
    assert allocations["AAPL"].attempt_count == 1


def test_compute_earlier_week_cross_stream_notional_sums_monday_sibling_spend(tmp_path):
    """Codex review: "Include prior-week sibling spend in the afternoon
    ledger". _compute_auto_derived_prior_allocations only scans
    trading_day itself, so a fresh Tuesday afternoon run never saw Monday's
    morning/midday/market-open spend against the shared weekly cap.
    _compute_earlier_week_cross_stream_notional must aggregate every
    completed earlier day in the current ISO week across the afternoon
    root and its cross-stream siblings."""
    import json

    from run_dca import _compute_earlier_week_cross_stream_notional

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"
    afternoon_run_dir.mkdir(parents=True)

    # Monday's morning live run: real, confirmed spend.
    morning_dir = root / "morning_dca" / "20260629_1030ET"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T10:30:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "morning_dca_live",
                    "stream": "morning",
                    "manual_run": False,
                    "trading_day": "2026-06-29",
                    "run_id": "morning_live_20260629_1030ET",
                },
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    # Monday's market-open live run: external stream, canonical CSV only.
    market_open_dir = root / "market_open_dca" / "20260629_0935ET"
    market_open_dir.mkdir(parents=True)
    (market_open_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T09:35:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "market_open_dca_live",
                    "stream": "market_open",
                    "manual_run": False,
                    "trading_day": "2026-06-29",
                    "run_id": "market_open_live_20260629_0935ET",
                },
                "buy_candidates": [],
            }
        ),
        encoding="utf-8",
    )
    (market_open_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
        "TRIN,ORD-2,filled,1.50,25.00,0.06\n",
        encoding="utf-8",
    )

    from datetime import date as _date

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    total, scan_had_failures = _compute_earlier_week_cross_stream_notional(
        now, trading_day, afternoon_run_dir, None
    )

    assert scan_had_failures is False
    assert total == pytest.approx(3.50)


def test_compute_earlier_week_cross_stream_notional_fails_closed_on_unreconstructable_order(
    tmp_path,
):
    """Codex review: "Preserve the originating ceiling for historical unknown
    orders". An earlier day's unreconstructable submission cannot be safely
    priced by TODAY's max_per_order_usd -- that day's config may have allowed
    a larger ceiling since lowered. Rather than risk under-counting real
    spend against the shared weekly cap, the scan must report
    scan_had_failures=True so the caller forces the worst-case tier ceiling,
    regardless of what max_per_order_usd is passed here."""
    import json

    from run_dca import _compute_earlier_week_cross_stream_notional

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"
    afternoon_run_dir.mkdir(parents=True)

    # Monday's morning live run: a confirmed submission whose true dollar
    # amount could not be reconstructed (morning_dca's blocked-retry
    # synthesis path).
    morning_dir = root / "morning_dca" / "20260629_1030ET"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T10:30:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "morning_dca_live",
                    "stream": "morning",
                    "manual_run": False,
                    "trading_day": "2026-06-29",
                    "run_id": "morning_live_20260629_1030ET",
                },
                "buy_candidates": [],
                "unreconstructable_submitted_tickers": ["AAPL"],
            }
        ),
        encoding="utf-8",
    )

    from datetime import date as _date

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    # A ceiling deliberately much smaller than a plausible originating one --
    # if it were trusted as a safe bound, scan_had_failures would stay False
    # and the caller would reconcile a possibly under-counted total instead
    # of failing closed.
    total, scan_had_failures = _compute_earlier_week_cross_stream_notional(
        now, trading_day, afternoon_run_dir, None, max_per_order_usd=10.0,
    )

    assert scan_had_failures is True, (
        "an unreconstructable submission's amount is never provably safe "
        "against an earlier day's real ceiling, so the caller must fail "
        "closed to the worst-case tier ceiling rather than trust this total"
    )


def test_compute_earlier_week_cross_stream_notional_no_earlier_spend_is_zero(tmp_path):
    from datetime import date as _date

    from run_dca import _compute_earlier_week_cross_stream_notional

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"
    afternoon_run_dir.mkdir(parents=True)

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    total, scan_had_failures = _compute_earlier_week_cross_stream_notional(
        now, trading_day, afternoon_run_dir, None
    )

    assert total == 0.0
    assert scan_had_failures is False


def test_compute_earlier_week_cross_stream_notional_includes_final_manual_review(tmp_path):
    """Codex review: "Include final-manual overrides in afternoon weekly
    seeding". final_manual_review is an out-of-tree manual final-buy override
    root (real budget-block-override fills, see
    scripts/run_classification.py's _ROOTS_NEVER_REACHED_BY_CORE_LOADER) that
    morning_dca.py's own default_cap_seed_roots() already scans. Before this
    patch, _resolve_cross_stream_prior_allocation_roots (shared by both the
    same-day and this earlier-week afternoon scan) never included it, so a
    Monday final-manual override fill was invisible to a fresh Tuesday
    afternoon run's shared weekly cap."""
    import json
    from datetime import date as _date

    from run_dca import _compute_earlier_week_cross_stream_notional

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"
    afternoon_run_dir.mkdir(parents=True)

    # Monday's out-of-tree manual final-buy override: a budget-blocked signal
    # that a human approved and filled after the scheduled run completed.
    override_dir = root / "final_manual_review" / "budget_block_override_20260629_1600ET"
    override_dir.mkdir(parents=True)
    (override_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T16:00:00-04:00",
                "run_classification": {"run_origin": "manual", "run_id": "final_override_0629"},
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 6.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    total, scan_had_failures = _compute_earlier_week_cross_stream_notional(
        now, trading_day, afternoon_run_dir, None
    )

    assert scan_had_failures is False
    assert total == pytest.approx(6.0)


def test_compute_earlier_week_cross_stream_notional_includes_manual_bare_csv_fallback(tmp_path):
    """Codex review: "Include unreferenced manual CSVs in prior-week
    totals". _compute_earlier_week_cross_stream_notional's per-earlier-day
    scan only used load_prior_same_day_allocations_multi (the core loader),
    which cannot see a manual/recovery run whose fills live ONLY in a bare
    sibling orders CSV the run_state never names (no embedded
    buy_candidates, no artifacts.orders_csv_path -- the Jul 21 manual midday
    shape). _compute_auto_derived_prior_allocations already merges
    load_same_day_top_up_context for the SAME day; this earlier-week scan
    must do the same for each EARLIER day, or that manual spend is invisible
    to the shared weekly cap entirely."""
    import json
    from datetime import date as _date

    from run_dca import _compute_earlier_week_cross_stream_notional

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"
    afternoon_run_dir.mkdir(parents=True)

    # Monday's manual midday run: no embedded buy_candidates and no recorded
    # artifacts.orders_csv_path, so the core loader can't see it -- its fills
    # live only in the sibling submitted_orders.csv below.
    manual_dir = root / "midday_dca" / "manual_runs" / "20260629_1345ET"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T13:45:00-04:00",
                "run_classification": {
                    "run_origin": "manual",
                    "manual_run": True,
                    "run_id": "manual_midday_20260629_1345ET",
                    "trading_day": "2026-06-29",
                    "same_day_top_up_context_only": True,
                    "valid_scheduled_evidence": False,
                    "valid_afternoon_scheduled_evidence": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (manual_dir / "submitted_orders.csv").write_text(
        "symbol,amount,order_id,status,average_price,quantity\n"
        "IBM,4.0,MANUAL-IBM-1,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    total, scan_had_failures = _compute_earlier_week_cross_stream_notional(
        now, trading_day, afternoon_run_dir, None
    )

    assert scan_had_failures is False
    assert total == pytest.approx(4.0)


def test_run_pipeline_afternoon_seeds_earlier_week_spend_into_state(tmp_path):
    """End-to-end wiring check for "Include prior-week sibling spend in the
    afternoon ledger": a Tuesday afternoon run_pipeline() call must fold
    Monday's cross-stream spend into state.week_notional(), not just the
    ephemeral return value -- StrategyState is mutated in place and the
    caller's own reference reflects it after the call returns."""
    import json

    root = tmp_path / "cron_tracking"
    afternoon_run_dir = root / "ce16ba6b" / "scheduled_runs" / "20260630_1530ET"

    morning_dir = root / "morning_dca" / "20260629_1030ET"
    morning_dir.mkdir(parents=True)
    (morning_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T10:30:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "morning_dca_live",
                    "stream": "morning",
                    "manual_run": False,
                    "trading_day": "2026-06-29",
                    "run_id": "morning_live_20260629_1030ET",
                },
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 12.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    from datetime import date as _date

    trading_day = _date(2026, 6, 30)
    now = datetime.combine(trading_day, time(15, 30), tzinfo=EASTERN)
    config = load_strategy_config(approved_test_dates=frozenset({trading_day}))
    state = StrategyState()

    run_pipeline(
        now,
        config,
        state,
        DemoDataProvider(SourceName.FINANCE),
        DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config),
        artifacts_dir=afternoon_run_dir,
    )

    # Monday's morning spend now counts against Tuesday's shared weekly cap,
    # even though _compute_auto_derived_prior_allocations only ever scans
    # trading_day (Tuesday) itself for its own per-ticker top-up context.
    assert state.week_notional(trading_day) >= 12.0


def test_run_pipeline_logs_error_when_bounded_reload_scan_still_fails(tmp_path, monkeypatch, caplog):
    import run_dca

    calls = 0

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        nonlocal calls
        calls += 1
        return {}, True

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    with caplog.at_level("ERROR", logger=run_dca.logger.name):
        run_dca.run_pipeline(
            _now(), config, StrategyState(),
            DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
            build_broker(config), artifacts_dir=tmp_path,
        )

    assert calls == 2
    assert any(
        record.levelname == "ERROR"
        and "still had unreadable/unparseable artifacts after the bounded reload" in record.getMessage()
        for record in caplog.records
    )


def test_run_pipeline_forces_worst_case_ceiling_on_same_day_unreconstructable_submission(
    tmp_path, monkeypatch,
):
    # Codex review ("Fail same-day unknown submission amounts closed
    # globally"): a same-day (not earlier-week) unreconstructable
    # submission's estimated amount is not proof against whatever ceiling
    # actually governed the EARLIER same-day run that placed it -- config
    # can change between an earlier same-day run and this one. That
    # unknown reference already blocks a fresh buy of the ticker itself,
    # but the estimated dollar amount also feeds the shared daily/weekly
    # totals, so it must force the SAME global worst-case-ceiling
    # fail-closed the earlier-week scan already applies for the analogous
    # earlier-WEEK case.
    import run_dca
    from near_close_dca import BUDGET_TIERS

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, *, unreconstructable_sink=None, **_kwargs):
        if unreconstructable_sink is not None:
            unreconstructable_sink.append("AAPL")
        return {}, False

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) >= worst_case_cap, (
        "a same-day unreconstructable submission must force the shared weekly "
        "bucket to the worst-case ceiling, not trust an under-verified estimate"
    )


def test_run_pipeline_forces_worst_case_ceiling_with_an_explicit_prior_allocation_root(
    tmp_path, monkeypatch,
):
    # Codex review ("Fail explicit-root unknown submissions closed"): the
    # explicit prior_allocation_state_root override path calls
    # load_prior_same_day_allocations() directly (not the auto-derived
    # branch above) and must fail closed the same way when an
    # unreconstructable submission's amount is seeded from this run's own
    # max_per_order_usd -- not proof about whatever ceiling governed the
    # earlier same-day run that actually placed it.
    import run_dca
    from near_close_dca import BUDGET_TIERS

    def _fake_load(root, trading_day, *, boundary_et=None, unreconstructable_sink=None, **_kwargs):
        if unreconstructable_sink is not None:
            unreconstructable_sink.append("AAPL")
        return {}

    monkeypatch.setattr(run_dca, "load_prior_same_day_allocations", _fake_load)

    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
        prior_allocation_state_root=tmp_path / "explicit_root",
    )

    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) >= worst_case_cap, (
        "an explicit-root unreconstructable submission must force the shared "
        "weekly bucket to the worst-case ceiling too, not just the "
        "auto-derived path"
    )


def test_run_pipeline_clears_first_pass_unreconstructable_hit_after_a_clean_reload(
    tmp_path, monkeypatch,
):
    # Codex review ("Clear recovered unknown-notional signals after
    # reload"): the FIRST pass's unreconstructable_sink is reused as the
    # OUTER `_same_day_unreconstructable_sink` before the bounded reload
    # runs. If the reload's own (separate) sink were merely appended to that
    # same list instead of replacing it, a stale first-pass hit for a
    # ticker the reload went on to read cleanly would still poison the
    # shared weekly bucket, even though nothing about the FINAL (reload)
    # evidence is actually uncertain.
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, *, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            # First pass: AAPL is unresolved (triggers the bounded reload)
            # AND a DIFFERENT ticker's submission is unreconstructable this
            # pass only.
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("STALE")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0
                    )
                },
                False,
            )
        # Reload: AAPL resolves cleanly and nothing is unreconstructable.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=3.0, reference_price=0.01, run_id="am", source="manual",
                    selected_base_amount_usd=2.0,
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2  # exactly one bounded reload
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) < worst_case_cap, (
        "STALE's unreconstructable hit was only on the superseded first "
        "pass -- the clean reload is this run's final evidence and must "
        "not be poisoned by a signal the reload itself resolved"
    )


def test_run_pipeline_preserves_uncertainty_when_the_reload_loses_the_ticker(
    tmp_path, monkeypatch,
):
    # Codex review ("Preserve uncertainty when the merge keeps first-pass
    # evidence"): when the first pass sees an unreconstructable submission
    # (AAPL, real entry with reference_price=None) but that ticker's
    # artifact disappears or is simply omitted from a CLEAN, complete
    # bounded reload (second pass's own dict has no AAPL key at all, and
    # second_scan_had_failures is False), the merge loop only visits
    # tickers present in the SECOND pass -- AAPL is never touched and keeps
    # its unresolved first-pass entry verbatim. The outer fail-closed
    # signal must still reflect that retained uncertainty; using only the
    # reload's own (empty) sink lost it entirely, even though the retained
    # allocation is exactly as uncertain as it was before the reload.
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, *, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0,
                        notional_uncertain=True,
                    )
                },
                False,
            )
        # Clean, complete reload -- but AAPL's artifact simply is not seen
        # this pass, so it is entirely absent from the returned mapping.
        return ({}, False)

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2  # exactly one bounded reload
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) >= worst_case_cap, (
        "AAPL's unresolved first-pass allocation survived the merge unchanged "
        "-- it is retained evidence, not resolved evidence -- so it must still "
        "force the shared weekly bucket to the worst-case ceiling"
    )


def test_run_pipeline_does_not_poison_a_resolved_allocation_with_no_fill_price(
    tmp_path, monkeypatch,
):
    # Codex review ("Track unknown notionals separately from missing
    # prices"): the fail-closed signal used to key off reference_price is
    # None, but an ordinary "submitted"/"unconfirmed" allocation with a
    # genuine, fully-reconstructed dollar amount can ALSO legitimately have
    # reference_price=None (no fill price yet) -- that is not the same
    # thing as still carrying the unreconstructable sentinel estimate. When
    # the reload resolves AAPL into exactly such an allocation (a larger,
    # real amount, notional_uncertain=False, reference_price still None),
    # the shared weekly bucket must not be forced closed.
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, *, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0,
                        notional_uncertain=True,
                        contributing_order_ids=frozenset({"order-1"}),
                    )
                },
                False,
            )
        # Clean, complete reload: the SAME order (order-1), now fully
        # reconstructed with a genuine, larger dollar total -- still no
        # fill price (a live "submitted" order), but NOT the
        # unreconstructable sentinel. The matching order_ids are the
        # provenance proof this branch's uncertainty-clearing guard
        # requires (Codex review: "Preserve uncertainty when the reload
        # amount increases") -- an unrelated order growing the total
        # without proving it still accounts for order-1 must NOT clear
        # the flag, but this is the SAME order, so it legitimately may.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=8.0, reference_price=None, selected_base_amount_usd=2.0,
                    notional_uncertain=False,
                    contributing_order_ids=frozenset({"order-1"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) < worst_case_cap, (
        "a resolved allocation with a genuine reconstructed amount must not "
        "poison the shared weekly bucket just because it still has no "
        "known fill price"
    )


def test_run_pipeline_clean_equal_reload_clears_notional_uncertainty(tmp_path, monkeypatch):
    """An equal, clean reconstructed amount from the SAME order supersedes
    a sentinel estimate."""
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 3.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"same_run"}),
                        contributing_order_ids=frozenset({"order-1"}),
                    )
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 3.0, None, notional_uncertain=False,
                    contributing_run_ids=frozenset({"same_run"}),
                    contributing_order_ids=frozenset({"order-1"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert state.week_notional(trading_day) < max(tier.weekly_cap_usd for tier in BUDGET_TIERS)


def test_run_pipeline_equal_reload_from_a_different_contributor_keeps_uncertainty(
    tmp_path, monkeypatch
):
    """Codex review ("Preserve uncertainty unless reload provenance matches"):
    an equal dollar total across two scans is not proof the SAME order was
    reconstructed. An uncertain $3 submission can disappear between scans
    while an unrelated $3 fill for the same ticker appears -- the true
    deployment could be $6, not $3, and clearing the uncertainty just
    because the numbers happened to match would drop the worst-case cap
    poisoning for a submission that is still genuinely unresolved.
    """
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 3.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"first_run"}),
                        contributing_order_ids=frozenset({"order-1"}),
                    )
                },
                False,
            )
        # A DIFFERENT run now accounts for the same $3 -- not proof the
        # first pass's uncertain submission was ever actually resolved.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 3.0, None, notional_uncertain=False,
                    contributing_run_ids=frozenset({"second_run"}),
                    contributing_order_ids=frozenset({"order-2"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) == worst_case_cap, (
        "an equal amount from a DIFFERENT contributor must not clear the "
        "first pass's uncertainty -- the worst-case poisoning must stand"
    )


def test_run_pipeline_same_run_id_but_different_order_keeps_uncertainty(
    tmp_path, monkeypatch
):
    """Codex review ("Match order-level provenance before clearing
    uncertainty"): equal contributing_run_ids does not prove the SAME order
    was reconstructed -- a single mutable run_state.json can expose
    MULTIPLE distinct same-ticker orders under one run_id across two scans.
    An uncertain $3 submission can disappear from a run while a DIFFERENT
    confirmed $3 order from that SAME run appears -- clearing uncertainty
    on run_id equality alone would understate deployment even though the
    run identity matches exactly.
    """
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 3.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"same_run"}),
                        contributing_order_ids=frozenset({"order-A"}),
                    )
                },
                False,
            )
        # The SAME run_id, but a genuinely DIFFERENT order under it.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 3.0, None, notional_uncertain=False,
                    contributing_run_ids=frozenset({"same_run"}),
                    contributing_order_ids=frozenset({"order-B"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) == worst_case_cap, (
        "an equal run_id with a DIFFERENT order_id must not clear the "
        "first pass's uncertainty -- run-level identity alone is not proof"
    )


def test_run_pipeline_a_lost_uncertain_contributor_keeps_uncertainty(
    tmp_path, monkeypatch
):
    """Codex review ("Require provenance for the uncertain contributor
    itself"): contributing_order_ids used to only be populated at the
    dollar-CONFIRMED site, so an uncertain/sentinel row's own order
    identity was never tracked. Pass one sees an uncertain order A (order
    id "order-A") plus a known $2 order B ("order-B") -- contributing_
    order_ids only contained {"order-B"}, silently omitting A. A clean
    reload that loses A entirely while correcting B to $5 (an entirely
    plausible independent reconciliation) would then show the SAME
    {"order-B"} set on both passes and clear the uncertainty, even though
    A may still represent live, unaccounted spend. With the fix,
    contributing_order_ids on pass one includes BOTH "order-A" and
    "order-B", so pass two's {"order-B"}-only set no longer matches and
    the uncertainty correctly survives.
    """
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 5.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"same_run"}),
                        # Both the uncertain order A and the known order B
                        # contributed to this pass's identity set -- the
                        # exact fix this test guards.
                        contributing_order_ids=frozenset({"order-A", "order-B"}),
                    )
                },
                False,
            )
        # A lost entirely; B alone now accounts for the same $5 total.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 5.0, None, notional_uncertain=False,
                    contributing_run_ids=frozenset({"same_run"}),
                    contributing_order_ids=frozenset({"order-B"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) == worst_case_cap, (
        "losing the uncertain contributor A while another known order B "
        "coincidentally accounts for the same total must not clear the "
        "first pass's uncertainty"
    )


def test_run_pipeline_a_larger_reload_amount_keeps_uncertainty_without_provenance(
    tmp_path, monkeypatch
):
    """Codex review ("Preserve uncertainty when the reload amount
    increases"): the order-level provenance guard only protected the
    EQUAL-amount branch of the cross-stream merge. When the reload's
    amount is strictly LARGER, the merge used to replace the entry
    wholesale (`prior_same_day_allocations[ticker] = alloc`), always
    carrying alloc's own notional_uncertain regardless of provenance.
    Pass one sees an uncertain $10 order A; pass two loses A entirely
    while discovering an unrelated, larger $12 order B. $12 > $10 looks
    "more complete", but B's total does not include A's $10 at all --
    the real aggregate could still be $22, not $12 -- so the merge must
    not silently clear the uncertainty just because the reload's number
    got bigger.
    """
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 10.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"first_run"}),
                        contributing_order_ids=frozenset({"order-A"}),
                    )
                },
                False,
            )
        # A lost entirely; a DIFFERENT, larger order B now accounts for a
        # bigger total that never actually included A's dollars.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 12.0, None, notional_uncertain=False,
                    contributing_run_ids=frozenset({"second_run"}),
                    contributing_order_ids=frozenset({"order-B"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) == worst_case_cap, (
        "a LARGER reload amount from an unrelated order must not clear the "
        "first pass's uncertainty without proving A's dollars are accounted for"
    )


def test_run_pipeline_a_new_reference_price_does_not_smuggle_away_uncertainty(
    tmp_path, monkeypatch
):
    """Codex review ("Preserve uncertainty through reload metadata
    replacement"): the provenance check only guards the FIRST branch's own
    explicit notional_uncertain clearing. When the reload has disjoint
    provenance (so that check correctly declines to clear) but ALSO
    supplies a new, different reference price, the separate reference-
    price-replacement branch used to overwrite the whole entry with the
    reload's own alloc -- whose notional_uncertain is False -- silently
    smuggling away the uncertainty this exact ticker was just proven NOT to
    have resolved.
    """
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(*_args, unreconstructable_sink=None, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if unreconstructable_sink is not None:
                unreconstructable_sink.append("AAPL")
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", 3.0, None, notional_uncertain=True,
                        contributing_run_ids=frozenset({"first_run"}),
                    )
                },
                False,
            )
        # A DIFFERENT run accounts for the same $3, but ALSO happens to
        # carry a known reference price this ticker never had before.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", 3.0, 99.0, notional_uncertain=False,
                    contributing_run_ids=frozenset({"second_run"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    trading_day = _now().astimezone(EASTERN).date()
    state = StrategyState()

    run_dca.run_pipeline(
        _now(), _demo_config(), state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(_demo_config()), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)
    assert state.week_notional(trading_day) == worst_case_cap, (
        "a new reference price from a DIFFERENT contributor must not clear "
        "the first pass's uncertainty through the metadata-replacement "
        "branch -- the worst-case poisoning must stand"
    )


def test_run_pipeline_incomplete_reload_preserves_any_notional_uncertainty(tmp_path, monkeypatch):
    """An incomplete reload cannot clear an unknown amount seen on pass one."""
    import run_dca
    from near_close_dca import BUDGET_TIERS, PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=3.0, reference_price=None,
                        selected_base_amount_usd=2.0, notional_uncertain=True,
                    )
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=8.0, reference_price=0.01,
                    selected_base_amount_usd=2.0, notional_uncertain=False,
                )
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)
    config = _demo_config()
    state = StrategyState()
    trading_day = _now().astimezone(EASTERN).date()

    run_dca.run_pipeline(
        _now(), config, state,
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert state.week_notional(trading_day) >= max(tier.weekly_cap_usd for tier in BUDGET_TIERS), (
        "the final allocation must retain first-pass uncertainty even when the "
        "incomplete reload supplies a larger nominal amount without using a sink"
    )


def test_run_pipeline_bounded_reload_preserves_already_resolved_ticker(tmp_path, monkeypatch):
    # Codex review ("Preserve resolved ticker context across the reload"): the
    # bounded reload exists to give an UNRESOLVED ticker a second chance, not
    # to let a transient scan hiccup on the SECOND pass silently discard a
    # DIFFERENT ticker's already-good first-pass data. Simulate AAPL
    # unresolved on pass one (triggering the reload) and MSFT resolved fine
    # on pass one but poisoned on pass two (e.g. MSFT's own artifact hit a
    # transient race during the reload scan) -- MSFT must keep its first-pass
    # value in the final result.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0),
                    "MSFT": PriorSameDayAllocation(
                        "MSFT", amount_usd=2.0, reference_price=0.01, run_id="am", source="manual", selected_base_amount_usd=2.0),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=3.0, reference_price=0.01, run_id="am", source="manual", selected_base_amount_usd=2.0),
                "MSFT": PriorSameDayAllocation("MSFT", amount_usd=2.0, reference_price=None, selected_base_amount_usd=2.0),
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2  # exactly one bounded reload, not an unbounded retry loop
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # AAPL used the reloaded (resolved) data.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "same_day_topup_not_lower_or_no_delta"
    # MSFT kept its first-pass (resolved) data. If the whole mapping had been
    # replaced with pass two's result, MSFT would fall back to the
    # missing_prior_price / manual_review $0 path instead.
    assert by_ticker["MSFT"]["top_up_guard_result"] == "same_day_topup_not_lower_or_no_delta"
    assert by_ticker["MSFT"]["top_up_manual_review"] is False


def test_run_pipeline_bounded_reload_uses_recovered_larger_amount_for_resolved_ticker(
    tmp_path, monkeypatch
):
    # Codex review ("Use recovered allocations when the first scan was
    # incomplete"): MSFT is already resolved (known price) on pass one, but
    # only from ONE of its same-day artifacts -- a SIBLING artifact for the
    # SAME ticker failed to parse on pass one and became readable on the
    # reload, so pass two's aggregate for MSFT is a strictly LARGER amount.
    # The merge must take pass two's more-complete total instead of freezing
    # MSFT at its understated first-pass amount, which would let the top-up
    # guard compare against too little prior capital and over-buy.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=None),
                    "MSFT": PriorSameDayAllocation(
                        "MSFT", amount_usd=2.0, reference_price=0.01, run_id="am", source="manual"
                    ),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=3.0, reference_price=0.01, run_id="am", source="manual"
                ),
                # Recovered a second, previously-unreadable artifact for
                # MSFT: the amount grew from $2 to $6, still resolved.
                "MSFT": PriorSameDayAllocation(
                    "MSFT", amount_usd=6.0, reference_price=0.01, run_id="am2", source="manual"
                ),
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # MSFT must reflect the recovered, larger prior amount, not the stale
    # first-pass $2 that would understate how much capital was already spent.
    assert by_ticker["MSFT"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_trusts_clean_reload_after_dirty_first_pass(
    tmp_path, monkeypatch
):
    # Codex review ("Use a clean reload after a failed first scan"): AAPL's
    # first pass reads a known-price $2 fill but misses a same-ticker $3
    # unconfirmed submission (scan_had_failures=True) -- the reload is
    # completely CLEAN (second_scan_had_failures=False) and correctly reads
    # BOTH, aggregating to the true $5/unknown context. A first-pass
    # failure flag must not by itself block trusting an otherwise-clean
    # reload: only the RELOAD's own cleanliness determines whether its
    # total can be trusted as complete, since a clean reload is
    # definitionally a full read of everything on disk regardless of what
    # the first pass happened to miss.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=2.0, reference_price=98.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
                },
                True,
            )
        return (
            {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=None, selected_base_amount_usd=2.0)},
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must use the clean reload's complete $5/unknown context, not the
    # stale, understated first-pass $2/known.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_accepts_corrected_reference_price(tmp_path, monkeypatch):
    # Codex review ("Accept updated references from a clean reload"): AAPL's
    # first pass reads a full-report CSV-only allocation at a preliminary
    # fill price ($2 at $100). By the clean reload, the same $2 notional has
    # been reconciled to the confirmed broker price ($90) -- same amount,
    # different KNOWN price. The equal-amount branch previously only
    # accepted a None-to-known transition, so it kept the stale $100
    # reference even though the clean reload has more current, accurate
    # data for the identical notional.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=2.0, reference_price=100.0, run_id="am", source="manual"
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=2.0, reference_price=90.0, run_id="am2", source="manual"
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must use the reload's corrected $90 reference, not the stale $100.
    assert by_ticker["AAPL"]["prior_same_day_reference_price"] == 90.0
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 2.0


def test_run_pipeline_bounded_reload_keeps_unresolved_when_reload_is_incomplete(
    tmp_path, monkeypatch
):
    # Codex review ("Preserve unresolved context when the reload is
    # incomplete"): AAPL's first pass aggregates a known-price fill ($2) and
    # an unknown-price submission ($3) into amount=5.0/price=None (correctly
    # unresolved -- the aggregate is poisoned by the unconfirmed submission).
    # If the RELOAD transiently loses the artifact behind that unconfirmed
    # submission, its aggregate becomes only amount=2.0/price=98.0 -- smaller
    # than pass one, and only "resolved" because the very context that made
    # it uncertain vanished. Blindly trusting any reload result for a
    # previously-unresolved ticker would treat this SMALLER, less-complete
    # aggregate as the final answer and route the guard through a straight
    # top-up comparison instead of failing conservatively closed.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=None, selected_base_amount_usd=2.0)},
                False,
            )
        # Reload lost the $3 unconfirmed-submission artifact: smaller
        # amount, but now "resolved" purely because the uncertain
        # contributor disappeared from this scan.
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=2.0, reference_price=98.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must still route through the conservative missing-price path with the
    # ORIGINAL $5 amount, not the reload's smaller, falsely-resolved $2.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_preserves_uncertainty_on_equal_swapped_totals(
    tmp_path, monkeypatch
):
    # Codex review ("Preserve uncertainty when equal totals swap
    # contributors"): AAPL genuinely has TWO same-day contributing
    # artifacts of equal size -- an unknown-price $5 submission and a
    # known-price $5 filled order -- but each scan only manages to read ONE
    # of the two (the other fails to parse that pass), so both passes
    # report the SAME $5 total: pass one via the submission (unresolved),
    # the reload via the filled order (resolved). Naively trusting "equal
    # amount, price newly resolved" here would silently understate the
    # true $10 prior spend as a falsely-confident $5. Both scans report
    # scan_had_failures=True (each genuinely skipped one artifact), which
    # is the signal the logic uses to refuse this equal-amount "resolution."
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=None, selected_base_amount_usd=2.0)},
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=5.0, reference_price=98.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must stay unresolved (the pre-reload $5/unknown-price state), not
    # silently accept the reload's coincidentally-equal resolved entry.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_preserves_uncertainty_on_unequal_swapped_totals(
    tmp_path, monkeypatch
):
    # Codex review ("Preserve uncertainty when unequal contributors swap"):
    # AAPL genuinely has TWO same-day contributing artifacts -- an
    # unknown-price $5 submission and a known-price $6 filled order (true
    # aggregate: $11/unknown) -- but each scan only manages to read ONE of
    # the two. Pass one reads the $5 submission (the $6 fill fails to parse
    # that pass); the reload reads the $6 fill (the $5 submission fails to
    # parse THIS pass instead). $6 > $5 looks like a strictly more complete
    # answer by amount alone, but it silently discards the other real
    # contributor and its unresolved status. Both scans report
    # scan_had_failures=True, which is the signal the logic uses to refuse
    # trusting either total as more complete than the other for this ticker.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=None, selected_base_amount_usd=2.0)},
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=6.0, reference_price=98.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must stay unresolved -- but now poisoned to the LARGER ($6) of the two
    # observed amounts (Codex review: "Poison resolved context when an
    # incomplete reload swaps contributors"), a conservative floor on the
    # true $11 aggregate, never the reload's resolved $98 reference price.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_keeps_newly_recovered_ticker_uncertain(
    tmp_path, monkeypatch
):
    # Codex review ("Keep recovered tickers uncertain after an incomplete
    # reload"): AAPL is entirely absent from the first pass -- BOTH of its
    # real same-day artifacts (a $6 fill and a $5 unknown-price submission)
    # failed to parse that pass. The reload recovers only the $6 fill (the
    # $5 submission still fails to parse THIS pass, so
    # second_scan_had_failures=True) -- a resolved $6, not the true
    # $11/unknown context. Since AAPL was invisible on the first pass, the
    # naive "nothing to protect" reasoning would trust this partial
    # recovery as complete; it must instead be treated as uncertain because
    # the reload itself wasn't fully clean.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return ({}, True)
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=6.0, reference_price=98.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # The recovered $6 amount is kept (more information than none), but the
    # price must be poisoned to unresolved rather than trusted as complete.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_keeps_uncertainty_when_reload_omits_id_less_contributor(
    tmp_path, monkeypatch
):
    # Codex review, PR badMade/all-stock-dca#1168 discussion r3900047294:
    # "Track identity for id-less uncertain contributors, not just a
    # known-ID subset check". The first scan combines a KNOWN $3 order
    # ("order-X") with an id-less $2 uncertain candidate (no order_id at
    # all, so it never enters contributing_order_ids -- only
    # uncertain_contributor_ids) -- amount_usd=5.0, notional_uncertain=True.
    # A clean reload swaps in a DIFFERENT known $3 order ("order-Y") while
    # never re-observing the id-less candidate -- amount_usd=6.0 (order-X +
    # order-Y), notional_uncertain=False, contributing_order_ids={"order-X",
    # "order-Y"}. existing.contributing_order_ids={"order-X"} IS a subset of
    # that, so the OLD known-ID-only subset check would wrongly treat this
    # as proof the first pass's uncertainty was fully resolved -- silently
    # dropping the id-less candidate's possibly-still-live $2 from
    # deployment/cap accounting. uncertain_contributor_ids must block that:
    # existing's is nonempty and the reload's own uncertain_contributor_ids
    # is empty, so the subset proof fails and the merge must keep the
    # ticker unresolved, poisoned to the larger $6.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL",
                        amount_usd=5.0,
                        reference_price=None,
                        selected_base_amount_usd=2.0,
                        notional_uncertain=True,
                        contributing_order_ids=frozenset({"order-X"}),
                        uncertain_contributor_ids=frozenset({"uncertain-1"}),
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL",
                    amount_usd=6.0,
                    reference_price=98.0,
                    run_id="am",
                    source="manual",
                    selected_base_amount_usd=2.0,
                    notional_uncertain=False,
                    contributing_order_ids=frozenset({"order-X", "order-Y"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must stay unresolved despite the known-order subset matching -- the
    # id-less uncertain contributor was never re-observed. notional_uncertain
    # staying True routes through evaluate_top_up_guard's own dedicated
    # branch for it (TOP_UP_MISSING_PRIOR_BASE), distinct from the
    # reference-price-poisoning branches (TOP_UP_MISSING_PRIOR_PRICE) the
    # OTHER bounded-reload tests in this file exercise.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_base"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_keeps_uncertainty_on_equal_total_when_id_less_contributor_vanishes(
    tmp_path, monkeypatch
):
    # Same gap as the "omits id-less contributor" test above, but for the
    # EQUAL-amount branch's exact-set-equality check instead of the
    # larger-amount branch's subset check (the task's "equal-amount
    # equality check nearby has the same completeness problem"). First
    # scan: a known $3 order ("order-X") plus an id-less $2 uncertain
    # candidate -- amount_usd=5.0, notional_uncertain=True. Clean reload:
    # a coincidentally equal $5.0 total built from order-X ($3) plus a
    # DIFFERENT known order ("order-Y", $2) -- contributing_order_ids=
    # {"order-X","order-Y"} != existing's {"order-X"}, so the
    # contributing_order_ids equality check alone already blocks this.
    # This test isolates the case where contributing_order_ids themselves
    # happen to match exactly (order-X's set is literally reused) while the
    # id-less uncertain candidate is simply dropped from the total this
    # pass and replaced by a resolved, equal-dollar in-place correction --
    # uncertain_contributor_ids must still block clearing since the id-less
    # candidate itself was never re-observed.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL",
                        amount_usd=5.0,
                        reference_price=None,
                        selected_base_amount_usd=2.0,
                        notional_uncertain=True,
                        contributing_order_ids=frozenset({"order-X"}),
                        uncertain_contributor_ids=frozenset({"uncertain-1"}),
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL",
                    amount_usd=5.0,
                    reference_price=98.0,
                    run_id="am",
                    source="manual",
                    selected_base_amount_usd=2.0,
                    notional_uncertain=False,
                    contributing_order_ids=frozenset({"order-X"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must stay unresolved despite contributing_order_ids matching exactly
    # -- the id-less uncertain contributor was never re-observed.
    # notional_uncertain staying True routes through evaluate_top_up_guard's
    # own dedicated branch for it (TOP_UP_MISSING_PRIOR_BASE).
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_base"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_resolves_an_id_less_contributor_that_becomes_reconstructable(
    tmp_path, monkeypatch
):
    # Codex review, PR badMade/all-stock-dca#1171 discussion: "Retain
    # identity after resolving an anonymous row" -- a P1 caught in review
    # on an EARLIER version of the fix above. That version assigned an
    # id-less row's synthetic uncertain_contributor_ids identity ONLY on
    # the uncertain/sentinel path, so a row that later became fully
    # reconstructable (e.g. its transient NaN amount replaced by a usable
    # one on a clean reload) necessarily produced an EMPTY set on the
    # resolved pass -- the subset/equality proof could then never succeed
    # for that contributor, permanently forcing notional_uncertain=True
    # and the worst-case shared-cap ceiling even after a clean, complete
    # re-observation. This is the positive case the fix must still allow:
    # AAPL's first pass has ONLY an id-less uncertain candidate (no known
    # order at all) contributing a $2 sentinel; a clean reload reads the
    # SAME row now resolved to a real $4 -- the synthetic identity must be
    # recorded on BOTH passes so the merge can tell this is the same
    # contributor, re-observed and resolved, not a different one replacing
    # it, and let the ticker clear to a normal (non-manual-review) outcome.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL",
                        amount_usd=2.0,
                        reference_price=None,
                        selected_base_amount_usd=2.0,
                        notional_uncertain=True,
                        uncertain_contributor_ids=frozenset({"uncertain:AAPL:run-1:row0"}),
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL",
                    amount_usd=4.0,
                    reference_price=98.0,
                    run_id="am",
                    source="manual",
                    selected_base_amount_usd=2.0,
                    notional_uncertain=False,
                    # Same row, same synthetic identity, now on the
                    # resolved/confirmed side -- the exact carry-forward
                    # the fix must produce.
                    uncertain_contributor_ids=frozenset({"uncertain:AAPL:run-1:row0"}),
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT be stuck at the manual-review missing_prior_base outcome --
    # the same contributor was cleanly re-observed and resolved, so the
    # guard must evaluate normally against the resolved $4/$98 context.
    assert by_ticker["AAPL"]["top_up_guard_result"] != "missing_prior_base"
    assert by_ticker["AAPL"]["top_up_manual_review"] is not True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 4.0


def test_run_pipeline_bounded_reload_poisons_resolved_ticker_on_reverse_swap(
    tmp_path, monkeypatch
):
    # Codex review ("Poison resolved context when an incomplete reload swaps
    # contributors"): the reverse direction of the swap-uncertainty case
    # above. The first pass reads a known-price $5 artifact for AAPL (a $6
    # unknown-price sibling artifact fails to parse that pass); the reload
    # then reads that $6 unknown-price artifact but misses the $5 one this
    # time. Both passes report scan_had_failures=True. Before this correction,
    # `elif not _reload_verified_complete: pass` blindly kept the first
    # pass's resolved $5/known-price allocation -- presenting FALSE
    # CONFIDENCE (a fully resolved, comparable amount) even though the true
    # same-day context is $11/unknown. The top-up guard could then submit
    # excess capital on top of an incomplete picture instead of failing this
    # ticker closed. The merged result must be poisoned to unresolved,
    # using the larger ($6) of the two observed amounts as a conservative
    # floor.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=50.0, selected_base_amount_usd=2.0)},
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=6.0, reference_price=None, run_id="am", source="manual", selected_base_amount_usd=2.0)
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT trust the first pass's resolved $5/known-price value -- the
    # reload's conflicting $6/unknown reading is direct evidence it wasn't
    # actually complete. Route through the conservative missing_prior_price
    # path with the larger ($6) amount as the floor.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_poisons_conflict_after_clean_first_scan(
    tmp_path, monkeypatch
):
    # Codex review ("Poison conflicts even after a clean first scan"): the
    # first pass is entirely clean (scan_had_failures=False) -- AAPL is
    # unresolved (triggering the reload) and MSFT is independently resolved
    # at $2/known. The reload itself hits an unrelated skip elsewhere
    # (second_scan_had_failures=True), but a sibling writer also lands a NEW
    # artifact for MSFT in the window between the two scans, so the reload
    # now sees MSFT at $6/unknown -- a genuine disagreement, not a scan
    # artifact. Gating the poison on the FIRST pass's scan_had_failures flag
    # (as the prior implementation did) would miss this: that flag says nothing about
    # whether pass one's snapshot of MSFT specifically is still current.
    # Any disagreement observed while the reload is unverified must poison
    # to uncertain, regardless of whether the earlier scan reported a
    # failure.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation("AAPL", amount_usd=5.0, reference_price=None, selected_base_amount_usd=2.0),
                    "MSFT": PriorSameDayAllocation(
                        "MSFT", amount_usd=2.0, reference_price=50.0, run_id="am", source="manual", selected_base_amount_usd=2.0),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=5.0, reference_price=99.0, run_id="am", source="manual", selected_base_amount_usd=2.0),
                # A sibling writer added a new MSFT artifact between the two
                # scans -- a real change, not a parse race.
                "MSFT": PriorSameDayAllocation("MSFT", amount_usd=6.0, reference_price=None, selected_base_amount_usd=2.0),
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # MSFT must NOT keep the stale $2/known value -- the reload's
    # conflicting $6/unknown reading is direct evidence it changed. Route
    # through the conservative missing_prior_price path with the larger
    # ($6) amount as the floor.
    assert by_ticker["MSFT"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["MSFT"]["top_up_manual_review"] is True
    assert by_ticker["MSFT"]["prior_same_day_amount"] == 6.0


def test_run_pipeline_bounded_reload_poisons_equal_metadata_swap(tmp_path, monkeypatch):
    # Codex review ("Poison equal-metadata swaps after an incomplete
    # reload"): AAPL genuinely has TWO same-day $5 fills at the identical
    # known price $90 -- but each scan only manages to read ONE of them.
    # Pass one reads the first $5/$90 fill (the second, coincidentally
    # identical, fill fails to parse that pass); the reload reads the
    # second $5/$90 fill (the first now fails to parse THIS pass instead).
    # Both passes report scan_had_failures=True. Comparing amount_usd and
    # reference_price alone makes this look like "nothing changed" -- they
    # match exactly -- but there is no per-artifact identity tracked here to
    # actually prove the two passes read the SAME fill. The true aggregate
    # is $10, and treating the matching metadata as proof of an unchanged,
    # resolved $5 context could let the top-up guard compare against too
    # little prior capital.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=5.0, reference_price=90.0, run_id="am", source="manual", selected_base_amount_usd=2.0)
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=5.0, reference_price=90.0, run_id="am2", source="manual", selected_base_amount_usd=2.0)
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT trust the matching $5/$90 metadata as proof the same fill was
    # read twice -- with both scans dirty, poison to unresolved rather than
    # risk comparing the top-up guard against an understated $5 when the
    # true aggregate could be $10.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True


def test_run_pipeline_bounded_reload_poisons_despite_matching_run_id_when_reload_incomplete(
    tmp_path, monkeypatch
):
    # Codex review ("Track all contributors before trusting an incomplete
    # reload"): a matching `run_id` across both passes only proves the
    # WINNING (most recent) contributor to amount_usd was read identically
    # both times -- it does NOT prove every artifact summed into amount_usd
    # was. Two passes can coincidentally agree on amount/reference/base/
    # run_id while each having missed a DIFFERENT non-winning, equal-dollar
    # fill for the same ticker, hiding a real undercount that would let the
    # top-up guard submit excess capital. An incomplete reload
    # (second_scan_had_failures=True) for a ticker present in both passes
    # must therefore always poison to the conservative missing_prior_price
    # path, even when every observed field -- including run_id -- matches
    # exactly.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        allocation = PriorSameDayAllocation(
            "AAPL",
            amount_usd=5.0,
            reference_price=90.0,
            run_id="am",
            source="manual",
            selected_base_amount_usd=1.0,
            selected_base_amount_usd_at="2026-07-06T09:45:00-04:00",
        )
        # Both passes report a failure (e.g. an unrelated MSFT artifact
        # mid-write); AAPL's own data happens to read identically both
        # times, but that agreement is no longer trusted as proof the same
        # full contributor set was read.
        return ({"AAPL": allocation}, True)

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must poison: an incomplete reload is never trusted for a ticker present
    # in both passes, regardless of field agreement.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_poisons_first_only_entry_after_incomplete_reload(
    tmp_path, monkeypatch
):
    # Codex review ("Poison first-only entries after an incomplete reload"):
    # the merge loop only iterates second_pass.items(), so a ticker present
    # ONLY in first_pass -- entirely absent from an INCOMPLETE reload -- never
    # passes through any of the poisoning logic and simply keeps its
    # first-pass value via the `dict(first_pass)` seed. AAPL reads a
    # known-price $5 fill on pass one while a same-ticker $6 unknown-price
    # submission fails to parse (scan_had_failures=True, which alone
    # triggers the bounded reload); the reload then fails to read EITHER
    # AAPL artifact (second_scan_had_failures=True), so AAPL is entirely
    # absent from second_pass.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=5.0, reference_price=50.0, run_id="am", source="manual", selected_base_amount_usd=2.0),
                },
                True,
            )
        # AAPL is entirely absent -- the reload failed to read either of its
        # artifacts this pass.
        return ({}, True)

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # AAPL must NOT keep its resolved $5/known first-pass value -- the
    # incomplete reload's total silence about it is not proof it no longer
    # exists, only that this pass couldn't read it either.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_accepts_corrected_base_from_clean_reload(tmp_path, monkeypatch):
    # Codex review ("Accept corrected allocation bases from a clean reload"):
    # AAPL's amount and reference price are unchanged between passes, but its
    # selected_base_amount_usd is corrected from $2 (a preliminary/estimated
    # base written by the first pass's artifact) to $1 by a CLEAN reload
    # (second_scan_had_failures=False, so this artifact is authoritative).
    # resolve_top_up_desired_amount_usd multiplies the base directly to
    # compute the top-up target, so keeping the stale $2 base could target
    # roughly double what the corrected $1 base actually supports.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=2.0, reference_price=50.0, run_id="am", source="manual",
                        selected_base_amount_usd=2.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=2.0, reference_price=50.0, run_id="am2", source="manual",
                    selected_base_amount_usd=1.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must use the clean reload's corrected $1 base, not the stale $2 -- this
    # is persisted as the candidate's own selected_base_amount_usd, since
    # that field records whichever base ACTUALLY produced planned_amount_usd
    # (guard.effective_base_amount_usd, see resolve_top_up_desired_amount_usd).
    assert by_ticker["AAPL"]["selected_base_amount_usd"] == 1.0


def test_run_pipeline_bounded_reload_poisons_reference_when_accepting_corrected_base(
    tmp_path, monkeypatch
):
    # Codex review ("Avoid combining a stale price with a reloaded base"):
    # AAPL's amount is unchanged, but the CLEAN reload's own record for this
    # ticker has an unknown reference price (e.g. a confirmed amount-only
    # fill with a valid base but no confirmed price yet) alongside a
    # corrected base ($1 -> $2). The prior patch for "Accept corrected
    # allocation bases from a clean reload" would have kept the first
    # pass's stale KNOWN price ($100) while adopting the reload's corrected
    # base ($2) -- a combination neither pass actually produced. If the
    # afternoon price falls below that stale $100, the guard would compute
    # atop the doubled $2 base instead of taking the reload's own
    # missing_prior_price path.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=2.0, reference_price=100.0, run_id="am", source="manual",
                        selected_base_amount_usd=1.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                    )
                },
                True,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=2.0, reference_price=None, run_id="am2", source="manual",
                    selected_base_amount_usd=2.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                )
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT combine the stale $100 known price with the reload's
    # corrected $2 base -- route through missing_prior_price instead.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["selected_base_amount_usd"] == 2.0


def test_run_pipeline_bounded_reload_treats_base_only_change_as_conflict(tmp_path, monkeypatch):
    # Codex review ("Include base metadata in incomplete-reload conflicts"):
    # MSFT is unresolved on pass one, triggering the bounded reload. AAPL's
    # pass-one read is clean (amount $2, reference $100, base $2). The
    # reload reads the SAME amount and reference for AAPL but a CORRECTED
    # base ($2 -> $1) -- while an unrelated artifact elsewhere in this same
    # scan fails to parse (second_scan_had_failures=True). The
    # not-_reload_verified_complete disagreement predicate previously
    # compared only amount_usd/reference_price, so a base-only correction
    # looked like "agreement" and kept the stale, larger $2 base. If the
    # afternoon price falls below $100, resolve_top_up_desired_amount_usd
    # would then target double what the reload's corrected $1 base supports.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=2.0, reference_price=100.0, run_id="am", source="manual",
                        selected_base_amount_usd=2.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                    ),
                    "MSFT": PriorSameDayAllocation("MSFT", amount_usd=3.0, reference_price=None),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=2.0, reference_price=100.0, run_id="am2", source="manual",
                    selected_base_amount_usd=1.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                ),
                "MSFT": PriorSameDayAllocation(
                    "MSFT", amount_usd=3.0, reference_price=40.0, run_id="am", source="manual"
                ),
            },
            True,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # AAPL's base-only correction must be treated as a conflict -- poisoned
    # to unresolved, adopting the reload's corrected (smaller) $1 base
    # rather than keeping the stale $100/known + $2 combination.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["selected_base_amount_usd"] == 1.0


def test_run_pipeline_bounded_reload_poisons_stale_reference_on_smaller_clean_reload(
    tmp_path, monkeypatch
):
    # Codex review ("Discard stale references when a clean reload is
    # smaller"): MSFT is unresolved on pass one, triggering the bounded
    # reload. AAPL's pass-one read is a $5 fill at a $100 reference. The
    # CLEAN reload (second_scan_had_failures=False) reconciles this to a
    # confirmed $4 partial fill at a $90 reference -- a genuine correction,
    # smaller amount AND a different known reference. The existing
    # smaller-amount fallthrough kept the stale $5/$100 snapshot entirely
    # (amount alone can't prove which pass is more complete without
    # per-artifact identity, already an accepted, SAFE limitation). But
    # blindly keeping a KNOWN reference the clean reload has since
    # corrected is actively unsafe: if the afternoon price is $95, the
    # stale $100 reference passes the price-below-prior check and permits
    # a top-up, while the reload's own $90 reference would correctly
    # reject it.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=5.0, reference_price=100.0, run_id="am", source="manual", selected_base_amount_usd=2.0),
                    "MSFT": PriorSameDayAllocation("MSFT", amount_usd=3.0, reference_price=None, selected_base_amount_usd=2.0),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=4.0, reference_price=90.0, run_id="am2", source="manual", selected_base_amount_usd=2.0),
                "MSFT": PriorSameDayAllocation(
                    "MSFT", amount_usd=3.0, reference_price=40.0, run_id="am", source="manual", selected_base_amount_usd=2.0),
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT keep the stale $100 reference -- poisoned to unresolved,
    # keeping the first pass's larger $5 amount as the conservative floor.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_run_pipeline_bounded_reload_poisons_base_change_on_smaller_clean_reload(
    tmp_path, monkeypatch
):
    # Codex review ("Handle base corrections on smaller clean reloads"):
    # MSFT is unresolved on pass one, triggering the bounded reload. AAPL's
    # pass-one read is $5 fill / base $2 / reference $100. The CLEAN reload
    # reduces BOTH the amount ($5 -> $4) AND the base ($2 -> $1) while
    # leaving the reference UNCHANGED at $100 -- so the earlier
    # "discard stale references" patch (which only checks reference_price)
    # doesn't trigger, and the entire stale $5/$2/$100 snapshot survives
    # unmodified. Kept unpoisoned, the stale $2 base combined with the kept
    # $5 floor amount can still compute a target that permits a top-up the
    # reload's own corrected $1 base would not.
    import run_dca
    from near_close_dca import PriorSameDayAllocation

    calls: list = []

    def _fake_compute(now, trading_day, artifacts_dir, config_dir, **_kwargs):
        calls.append((now, trading_day, artifacts_dir, config_dir))
        if len(calls) == 1:
            return (
                {
                    "AAPL": PriorSameDayAllocation(
                        "AAPL", amount_usd=5.0, reference_price=100.0, run_id="am", source="manual",
                        selected_base_amount_usd=2.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                    ),
                    "MSFT": PriorSameDayAllocation("MSFT", amount_usd=3.0, reference_price=None),
                },
                False,
            )
        return (
            {
                "AAPL": PriorSameDayAllocation(
                    "AAPL", amount_usd=4.0, reference_price=100.0, run_id="am2", source="manual",
                    selected_base_amount_usd=1.0, selected_base_amount_usd_at="2026-06-29T09:00:00-04:00",
                ),
                "MSFT": PriorSameDayAllocation(
                    "MSFT", amount_usd=3.0, reference_price=40.0, run_id="am", source="manual"
                ),
            },
            False,
        )

    monkeypatch.setattr(run_dca, "_compute_auto_derived_prior_allocations", _fake_compute)

    config = _demo_config()
    result = run_dca.run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path,
    )

    assert len(calls) == 2
    assert result["blocked"] is False
    import json

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Must NOT keep the stale $2 base -- poisoned to unresolved, keeping the
    # first pass's larger $5 amount as the conservative floor.
    assert by_ticker["AAPL"]["top_up_guard_result"] == "missing_prior_price"
    assert by_ticker["AAPL"]["top_up_manual_review"] is True
    assert by_ticker["AAPL"]["prior_same_day_amount"] == 5.0


def test_fills_from_orders_csv(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,filled,O1,100.0,0.5\n"
        "NVDA,submitted,O2,,\n",
        encoding="utf-8",
    )
    fills = _fills_from_orders_csv(csv_path)
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"


def test_fills_from_orders_csv_malformed(tmp_path):
    csv_path = tmp_path / "malformed_orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,,O1,100.0,0.5\n"
        "MSFT,filled,O2,,\n"
        "TSLA,FiLleD,O3,200.0,0.1\n"
        "GOOG\n"
        "AMZN,filled,O4,150.0,\n"
        "NFLX,filled,O5,,0.2\n",
        encoding="utf-8",
    )
    fills = _fills_from_orders_csv(csv_path)

    assert len(fills) == 1

    assert fills[0]["ticker"] == "TSLA"
    assert fills[0]["fill_price"] == "200.0"
    assert fills[0]["filled_qty"] == "0.1"
    assert fills[0]["order_id"] == "O3"

    csv_path2 = tmp_path / "missing_cols_orders.csv"
    csv_path2.write_text(
        "ticker,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,O1,100.0,0.5\n",
        encoding="utf-8",
    )
    fills2 = _fills_from_orders_csv(csv_path2)
    assert len(fills2) == 0


def test_execution_targets_from_orders_csv(tmp_path):
    from run_dca import _execution_targets_from_orders_csv

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,target_price\n"
        "AAPL,filled,O1,97.50\n"
        "NVDA,submitted,O2,\n"
        "MU,submitted,O3,-\n",
        encoding="utf-8",
    )
    targets = _execution_targets_from_orders_csv(csv_path)
    assert targets["AAPL"] == 97.50
    assert targets["NVDA"] is None
    assert targets["MU"] is None


def test_execution_targets_by_order_id_from_orders_csv_disambiguates_same_ticker_orders(tmp_path):
    """Regression test for "Match execution targets by order ID". A recovered
    order and a separate current-cycle order for the same ticker each get their
    own target_price row in orders.csv; the ticker-keyed map can only retain
    one of them, so the order-id-keyed map must expose both distinctly.
    """
    from run_dca import _execution_targets_by_order_id_from_orders_csv

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,target_price\n"
        "AAPL,filled,O-recovered,150.00\n"
        "AAPL,filled,O-current,97.50\n",
        encoding="utf-8",
    )
    targets = _execution_targets_by_order_id_from_orders_csv(csv_path)
    assert targets["O-recovered"] == 150.00
    assert targets["O-current"] == 97.50


def test_apply_execution_targets_keeps_a_zero_recovery_target_marked_missing():
    """Regression test for "Keep zero recovery targets marked as missing".
    A recovered crash-lease execution row stores target_price=0.0 as a
    no-target sentinel. That must not be accepted as a real target_price --
    the candidate must retain target_price_missing=True, same as if no
    target existed at all.
    """
    from run_dca import _apply_execution_targets_to_candidates

    candidates = [
        {
            "ticker": "AAPL",
            "order_id": "o-recovered",
            "target_price": None,
            "target_price_missing": True,
        }
    ]
    targets = {"AAPL": 0.0}
    targets_by_order_id = {"o-recovered": 0.0}
    _apply_execution_targets_to_candidates(candidates, targets, targets_by_order_id)
    assert candidates[0]["target_price_missing"] is True
    assert candidates[0]["target_price"] is None


def test_apply_execution_targets_still_accepts_a_real_positive_target():
    from run_dca import _apply_execution_targets_to_candidates

    candidates = [
        {
            "ticker": "AAPL",
            "order_id": "o-current",
            "target_price": None,
            "target_price_missing": True,
        }
    ]
    targets = {"AAPL": 97.50}
    targets_by_order_id = {"o-current": 97.50}
    _apply_execution_targets_to_candidates(candidates, targets, targets_by_order_id)
    assert candidates[0]["target_price"] == 97.50
    assert candidates[0]["target_price_missing"] is False


def test_refresh_fills_from_broker(tmp_path):
    from near_close_dca import OrderFill
    from run_dca import _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id\n"
        "AAPL,submitted,O1\n"
        "NVDA,filled,O2\n",
        encoding="utf-8",
    )

    class _Broker:
        def get_order_fill(self, order_id):
            # A fill has since settled for the previously-submitted order.
            if order_id == "O1":
                return OrderFill(avg_fill_price=100.0, quantity_filled=0.5)
            return None

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _Broker()), [])
    # O1 (submitted) resolved to a real fill. O2 (already "filled" in the CSV
    # but with no columns/data for get_order_fill() to recover) is included
    # as an UNPRICED fallback rather than dropped entirely -- see
    # test_refresh_fills_from_broker_keeps_a_notional_only_fill_unpriced.
    assert len(fills) == 2
    by_ticker = {f["ticker"]: f for f in fills}
    assert by_ticker["AAPL"]["fill_price"] == 100.0
    assert by_ticker["AAPL"]["filled_qty"] == 0.5
    assert by_ticker["NVDA"]["fill_price"] == ""
    assert by_ticker["NVDA"]["filled_qty"] == ""


def test_refresh_fills_from_broker_recovers_a_filled_row_missing_execution_details(tmp_path):
    """A row marked status='filled' in orders.csv but still missing
    avg_fill_price/quantity_filled is excluded from _fills_from_orders_csv's
    own result (its data is not yet usable) -- so _refresh_fills_from_broker
    must NOT also treat the bare 'filled' status as already-known and skip
    re-querying it, or the order can never be recovered even once
    get_order_fill() has complete data, and a real fill permanently reports
    as an execution gap (Codex review: "Refresh filled rows whose execution
    details are missing")."""
    from near_close_dca import OrderFill
    from run_dca import _fills_from_orders_csv, _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,filled,O1,,\n",
        encoding="utf-8",
    )

    initial_fills = _fills_from_orders_csv(csv_path)
    assert initial_fills == []  # excluded: incomplete execution data

    class _Broker:
        def get_order_fill(self, order_id):
            assert order_id == "O1"
            return OrderFill(avg_fill_price=101.5, quantity_filled=0.3)

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _Broker()), initial_fills)
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"
    assert fills[0]["fill_price"] == 101.5
    assert fills[0]["filled_qty"] == 0.3


def test_refresh_fills_from_broker_keeps_a_notional_only_fill_unpriced(tmp_path):
    """Regression test for "Propagate notional-only fills to full reporting".
    A crash-lease recovery valued only through executed_notional is durably
    "filled" in orders.csv but has no avg_fill_price/quantity_filled -- and
    get_order_fill() will never expose more than notional for it, so a
    re-query always returns None. Leaving such a row permanently excluded
    from `fills` made build_missing_fills() (which keys presence purely off
    ticker membership) report a broker-confirmed recovery as missing,
    understating coverage. It must be included as an UNPRICED fill instead.
    """
    from run_dca import _fills_from_orders_csv, _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,filled,O1,,\n",
        encoding="utf-8",
    )

    initial_fills = _fills_from_orders_csv(csv_path)
    assert initial_fills == []  # excluded: no granular breakdown at all

    class _NotionalOnlyBroker:
        def get_order_fill(self, order_id):
            # The broker genuinely has nothing more to offer for this order.
            return None

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _NotionalOnlyBroker()), initial_fills)
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"
    assert fills[0]["fill_price"] == ""
    assert fills[0]["filled_qty"] == ""


def test_refresh_fills_from_broker_never_requeries_a_recovered_lease_row(tmp_path):
    """Regression test for "Keep terminal notional-only recoveries unpriced".
    A row's action_reason == "recovered_inverse_martingale_lease" marks it as
    a terminal crash-lease recovery the recovery logger itself already
    deliberately left unpriced (preferring the retained terminal quantity
    over a get_order_fill() lookup, since that lookup can be eventually
    consistent and still return an earlier PARTIAL fill). Re-querying it
    again here, independently, at report-generation time would accept that
    same stale partial as if it were the terminal execution detail. It must
    never be queried at all -- not just discard a bad answer after the fact.
    """
    from dca_reporting import build_missing_fills
    from run_dca import _fills_from_orders_csv, _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled,action_reason\n"
        "AAPL,filled,O1,,,recovered_inverse_martingale_lease\n",
        encoding="utf-8",
    )
    initial_fills = _fills_from_orders_csv(csv_path)
    assert initial_fills == []

    class _StalePartialBroker:
        def __init__(self):
            self.queried = []

        def get_order_fill(self, order_id):
            self.queried.append(order_id)
            # A stale, eventually-consistent partial fill -- must never be
            # accepted for a recovered-lease row.
            from near_close_dca import OrderFill

            return OrderFill(avg_fill_price=50.0, quantity_filled=0.001)

    broker = _StalePartialBroker()
    fills = _refresh_fills_from_broker(csv_path, cast(Any, broker), initial_fills)
    assert broker.queried == [], "a recovered-lease row must never be re-queried"
    assert len(fills) == 1
    assert fills[0]["fill_price"] == ""
    assert fills[0]["filled_qty"] == ""

    # And it now correctly counts as covered, not missing.
    buy_candidates = [{"ticker": "AAPL", "status": "submitted", "order_id": "O1"}]
    assert build_missing_fills(buy_candidates, fills) == []


def test_refresh_fills_from_broker_overrides_a_stale_persisted_partial_for_a_recovery_row(
    tmp_path,
):
    """Regression test for "Override stale persisted fills for recovery
    rows". A failed attempt's preserved raw/orders_after_place.json can
    carry an EARLIER PARTIAL fill for an order later upgraded to a terminal
    notional-only recovery. _run_pipeline_impl loads that partial into
    `fills` BEFORE calling this function, so the order_id is already in
    `known` when the loop reaches this row -- the old code's `order_id in
    known` check short-circuited before ever reaching the recovery-marker
    branch, so the stale partial survived as the reported "terminal" fill.
    The recovery marker must override it (this function never calls
    get_order_fill() for the persisted-snapshot path at all).
    """
    from run_dca import _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled,action_reason\n"
        "AAPL,filled,O1,,,recovered_inverse_martingale_lease\n",
        encoding="utf-8",
    )
    # Simulates a stale partial already loaded from a persisted
    # orders_after_place.json snapshot before this function is called.
    stale_persisted_fills = [
        {"ticker": "AAPL", "fill_price": 50.0, "filled_qty": 0.001, "order_id": "O1"}
    ]

    class _NeverCalledBroker:
        def get_order_fill(self, order_id):
            raise AssertionError("must never be queried for a recovered-lease row")

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _NeverCalledBroker()), stale_persisted_fills)
    matching = [f for f in fills if f.get("order_id") == "O1"]
    assert len(matching) == 1, "the stale persisted partial must be replaced, not kept alongside"
    assert matching[0]["fill_price"] == ""
    assert matching[0]["filled_qty"] == ""


def test_refresh_fills_from_broker_preserves_a_priced_recovery_row(tmp_path):
    """Regression test for "Preserve granular details for priced recovery
    fills". When terminal recovery history supplies a valid fill price AND
    quantity, the recovery execution row is already complete -- the
    unpriced-recovery override must be restricted to genuinely notional-only
    rows, not blank out a valid granular breakdown just because the row
    carries the recovery action_reason.
    """
    from run_dca import _fills_from_orders_csv, _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled,action_reason\n"
        "AAPL,filled,O1,96.50,0.0725389,recovered_inverse_martingale_lease\n",
        encoding="utf-8",
    )
    initial_fills = _fills_from_orders_csv(csv_path)
    assert len(initial_fills) == 1  # already complete -- included as-is

    class _NeverCalledBroker:
        def get_order_fill(self, order_id):
            raise AssertionError("must never be queried for a recovered-lease row")

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _NeverCalledBroker()), initial_fills)
    matching = [f for f in fills if f.get("order_id") == "O1"]
    assert len(matching) == 1
    assert matching[0]["fill_price"] == "96.50"
    assert matching[0]["filled_qty"] == "0.0725389"


def test_refresh_fills_from_broker_skips_a_genuinely_complete_filled_row(tmp_path):
    """A row already carrying a complete fill (status='filled' plus a usable
    avg_fill_price/quantity_filled) must still be treated as already known --
    the patch for the incomplete case must not turn every filled row into a
    redundant broker re-query."""
    from run_dca import _fills_from_orders_csv, _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,filled,O1,100.0,0.5\n",
        encoding="utf-8",
    )

    initial_fills = _fills_from_orders_csv(csv_path)
    assert len(initial_fills) == 1

    calls = []

    class _Broker:
        def get_order_fill(self, order_id):
            calls.append(order_id)
            raise AssertionError("must not re-query an already-complete fill")

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _Broker()), initial_fills)
    assert calls == []
    assert fills == initial_fills


def test_refresh_fills_from_broker_dedupes_and_preserves_order(tmp_path):
    import threading
    import time as time_module

    from near_close_dca import OrderFill
    from run_dca import _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id\n"
        "AAPL,submitted,O1\n"
        "NVDA,submitted,O2\n"
        "MU,submitted,O3\n"
        "AAPL,submitted,O1\n",
        encoding="utf-8",
    )

    # Slowest order first, fastest last: a naive as_completed-based collection
    # would yield O3 before O1, exposing any loss of deterministic ordering.
    delays = {"O1": 0.06, "O2": 0.03, "O3": 0.0}
    call_order = []
    lock = threading.Lock()

    class _Broker:
        # Opts into the concurrent path; this is what it exercises.
        supports_concurrent_fill_lookup = True

        def get_order_fill(self, order_id):
            with lock:
                call_order.append(order_id)
            time_module.sleep(delays[order_id])
            return OrderFill(avg_fill_price=100.0, quantity_filled=1.0)

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _Broker()), [])

    # The duplicate O1 row must not trigger a second broker call.
    assert call_order.count("O1") == 1
    assert sorted(call_order) == ["O1", "O2", "O3"]

    # Results preserve CSV row order regardless of completion timing.
    assert [f["order_id"] for f in fills] == ["O1", "O2", "O3"]


def test_refresh_fills_from_broker_defaults_to_sequential(tmp_path):
    import threading
    import time as time_module

    from near_close_dca import OrderFill
    from run_dca import _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id\n"
        "AAPL,submitted,O1\n"
        "NVDA,submitted,O2\n"
        "MU,submitted,O3\n",
        encoding="utf-8",
    )

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    class _Broker:
        # No supports_concurrent_fill_lookup: must fall back to sequential calls,
        # since get_order_fill is not guaranteed safe to call from multiple threads.
        def get_order_fill(self, order_id):
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time_module.sleep(0.02)
            with lock:
                in_flight -= 1
            return OrderFill(avg_fill_price=100.0, quantity_filled=1.0)

    fills = _refresh_fills_from_broker(csv_path, cast(Any, _Broker()), [])

    assert max_in_flight == 1
    assert len(fills) == 3


def test_fills_from_missing_csv_is_empty(tmp_path):
    assert _fills_from_orders_csv(tmp_path / "nope.csv") == []


def test_cli_demo_exit_code(tmp_path):
    rc = main(["--demo", "--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "run_state.json").exists()


def test_cli_refuses_without_demo(tmp_path):
    rc = main(["--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 2


def test_cli_demo_does_not_touch_the_real_cron_tracking_root(tmp_path, monkeypatch):
    """Regression test for "Keep CLI demos out of the live accumulator".

    --demo is documented (CLAUDE.md's "third standard pre-handoff check",
    AGENTS.md) as a safe, synthetic-data smoke test that never touches live
    state ("Nothing contacts a broker or the network"). But run_pipeline()
    defaults to scheduled=True (unset by the --demo branch), and several
    trackers it drives along that path -- InverseMartingaleLedger among them
    -- gate their bookkeeping on `scheduled` alone, not `config.dry_run`.
    --artifacts-dir only isolates THIS run's own run_state/report artifacts;
    dca_paths.stream_root()'s separate $DCA_CRON_TRACKING_ROOT-derived tree
    those trackers read and write is a different root entirely. Enroll a
    real ticker so this demo run actually exercises the accumulator (a
    ~1.7% synthetic dip on AAPL crosses a 0.5%-threshold tier), point
    DCA_CRON_TRACKING_ROOT at a real-looking root with a pre-existing file,
    and confirm main() never writes into it -- and restores the env var
    afterward, since main() is also called in-process, repeatedly, by other
    tests in this same module.
    """
    import os
    from dataclasses import replace

    import run_dca

    real_root = tmp_path / "real_cron_tracking"
    real_root.mkdir()
    canary = real_root / "canary.txt"
    canary.write_text("do not touch", encoding="utf-8")
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(real_root))

    real_load_strategy_config = run_dca.load_strategy_config

    def enrolled_load_strategy_config(*args, **kwargs):
        config = real_load_strategy_config(*args, **kwargs)
        return replace(
            config,
            inverse_martingale={
                "AAPL": {"basis": "pct", "tiers": ((0.5, 1.0),), "recovery_offset": 1.0}
            },
        )

    monkeypatch.setattr(run_dca, "load_strategy_config", enrolled_load_strategy_config)

    # Capture the isolated temp root main() actually points trackers at,
    # instead of letting TemporaryDirectory clean it up on __exit__, so this
    # test can prove the accumulator really ran (and wrote there) rather than
    # the real root merely being untouched because nothing ever reached it.
    captured_isolated_root: list[str] = []
    real_temporary_directory = run_dca.tempfile.TemporaryDirectory

    class _NonCleaningTemporaryDirectory(real_temporary_directory):
        def __exit__(self, exc=None, value=None, tb=None):
            # TemporaryDirectory also registers a weakref.finalize at
            # construction time that deletes the directory on garbage
            # collection, independent of __exit__ -- detach it too, or the
            # directory vanishes the moment this object is collected.
            cast(Any, self)._finalizer.detach()
            captured_isolated_root.append(self.name)
            # Deliberately skip cleanup so the test can inspect it afterward.

    monkeypatch.setattr(run_dca.tempfile, "TemporaryDirectory", _NonCleaningTemporaryDirectory)

    artifacts_dir = tmp_path / "demo_artifacts"
    rc = run_dca.main(["--demo", "--now", "2026-07-06T15:45", "--artifacts-dir", str(artifacts_dir)])
    assert rc == 0

    # The env var must be restored to exactly what this test set it to.
    assert os.environ.get("DCA_CRON_TRACKING_ROOT") == str(real_root)
    # And the real root must be completely untouched -- still just the canary,
    # no inverse_martingale_dca/ (or any other stream tracker) directory.
    assert [p.name for p in real_root.iterdir()] == ["canary.txt"]

    # The accumulator DID run -- just against the isolated temp root, proving
    # this test is actually exercising the fix rather than passing trivially
    # because nothing ever touched a tracker.
    [isolated_root] = captured_isolated_root
    tracker_path = Path(isolated_root) / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert tracker_path.exists()


def test_cli_refuses_when_dry_run_false(tmp_path, monkeypatch):
    import run_dca

    monkeypatch.setattr(run_dca, "load_strategy_config", lambda *a, **k: StrategyConfig(dry_run=False))
    rc = run_dca.main(["--demo", "--artifacts-dir", str(tmp_path)])
    assert rc == 2


def test_cli_reloads_config_with_the_resolved_replay_now_for_ml_staleness(tmp_path, monkeypatch):
    """Regression test for "Validate ML staleness against the run timestamp".

    main() must reload config with `now=` set to the RESOLVED --now replay
    timestamp (not the real wall clock) before the run proceeds, so
    ml_sizing_adjustment.json's staleness is judged against the replay's own
    intended "now" -- otherwise a currently-live publish could be applied to
    historical decisions from the future relative to the replay, or a
    publish that was genuinely fresh at the replay date could be wrongly
    treated as stale, silently contaminating the replayed decisions.
    """
    import run_dca

    captured_now_kwargs = []
    real_load_strategy_config = run_dca.load_strategy_config

    def spying_load_strategy_config(*args, **kwargs):
        captured_now_kwargs.append(kwargs.get("now"))
        return real_load_strategy_config(*args, **kwargs)

    monkeypatch.setattr(run_dca, "load_strategy_config", spying_load_strategy_config)

    rc = run_dca.main(["--demo", "--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 0

    # Two loads: the preliminary one (now=None, used only to resolve the
    # decision_time_et fallback -- never the config the run actually uses),
    # then the real reload with now= the RESOLVED replay timestamp, not
    # whatever the real wall clock happens to be right now.
    assert len(captured_now_kwargs) == 2
    assert captured_now_kwargs[0] is None
    resolved_now = captured_now_kwargs[1]
    assert resolved_now is not None
    assert (resolved_now.year, resolved_now.month, resolved_now.day) == (2026, 7, 6)


def test_run_pipeline_persists_raw_inputs_before_compute(tmp_path):
    import json

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )
    assert result["blocked"] is False
    raw_file = scheduled_dir / "raw" / "quotes_batch1.json"
    assert raw_file.exists()
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["raw_input_dir"] == str(scheduled_dir / "raw")
    assert run_state["raw_inputs_saved"] == ["quotes_batch1.json"]


def test_run_pipeline_raw_persistence_failure_preserves_prior_successful_raw_files(tmp_path):
    # Codex review on PR #49: a same-day retry that fails to persist its own
    # raw connector responses must not destroy a PRIOR successful attempt's
    # durable raw/ files -- those are the audit/replay record for any live
    # orders that attempt already placed.


    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")

    first = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )
    assert first["blocked"] is False
    raw_file = scheduled_dir / "raw" / "quotes_batch1.json"
    assert raw_file.exists()
    prior_contents = raw_file.read_text(encoding="utf-8")

    second = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in second
    assert raw_file.exists(), "prior successful run's raw file was destroyed by the failed retry"
    assert raw_file.read_text(encoding="utf-8") == prior_contents


def test_a_failed_replay_validation_restores_the_prior_raw_snapshot(tmp_path):
    # Codex review ("Keep the prior raw snapshot until replay validation
    # succeeds"): if _build_frozen_providers_from_raw fails AFTER the staged
    # directory has already been renamed into raw/ (e.g. because
    # strict_robinhood finds no usable persisted batch), the swap had
    # already deleted raw.bak, so the failure handler's cleanup (which only
    # removes raw.tmp) permanently replaced the prior successful run's raw
    # audit snapshot with this attempt's unusable one. raw/ must be restored
    # to the prior successful attempt's contents in that case.
    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }
    first = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=good_inputs,
    )
    assert "raw_persistence_error" not in first
    raw_dir = scheduled_dir / "raw"
    good_robinhood_file = raw_dir / "quotes_batch1.json"
    assert good_robinhood_file.exists()
    prior_contents = good_robinhood_file.read_text(encoding="utf-8")

    # This attempt stages and swaps cleanly (valid JSON, normalizes fine),
    # but omits every Robinhood-recognized filename -- strict_robinhood then
    # raises AFTER raw/ already holds this attempt's finance-only files.
    finance_only_inputs = {
        "finance_bars_batch1.json": {"results": []},
        "finance_close_batch1.json": {"results": []},
    }
    second = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=finance_only_inputs,
    )

    assert "raw_persistence_error" in second
    assert not raw_dir.with_name("raw.bak").exists(), "backup must be cleaned up, not left dangling"
    assert good_robinhood_file.exists(), (
        "the prior successful run's Robinhood raw file was destroyed by the failed replay"
    )
    assert good_robinhood_file.read_text(encoding="utf-8") == prior_contents


def test_run_pipeline_raw_persistence_failure_writes_error_state(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    assert not (scheduled_dir / "orders.csv").exists()
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_persistence_error"]
    assert run_state["raw_inputs_saved"] == []
    assert run_state["buy_candidates"] == []


def _flaky_save_raw_input(monkeypatch, fail_times):
    """Patch save_raw_input to raise OSError for the first ``fail_times`` calls.

    OSError is the one failure class _recovery_trigger_for treats as retryable
    (a transient staging/rename/ENOSPC fault), so this drives the automatic
    failure-recovery path without touching any other behavior.
    """
    import run_dca as run_dca_module

    real_save = run_dca_module.save_raw_input
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise OSError("transient raw-write fault (injected)")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(run_dca_module, "save_raw_input", flaky)
    return calls


def test_run_pipeline_recovers_once_from_transient_raw_write_failure(tmp_path, monkeypatch):
    """A transient raw-write fault is recovered in-process, not escalated.

    The single permitted retry re-runs persist/normalize/swap/freeze, the run
    then continues through the UNCHANGED guarded path, and the resulting state
    is stamped recovery_rerun=true in the SAME run directory.
    """
    import json

    calls = _flaky_save_raw_input(monkeypatch, fail_times=1)
    config = _demo_config()
    scheduled_dir = tmp_path / "cron_tracking" / "ce16ba6b" / "scheduled_runs" / "20260706_1530ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )

    # Recovered, not escalated: the run completed normally.
    assert "raw_persistence_error" not in result
    assert result["blocked"] is False
    assert calls["n"] == 2, "expected exactly one retry after the injected failure"
    # raw/ was genuinely rewritten by the retry, in the same run directory.
    assert (scheduled_dir / "raw" / "quotes_batch1.json").exists()

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["recovery_rerun"] is True
    recovery = run_state["recovery"]
    assert recovery["trigger_reason"] == "raw_file_write_failure"
    assert recovery["attempt"] == 1
    assert recovery["persisted_raw_first"] is True
    assert recovery["recomputed_from_persisted"] is True
    assert recovery["escalated"] is False
    assert recovery["blocking_reason"] is None
    # Never reclassified as manual by the recovery.
    assert run_state["run_classification"]["run_origin"] == "scheduled"
    assert run_state["run_classification"]["manual_run"] is False


def test_run_pipeline_escalates_when_the_single_recovery_retry_also_fails(tmp_path, monkeypatch):
    """Only ONE retry is permitted; a still-failing retry escalates with evidence."""
    import json

    calls = _flaky_save_raw_input(monkeypatch, fail_times=99)
    config = _demo_config()
    scheduled_dir = tmp_path / "cron_tracking" / "ce16ba6b" / "scheduled_runs" / "20260706_1530ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )

    assert "raw_persistence_error" in result
    # Bounded: the initial attempt plus exactly one recovery retry, never more.
    assert calls["n"] == 2

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["recovery_rerun"] is True
    recovery = run_state["recovery"]
    assert recovery["trigger_reason"] == "raw_file_write_failure"
    assert recovery["attempt"] == 1
    assert recovery["escalated"] is True
    assert recovery["blocking_reason"]
    # Codex review ("Stamp exhausted recovery with recovery failure"): an
    # exhausted recovery is a distinct failure mode from an ordinary
    # first-attempt raw-persistence block, and error_class-keyed reporting
    # must be able to tell them apart.
    assert run_state["error_class"] == "recovery_failure"
    # The retry re-raised, so neither step completed -- reported honestly.
    assert recovery["persisted_raw_first"] is False
    assert recovery["recomputed_from_persisted"] is False
    # No orders on an escalated run.
    assert not (scheduled_dir / "orders.csv").exists()


def test_run_pipeline_does_not_retry_a_deterministic_raw_failure(tmp_path, monkeypatch):
    """A non-retryable failure escalates immediately without burning the attempt.

    An unserializable payload fails identically on a second identical attempt, so
    retrying it would waste the one permitted recovery and misreport a payload
    fault as an exhausted transient recovery. It must escalate on attempt 1 with
    NO recovery block.
    """
    import json

    import run_dca as run_dca_module

    class ExplodingPayload:
        pass

    real_save = run_dca_module.save_raw_input
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_save(*args, **kwargs)

    monkeypatch.setattr(run_dca_module, "save_raw_input", counting)

    config = _demo_config()
    scheduled_dir = tmp_path / "cron_tracking" / "ce16ba6b" / "scheduled_runs" / "20260706_1530ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )

    assert "raw_persistence_error" in result
    assert calls["n"] == 1, "a deterministic failure must not be retried"
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    # Unchanged from the pre-recovery behavior: no recovery block is claimed.
    assert "recovery" not in run_state
    assert run_state.get("recovery_rerun") is not True


def test_recovery_trigger_classification():
    """The classifier's boundaries, including the deliberate re-fetch exclusion."""
    import json

    from run_dca import RECOVERY_RETRY_TRIGGERS, _recovery_trigger_for

    assert _recovery_trigger_for(OSError("ENOSPC")) == "raw_file_write_failure"
    assert _recovery_trigger_for(PermissionError("denied")) == "raw_file_write_failure"
    assert (
        _recovery_trigger_for(RuntimeError("normalization failed for 'q.json': bad"))
        == "wrapper_normalization_failure"
    )
    assert _recovery_trigger_for(json.JSONDecodeError("x", "y", 0)) == "parsing_failure"
    # A contract/config fault is not a recovery trigger at all.
    assert _recovery_trigger_for(RuntimeError("requires non-empty raw_connector_inputs")) is None

    # Only the filesystem/IO class is retried in-process. Deterministic classes
    # classify (for reporting) but must never consume the single attempt.
    assert RECOVERY_RETRY_TRIGGERS == {"raw_file_write_failure"}
    assert "wrapper_normalization_failure" not in RECOVERY_RETRY_TRIGGERS
    assert "parsing_failure" not in RECOVERY_RETRY_TRIGGERS
    # run_dca.py cannot re-fetch connector data, so throttling is out of scope here.
    assert "transient_connector_throttling" not in RECOVERY_RETRY_TRIGGERS


def test_run_slot_raw_persistence_failure_preserves_the_slots_own_identity(tmp_path, monkeypatch):
    """A 10:30/12:30 run whose raw persistence fails must not stamp itself as
    the afternoon (15:30, cron_id 10ce362c) scheduled run either --
    raw_persistence_failure_run_state is the OTHER early-exit builder that
    must thread classification_overrides/correlation_id (Codex review:
    "Preserve slot identity on early failure artifacts"). Exercises this on
    the 10:30 slot specifically, so a same-day 9:45 preflight artifact must
    be present -- otherwise run_slot's own preflight gate now blocks the run
    before reaching raw persistence at all (Codex review: "Persist a missing
    preflight as a blocked morning run")."""
    import json

    import run_dca as run_dca_module
    from run_dca import run_slot

    # This test exercises raw-persistence failure, not the morning preflight
    # gate -- admit the full universe so the run reaches run_pipeline().
    monkeypatch.setattr(
        run_dca_module,
        "morning_preflight_eligible_tickers",
        lambda now, tickers, **kw: (list(tickers), {}),
    )

    class ExplodingPayload:
        pass

    config = _demo_config()
    cron_tracking_root = tmp_path / "cron_tracking"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(cron_tracking_root))
    preflight_dir = cron_tracking_root / "morning_dca_preflight" / "20260706_0945ET"
    preflight_dir.mkdir(parents=True)
    (preflight_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "morning_preflight_20260706",
            "trading_day": "2026-07-06",
            "overall_ok": True,
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "morning",
                "run_type": "morning_dca_preflight",
                "state_schema_version": "morning-preflight-v2-machine-complete",
            },
            "per_ticker_eligibility": [
                {"symbol": t, "eligible_to_execute": True} for t in config.tickers
            ],
        }),
        encoding="utf-8",
    )
    result = run_slot(
        "1030ET",
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    rc = run_state["run_classification"]
    assert rc["stream"] == "morning"
    assert rc["cron_id"] == "42173154"
    assert rc["run_id"] == "scheduled_20260706_1030ET"
    assert run_state["run_id"] == rc["run_id"]
    assert run_state["correlation_id"] == result["correlation_id"]
    assert run_state["correlation_id"] == "dca-2026-07-06-morning-1030et"


def test_run_pipeline_raw_persistence_failure_writes_contract_artifacts(tmp_path):
    # The raw-persistence fail-closed path must route through the scheduled artifact
    # contract: write a header-only submitted_orders.csv and a canonical
    # run_summary.json mirroring run_state's run_classification (signals.csv is
    # legitimately absent -- nothing was computed), overwriting stale copies.
    import csv as _csv
    import json

    from scripts.scheduled_artifacts import SUBMITTED_ORDERS_FIELDS, validate_run_artifacts

    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    # Seed stale contract artifacts from a prior successful attempt.
    (scheduled_dir / "submitted_orders.csv").write_text("STALE\n", encoding="utf-8")
    (scheduled_dir / "run_summary.json").write_text('{"stale": true}', encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result

    # header-only submitted_orders.csv (stale content overwritten)
    so_rows = list(_csv.reader((scheduled_dir / "submitted_orders.csv").open()))
    assert so_rows and so_rows[0] == list(SUBMITTED_ORDERS_FIELDS)
    assert len(so_rows) == 1  # header only, no simulated orders

    # canonical run_summary.json mirrors run_state's run_classification
    run_state = json.loads((scheduled_dir / "run_state.json").read_bytes())
    summary = json.loads((scheduled_dir / "run_summary.json").read_bytes())
    assert summary["run_classification"] == run_state["run_classification"]

    # the fail-closed state satisfies the contract (signals not required)
    assert validate_run_artifacts(
        scheduled_dir, require_signals=False, require_submitted_orders=True, require_summary=True
    )


def test_run_pipeline_raw_persistence_failure_clears_stale_compact_artifacts(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    # Seed stale derived-report artifacts from a prior attempt.
    # orders.csv and buy_candidates.csv are intentionally NOT seeded here; they are
    # preserved across retry failures so a successful prior run's fill records survive
    # (see test_run_pipeline_raw_persistence_failure_preserves_prior_order_artifacts).
    cleared_files = (
        "latest_run_summary.json", "slippage_and_coverage_report.md",
        "slippage_ranked.csv", "missing_fills_report.csv",
        "notion_latest_run_compact.md", "notion_latest_run_payload.json",
    )
    for name in cleared_files:
        (scheduled_dir / name).write_text("stale", encoding="utf-8")
    # signals.csv is REPLACED rather than removed: the stale content must not
    # survive, but the scheduled contract requires all four root-level files on
    # every outcome, so a fresh header-only file takes its place.
    (scheduled_dir / "signals.csv").write_text("stale", encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    for name in cleared_files:
        assert not (scheduled_dir / name).exists(), f"{name} was not removed"
    # The stale signals content is gone, replaced by a header-only file so the
    # failure directory still satisfies the four-file contract.
    signals_text = (scheduled_dir / "signals.csv").read_text(encoding="utf-8")
    assert "stale" not in signals_text
    assert signals_text.splitlines() == ["ticker"]
    from scripts.scheduled_artifacts import validate_run_artifacts

    assert validate_run_artifacts(scheduled_dir, require_summary=True)
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["broker"] == "robinhood"


def test_raw_persistence_retry_keeps_the_slots_prior_submitted_orders(tmp_path):
    """The failure state overwrites the slot's canonical run_state.json, so
    asserting zero orders would erase a prior attempt's accepted orders from
    scheduled fill and slippage results (Codex review)."""
    import run_dca as _r

    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    (scheduled_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "AAA,o-earlier,2.0,submitted\n"
        "BBB,,0,skipped\n",
        encoding="utf-8",
    )

    rows = _r._prior_submitted_order_rows(scheduled_dir)

    # Only the real submission is carried forward.
    assert [r["ticker"] for r in rows] == ["AAA"]
    assert rows[0]["order_id"] == "o-earlier"
    assert rows[0]["status"] == "submitted"


def test_prior_order_rows_keep_pending_and_unconfirmed_submissions(tmp_path):
    """An uncertain-but-possibly-accepted order must not be erased, or a later
    run can allocate it again (Codex review)."""
    import run_dca as _r

    run_dir = tmp_path / "slot"
    run_dir.mkdir()
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "AAA,o-1,2.0,pending\n"
        "BBB,o-2,2.0,unconfirmed\n"
        "CCC,,0,skipped\n",
        encoding="utf-8",
    )

    rows = _r._prior_submitted_order_rows(run_dir)

    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}


def test_the_raw_failure_state_on_disk_carries_the_prior_orders(tmp_path):
    """The mutation must happen BEFORE the state is serialized, or the on-disk
    state lacks the orders its companions record (Codex review)."""
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    (scheduled_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n" "AAA,o-earlier,2.0,submitted\n",
        encoding="utf-8",
    )
    import json as _json

    class ExplodingPayload:
        pass

    config = _demo_config()

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )

    assert "raw_persistence_error" in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    assert run_state["orders_submitted_count"] == 1
    assert [c["order_id"] for c in run_state["buy_candidates"]] == ["o-earlier"]
    # The aggregate fields agree with the carried count: the state must not claim
    # simultaneously that orders were and were not submitted (Codex review).
    assert run_state["order_submit_status"] == "submitted"
    assert run_state["run_classification_outcome"] == "failed_after_partial_submission"
    assert run_state["counts"]["submitted"] == 1
    assert run_state["is_trading_failure"] is True
    # The builder denies its own evidence, which is right for a run that placed
    # nothing. Here the slot DID place, and is_scheduled_state honors the same
    # marker, so leaving it denied would drop these fills from scheduled fill and
    # slippage reporting -- the exact loss the carry-forward prevents.
    from scripts.run_classification import is_scheduled_state
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    # The denial STAYS -- this run failed, so it is not evidence the slot
    # executed -- and the orders remain ATTRIBUTABLE, so reporting still sees
    # the fills. Two questions, two answers (Codex review).
    assert run_state["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(run_state) is False
    assert is_scheduled_state(run_state)


def test_a_carried_confirmation_required_row_leaves_the_denial_in_place(tmp_path):
    """Nothing reached the broker, so the slot did not execute and the state
    stays denied as evidence (Codex review)."""
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    (scheduled_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "AAA,,2.0,not_submitted_confirmation_required\n",
        encoding="utf-8",
    )
    import json as _json

    class ExplodingPayload:
        pass

    config = _demo_config()

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    # Kept as the slot's record...
    assert [c["ticker"] for c in run_state["buy_candidates"]] == ["AAA"]
    # ...but never counted as a submission.
    assert run_state["orders_submitted_count"] == 0
    assert run_state["counts"]["submitted"] == 0
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["scheduled_evidence_denied"] is True


def test_the_raw_persistence_state_does_not_assert_zero_orders():
    """An absent count is honest where a zero is wrong: this attempt placed
    nothing, but the slot's total is unknown to the builder (Codex review)."""
    from scripts.run_classification import raw_persistence_failure_run_state

    state = raw_persistence_failure_run_state("2026-07-06", "boom")

    assert "orders_submitted_count" not in state
    assert state["is_trading_failure"] is True
    assert state["order_submit_status"] == "not_submitted_error"
    # A concrete cause, not just the generic outcome: build_run_summary mirrors
    # error_class only when present (Codex review).
    assert state["error_class"] == "missing_or_invalid_raw_inputs"


def test_the_recovery_escalation_state_carries_its_own_error_class():
    """A failed recovery is not a raw-input failure."""
    from scripts.run_classification import recovery_escalation_run_state

    state = recovery_escalation_run_state(
        "2026-07-06", "internal_persistence_failure", "boom"
    )

    assert state["error_class"] == "recovery_failure"


@pytest.mark.parametrize("which", ["raw_persistence", "recovery_escalation"])
def test_a_fail_closed_builder_denies_its_own_scheduled_evidence(which):
    """The slot FIRED, which is not the same claim as the slot EXECUTED.

    Both states are stamped run_origin='scheduled'/manual_run=False, which is
    all derive_valid_scheduled_evidence reads, so without an explicit denial the
    pointer publishes a run that never computed a signal as valid evidence
    (Codex review). Both markers are checked: the top-level flag the derivation
    honors, and the nested value a consumer reading the block would trust.
    """
    from scripts.run_classification import (
        raw_persistence_failure_run_state,
        recovery_escalation_run_state,
    )
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    if which == "raw_persistence":
        state = raw_persistence_failure_run_state("2026-07-06", "boom")
    else:
        state = recovery_escalation_run_state(
            "2026-07-06", "internal_persistence_failure", "boom"
        )

    assert state["scheduled_evidence_denied"] is True
    assert cast(dict, state["run_classification"])["valid_scheduled_evidence"] is False
    assert derive_valid_scheduled_evidence(state) is False


def test_a_confirmation_required_row_is_carried_but_never_counted():
    """It belongs in the slot's record and is the opposite of a submission.

    The confirmation gate stopped the placement before the broker saw it, which
    is why the row has no order id. Counting it would report an accepted order
    for a slot that submitted nothing (Codex review).
    """
    from run_dca import _accepted_order_rows

    rows = [
        {"ticker": "AAPL", "status": "not_submitted_confirmation_required", "order_id": ""},
        {"ticker": "MSFT", "status": "pending", "order_id": "ord-1"},
    ]

    accepted = _accepted_order_rows(rows)

    assert [row["ticker"] for row in accepted] == ["MSFT"]
    # An id can only come from the broker, so a row carrying one is kept
    # whatever its status.
    assert _accepted_order_rows(
        [{"ticker": "AAPL", "status": "not_submitted_confirmation_required", "order_id": "ord-9"}]
    )


def test_run_pipeline_raw_persistence_failure_preserves_prior_order_artifacts(tmp_path):
    """A raw-persistence failure must not delete orders.csv or buy_candidates.csv.

    If a prior successful run in the same directory already placed orders,
    a subsequent retry that fails at the raw-persistence step must preserve
    those fill records so reporting and cap accounting are not lost.
    """
    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    # Simulate a prior successful run that already wrote real order artifacts.
    prior_orders_csv = scheduled_dir / "orders.csv"
    prior_candidates_csv = scheduled_dir / "buy_candidates.csv"
    prior_orders_csv.write_text("ticker,order_id\nAAPL,ord-123\n", encoding="utf-8")
    prior_candidates_csv.write_text("ticker,amount\nAAPL,7.50\n", encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    # Order artifacts from the prior successful run must survive.
    assert prior_orders_csv.exists(), "orders.csv was deleted by a retry failure"
    assert prior_orders_csv.read_text(encoding="utf-8") == "ticker,order_id\nAAPL,ord-123\n"
    assert prior_candidates_csv.exists(), "buy_candidates.csv was deleted by a retry failure"


def test_run_pipeline_live_run_missing_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    # The raw persistence error fires before any broker call, so a DryRunBroker is fine here.
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=None,
    )
    assert "raw_persistence_error" in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


def test_run_pipeline_live_run_empty_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs={},
    )
    assert "raw_persistence_error" in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


def test_run_pipeline_live_run_partial_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
        required_raw_filenames=frozenset({"quotes_batch1.json", "historicals_batch1.json"}),
    )
    assert "raw_persistence_error" in result
    assert "historicals_batch1.json" in result["raw_persistence_error"]
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


def test_run_pipeline_late_failure_after_run_artifact_written_is_not_a_failed_start(tmp_path, monkeypatch):
    # guarded_run's run_artifact_written probe distinguishes a crash BEFORE any
    # artifact exists from an ordinary late error AFTER run_state.json was
    # already persisted (the generate_full_reports=True path can fail during
    # report generation, well after run_daily_compact() wrote the run). This
    # production call site never passed the probe, so every escaping
    # exception recorded failure_phase='before_run_artifact' regardless --
    # codex review discussion_r3660464501.
    import json

    import run_dca as run_dca_module

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    artifacts_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)
    model_usage_log_root = tmp_path / "model_usage"

    real_impl = run_dca_module._run_pipeline_impl

    def _boom(*args, **kwargs):
        real_impl(*args, **kwargs)
        raise RuntimeError("report generation exploded after the run was written")

    monkeypatch.setattr(run_dca_module, "_run_pipeline_impl", _boom)

    with pytest.raises(RuntimeError, match="report generation exploded"):
        run_pipeline(
            _now(), config, StrategyState(),
            DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
            cast(Any, broker), artifacts_dir=artifacts_dir, generate_full_reports=True,
            model_usage_log_root=model_usage_log_root,
        )

    assert (artifacts_dir / "run_state.json").exists()
    attempt_log_root = model_usage_log_root.parent / "attempt_log"
    marker_path = next(attempt_log_root.glob("*/afternoon.json"))
    marker = json.loads(marker_path.read_bytes())
    assert marker["failure_phase"] == "after_run_artifact"


def test_run_pipeline_stale_run_artifact_is_not_credited_to_a_later_failed_invocation(tmp_path, monkeypatch):
    # A retry can reuse the SAME artifacts_dir as the attempt before it. An
    # existence-only probe would see the earlier invocation's run_state.json
    # and wrongly report after_run_artifact for a crash that THIS invocation
    # never got far enough to write anything of its own, attributing the
    # failure to a wholly unrelated earlier attempt's artifact -- codex
    # review discussion_r3660858013.
    import json
    import os

    import run_dca as run_dca_module

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    artifacts_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    artifacts_dir.mkdir(parents=True)
    stale_run_state = artifacts_dir / "run_state.json"
    stale_run_state.write_text('{"run_id": "stale_earlier_attempt"}', encoding="utf-8")
    # Force the stale file's mtime clearly into the past so this test cannot
    # flake on filesystem timestamp resolution.
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(stale_run_state, (old, old))

    broker = DryRunBroker(account=config.target_account_number)
    model_usage_log_root = tmp_path / "model_usage"

    def _boom(*args, **kwargs):
        raise RuntimeError("crashed before writing anything of its own")

    monkeypatch.setattr(run_dca_module, "_run_pipeline_impl", _boom)

    with pytest.raises(RuntimeError, match="crashed before writing anything"):
        run_pipeline(
            _now(), config, StrategyState(),
            DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
            cast(Any, broker), artifacts_dir=artifacts_dir, generate_full_reports=True,
            model_usage_log_root=model_usage_log_root,
        )

    # The stale file is untouched -- still exactly the earlier attempt's content.
    assert stale_run_state.read_text(encoding="utf-8") == '{"run_id": "stale_earlier_attempt"}'
    attempt_log_root = model_usage_log_root.parent / "attempt_log"
    marker_path = next(attempt_log_root.glob("*/afternoon.json"))
    marker = json.loads(marker_path.read_bytes())
    assert marker["failure_phase"] == "before_run_artifact"


def test_build_frozen_providers_strict_finance_raises_on_live_run(tmp_path):
    """A live run (dry_run=False) must fail closed when finance raw files are absent.

    Providing only Robinhood raw inputs satisfies raw persistence but
    _build_frozen_providers_from_raw must raise (not warn) when strict_finance=True
    and no finance_bars/finance_close batch files were written.
    """
    import json as _json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)
    rh_only_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
    }
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=rh_only_inputs,
    )
    assert "raw_persistence_error" in result, (
        "Expected raw_persistence_error when finance raw files absent on a live run"
    )
    assert "finance" in result["raw_persistence_error"].lower(), (
        f"Expected 'finance' in error message, got: {result['raw_persistence_error']!r}"
    )
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"


def test_build_frozen_providers_strict_robinhood_raises_on_live_run(tmp_path):
    """A live run (dry_run=False) must fail closed when Robinhood raw files are absent.

    Providing only finance raw inputs satisfies raw persistence but
    _build_frozen_providers_from_raw must raise (not warn) when strict_robinhood=True
    and no historicals/quotes batch files were written.
    """
    import json as _json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)
    finance_only_inputs = {
        "finance_bars_batch1.json": {"results": []},
        "finance_close_batch1.json": {"results": []},
    }
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=finance_only_inputs,
    )
    assert "raw_persistence_error" in result, (
        "Expected raw_persistence_error when Robinhood raw files absent on a live run"
    )
    assert "robinhood" in result["raw_persistence_error"].lower(), (
        f"Expected 'robinhood' in error message, got: {result['raw_persistence_error']!r}"
    )
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"


def test_build_frozen_providers_persists_live_corporate_action_evidence(tmp_path):
    """The frozen finance provider's get_corporate_action_factor must persist
    each live query's evidence under raw_dir before returning it, so a later
    replay of the saved raw snapshot can reproduce/audit the decision -- and
    must fail closed (return None) rather than use unpersisted evidence.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            return 2.0, as_of

    finance, _robinhood = _build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) == (2.0, as_of)

    persisted = _json.loads((raw_dir / "corporate_action_AAPL.json").read_text(encoding="utf-8"))
    assert persisted["ticker"] == "AAPL"
    assert persisted["since"] == since.isoformat()
    assert persisted["as_of"] == as_of.isoformat()
    assert persisted["factor"] == 2.0
    assert persisted["cursor"] == as_of.isoformat()


def test_build_frozen_providers_serializes_concurrent_action_retries(tmp_path):
    """Concurrent retries sharing raw/ must use one immutable action answer."""
    import json as _json
    import threading
    import time as _time

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")
    calls = []
    first_lookup_started = threading.Event()

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            calls.append(ticker)
            first_lookup_started.set()
            _time.sleep(0.05)
            return float(len(calls) + 1), as_of

    providers = [
        _build_frozen_providers_from_raw(
            LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
        )[0]
        for _ in range(2)
    ]
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    results = []
    first = threading.Thread(
        target=lambda: results.append(cast(Any, providers[0]).get_corporate_action_factor("AAPL", since, as_of))
    )
    second = threading.Thread(
        target=lambda: results.append(cast(Any, providers[1]).get_corporate_action_factor("AAPL", since, as_of))
    )
    first.start()
    assert first_lookup_started.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert calls == ["AAPL"]
    assert results == [(2.0, as_of), (2.0, as_of)]
    persisted = _json.loads((raw_dir / "corporate_action_AAPL.json").read_text(encoding="utf-8"))
    assert persisted["factor"] == 2.0


def test_corporate_action_persistence_holds_snapshot_lock(tmp_path, monkeypatch):
    """Delayed evidence writes cannot overlap another retry's raw swap."""
    from contextlib import contextmanager

    import run_dca
    import scripts.file_lock as file_lock

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text('{"results": []}', encoding="utf-8")
    held = False
    observed_lock_paths = []

    @contextmanager
    def recording_lock(path, *args, **kwargs):
        nonlocal held
        observed_lock_paths.append(Path(path))
        held = True
        try:
            yield
        finally:
            held = False

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            assert held
            return 2.0, as_of

    monkeypatch.setattr(file_lock, "cross_process_lock", recording_lock)
    finance, _ = run_dca._build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir
    )

    as_of = date(2026, 8, 18)
    assert cast(Any, finance).get_corporate_action_factor("AAPL", date(2026, 8, 17), as_of) == (
        2.0,
        as_of,
    )
    assert observed_lock_paths == [tmp_path / ".corporate_actions.lock"]


def test_build_frozen_providers_replays_persisted_corporate_action_evidence(tmp_path):
    """Reconstructing frozen providers from an already-persisted raw_dir must
    replay the SAME evidence a prior live query wrote, not re-issue a fresh
    live query that could answer differently -- otherwise the identical
    persisted snapshot could authorize a different fire on replay.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    (raw_dir / "corporate_action_AAPL.json").write_text(
        _json.dumps(
            {
                "ticker": "AAPL",
                "since": since.isoformat(),
                "as_of": as_of.isoformat(),
                "factor": 3.0,
                "cursor": as_of.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    live_calls = []

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            live_calls.append((ticker, since, as_of))
            return 999.0, as_of  # would prove the replay used the live path if returned

    finance, _robinhood = _build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) == (3.0, as_of)
    assert live_calls == []
    # A cache hit costs nothing externally -- near_close_dca's inverse-
    # Martingale wiring reads this flag to skip counting it against
    # usage_proxy.finance_calls_attempted.
    assert cast(Any, finance).get_corporate_action_factor.served_from_cache is True

    # A query the persisted snapshot never saw still falls through to live.
    other_since = date(2026, 8, 10)
    assert cast(Any, finance).get_corporate_action_factor("AAPL", other_since, as_of) == (999.0, as_of)
    assert live_calls == [("AAPL", other_since, as_of)]
    assert cast(Any, finance).get_corporate_action_factor.served_from_cache is False
    original = _json.loads((raw_dir / "corporate_action_AAPL.json").read_text(encoding="utf-8"))
    assert original["factor"] == 3.0
    distinct_query = _json.loads(
        (raw_dir / f"corporate_action_AAPL_{other_since.isoformat()}_{as_of.isoformat()}.json").read_text(
            encoding="utf-8"
        )
    )
    assert distinct_query["factor"] == 999.0


def test_build_frozen_providers_replays_persisted_unavailable_action_evidence(tmp_path):
    """A persisted fail-closed answer is evidence too and must suppress a
    later live retry just like a confirmed factor does.

    Otherwise an unavailable response recorded by the original scheduled run
    could become a confirmed split during replay, making the same ``raw/``
    snapshot produce a different latch decision.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    (raw_dir / "corporate_action_AAPL.json").write_text(
        _json.dumps(
            {
                "ticker": "AAPL",
                "since": since.isoformat(),
                "as_of": as_of.isoformat(),
                "factor": None,
                "cursor": None,
            }
        ),
        encoding="utf-8",
    )

    live_calls = []

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            live_calls.append((ticker, since, as_of))
            return 2.0, as_of

    finance, _robinhood = _build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )

    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) is None
    assert live_calls == []


@pytest.mark.parametrize(
    "cached_factor,cached_cursor",
    [
        (0.0, None),  # nonpositive factor
        (float("nan"), None),  # non-finite factor
        (-2.0, None),  # negative factor
        (2.0, "before_since"),  # cursor before since -- would let the action re-apply
        (2.0, "before_as_of"),  # cursor short of as_of -- an incomplete-window contract violation
    ],
)
def test_build_frozen_providers_rejects_a_malformed_cached_corporate_action(
    tmp_path, cached_factor, cached_cursor
):
    """A cached result must satisfy the exact same contract the live method
    itself guarantees (finite positive factor, cursor == as_of) -- a
    malformed persisted file or non-conforming custom provider must not
    silently move the durable cursor on bad data."""
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    cursor_map = {
        None: as_of.isoformat(),
        "before_since": date(2026, 8, 16).isoformat(),
        "before_as_of": date(2026, 8, 17).isoformat(),
    }
    (raw_dir / "corporate_action_AAPL.json").write_text(
        _json.dumps(
            {
                "ticker": "AAPL",
                "since": since.isoformat(),
                "as_of": as_of.isoformat(),
                "factor": cached_factor,
                "cursor": cursor_map[cached_cursor],
            }
        ),
        encoding="utf-8",
    )

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            return None

    finance, _robinhood = _build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) is None


@pytest.mark.parametrize(
    "live_result",
    [
        (0.0, "as_of"),  # nonpositive factor
        (float("nan"), "as_of"),  # non-finite factor
        (2.0, "before_as_of"),  # cursor short of as_of
        "not-a-tuple",  # malformed shape entirely
    ],
)
def test_build_frozen_providers_rejects_a_malformed_fresh_corporate_action(tmp_path, live_result):
    """A non-conforming FRESH result from a custom/injected provider must be
    rejected the same way a malformed cached one is, before it is either
    persisted or returned -- not silently pass through and let the durable
    cursor advance on bad data (Codex review: "Validate fresh action results
    before persisting them")."""
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    cursor_map = {"as_of": as_of, "before_as_of": date(2026, 8, 17)}
    resolved_result = (
        live_result
        if not isinstance(live_result, tuple)
        else (live_result[0], cursor_map[live_result[1]])
    )

    class LiveFinance(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            return resolved_result

    finance, _robinhood = _build_frozen_providers_from_raw(
        LiveFinance(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) is None
    persisted = _json.loads((raw_dir / "corporate_action_AAPL.json").read_text(encoding="utf-8"))
    assert persisted["factor"] is None
    assert persisted["cursor"] is None


def test_run_pipeline_refreshes_raw_input_manifest_after_compute_time_persistence(tmp_path, monkeypatch):
    """The frozen finance provider's live corporate-action lookups can write
    NEW raw/corporate_action_SYMBOL.json files DURING run_daily_compact, after
    build_raw_persistence_state() already captured the pre-compute manifest
    in _persist_raw_and_freeze(). run_pipeline must re-scan raw/ once compute
    finishes and patch the already-written run_state.json so raw_inputs_saved
    lists every file that actually influenced the run (Codex review: "Refresh
    the raw-input manifest after action persistence")."""
    import json as _json

    import run_dca
    import scripts.file_lock as file_lock

    config = load_strategy_config(approved_test_dates=frozenset({_now().date()}), dry_run=False)
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }

    real_run_daily_compact = run_dca.run_daily_compact

    def compute_time_writer(*args, **kwargs):
        # Simulate a live corporate-action lookup persisting new evidence
        # mid-compute, exactly like the real wrapper in
        # _build_frozen_providers_from_raw does.
        raw_dir = scheduled_dir / "raw"
        (raw_dir / "corporate_action_AAPL.json").write_text(
            _json.dumps(
                {
                    "ticker": "AAPL",
                    "since": "2026-07-05",
                    "as_of": "2026-07-06",
                    "factor": 1.0,
                    "cursor": "2026-07-06",
                }
            ),
            encoding="utf-8",
        )
        return real_run_daily_compact(*args, **kwargs)

    monkeypatch.setattr(run_dca, "run_daily_compact", compute_time_writer)

    real_lock = file_lock.cross_process_lock
    real_build_persistence_state = run_dca.build_raw_persistence_state
    action_lock_depth = 0
    manifest_lock_observations = []

    def _is_action_lock(path) -> bool:
        return Path(path) == scheduled_dir / ".corporate_actions.lock"

    from contextlib import contextmanager

    @contextmanager
    def observed_lock(path, *args, **kwargs):
        nonlocal action_lock_depth
        with real_lock(path, *args, **kwargs):
            if _is_action_lock(path):
                action_lock_depth += 1
            try:
                yield
            finally:
                if _is_action_lock(path):
                    action_lock_depth -= 1

    def observed_build_persistence_state(*args, **kwargs):
        manifest_lock_observations.append(action_lock_depth > 0)
        return real_build_persistence_state(*args, **kwargs)

    monkeypatch.setattr(file_lock, "cross_process_lock", observed_lock)
    monkeypatch.setattr(run_dca, "build_raw_persistence_state", observed_build_persistence_state)

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=good_inputs,
    )
    assert "raw_persistence_error" not in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    assert "corporate_action_AAPL.json" in run_state["raw_inputs_saved"]
    assert (scheduled_dir / "raw" / "corporate_action_AAPL.json").exists()
    # Both the initial manifest and the post-compute refresh are read while
    # the action-snapshot lock excludes another invocation's raw swap.
    assert manifest_lock_observations == [True, True]


def test_frozen_provider_snapshots_archive_fallback_over_null_live_evidence(tmp_path):
    """Archive evidence that replaces a null live answer must be replayable."""
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(
        _json.dumps({"results": []}), encoding="utf-8"
    )
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)

    class UnavailableLiveActions(DemoDataProvider):
        def get_corporate_action_factor(self, ticker, since, as_of):
            return None

    finance, _ = _build_frozen_providers_from_raw(
        UnavailableLiveActions(SourceName.FINANCE),
        DemoDataProvider(SourceName.ROBINHOOD),
        raw_dir,
    )
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) is None
    assert cast(Any, finance).persist_archive_corporate_action_factor(
        "AAPL", since, as_of, (0.5, as_of)
    )

    persisted = _json.loads((raw_dir / "corporate_action_AAPL.json").read_text(encoding="utf-8"))
    assert persisted["factor"] == pytest.approx(0.5)
    assert persisted["cursor"] == as_of.isoformat()
    assert persisted["source"] == "ticker_history_archive"
    # The ordinary lookup now replays the captured fallback and does not
    # depend on whatever the mutable global archive contains later.
    assert cast(Any, finance).get_corporate_action_factor("AAPL", since, as_of) == (0.5, as_of)


def test_run_pipeline_manifest_refresh_skips_a_concurrently_modified_run_state(
    tmp_path, monkeypatch, caplog
):
    """If another invocation rewrites run_state.json between this refresh's
    read and its own write, the refresh must skip rather than clobber the
    newer state -- the same re-read-before-write CAS guard the intraday-
    archive annotation path already uses (Codex review: "Guard the manifest
    rewrite against concurrent retries"). The concurrent write below keeps
    size and mtime unchanged to prove the guard compares actual file
    contents rather than a stat tuple (Codex review: "Use a portable change
    detector for concurrent writes")."""
    import json as _json
    import os

    import run_dca

    config = load_strategy_config(approved_test_dates=frozenset({_now().date()}), dry_run=False)
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }

    real_run_daily_compact = run_dca.run_daily_compact

    def compute_time_writer(*args, **kwargs):
        raw_dir = scheduled_dir / "raw"
        (raw_dir / "corporate_action_AAPL.json").write_text(
            _json.dumps(
                {
                    "ticker": "AAPL", "since": "2026-07-05", "as_of": "2026-07-06",
                    "factor": 1.0, "cursor": "2026-07-06",
                }
            ),
            encoding="utf-8",
        )
        return real_run_daily_compact(*args, **kwargs)

    monkeypatch.setattr(run_dca, "run_daily_compact", compute_time_writer)

    real_existing_csv_artifacts = run_dca._existing_csv_artifacts
    concurrent_run_id = "concurrent-retry-sentinel"
    concurrent_run_id_holder = {}

    def csv_artifacts_with_concurrent_write(run_dir):
        # Fires between this refresh's read and its re-read-before-write,
        # simulating another invocation finishing and rewriting
        # run_state.json in that window. Keep both size and mtime unchanged
        # (an os.utime-preserving in-place rewrite is exactly the case that
        # defeats a stat-tuple CAS check, e.g. on Windows where st_ctime is
        # creation time rather than a metadata-change time) to prove the
        # guard instead detects the change by comparing actual bytes.
        run_state_path = run_dir / "run_state.json"
        before = run_state_path.stat()
        payload = run_state_path.read_bytes()
        obj = _json.loads(payload)
        concurrent_run_id_padded = concurrent_run_id.ljust(len(obj["run_id"]), "-")[
            : len(obj["run_id"])
        ]
        concurrent_run_id_holder["value"] = concurrent_run_id_padded
        obj["run_id"] = concurrent_run_id_padded
        rewritten = _json.dumps(obj, indent=2, sort_keys=True).encode()
        assert len(rewritten) == len(payload)
        run_state_path.write_bytes(rewritten)
        os.utime(run_state_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return real_existing_csv_artifacts(run_dir)

    monkeypatch.setattr(run_dca, "_existing_csv_artifacts", csv_artifacts_with_concurrent_write)

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=good_inputs,
    )
    assert "raw_persistence_error" not in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    # The concurrent write survives untouched -- our own refresh backed off
    # rather than overwriting it with its stale in-memory copy.
    assert run_state["run_id"] == concurrent_run_id_holder["value"]
    assert "corporate_action_AAPL.json" not in run_state.get("raw_inputs_saved", [])
    assert "skipping raw-input manifest refresh" in caplog.text


def test_run_pipeline_manifest_refresh_holds_run_state_lock(tmp_path, monkeypatch):
    """The manifest refresh's read/modify/replace of run_state.json must all
    happen under the same ``run_state.json.lock`` sidecar write_run_artifacts
    itself takes for the primary publication -- a byte-comparison CAS guard
    alone (see the concurrent-modification test above) still leaves a window
    between its final compare and os.replace that a concurrent retry can win
    (PR #1357: "Fix corporate action isolation and state update races").
    Proven by observing the refresh's own CSV-byte sizing read -- the call that
    sits between its read of run_state.json and its rewrite -- and requiring
    that it ran with the lock held, pinned to the refresh (not some other lock
    site) by only counting reads made after the post-compute rescan and before
    the on-disk manifest lists the compute-time file."""
    import json as _json
    from contextlib import contextmanager

    import run_dca
    import scripts.file_lock as file_lock

    config = load_strategy_config(approved_test_dates=frozenset({_now().date()}), dry_run=False)
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    broker = DryRunBroker(account=config.target_account_number)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }

    real_run_daily_compact = run_dca.run_daily_compact

    def compute_time_writer(*args, **kwargs):
        # Simulate a live corporate-action lookup persisting new evidence
        # mid-compute, so the post-compute rescan finds a changed manifest
        # and the refresh's read-modify-write actually runs.
        raw_dir = scheduled_dir / "raw"
        (raw_dir / "corporate_action_AAPL.json").write_text(
            _json.dumps(
                {
                    "ticker": "AAPL", "since": "2026-07-05", "as_of": "2026-07-06",
                    "factor": 1.0, "cursor": "2026-07-06",
                }
            ),
            encoding="utf-8",
        )
        return real_run_daily_compact(*args, **kwargs)

    monkeypatch.setattr(run_dca, "run_daily_compact", compute_time_writer)

    # run_dca imports cross_process_lock at call time from scripts.file_lock,
    # so patching the module attribute is what its manifest refresh sees.
    # The real lock is still taken underneath -- only the held-state of the
    # run_state.json sidecar is tracked on top of it.
    real_lock = file_lock.cross_process_lock
    run_state_lock_depth = 0

    @contextmanager
    def observed_lock(path, *args, **kwargs):
        nonlocal run_state_lock_depth
        is_run_state_lock = Path(path).name == "run_state.json.lock"
        with real_lock(path, *args, **kwargs):
            if is_run_state_lock:
                run_state_lock_depth += 1
            try:
                yield
            finally:
                if is_run_state_lock:
                    run_state_lock_depth -= 1

    monkeypatch.setattr(file_lock, "cross_process_lock", observed_lock)

    real_build_persistence_state = run_dca.build_raw_persistence_state
    rescans = 0

    def counting_build_persistence_state(*args, **kwargs):
        nonlocal rescans
        rescans += 1
        return real_build_persistence_state(*args, **kwargs)

    monkeypatch.setattr(run_dca, "build_raw_persistence_state", counting_build_persistence_state)

    real_existing_csv_artifacts = run_dca._existing_csv_artifacts
    observations = []

    def csv_artifacts_observing_lock(run_dir):
        run_state_path = run_dir / "run_state.json"
        try:
            on_disk = _json.loads(run_state_path.read_bytes())
        except (OSError, ValueError):
            on_disk = {}
        observations.append(
            {
                "locked": run_state_lock_depth > 0,
                # The initial pre-compute manifest capture is rescan 1; the
                # post-compute refresh is rescan 2.
                "after_refresh_rescan": rescans >= 2,
                "manifest_lists_action": (
                    "corporate_action_AAPL.json" in on_disk.get("raw_inputs_saved", [])
                ),
            }
        )
        return real_existing_csv_artifacts(run_dir)

    monkeypatch.setattr(run_dca, "_existing_csv_artifacts", csv_artifacts_observing_lock)

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=good_inputs,
    )
    assert "raw_persistence_error" not in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)
    # The refresh really ran and published the compute-time file.
    assert "corporate_action_AAPL.json" in run_state["raw_inputs_saved"]
    # Its sizing read happened after the rescan, before the manifest was
    # rewritten -- i.e. inside the refresh's read-modify-write -- and every
    # such read held run_state.json.lock.
    refresh_reads = [
        o for o in observations if o["after_refresh_rescan"] and not o["manifest_lists_action"]
    ]
    assert refresh_reads, observations
    assert all(o["locked"] for o in refresh_reads), observations
    # Nothing leaked the lock past the pipeline's return.
    assert run_state_lock_depth == 0


def test_run_pipeline_skips_post_processing_when_ownership_not_verified(
    tmp_path, monkeypatch, caplog
):
    """Codex review ("Propagate foreign ownership to the outer pipeline"):
    run_daily_compact() can determine the shared artifacts directory
    belongs to a DIFFERENT invocation or account and correctly leave it
    untouched -- but this caller used to still trust
    result["run_state_path"] as its own output regardless, letting the
    raw-input manifest refresh and preflight_gate persistence rewrite that
    foreign artifact. When result["directory_ownership_verified"] is False,
    the pipeline must skip all of that post-processing and return
    immediately, the foreign state left byte-for-byte untouched."""
    import json as _json

    import run_dca

    config = load_strategy_config(approved_test_dates=frozenset({_now().date()}), dry_run=False)
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    broker = DryRunBroker(account=config.target_account_number)

    foreign_state = {
        "run_id": "foreign-owner-2026-07-06-1530ET",
        "trading_day": "2026-07-06",
        "run_classification": {
            "stream": "afternoon", "scheduled_for_et": "15:30", "run_origin": "scheduled",
        },
        "raw_inputs_saved": [],
    }
    run_state_path = scheduled_dir / "run_state.json"
    run_state_path.write_text(_json.dumps(foreign_state), encoding="utf-8")

    def foreign_owner_compact(*_args, **_kwargs):
        # Stands in for run_daily_compact() having already refused to touch
        # this directory -- the minimal result shape _run_pipeline_impl
        # reads before reaching the new ownership check.
        return {
            "blocked": False,
            "run_state_path": str(run_state_path),
            "directory_ownership_verified": False,
        }

    monkeypatch.setattr(run_dca, "run_daily_compact", foreign_owner_compact)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }

    with caplog.at_level("WARNING"):
        result = run_pipeline(
            _now(), config, StrategyState(),
            DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
            cast(Any, broker), artifacts_dir=scheduled_dir,
            raw_connector_inputs=good_inputs,
        )
    assert result.get("directory_ownership_verified") is False
    assert "could not verify ownership" in caplog.text

    # The foreign state must survive completely untouched -- no manifest
    # refresh, no preflight_gate rewrite.
    persisted = _json.loads(run_state_path.read_bytes())
    assert persisted == foreign_state


def test_run_pipeline_pings_a_failed_heartbeat_when_ownership_not_verified(
    tmp_path, monkeypatch
):
    """Codex review ("Fail the heartbeat on directory ownership
    conflicts"): the sibling test above confirms the pipeline skips
    post-processing when directory_ownership_verified is False, but
    guarded_run's own result_ok predicate never checked that flag --
    it only recognized raw_persistence_error and
    ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT. A scheduled LIVE run whose
    artifact publication was rejected due to an ownership conflict
    still returned a result result_ok read as healthy, clearing the
    success heartbeat and sending no failure alert even though its
    live placements may exist only in a manual-reconciliation
    sidecar."""
    import json as _json

    import dca_alerts
    import run_dca

    config = load_strategy_config(approved_test_dates=frozenset({_now().date()}), dry_run=False)
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)
    broker = DryRunBroker(account=config.target_account_number)

    foreign_state = {
        "run_id": "foreign-owner-2026-07-06-1530ET",
        "trading_day": "2026-07-06",
        "run_classification": {
            "stream": "afternoon", "scheduled_for_et": "15:30", "run_origin": "scheduled",
        },
        "raw_inputs_saved": [],
    }
    run_state_path = scheduled_dir / "run_state.json"
    run_state_path.write_text(_json.dumps(foreign_state), encoding="utf-8")

    def foreign_owner_compact(*_args, **_kwargs):
        return {
            "blocked": False,
            "run_state_path": str(run_state_path),
            "directory_ownership_verified": False,
        }

    monkeypatch.setattr(run_dca, "run_daily_compact", foreign_owner_compact)

    heartbeat_calls = []
    monkeypatch.setattr(
        dca_alerts, "send_heartbeat",
        lambda stream, ok: heartbeat_calls.append((stream, ok)),
    )
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **k: None)

    good_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
        "finance_bars_batch1.json": {"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]},
        "finance_close_batch1.json": {"results": [{"ticker": "AAPL", "previous_close": 149.0}]},
    }

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs=good_inputs,
    )
    assert result.get("directory_ownership_verified") is False

    assert len(heartbeat_calls) == 1
    _stream, ok = heartbeat_calls[0]
    assert ok is False


def test_build_frozen_providers_hides_corporate_action_lookup_for_legacy_providers(tmp_path):
    """A legacy/test finance provider without get_corporate_action_factor must
    not gain the capability just because it's backed by a real
    FinanceDataProvider instance -- near_close_dca's callable(...) check must
    see it as unavailable so it falls through to archived_split_factor rather
    than silently getting None from a frozen 1440-minute lookup with no
    persisted daily rows.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "finance_bars_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")

    finance, _robinhood = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert not callable(getattr(finance, "get_corporate_action_factor", None))


def test_build_frozen_providers_keeps_bid_ask_live(tmp_path):
    """get_bid_ask backs the final pre-placement spread circuit breaker
    (near_close_dca.quote_spread_pct), a last-moment safety check meant to
    catch price movement between signal computation and order placement.
    The frozen Robinhood provider must never let that breaker read the
    archived quote snapshot -- it should delegate to whatever live provider
    the caller originally injected, and fail closed (return None) when no
    live bid/ask capability is available, rather than falling back to the
    frozen data.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # A stale persisted quote with its own (deliberately different) price,
    # standing in for what a scheduler fetched well before placement time.
    (raw_dir / "quotes_batch1.json").write_text(
        _json.dumps({"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "1.0"}}]}),
        encoding="utf-8",
    )

    class LiveStub(DemoDataProvider):
        def get_bid_ask(self, ticker, as_of):
            return (50.0, 51.0)

    _finance, robinhood = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), LiveStub(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, robinhood).get_bid_ask("AAPL", _now()) == (50.0, 51.0)

    _finance_no_live, robinhood_no_live = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, robinhood_no_live).get_bid_ask("AAPL", _now()) is None


def test_build_frozen_providers_unwraps_doubly_nested_pointer_data(tmp_path):
    """Codex review on PR #95 (ported): unwrap all normalized data envelopes.

    When an output_path pointer targets a raw response that is itself
    {"data": {...}}, normalize_connector_payload nests it one layer deeper,
    producing {"output_path": ..., "data": {"data": {...}}} on disk (the
    same doubly-wrapped shape dca_reporting._order_list_from_payload already
    handles). The frozen-provider builder must unwrap both layers, not just
    one, or a persisted batch in this shape looks empty.
    """
    import json as _json

    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_dir.joinpath("historicals_batch1.json").write_text(
        _json.dumps({
            "data": {"data": {
                "results": [{
                    "symbol": "AAPL",
                    "interval": "5minute",
                    "bars": [{"begins_at": "2026-07-06T14:00:00-04:00", "close_price": "100.0"}],
                }],
            }},
        }),
        encoding="utf-8",
    )
    raw_dir.joinpath("quotes_batch1.json").write_text(_json.dumps({"results": []}), encoding="utf-8")

    robinhood_live = DemoDataProvider(SourceName.ROBINHOOD)
    _finance, robinhood = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), robinhood_live, raw_dir,
    )
    bars = robinhood.get_intraday_bars(
        "AAPL",
        datetime.combine(datetime(2026, 7, 6).date(), time(13, 0), tzinfo=EASTERN),
        _now(),
        5,
    )
    assert len(bars) == 1
    assert bars[0].close == 100.0


def test_build_frozen_providers_skips_unreadable_raw_files(tmp_path):
    from run_dca import _build_frozen_providers_from_raw

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_dir.joinpath("quotes_batch1.json").write_text(
        '{"results": [{"quote": {"symbol": "AAPL"}}]}',
        encoding="utf-8",
    )
    raw_dir.joinpath("aaa_corrupt.json").write_bytes(b"\xff\xfe\x00")

    _finance, robinhood = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert cast(Any, robinhood).get_bid_ask("AAPL", _now()) is None


def test_run_pipeline_retry_preserves_orders_after_place(tmp_path):
    """A successful retry must carry over orders_after_place.json from the prior raw/.

    Simulates a prior raw/ containing orders_after_place.json (written by a first
    successful attempt that placed an order). The second run's staging swap must
    preserve it so a broker-deduplicated retry that places no new order does not
    lose the durable fill snapshot from the original placement attempt.
    """
    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)

    # Seed a prior raw/ with orders_after_place.json as a prior run would leave it.
    prior_raw = scheduled_dir / "raw"
    prior_raw.mkdir()
    prior_oap_content = '{"orders": [{"ticker": "AAPL", "order_id": "prior-X1"}]}'
    (prior_raw / "orders_after_place.json").write_text(prior_oap_content, encoding="utf-8")

    # Second run with new raw connector inputs. The dry-run broker never calls
    # get_raw_placement_responses, so no new orders_after_place.json is written.
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )
    assert result["blocked"] is False
    preserved = scheduled_dir / "raw" / "orders_after_place.json"
    assert preserved.exists(), "orders_after_place.json was lost during staging swap"
    assert "prior-X1" in preserved.read_text(encoding="utf-8")


def test_run_pipeline_retry_preserves_corporate_action_evidence(tmp_path):
    """A same-artifacts-dir retry must carry over corporate_action_SYMBOL.json
    from the prior raw/ -- it is written DURING compute (by a live
    corporate-action lookup), not from raw_connector_inputs, so the staging
    swap would otherwise silently drop the prior attempt's evidence, the
    same class of loss the orders_after_place.json preservation above exists
    to prevent (Codex review: "Preserve action evidence across raw snapshot
    swaps")."""
    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)

    prior_raw = scheduled_dir / "raw"
    prior_raw.mkdir()
    prior_action_content = (
        '{"ticker": "AAPL", "since": "2026-07-01", "as_of": "2026-07-05", '
        '"factor": 2.0, "cursor": "2026-07-05"}'
    )
    (prior_raw / "corporate_action_AAPL.json").write_text(prior_action_content, encoding="utf-8")

    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )
    assert result["blocked"] is False
    preserved = scheduled_dir / "raw" / "corporate_action_AAPL.json"
    assert preserved.exists(), "corporate_action_AAPL.json was lost during staging swap"
    assert "2026-07-05" in preserved.read_text(encoding="utf-8")


def test_run_pipeline_partial_retry_merges_orders_after_place(tmp_path):
    """Partial retry: new orders merged with prior fill history, not overwritten.

    First attempt placed AAPL; raw/orders_after_place.json holds its fill.
    Retry places MSFT (broker deduplicates AAPL so no new AAPL response).
    After the retry, orders_after_place.json must contain both AAPL and MSFT.
    """
    import json as _json

    class _BrokerWithResponses(DryRunBroker):
        def get_raw_placement_responses(self):
            return [{"ticker": "MSFT", "order_id": "retry-Y2"}]

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1530ET"
    scheduled_dir.mkdir(parents=True)

    prior_raw = scheduled_dir / "raw"
    prior_raw.mkdir()
    (prior_raw / "orders_after_place.json").write_text(
        '{"orders": [{"ticker": "AAPL", "order_id": "prior-X1"}]}', encoding="utf-8"
    )
    # Provide valid Robinhood + Finance batch files so _build_frozen_providers_from_raw
    # does not raise under strict_robinhood=True / strict_finance=True (dry_run=False).
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text(
        '{"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "150.00"}}]}',
        encoding="utf-8",
    )
    finance_bars_file = tmp_path / "finance_bars.json"
    finance_bars_file.write_text(
        '{"results": [{"ticker": "AAPL", "interval_minutes": 5, "bars": []}]}',
        encoding="utf-8",
    )
    finance_close_file = tmp_path / "finance_close.json"
    finance_close_file.write_text(
        '{"results": [{"ticker": "AAPL", "previous_close": 149.0}]}',
        encoding="utf-8",
    )

    broker = _BrokerWithResponses(account=config.target_account_number)
    run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        cast(Any, broker), artifacts_dir=scheduled_dir,
        raw_connector_inputs={
            "quotes_batch1.json": {"output_path": str(quotes_file)},
            "finance_bars_batch1.json": {"output_path": str(finance_bars_file)},
            "finance_close_batch1.json": {"output_path": str(finance_close_file)},
        },
    )
    oap = scheduled_dir / "raw" / "orders_after_place.json"
    assert oap.exists(), "orders_after_place.json missing after partial retry"
    order_ids = {o["order_id"] for o in _json.loads(oap.read_bytes())["orders"]}
    assert "retry-Y2" in order_ids, "New MSFT order missing from merged file"
    assert "prior-X1" in order_ids, "Prior AAPL fill lost during partial retry"

# --- New live-stream schema contracts (market_open, intraday) ---------------
#
# Codex review ("Define run-classification contracts for the new streams"):
# schemas/market_open_run_classification.schema.json and
# schemas/intraday_run_classification.schema.json validate the run_classification
# shape config/scheduled_tasks.json promises for the 9:35 AM market-open task
# (2ba19526) and the 11:30/13:30/14:30 intraday task (7be82b97). Neither stream
# has a dedicated *_dca.py executor module of its own (see
# run_dca.DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT / DEFAULT_INTRADAY_LIVE_STATE_ROOT),
# so these documents are hand-constructed to the documented contract shape
# rather than generated by calling a real executor's run_classification(), the
# way test_morning_dca.py/test_midday_dca.py do for their own streams.


def _market_open_run_classification(*, manual: bool):
    if manual:
        return {
            "run_origin": "manual",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "trades": True,
            "manual_run": True,
            "cron_id": "2ba19526",
            "scheduled_for_et": None,
            "scheduled_window_start_et": None,
            "scheduled_window_end_et": None,
            "run_id": "manual_market_open_live_20260727_133501Z_abc123",
            "triggered_at_utc": "2026-07-27T13:35:01Z",
            "state_schema_version": "market-open-live-v1",
            "valid_afternoon_evidence": False,
        }
    return {
        "run_origin": "scheduled",
        "run_type": "market_open_dca_live",
        "stream": "market_open",
        "trades": True,
        "manual_run": False,
        "cron_id": "2ba19526",
        "scheduled_for_et": "09:35",
        "scheduled_window_start_et": "09:30",
        "scheduled_window_end_et": "09:50",
        "run_id": "market_open_live_20260727_0935ET",
        "triggered_at_utc": "2026-07-27T13:35:00Z",
        "state_schema_version": "market-open-live-v1",
        "valid_afternoon_evidence": False,
    }


def test_market_open_run_classification_validates_against_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "market_open_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    for rc in (_market_open_run_classification(manual=False), _market_open_run_classification(manual=True)):
        jsonschema.validate(rc, schema)


def test_market_open_run_classification_rejects_wrong_cron_id():
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "market_open_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    bad = _market_open_run_classification(manual=False)
    bad["cron_id"] = "42173154"  # the morning task's cron_id, not market-open's
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


_INTRADAY_SLOT_WINDOWS = {
    "11:30": ("11:25", "11:35", "1130"),
    "13:30": ("13:25", "13:35", "1330"),
    "14:30": ("14:25", "14:35", "1430"),
}


def _intraday_run_classification(slot: str, *, manual: bool):
    start, end, hhmm = _INTRADAY_SLOT_WINDOWS[slot]
    if manual:
        return {
            "run_origin": "manual",
            "run_type": "intraday_dca_live",
            "stream": "intraday",
            "trades": True,
            "manual_run": True,
            "cron_id": "7be82b97",
            "scheduled_for_et": None,
            "scheduled_window_start_et": None,
            "scheduled_window_end_et": None,
            "run_id": "manual_intraday_live_20260727_133501Z_abc123",
            "triggered_at_utc": "2026-07-27T18:30:01Z",
            "state_schema_version": "intraday-live-v1",
            "valid_afternoon_evidence": False,
        }
    return {
        "run_origin": "scheduled",
        "run_type": "intraday_dca_live",
        "stream": "intraday",
        "trades": True,
        "manual_run": False,
        "cron_id": "7be82b97",
        "scheduled_for_et": slot,
        "scheduled_window_start_et": start,
        "scheduled_window_end_et": end,
        "run_id": f"intraday_live_20260727_{hhmm}ET",
        "triggered_at_utc": "2026-07-27T18:30:00Z",
        "state_schema_version": "intraday-live-v1",
        "valid_afternoon_evidence": False,
    }


def test_intraday_run_classification_validates_against_json_schema():
    # Every one of the three fired slots (11:30/13:30/14:30 ET, see
    # config/scheduled_tasks.json:multi_slot_task_note) plus a manual run
    # must validate.
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "intraday_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    docs = [_intraday_run_classification(slot, manual=False) for slot in _INTRADAY_SLOT_WINDOWS]
    docs.append(_intraday_run_classification("11:30", manual=True))
    for rc in docs:
        jsonschema.validate(rc, schema)


def test_intraday_run_classification_rejects_mismatched_slot_window():
    # A run_id/window pair from a DIFFERENT slot than scheduled_for_et must
    # be rejected -- each slot's window is pinned independently (Codex
    # review: "Include the 11:30 intraday run in midday context").
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "intraday_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    bad = _intraday_run_classification("11:30", manual=False)
    bad["scheduled_window_start_et"] = "13:25"  # the 13:30 slot's window, not 11:30's
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_intraday_run_classification_rejects_ad_hoc_recovery_stream_suffix():
    # The ad hoc one-off intraday scheduled-recovery stream (e.g. the
    # 2026-07-24 'intraday_1430' recovery) uses a slot-suffixed stream value
    # recognized by near_close_dca._prior_run_is_scheduled's startswith
    # check, but it is NOT this recurring live task and must not validate
    # against this schema, which pins stream to exactly 'intraday'.
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "intraday_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    bad = _intraday_run_classification("14:30", manual=False)
    bad["stream"] = "intraday_1430"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_carried_prior_order_rows_are_schema_valid_candidates(tmp_path):
    """These dicts are assigned straight to run_state["buy_candidates"], so they
    must satisfy the schema's candidate shape. csv.DictReader returns every field
    as a string and the row carries no buy_multiplier, so the raw row produced a
    canonical failure state schema-based consumers reject while the lightweight
    validator still passed (Codex review)."""
    import json as _json

    import run_dca as _r

    run_dir = tmp_path / "slot"
    run_dir.mkdir()
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "AAA,o-1,2.50,submitted\n"
        "BBB,o-2,3.00,unconfirmed\n",
        encoding="utf-8",
    )

    rows = _r._prior_submitted_order_rows(run_dir)

    schema = _json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "run_state.schema.json").read_text(
            encoding="utf-8"
        )
    )
    item_schema = schema["properties"]["buy_candidates"]["items"]
    for row in rows:
        for field in item_schema["required"]:
            assert field in row, field
        assert isinstance(row["planned_amount_usd"], float)
        assert row["planned_amount_usd"] >= 0
        # The concrete broker status is preserved, never remapped: the same-day
        # guard counts 'submitted'/'filled' and deliberately does not count
        # 'unconfirmed', so rewriting it would change which rows book capital.
        assert row["status"] in item_schema["properties"]["status"]["enum"]
    assert [r["status"] for r in rows] == ["submitted", "unconfirmed"]
    assert rows[0]["planned_amount_usd"] == 2.5


def test_a_malformed_amount_does_not_break_the_carried_row(tmp_path):
    """A truncated or non-numeric amount still has to produce a schema-valid
    number rather than propagating a string into the canonical state."""
    import run_dca as _r

    run_dir = tmp_path / "slot"
    run_dir.mkdir()
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nAAA,o-1,notanumber,submitted\n",
        encoding="utf-8",
    )

    rows = _r._prior_submitted_order_rows(run_dir)

    assert rows[0]["planned_amount_usd"] == 0.0
    assert isinstance(rows[0]["planned_amount_usd"], float)


def test_a_blank_or_padded_ticker_never_reaches_the_canonical_state(tmp_path):
    """A blank ticker would produce a schema-valid but meaningless candidate row,
    and the same-day guards key on the ticker (Copilot review)."""
    import run_dca as _r

    run_dir = tmp_path / "slot"
    run_dir.mkdir()
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "  AAA  , o-1 , 2.50 ,submitted\n"
        ",o-2,3.00,submitted\n"
        "   ,o-3,3.00,submitted\n",
        encoding="utf-8",
    )

    rows = _r._prior_submitted_order_rows(run_dir)

    assert [r["ticker"] for r in rows] == ["AAA"]
    assert rows[0]["order_id"] == "o-1"
    assert rows[0]["planned_amount_usd"] == 2.5


def test_a_whitespace_only_order_id_is_not_a_concrete_order(tmp_path):
    """Stripped before the gate, so it cannot pass as a real id and then be
    normalized away to None afterwards."""
    import run_dca as _r

    run_dir = tmp_path / "slot"
    run_dir.mkdir()
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nAAA,   ,2.00,skipped\n",
        encoding="utf-8",
    )

    assert _r._prior_submitted_order_rows(run_dir) == []


def test_fallback_discovery_roots_keep_nested_configured_roots(tmp_path, monkeypatch):
    # Codex review (P1): "Preserve nested configured roots for fallback
    # discovery". _resolve_cross_stream_prior_allocation_roots prunes a
    # configured root nested under afternoon_root because the CORE recursive
    # scan already covers its state files -- but the bare-CSV fallback never
    # scans afternoon_root, so it still needs that root. Without it, a recovery
    # whose fills exist only in an unreferenced sibling CSV is invisible to
    # both, and the full amount gets bought again.
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    cron_base = tmp_path / "cron_tracking"
    afternoon_root = cron_base / "ce16ba6b"
    afternoon_root.mkdir(parents=True)
    nested = afternoon_root / "intraday_dca"
    nested.mkdir()
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", str(nested))

    core = _resolve_cross_stream_prior_allocation_roots(None, afternoon_root)
    fallback = _resolve_cross_stream_prior_allocation_roots(
        None, afternoon_root, for_fallback_discovery=True
    )

    # Pruned from the core scan (afternoon_root covers it recursively)...
    assert nested not in core
    # ...but preserved for fallback discovery.
    assert nested in fallback


# --- Seven-slot roster: the live Gate 5 entry point --------------------------
#
# Gate 5 in claude/dca-executor-prompts/*.txt says "Run the repo's own signal
# generation for this slot ... Use the repo entry point, never a
# reimplementation." Until now no such entry point existed: main() is dry-run
# only (run_dca.py:2169, :2178) and the live path was an in-process
# run_pipeline() call the retired Perplexity scheduler supplied with MCP
# provider objects. See PRODUCTION_READINESS.md.
#
# Scope decision (2026-07-29): only the three canonical slots ship. They map 1:1
# onto the morning/midday/afternoon streams that same_day_top_up_policy already
# understands. The other four slots stay disabled because their fills are folded
# into no prior-allocation chain, so enabling them would let one ticker be
# bought repeatedly at rising prices.


def test_slot_map_exposes_only_the_three_canonical_slots():
    """A slot is enabled only if the top-up guard already models its stream."""
    from run_dca import SLOT_MAP

    assert SLOT_MAP == {
        "1030ET": ("morning", "1030ET"),
        "1230ET": ("midday", "1230ET"),
        "1530ET": ("afternoon", "1530ET"),
    }


def test_disabled_slots_are_the_four_unmodelled_ones():
    from run_dca import DISABLED_SLOTS

    assert set(DISABLED_SLOTS) == {"0935ET", "1130ET", "1330ET", "1430ET"}


@pytest.mark.parametrize("slot", ["0935ET", "1130ET", "1330ET", "1430ET"])
def test_run_slot_refuses_a_disabled_slot_and_places_nothing(slot, tmp_path):
    """The four unmodelled slots must abort, not silently run as some stream.

    An abort is the whole point: these slots have no prior-allocation chain, so
    a "successful" run here is what buys the same ticker twice in a day.
    """
    from run_dca import UnsupportedSlotError, run_slot

    config = _demo_config()
    broker = DryRunBroker(account=config.target_account_number)
    with pytest.raises(UnsupportedSlotError) as excinfo:
        run_slot(
            slot,
            now=_now_for_slot(slot),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=cast(Any, broker),
            artifacts_dir=tmp_path,
        )
    # The error has to say WHY, so the executor's alert is actionable rather
    # than looking like a crash.
    message = str(excinfo.value)
    assert slot in message
    assert "prior" in message.lower()
    assert broker.get_raw_placement_responses() == []
    # An aborted slot writes no run artifact at all.
    assert not (tmp_path / "run_state.json").exists()


def test_run_slot_rejects_an_unknown_slot_label(tmp_path):
    from run_dca import UnsupportedSlotError, run_slot

    config = _demo_config()
    with pytest.raises(UnsupportedSlotError):
        run_slot(
            "0900ET",
            now=_now(),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=cast(Any, DryRunBroker(account=config.target_account_number)),
            artifacts_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "slot,stream",
    [("1030ET", "morning"), ("1230ET", "midday"), ("1530ET", "afternoon")],
)
def test_run_slot_runs_the_pipeline_under_the_slots_own_stream(slot, stream, tmp_path, monkeypatch):
    """run_pipeline() hardcoded AFTERNOON_STREAM (run_dca.py:1193).

    Every slot ran under the afternoon stream's guarded_run identity, heartbeat,
    and attempt log. A morning slot must not report itself as the afternoon run.
    """
    import run_dca as run_dca_module
    from run_dca import run_slot

    seen = {}

    def _capture(stream_name, invoke, *args, **kwargs):
        seen["stream"] = stream_name
        return invoke()

    monkeypatch.setattr(run_dca_module, "guarded_run", _capture)
    # This test exercises stream attribution, not the morning preflight gate.
    monkeypatch.setattr(
        run_dca_module,
        "morning_preflight_eligible_tickers",
        lambda now, tickers, **kw: (list(tickers), {}),
    )

    config = _demo_config()
    result = run_slot(
        slot,
        now=_now_for_slot(slot),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )

    assert seen["stream"] == stream
    assert result["run_state_path"]


@pytest.mark.parametrize(
    "slot,expected",
    [
        ("1030ET", "dca-2026-07-06-morning-1030et"),
        ("1230ET", "dca-2026-07-06-midday-1230et"),
        ("1530ET", "dca-2026-07-06-afternoon-1530et"),
    ],
)
def test_run_slot_records_the_deterministic_correlation_id(slot, expected, tmp_path):
    """scripts/correlation.py already models a run as (day, stream, window).

    The id must be derivable independently by the scheduler and the runner, so
    it is asserted literally rather than round-tripped.
    """
    from run_dca import run_slot

    config = _demo_config()
    result = run_slot(
        slot,
        now=_now_for_slot(slot),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    assert result["correlation_id"] == expected


def test_prior_slots_returns_earlier_canonical_slots_in_order():
    from run_dca import prior_slots

    assert prior_slots("1030ET") == ()
    assert prior_slots("1230ET") == ("1030ET",)
    assert prior_slots("1530ET") == ("1030ET", "1230ET")


@pytest.mark.parametrize(
    "slot,expected_cron_id",
    [("1030ET", "42173154"), ("1230ET", "a6239dae"), ("1530ET", "10ce362c")],
)
def test_run_slot_logs_model_usage_under_the_slots_own_cron_id(slot, expected_cron_id, tmp_path, monkeypatch):
    """Codex/Antigravity review: run_slot() omitted cron_id, so log_model_usage
    always recorded the afternoon task id (10ce362c) for every slot."""
    import run_dca as run_dca_module
    from run_dca import run_slot

    captured = {}

    def _fake_log_model_usage(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(run_dca_module, "log_model_usage", _fake_log_model_usage)
    # This test exercises model-usage cron_id attribution, not the morning
    # preflight gate.
    monkeypatch.setattr(
        run_dca_module,
        "morning_preflight_eligible_tickers",
        lambda now, tickers, **kw: (list(tickers), {}),
    )

    config = _demo_config()
    run_slot(
        slot,
        now=_now_for_slot(slot),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
        model_usage_log_root=str(tmp_path / "model_usage"),
    )

    assert captured.get("cron_id") == expected_cron_id


@pytest.mark.parametrize("slot,expected_time", [("1030ET", time(10, 30)), ("1230ET", time(12, 30)), ("1530ET", time(15, 30))])
def test_run_slot_anchors_decision_time_to_the_slot(slot, expected_time, tmp_path, monkeypatch):
    """Codex review discussion_r3679391268: a 1030/1230 slot must not evaluate
    signals against the bundled config's 15:30 decision anchor."""
    import run_dca as run_dca_module
    from run_dca import run_slot

    captured = {}
    real_run_pipeline = run_dca_module.run_pipeline

    def _capture(now, config, *args, **kwargs):
        captured["decision_time_et"] = config.decision_time_et
        return real_run_pipeline(now, config, *args, **kwargs)

    monkeypatch.setattr(run_dca_module, "run_pipeline", _capture)
    # This test exercises decision-time anchoring, not the morning preflight
    # gate.
    monkeypatch.setattr(
        run_dca_module,
        "morning_preflight_eligible_tickers",
        lambda now, tickers, **kw: (list(tickers), {}),
    )

    config = _demo_config()
    run_slot(
        slot,
        now=_now_for_slot(slot),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )

    assert captured["decision_time_et"] == expected_time


@pytest.mark.parametrize(
    "slot,stream,scheduled_for_et,cron_id",
    [
        ("1030ET", "morning", "10:30", "42173154"),
        ("1230ET", "midday", "12:30", "a6239dae"),
    ],
)
def test_run_slot_stamps_its_own_scheduled_classification(slot, stream, scheduled_for_et, cron_id, tmp_path):
    """Codex review discussion_r3679391261: a 1030/1230 slot must not stamp
    itself as the afternoon (15:30, cron_id 10ce362c) scheduled run -- that
    misattributes telemetry and hides the slot's fills from a later slot's
    prior-allocation scan (which keys off run_classification.stream)."""
    import json

    from run_dca import run_slot

    config = _demo_config()
    result = run_slot(
        slot,
        now=_now_for_slot(slot),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    rc = run_state["run_classification"]
    assert rc["stream"] == stream
    assert rc["cron_id"] == cron_id
    assert rc["scheduled_for_et"] == scheduled_for_et
    assert rc["run_id"] == f"scheduled_20260706_{slot}"
    # The afternoon-only lineage marker must not be carried onto another stream.
    assert "supersedes_cron_id" not in rc


def test_run_slot_leaves_the_afternoon_classification_unchanged(tmp_path):
    """Anti-entropy pin: the 1530ET slot -- and every other, non-roster caller
    of build_run_artifacts -- must reproduce the original afternoon identity
    byte-for-byte."""
    import json

    from run_dca import run_slot

    config = _demo_config()
    result = run_slot(
        "1530ET",
        now=_now_for_slot("1530ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    rc = run_state["run_classification"]
    assert rc["stream"] == "afternoon"
    assert rc["cron_id"] == "10ce362c"
    assert rc["scheduled_for_et"] == "15:30"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert rc["run_id"] == "scheduled_20260706_1530ET"


def test_run_slot_persists_the_correlation_id_into_run_state(tmp_path):
    """Codex review discussion_r3679633768: the deterministic correlation_id
    must survive in the durable run artifact, not only the in-process return
    value -- a process that exits or crashes after writing run_state.json must
    still let an operator recover the id that fired this run."""
    import json

    from run_dca import run_slot

    config = _demo_config()
    result = run_slot(
        "1030ET",
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert run_state["correlation_id"] == result["correlation_id"]
    assert run_state["correlation_id"] == "dca-2026-07-06-morning-1030et"


def test_run_slot_date_gate_block_preserves_the_slots_own_identity(tmp_path):
    """A 10:30/12:30 run blocked by the date/holiday gate must not stamp
    itself as the afternoon (15:30, cron_id 10ce362c) scheduled run: that
    misattributes watchdog/telemetry diagnosis for the slot that actually
    fired and drops its deterministic correlation id (Codex review: "Preserve
    slot identity on early failure artifacts"). _write_blocked_run_stub is one
    of the early-exit builders that must thread classification_overrides/
    correlation_id the same way the normal compact path does."""
    import json

    from run_dca import run_slot

    config = _demo_config()  # only approves 2026-07-06
    off_approved_day_now = _now_for_slot("1030ET", day=(2026, 7, 7))
    result = run_slot(
        "1030ET",
        now=off_approved_day_now,
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert run_state["status"] == "blocked"
    rc = run_state["run_classification"]
    assert rc["stream"] == "morning"
    assert rc["cron_id"] == "42173154"
    assert rc["scheduled_for_et"] == "10:30"
    assert rc["run_id"] == "scheduled_20260707_1030ET"
    assert "supersedes_cron_id" not in rc
    assert run_state["correlation_id"] == result["correlation_id"]
    assert run_state["correlation_id"] == "dca-2026-07-07-morning-1030et"


def test_run_slot_calendar_gate_takes_priority_over_a_missing_preflight(tmp_path, monkeypatch):
    """On a weekend/holiday/out-of-window date, the same-day 09:45 preflight
    artifact is legitimately absent too -- this must resolve as the ordinary
    date/holiday block (Codex review: "Apply the calendar gate before the
    preflight early return"), not be misdiagnosed as a failed safety
    prerequisite. Uses an out-of-approved-window date (like
    test_run_slot_date_gate_block_preserves_the_slots_own_identity) with no
    preflight artifact written either, so both gates would fire if checked
    independently -- the calendar gate's shape must win."""
    import json

    from run_dca import run_slot

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()  # only approves 2026-07-06
    off_approved_day_now = _now_for_slot("1030ET", day=(2026, 7, 7))
    result = run_slot(
        "1030ET",
        now=off_approved_day_now,
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
    )
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert run_state["status"] == "blocked"
    assert run_state["completion_reason"] == "date_or_holiday_gate_blocked"
    assert "error_class" not in run_state or run_state["error_class"] is None
    assert run_state["is_trading_failure"] is False


def test_run_slot_1030et_evaluates_full_universe_when_no_preflight_artifact_exists(
    tmp_path, monkeypatch
):
    """NON-BLOCKING as of 2026-08-02 (Geoff: "should not block any later
    runs"), superseding the old fail-closed contract (Codex review: "Route
    the morning slot through its required preflight gate", "Persist a
    missing preflight as a blocked morning run"). A missing/invalid same-day
    pre-flight artifact used to reduce the 10:30 slot's eligible universe to
    ZERO and short-circuit into a startup-failure-shaped blocked run_state.
    It now degrades to the FULL configured universe with a warning instead
    -- the same non-blocking contract morning_dca.py's own executor uses --
    and the run proceeds and evaluates normally; the run_pipeline() branch
    that used to build that blocked stub has been removed entirely, since
    preflight_missing_block is never set anymore."""
    import json

    from run_dca import run_slot

    # No preflight artifact is ever written under this root.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()
    assert config.tickers  # sanity: the configured universe is nonempty
    result = run_slot(
        "1030ET",
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
    )
    assert result.get("blocked") is not True
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    # Whole-share tickers (BYDDY; see near_close_dca.WHOLE_SHARE_TICKERS,
    # docs/whole_share_accumulation.md) are evaluated only on the afternoon
    # stream, so the 1030ET/morning slot's evaluated count is config.tickers only.
    assert run_state["counts"]["evaluated"] == len(config.tickers)
    assert run_state["run_classification"]["stream"] == "morning"
    assert run_state["run_classification"]["valid_afternoon_scheduled_evidence"] is False
    assert run_state["correlation_id"] == result["correlation_id"]
    # Codex review: "Emit the advisory preflight warning from run_slot" --
    # run_slot's preflight_gate check used to look for preflight_missing_block,
    # a flag nothing sets anymore, so the advisory "warning" was silently
    # dropped instead of being logged or persisted. It must now be stamped
    # onto the returned result for durable observability.
    assert result["preflight_gate"]["preflight_found"] is False
    assert "warning" in result["preflight_gate"]
    # Codex review: "Persist preflight diagnostics in the canonical
    # artifact" -- build_run_artifacts/run_daily_compact already publish
    # run_state.json to disk before _run_pipeline_impl can stamp
    # preflight_gate onto its in-memory return value, so the on-disk
    # artifact (not just the ephemeral Python dict) must carry it too --
    # that's what reporting and operators actually read after the fact.
    assert run_state["preflight_gate"] == result["preflight_gate"]
    assert run_state["preflight_gate"]["preflight_found"] is False
    assert "warning" in run_state["preflight_gate"]
    # Codex review: "Recompute usage bytes after rewriting the run state" --
    # run_daily_compact() already settled usage_proxy.state_file_bytes/
    # total_artifact_bytes against the smaller pre-rewrite payload before
    # preflight_gate was added, so those counters (which dca_cost_tracker.py
    # consumes directly as its artifact-I/O proxy) must be re-settled against
    # the actual on-disk size after this rewrite, not left stale.
    from near_close_dca import _existing_csv_artifacts

    usage = run_state["usage_proxy"]
    run_dir = Path(result["run_state_path"]).parent
    actual_state_bytes = Path(result["run_state_path"]).stat().st_size
    actual_csv_bytes = sum(p.stat().st_size for p in _existing_csv_artifacts(run_dir))
    assert usage["state_file_bytes"] == actual_state_bytes
    assert usage["total_artifact_bytes"] == actual_state_bytes + actual_csv_bytes


def test_run_slot_1030et_survives_a_failed_preflight_gate_artifact_rewrite(
    tmp_path, monkeypatch
):
    """Codex review: "Keep the diagnostic rewrite best-effort" -- the
    run_state.json read-modify-write that persists preflight_gate onto the
    canonical artifact is documented as best-effort (a full/unwritable
    artifact volume must not turn an already-completed trading run into an
    exception), but the original patch only guarded the read, not the
    subsequent atomic_write_text call. An OSError from that write must be
    swallowed (logged, not raised) so the rest of run_slot's return path --
    including everything generate_full_reports/raw-response persistence do
    afterward -- still completes."""
    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    def _raise_oserror(*args, **kwargs):
        raise OSError("simulated full/unwritable artifact volume")

    monkeypatch.setattr(run_dca, "atomic_write_text", _raise_oserror)

    result = run_dca.run_slot(
        "1030ET",
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
    )
    assert result.get("blocked") is not True
    # The in-memory return value is unaffected by the on-disk write failing.
    assert result["preflight_gate"]["preflight_found"] is False


def test_run_slot_1030et_still_runs_account_pinning_with_no_preflight_artifact(
    tmp_path, monkeypatch
):
    """A mismatched broker/account must still raise regardless of preflight
    artifact state -- account pinning is a standard execution gate every live
    path crosses, not something the (now-removed) preflight-block early
    return needed its own extra copy of."""
    import json

    from run_dca import run_slot

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()
    mismatched_broker = DryRunBroker(account=f"not-{config.target_account_number}")

    with pytest.raises(RuntimeError, match="Refusing to trade account"):
        run_slot(
            "1030ET",
            now=_now_for_slot("1030ET"),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=cast(Any, mismatched_broker),
            artifacts_dir=tmp_path / "artifacts",
        )
    run_state = json.loads((tmp_path / "artifacts" / "run_state.json").read_bytes())
    assert run_state["error_class"] == "scheduled_run_startup_failure"
    assert "Refusing to trade account" in run_state["error_detail"]
    assert run_state["run_classification_outcome"] == "failed_scheduled_run_startup"


def test_run_slot_1030et_skips_preflight_gate_rewrite_on_concurrent_state_change(
    tmp_path, monkeypatch
):
    """Codex review ("Reverify ownership before rewriting post-run state"):
    the preflight_gate persistence read-modify-write used to have neither a
    lock nor a byte comparison. Two overlapping live invocations of the
    same slot can both pass the earlier ownership check; without
    serializing this rewrite, invocation A reads run_state.json here,
    invocation B publishes its own newer state in the meantime, and A's
    write would overwrite B's state built from A's stale read -- dropping
    B's own submitted candidates from the canonical state. This test
    injects B's overwrite into the gap between A's initial read and A's
    final compare-before-write (via _existing_csv_artifacts, the one call
    made between those two reads whenever usage_proxy is present), and
    asserts A's write is skipped rather than clobbering B's state."""
    import json

    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A SEPARATE invocation of the same slot's own genuinely different,
    # already-published state -- standing in for invocation B.
    peer_state = {
        "run_id": "peer-b-1030et",
        "trading_day": "peer",
        "buy_candidates": [{"ticker": "MSFT", "status": "submitted", "order_id": "PEER-B-ORDER"}],
        "usage_proxy": {},
    }

    real_existing_csv_artifacts = run_dca._existing_csv_artifacts
    _calls = {"count": 0}

    def existing_csv_artifacts_then_peer_overwrites(run_dir):
        # Only the preflight_gate rewrite's OWN call reaches here in this
        # scenario (no raw_persistence_state is passed, so the sibling
        # manifest-refresh block above it never runs its own identical
        # call) -- landing exactly between A's initial read and A's final
        # compare-before-write, still inside A's own lock.
        _calls["count"] += 1
        if _calls["count"] == 1:
            run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return real_existing_csv_artifacts(run_dir)

    monkeypatch.setattr(run_dca, "_existing_csv_artifacts", existing_csv_artifacts_then_peer_overwrites)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        preflight_gate={"preflight_found": False, "warning": "test"},
    )
    # The in-memory return value still carries it (documented behavior)...
    assert result["preflight_gate"]["preflight_found"] is False
    # ...but B's on-disk state must survive completely untouched -- A's
    # stale-read rewrite must never have been written.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_rechecks_identity_when_a_peer_claims_the_directory_after_ownership_verified(
    tmp_path, monkeypatch
):
    """Codex review ("Recheck artifact identity inside each post-run
    lock"): the sibling test above (`..._skips_preflight_gate_rewrite_on_
    concurrent_state_change`) proves the byte comparison catches a peer
    that overwrites BETWEEN this rewrite's own initial read and its final
    compare-before-write, still inside the SAME lock. It does not cover
    the narrower, earlier gap this fix closes: a peer that publishes
    AFTER run_daily_compact() returned with directory_ownership_verified=
    True but BEFORE this rewrite's own lock is even acquired. The
    rewrite's own first read (taken only once the lock is held) is
    already that peer's payload in that case, so comparing the final
    read against IT (the old behavior) trivially matches. The fix binds
    the comparison to a snapshot captured immediately when ownership was
    last confirmed, before either rewrite does any further work of its
    own."""
    import json
    import sys

    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A SEPARATE invocation of the same slot's own genuinely different,
    # already-published state -- standing in for invocation B, publishing
    # in the gap BEFORE either rewrite's own lock is acquired at all.
    peer_state = {
        "run_id": "peer-b-1030et-pre-lock",
        "trading_day": "peer",
        "buy_candidates": [{"ticker": "MSFT", "status": "submitted", "order_id": "PEER-B-PRE-LOCK-ORDER"}],
        "usage_proxy": {},
    }

    real_read_bytes = Path.read_bytes
    _fired = {"value": False}

    def read_bytes_then_peer_claims_directory(self, *args, **kwargs):
        result = real_read_bytes(self, *args, **kwargs)
        # Fires exactly once, right after `_run_pipeline_impl`'s OWN
        # `_known_run_state_bytes` capture -- the pipeline-level snapshot
        # taken immediately after run_daily_compact() returned with
        # ownership verified, before the preflight_gate rewrite's own
        # lock is ever acquired.
        if (
            not _fired["value"]
            and self.name == "run_state.json"
            and sys._getframe(1).f_code.co_name == "_run_pipeline_impl"
        ):
            _fired["value"] = True
            run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "read_bytes", read_bytes_then_peer_claims_directory)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        preflight_gate={"preflight_found": False, "warning": "test"},
    )
    assert _fired["value"] is True
    # The in-memory return value still carries it (documented behavior)...
    assert result["preflight_gate"]["preflight_found"] is False
    # ...but B's on-disk state must survive completely untouched -- A's
    # rewrite must never have landed in B's file just because A's own
    # in-lock self-comparison trivially matched.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_binds_post_run_snapshot_to_run_daily_compacts_own_lock(
    tmp_path, monkeypatch
):
    """Codex review ("Bind the post-run snapshot to the ownership check"):
    a follow-up on the sibling test above
    (`..._rechecks_identity_when_a_peer_claims_the_directory_after_
    ownership_verified`), which only injects the peer AFTER `_run_pipeline_
    impl`'s own `_known_run_state_bytes` read -- proving the byte-compare
    inside each rewrite's lock works, but not closing the window one level
    earlier: a peer replacing run_state.json AFTER `run_daily_compact()`
    releases its own ownership lock but BEFORE `_known_run_state_bytes` is
    even captured. An unconditional re-read there would silently adopt the
    PEER's payload as "this invocation's last verified state" -- passing
    every later in-lock comparison trivially, since they all compare
    against that same now-wrong snapshot, and letting the downstream
    rewrites mutate the peer's artifact. The fix threads the exact bytes
    out of `run_daily_compact()`'s own lock instead of ever re-reading the
    shared path here, so this window must be closed even when the
    injection happens as early as immediately after `run_daily_compact()`
    itself returns."""
    import json

    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A SEPARATE invocation's own genuinely different, already-published
    # state -- standing in for invocation B, publishing in the gap
    # immediately after run_daily_compact() released its own ownership
    # lock but before this invocation's post-run snapshot is captured.
    peer_state = {
        "run_id": "peer-b-1030et-post-return",
        "trading_day": "peer",
        "buy_candidates": [
            {"ticker": "MSFT", "status": "submitted", "order_id": "PEER-B-POST-RETURN-ORDER"}
        ],
        "usage_proxy": {},
    }

    real_run_daily_compact = run_dca.run_daily_compact
    _fired = {"value": False}

    def run_daily_compact_then_peer_claims_directory(*args, **kwargs):
        result = real_run_daily_compact(*args, **kwargs)
        # Fires exactly once, immediately after run_daily_compact() itself
        # returns -- BEFORE `_run_pipeline_impl` captures its own
        # `_known_run_state_bytes` snapshot at all. The old, unconditional
        # re-read there would adopt this peer's bytes as "known"; the fix
        # must use the bytes `run_daily_compact()` itself returns instead.
        if not _fired["value"] and result.get("directory_ownership_verified") is True:
            _fired["value"] = True
            run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return result

    monkeypatch.setattr(run_dca, "run_daily_compact", run_daily_compact_then_peer_claims_directory)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        preflight_gate={"preflight_found": False, "warning": "test"},
    )
    assert _fired["value"] is True
    # The in-memory return value still carries it (documented behavior)...
    assert result["preflight_gate"]["preflight_found"] is False
    # ...but B's on-disk state must survive completely untouched -- neither
    # the raw-input-manifest refresh nor the preflight_gate rewrite may
    # bind their own "known" snapshot to the peer's bytes just because this
    # invocation re-read the shared path after the peer published.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_rechecks_ownership_immediately_before_raw_response_merge(
    tmp_path, monkeypatch
):
    """Codex review ("Recheck ownership immediately before downstream
    writes"): `_ownership_lost_mid_pipeline` was previously only ever set by
    the raw-manifest-refresh and preflight_gate rewrites above -- and each
    only performs its own in-lock ownership comparison when it actually has
    something to rewrite (`raw_persistence_state`/`preflight_gate` set).
    With NEITHER present (the scenario here), nothing at all rechecked
    ownership between `run_daily_compact()` confirming it and the raw-
    placement-response merge / full-report generation that follow -- so a
    peer publishing in that exact window went completely undetected, and
    both would have run against a directory this invocation no longer
    owned. Companion to the two rewrite-specific tests above -- this proves
    the recheck this fix adds is not merely redundant with them."""
    import json

    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A SEPARATE invocation's own genuinely different, already-published
    # state -- standing in for invocation B, publishing immediately after
    # run_daily_compact() returns and before either rewrite (both skipped
    # here) or this fix's own recheck runs.
    peer_state = {
        "run_id": "peer-b-1030et-no-rewrites",
        "trading_day": "peer",
        "buy_candidates": [
            {"ticker": "MSFT", "status": "submitted", "order_id": "PEER-B-NO-REWRITES-ORDER"}
        ],
        "usage_proxy": {},
    }

    real_run_daily_compact = run_dca.run_daily_compact
    _fired = {"value": False}

    def run_daily_compact_then_peer_claims_directory(*args, **kwargs):
        result = real_run_daily_compact(*args, **kwargs)
        if not _fired["value"] and result.get("directory_ownership_verified") is True:
            _fired["value"] = True
            run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return result

    monkeypatch.setattr(run_dca, "run_daily_compact", run_daily_compact_then_peer_claims_directory)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        generate_full_reports=True,
        # Neither `raw_persistence_state` nor `preflight_gate` is passed --
        # both existing rewrites are skipped entirely, so only this fix's
        # own recheck stands between run_daily_compact() and the merge/
        # report section below.
    )
    assert _fired["value"] is True
    assert result.get("directory_ownership_verified") is False
    # Full-report generation never ran against the peer's directory.
    assert "reports" not in result
    # B's on-disk state must survive completely untouched.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_fails_closed_when_the_ownership_recheck_cannot_be_verified(
    tmp_path, monkeypatch
):
    """Codex review (finding: "Fail closed when the ownership lock cannot be
    checked"): the pre-merge ownership recheck's own `except (TimeoutError,
    OSError):` handler used to only log a warning, leaving
    `_ownership_lost_mid_pipeline` False -- an UNVERIFIABLE check was
    treated the same as a VERIFIED one, even though a lock timeout can
    happen exactly because a peer is actively publishing right now, the
    precise scenario this recheck exists to catch. This proves a lock
    failure AT THAT FIRST RECHECK SPECIFICALLY fails closed -- isolated
    from the later report-generation lock (which has its own, separate
    fail-closed handling: see
    `test_run_pipeline_reverifies_ownership_at_the_report_generation_
    write_site`) by only failing the FIRST `run_state.json.lock`
    acquisition and letting every later one succeed normally, so this test
    cannot pass merely because some OTHER lock site also fails closed."""
    import run_dca
    import scripts.file_lock as file_lock_mod

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    real_cross_process_lock = file_lock_mod.cross_process_lock
    _run_state_lock_calls = {"count": 0}

    def timeout_on_first_run_state_lock_only(path, *args, **kwargs):
        if Path(path).name == "run_state.json.lock":
            _run_state_lock_calls["count"] += 1
            if _run_state_lock_calls["count"] == 1:
                raise TimeoutError("simulated: lock contended by an actively-publishing peer")
        return real_cross_process_lock(path, *args, **kwargs)

    monkeypatch.setattr(file_lock_mod, "cross_process_lock", timeout_on_first_run_state_lock_only)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        generate_full_reports=True,
    )
    # Proves the injected failure landed at the FIRST run_state.json.lock
    # acquisition (the pre-merge recheck) and nothing else -- if the pipeline
    # never even reached that lock, this test would prove nothing about the
    # recheck's own except-clause.
    assert _run_state_lock_calls["count"] >= 1
    # Fail closed: an unverifiable check is treated as ownership lost, not
    # as ownership confirmed.
    assert result.get("directory_ownership_verified") is False
    # Full-report generation never ran -- the unverifiable check stopped
    # execution at the same early-return guard a confirmed loss uses.
    assert "reports" not in result


def test_run_pipeline_reverifies_ownership_at_the_report_generation_write_site(
    tmp_path, monkeypatch
):
    """Codex review (finding: "Keep ownership through the downstream
    artifact writes"): the standalone pre-merge recheck acquires the lock,
    compares, and RELEASES it -- report generation (which reads
    run_state.json and then writes derived report/notion-sync files into
    the same directory) runs afterward, completely unprotected by that
    released lock. A peer publishing in the gap between the recheck's own
    lock release and report generation's read of run_state.json went
    completely undetected by the standalone recheck (which already
    confirmed ownership BEFORE the peer published).

    This proves the fix closes that specific gap: report generation's own
    read of run_state.json is now itself wrapped in a fresh lock+compare,
    immediately preceding use -- not a second standalone check released
    before the read, but the SAME lock scope as the read that feeds report
    generation. A peer publishing between the standalone recheck's release
    and this locked read is caught here, where the standalone recheck alone
    could not have caught it."""
    import json

    import run_dca
    import scripts.file_lock as file_lock_mod

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A separate invocation's own genuinely different, already-published
    # state -- standing in for a peer publishing in the gap between the
    # standalone recheck's lock release and report generation's own locked
    # read.
    peer_state = {
        "run_id": "peer-b-1030et-after-standalone-recheck",
        "trading_day": "peer",
        "buy_candidates": [
            {
                "ticker": "MSFT",
                "status": "submitted",
                "order_id": "PEER-B-AFTER-STANDALONE-RECHECK-ORDER",
            }
        ],
        "usage_proxy": {},
    }

    real_cross_process_lock = file_lock_mod.cross_process_lock
    _run_state_lock_calls = {"count": 0}

    def claim_directory_before_second_run_state_lock(path, *args, **kwargs):
        if Path(path).name == "run_state.json.lock":
            _run_state_lock_calls["count"] += 1
            # The FIRST run_state.json.lock acquisition is the standalone
            # pre-merge recheck -- let it pass normally, confirming
            # ownership fine (this invocation still owns the directory at
            # that point). The SECOND is report generation's own locked
            # read -- inject the peer's publish immediately before it is
            # acquired, simulating the peer winning the race in the exact
            # gap between the two.
            if _run_state_lock_calls["count"] == 2:
                run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return real_cross_process_lock(path, *args, **kwargs)

    monkeypatch.setattr(
        file_lock_mod, "cross_process_lock", claim_directory_before_second_run_state_lock
    )

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        generate_full_reports=True,
    )
    # Proves the injection actually landed at the intended point.
    assert _run_state_lock_calls["count"] >= 2
    # The standalone recheck alone would have seen ownership as fine (the
    # peer had not published yet) -- only the SECOND, report-generation-
    # site lock catches this.
    assert result.get("directory_ownership_verified") is False
    assert "reports" not in result
    # B's on-disk state must survive completely untouched -- report
    # generation never read it, let alone wrote derived files describing
    # it.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_keeps_ownership_across_full_report_publication(
    tmp_path, monkeypatch
):
    """Codex review (finding: "Keep ownership while publishing full
    reports"): the sibling test above
    (`..._reverifies_ownership_at_the_report_generation_write_site`) proves
    a peer publishing between the pre-merge recheck and report generation's
    own locked READ of `run_state.json` is caught. But that read-site lock
    itself released the moment the read completed -- fills/targets
    computation (including `_refresh_fills_from_broker`'s network calls)
    and the two actual DURABLE WRITES, `generate_reports()` and
    `write_compact_sync()`, all ran afterward with no lock held at all. A
    peer publishing in THAT gap -- after the read-site lock released but
    before the writes actually happen -- went completely undetected.

    This proves the fix closes that specific gap: a peer claiming the
    directory between the report-generation read lock and the point where
    reports are actually published is caught by the new write-site lock
    immediately preceding `generate_reports`/`write_compact_sync`, and
    neither write runs against the peer's directory."""
    import json

    import run_dca
    import scripts.file_lock as file_lock_mod

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    # A separate invocation's own genuinely different, already-published
    # state -- standing in for a peer publishing in the gap between report
    # generation's own locked read and the locked writes that follow it.
    peer_state = {
        "run_id": "peer-b-1030et-after-report-read",
        "trading_day": "peer",
        "buy_candidates": [
            {
                "ticker": "MSFT",
                "status": "submitted",
                "order_id": "PEER-B-AFTER-REPORT-READ-ORDER",
            }
        ],
        "usage_proxy": {},
    }

    real_cross_process_lock = file_lock_mod.cross_process_lock
    _run_state_lock_calls = {"count": 0}

    def claim_directory_before_third_run_state_lock(path, *args, **kwargs):
        if Path(path).name == "run_state.json.lock":
            _run_state_lock_calls["count"] += 1
            # 1st acquisition: the standalone pre-merge recheck. 2nd: report
            # generation's own locked read of run_state.json -- both left
            # alone, confirming ownership fine at each of those points. The
            # 3rd is the NEW write-site lock immediately before
            # generate_reports()/write_compact_sync() -- inject the peer's
            # publish immediately before THAT one is acquired, simulating
            # the peer winning the race in the gap this fix closes (the
            # fills/broker-refresh work that runs, deliberately unlocked,
            # between the 2nd and 3rd acquisitions).
            if _run_state_lock_calls["count"] == 3:
                run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return real_cross_process_lock(path, *args, **kwargs)

    monkeypatch.setattr(
        file_lock_mod, "cross_process_lock", claim_directory_before_third_run_state_lock
    )

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        generate_full_reports=True,
    )
    # Proves the injection actually landed at the intended (3rd) point.
    assert _run_state_lock_calls["count"] >= 3
    # The read-site lock alone would have seen ownership as fine (the peer
    # had not published yet) -- only the NEW write-site lock catches this.
    assert result.get("directory_ownership_verified") is False
    assert "reports" not in result
    assert "notion_sync" not in result
    # Neither durable write actually ran against the peer's directory.
    assert not (tmp_path / "artifacts" / "latest_run_summary.json").exists()
    # B's on-disk state must survive completely untouched.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_pipeline_propagates_report_write_failures_instead_of_ownership_loss(
    tmp_path, monkeypatch
):
    """Codex review (finding: "Do not swallow report write failures as
    ownership loss"): the write-site lock the sibling test above proves
    (`..._keeps_ownership_across_full_report_publication`) originally wrapped
    the ENTIRE `with cross_process_lock(...):` block -- including the calls
    to `generate_reports()`/`write_compact_sync()` themselves -- in a single
    `try/except (TimeoutError, OSError):`. A genuine write failure inside
    those calls (a full disk, a permission change, an atomic rename failing)
    is also an `OSError`, so it landed in the SAME except and got
    misclassified as an ownership-check failure: `directory_ownership_
    verified` was set to False and a normal-looking result was returned,
    instead of the exception propagating the way it did before this lock
    existed (through `guarded_run`, so a caller like the demo CLI would
    correctly exit non-zero on an incomplete publish).

    This proves the fix distinguishes the two: ownership is still verified
    (ordinary ownership ownership-loss paths are unaffected -- see the
    sibling tests), but once verified, a write failure from
    `generate_reports()` itself must raise, not silently report success."""
    import dca_reporting
    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _demo_config()

    def _boom(*args, **kwargs):
        raise OSError("disk full while writing latest_run_summary.json")

    monkeypatch.setattr(dca_reporting, "generate_reports", _boom)

    with pytest.raises(OSError, match="disk full"):
        run_dca._run_pipeline_impl(
            now=_now_for_slot("1030ET"),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=build_broker(config),
            artifacts_dir=tmp_path / "artifacts",
            scheduled=True,
            generate_full_reports=True,
        )


def test_run_pipeline_binds_post_run_snapshot_for_owned_blocked_runs(
    tmp_path, monkeypatch
):
    """Codex review ("Return a bound snapshot for owned blocked runs"): a
    calendar-blocked run whose ownership check returns True used to leave
    `run_daily_compact()`'s own result without a `_run_state_bytes_under_
    lock` key at all -- the ONLY normal-path branch that didn't populate
    it -- so this pipeline's `_known_run_state_bytes` fell back to its own
    unlocked read of the shared path before the raw-manifest and
    preflight_gate rewrites (both of which run even on a blocked result;
    see the `if result.get("blocked"): return` a few lines further down).
    A same-identity peer replacing the state between the blocked ownership
    check inside `run_daily_compact()` and this unlocked fallback read
    would become the accepted baseline, letting this invocation's own
    preflight_gate rewrite land in the peer's artifact."""
    import json

    import run_dca

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # This config approves NO trading day at all -- every call blocks on the
    # calendar gate.
    config = load_strategy_config(approved_test_dates=frozenset())

    run_state_path = tmp_path / "artifacts" / "run_state.json"

    peer_state = {
        "run_id": "peer-b-1030et-post-blocked-return",
        "trading_day": "peer",
        "buy_candidates": [
            {"ticker": "MSFT", "status": "submitted", "order_id": "PEER-B-BLOCKED-ORDER"}
        ],
        "usage_proxy": {},
    }

    real_run_daily_compact = run_dca.run_daily_compact
    _fired = {"value": False}

    def run_daily_compact_then_peer_claims_directory(*args, **kwargs):
        result = real_run_daily_compact(*args, **kwargs)
        # Fires exactly once, immediately after run_daily_compact() itself
        # returns from the calendar-blocked-but-owned branch -- BEFORE
        # `_run_pipeline_impl` captures its own `_known_run_state_bytes`
        # snapshot at all.
        if (
            not _fired["value"]
            and result.get("blocked") is True
            and result.get("directory_ownership_verified") is True
        ):
            _fired["value"] = True
            run_state_path.write_text(json.dumps(peer_state), encoding="utf-8")
        return result

    monkeypatch.setattr(run_dca, "run_daily_compact", run_daily_compact_then_peer_claims_directory)

    result = run_dca._run_pipeline_impl(
        now=_now_for_slot("1030ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path / "artifacts",
        scheduled=True,
        preflight_gate={"preflight_found": False, "warning": "test"},
    )
    assert _fired["value"] is True
    assert result["blocked"] is True
    # B's on-disk state must survive completely untouched -- the preflight_
    # gate rewrite (which still runs on a blocked result) must never bind
    # its own "known" snapshot to the peer's bytes just because this
    # invocation had nothing lock-bound to compare against.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted == peer_state


def test_run_slot_1230et_and_1530et_are_unaffected_by_a_missing_preflight_artifact(tmp_path, monkeypatch):
    """The 9:45 pre-flight gate is a MORNING-only requirement; the midday and
    afternoon slots must keep scanning their full configured universe
    regardless of whether a preflight artifact exists."""
    import json

    from run_dca import run_slot

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    for slot in ("1230ET", "1530ET"):
        config = _demo_config()
        result = run_slot(
            slot,
            now=_now_for_slot(slot),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=build_broker(config),
            artifacts_dir=tmp_path / f"artifacts_{slot}",
        )
        run_state = json.loads(Path(result["run_state_path"]).read_bytes())
        # Whole-share tickers (BYDDY) are only evaluated on the afternoon
        # stream (1530ET); 1230ET/midday sees config.tickers only.
        expected_evaluated = len(config.tickers) + (
            len(config.whole_share_tickers) if slot == "1530ET" else 0
        )
        assert run_state["counts"]["evaluated"] == expected_evaluated, slot


def test_run_slot_without_a_guarded_prior_allocation_still_blocks_a_same_day_repeat(tmp_path):
    """Safe default, unaffected by the guarded-top-up decision below: a ticker
    with NO loaded prior same-day allocation is not "guarded"
    (near_close_dca._process_buy_candidate's `ticker_guarded`), so the broad
    broker-level same-day check still gates it regardless of
    one_order_per_ticker_per_day.
    """
    import json

    from run_dca import run_slot

    config = _demo_config()
    captured = {}

    class _RecordingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            captured.setdefault("asked", []).append(ticker)
            return True

    # A bare tmp_path as artifacts_dir (not nested under a "scheduled_runs"
    # segment) makes _default_prior_allocation_state_root's documented
    # fallback land on tmp_path.parent -- the pytest session's SHARED base
    # temp directory once other tests' tmp_path fixtures are siblings under
    # it -- and the same-day scan can then pick up an unrelated test's
    # leftover run_state.json for this same fixed demo trading day. Nesting
    # under the canonical <task_root>/scheduled_runs/YYYYMMDD_1530ET/ layout
    # (config/scheduled_tasks.json; see the sibling tests above using the
    # same shape) keeps the derived root confined to this test's own
    # tmp_path, matching how a real scheduled run is actually rooted.
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1530ET"

    result = run_slot(
        "1530ET",
        now=_now_for_slot("1530ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=cast(Any, _RecordingBroker(account=config.target_account_number)),
        artifacts_dir=scheduled_dir,
    )

    # The broker, not local bookkeeping, is the authority (Gate 6).
    assert captured.get("asked")
    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    placed = [c for c in run_state["buy_candidates"] if c["status"] not in {"skipped", "blocked"}]
    assert placed == []
    reasons = {c.get("skip_reason") for c in run_state["buy_candidates"]}
    assert "same-day broker order exists" in reasons


@pytest.mark.parametrize("slot", ["1030ET", "1230ET", "1530ET"])
def test_run_slot_no_longer_forces_one_order_per_ticker_per_day(slot, tmp_path):
    """Human decision, 2026-07-30 (formerly documented in the now-removed GATE5
    proposal doc's #4 item 2): guarded top-up,
    not "first slot wins". run_slot must stop forcing
    one_order_per_ticker_per_day=True so near_close_dca's existing
    strictly-lower-price top-up guard (already exercised by
    test_flag_off_leaves_the_documented_top_up_bypass_intact and friends in
    test_near_close_dca.py) can run for these three canonical slots.
    """
    import run_dca as run_dca_module
    from run_dca import run_slot

    captured = {}
    real_run_pipeline = run_dca_module.run_pipeline

    def _capture(now, config, *args, **kwargs):
        captured["one_order_per_ticker_per_day"] = config.one_order_per_ticker_per_day
        return real_run_pipeline(now, config, *args, **kwargs)

    monkeypatch_target = run_dca_module
    original = monkeypatch_target.run_pipeline
    original_preflight = monkeypatch_target.morning_preflight_eligible_tickers
    monkeypatch_target.run_pipeline = _capture
    # This test exercises the one_order_per_ticker_per_day flag, not the
    # morning preflight gate.
    monkeypatch_target.morning_preflight_eligible_tickers = (
        lambda now, tickers, **kw: (list(tickers), {})
    )
    try:
        config = _demo_config()
        run_slot(
            slot,
            now=_now_for_slot(slot),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=build_broker(config),
            artifacts_dir=tmp_path,
        )
    finally:
        monkeypatch_target.run_pipeline = original
        monkeypatch_target.morning_preflight_eligible_tickers = original_preflight

    assert captured["one_order_per_ticker_per_day"] is False


def test_run_slot_allows_a_guarded_top_up_the_old_forced_flag_used_to_block(tmp_path):
    """End-to-end proof the flip above actually changes behavior: a ticker
    with a loaded prior same-day allocation at a strictly higher reference
    price (e.g. the morning slot buying it earlier the same day, at $105,
    while this slot's own decision price is ~$99.05 under DemoDataProvider)
    is no longer unconditionally blocked by the broker's broad same-day
    check the way it was when run_slot forced
    one_order_per_ticker_per_day=True -- it now reaches
    near_close_dca.evaluate_top_up_guard, which computes the real strictly-
    lower-price incremental top-up ($3.00 desired - $1.00 already bought =
    $2.00 incremental) instead of a flat block.

    DemoDataProvider has no get_bid_ask, so a *scheduled* run (spread_ok
    required) always blocks at the spread gate afterward regardless of this
    patch -- that gate is downstream of and irrelevant to the same-day guard
    this test targets, so the status itself is not asserted here.
    """
    import json

    from near_close_dca import PriorSameDayAllocation
    from run_dca import run_slot

    config = _demo_config()

    class _RecordingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True  # e.g. the morning slot's own earlier order for AAPL.

    prior = {
        "AAPL": PriorSameDayAllocation(
            ticker="AAPL", amount_usd=1.0, reference_price=105.0, selected_base_amount_usd=2.0,
        ),
    }

    result = run_slot(
        "1530ET",
        now=_now_for_slot("1530ET"),
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=cast(Any, _RecordingBroker(account=config.target_account_number)),
        artifacts_dir=tmp_path,
        # Isolated from the shared pytest tmp-dir tree: without this,
        # deployment_history_root defaults to artifacts_dir's PARENT (the
        # session-wide pytest base directory) and _apply_deployment_history's
        # recursive scan can pick up an unrelated sibling test's own
        # run_state.json -- including one deliberately carrying
        # deployed_notional_uncertain=True -- and force this test's kill
        # switch closed for a reason that has nothing to do with the
        # same-day top-up scenario this test actually targets.
        deployment_history_root=tmp_path / "no_deployment_history",
        prior_same_day_allocations=prior,
    )

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    aapl = by_ticker["AAPL"]
    assert aapl.get("skip_reason") != "same-day broker order exists"
    assert aapl.get("top_up_guard_result") == "incremental_top_up"
    assert aapl.get("top_up_skipped") is False
    assert aapl.get("incremental_order_amount") == 2.0


@pytest.mark.parametrize("slot", ["1030ET", "1230ET", "1530ET"])
def test_run_slot_rejects_an_off_schedule_invocation(slot, tmp_path):
    """Every live executor contract treats an off-schedule firing as a
    scheduler fault that must abort -- run_pipeline's generic execution gates
    only ever see the trading date, so without this run_slot("1030ET",
    now=<14:00 ET>) would merely anchor signal history to 10:30 and still
    place a live order hours off-schedule (Codex review: "Reject slot
    invocations outside their scheduled window")."""
    from run_dca import OffScheduleSlotError, run_slot

    config = _demo_config()
    off_schedule_now = _now_for_slot(slot).replace(hour=14, minute=0)
    with pytest.raises(OffScheduleSlotError) as excinfo:
        run_slot(
            slot,
            now=off_schedule_now,
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=build_broker(config),
            artifacts_dir=tmp_path,
        )
    assert slot in str(excinfo.value)
    assert not (tmp_path / "run_state.json").exists()


@pytest.mark.parametrize("offset_minutes", [-5, 0, 5])
def test_run_slot_accepts_the_edges_of_the_five_minute_window(offset_minutes, tmp_path):
    """The window is inclusive at both edges, matching the +/-5-minute
    scheduled_window_start_et/scheduled_window_end_et already stamped by
    _slot_classification_overrides."""
    from datetime import timedelta as _timedelta

    from run_dca import run_slot

    config = _demo_config()
    now = _now_for_slot("1030ET") + _timedelta(minutes=offset_minutes)
    result = run_slot(
        "1030ET",
        now=now,
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    assert result["run_state_path"]


@pytest.mark.parametrize("offset_minutes", [0, 2])
def test_run_slot_accepts_1530ets_own_narrower_start_window(offset_minutes, tmp_path):
    """1530ET's approved trigger_window is 15:30-15:32 ET, not the shared
    +/-5-minute tolerance: the 15:35 post-trade sync neither waits nor retries
    for an in-progress run (config/scheduled_tasks.json's "10ce362c" entry)."""
    from datetime import timedelta as _timedelta

    from run_dca import run_slot

    config = _demo_config()
    now = _now_for_slot("1530ET") + _timedelta(minutes=offset_minutes)
    result = run_slot(
        "1530ET",
        now=now,
        config=config,
        state=StrategyState(),
        finance_data=DemoDataProvider(SourceName.FINANCE),
        robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
        broker=build_broker(config),
        artifacts_dir=tmp_path,
    )
    assert result["run_state_path"]


@pytest.mark.parametrize("offset_minutes", [-1, 3, 5])
def test_run_slot_rejects_1530et_outside_its_narrower_start_window(offset_minutes, tmp_path):
    """A 15:34 firing must abort even though it is inside the shared
    +/-5-minute tolerance every other slot uses -- it would otherwise race the
    15:35 post-trade sync and risk a missing or partial final-run report
    (Codex review: "Enforce the final slot's 15:32 start cutoff")."""
    from datetime import timedelta as _timedelta

    from run_dca import OffScheduleSlotError, run_slot

    config = _demo_config()
    now = _now_for_slot("1530ET") + _timedelta(minutes=offset_minutes)
    with pytest.raises(OffScheduleSlotError):
        run_slot(
            "1530ET",
            now=now,
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            broker=build_broker(config),
            artifacts_dir=tmp_path,
        )
    assert not (tmp_path / "run_state.json").exists()


def test_run_slot_does_not_construct_its_own_broker(tmp_path):
    """The caller wires the live broker. run_slot must never build one itself.

    If run_slot could construct a broker it would be one refactor away from
    being able to place real orders from the dry-run-only CLI.
    """
    from run_dca import run_slot

    config = _demo_config()
    with pytest.raises(TypeError):
        run_slot(  # type: ignore[call-arg]
            "1530ET",
            now=_now(),
            config=config,
            state=StrategyState(),
            finance_data=DemoDataProvider(SourceName.FINANCE),
            robinhood_data=DemoDataProvider(SourceName.ROBINHOOD),
            artifacts_dir=tmp_path,
        )


def test_cli_still_refuses_to_run_a_slot_live(tmp_path, capsys):
    """--slot must not become a live path on the CLI. Gate 5 calls run_slot()
    in-process with wired MCP callables; the CLI stays dry-run only."""
    exit_code = main([
        "--slot", "1530ET",
        "--artifacts-dir", str(tmp_path),
        "--now", "2026-07-06T15:45:00",
    ])
    assert exit_code == 2

@patch("run_dca.atomic_write_text")
@patch("scripts.drift_detection.send_slack_alert")
@patch("scripts.drift_detection.detect_drift")
def test_run_drift_check(mock_detect_drift, mock_send_slack_alert, mock_atomic_write_text, tmp_path):
    """Test _run_drift_check handles drift reporting without invoking slack."""
    from run_dca import _run_drift_check

    report = mock_detect_drift.return_value
    report.drift_detected = False
    report.to_dict.return_value = {"status": "ok"}
    report.to_json.return_value = "{\"status\": \"ok\"}\n"
    report.to_markdown.return_value = "status: ok\n"

    assert _run_drift_check(tmp_path) == {"status": "ok"}
    mock_detect_drift.assert_called_once_with(tmp_path)
    mock_atomic_write_text.assert_any_call(tmp_path / "drift_report.json", "{\"status\": \"ok\"}\n")
    mock_atomic_write_text.assert_any_call(tmp_path / "drift_report.md", "status: ok\n")
    assert mock_atomic_write_text.call_count == 2
    mock_send_slack_alert.assert_not_called()


@pytest.mark.parametrize(
    "dt, expected_time",
    [
        # Pre-retime: Keep legacy 15:15 window
        (datetime(2026, 7, 26, 12, 0, tzinfo=EASTERN), time(15, 15)),
        # Exact retime date: Canonical 15:25 window
        (datetime(2026, 7, 27, 12, 0, tzinfo=EASTERN), time(15, 25)),
        # Post-retime date: Canonical 15:25 window
        (datetime(2026, 7, 28, 12, 0, tzinfo=EASTERN), time(15, 25)),
        # Pre-retime but provided in UTC (16:00 UTC -> 12:00 EDT)
        (datetime(2026, 7, 26, 16, 0, tzinfo=timezone.utc), time(15, 15)),
        # UTC retime-date timestamp that is still pre-retime in ET (00:30 UTC -> 20:30 EDT previous day)
        (datetime(2026, 7, 27, 0, 30, tzinfo=timezone.utc), time(15, 15)),
        # Post-retime but provided in UTC (16:00 UTC -> 12:00 EDT)
        (datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc), time(15, 25)),
        # UTC post-retime timestamp that is still on retime date in ET (00:30 UTC -> 20:30 EDT)
        (datetime(2026, 7, 28, 0, 30, tzinfo=timezone.utc), time(15, 25))
    ]
)
def test_final_buy_trend_override_window_start(dt, expected_time):
    assert _final_buy_trend_override_window_start(dt) == expected_time


@patch("run_dca.logging.basicConfig")
def test_configure_logging_default(mock_basic_config):
    configure_logging()
    mock_basic_config.assert_called_once_with(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@patch("run_dca.logging.basicConfig")
def test_configure_logging_custom_level(mock_basic_config):
    configure_logging("DEBUG")
    mock_basic_config.assert_called_once_with(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@patch("run_dca.logging.basicConfig")
def test_configure_logging_invalid_level(mock_basic_config):
    # If the level doesn't exist, getattr returns the default, which is INFO
    configure_logging("INVALID")
    mock_basic_config.assert_called_once_with(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def test_prior_allocation_live_state_roots_handles_config_failures(monkeypatch, tmp_path):
    from run_dca import _resolve_cross_stream_prior_allocation_roots

    monkeypatch.setattr(
        "run_dca.load_morning_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad morning")),
    )
    monkeypatch.setattr(
        "run_dca.load_midday_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad midday")),
    )
    roots = _resolve_cross_stream_prior_allocation_roots(tmp_path, tmp_path / "afternoon")
    assert isinstance(roots, tuple)


def test_validated_corporate_action_result_edge_cases():
    from datetime import date

    from run_dca import _validated_corporate_action_result

    assert _validated_corporate_action_result(None, date(2026, 7, 8)) is None
    assert _validated_corporate_action_result((1.0, "not-a-valid-date"), date(2026, 7, 8)) is None
    assert _validated_corporate_action_result((1.0, 12345), date(2026, 7, 8)) is None
    assert _validated_corporate_action_result((-1.0, date(2026, 7, 8)), date(2026, 7, 8)) is None
    assert _validated_corporate_action_result((float("inf"), date(2026, 7, 8)), date(2026, 7, 8)) is None
    assert _validated_corporate_action_result((1.0, date(2026, 7, 9)), date(2026, 7, 8)) is None


def test_execution_targets_edge_cases(tmp_path):
    from run_dca import (
        _execution_targets_by_order_id_from_orders_csv,
        _execution_targets_from_orders_csv,
    )

    missing = tmp_path / "nonexistent.csv"
    assert _execution_targets_from_orders_csv(missing) == {}
    assert _execution_targets_by_order_id_from_orders_csv(missing) == {}

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,order_id,target_price\n"
        ",ord-empty-ticker,10.0\n"
        "AAPL,,15.0\n"
        "MSFT,ord-bad-target,bad_number\n",
        encoding="utf-8",
    )
    targets = _execution_targets_from_orders_csv(csv_path)
    assert targets.get("AAPL") == 15.0
    assert targets.get("MSFT") is None

    targets_by_id = _execution_targets_by_order_id_from_orders_csv(csv_path)
    assert targets_by_id.get("ord-empty-ticker") == 10.0
    assert targets_by_id.get("ord-bad-target") is None


def test_refresh_fills_from_broker_edge_cases(tmp_path):
    from run_dca import _refresh_fills_from_broker

    missing = tmp_path / "nonexistent.csv"

    class _DummyBroker:
        account_number: str = "dummy"

        def get_order_fill(self, oid):
            raise RuntimeError("lookup error")

    broker = cast(Any, _DummyBroker())
    assert _refresh_fills_from_broker(missing, broker, []) == []

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,order_id,status,avg_fill_price,quantity_filled\n"
        "AAPL,,submitted,,\n"
        "NVDA,ord-fail,submitted,,\n",
        encoding="utf-8",
    )
    refreshed = _refresh_fills_from_broker(csv_path, broker, [])
    assert refreshed == []


def test_read_prior_allocation_rows_handles_read_failure(tmp_path):
    from run_dca import _prior_submitted_order_rows

    run_dir = tmp_path / "slot_run"
    run_dir.mkdir()
    unreadable = run_dir / "submitted_orders.csv"
    unreadable.mkdir()  # directory instead of file causes OSError on open()
    assert _prior_submitted_order_rows(run_dir) == []


def test_run_drift_check_with_drift(monkeypatch, tmp_path):
    import scripts.drift_detection as dd
    from run_dca import _run_drift_check
    from scripts.drift_detection import DriftReport, SymbolDrift

    report = DriftReport(
        run_dir=str(tmp_path),
        drift_detected=True,
        affected_symbols=[
            SymbolDrift(
                symbol="AAPL",
                expected_amount_usd=10.0,
                auto_buy_signal=True,
                suspected_cause="missing",
            )
        ],
        run_suspected_cause="broker_error",
        recommended_action="retry",
        signals_present=True,
        orders_present=True,
        submitted_orders_empty=False,
        raw_dir_present=True,
        expected_symbol_count=1,
        submitted_symbol_count=0,
    )
    monkeypatch.setattr(dd, "detect_drift", lambda run_dir: report)
    slack_calls = []
    monkeypatch.setattr(dd, "send_slack_alert", lambda rep, env_var: slack_calls.append((rep, env_var)))

    result = _run_drift_check(tmp_path)
    assert result is not None
    assert (tmp_path / "drift_report.json").exists()
    assert (tmp_path / "drift_report.md").exists()
    assert len(slack_calls) == 1


def test_redacted_strings_scrubs_account_shaped_values_in_persisted_placement_rows():
    """2026-09-07 diagnostic S:ADP-05. raw/orders_after_place.json spreads the
    broker's whole order dict per row; account identifiers echoed by the
    broker were persisted verbatim under cron_tracking/ and synced onward."""
    import json

    from run_dca import _redacted_strings

    rows = [
        {
            "ticker": "AAPL",
            "order_id": "ORD-1",
            "account_number": "123456789",
            "account": "https://api.broker.example/accounts/123456789/",
            "dollar_amount": 2.0,
            "nested": {"note": "account_number=123456789"},
            "tags": ["account_number: 123456789", 7],
        }
    ]
    scrubbed = _redacted_strings(rows)
    assert scrubbed[0]["ticker"] == "AAPL"
    assert scrubbed[0]["dollar_amount"] == 2.0
    assert scrubbed[0]["tags"][1] == 7
    assert "123456789" not in json.dumps(scrubbed)
    assert "123456789" in json.dumps(rows)  # the input is not mutated


def test_redacted_strings_scrubs_account_shaped_dictionary_keys_too():
    """Copilot review of PR #1394: only VALUES were scrubbed, so a broker
    payload whose key carries the identifier ({"account_number=<n>": ...})
    still leaked it into raw/orders_after_place.json."""
    from run_dca import _redacted_strings

    synthetic_account = "123456789"  # synthetic, allow-listed account-shaped value
    scrubbed = _redacted_strings({f"account_number={synthetic_account}": {f"nested {synthetic_account}": "v"}})
    assert synthetic_account not in repr(scrubbed)
    assert len(scrubbed) == 1 and len(next(iter(scrubbed.values()))) == 1
