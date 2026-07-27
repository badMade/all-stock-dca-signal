"""Tests for dca_alerts: failure alerting, heartbeats, and the guarded_run wrapper."""

import io
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dca_alerts
from dca_alerts import alert_failure, guarded_run, heartbeat_url, send_heartbeat


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured_requests(monkeypatch):
    """Capture urllib.request.urlopen calls made by dca_alerts."""
    calls = []

    def fake_urlopen(url_or_request, timeout=None):
        calls.append(url_or_request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _clear_alert_env(monkeypatch):
    for name in (
        "DCA_HEARTBEAT_URL",
        "DCA_HEARTBEAT_URL_MORNING",
        "DCA_HEARTBEAT_URL_AFTERNOON",
        "DCA_ALERT_SLACK_WEBHOOK",
        "DCA_DRIFT_SLACK_WEBHOOK",
    ):
        monkeypatch.delenv(name, raising=False)


# -- heartbeats -------------------------------------------------------------------


def test_send_heartbeat_noop_without_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_prefers_stream_specific_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/generic")
    monkeypatch.setenv("DCA_HEARTBEAT_URL_MORNING", "https://hc.example/morning")
    assert heartbeat_url("morning") == "https://hc.example/morning"
    assert send_heartbeat("morning") is True
    assert captured_requests == ["https://hc.example/morning"]


def test_send_heartbeat_failure_ping_appends_fail(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")
    assert send_heartbeat("afternoon", ok=False) is True
    assert captured_requests == ["https://hc.example/check/fail"]


def test_send_heartbeat_rejects_non_http_scheme(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "ftp://hc.example/check")
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_never_raises_on_network_error(monkeypatch):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")

    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert send_heartbeat("morning") is False


# -- failure alerts ---------------------------------------------------------------


def test_alert_failure_noop_without_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    assert alert_failure("morning", RuntimeError("boom")) is False
    assert captured_requests == []


def test_alert_failure_posts_stream_and_exception(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/alert")
    assert alert_failure("afternoon", RuntimeError("boom"), context="ctx-info") is True
    (request,) = captured_requests
    assert request.full_url == "https://hooks.example/alert"
    text = json.loads(request.data.decode("utf-8"))["text"]
    assert "afternoon" in text
    assert "RuntimeError" in text
    assert "boom" in text
    assert "ctx-info" in text


def test_alert_failure_falls_back_to_drift_webhook(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_DRIFT_SLACK_WEBHOOK", "https://hooks.example/drift")
    assert alert_failure("morning", ValueError("bad")) is True
    assert captured_requests[0].full_url == "https://hooks.example/drift"


def test_alert_failure_never_raises_on_network_error(monkeypatch):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/alert")

    def boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert alert_failure("morning", RuntimeError("boom")) is False


# -- guarded_run ------------------------------------------------------------------


def test_guarded_run_success_pings_ok(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    assert guarded_run("morning", lambda: 42) == 42
    assert pings == [("morning", True)]
    assert alerts == []


def test_guarded_run_failure_alerts_pings_fail_and_reraises(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append((stream, exc)))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pings == [("midday", False)]
    assert len(alerts) == 1 and isinstance(alerts[0][1], RuntimeError)


def test_guarded_run_disabled_never_pings(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    with pytest.raises(RuntimeError):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("boom")), enabled=False)
    assert pings == []
    assert alerts == []


def test_guarded_run_result_ok_false_pings_fail_without_alert(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    result = guarded_run(
        "afternoon",
        lambda: {"raw_persistence_error": "disk full"},
        result_ok=lambda r: not r.get("raw_persistence_error"),
    )
    assert result == {"raw_persistence_error": "disk full"}
    assert pings == [("afternoon", False)]
    # Structured-failure paths emit their own targeted alerts; no duplicate here.
    assert alerts == []


def test_guarded_run_result_ok_predicate_error_fails_closed(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))

    result = guarded_run(
        "morning",
        lambda: {"fine": True},
        result_ok=lambda r: r["missing-key"],
    )
    assert result == {"fine": True}
    assert pings == [("morning", False)]


def test_guarded_run_alerting_bug_does_not_mask_failure(monkeypatch):
    def broken_alert(*a, **kw):
        raise ValueError("alert plumbing broken")

    monkeypatch.setattr(dca_alerts, "alert_failure", broken_alert)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", broken_alert)

    with pytest.raises(RuntimeError, match="original"):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("original")))


def test_guarded_run_failure_writes_failed_start_marker(monkeypatch):
    markers = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda stream, exc, **kw: markers.append((stream, exc)))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert len(markers) == 1
    assert markers[0][0] == "midday"
    assert isinstance(markers[0][1], RuntimeError)


def test_guarded_run_marker_write_bug_does_not_mask_failure(monkeypatch):
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda *a, **kw: (_ for _ in ()).throw(ValueError("marker plumbing broken")))

    with pytest.raises(RuntimeError, match="original"):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("original")))


