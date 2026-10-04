"""Local DCA control panel and single-command test launcher.

Core runtime is stdlib-only. Research commands run in a separate interpreter;
the console reads their documented artifacts and never imports their models.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from dca_console_jobs import Command, JobBusy, JobManager, is_sensitive_variable, redact, register_secret
from dca_console_reader import root_label
from dca_console_routes import CREDENTIAL_FREE_ROUTES, DEFAULT_CSP, GET_ROUTES, RouteRequest
from dca_fileio import atomic_write_text
from dca_paths import cron_tracking_root, stream_root

REPO = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
# The complete browser bundle, as (route, file under console/, content type).
# ASSETS stays an exact-match dict derived from this literal tuple: there is no
# directory walk, so no other repository file can ever be served (pinned by
# tests/test_dca_console.py and tests/test_console_assets.py).
CONSOLE_FILES = (
    ("/", "index.html", "text/html"),
    ("/app.js", "app.js", "text/javascript"),
    ("/pages.js", "pages.js", "text/javascript"),
    ("/markdown.js", "markdown.js", "text/javascript"),
    ("/styles.css", "styles.css", "text/css"),
)
ASSETS = {route: (name, kind) for route, name, kind in CONSOLE_FILES}
MAX_AUTH_FILE_BYTES = 4096
MIN_AUTH_PASSWORD_CHARACTERS = 12
HANDLER_TIMEOUT_SECONDS = 30
MAX_REPORTED_HOST_CHARACTERS = 253
MAX_SUMMARY_SIGNATURE_DAYS = 32
_HOSTNAME_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_PATTERN = re.compile(rf"{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*")


def _configured_authority(value: object) -> str:
    """Normalize one configured hostname authority or raise a rule-specific error."""
    if not isinstance(value, str) or not value:
        raise ValueError("--trusted-host must be a non-empty hostname with an optional port.")
    try:
        authority = value.encode("ascii").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("--trusted-host must contain ASCII hostname characters only.") from None
    hostname, separator, port = authority.rpartition(":")
    if not separator:
        hostname = authority
    elif not hostname or not port.isdigit() or int(port) > 65535:
        raise ValueError("--trusted-host port must be a number no greater than 65535.")
    if len(hostname) > MAX_REPORTED_HOST_CHARACTERS or _HOSTNAME_PATTERN.fullmatch(hostname) is None:
        raise ValueError("--trusted-host must be a hostname without a scheme, path, wildcard, or whitespace.")
    return authority


def _single_header(headers, name: str) -> tuple[bool, str | None]:
    """Return one header without accepting ambiguous duplicate security fields."""
    try:
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            raw_values = get_all(name, [])
            values = list(raw_values) if isinstance(raw_values, (list, tuple)) else []
            if not values:
                return False, None
            if len(values) != 1 or not isinstance(values[0], str):
                return True, None
            return True, values[0]
        value = headers.get(name)
    except Exception:
        return True, None
    if value is None:
        return False, None
    return True, value if isinstance(value, str) else None


def _lower_ascii_header(headers, name: str) -> tuple[bool, str | None]:
    """Read one security header as lowercase ASCII; invalid bytes never match."""
    present, value = _single_header(headers, name)
    if value is None:
        return present, None
    try:
        return True, value.encode("ascii").decode("ascii").lower()
    except UnicodeError:
        return True, None


def _reported_header(headers, name: str) -> str | None:
    """Bound and redact one client-supplied authority for diagnostic JSON."""
    try:
        value = headers.get(name)
        if value is None:
            return None
        # Redaction must precede truncation: a boundary cut can otherwise leave
        # a credential fragment the shared matcher no longer recognizes (Same
        # Page round 2).
        return redact(str(value))[:MAX_REPORTED_HOST_CHARACTERS]
    except Exception:
        # Diagnostics are never important enough to weaken a 403 or health
        # response when a malformed test double/header object raises.
        return None


@dataclass(frozen=True)
class Settings:
    """Explicit data roots and Python runtimes for one local console session."""

    mode: str
    root: Path
    output: Path
    day: date | None
    python: str
    research_python: str
    octopus_run: Path | None = None
    config_dir: Path = REPO / "config"
    # Extra artifact trees to observe read-only beside ``root``: the gitignored
    # repo-root artifacts/ directory holds more run states and alert files than
    # the dca_paths-managed tree on this machine (GUI expansion Phase 0). Demo
    # mode never sets it; readers label every root and never echo its path.
    secondary_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ConsoleAccess:
    """Optional HTTP credential, validated wherever a console server is built."""

    username: str = ""
    credential: bytes | None = field(default=None, repr=False)
    bind: str = "127.0.0.1"
    trusted_hosts: tuple[str, ...] = ()
    behind_proxy: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.username, str):
            raise ValueError("The auth username must be text.")
        if self.credential is None:
            if self.username:
                # Keep the unauthenticated compatibility state singular.  A
                # username with no comparison value must never quietly turn
                # authentication off for a future make_server() caller (Same
                # Page round 1).
                raise ValueError("Console access without a credential requires an empty username.")
        else:
            if type(self.credential) is not bytes:
                raise ValueError("The console credential must be bytes.")
            if (not self.username or ":" in self.username
                    or any(not 0x20 <= ord(character) <= 0x7e for character in self.username)):
                raise ValueError("The auth username must be non-empty printable ASCII without a colon.")
            if len(self.credential) > MAX_AUTH_FILE_BYTES:
                raise ValueError("The console credential must not exceed the auth-file size limit.")
            try:
                decoded = self.credential.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("The auth credential must be valid UTF-8.") from None
            credential_username, separator, password = decoded.partition(":")
            if not separator or credential_username != self.username:
                raise ValueError("The auth credential must match its username and contain a colon.")
            if "\r" in password or "\n" in password:
                raise ValueError("The auth credential must contain exactly one line.")
            if len(password) < MIN_AUTH_PASSWORD_CHARACTERS:
                raise ValueError("The auth password must contain at least 12 characters.")

            # Do not put this value in argv or the environment just to make the
            # pre-existing redactor discover it: jobs copy both.  Register the
            # password, full Basic cleartext, and its wire representation so any
            # later diagnostic path masks all useful forms (Same Page round 1).
            for value in (password, decoded):
                register_secret(value)
                # http.server decodes request-line bytes as ISO-8859-1, while a
                # browser may percent-encode them.  Register each full spelling,
                # not a short printable projection, so normalization cannot expose
                # most of a non-ASCII secret (panel finding, round 1).
                wire_value = value.encode("utf-8").decode("iso-8859-1")
                if wire_value != value:
                    register_secret(wire_value)
                quoted_value = quote(value, safe="")
                if quoted_value != value:
                    register_secret(quoted_value)
            register_secret(base64.b64encode(self.credential).decode("ascii"))

        if not isinstance(self.bind, str):
            raise ValueError("--bind must be an IPv4 address literal.")
        try:
            bind_address = ipaddress.IPv4Address(self.bind)
        except ipaddress.AddressValueError:
            raise ValueError("--bind must be an IPv4 address literal.") from None
        object.__setattr__(self, "bind", str(bind_address))
        if not isinstance(self.trusted_hosts, (tuple, list)):
            raise ValueError("trusted_hosts must be a sequence of --trusted-host values.")
        normalized_hosts = tuple(dict.fromkeys(_configured_authority(value) for value in self.trusted_hosts))
        object.__setattr__(self, "trusted_hosts", normalized_hosts)
        if type(self.behind_proxy) is not bool:
            raise ValueError("behind_proxy must be a boolean.")

        # All exposure invariants live here rather than in main(): a future
        # make_server() caller therefore cannot open a reachable socket with an
        # invalid policy (Rock 2 contract; Same Page round 2).
        if self.behind_proxy and not normalized_hosts:
            raise ValueError("--behind-proxy requires at least one --trusted-host.")
        if bind_address.is_unspecified and not normalized_hosts:
            raise ValueError("--bind 0.0.0.0 requires at least one --trusted-host.")
        if self.behind_proxy and not bind_address.is_loopback:
            raise ValueError("--behind-proxy requires a loopback --bind address.")
        if not bind_address.is_loopback and self.credential is None:
            raise ValueError("A non-loopback --bind requires --auth-file.")
        if normalized_hosts and self.credential is None:
            raise ValueError("--trusted-host requires --auth-file.")

    def hosts(self, port: int) -> set[str]:
        """Return the complete set of accepted Host authorities for one port."""
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", *self.trusted_hosts}
        bind_address = ipaddress.IPv4Address(self.bind)
        if not bind_address.is_loopback and not bind_address.is_unspecified:
            allowed.add(f"{self.bind}:{port}")
        return allowed

    def effective_authority(self, headers, port: int) -> str | None:
        """Return the accepted Host, or a configured forwarded Host in proxy mode."""
        _, received_host = _lower_ascii_header(headers, "Host")
        if received_host not in self.hosts(port):
            return None
        if not self.behind_proxy:
            return received_host
        forwarded_present, forwarded_host = _lower_ascii_header(headers, "X-Forwarded-Host")
        if not forwarded_present:
            return received_host
        if forwarded_host not in self.trusted_hosts or self._loopback_authority(forwarded_host):
            # A present forwarded authority has precedence and must never fall
            # back to Host when it is malformed/unconfigured (Same Page round 3).
            return None
        return forwarded_host

    def trusted(self, headers, port: int) -> bool:
        """Return whether Host, forwarded Host and Origin describe one authority."""
        authority = self.effective_authority(headers, port)
        if authority is None:
            return False
        origin_present, origin = _lower_ascii_header(headers, "Origin")
        if not origin_present:
            return True
        return origin in {f"http://{authority}", f"https://{authority}"}

    @staticmethod
    def _loopback_authority(authority: str | None) -> bool:
        if authority is None:
            return False
        hostname = authority.rsplit(":", 1)[0] if ":" in authority else authority
        if hostname == "localhost":
            return True
        try:
            return ipaddress.IPv4Address(hostname).is_loopback
        except ipaddress.AddressValueError:
            return False

    def authorized(self, headers) -> bool:
        """Return whether one strict HTTP Basic header matches this policy."""
        if self.credential is None:
            return True
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            raw_values = get_all("Authorization", [])
            values = list(raw_values) if isinstance(raw_values, (list, tuple)) else []
        else:
            value = headers.get("Authorization")
            values = [] if value is None else [value]
        # Multiple Authorization fields are ambiguous after HTTP combination;
        # refusing them is the only fail-closed interpretation.
        if len(values) != 1 or not isinstance(values[0], str):
            return False
        parts = values[0].strip().split(None, 1)
        if len(parts) != 2 or parts[0].casefold() != "basic":
            return False
        try:
            supplied = base64.b64decode(parts[1].encode("ascii"), validate=True)
        except (UnicodeError, ValueError):
            return False
        return secrets.compare_digest(supplied, self.credential)


def load_auth_file(path: str | os.PathLike[str]) -> tuple[str, bytes]:
    """Load one checked Basic credential from the same bounded POSIX descriptor."""
    if os.name != "posix":
        raise ValueError("--auth-file is supported on POSIX only.")
    try:
        # O_NOFOLLOW turns a symlink into ELOOP, and every later check/read uses
        # the returned descriptor, closing the writable-parent check/use race
        # (Same Page round 2). O_NONBLOCK lets a writerless FIFO open at once so
        # fstat can reject it and the CLI exits 2 instead of hanging the console
        # and launchd renderer; regular-file reads are unaffected (Same Page
        # round 3). O_CLOEXEC keeps the descriptor out of every job child.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    except AttributeError:
        raise ValueError("--auth-file requires POSIX no-follow and nonblocking file flags.") from None
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("--auth-file must not be a symlink.") from None
        raise ValueError("--auth-file must exist and be readable without following symlinks.") from None

    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("--auth-file must be a regular file.")
            if metadata.st_uid != os.getuid():
                raise ValueError("--auth-file owner must be the current user.")
            if metadata.st_mode & 0o077:
                raise ValueError("--auth-file must not grant group or other permissions.")
            if not 0 <= metadata.st_size <= MAX_AUTH_FILE_BYTES:
                raise ValueError("--auth-file must contain at most 4096 bytes.")

            # The pathname is intentionally never opened a second time.  Both
            # the check and bounded read stay on this descriptor, closing the
            # writable-parent check/use race identified in Same Page round 2.
            remaining = MAX_AUTH_FILE_BYTES + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            contents = b"".join(chunks)
        except OSError:
            raise ValueError("--auth-file must remain readable through its checked descriptor.") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            # Closing cannot make already-read bytes come from another inode,
            # and an error here must not replace a more useful rule violation.
            pass

    if len(contents) > MAX_AUTH_FILE_BYTES:
        raise ValueError("--auth-file must contain at most 4096 bytes.")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("--auth-file must contain valid UTF-8.") from None
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text:
        raise ValueError("--auth-file must contain one non-empty line.")
    if "\r" in text or "\n" in text:
        raise ValueError("--auth-file must contain exactly one line.")
    username, separator, password = text.partition(":")
    if not separator:
        raise ValueError("--auth-file line must contain a colon.")
    credential = f"{username}:{password}".encode("utf-8")
    access = ConsoleAccess(username, credential)
    return access.username, credential


# The probe answers with import names; a reason shown to an operator has to name
# what ``pip install`` actually takes, so scikit-learn is never reported as the
# "sklearn" spec name (console clarity round).
RESEARCH_PACKAGES = ("numpy", "pandas", "sklearn")
PACKAGE_DISTRIBUTIONS = {"sklearn": "scikit-learn"}


def missing_packages(packages: dict, names: tuple[str, ...]) -> list[str]:
    """Name the probed packages an action still needs, in requirement order."""
    return [PACKAGE_DISTRIBUTIONS.get(name, name) for name in names if packages.get(name) is not True]


def dependency_reason(packages: dict, names: tuple[str, ...]) -> str:
    """Explain which research dependency blocks an action, or why none is known.

    One shared "install the requirements" line stood next to every research
    button, so an operator who already had NumPy and pandas installed could not
    tell which package was actually missing, and a probe that never answered
    read as a missing package (console clarity round).
    """
    probe_error = packages.get("error")
    if isinstance(probe_error, str):
        # A failed probe is not evidence of a missing package. Pointing pip at an
        # interpreter that never answered sends the operator the wrong way, so
        # report the probe itself and where its details are printed.
        return f"{probe_error} for --research-python; run --check to see which interpreter was probed."
    missing = missing_packages(packages, names)
    if not missing:
        return ""
    return (f"Install {', '.join(missing)} in --research-python's environment; "
            "wondrous-octopus/requirements.txt covers them.")


def python_packages(executable: str) -> dict:
    """Probe optional package availability without importing research modules."""
    names = ("numpy", "pandas", "sklearn", "torch", "pytest", "ruff", "jsonschema", "yaml", "yfinance", "tkinter")
    code = (
        "import importlib.util,json; "
        f"print(json.dumps({{n: importlib.util.find_spec(n) is not None for n in {names!r}}}))"
    )
    try:
        result = subprocess.run([executable, "-c", code], capture_output=True, text=True, timeout=15, shell=False)  # nosec B603 - fixed command, explicit shell=False
        return json.loads(result.stdout) if result.returncode == 0 else {"error": "Python probe failed"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"error": "Python runtime is unavailable"}


def default_demo_day() -> date:
    """Choose a recent full market session using the core's holiday calendar."""
    from dca_runner import market_day_status

    day = datetime.now(ET).date() - timedelta(days=1)
    for _ in range(15):
        status = market_day_status(day)
        if not status.is_closed and not status.is_early_close:
            return day
        day -= timedelta(days=1)
    raise ValueError("No recent full demo session could be selected; supply --date explicitly.")


