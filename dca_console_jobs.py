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
from typing import Any, Callable
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
_REPO_LOCATION = str(Path(__file__).resolve().parent)
_HOME_LOCATION = str(Path.home())
# Longest first, so the checkout (which usually sits under the home directory)
# is rewritten before the home prefix could split it.
_LOCATION_PLACEHOLDERS = tuple(sorted(
    ((_REPO_LOCATION, "<checkout>"), (_HOME_LOCATION, "~")), key=lambda pair: len(pair[0]), reverse=True,
))
# Same shape as near_close_dca._ACCOUNT_SHAPED_LITERAL with the lower bound
# raised to nine digits; see redact_value for why eight-digit ids must survive.
_STRUCTURED_ACCOUNT_LITERAL = re.compile(r"(?<![.\w])\d{9,12}(?!\w|\.\d)")


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
    from near_close_dca import _ACCOUNT_LABELED_DIGITS, _URL_EMBEDDED_CREDENTIALS

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
    # The shared literal rule covers 6-12 bare digits, which also swallows the
    # scheduler's 8-hex-character task ids whenever one happens to be all
    # digits (42173154 is the 10:30 morning cron). A brokerage account number
    # is nine digits or longer, so structured responses use the 9-12 span and
    # keep the free-text sanitizer's wider net for logs and error bodies.
    text = _STRUCTURED_ACCOUNT_LITERAL.sub("[redacted]", text)
    for name, value in os.environ.items():
        if value and len(value) >= 6 and is_sensitive_variable(name):
            text = text.replace(value, "[redacted]")
    # Locations are not secrets, but an operator's home directory and the
    # checkout path are still never echoed to a browser: job logs persisted
    # before argv was spelled checkout-relative, and any artifact string that
    # embeds one, are rewritten to a placeholder (GUI expansion Phase 0 leak
    # fix, applied at the redaction chokepoint so every route inherits it).
    for location, placeholder in _LOCATION_PLACEHOLDERS:
        if location and location in text:
            text = text.replace(location, placeholder)
    return text


# A captured stdout document is kept whole up to this size; the Gate 5
# candidate documents are tens of kilobytes, a market graph payload a few
# megabytes, so this bound is generous without letting a runaway child fill
# memory (GUI expansion Phase 4 launchers).
CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
# Job return code reserved for an operator cancel, beside 3 for shutdown/deadline.
CANCELLED_RETURNCODE = 4


