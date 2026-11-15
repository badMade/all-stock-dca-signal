"""Job restoration, child cleanup, and streamed-output redaction regressions."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import dca_console_jobs
from dca_console_jobs import Command, JobManager


def saved_job(root: Path, name: str, **updates):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": name,
        "status": "completed",
        "action": "demo-dca",
        "label": "DCA demo",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "returncode": 0,
        "log": "Completed normally.",
    }
    payload.update(updates)
    (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def join_job(manager):
    assert manager.worker is not None
    manager.worker.join(timeout=5)
    assert not manager.worker.is_alive()
    return manager.snapshot()[0]


def test_malformed_restored_jobs_cannot_break_json_or_block_new_work(tmp_path):
    root = tmp_path / "jobs"
    saved_job(root, "bad-status", status=["running"])
    saved_job(root, "bad-returncode", returncode=float("nan"))
    saved_job(root, "valid")
    manager = JobManager(root)
    try:
        restored = manager.snapshot()
        json.dumps(restored, allow_nan=False)
        assert any(item["id"] == "valid" for item in restored)
        assert all(isinstance(item["status"], str) for item in restored)
        manager.submit("demo-dca", "New test", lambda job_id, log: 0)
        assert join_job(manager)["status"] == "completed"
    finally:
        manager.close()


def test_restored_logs_are_bounded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CONSOLE_TEST_SECRET", "restore-private-credential")
    root = tmp_path / "jobs"
    saved_job(root, "large-log", log="x" * 50_000 + "\nrestore-private-credential\n")
    manager = JobManager(root)
    try:
        item = manager.snapshot()[0]
        assert len(item["log"]) <= 24_000
        assert "restore-private-credential" not in item["log"]
        assert "[redacted]" in item["log"]
    finally:
        manager.close()


def test_restore_is_newest_first_and_new_job_takes_first_position(tmp_path):
    root = tmp_path / "jobs"
    # Written out per day rather than interpolated: interpolating the day into
    # the middle of the stamp leaves a truncated seven-digit run in the source
    # that is not a real date, so the secrets guard
    # (test_no_account_shaped_literal_in_tracked_config_source_or_docs) reads it
    # as an account-shaped literal. A whole stamp is exempted as a date; a
    # truncated one is not. Saved out of order on purpose: the assertion below
    # is what pins newest-first.
    for job_id, started_at in (
        ("20260101T120000-example", "2026-01-01T12:00:00+00:00"),
        ("20260103T120000-example", "2026-01-03T12:00:00+00:00"),
        ("20260102T120000-example", "2026-01-02T12:00:00+00:00"),
    ):
        saved_job(root, job_id, started_at=started_at)
    manager = JobManager(root)
    try:
        assert [item["id"] for item in manager.snapshot()] == [
            "20260103T120000-example",
            "20260102T120000-example",
            "20260101T120000-example",
        ]
        submitted = manager.submit("demo-dca", "New job", lambda job_id, log: 0)
        assert join_job(manager)["id"] == submitted["id"]
        assert manager.snapshot()[1]["id"] == "20260103T120000-example"
    finally:
        manager.close()


@pytest.mark.parametrize("stop_kind", ["timeout", "shutdown"])
def test_command_timeout_and_shutdown_reap_the_child_process(tmp_path, monkeypatch, stop_kind):
    original_popen = subprocess.Popen
    processes = []
    launched = threading.Event()

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(dca_console_jobs.subprocess, "Popen", capture_process)
    manager = JobManager(tmp_path / "jobs")
    command = Command(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        tmp_path,
        dict(os.environ),
        timeout=0.1 if stop_kind == "timeout" else 30,
    )
    try:
        manager.submit("demo-dca", "Bounded child", lambda job_id, log: manager.run_command(command, log))
        assert launched.wait(2)
        if stop_kind == "shutdown":
            manager.close()
        result = join_job(manager)
        assert result["status"] == "interrupted"
        assert result["returncode"] == 3
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        manager.close()
        # A failed cleanup assertion must not itself leave a test child alive.
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def test_secrets_split_across_child_output_reads_are_redacted_before_persistence(tmp_path, monkeypatch):
    private_value = "streamed-private-credential"
    monkeypatch.setenv("DCA_CONSOLE_TEST_SECRET", private_value)
    code = (
        "import os,time; value=os.environ['DCA_CONSOLE_TEST_SECRET']; "
        "os.write(1,value[:9].encode()); time.sleep(0.15); "
        "os.write(1,(value[9:]+'\\n').encode())"
    )
    command = Command((sys.executable, "-c", code), tmp_path, dict(os.environ), timeout=5)
    manager = JobManager(tmp_path / "jobs")
    try:
        manager.submit("demo-dca", "Stream output", lambda job_id, log: manager.run_command(command, log))
        result = join_job(manager)
        assert result["status"] == "completed"
        assert private_value not in result["log"]
        assert "private-credential" not in result["log"]
        assert "[redacted]" in result["log"]
        assert private_value not in (manager.root / f"{result['id']}.json").read_text()
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "posix", reason="Process-group cleanup requires POSIX signals")
@pytest.mark.parametrize("stop_kind", ["timeout", "shutdown"])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_deadline_and_shutdown_reap_descendants_that_retain_stdout(tmp_path, monkeypatch, stop_kind, ignore_term):
    original_popen, original_killpg = subprocess.Popen, os.killpg
    processes, sent_signals = [], []
    launched = threading.Event()

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    def capture_signal(group, signum):
        sent_signals.append(signum)
        original_killpg(group, signum)

    monkeypatch.setattr(dca_console_jobs.subprocess, "Popen", capture_process)
    monkeypatch.setattr(dca_console_jobs.os, "killpg", capture_signal)
    child = (
        "import pathlib,signal,sys,time; "
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
        + "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
        "ready=pathlib.Path(sys.argv[2]); "
        "\nwhile not ready.exists(): time.sleep(0.01)"
    )
    manager = JobManager(tmp_path / "jobs")
    command = Command(
        (sys.executable, "-c", parent, child, str(tmp_path / "ready")),
        tmp_path,
        dict(os.environ),
        # The deadline must outlast the two nested interpreter starts plus the ready handshake below, or it fires
        # first and SIGTERMs the parent before it can exit 0 (-15). 0.5 s was enough on a quiet Linux box but not on
        # a loaded macOS runner (CI 2026-11-15, both Mac runners). Slowing every interpreter start by 0.35 s
        # (a sitecustomize sleep) reproduces it deterministically; 6 s leaves room for a start ten times slower.
        timeout=6 if stop_kind == "timeout" else 30,
    )
    try:
        manager.submit("demo-dca", "Descendant cleanup", lambda job_id, log: manager.run_command(command, log))
        assert launched.wait(5)
        # The pipe remains open after the process that run_command owns exits.
        assert processes[0].wait(timeout=5) == 0
        if stop_kind == "shutdown":
            manager.close()
        assert manager.worker is not None
        # Deadline (6 s) + the 10 s SIGTERM grace in run_command + slack; it was 13 s for the old 0.5 s deadline.
        manager.worker.join(timeout=25)
        assert not manager.worker.is_alive()
        result = manager.snapshot()[0]
        assert result["status"] == "interrupted"
        assert result["returncode"] == 3
        assert signal.SIGTERM in sent_signals
        if ignore_term:
            assert signal.SIGKILL in sent_signals
    finally:
        # Only signal the group created by this test, including if its leader exited.
        for process in processes:
            try:
                original_killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        manager.close()


@pytest.mark.parametrize("failure", ["persist", "construct", "start"])
@pytest.mark.parametrize("prior_work", [False, True])
def test_submit_startup_failure_preserves_history_and_allows_retry(tmp_path, monkeypatch, failure, prior_work):
    root = tmp_path / "jobs"
    for index in range(30):
        saved_job(root, f"prior-{index:02}")
    manager = JobManager(root)
    ran = threading.Event()
    error_type = OSError if failure == "persist" else RuntimeError

    def fail(*args, **kwargs):
        raise error_type("Injected startup failure")

    try:
        if prior_work:
            manager.submit("demo-dca", "Prior work", lambda job_id, log: 0)
            join_job(manager)
        before, previous_worker = manager.snapshot(), manager.worker
        with monkeypatch.context() as startup:
            if failure == "persist":
                startup.setattr(manager, "_persist", fail)
            elif failure == "construct":
                startup.setattr(dca_console_jobs.threading, "Thread", fail)
            else:
                startup.setattr(threading.Thread, "start", fail)
            with pytest.raises(error_type, match="Injected startup failure"):
                manager.submit("demo-dca", "Failed startup", lambda job_id, log: ran.set() or 0)
        assert not ran.is_set()
        assert manager.snapshot() == before
        assert manager.worker is previous_worker
        manager.submit("demo-dca", "Retry", lambda job_id, log: 0)
        result = join_job(manager)
        assert result["status"] == "completed"
        assert len(manager.snapshot()) == 30
        manager.close()
    finally:
        # Preserve cleanup even when exercising the original unstarted-thread bug.
        if manager.worker is not None and manager.worker.ident is None:
            manager.worker = None
        manager.close()


def test_capture_to_keeps_stdout_as_a_redacted_document_and_streams_stderr(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    target = tmp_path / "out" / "document.json"
    code = (
        "import json, sys; sys.stderr.write('progress line\\n'); "
        "print(json.dumps({'volume': 1234567.5, 'note': 'account 999000111 seen', 'ok': True}))"
    )
    command = Command((sys.executable, "-c", code), tmp_path, dict(os.environ), capture_to=target)
    try:
        manager.submit("gate5-replay", "Capture", lambda job_id, log: manager.run_command(command, log))
        result = join_job(manager)
        assert result["status"] == "completed" and result["returncode"] == 0
        assert "progress line" in result["log"]
        # stdout went to the document, not the log; numbers survive because the
        # document is walked as JSON rather than scrubbed as text.
        assert '{"volume"' not in result["log"]
        assert "document.json" in result["log"]
        document = json.loads(target.read_bytes())
        assert document["volume"] == 1234567.5 and document["ok"] is True
        assert "999000111" not in document["note"]
    finally:
        manager.close()


def test_capture_limit_refuses_to_save_a_partial_document(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console_jobs, "CAPTURE_LIMIT_BYTES", 16)
    manager = JobManager(tmp_path / "jobs")
    target = tmp_path / "out" / "document.json"
    command = Command((sys.executable, "-c", "print('x' * 200)"), tmp_path, dict(os.environ), capture_to=target)
    try:
        manager.submit("gate5-replay", "Capture", lambda job_id, log: manager.run_command(command, log))
        result = join_job(manager)
        assert result["status"] == "completed"
        assert not target.exists()
        assert "was not saved" in result["log"]
    finally:
        manager.close()


def test_cancel_terminates_the_child_and_marks_the_job_cancelled(tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    processes = []
    launched = threading.Event()

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(dca_console_jobs.subprocess, "Popen", capture_process)
    manager = JobManager(tmp_path / "jobs")
    command = Command((sys.executable, "-c", "import time; time.sleep(30)"), tmp_path, dict(os.environ), timeout=30)
    try:
        assert manager.cancel("nothing-running") is False
        submitted = manager.submit("demo-dca", "Cancellable", lambda job_id, log: manager.run_command(command, log))
        assert launched.wait(2)
        assert manager.cancel("another-id") is False
        assert manager.cancel(submitted["id"]) is True
        result = join_job(manager)
        assert result["status"] == "cancelled"
        assert result["returncode"] == dca_console_jobs.CANCELLED_RETURNCODE
        assert "cancelled by the operator" in result["log"]
        assert processes and all(process.poll() is not None for process in processes)
        # A finished job can no longer be cancelled, and its file says cancelled.
        assert manager.cancel(submitted["id"]) is False
        assert json.loads((manager.root / f"{submitted['id']}.json").read_bytes())["status"] == "cancelled"
        restored = JobManager(manager.root)
        assert restored.snapshot()[0]["status"] == "cancelled"
        restored.close()
    finally:
        manager.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def test_log_since_returns_only_unseen_text_and_reports_window_truncation(tmp_path):
    manager = JobManager(tmp_path / "jobs")

    def work(job_id, log):
        log("a" * 100)
        log("b" * 30_000)
        return 0

    try:
        submitted = manager.submit("demo-dca", "Chatty", work)
        join_job(manager)
        assert manager.log_since("missing-job", 0) is None
        first = manager.log_since(submitted["id"], 0)
        assert first is not None
        assert first["offset"] == 30_100 and first["truncated"] is True
        assert first["text"] == "b" * 24_000 and first["status"] == "completed"
        tail = manager.log_since(submitted["id"], 30_090)
        assert tail is not None
        assert tail["text"] == "b" * 10 and tail["truncated"] is False
        at_end = manager.log_since(submitted["id"], 30_100)
        assert at_end is not None and at_end["text"] == ""
        past_end = manager.log_since(submitted["id"], 99_999)
        assert past_end is not None and past_end["text"] == ""
    finally:
        manager.close()


def test_job_log_shows_checkout_relative_argv_not_absolute_paths(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    target = tmp_path / "out" / "document.json"
    command = Command(
        (sys.executable, "-c", "print('ok')", "--artifacts-dir", str(tmp_path / "out")),
        tmp_path,
        dict(os.environ),
        capture_to=target,
    )
    try:
        manager.submit("demo-dca", "Relative argv", lambda job_id, log: manager.run_command(command, log))
        result = join_job(manager)
        assert result["status"] == "completed"
        assert str(tmp_path) not in result["log"]
        assert "--artifacts-dir out" in result["log"]
        assert os.path.basename(sys.executable) in result["log"]
    finally:
        manager.close()


def test_restored_timestamps_are_validated_not_redacted(tmp_path):
    root = tmp_path / "jobs"
    saved_job(
        root, "stamped", started_at="2026-09-17T23:36:05.123456+00:00", finished_at="not a timestamp", log_total=12
    )
    saved_job(root, "unstamped", started_at=None, log_total="many")
    manager = JobManager(root)
    try:
        by_id = {item["id"]: item for item in manager.snapshot()}
        assert by_id["stamped"]["started_at"] == "2026-09-17T23:36:05.123456+00:00"
        assert by_id["stamped"]["finished_at"] is None
        assert by_id["stamped"]["log_total"] == 12
        assert by_id["unstamped"]["started_at"] is None
        assert by_id["unstamped"]["log_total"] == len("Completed normally.")
    finally:
        manager.close()
