"""local_heartbeat start/finish publish the slot-status signal the Perplexity backup reads first.

Only the primary's own lifecycle publishes: start -> in_progress, finish -> succeeded or failed. Probes, claims,
confirms and beats never publish (a backup must not mark a slot done, and a push every 60 s would be noise).
Publishing is best-effort: whatever it does, start and finish return exactly what they returned before.
"""

import pytest

from scripts import local_heartbeat as hb

DAY, SLOT = "2026-10-29", "0935"


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(hb.router, "default_log_dir", lambda: tmp_path / "routing")


@pytest.fixture
def published(monkeypatch):
    calls = []

    def fake_update(day, slot, status, **kwargs):
        calls.append((day, slot, status))
        return {"recorded": True, "published": True, "detail": "abc123"}

    monkeypatch.setattr(hb.slot_status, "update", fake_update)
    return calls


def test_start_publishes_in_progress(published):
    assert hb.start(DAY, SLOT, now=1000.0)["ok"]
    assert published == [(DAY, SLOT, "in_progress")]


def test_a_refused_start_publishes_nothing(published):
    hb.start(DAY, SLOT, now=1000.0)
    published.clear()
    assert hb.start(DAY, SLOT, now=1010.0)["ok"] is False  # a live run owns the slot
    assert published == []


@pytest.mark.parametrize("outcome, status", [("success", "succeeded"), ("failure", "failed")])
def test_finish_publishes_the_outcome(published, outcome, status):
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    published.clear()
    assert hb.finish(DAY, SLOT, token, outcome) == {"ok": True, "released": True}
    assert published == [(DAY, SLOT, status)]


def test_a_superseded_finish_publishes_nothing(published):
    old = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.start(DAY, SLOT, now=5000.0)  # the first lease went stale and a new run took the slot
    published.clear()
    assert hb.finish(DAY, SLOT, old, "success") == {"ok": False, "released": False}
    assert published == []


def test_probes_claims_confirms_and_beats_never_publish(published):
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    published.clear()
    hb.beat(DAY, SLOT, token, now=1030.0)
    hb.status(DAY, SLOT, now=1040.0)
    hb.claim(DAY, SLOT, now=1050.0)
    hb.confirm(DAY, SLOT, now=1060.0)
    assert published == []


def test_a_crashing_publisher_never_changes_what_start_and_finish_return(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("publisher bug")

    monkeypatch.setattr(hb.slot_status, "update", boom)
    started = hb.start(DAY, SLOT, now=1000.0)
    assert started["ok"] is True and "token" in started
    assert hb.finish(DAY, SLOT, started["token"], "success") == {"ok": True, "released": True}
    assert hb.status(DAY, SLOT, now=1010.0)["local_status"] == "succeeded"


def test_cli_output_is_unchanged_by_publishing(published, capsys):
    import json

    assert hb.main(["start", "--trading-day", DAY, "--slot", SLOT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"ok", "reason", "token"}