@dataclass(frozen=True)
class Command:
    """An explicit executable argument vector; shell evaluation is never used."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float = 600
    # When set, the child's stdout is captured whole into this file instead of
    # the job log (stderr still streams to the log). The seven Gate 5 candidate
    # generators, drift detection and the risk CLI print their document to
    # stdout and accept no --out flag, so this is how a launcher keeps their
    # output as an artifact (GUI expansion Phase 4).
    capture_to: Path | None = None


def _iso_timestamp_or_none(value: object) -> str | None:
    """Accept only a parseable ISO-8601 timestamp of bounded length."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _display_argv(command: Command) -> list[str]:
    """Spell argv for the job log with checkout-relative paths, never absolute ones.

    The interpreter and every artifact target are absolute on the wire (the
    child needs them) but the log is a browser response and a persisted file,
    so anything under the working directory is shown relative to it and any
    other absolute path is reduced to its basename (GUI expansion Phase 0
    leak fix, extended to job logs in Phase 5).
    """
    prefix = str(command.cwd).rstrip(os.sep) + os.sep
    shown = []
    for argument in command.argv:
        if argument.startswith(prefix):
            shown.append(argument[len(prefix):])
        elif os.path.isabs(argument):
            shown.append(os.path.basename(argument) or argument)
        else:
            shown.append(argument)
    return shown


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
        # One job runs at a time, so one cancel flag is enough: it belongs to
        # the job named by active_id and is replaced on every submit.
        self.active_cancel: threading.Event = threading.Event()
        self.active_id: str | None = None
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
                    "queued", "running", "completed", "failed", "interrupted", "cancelled",
                }:
                    continue
                if code is not None and (type(code) is not int or not -255 <= code <= 255):
                    continue
                clean: dict[str, Any] = {"id": item["id"], "status": status, "returncode": code}
                for key in ("action", "label", "log"):
                    value = item.get(key)
                    if isinstance(value, str):
                        safe = redact(value)
                        clean[key] = safe[-24_000:] if key == "log" else safe[:500]
                    else:
                        clean[key] = None
                for key in ("started_at", "finished_at"):
                    # A timestamp is validated, not redacted: the free-text
                    # sanitizer used to turn the microsecond field of a restored
                    # ISO stamp into "[redacted-number]", which the page could
                    # no longer parse (GUI expansion Phase 5 history fix).
                    clean[key] = _iso_timestamp_or_none(item.get(key))
                total = item.get("log_total")
                log_text = clean["log"] if isinstance(clean.get("log"), str) else ""
                clean["log_total"] = total if type(total) is int and 0 <= total <= 10**12 else len(log_text)
                item = clean
                if status in {"queued", "running"}:
                    item["status"] = "interrupted"
                    item["log"] = log_text + "\nConsole stopped before this job completed."
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
                # Total characters ever appended; the stored log is only the
                # last 24,000 of them, and log_since() needs the true offset to
                # hand a delta-polling page exactly the text it has not seen.
                "log_total": 0,
            }
            worker = threading.Thread(target=self._work, args=(item, work), daemon=True)
            self._persist(item)
            cancel = threading.Event()
            worker.start()
            # _work takes this lock before running. Publish only after startup
            # succeeds, so a failure preserves the prior worker and job history.
            self.jobs = (self.jobs + [item])[-30:]
            self.worker = worker
            self.active_cancel = cancel
            self.active_id = item["id"]
            return dict(item)

    def cancel(self, job_id: str) -> bool:
        """Ask the active job to stop; True only when that job is still queued or running.

        Only the job named by ``active_id`` can be cancelled: history entries
        are already terminal, and an unknown id is not evidence of anything.
        The flag is observed by run_command's poll loop, which terminates the
        child's whole process group and reports CANCELLED_RETURNCODE
        (GUI expansion Phase 5).
        """
        with self.lock:
            if self.active_id != job_id:
                return False
            active = next((item for item in self.jobs if item["id"] == job_id), None)
            if active is None or active["status"] not in {"queued", "running"}:
                return False
            self.active_cancel.set()
            return True

    def log_since(self, job_id: str, offset: int) -> dict | None:
        """Return the log text appended after ``offset`` characters, bounded by the stored window."""
        with self.lock:
            item = next((entry for entry in self.jobs if entry["id"] == job_id), None)
            if item is None:
                return None
            window = item.get("log") or ""
            total = int(item.get("log_total") or len(window))
            pending = max(0, total - max(0, offset))
            visible = min(pending, len(window))
            return {
                "id": item["id"], "status": item["status"], "returncode": item.get("returncode"),
                "offset": total, "text": window[len(window) - visible:] if visible else "",
                # True when the page fell further behind than the window keeps.
                "truncated": pending > len(window),
            }

    def _persist(self, item: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.root / f"{item['id']}.json", json.dumps(item, indent=2))

    def _work(self, item: dict, work: Callable) -> None:
        def log(message: str) -> None:
            with self.lock:
                safe = redact(message)
                item["log"] = (item["log"] + safe)[-24_000:]
                item["log_total"] = int(item.get("log_total") or 0) + len(safe)

        with self.lock:
            item["status"] = "running"
        try:
            result = work(item["id"], log)
            status = ("completed" if result == 0 else "interrupted" if result == 3
                      else "cancelled" if result == CANCELLED_RETURNCODE else "failed")
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
        cancelled = self.active_cancel
        if cancelled.is_set():
            return CANCELLED_RETURNCODE
        log("\nRunning: " + " ".join(_display_argv(command)) + "\n")
        capturing = command.capture_to is not None
        with subprocess.Popen(
            command.argv, cwd=command.cwd, env=command.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capturing else subprocess.STDOUT,
            start_new_session=os.name == "posix", bufsize=0,
        ) as process:
            assert process.stdout is not None
            # The log stream is the merged pipe normally and stderr alone when
            # stdout is being captured as a document.
            log_stream = process.stderr if capturing else process.stdout
            assert log_stream is not None
            if os.name == "posix":
                os.set_blocking(log_stream.fileno(), False)
            stop_reading = threading.Event()
            captured = bytearray()
            capture_truncated = [False]

            def capture() -> None:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    remaining = CAPTURE_LIMIT_BYTES - len(captured)
                    if len(chunk) > remaining:
                        # Mark the overflow on THIS chunk: a short document that
                        # arrives in one read would otherwise never be flagged.
                        capture_truncated[0] = True
                    if remaining > 0:
                        captured.extend(chunk[:remaining])

            def pump() -> None:
                from near_close_dca import _find_armor_spans

                # Buffer partial lines so a secret cannot be split across redaction calls.
                pending = ""
                discarding = False
                omit_remainder = False
                while not stop_reading.is_set():
                    chunk = log_stream.read(4096)
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
            readers = [reader]
            if capturing:
                capture_reader = threading.Thread(target=capture, daemon=True)
                capture_reader.start()
                readers.append(capture_reader)
            deadline = time.monotonic() + command.timeout
            interrupted = False
            was_cancelled = False
            while process.poll() is None or any(thread.is_alive() for thread in readers):
                if self.stop.wait(0.2) or cancelled.is_set() or time.monotonic() >= deadline:
                    interrupted = True
                    was_cancelled = cancelled.is_set()
                    log("\nJob stopping: cancelled by the operator.\n" if was_cancelled
                        else "\nJob stopping: console shutdown or execution time limit reached.\n")
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    except ProcessLookupError:
                        pass
                    grace_deadline = time.monotonic() + 10
                    while ((process.poll() is None or any(thread.is_alive() for thread in readers))
                           and time.monotonic() < grace_deadline):
                        time.sleep(0.05)
                    if process.poll() is None or any(thread.is_alive() for thread in readers):
                        try:
                            if os.name == "posix":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        except ProcessLookupError:
                            pass
                    break
            code = process.wait()
            for thread in readers:
                thread.join(timeout=2)
            # A descendant may have escaped the group while retaining stdout.
            # Stop the POSIX reader even without EOF; unbuffered close never waits
            # behind a reader's buffered-I/O lock on the other platforms.
            stop_reading.set()
            reader.join(timeout=0.2)
            if command.capture_to is not None and not interrupted:
                self._write_capture(command.capture_to, bytes(captured), capture_truncated[0], log)
            return CANCELLED_RETURNCODE if was_cancelled else 3 if interrupted else code

    @staticmethod
    def _write_capture(target: Path, data: bytes, truncated: bool, log: Callable[[str], None]) -> None:
        """Persist a captured stdout document, redacting strings without breaking JSON.

        A JSON document is walked so only its string values are redacted (a
        volume of 1234567.5 must stay a number); anything else goes through the
        free-text sanitizer. Truncation is recorded in the log and refuses to
        pass off a partial document as complete.
        """
        from dca_console_routes import walk_redact

        text = data.decode("utf-8", errors="replace")
        if truncated:
            log(f"\nCaptured stdout exceeded {CAPTURE_LIMIT_BYTES} bytes and was not saved.\n")
            return
        try:
            document = json.loads(text)
        except ValueError:
            safe = redact(text)
        else:
            safe = json.dumps(walk_redact(document), indent=2, allow_nan=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, safe)
        log(f"\nCaptured stdout ({len(data)} bytes) to {target.name}\n")

    def close(self) -> None:
        """Request termination and let the running action persist its final state."""
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=15)