@contextmanager
def _sigterm_as_keyboard_interrupt():
    """Route SIGTERM through the existing Ctrl+C cleanup path while installed."""
    if threading.current_thread() is not threading.main_thread():
        # signal.signal() is main-thread-only. A future embedded caller still
        # gets a usable context manager without weakening the process-wide
        # handler owned by its main thread (Same Page round 2).
        yield
        return

    previous_handler = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum, _frame) -> None:
        # server.shutdown() deadlocks when called by the same thread that is in
        # serve_forever(). Raising here deliberately reuses the proven Ctrl+C
        # unwind and JobManager child teardown (Same Page round 2).
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        # This launcher must not leave a caller's process-wide signal policy
        # changed after serving stops (Rock 3 handler-hygiene contract).
        signal.signal(signal.SIGTERM, previous_handler)


def display_python(python: str) -> str:
    """Spell the console's interpreter for a copyable command without its absolute path.

    The catalog used to interpolate ``shlex.quote(python)``, which put the
    operator's home directory into every snapshot and saved summary. A path
    inside the checkout is shown relative to it (``.venv/bin/python`` still
    runs from the repository root); anything else is reduced to its basename
    (GUI expansion Phase 0 leak fix).
    """
    try:
        path = Path(python)
        if path.is_absolute() and path.resolve().is_relative_to(REPO):
            return shlex.quote(str(path.resolve().relative_to(REPO)))
        return shlex.quote(path.name or python)
    except (OSError, RuntimeError, ValueError):
        return "python3"


