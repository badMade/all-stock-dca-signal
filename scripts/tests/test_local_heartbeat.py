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


def test_status_probe_is_repeatable_but_a_claim_blocks_a_second_buy(tmp_path):
    # The late-start guard probes twice; neither probe is a buy (review of PR 1639).
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "off", "backup_may_act": True}
    assert hb.status(DAY, SLOT, now=1001.0) == {"local_status": "off", "backup_may_act": True}

    assert hb.claim(DAY, SLOT, now=1002.0) == {"local_status": "off", "backup_may_act": True}
    log = tmp_path / "routing" / f"routing_{DAY}.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(row["probe"], row["path"]) for row in rows] == [
        (True, "perplexity-fallback"),
        (True, "perplexity-fallback"),
        (False, "perplexity-fallback"),
    ]
    assert rows[0]["role"] == "perplexity"

    assert hb.claim(DAY, SLOT, now=1003.0) == {"local_status": "off", "backup_may_act": False}
    assert hb.status(DAY, SLOT, now=1004.0) == {"local_status": "off", "backup_may_act": False}


def test_a_corrupt_lease_or_marker_is_unknown_and_the_backup_stands_down(tmp_path):
    lease_path = tmp_path / "leases" / f"{hb.window_key(DAY, hb.STREAM, SLOT)}.lease"
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{not json", encoding="utf-8")
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "unknown", "backup_may_act": False}

    lease_path.unlink()
    lease_path.with_suffix(".result").write_text("garbage\n", encoding="utf-8")
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "unknown", "backup_may_act": False}


def test_a_superseded_run_cannot_write_a_marker_or_release(tmp_path):
    old = hb.start(DAY, SLOT, now=1000.0)["token"]
    new = hb.start(DAY, SLOT, now=5000.0)["token"]  # the first lease is stale: reclaimed by the new run
    assert hb.finish(DAY, SLOT, old, "failure") == {"ok": False, "released": False}
    assert hb.status(DAY, SLOT, now=5010.0)["local_status"] == "in_progress"
    assert hb.finish(DAY, SLOT, new, "success") == {"ok": True, "released": True}


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


def _claim_worker(lease_root, log_root, queue):
    hb.lease_dir = lambda: lease_root
    hb.router.default_log_dir = lambda: log_root
    queue.put(hb.claim(DAY, SLOT, now=1000.0)["backup_may_act"])


