"""Hermetic CLI tests for the plan-first courier cycle."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from dca_market_sources import ET
from dca_rec_models import ExpectedPrice, ExpectedPriceSet, Recommendation, RecommendationSet
from dca_rec_store import RecommendationStore
from scripts import snapshot_cycle as cycle

NOW = datetime(2026, 10, 13, 12, 25, tzinfo=ET)
BUDGET = {"budget_tier": "Normal", "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0}


def _pair(slot="1230", as_of=NOW):
    day = as_of.date()
    rec = RecommendationSet(day, as_of, slot, "robinhood_agent_fetch", "v1", "hash",
                            (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    expected = ExpectedPriceSet(day, as_of, slot, "run_state_artifacts",
                                (ExpectedPrice("AAPL", 100, 101, 99, 1.5, 1, 3),), BUDGET)
    return rec, expected


def _mark(run: Path, planned_at: datetime) -> Path:
    """Write the fetch-plan marker a real `fetch-plan` leaves in the run directory."""
    run.mkdir(parents=True, exist_ok=True)
    (run / cycle.RUN_MARKER).write_text(json.dumps({"planned_at": planned_at.isoformat()}), encoding="utf-8")
    return run


def test_now_parsing_and_compact_json(capsys):
    assert cycle._now(None).tzinfo == ET
    assert cycle._now("2026-10-13T12:25:00Z").hour == 8
    assert cycle._now("2026-10-13T12:25").tzinfo == ET
    cycle._emit({"status": "OK"})
    assert capsys.readouterr().out == '{"status":"OK"}\n'


def test_fetch_plan_idle_and_account_error(monkeypatch, capsys):
    monkeypatch.setattr(cycle, "_session_status", lambda _: "not_a_market_day")
    assert cycle._fetch_plan(NOW, Path("config")) == 3
    assert json.loads(capsys.readouterr().out) == {"status": "IDLE", "reason": "not_a_market_day"}
    monkeypatch.setattr(cycle, "_session_status", lambda _: None)
    monkeypatch.setattr(cycle, "_account_number", lambda: (_ for _ in ()).throw(ValueError("secret")))
    assert cycle._fetch_plan(NOW, Path("config")) == 2
    assert "secret" not in capsys.readouterr().out


def test_fetch_plan_includes_calls_only_on_open_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cycle, "_session_status", lambda _: None)
    monkeypatch.setattr(cycle, "_account_number", lambda: "test-account")
    monkeypatch.setattr(cycle, "_universe", lambda _: ["SPY"])
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: tmp_path))
    assert cycle._fetch_plan(NOW, tmp_path) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "FETCH"
    assert any(call["args"].get("account_number") == "test-account" for call in output["calls"])


def test_session_checks_reuse_gate0_and_regular_hours(monkeypatch):
    monkeypatch.setattr(cycle.dca_runner, "market_day_status", lambda _: SimpleNamespace(is_closed=True, is_early_close=False))
    assert cycle._session_status(NOW) == "not_a_market_day"
    monkeypatch.setattr(cycle.dca_runner, "market_day_status", lambda _: SimpleNamespace(is_closed=False, is_early_close=False))
    monkeypatch.setattr(cycle.dca_runner, "gate_trading_window", lambda _: False)
    assert cycle._session_status(NOW) == "outside_approved_trading_window"
    monkeypatch.setattr(cycle.dca_runner, "gate_trading_window", lambda _: True)
    assert cycle._session_status(datetime(2026, 10, 13, 8, 0, tzinfo=ET)) == "outside_regular_session"
    assert cycle._session_status(NOW) is None
    monkeypatch.setattr(cycle.dca_runner, "market_day_status", lambda _: SimpleNamespace(is_closed=False, is_early_close=True))
    assert cycle._session_status(datetime(2026, 10, 13, 14, 0, tzinfo=ET)) == "outside_regular_session"


def test_path_must_resolve_under_incoming(tmp_path):
    incoming = tmp_path / "incoming"
    valid = incoming / "run"
    valid.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    assert cycle._inside_incoming(valid, incoming)
    assert not cycle._inside_incoming(outside, incoming)
    assert not cycle._inside_incoming(incoming, incoming)
    assert not cycle._inside_incoming(tmp_path / "missing", incoming)


def test_ingest_refuses_traversal_before_acquiring_lease(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: tmp_path / "today"))
    assert cycle._ingest(tmp_path, NOW, Path("config")) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "run_dir_outside_today_incoming"


def test_busy_lease_and_release(monkeypatch, tmp_path, capsys):
    day_dir = tmp_path / "day"
    run = _mark(day_dir / "raw/incoming/run", NOW)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: tmp_path / "day"))
    class Lease:
        def __init__(self, *_): pass
        def acquire(self, *_): return SimpleNamespace(acquired=False)
    monkeypatch.setattr(cycle, "SchedulerLease", Lease)
    assert cycle._ingest(run, NOW, tmp_path) == 3
    assert json.loads(capsys.readouterr().out) == {"status": "BUSY"}


def test_success_ingests_two_slots_publishes_and_compares(monkeypatch, tmp_path, capsys):
    from test_dca_market_history import DAY as RAW_DAY
    from test_dca_market_history import NOW as RAW_NOW
    from test_dca_market_history import _daily
    from test_dca_market_history import _run as raw_run

    real_store = RecommendationStore(tmp_path / "recommendations")
    raw_store = cycle.MarketHistoryStore(real_store.day_dir(RAW_DAY))
    run = _mark(raw_run(raw_store, "e2e", daily=_daily()), RAW_NOW)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: real_store)
    monkeypatch.setattr(cycle.dca_paths, "cron_tracking_root", lambda: tmp_path / "cron")
    monkeypatch.setattr(cycle, "_account_number", lambda: "test-account")
    monkeypatch.setattr(cycle, "_universe", lambda _: ["SPY"])
    settings = SimpleNamespace(lookahead=timedelta(minutes=15), publish={})
    monkeypatch.setattr(cycle, "load_settings", lambda _: settings)
    class Lease:
        def __init__(self, *_): pass
        def acquire(self, *_): return SimpleNamespace(acquired=True)
        def release(self, *_): return True
    monkeypatch.setattr(cycle, "SchedulerLease", Lease)
    class Engine:
        def __init__(self, **_): pass
        def compute(self, _snapshot, _broker, _now, *, slot):
            rec, expected = _pair(slot, RAW_NOW)
            return SimpleNamespace(status="OK", recommendations=rec, expected=expected, reasons=())
    monkeypatch.setattr(cycle, "RecommendationEngine", Engine)
    monkeypatch.setattr(cycle, "slots_for", lambda *_: ["1430", "1530"])
    monkeypatch.setattr(cycle, "publishers_from_settings", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(cycle, "publish_all", lambda *_: [SimpleNamespace(channel="test", ok=False, detail="offline")])
    monkeypatch.setattr(cycle, "_root", lambda: tmp_path)
    class Comparator:
        def __init__(self, *_): pass
        def compare(self, *_): return []
        def write_report(self, *_args, **_kwargs): return tmp_path / "report"
    monkeypatch.setattr(cycle, "FillComparator", Comparator)
    assert cycle._ingest(run, RAW_NOW, tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "OK"
    assert result["publish"] == [{"channel": "test", "ok": False, "detail": "offline"}]
    for slot in ("1430", "1530"):
        selection = real_store.select_for(slot, RAW_NOW, timedelta(minutes=15))
        assert selection.status == "OK"
        agent_doc = selection.recommendation.to_json()
        assert "price" not in json.dumps(agent_doc).lower()
        assert real_store.expected_for(selection.recommendation).budget == BUDGET
    from scripts import plan_first_slot
    monkeypatch.setattr(plan_first_slot, "_clock_reason", lambda *_: None)  # pipeline test, not the clock
    assert plan_first_slot.main(["begin", "--slot", "1530", "--now", RAW_NOW.isoformat(), "--root", str(real_store.root)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "OK"


def test_ingest_partial_and_error_release_lease(monkeypatch, tmp_path, capsys):
    store = RecommendationStore(tmp_path / "rec")
    run = _mark(store.day_dir(NOW.date()) / "raw/incoming/run", NOW)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: store)
    monkeypatch.setattr(cycle.dca_paths, "cron_tracking_root", lambda: tmp_path)
    monkeypatch.setattr(cycle, "_account_number", lambda: "test-account")
    monkeypatch.setattr(cycle, "_universe", lambda _: ["AAPL"])
    monkeypatch.setattr(cycle, "load_settings", lambda _: SimpleNamespace(lookahead=timedelta(0), publish={}))
    state = {"released": False}
    class Lease:
        def __init__(self, *_): pass
        def acquire(self, *_): return SimpleNamespace(acquired=True)
        def release(self, *_): state["released"] = True
    monkeypatch.setattr(cycle, "SchedulerLease", Lease)
    class History:
        def __init__(self, *args): self.raw_dir = Path(args[0]) / "raw"
        def ingest_run(self, *_args, **_kwargs): return object(), object()
    monkeypatch.setattr(cycle, "MarketHistoryStore", History)
    monkeypatch.setattr(cycle, "slots_for", lambda *_: ["1230"])
    class AbortEngine:
        def __init__(self, **_): pass
        def compute(self, *_args, **_kwargs): return SimpleNamespace(status="ABORT", recommendations=None, expected=None, reasons=("blocked",))
    monkeypatch.setattr(cycle, "RecommendationEngine", AbortEngine)
    monkeypatch.setattr(cycle, "publishers_from_settings", lambda *_args, **_kwargs: [])
    class Comparator:
        def __init__(self, *_): pass
        def compare(self, *_): return []
        def write_report(self, *_args, **_kwargs): pass
    monkeypatch.setattr(cycle, "FillComparator", Comparator)
    assert cycle._ingest(run, NOW, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PARTIAL"
    assert state["released"]
    monkeypatch.setattr(cycle, "MarketHistoryStore", lambda *_: (_ for _ in ()).throw(ValueError("private")))
    assert cycle._ingest(run, NOW, tmp_path) == 2
    output = capsys.readouterr().out
    assert "private" not in output
    assert state["released"]


def test_status_reports_fresh_and_stale(tmp_path, monkeypatch, capsys):
    store = RecommendationStore(tmp_path)
    fresh, expected = _pair("0935", NOW - timedelta(minutes=5))
    stale, stale_expected = _pair("1030", NOW - timedelta(minutes=20))
    store.write(fresh, expected)
    store.write(stale, stale_expected)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: store)
    monkeypatch.setattr(cycle, "load_settings", lambda _: SimpleNamespace(max_age=timedelta(minutes=15)))
    assert cycle._status(NOW, tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["slots"]["0935"]["status"] == "FRESH"
    assert result["slots"]["1030"]["status"] == "STALE"
    assert result["slots"]["1030"]["reason"] == "stale"


def test_main_routes_subcommands_and_sanitizes_config_errors(monkeypatch, capsys):
    monkeypatch.setattr(cycle.dca_config, "get_config_dir", lambda: Path("config"))
    monkeypatch.setattr(cycle, "_fetch_plan", lambda now, config: 7)
    assert cycle.main(["fetch-plan", "--now", "2026-10-13T12:25:00-04:00"]) == 7
    monkeypatch.setattr(cycle, "_ingest", lambda run, now, config: 8)
    assert cycle.main(["ingest", "--run-dir", "/tmp/run", "--now", "2026-10-13T12:25:00-04:00"]) == 8
    monkeypatch.setattr(cycle, "load_settings", lambda *_: (_ for _ in ()).throw(ValueError("private")))
    assert cycle.main(["status", "--now", "2026-10-13T12:25:00-04:00"]) == 2
    assert "private" not in capsys.readouterr().out


def test_ingest_error_from_history_emits_sanitized_reason(monkeypatch, tmp_path, capsys):
    day_dir = tmp_path / "day"
    run = _mark(day_dir / "raw/incoming/run", NOW)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: tmp_path / "day"))
    monkeypatch.setattr(cycle.dca_paths, "cron_tracking_root", lambda: tmp_path)
    class Lease:
        def __init__(self, *_): pass
        def acquire(self, *_): return SimpleNamespace(acquired=True)
        def release(self, *_): return True
    monkeypatch.setattr(cycle, "SchedulerLease", Lease)
    monkeypatch.setattr(cycle, "_account_number", lambda: "test-account")
    monkeypatch.setattr(cycle, "_universe", lambda _: ["AAPL"])
    monkeypatch.setattr(cycle, "load_settings", lambda _: SimpleNamespace(lookahead=timedelta(0), publish={}))
    class History:
        def __init__(self, *args): self.raw_dir = Path(args[0]) / "raw"
        def ingest_run(self, *_args, **_kwargs): raise cycle.SourceUnavailable("private payload")
    monkeypatch.setattr(cycle, "MarketHistoryStore", History)
    assert cycle._ingest(run, NOW, tmp_path) == 2
    assert "private payload" not in capsys.readouterr().out
    assert not (day_dir / "raw/day_bars.json").exists()


# --- PR #1667 review: bind each run to its fetch-plan time ---------------------------------------


import pytest  # noqa: E402


@pytest.mark.parametrize("marker,reason", [
    (None, "missing"),
    ({"planned_at": (NOW - timedelta(minutes=11)).isoformat()}, "too old"),
    ({"planned_at": (NOW + timedelta(minutes=2)).isoformat()}, "future"),
    ({"planned_at": (NOW - timedelta(days=1)).isoformat()}, "another day"),
    ({"planned_at": "2026-10-13T12:20:00"}, "naive"),
    ({"other": 1}, "malformed"),
])
def test_ingest_refuses_runs_that_are_not_fresh_fetch_plan_runs(monkeypatch, tmp_path, capsys, marker, reason):
    day_dir = tmp_path / "day"
    run = day_dir / "raw/incoming/run"
    run.mkdir(parents=True)
    if marker is not None:
        (run / cycle.RUN_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: day_dir))
    monkeypatch.setattr(cycle, "SchedulerLease", lambda *_: pytest.fail("lease taken for a stale run"))
    assert cycle._ingest(run, NOW, tmp_path) == 2, reason
    assert json.loads(capsys.readouterr().out)["reason"] == "run_not_fresh_or_not_from_fetch_plan"


def test_ingest_stamps_everything_with_the_fetch_time_not_the_ingest_clock(monkeypatch, tmp_path, capsys):
    day_dir = tmp_path / "day"
    planned = NOW - timedelta(minutes=3)
    run = _mark(day_dir / "raw/incoming/run", planned)
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: day_dir))
    monkeypatch.setattr(cycle.dca_paths, "cron_tracking_root", lambda: tmp_path)
    seen = {}

    class Lease:
        def __init__(self, *_): pass
        def acquire(self, *_): return SimpleNamespace(acquired=True)
        def release(self, *_): return True

    class History:
        def __init__(self, *_): self.raw_dir = tmp_path / "staged"
        def ingest_run(self, _run, **kwargs):
            seen["now"] = kwargs["now"]
            raise cycle.SourceUnavailable("stop here")

    monkeypatch.setattr(cycle, "SchedulerLease", Lease)
    monkeypatch.setattr(cycle, "MarketHistoryStore", History)
    monkeypatch.setattr(cycle, "_account_number", lambda: "acct")
    monkeypatch.setattr(cycle, "load_settings", lambda _: SimpleNamespace(lookahead=timedelta(0), publish={}))
    monkeypatch.setattr(cycle, "_universe", lambda _: ["AAPL"])
    assert cycle._ingest(run, NOW, tmp_path) == 2
    assert seen["now"] == planned


def test_fetch_plan_writes_the_run_marker(monkeypatch, tmp_path, capsys):
    run_dir = tmp_path / "day/raw/incoming/12h25m00s"
    monkeypatch.setattr(cycle, "_session_status", lambda _now: None)
    monkeypatch.setattr(cycle, "_account_number", lambda: "acct")
    monkeypatch.setattr(cycle, "_universe", lambda _: ["AAPL"])
    monkeypatch.setattr(cycle, "RecommendationStore", lambda: SimpleNamespace(day_dir=lambda _: tmp_path / "day"))

    class History:
        def __init__(self, *_): pass
        def fetch_plan(self, **_): return {"run_dir": str(run_dir), "calls": []}

    monkeypatch.setattr(cycle, "MarketHistoryStore", History)
    assert cycle._fetch_plan(NOW, tmp_path) == 0
    marker = json.loads((run_dir / cycle.RUN_MARKER).read_text(encoding="utf-8"))
    assert datetime.fromisoformat(marker["planned_at"]) == NOW