def module_catalog(python: str) -> list[dict]:
    """Expose the repository's canonical entry points without executing them."""
    from scripts.agent_entry_points import ENTRY_POINTS

    interpreter = display_python(python)

    def runnable(command: str) -> str:
        for prefix in ("python3 ", "python "):
            if command.startswith(prefix):
                return interpreter + " " + command[len(prefix):]
        return command

    rows = [{"id": f"entry-{i}", "name": e.need, "description": e.notes,
             "command": runnable(e.command), "docs": e.doc_ref, "status": "Existing entry point"}
            for i, e in enumerate(ENTRY_POINTS)]
    extras = [
        ("Octopus replay", "Walk-forward OHLC predictions and regime validation.",
         "wondrous-octopus/run_walkforward.py --preset quick --dry-run", "docs/wondrous_octopus.md"),
        ("Octopus daily update", "Score matured forecasts, update models, predict the next trading row.",
         "wondrous-octopus/daily_update.py --run-dir <run-dir>", "docs/wondrous_octopus.md"),
        ("Octopus inspection", "Inspect a ticker, predictions, or model report.",
         "wondrous-octopus/inspect_run.py --run-dir <run-dir> --summary", "docs/wondrous_octopus.md"),
        ("Ridge sizing research", "Train and evaluate sizing research without publishing to live configuration.",
         "ml-forecaster/train.py --dry-run --models-dir <research-output>", "ml-forecaster/README.md"),
        ("Market graph", "Create the existing interactive market graph from your local archive.",
         "-m scripts.generate_market_graph --source archive --out <output.html>", "docs/market_graph.md"),
        ("Market breadth", "Optional external market breadth data collector; requires its own setup.",
         "ticker-market-breadth/fetch_tick.py --help", "ticker-market-breadth/README.md"),
        ("Strategy research", "Independent backtesting and sentiment tools with optional dependencies.",
         "trading-strategies/backtest.py --help", "trading-strategies/README.md"),
        ("Agent loop primitives", "Specification and bounded-agent-loop utilities; no live wiring by default.",
         "", "production-safe-agent-loop/README.md"),
        ("Sizing signal interfaces", "Research-side forecasting and sizing interfaces.",
         "", "dca-sizing-signals/README.md"),
    ]
    rows.extend({"id": f"extra-{i}", "name": name, "description": description,
                 "command": f"{interpreter} {command}" if command else "Library API; see documentation",
                 "docs": docs, "status": "Optional research / manual inputs"}
                for i, (name, description, command, docs) in enumerate(extras))
    return rows


