"""Tests for dca_alerts: failure alerting, heartbeats, and the guarded_run wrapper."""

import io
import json
import urllib.request
from datetime import datetime

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