def test_guarded_run_alert_failure_raising_does_not_block_heartbeat_or_marker(monkeypatch):
    # alert_failure raising (e.g. malformed webhook URL parsing outside its
    # own try) must not prevent the heartbeat or the marker -- the marker
    # especially is the only durable local evidence this failure happened --
    # codex review discussion_r3658373974.
    pings = []
    markers = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad url")))
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda stream, exc, **kw: markers.append(stream))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pings == [("midday", False)]
    assert markers == ["midday"]


def test_guarded_run_passes_attempt_log_root_to_marker(monkeypatch, tmp_path):
    roots = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, **kw: roots.append(root),
    )

    with pytest.raises(RuntimeError):
        guarded_run(
            "morning",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            attempt_log_root=str(tmp_path),
        )
    assert roots == [str(tmp_path)]


# -- attempt_log_root_sibling_to ---------------------------------------------------


def test_attempt_log_root_sibling_to_none_when_unset(monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    assert dca_alerts.attempt_log_root_sibling_to(None) is None
    assert dca_alerts.attempt_log_root_sibling_to("") is None


def test_attempt_log_root_sibling_to_derives_sibling_directory(monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    result = dca_alerts.attempt_log_root_sibling_to("/custom/base/model_usage")
    assert result == "/custom/base/attempt_log"


def test_attempt_log_root_sibling_to_defers_to_explicit_env_override(monkeypatch):
    # A deployment that intentionally sets DCA_ATTEMPT_LOG_ROOT to its own
    # active artifact tree must have that win over a derived sibling path --
    # codex review discussion_r3659124650.
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", "/configured/attempt_log")
    assert dca_alerts.attempt_log_root_sibling_to("/custom/base/model_usage") is None
    assert dca_alerts.attempt_log_root_sibling_to(None) is None


# -- write_failed_start_marker ------------------------------------------------------


def test_write_failed_start_marker_persists_readable_json(tmp_path):
    triggered = datetime(2026, 7, 27, 20, 30, 0, tzinfo=timezone.utc)
    path = dca_alerts.write_failed_start_marker(
        "market_open", RuntimeError("connector timed out"), triggered_at_utc=triggered, root=str(tmp_path)
    )
    assert path == tmp_path / "20260727" / "market_open.json"
    marker = json.loads(path.read_text(encoding="utf-8"))
    assert marker["stream"] == "market_open"
    assert marker["error_class"] == "RuntimeError"
    assert marker["error_message"] == "connector timed out"
    assert marker["triggered_at_utc"] == "2026-07-27T20:30:00Z"
    # Default phase is the genuine failed start this marker was built for.
    assert marker["failure_phase"] == "before_run_artifact"


def test_write_failed_start_marker_records_the_late_failure_phase(tmp_path):
    # A run can persist a valid run_state.json and only then raise; the marker
    # must say so, or the report invents an earlier failed start plus a
    # successful retry -- codex review discussion_r3659298031.
    path = dca_alerts.write_failed_start_marker(
        "morning",
        RuntimeError("report generation failed after the run was written"),
        triggered_at_utc=datetime(2026, 7, 27, 14, 40, 0, tzinfo=timezone.utc),
        root=str(tmp_path),
        run_artifact_written=True,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["failure_phase"] == "after_run_artifact"


def test_guarded_run_probes_whether_the_run_artifact_was_written(monkeypatch):
    phases = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, run_artifact_written=False: phases.append(run_artifact_written),
    )

    # A probe reporting the artifact already exists records the late phase; a
    # probe reporting otherwise -- and no probe at all -- record a failed start.
    for probe, expected in ((lambda: True, True), (lambda: False, False), (None, False)):
        phases.clear()
        with pytest.raises(RuntimeError, match="boom"):
            guarded_run(
                "morning",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                run_artifact_written=probe,
            )
        assert phases == [expected], probe


def test_guarded_run_artifact_probe_failure_falls_back_to_failed_start(monkeypatch):
    # A broken probe must never lose the failure record; it fails closed to a
    # plain failed start, the pre-existing behavior.
    phases = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, run_artifact_written=False: phases.append(run_artifact_written),
    )

    def _broken_probe():
        raise OSError("cannot stat the run directory")

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run(
            "morning",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            run_artifact_written=_broken_probe,
        )
    assert phases == [False]


def test_artifact_freshness_probe_true_for_a_fresh_write(tmp_path):
    # No file exists at snapshot time; one appears afterward -- that must be
    # THIS invocation's own write.
    path = tmp_path / "run_state.json"
    probe = dca_alerts.artifact_freshness_probe(path)
    assert probe() is False
    path.write_text("{}", encoding="utf-8")
    assert probe() is True


def test_artifact_freshness_probe_false_for_a_stale_untouched_file(tmp_path):
    # A retry can reuse a directory that already has an EARLIER invocation's
    # run_state.json. If this invocation never touches it, the probe must NOT
    # credit that pre-existing file as its own work -- codex review
    # discussion_r3660858013.
    import os

    path = tmp_path / "run_state.json"
    path.write_text('{"run_id": "earlier_attempt"}', encoding="utf-8")
    old = datetime(2026, 7, 27, 12, 0, 0).timestamp()
    os.utime(path, (old, old))

    probe = dca_alerts.artifact_freshness_probe(path)
    # Untouched since the snapshot -- not this invocation's write.
    assert probe() is False


def test_artifact_freshness_probe_true_when_a_stale_file_is_overwritten(tmp_path):
    # The mirror: if THIS invocation genuinely rewrites the file after the
    # snapshot, it must be credited even though a stale version existed
    # before.
    import os

    path = tmp_path / "run_state.json"
    path.write_text('{"run_id": "earlier_attempt"}', encoding="utf-8")
    old = datetime(2026, 7, 27, 12, 0, 0).timestamp()
    os.utime(path, (old, old))

    probe = dca_alerts.artifact_freshness_probe(path)
    path.write_text('{"run_id": "this_invocation"}', encoding="utf-8")
    new = datetime(2026, 7, 27, 12, 5, 0).timestamp()
    os.utime(path, (new, new))
    assert probe() is True


def test_write_failed_start_marker_uses_env_root(monkeypatch, tmp_path):
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", str(tmp_path))
    triggered = datetime(2026, 7, 27, 14, 0, 0, tzinfo=timezone.utc)
    path = dca_alerts.write_failed_start_marker("watchlist", ValueError("x"), triggered_at_utc=triggered)
    assert path == tmp_path / "20260727" / "watchlist.json"
    assert path.is_file()


def test_repeated_failures_keep_every_attempt_in_the_history(tmp_path):
    # <stream>.json is a single fixed path, so a second same-day failure
    # overwrites the first and the report could only ever see the last
    # attempt. Every attempt is additionally appended to the append-only
    # <stream>.attempts.jsonl -- codex review discussion_r3659140585.
    for index, message in enumerate(("first failure", "second failure")):
        dca_alerts.write_failed_start_marker(
            "morning",
            RuntimeError(message),
            triggered_at_utc=datetime(2026, 7, 27, 14, index, 0, tzinfo=timezone.utc),
            root=str(tmp_path),
        )
    history = tmp_path / "20260727" / f"morning{dca_alerts.ATTEMPT_LOG_HISTORY_SUFFIX}"
    entries = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [e["error_message"] for e in entries] == ["first failure", "second failure"]
    # The single marker still holds the LATEST attempt, so existing readers
    # keep working unchanged.
    latest = json.loads((tmp_path / "20260727" / "morning.json").read_text(encoding="utf-8"))
    assert latest["error_message"] == "second failure"


def test_attempt_history_append_failure_does_not_lose_the_marker(monkeypatch, tmp_path):
    # The history append is best-effort like every other write here: if it
    # fails, the single marker must still be written rather than the whole
    # attempt being lost.
    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name.endswith(dca_alerts.ATTEMPT_LOG_HISTORY_SUFFIX):
            raise OSError("history unwritable")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(dca_alerts.Path, "open", failing_open)
    path = dca_alerts.write_failed_start_marker(
        "midday",
        RuntimeError("boom"),
        triggered_at_utc=datetime(2026, 7, 27, 16, 30, 0, tzinfo=timezone.utc),
        root=str(tmp_path),
    )
    assert path is not None and path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["error_message"] == "boom"


def test_write_failed_start_marker_never_raises_on_unwritable_root(monkeypatch):
    def broken_mkdir(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dca_alerts.Path, "mkdir", broken_mkdir)
    result = dca_alerts.write_failed_start_marker("morning", RuntimeError("boom"), root="/no/such/root")
    assert result is None


# -- entrypoint wiring ------------------------------------------------------------


def test_run_pipeline_scheduled_live_failure_alerts_and_reraises(monkeypatch):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append(stream))

    def failing_impl(*args, **kwargs):
        raise RuntimeError("impl exploded")

    monkeypatch.setattr(run_dca, "_run_pipeline_impl", failing_impl)

    with pytest.raises(RuntimeError, match="impl exploded"):
        run_dca.run_pipeline(
            datetime(2026, 7, 23, 15, 20),
            StrategyConfig(dry_run=False),
            None,
            None,
            None,
            None,
            artifacts_dir="/tmp/unused",
            scheduled=True,
        )
    assert pings == [("afternoon", False)]
    assert alerts == ["afternoon"]