class ConsoleApp:
    """Bridge local artifact readers, fixed jobs, daily summaries, and the UI."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.access: ConsoleAccess | None = None
        self.jobs = JobManager(settings.output / "jobs")
        self.token = secrets.token_urlsafe(32)
        self.packages = python_packages(settings.research_python)
        self.dev_packages = python_packages(settings.python)
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.lock = threading.RLock()
        self.signatures: dict[str, str] = {}
        self.octopus_run = settings.octopus_run
        self._load_demo_pointer()

    def _load_demo_pointer(self) -> None:
        if self.settings.mode != "demo" or self.octopus_run is not None:
            return
        pointer = self.settings.output / "demo" / "octopus-current.json"
        try:
            value = json.loads(pointer.read_bytes())
            path = Path(value["path"]).resolve()
            if path.is_relative_to((self.settings.output / "demo" / "octopus").resolve()) and path.is_dir():
                self.octopus_run = path
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def default_day(self) -> date:
        """Return the pinned day or today's Eastern date for an unpinned session."""
        if self.settings.day is not None:
            return self.settings.day
        # Local launchd service sessions can survive midnight. Resolve this
        # through the module's datetime name on every request so a new tab does
        # not inherit the process-start date (Same Page round 2).
        return datetime.now(ET).date()

    def actions(self) -> list[dict]:
        """List fixed actions, with explicit dependency/setup availability."""
        # Every reason below names exactly what is missing. A shared research
        # string could not distinguish one absent package from a failed probe,
        # and the update action reported both blockers whichever one applied
        # (console clarity round).
        research_reason = dependency_reason(self.packages, RESEARCH_PACKAGES)
        research = not research_reason
        neural_reason = dependency_reason(self.packages, (*RESEARCH_PACKAGES, "torch"))
        update_reason = " ".join(part for part in (
            "Supply --octopus-run with an existing Octopus run directory." if self.octopus_run is None else "",
            research_reason,
        ) if part)
        definitions = [
            ("demo-all", "Run full demo", "DCA pipeline + Octopus replay + daily summary.", True, ""),
            ("demo-dca", "Test DCA pipeline", "Run the real dry-run pipeline and reporting with synthetic market data.", True, ""),
            ("demo-octopus", "Test Octopus pipeline", "Replay synthetic data through persistence and GBM baselines.", research, research_reason),
            ("demo-neural", "Test neural Octopus", "Replay synthetic data through the actual PyTorch Octopus model.",
             not neural_reason, neural_reason),
            ("reports", "Save daily reports", "Generate dated JSON / Markdown daily summaries and confirmed-purchase exports.",
             self.settings.root.is_dir(), "No artifact directory is available yet."),
            ("test-signals", "Test all seven signal slots", "Run the existing seven-slot regression tests locally.",
             self.dev_packages.get("pytest") is True, "Install requirements-dev.txt in the Python environment."),
            ("octopus-update", "Update Octopus day", "Score, update and forecast using the configured research checkpoint and local archive.",
             research and self.octopus_run is not None, update_reason),
            ("octopus-estimate", "Estimate archive replay", "Read the archive and estimate a quick replay; fits no models.",
             research, research_reason),
            ("ticker-refresh-missing", "Update active ticker data",
             "Gather and update daily OHLCV bars for active DCA tickers from Yahoo chart feed.",
             True, ""),
            ("train-foundation-smoke", "Train Octopus foundation (smoke run)",
             "Run Stage 1 curriculum pre-training smoke run on PyTorch with the neural Octopus model.",
             not neural_reason, neural_reason),
        ]
        # Demo actions always write under output/demo, never into the observed local root.
        from dca_console_launch import launch_actions

        rows = [{"id": action, "label": label, "description": description,
                 "available": bool(available), "reason": "" if available else reason}
                for action, label, description, available, reason in definitions]
        # The Phase 4 launchers join the same closed list; their inputs are
        # resolved server-side, never posted (dca_console_launch).
        rows.extend(launch_actions(self, REPO))
        return rows

    def snapshot(self, day: date | None = None, ticker: str = "", period: str = "all") -> dict:
        """Read current evidence, cache briefly, and save a sanitized daily digest."""
        from dca_console_data import build_daily_snapshot
        from dca_console_octopus import build_octopus_snapshot

        request_default_day = self.default_day()
        day = request_default_day if day is None else day
        key = (day.isoformat(), ticker, period)
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 12:
                data = dict(cached[1])
            else:
                now = datetime.now(ET)
                daily = build_daily_snapshot(
                    self.settings.root, day, now, self.settings.config_dir,
                    excluded_roots=(self.settings.output / "demo",) if self.settings.mode == "local" else (),
                )
                octopus = build_octopus_snapshot(self.octopus_run, ticker=ticker, period=period)
                data = {
                    "mode": self.settings.mode, "now": now.isoformat(), "default_date": request_default_day.isoformat(),
                    "daily": daily, "octopus": octopus, "warnings": [],
                    "modules": module_catalog(self.settings.python),
                    # Labels, never absolute paths: this document is a browser
                    # response and a saved summary (GUI expansion Phase 0).
                    "paths": {"artifacts": root_label(self.settings.root),
                              "summaries": root_label(self.settings.output / "summaries")},
                    "roots": [{"label": root_label(self.settings.root), "kind": "primary"}]
                    + [{"label": root_label(extra), "kind": "secondary"} for extra in self.settings.secondary_roots],
                }
                try:
                    overview = build_octopus_snapshot(self.octopus_run) if ticker or period != "all" else octopus
                    self._save_summary(daily, overview)
                except OSError as error:
                    data["warnings"].append(redact(f"Could not save daily summary: {error}"))
                if len(self.cache) >= 32:
                    self.cache.clear()
                self.cache[key] = (time.monotonic(), data)
            # An explicit-date snapshot can remain cached across midnight. The
            # evidence is still valid, but the UI's new-tab default must roll at
            # request time rather than lag for the cache TTL (Same Page round 2).
            data["default_date"] = request_default_day.isoformat()
        return dict(data, jobs=self.jobs.snapshot(), actions=self.actions())

    def _save_summary(self, daily: dict, octopus: dict) -> None:
        # Include the forecasting overview only; filters do not change the saved daily summary.
        doc = {"mode": self.settings.mode, "daily": daily,
               "forecast_summary": {"status": octopus.get("status"), "summary": octopus.get("summary", []),
                                    "warnings": octopus.get("warnings", [])}}
        content = json.dumps(doc, indent=2, allow_nan=False)
        day = daily["date"]
        signature = hashlib.sha256(content.encode()).hexdigest()
        if self.signatures.get(day) == signature:
            return
        folder = self.settings.output / "summaries" / self.settings.mode
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"# DCA daily summary — {day}", "", f"Mode: {self.settings.mode}", ""]
        lines.extend(str(line) for line in daily.get("summary", []))
        lines.extend(["", "## Observed runs", ""])
        lines.extend(f"- {r.get('time', 'Unknown time')} · {r.get('stream', 'unknown')} · "
                     f"{r.get('status', 'unknown')} · {r.get('mode', 'unknown')}"
                     for r in daily.get("runs", []))
        lines.extend(["", "## Forecast research", ""] + octopus.get("summary", []))
        lines.extend(["", "## Evidence warnings", ""] + daily.get("warnings", []) + octopus.get("warnings", []))
        atomic_write_text(folder / f"{day}.json", content)
        atomic_write_text(folder / f"{day}.md", "\n".join(lines) + "\n")
        self.signatures[day] = signature
        if len(self.signatures) > MAX_SUMMARY_SIGNATURE_DAYS:
            # A long-running service otherwise retains one digest hash per day
            # forever. ISO dates sort chronologically, so even an out-of-order
            # historical request cannot displace a newer day from the bounded
            # history (Rock 3 uptime bound).
            del self.signatures[min(self.signatures)]

    def submit(self, action: str) -> dict:
        """Dispatch an allowlisted action; API requests cannot supply commands or paths."""
        match = next((item for item in self.actions() if item["id"] == action), None)
        if not match:
            raise ValueError("Unknown action")
        if not match["available"]:
            raise ValueError(match["reason"])
        return self.jobs.submit(action, match["label"], lambda job_id, log: self._run(action, job_id, log))

    def _command(self, argv: list[str], log, *, demo: bool = False, research: bool = False, timeout: float = 600,
                 capture_to: Path | None = None) -> int:
        env = dict(os.environ)
        env["DCA_CRON_TRACKING_ROOT"] = str(self.settings.output / "demo" / "dca" if demo else self.settings.root)
        env["PYTHONUNBUFFERED"] = "1"
        # Job logs are read in a <pre>, not a terminal: ask pytest, ruff and
        # friends for plain text so ANSI colour codes never reach the page.
        env["NO_COLOR"] = "1"
        env["PY_COLORS"] = "0"
        env.pop("FORCE_COLOR", None)
        # No allowlisted job needs the brokerage account, a webhook, or any
        # token: purchase_history, the generators and the research CLIs read
        # none of them. Dropping every sensitive variable from EVERY child, not
        # only demo ones, makes that a property of the runner rather than of
        # each action's argv review (GUI expansion Phase 0).
        for key in list(env):
            if is_sensitive_variable(key):
                del env[key]
        if demo:
            # External artifact overrides would defeat the isolated demo root.
            for key in list(env):
                if key.startswith("DCA_") and key not in {"DCA_CRON_TRACKING_ROOT", "DCA_TICKER_HISTORY_DIR"}:
                    del env[key]
        executable = self.settings.research_python if research else self.settings.python
        return self.jobs.run_command(Command(tuple([executable, *argv]), REPO, env, timeout, capture_to=capture_to), log)

    def _run(self, action: str, job_id: str, log) -> int:
        from dca_console_launch import LAUNCH_SPECS, run_launch

        if action in LAUNCH_SPECS:
            today = self.settings.day or datetime.now(ET).date()
            return run_launch(self, action, job_id, log, repo=REPO, run_command=self._command, today=today)
        results = []
        if action in {"demo-all", "demo-dca"}:
            day = self.settings.day if self.settings.mode == "demo" else default_demo_day()
            target = self.settings.output / "demo" / "dca" / "ce16ba6b" / f"{day:%Y%m%d}_demo_{job_id}"
            results.append(self._command([
                "run_dca.py", "--demo", "--report", "--now", f"{day}T15:45",
                "--artifacts-dir", str(target), "--run-label", "console-demo",
            ], log, demo=True))
            if results[-1] == 0 and not (target / "run_state.json").exists():
                log("DCA produced no run_state.json: the configured date/window gate may have skipped this date.\n")
                results[-1] = 1
        if action in {"demo-all", "demo-octopus", "demo-neural"} and not self.jobs.stop.is_set():
            if not all(self.packages.get(k) for k in ("numpy", "pandas", "sklearn")):
                log("SKIPPED Octopus: research dependencies unavailable. DCA demo and summaries remain usable.\n")
                results.append(1)
            else:
                target = self.settings.output / "demo" / "octopus" / job_id
                argv = ["wondrous-octopus/run_walkforward.py", "--demo", "--run-dir", str(target)]
                if action == "demo-neural":
                    argv.extend(["--model", "octopus", "--device", "cpu", "--end", "2019-09-13", "--max-hours", "0.02"])
                result = self._command(argv, log, demo=True, research=True)
                results.append(result)
                if result == 0:
                    daily_args = ["wondrous-octopus/daily_update.py", "--run-dir", str(target)]
                    if action == "demo-neural":
                        daily_args.extend(["--now", "2019-09-14"])
                    results.append(self._command(
                        daily_args, log, demo=True, research=True,
                    ))
                if result == 0 and self.settings.mode == "demo":
                    with self.lock:
                        self.octopus_run = target
                    atomic_write_text(self.settings.output / "demo" / "octopus-current.json",
                                      json.dumps({"path": str(target)}))
                elif result == 0:
                    log("Demo forecast artifacts saved. Use demo mode to inspect them; local view keeps your configured run.\n")
        if action == "octopus-update":
            argv = ["wondrous-octopus/daily_update.py", "--run-dir", str(self.octopus_run)]
            # A synthetic neural smoke run stops at a fixed historical cursor.
            # The full update is still available for genuine local research runs.
            if self.settings.mode == "demo" and self.octopus_run:
                meta = json.loads((self.octopus_run / "run_meta.json").read_bytes())
                if meta.get("model_selection") == "octopus":
                    argv.extend(["--now", "2019-09-14"])
            results.append(self._command(argv, log, research=True, timeout=1800))
            if results[-1] == 0:
                results.append(self._command(["wondrous-octopus/run_walkforward.py", "--report-only", str(self.octopus_run)],
                                             log, research=True))
        if action == "octopus-estimate":
            results.append(self._command(["wondrous-octopus/run_walkforward.py", "--preset", "quick", "--dry-run"],
                                         log, research=True))
        if action == "test-signals":
            files = ["market_open", "morning", "midday", "intraday", "buy"]
            results.append(self._command(["-m", "pytest", "-q", *[
                f"scripts/tests/test_generate_{name}_candidates.py" for name in files]], log, demo=True, timeout=900))
        if action == "reports":
            results.append(self._command(["-m", "scripts.purchase_history", "--cron-tracking-root",
                                         str(self.settings.root), "--output-dir",
                                         str(self.settings.output / "reports" / job_id)], log))
        if action == "ticker-refresh-missing":
            results.append(self._command(["-m", "scripts.refresh_active_ticker_histories", "--force"], log, timeout=600))
        if action == "train-foundation-smoke":
            target = self.settings.output / "launches" / job_id
            results.append(self._command([
                "wondrous-octopus/train_foundation.py", "--limit-steps", "10", "--epochs", "1",
                "--stage", "1", "--device", "cpu", "--max-tickers", "5", "--run-dir", str(target),
            ], log, research=True, timeout=600))
        with self.lock:
            self.cache.clear()
        snapshot = self.snapshot()
        log("\n" + "\n".join(snapshot["daily"]["summary"]) + "\n")
        log(f"Daily summaries: {self.settings.output / 'summaries' / self.settings.mode}\n")
        return next((code for code in results if code != 0), 0)


