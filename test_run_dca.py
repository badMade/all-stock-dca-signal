"""Tests for the orchestrator + CLI (run_dca)."""

from __future__ import annotations

from datetime import datetime, time

from dca_config import load_strategy_config
from near_close_dca import EASTERN, DryRunBroker, SourceName, StrategyConfig, StrategyState, build_broker
from run_dca import DemoDataProvider, _fills_from_orders_csv, main, run_pipeline


def _now(day=(2026, 7, 6)):
    return datetime.combine(datetime(*day).date(), time(15, 45), tzinfo=EASTERN)


def _demo_config():
    # Approve the demo trading day regardless of the bundled window.
    return load_strategy_config(approved_test_dates=frozenset({_now().date()}))


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


def test_run_pipeline_scheduled_run_auto_loads_prior_allocations(tmp_path):
    import json

    # Seed a manual same-day allocation under the cron task root's manual_runs/,
    # mirroring the real production layout from config/scheduled_tasks.json:
    # <task_root>/scheduled_runs/YYYYMMDD_1520ET/ (artifacts_dir) as a sibling of
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
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
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
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
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
    # Codex follow-up review: fixing the above (honoring a stream's configured
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
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
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
    # descendant pruning"): a residual case after the fix above. When a
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
    scheduled_dir = afternoon_root / "scheduled_runs" / "20260706_1520ET"
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
    artifacts_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    assert _default_prior_allocation_state_root(artifacts_dir) == task_root


def test_default_prior_allocation_state_root_falls_back_to_parent():
    from pathlib import Path

    from run_dca import _default_prior_allocation_state_root

    # No scheduled_runs/ segment in the path (e.g. a flat test/demo layout) ->
    # fall back to the immediate parent.
    artifacts_dir = Path("/tmp/some_root/scheduled")
    assert _default_prior_allocation_state_root(artifacts_dir) == Path("/tmp/some_root")


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

    fills = _refresh_fills_from_broker(csv_path, _Broker(), [])
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["fill_price"] == 100.0


def test_fills_from_missing_csv_is_empty(tmp_path):
    assert _fills_from_orders_csv(tmp_path / "nope.csv") == []


def test_cli_demo_exit_code(tmp_path):
    rc = main(["--demo", "--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "run_state.json").exists()


def test_cli_refuses_without_demo(tmp_path):
    rc = main(["--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 2


def test_cli_refuses_when_dry_run_false(tmp_path, monkeypatch):
    import run_dca

    monkeypatch.setattr(run_dca, "load_strategy_config", lambda *a, **k: StrategyConfig(dry_run=False))
    rc = run_dca.main(["--demo", "--artifacts-dir", str(tmp_path)])
    assert rc == 2


def test_run_pipeline_persists_raw_inputs_before_compute(tmp_path):
    import json

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
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


def test_run_pipeline_raw_persistence_failure_writes_error_state(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
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


def test_run_pipeline_raw_persistence_failure_clears_stale_compact_artifacts(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    scheduled_dir.mkdir(parents=True)
    # Seed stale derived-report artifacts from a prior attempt.
    # orders.csv and buy_candidates.csv are intentionally NOT seeded here; they are
    # preserved across retry failures so a successful prior run's fill records survive
    # (see test_run_pipeline_raw_persistence_failure_preserves_prior_order_artifacts).
    cleared_files = (
        "signals.csv",
        "latest_run_summary.json", "slippage_and_coverage_report.md",
        "slippage_ranked.csv", "missing_fills_report.csv",
        "notion_latest_run_compact.md", "notion_latest_run_payload.json",
    )
    for name in cleared_files:
        (scheduled_dir / name).write_text("stale", encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    for name in cleared_files:
        assert not (scheduled_dir / name).exists(), f"{name} was not removed"
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["broker"] == "robinhood"


def test_run_pipeline_raw_persistence_failure_preserves_prior_order_artifacts(tmp_path):
    """A raw-persistence failure must not delete orders.csv or buy_candidates.csv.

    If a prior successful run in the same directory already placed orders,
    a subsequent retry that fails at the raw-persistence step must preserve
    those fill records so reporting and cap accounting are not lost.
    """
    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    # The raw persistence error fires before any broker call, so a DryRunBroker is fine here.
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
        required_raw_filenames=frozenset({"quotes_batch1.json", "historicals_batch1.json"}),
    )
    assert "raw_persistence_error" in result
    assert "historicals_batch1.json" in result["raw_persistence_error"]
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    broker = DryRunBroker(account=config.target_account_number)
    rh_only_inputs = {
        "quotes_batch1.json": {"results": [{"quote": {"symbol": "AAPL"}}]},
        "historicals_batch1.json": {"results": [{"symbol": "AAPL", "interval": 5, "items": []}]},
    }
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    broker = DryRunBroker(account=config.target_account_number)
    finance_only_inputs = {
        "finance_bars_batch1.json": {"results": []},
        "finance_close_batch1.json": {"results": []},
    }
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
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
    assert robinhood.get_bid_ask("AAPL", _now()) == (50.0, 51.0)

    _finance_no_live, robinhood_no_live = _build_frozen_providers_from_raw(
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD), raw_dir,
    )
    assert robinhood_no_live.get_bid_ask("AAPL", _now()) is None


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


def test_run_pipeline_retry_preserves_orders_after_place(tmp_path):
    """A successful retry must carry over orders_after_place.json from the prior raw/.

    Simulates a prior raw/ containing orders_after_place.json (written by a first
    successful attempt that placed an order). The second run's staging swap must
    preserve it so a broker-deduplicated retry that places no new order does not
    lose the durable fill snapshot from the original placement attempt.
    """
    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
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
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
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
        broker, artifacts_dir=scheduled_dir,
        raw_connector_inputs={
            "quotes_batch1.json": {"output_path": str(quotes_file)},
            "finance_bars_batch1.json": {"output_path": str(finance_bars_file)},
            "finance_close_batch1.json": {"output_path": str(finance_close_file)},
        },
    )
    oap = scheduled_dir / "raw" / "orders_after_place.json"
    assert oap.exists(), "orders_after_place.json missing after partial retry"
    order_ids = {o["order_id"] for o in _json.loads(oap.read_text(encoding="utf-8"))["orders"]}
    assert "retry-Y2" in order_ids, "New MSFT order missing from merged file"
    assert "prior-X1" in order_ids, "Prior AAPL fill lost during partial retry"
