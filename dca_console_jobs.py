"""Bounded, observable local jobs for the DCA console (standard library only)."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from dca_fileio import atomic_write_text

_REGISTERED_SECRETS_LOCK = threading.Lock()
_REGISTERED_SECRETS: tuple[str, ...] = ()


def register_secret(secret: str) -> None:
    """Keep an in-process secret available to every console redaction boundary."""
    if not secret:
        return
    if not isinstance(secret, str):
        raise TypeError("registered console secrets must be text")
    global _REGISTERED_SECRETS
    with _REGISTERED_SECRETS_LOCK:
        # The auth-file password must never be moved into the environment merely
        # so the older environment-value pass below can find it: every console
        # job inherits a copy of that environment.  Keep an immutable snapshot
        # instead, and retain old values for the process lifetime so credential
        # rotation cannot uncover a value already held by an exception or log
        # buffer (Same Page round 1).
        _REGISTERED_SECRETS = tuple(sorted({*_REGISTERED_SECRETS, secret}, key=len, reverse=True))


def redact(text: str) -> str:
    """Remove credential-shaped values before persisting or showing job output."""
    from near_close_dca import _redacted_diagnostic_text

    # HTTP handlers are threaded, and a future credential reload may register a
    # replacement while another thread is formatting a failure.  Copy the
    # immutable tuple under its small dedicated lock, then release the lock
    # before the deliberately thorough diagnostic sanitizer runs (panel finding,
    # round 3).  Longest-first replacement prevents an overlapping shorter
    # secret from leaving the suffix of a longer one visible.
    with _REGISTERED_SECRETS_LOCK:
        registered = _REGISTERED_SECRETS
    for secret in registered:
        text = text.replace(secret, "[redacted]")
    # Preserve multiline credential delimiters until the shared sanitizer sees them.
    text = _redacted_diagnostic_text(text).replace("[REDACTED]", "[redacted]")
    for name, value in os.environ.items():
        if value and len(value) >= 6 and any(key in name.upper() for key in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "ACCOUNT_NUMBER")):
            text = text.replace(value, "[redacted]")
    text = re.sub(r'(?i)((?:account(?:_number)?|token|api_key|password|secret)[\s\"\x27:=]+)[^\s,}\"\x27]+', r'\1[redacted]', text)
    text = re.sub(r"\b\d{6,12}\b", "[redacted-number]", text)
    return text


_SENSITIVE_VARIABLE_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "ACCOUNT_NUMBER", "WEBHOOK")


def is_sensitive_variable(name: str) -> bool:
    """Return whether an environment variable name marks its value as secret."""
    upper = name.upper()
    return any(marker in upper for marker in _SENSITIVE_VARIABLE_MARKERS)


def redact_value(text: str) -> str:
    """Remove secret VALUES from one string inside a structured response.

    :func:`redact` is the boundary for free text (job logs, error bodies,
    request headers) and therefore also erases every bare 6-12 digit number and
    every ``label=value`` pair. That is right for a log line and wrong for a
    JSON field: a job id carries an 8-digit date stamp, a catalog command shows
    the literal placeholder ``--account <acct>``, and a documentation note may
    legitimately say "password". This variant keeps those and removes only what
    is actually secret: every registered console secret, every environment
    value whose variable name marks it sensitive, and the credential/account
    shapes the shared strategy sanitizer recognises (GUI expansion, Phase 0).
    """
    from near_close_dca import _ACCOUNT_LABELED_DIGITS, _ACCOUNT_SHAPED_LITERAL, _URL_EMBEDDED_CREDENTIALS

    with _REGISTERED_SECRETS_LOCK:
        registered = _REGISTERED_SECRETS
    for secret in registered:
        text = text.replace(secret, "[redacted]")
    # The strategy sanitizer's labelled-secret scan is deliberately NOT applied:
    # it rewrites "password must have 12 characters" to "password=[redacted]",
    # which is right for an exception message and wrong for documentation
    # prose. Its account-number and URL-credential shapes are exact enough to
    # keep; a bare digit run glued to a word character (a job id's date stamp,
    # a run directory name) never matches them.
    text = _URL_EMBEDDED_CREDENTIALS.sub("://[redacted]@", text)
    text = _ACCOUNT_LABELED_DIGITS.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _ACCOUNT_SHAPED_LITERAL.sub("[redacted]", text)
    for name, value in os.environ.items():
        if value and len(value) >= 6 and is_sensitive_variable(name):
            text = text.replace(value, "[redacted]")
    return text


@dataclass(frozen=True)
class Command:
    """An explicit executable argument vector; shell evaluation is never used."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float = 600


class JobBusy(RuntimeError):
    """A console job is already using the local worker."""