class AuthFailureLog:
    """Write bounded access failures without letting clients grow stderr at line rate."""

    def __init__(self, interval: float = 5.0, clock=time.monotonic, stream=sys.stderr):
        self.interval = max(0.0, float(interval))
        self.clock = clock
        self.stream = stream
        self.lock = threading.Lock()
        self.last_emitted: float | None = None
        self.suppressed = 0

    @staticmethod
    def _field(value: object, fallback: str, limit: int) -> str:
        try:
            rendered = str(value)
        except Exception:
            rendered = fallback
        # Passwords may themselves contain non-ASCII or control characters.
        # Mask the unmodified spelling first so normalization cannot reveal its
        # printable remainder; do not register that remainder globally because
        # it may be one common character and corrupt every job log (panel,
        # round 3).
        try:
            rendered = redact(rendered)
        except Exception:
            rendered = fallback
        # Strip controls before redaction so a hostile target cannot inject a
        # second launchd.err.log line.  Redact before truncation because cutting
        # a secret at the boundary can leave an unrecognizable suffix (Same Page
        # round 2).
        printable = "".join(character for character in rendered if 0x20 <= ord(character) <= 0x7e)
        try:
            safe = redact(printable)
        except Exception:
            safe = fallback
        return (safe or fallback)[:limit]

    def _line(self, client_address: object, method: object, target: object, suppressed: int) -> str:
        try:
            # Parse only after exact raw-value and percent-decoded passes.
            # urlsplit discards controls and treats '?'/'#' as boundaries;
            # either transformation could otherwise expose most of an allowed
            # UTF-8 password before the route-only pass sees it (panel finding,
            # round 3).
            safe_target = redact(str(target))
            # Decode only after raw matches are gone: a literal '%' inside a
            # registered password must not mutate before it can be masked.
            safe_target = redact(unquote(safe_target))
            unparsed_route = safe_target.split("?", 1)[0].split("#", 1)[0]
            # Legitimate console routes are pure ASCII, so anything else has
            # no diagnostic value; stripping it can only expose a secret's
            # printable projection (Visionary review, Rock 1 fix round 2).
            if any(not 0x20 <= ord(character) <= 0x7e for character in unparsed_route):
                route = "non-ascii-target"
            else:
                route = urlsplit(safe_target).path
        except Exception:
            route = "malformed-target"
        if isinstance(client_address, tuple) and client_address:
            client_address = client_address[0]
        client = self._field(client_address, "unknown-client", 64)
        verb = self._field(method, "unknown-method", 32)
        route = self._field(route, "malformed-target", 128)
        skipped = f" suppressed={suppressed}" if suppressed else ""
        return f"Console auth failure: client={client} method={verb}{skipped} route={route}"

    def record(self, client_address: object, method: object, target: object) -> None:
        """Record one failure, reporting any suppressed burst on the next line."""
        try:
            now = self.clock()
            with self.lock:
                if self.last_emitted is not None and now - self.last_emitted < self.interval:
                    self.suppressed += 1
                    return
                suppressed = self.suppressed
                self.suppressed = 0
                self.last_emitted = now
                line = self._line(client_address, method, target, suppressed)
                self.stream.write(line + "\n")
                flush = getattr(self.stream, "flush", None)
                if callable(flush):
                    flush()
        except Exception:
            # Logging is deliberately best-effort: malformed request metadata,
            # an injected clock, or a closed stderr must never bypass a 401/403.
            return