def test_run_pipeline_dry_run_never_pings(monkeypatch, tmp_path):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))

    monkeypatch.setattr(run_dca, "_run_pipeline_impl", lambda *a, **kw: {"ok": True})

    result = run_dca.run_pipeline(
        datetime(2026, 7, 23, 15, 20),
        StrategyConfig(dry_run=True),
        None,
        None,
        None,
        None,
        artifacts_dir=str(tmp_path),
        scheduled=True,
    )
    assert result == {"ok": True}
    assert pings == []


def test_run_pipeline_raw_persistence_failure_result_pings_fail(monkeypatch, tmp_path):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    monkeypatch.setattr(
        run_dca,
        "_run_pipeline_impl",
        lambda *a, **kw: {"raw_persistence_error": "raw/ not writable", "blocked": False},
    )

    result = run_dca.run_pipeline(
        datetime(2026, 7, 23, 15, 20),
        StrategyConfig(dry_run=False),
        None,
        None,
        None,
        None,
        artifacts_dir=str(tmp_path),
        scheduled=True,
    )
    assert result["raw_persistence_error"] == "raw/ not writable"
    assert pings == [("afternoon", False)]
    assert alerts == []


def test_morning_run_scheduled_live_failure_alerts_and_reraises(monkeypatch):
    from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append(stream))

    executor = MorningDCAExecutor(MorningConfig(), mode=MORNING_LIVE_MODE)
    assert executor.trades, "live-mode executor should be trading-enabled for this test"

    def failing_impl(*args, **kwargs):
        raise RuntimeError("morning exploded")

    monkeypatch.setattr(executor, "_run_impl", failing_impl)

    with pytest.raises(RuntimeError, match="morning exploded"):
        executor.run(datetime(2026, 7, 23, 10, 30), data=None)
    assert pings == [("morning", False)]
    assert alerts == ["morning"]


def test_morning_run_manual_failure_does_not_ping(monkeypatch):
    from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))

    executor = MorningDCAExecutor(MorningConfig(), mode=MORNING_LIVE_MODE)
    monkeypatch.setattr(
        executor, "_run_impl", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        executor.run(datetime(2026, 7, 23, 10, 30), data=None, manual=True)
    assert pings == []