class JobManager:
    """Run one action at a time, retain bounded logs, and persist terminal status."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.jobs: list[dict] = []
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self._restore()

    def _restore(self) -> None:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.json"))[-30:]:
            try:
                if not path.resolve().is_relative_to(self.root.resolve()) or path.stat().st_size > 100_000:
                    continue
                item = json.loads(path.read_bytes())
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or re.fullmatch(r"[a-zA-Z0-9-]{1,100}", item["id"]) is None):
                    continue
                status, code = item.get("status"), item.get("returncode")
                if not isinstance(status, str) or status not in {
                    "queued", "running", "completed", "failed", "interrupted",
                }:
                    continue
                if code is not None and (type(code) is not int or not -255 <= code <= 255):
                    continue
                clean = {"id": item["id"], "status": status, "returncode": code}
                for key in ("action", "label", "started_at", "finished_at", "log"):
                    value = item.get(key)
                    if isinstance(value, str):
                        safe = redact(value)
                        clean[key] = safe[-24_000:] if key == "log" else safe[:500]
                    else:
                        clean[key] = None
                item = clean
                if status in {"queued", "running"}:
                    item["status"] = "interrupted"
                    item["log"] = (item.get("log") or "") + "\nConsole stopped before this job completed."
                self.jobs.append(item)
            except (OSError, ValueError, RecursionError):
                continue

    def snapshot(self) -> list[dict]:
        """Return a detached copy of the most recent jobs, newest first."""
        with self.lock:
            return [dict(item) for item in self.jobs[-30:]][::-1]

    def submit(self, action: str, label: str, work: Callable[[str, Callable[[str], None]], int]) -> dict:
        """Start fixed application work, refusing duplicate or overlapping jobs."""
        with self.lock:
            if self.stop.is_set():
                raise JobBusy("The console is shutting down.")
            if any(j["status"] in {"running", "queued"} for j in self.jobs):
                raise JobBusy("Another job is running. Wait for it to finish before starting another.")
            item = {
                "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8],
                "action": action, "label": label, "status": "queued",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None, "returncode": None, "log": "",
            }
            worker = threading.Thread(target=self._work, args=(item, work), daemon=True)
            self._persist(item)
            worker.start()
            # _work takes this lock before running. Publish only after startup
            # succeeds, so a failure preserves the prior worker and job history.
            self.jobs = (self.jobs + [item])[-30:]
            self.worker = worker
            return dict(item)

    def _persist(self, item: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.root / f"{item['id']}.json", json.dumps(item, indent=2))

    def _work(self, item: dict, work: Callable) -> None:
        def log(message: str) -> None:
            with self.lock:
                item["log"] = (item["log"] + redact(message))[-24_000:]

        with self.lock:
            item["status"] = "running"
        try:
            result = work(item["id"], log)
            status = "completed" if result == 0 else "interrupted" if result == 3 else "failed"
        except Exception as error:
            # A failed action must be reported without taking down the HTTP worker.
            log(f"\n{type(error).__name__}: {error}\n")
            result, status = 1, "failed"
        with self.lock:
            item.update(status=status, returncode=result, finished_at=datetime.now(timezone.utc).isoformat())
            try:
                self._persist(item)
            except OSError as error:
                log(f"\nCould not persist job status: {error}\n")
                item["status"] = "failed"
                item["returncode"] = 1

    def run_command(self, command: Command, log: Callable[[str], None]) -> int:
        """Stream command output while enforcing a deadline and graceful shutdown."""
        if self.stop.is_set():
            return 3
        log("\nRunning: " + " ".join(command.argv) + "\n")
        with subprocess.Popen(
            command.argv, cwd=command.cwd, env=command.env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=os.name == "posix", bufsize=0,
        ) as process:
            assert process.stdout is not None
            if os.name == "posix":
                os.set_blocking(process.stdout.fileno(), False)
            stop_reading = threading.Event()

            def pump() -> None:
                from near_close_dca import _find_armor_spans

                assert process.stdout is not None
                # Buffer partial lines so a secret cannot be split across redaction calls.
                pending = ""
                discarding = False
                omit_remainder = False
                while not stop_reading.is_set():
                    chunk = process.stdout.read(4096)
                    if chunk is None:
                        stop_reading.wait(0.05)
                        continue
                    if not chunk or stop_reading.is_set():
                        break
                    if omit_remainder:
                        continue
                    decoded = chunk.decode("utf-8", errors="replace")
                    if discarding:
                        if "\n" not in decoded:
                            continue
                        _, decoded = decoded.split("\n", 1)
                        discarding = False
                    pending += decoded
                    while "\n" in pending:
                        boundary = pending.index("\n") + 1
                        spans = _find_armor_spans(pending)
                        if any(private and not closed for _, _, private, closed in spans):
                            break
                        # Keep each private-key block intact until redaction, including nested armor.
                        for start, end, private, _ in spans:
                            if private and start < boundary:
                                boundary = max(boundary, end)
                        log(pending[:boundary])
                        pending = pending[boundary:]
                    if len(pending) > 32_000:
                        # Never expose a suffix after dropping the context that identifies a secret.
                        omit_remainder = any(private and not closed for _, _, private, closed in _find_armor_spans(pending))
                        log("[long output line omitted]\n")
                        pending, discarding = "", True
                if pending:
                    log(pending)

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            deadline = time.monotonic() + command.timeout
            interrupted = False
            while process.poll() is None or reader.is_alive():
                if self.stop.wait(0.2) or time.monotonic() >= deadline:
                    interrupted = True
                    log("\nJob stopping: console shutdown or execution time limit reached.\n")
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    except ProcessLookupError:
                        pass
                    grace_deadline = time.monotonic() + 10
                    while (process.poll() is None or reader.is_alive()) and time.monotonic() < grace_deadline:
                        time.sleep(0.05)
                    if process.poll() is None or reader.is_alive():
                        try:
                            if os.name == "posix":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        except ProcessLookupError:
                            pass
                    break
            code = process.wait()
            reader.join(timeout=2)
            # A descendant may have escaped the group while retaining stdout.
            # Stop the POSIX reader even without EOF; unbuffered close never waits
            # behind a reader's buffered-I/O lock on the other platforms.
            stop_reading.set()
            reader.join(timeout=0.2)
            return 3 if interrupted else code

    def close(self) -> None:
        """Request termination and let the running action persist its final state."""
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=15)
