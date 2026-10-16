"""scripts/local_heartbeat.py: fail-closed behaviour for the duplicate-buy paths (review findings 1 to 4, PR #1644).

Written from the required behaviour, not from any fix:

2. A crash mid-write of the result marker must never leave a torn marker at the marker path.
3. A lease or marker file that exists but cannot be understood is UNKNOWN (backup stands down), never OFF.

Findings 1 and 4 (overlapping probes, a late local start beside the backup) are now covered by the claim/confirm
protocol merged to main in PR 1639 (``test_concurrent_claims_have_exactly_one_winner``,
``test_a_late_primary_start_is_refused_after_a_fallback_claim``), so their tests were dropped in the merge.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from scripts import local_heartbeat as hb

DAY, SLOT = "2026-10-08", "1530"


@pytest.fixture(autouse=True)
def _lease_root(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(hb.router, "default_log_dir", lambda: tmp_path / "routing")


# --- finding 2 -------------------------------------------------------------------------------------------------

def test_a_crash_mid_write_of_the_marker_never_leaves_a_torn_marker(monkeypatch):
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    _, marker = hb._lease(DAY, SLOT)
    armed = {"on": False}
    real_write_text, real_os_write = pathlib.Path.write_text, os.write

    def torn_write_text(self, data, *args, **kwargs):
        if armed["on"] and self.name.endswith(".result"):
            real_write_text(self, data[:3], *args, **kwargs)  # partial content, then the process "dies"
            raise OSError("simulated crash mid-write")
        return real_write_text(self, data, *args, **kwargs)

    def torn_os_write(fd, data):
        if armed["on"]:
            real_os_write(fd, data[:3])
            raise OSError("simulated crash mid-write")
        return real_os_write(fd, data)

    monkeypatch.setattr(pathlib.Path, "write_text", torn_write_text)
    monkeypatch.setattr(os, "write", torn_os_write)
    armed["on"] = True
    try:
        hb.finish(DAY, SLOT, token, "success")
    except OSError:
        pass
    armed["on"] = False

    assert not marker.exists() or marker.read_text(encoding="utf-8") == "success\n"
    # The run never recorded its outcome, so the backup must not be told it may act while the lease is live.
    assert hb.status(DAY, SLOT, now=1001.0)["backup_may_act"] is False


# --- state directory not writable (2026-10-16 09:49 ET incident) -------------------------------------------------
# macOS privacy protection on ~/Documents made ``cron_tracking/scheduler_leases`` uncreatable, and ``status`` died with
# a PermissionError traceback instead of printing the documented stand-down. The outcome (no backup buy) was right,
# but the crash gave the caller no parseable answer and no reason.

def _deny_state_dir(monkeypatch):
    real_mkdir = pathlib.Path.mkdir

    def denied(self, *args, **kwargs):
        if self == hb.lease_dir():
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", denied)


def test_an_uncreatable_state_dir_makes_status_stand_down_instead_of_crashing(monkeypatch):
    _deny_state_dir(monkeypatch)
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "unknown", "backup_may_act": False}


def test_an_uncreatable_state_dir_makes_claim_stand_down_instead_of_crashing(monkeypatch):
    _deny_state_dir(monkeypatch)
    assert hb.claim(DAY, SLOT, now=1000.0) == {"local_status": "unknown", "backup_may_act": False}


def test_the_status_command_exits_zero_with_stand_down_json_when_the_state_dir_is_unwritable(monkeypatch, capsys):
    _deny_state_dir(monkeypatch)
    assert hb.main(["status", "--trading-day", DAY, "--slot", SLOT]) == 0
    assert capsys.readouterr().out.strip() == '{"backup_may_act": false, "local_status": "unknown"}'


# --- finding 3 -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("garbage", ["not json", "", "[]", '{"holder_id": "x", "ttl_seconds": 0}'])
def test_an_unreadable_lease_file_stands_the_backup_down(garbage):
    lease, _ = hb._lease(DAY, SLOT)
    lease.base_dir.mkdir(parents=True, exist_ok=True)
    lease.path.write_text(garbage, encoding="utf-8")
    assert hb.status(DAY, SLOT, now=1000.0) == {"local_status": "unknown", "backup_may_act": False}


@pytest.mark.parametrize("garbage", ["succ", "", "SUCCESS", "banana\n"])
def test_an_unrecognized_marker_stands_the_backup_down(garbage):
    token = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, token, "success")  # releases the lease and leaves a marker...
    _, marker = hb._lease(DAY, SLOT)
    marker.write_text(garbage, encoding="utf-8")  # ...which we then corrupt
    assert hb.status(DAY, SLOT, now=1001.0) == {"local_status": "unknown", "backup_may_act": False}