def make_server(
    app: ConsoleApp,
    port: int,
    access: ConsoleAccess | None = None,
    failure_log: AuthFailureLog | None = None,
) -> ThreadingHTTPServer:
    """Serve fixed assets/APIs under one validated access policy."""
    access = ConsoleAccess() if access is None else access
    if not isinstance(access, ConsoleAccess):
        raise ValueError("access must be a validated ConsoleAccess policy")
    # The Environment page reports the listener's non-secret policy (bind,
    # trusted hosts, auth mode); the credential bytes never leave ConsoleAccess.
    app.access = access
    # This limiter belongs to one server, rather than the module, so separate
    # consoles and tests cannot suppress each other's diagnostics (panel,
    # round 3).
    failure_log = AuthFailureLog() if failure_log is None else failure_log
    active_failure_log: AuthFailureLog = failure_log

    class Handler(BaseHTTPRequestHandler):
        # StreamRequestHandler applies this while setting up every accepted
        # socket. An idle LAN/proxy peer therefore cannot retain a worker thread
        # forever without sending even a request line (Rock 2 handler finding).
        timeout = 5

        def _server_port(self) -> int:
            addr = getattr(self.server, "server_address", None)
            if isinstance(addr, tuple) and len(addr) > 1 and isinstance(addr[1], int):
                return addr[1]
            return 0

        def log_message(self, format: str, *args) -> None:
            return

        def version_string(self) -> str:
            # BaseHTTPRequestHandler otherwise exposes the Python version in
            # every Server header, which gives an unauthenticated probe needless
            # implementation detail (Rock 2 handler finding).
            return "DCA-Console"

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # The stdlib implementation interpolates malformed request syntax
            # and unsupported method names into an HTML body before do_GET or
            # do_POST can apply redact(). A fixed JSON error closes that
            # pre-handler credential echo path (Rock 2 hardened-handler review).
            self.close_connection = True
            self._reply(
                code,
                json.dumps({"error": "HTTP request rejected."}).encode(),
                extra_headers=(("Connection", "close"),),
            )

        def _reply(
            self,
            code: int,
            body: bytes,
            kind: str = "application/json",
            *,
            download: str = "",
            extra_headers: tuple[tuple[str, str], ...] = (),
            csp: str = DEFAULT_CSP,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # Only a generated research page served from its own route may
            # relax this, and then only into an opaque sandbox (dca_console_routes).
            self.send_header("Content-Security-Policy", csp)
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: dict) -> None:
            self._reply(code, json.dumps(value, allow_nan=False).encode())

        def _trusted(self) -> bool:
            port_number = self._server_port()
            if not access.trusted(self.headers, port_number):
                return False
            site_present, site = _lower_ascii_header(self.headers, "Sec-Fetch-Site")
            if site is not None:
                # Optional HTTP whitespace does not change a structured token,
                # so a padded value gets the same treatment as its canonical
                # spelling.  The allow-list on the next line is the load-bearing
                # fail-closed check; this strip only normalizes (Rock 2 review).
                site = site.strip(" \t")
            if site_present and site not in {"same-origin", "same-site", "cross-site", "none"}:
                return False
            if site != "cross-site":
                return True
            mode_present, fetch_mode = _lower_ascii_header(self.headers, "Sec-Fetch-Mode")
            destination_present, destination = _lower_ascii_header(self.headers, "Sec-Fetch-Dest")
            if fetch_mode is not None:
                fetch_mode = fetch_mode.strip(" \t")
            if destination is not None:
                destination = destination.strip(" \t")
            # Cloudflare Access can leave the first harmless document navigation
            # marked cross-site after its login redirect chain. The exception is
            # deliberately route/method/mode/destination exact; every API read
            # and every write remains rejected (Rock 2 contract).
            return (
                self.command == "GET"
                and self._request_path() == "/"
                and mode_present
                and fetch_mode == "navigate"
                and destination_present
                and destination == "document"
            )

        def _authority_diagnostics(self) -> dict[str, str | None]:
            return {
                "received_host": _reported_header(self.headers, "Host"),
                "forwarded_host": _reported_header(self.headers, "X-Forwarded-Host"),
            }

        def _log_auth_failure(self) -> None:
            try:
                active_failure_log.record(self.client_address, self.command, self.path)
            except Exception:
                # Test doubles get the same fail-safe boundary as the concrete
                # logger; an observability failure cannot become an auth bypass.
                pass

        def _forbidden(self, message: str) -> None:
            self._log_auth_failure()
            # Host values are useful precisely when a proxy was configured
            # incorrectly, but remain hostile input: redact before bounding and
            # emit JSON only (Same Page round 3).
            self._json(403, {"error": redact(message), **self._authority_diagnostics()})

        def _unauthorized(self) -> None:
            self._log_auth_failure()
            body = json.dumps({"error": "HTTP Basic authentication is required."}).encode()
            self._reply(401, body, extra_headers=((
                "WWW-Authenticate", 'Basic realm="DCA console", charset="UTF-8"',
            ),))

        def _request_path(self) -> str | None:
            try:
                return urlsplit(self.path).path
            except Exception:
                return None

        def do_GET(self) -> None:
            if not self._trusted():
                self._forbidden("This console accepts same-origin loopback requests only.")
                return
            route = self._request_path()
            # Health stays credential-free for local probes.  The trust check
            # remains first, so this exception never becomes a cross-origin
            # reachability oracle (Same Page round 1).
            if route not in CREDENTIAL_FREE_ROUTES and not access.authorized(self.headers):
                self._unauthorized()
                return
            try:
                # Malformed targets become 400; handler tracebacks grow launchd.err.log outside the limiter (panel, round 1).
                url = urlsplit(self.path)
                if url.path in ASSETS:
                    name, kind = ASSETS[url.path]
                    self._reply(200, (REPO / "console" / name).read_bytes(), kind)
                    return
                # Every API read lives in the route table; the gate order above
                # is the only thing this handler still owns (GUI expansion Phase 0).
                handler = GET_ROUTES.get(url.path)
                if handler is None:
                    self._json(404, {"error": "Not found"})
                    return
                response = handler(app, RouteRequest(url, self._authority_diagnostics(), self._server_port()))
                self._reply(response.code, response.body, response.kind,
                            download=response.download, csp=response.csp)
            except ValueError:
                self._json(400, {"error": "Invalid date or query. Use date=YYYY-MM-DD."})
            except Exception as error:
                self._json(500, {"error": redact(f"Could not read local artifacts: {type(error).__name__}: {error}")})

        def do_POST(self) -> None:
            # Preserve the deliberate oracle ordering: an untrusted peer sees
            # 403 before Basic is considered; a trusted peer must pass Basic
            # before the CSRF token can reveal anything (Same Page round 1).
            if not self._trusted():
                self._forbidden("Missing or invalid console token.")
                return
            if not access.authorized(self.headers):
                self._unauthorized()
                return
            if not secrets.compare_digest(self.headers.get("X-Console-Token", ""), app.token):
                self._forbidden("Missing or invalid console token.")
                return
            if self.path not in {"/api/jobs", "/api/jobs/cancel"}:
                self._json(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("Request body must be between 1 and 1024 bytes")
                # Retain explicit timeout before reading payload to protect against body-trickling DoS attacks.
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(length))
                if self.path == "/api/jobs/cancel":
                    # The cancel body is as closed as the start body: one string
                    # naming the job the page is looking at (GUI expansion Phase 5).
                    if not isinstance(payload, dict) or set(payload) != {"job"} or not isinstance(payload["job"], str):
                        raise ValueError("Supply only a string job id")
                    if app.jobs.cancel(payload["job"]):
                        self._json(202, {"cancelled": payload["job"]})
                    else:
                        self._json(404, {"error": "No active job with that id"})
                    return
                if not isinstance(payload, dict) or set(payload) != {"action"} or not isinstance(payload["action"], str):
                    raise ValueError("Supply only a string action")
                self._json(202, {"job": app.submit(payload["action"])})
            except JobBusy as error:
                self._json(409, {"error": redact(str(error))})
            except (ValueError, TimeoutError) as error:
                self._json(400, {"error": redact(str(error))})
            except OSError as error:
                self._json(500, {"error": redact(str(error))})

    return ThreadingHTTPServer((access.bind, port), Handler)


