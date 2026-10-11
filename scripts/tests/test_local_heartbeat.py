"""scripts/local_heartbeat.py: the local run's heartbeat and the backup's status probe."""

import json

import pytest

from scripts import local_heartbeat as hb

DAY, SLOT = "2026-10-08", "1530"


@pytest.fixture(autouse=True)
def _lease_root(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(hb.router, "default_log_dir", lambda: tmp_path / "routing")


def test_no_lease_on_an_online_mac_means_off_and_the_backup_may_act():
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "off", "backup_may_act": True}


def test_status_persists_fallback_and_blocks_a_repeated_decision(tmp_path):
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "off", "backup_may_act": True}

    log = tmp_path / "routing" / f"routing_{DAY}.jsonl"
    first = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert first["role"] == "perplexity"
    assert first["path"] == "perplexity-fallback"
    assert first["may_place"] is True

    assert hb.status(DAY, SLOT, now=1001.0) == {"local_status": "off", "backup_may_act": False}
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["may_place"] for row in rows] == [True, False]


def test_fresh_heartbeat_is_in_progress_and_stale_one_is_errored():
    assert hb.start(DAY, SLOT, now=1000.0)["ok"]
    assert hb.status(DAY, SLOT, now=1299.0)["local_status"] == "in_progress"
    assert hb.status(DAY, SLOT, now=1300.0) == {"local_status": "errored", "backup_may_act": True}


def test_beat_keeps_the_run_alive_past_the_original_window():
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    assert hb.beat(DAY, SLOT, token, now=1200.0)["ok"]
    assert hb.status(DAY, SLOT, now=1450.0)["local_status"] == "in_progress"


def test_finish_success_blocks_the_backup_and_failure_allows_it():
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    assert hb.finish(DAY, SLOT, token, "success") == {"ok": True, "released": True}
    assert hb.status(DAY, SLOT, now=1010.0) == {"local_status": "succeeded", "backup_may_act": False}
    token = hb.start(DAY, SLOT, now=1020.0)["token"]
    hb.finish(DAY, SLOT, token, "failure")
    assert hb.status(DAY, SLOT, now=1030.0) == {"local_status": "errored", "backup_may_act": True}


def test_a_restart_clears_the_previous_marker():
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "failure")
    hb.start(DAY, SLOT, now=2000.0)
    assert hb.status(DAY, SLOT, now=2001.0)["local_status"] == "in_progress"


@pytest.mark.parametrize("mac,expected", [("offline", "off"), ("timeout", "unreachable"),
                                          ("device_error", "errored"), ("banana", "unknown")])
def test_a_non_online_probe_status_ignores_the_files(mac, expected):
    hb.start(DAY, SLOT, now=1000.0)
    assert hb.status(DAY, SLOT, mac, now=1001.0)["local_status"] == expected


def test_every_start_gets_its_own_token():
    first = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, first, "success")
    assert hb.start(DAY, SLOT, now=1001.0)["token"] != first


def test_a_duplicate_start_for_a_live_slot_is_refused_and_leaves_the_first_run_intact():
    first = hb.start(DAY, SLOT, now=1000.0)
    assert first["ok"]
    second = hb.start(DAY, SLOT, now=1010.0)
    assert second["ok"] is False and second["reason"] == "held_by_live_holder" and "token" not in second
    # The first run still owns the lease, and the refused start did not refresh it.
    assert hb.beat(DAY, SLOT, first["token"], now=1100.0)["ok"]
    assert hb.status(DAY, SLOT, now=1399.0)["local_status"] == "in_progress"


def test_a_refused_duplicate_start_does_not_clear_the_result_marker():
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "success")
    lease, marker = hb._lease(DAY, SLOT)
    lease.acquire("someone-else", ttl_seconds=300, now=1005.0)  # a live holder appears after the finish
    assert hb.start(DAY, SLOT, now=1010.0)["ok"] is False
    assert marker.read_text(encoding="utf-8").strip() == "success"


def test_beat_and_finish_reject_a_token_that_does_not_own_the_lease():
    owner = hb.start(DAY, SLOT, now=1000.0)["token"]
    assert hb.beat(DAY, SLOT, "not-the-owner", now=1100.0)["ok"] is False
    assert hb.finish(DAY, SLOT, "not-the-owner", "success") == {"ok": False, "released": False}
    # The impostor neither wrote a result marker nor released the owner's lease.
    assert hb.status(DAY, SLOT, now=1101.0)["local_status"] == "in_progress"
    assert hb.finish(DAY, SLOT, owner, "success")["released"] is True


def test_a_superseded_run_cannot_finish_or_beat_the_run_that_replaced_it():
    old = hb.start(DAY, SLOT, now=1000.0)["token"]
    new = hb.start(DAY, SLOT, now=1400.0)["token"]  # the old lease went stale and was reclaimed
    assert hb.beat(DAY, SLOT, old, now=1401.0)["ok"] is False
    assert hb.finish(DAY, SLOT, old, "failure")["ok"] is False
    assert hb.beat(DAY, SLOT, new, now=1402.0)["ok"]


def test_unknown_stands_the_backup_down():
    assert hb.status(DAY, SLOT, "banana", now=1.0)["backup_may_act"] is False


def test_cli_prints_json_and_exit_codes(capsys):
    assert hb.main(["start", "--trading-day", DAY, "--slot", SLOT]) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert hb.main(["status", "--trading-day", DAY, "--slot", SLOT]) == 0
    assert '"in_progress"' in capsys.readouterr().out
    assert hb.main(["start", "--trading-day", DAY, "--slot", SLOT]) == 1  # duplicate start
    assert hb.main(["beat", "--trading-day", DAY, "--slot", SLOT, "--token", token]) == 0
    assert hb.main(["beat", "--trading-day", DAY, "--slot", "0935", "--token", token]) == 1  # no lease for that slot
    assert hb.main(["finish", "--trading-day", DAY, "--slot", SLOT, "--token", "nope", "--outcome", "success"]) == 1
    assert hb.main(["finish", "--trading-day", DAY, "--slot", SLOT, "--token", token, "--outcome", "success"]) == 0


def test_cli_requires_a_token_for_beat_and_finish():
    with pytest.raises(SystemExit):
        hb.main(["beat", "--trading-day", DAY, "--slot", SLOT])
    with pytest.raises(SystemExit):
        hb.main(["finish", "--trading-day", DAY, "--slot", SLOT, "--outcome", "success"])