def test_concurrent_claims_have_exactly_one_winner(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")  # fork keeps the fixture's patches; the workers re-patch anyway
    queue = ctx.Queue()
    procs = [ctx.Process(target=_claim_worker, args=(tmp_path / "leases", tmp_path / "routing", queue)) for _ in range(6)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
    assert sorted(results) == [False] * 5 + [True]


def test_start_after_a_failed_run_never_exposes_a_fresh_lease_with_the_stale_marker(tmp_path, monkeypatch):
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "failure")
    seen = []
    real_acquire = hb.SchedulerLease.acquire

    def acquire_then_probe(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        # A probe (which needs the state lock) cannot run between the acquire and the marker reset.
        try:
            with hb.cross_process_lock(tmp_path / "leases" / f"{hb.window_key(DAY, hb.STREAM, SLOT)}.state.lock", timeout_seconds=0.2):
                seen.append("probe-could-run")
        except TimeoutError:
            seen.append("blocked")
        return result

    monkeypatch.setattr(hb.SchedulerLease, "acquire", acquire_then_probe)
    assert hb.start(DAY, SLOT, now=2000.0)["ok"]
    assert seen == ["blocked"]
    assert hb.status(DAY, SLOT, now=2010.0)["local_status"] == "in_progress"


def test_an_unobtainable_state_lock_fails_closed_for_every_command(monkeypatch):
    class _Held:
        def __enter__(self):
            raise TimeoutError("held")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(hb, "_state_lock", lambda day, slot: _Held())
    stand_down = {"local_status": "unknown", "backup_may_act": False}
    assert hb.status(DAY, SLOT, now=1000.0) == stand_down
    assert hb.claim(DAY, SLOT, now=1000.0) == stand_down
    assert hb.start(DAY, SLOT, now=1000.0) == {"ok": False, "reason": "state_lock_timeout"}
    assert hb.finish(DAY, SLOT, "tok", "success") == {"ok": False, "released": False}


def test_a_late_primary_start_is_refused_after_a_fallback_claim():
    assert hb.claim(DAY, SLOT, now=1000.0)["backup_may_act"] is True
    assert hb.start(DAY, SLOT, now=1001.0) == {"ok": False, "reason": "fallback_claimed"}
    assert hb.status(DAY, SLOT, now=1002.0)["local_status"] == "off"  # no lease was taken
    # A bare probe is not a claim, so a primary can still start.
    assert hb.start("2026-10-09", SLOT, now=1.0)["ok"]


# --- Codex adversarial review of PR 1639: a claim is not a buy ------------------------------------------------


def test_a_claim_is_not_purchase_evidence_and_confirm_is():
    import dca_execution_router as router

    assert hb.claim(DAY, SLOT, now=1000.0)["backup_may_act"] is True
    assert router.confirmed_buy_path(router.default_log_dir(), DAY, SLOT) is None  # claimed, nothing bought
    assert hb.confirm(DAY, SLOT, now=1005.0) == {"ok": True, "already": False}
    assert router.confirmed_buy_path(router.default_log_dir(), DAY, SLOT) == router.PATH_FALLBACK
    assert hb.confirm(DAY, SLOT, now=1006.0) == {"ok": True, "already": True}  # idempotent


def test_confirm_without_a_claim_is_refused():
    assert hb.confirm(DAY, SLOT, now=1000.0) == {"ok": False, "reason": "no_claim"}


def test_a_live_claim_blocks_a_second_backup_but_an_abandoned_one_expires():
    assert hb.claim(DAY, SLOT, now=1000.0)["backup_may_act"] is True
    assert hb.claim(DAY, SLOT, now=1100.0)["backup_may_act"] is False  # inside the 600s TTL: still held
    # The first run died before placing anything: no confirm, so after the TTL a later run may act again.
    assert hb.claim(DAY, SLOT, now=1000.0 + 601)["backup_may_act"] is True


def test_a_confirmed_buy_blocks_every_later_claim_even_after_the_ttl():
    hb.claim(DAY, SLOT, now=1000.0)
    hb.confirm(DAY, SLOT, now=1010.0)
    assert hb.claim(DAY, SLOT, now=1000.0 + 5000)["backup_may_act"] is False


def test_a_primary_start_is_refused_during_a_live_claim_and_allowed_once_it_lapses_unconfirmed():
    hb.claim(DAY, SLOT, now=1000.0)
    assert hb.start(DAY, SLOT, now=1100.0) == {"ok": False, "reason": "fallback_claimed"}
    assert hb.start(DAY, SLOT, now=1000.0 + 601)["ok"] is True  # nothing was bought: the primary may still run


def test_a_primary_start_stays_refused_after_a_confirmed_fallback_buy():
    hb.claim(DAY, SLOT, now=1000.0)
    hb.confirm(DAY, SLOT, now=1010.0)
    assert hb.start(DAY, SLOT, now=1000.0 + 5000) == {"ok": False, "reason": "fallback_claimed"}


def test_status_after_a_live_claim_reports_the_backup_may_not_act():
    hb.claim(DAY, SLOT, now=1000.0)
    assert hb.status(DAY, SLOT, now=1050.0)["backup_may_act"] is False


def test_start_that_cannot_clear_the_stale_marker_releases_its_lease_and_reports_it(tmp_path, monkeypatch):
    # Adversarial review of PR 1639: start() acquires the lease and only then clears the previous result marker. If
    # that clear fails (or the process dies between the two), a FRESH lease sits beside a stale "failure" marker, and
    # the marker wins in local_status_from_probe: the backup reads ERRORED and buys while the new primary is live.
    # Contract: a failed marker reset must not leave the lease held, must not raise, and must say why.
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "failure")
    marker = tmp_path / "leases" / f"{hb.window_key(DAY, hb.STREAM, SLOT)}.result"
    assert marker.read_text(encoding="utf-8").strip() == "failure"

    real_unlink = hb.Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == marker:
            raise PermissionError("cannot clear the marker")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(hb.Path, "unlink", failing_unlink)
    assert hb.start(DAY, SLOT, now=2000.0) == {"ok": False, "reason": "marker_reset_failed"}

    lease, _ = hb._lease(DAY, SLOT)
    assert not lease.is_held(now=2000.0)  # nothing is live: no primary run owns the slot
    # The honest picture is the previous run's failure, with no live lease: the backup may act, and no run is live.
    assert hb.status(DAY, SLOT, now=2001.0) == {"local_status": "errored", "backup_may_act": True}

    # Once the marker can be cleared again, a retry starts normally.
    monkeypatch.setattr(hb.Path, "unlink", real_unlink)
    assert hb.start(DAY, SLOT, now=2002.0)["ok"]


def test_start_reports_when_it_cannot_give_the_lease_back_after_a_failed_marker_reset(tmp_path, monkeypatch):
    # Copilot review of PR 1648: release() returns False when its atomic take fails. start() must not report only
    # "marker_reset_failed" then: the fresh lease is still held and blocks retries until its TTL, so say so.
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "failure")
    marker = tmp_path / "leases" / f"{hb.window_key(DAY, hb.STREAM, SLOT)}.result"
    real_unlink = hb.Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == marker:
            raise PermissionError("cannot clear the marker")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(hb.Path, "unlink", failing_unlink)
    monkeypatch.setattr(hb.SchedulerLease, "release", lambda self, holder_id: False)
    assert hb.start(DAY, SLOT, now=2000.0) == {"ok": False, "reason": "marker_reset_failed", "lease_released": False}

    lease, _ = hb._lease(DAY, SLOT)
    assert lease.is_held(now=2000.0)  # the lease really is still held, which is what the caller is being told