def startup_banner(access: ConsoleAccess, port: int) -> str:
    """Render the non-secret network policy shown before serving requests."""
    trusted_hosts = ", ".join(access.trusted_hosts) if access.trusted_hosts else "(none)"
    authentication = f"basic user={access.username}" if access.credential is not None else "none"
    lines = [
        "DCA console:",
        f"  bind: {access.bind}",
        f"  port: {port}",
        f"  trusted hosts: {trusted_hosts}",
        f"  auth: {authentication}",
    ]
    # Redact the completed dynamic section, not only individual fields: a
    # credential can equal a username/host or even span joined trusted-host
    # values. Keep the required fixed warning outside the generic key/value
    # sanitizer, which otherwise interprets its prose use of "token" as a
    # credential assignment (Rock 2 review).
    banner = redact("\n".join(lines))
    if not ipaddress.IPv4Address(access.bind).is_loopback:
        # A valid credential makes a reachable socket intentional, not private:
        # neither Basic nor the CSRF token is encrypted on this listener.
        banner += "\n  WARNING: plain HTTP: the Basic credential and console token are visible to anyone on this network"
    return banner


def main(argv: list[str] | None = None) -> int:
    """Launch the browser UI, run a single action, or produce one daily snapshot."""
    parser = argparse.ArgumentParser(
        description="Local DCA console. Default: observe real local artifacts read-only and open the browser; "
                     "use --demo for an isolated synthetic sandbox."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use isolated synthetic demo artifacts (opt-in, not the default)")
    mode.add_argument("--local", action="store_true",
                       help="Observe existing artifacts; no automatic demo or broker calls (default when no mode flag is given)")
    parser.add_argument("--root", type=Path, help="Existing DCA artifact root (implies --local)")
    parser.add_argument("--secondary-root", type=Path, action="append", default=[], metavar="DIR",
                        help="Additional existing artifact tree to observe read-only (repeatable; implies --local)")
    parser.add_argument("--output-dir", type=Path, help="Console artifacts; default dca_paths.stream_root('console')")
    parser.add_argument("--octopus-run", type=Path, help="Existing Octopus run directory (use with --local)")
    parser.add_argument("--research-python", help="Python with Octopus dependencies; auto-detect repository .venv")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="Day to inspect; local mode resolves the date per request unless pinned, --demo defaults to "
                             "the previous market session")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port; use 0 for an available port")
    parser.add_argument("--bind", default="127.0.0.1", help="IPv4 address to listen on (default: 127.0.0.1)")
    parser.add_argument("--trusted-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="Additional accepted Host authority; repeat for more names")
    parser.add_argument("--behind-proxy", action="store_true",
                        help="Honor a configured X-Forwarded-Host on a loopback listener")
    parser.add_argument("--auth-file", type=Path, help="POSIX owner-only file containing one user:password line")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--once", action="store_true",
                        help="Save summaries and print JSON once, then exit (runs demo-all first only with --demo)")
    parser.add_argument("--run", metavar="ACTION", help="Run one allowlisted action, print its result and exit")
    parser.add_argument("--check", action="store_true", help="Show runtime and action readiness without starting jobs")
    args = parser.parse_args(argv)
    if args.demo and (args.root or args.octopus_run or args.secondary_root):
        parser.error("Use --local with existing --root/--octopus-run/--secondary-root; --demo keeps synthetic data isolated.")
    secondary_roots = []
    for extra in args.secondary_root:
        resolved = extra.expanduser().resolve()
        if not resolved.is_dir():
            # A missing secondary tree is a typo, not an empty history: refuse
            # rather than silently observe nothing (GUI expansion Phase 0).
            parser.error(f"--secondary-root must name an existing directory: {extra}")
        if resolved not in secondary_roots:
            secondary_roots.append(resolved)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        if args.auth_file:
            username, credential = load_auth_file(args.auth_file)
        else:
            username, credential = "", None
        access = ConsoleAccess(
            username=username,
            credential=credential,
            bind=args.bind,
            trusted_hosts=tuple(args.trusted_host),
            behind_proxy=args.behind_proxy,
        )
    except ValueError as error:
        # Keep every construction path fail-closed, including --check/--once.
        # parser.error provides the CLI's established exit-2 contract without
        # ever printing file contents (Same Page round 1).
        parser.error(str(error))
    # Real local artifacts are the default (--demo is already mutually exclusive
    # with --local/--root/--octopus-run/--secondary-root, enforced above), so
    # observing them is simply "not an explicit demo" (Sep 2026 default flip:
    # the reporting console must show real data unless a synthetic sandbox is
    # explicitly requested).
    local = not args.demo
    root = (args.root or Path(cron_tracking_root())).expanduser().resolve()
    output = (args.output_dir or Path(stream_root("console"))).expanduser().resolve()
    venv_python = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = str(venv_python) if venv_python.is_file() else sys.executable
    if local:
        # None is intentional here: an unpinned local service resolves its date
        # per request instead of freezing startup's calendar day (Same Page
        # round 2). An explicit --date retains the established pinned behavior.
        selected_day = args.date
    else:
        try:
            # Demo mode always pins a full market session because its pipeline
            # cannot safely use an arbitrary calendar day.
            selected_day = args.date or default_demo_day()
        except (RuntimeError, ValueError) as error:
            parser.error(f"Cannot select a demo session: {error}. Supply --date YYYY-MM-DD.")
    settings = Settings("local" if local else "demo", root if local else output / "demo" / "dca",
                        output, selected_day,
                        python, args.research_python or python,
                        args.octopus_run.expanduser().resolve() if args.octopus_run else None,
                        secondary_roots=tuple(secondary_roots))
    app = ConsoleApp(settings)
    if args.check:
        print(json.dumps({"mode": settings.mode, "python": python, "research_python": settings.research_python,
                          "packages": app.packages, "actions": app.actions(), "modules": module_catalog(python)}, indent=2))
        return 0
    if args.run or args.once:
        try:
            if args.run or not local:
                app.submit(args.run or "demo-all")
                assert app.jobs.worker is not None
                app.jobs.worker.join()
            print(json.dumps(app.snapshot(), indent=2, allow_nan=False))
            return app.jobs.snapshot()[0]["returncode"] if (args.run or not local) else 0
        except (OSError, ValueError, JobBusy) as error:
            print(redact(str(error)), file=sys.stderr)
            return 1
        finally:
            app.jobs.close()
    # Install SIGTERM before constructing the server and before exposing the
    # flushed readiness line. A supervisor may stop the process as soon as that
    # line appears, and must still get the Ctrl+C cleanup path (Same Page round 2).
    with _sigterm_as_keyboard_interrupt():
        server = None
        updater = None
        try:
            try:
                server = make_server(app, args.port, access)
            except OSError as error:
                print(
                    redact(f"Cannot bind {access.bind} on HTTP port {args.port}: {error}. Try --port 0."),
                    file=sys.stderr,
                )
                return 1
            actual_port = server.server_address[1]
            browser_host = access.bind if access.bind != "0.0.0.0" else "127.0.0.1"
            url = f"http://{browser_host}:{actual_port}"
            # Output paths are operator-controlled and launchd captures this
            # stream, so give the non-network banner details the same complete
            # redaction boundary. Retain the original readiness phrase because
            # operators and the SIGTERM regression synchronize on it.
            details = redact(
                f"DCA console ({settings.mode}): {url}\n"
                f"Daily summaries: {output / 'summaries' / settings.mode}\nCtrl+C stops the console."
            )
            print(f"{startup_banner(access, actual_port)}\n{details}", flush=True)

            def refresh() -> None:
                while not app.jobs.stop.is_set():
                    try:
                        # Continue collecting today's summary across midnight without needing an open browser.
                        app.snapshot(datetime.now(ET).date() if local else settings.day)
                    except Exception as error:
                        print(redact(f"Summary refresh failed: {error}"), file=sys.stderr)
                    app.jobs.stop.wait(15)

            updater = threading.Thread(target=refresh, daemon=True)
            updater.start()
            if not local:
                app.submit("demo-all")
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\nStopping local console.", flush=True)
        finally:
            app.jobs.close()
            if server is not None:
                server.server_close()
            # A SIGTERM that lands between creating and starting the refresh
            # thread would otherwise turn the clean stop into a RuntimeError
            # from join() on a never-started thread (Rock 3 review, unverified
            # race closed by the Visionary).
            if updater is not None and updater.is_alive():
                updater.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
