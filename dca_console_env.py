"""Readiness and provenance for the console's Environment page (GUI expansion Phase 5).

Everything here answers "what is this console running on and reading from",
never "what is inside": environment variables are reported by NAME with a
set/unset flag (a sensitive value is never read into the response), roots are
labelled rather than located, the Google Drive ticker archive is inspected
with ``lstat`` only so a stalled mount cannot hold a request thread, and the
repository's engineering gates are listed as copyable commands rather than
run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dca_console_jobs import is_sensitive_variable
from dca_console_reader import BoundedReader, root_label, roots_from_settings
from dca_console_release import CONSOLE_VERSION


@dataclass(frozen=True)
class EnvVar:
    name: str
    purpose: str
    sensitive: bool = False


# Every variable the core modules and scripts read (grep 'environ' across the
# checkout, 2026-09-17), with the purpose the reader needs to judge a gap. A
# hand-curated tuple so a new variable is a deliberate one-line addition.
ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "The pinned brokerage account; required for live runs, never read by this console", True),
    EnvVar("DCA_CRON_TRACKING_ROOT", "Root of the per-stream artifact tree (dca_paths.cron_tracking_root), read at import time"),
    EnvVar("DCA_CONFIG_DIR", "Where config/*.json is loaded from (dca_config.get_config_dir)"),
    EnvVar("DCA_ATTEMPT_LOG_ROOT", "Failed-start marker tree; read at call time by dca_alerts"),
    EnvVar("DCA_INTRADAY_LIVE_STATE_ROOT", "Replaces (not extends) the intraday recovery/prior-allocation search root"),
    EnvVar("DCA_MARKET_OPEN_LIVE_STATE_ROOT", "Replaces (not extends) the market-open recovery/prior-allocation search root"),
    EnvVar("DCA_TICKER_HISTORY_DIR", "The daily OHLCV ticker archive (dca_ticker_paths.configured_ticker_history_dir)"),
    EnvVar("DCA_INTRADAY_ARCHIVE_DIR", "The per-poll intraday observation archive (dca_intraday_archive)"),
    EnvVar("DCA_VIZ_DIR", "Alternate viewer asset directory for the 3D market graph"),
    EnvVar("DCA_COMBINED_CSV", "Alternate combined OHLCV CSV for the market graph's csv source"),
    EnvVar("DCA_TICKER_DAILY_UPDATE_SCRIPT", "Override for the resumable ticker-history updater script"),
    EnvVar("DCA_BREADTH_FETCH_PYTHON", "Interpreter used for the ticker-market-breadth fetch"),
    EnvVar("DCA_ALERT_EMAIL_ADDRESS", "Operator address for the Gate 9 email channel (config/notifications.json)"),
    EnvVar("DCA_DRIFT_SLACK_WEBHOOK", "Slack webhook for scripts/drift_detection.py --slack", True),
    EnvVar("DCA_ALERT_SLACK_WEBHOOK", "Slack webhook for dca_alerts failure alerts", True),
    EnvVar("DCA_HEARTBEAT_URL", "Dead-man heartbeat URL prefix (per-stream suffixes in dca_alerts)", True),
    EnvVar("NOTION_TOKEN", "Notion connector token used by external report sessions, not by this repo", True),
    EnvVar("NEWS_API_KEY", "ticker-advisor sentiment source", True),
    EnvVar("SMTP_PASSWORD", "trading-strategies sentiment monitor mail send", True),
    EnvVar("PIPEDREAM_TOKEN", "trading-strategies monitor delivery", True),
)

@dataclass(frozen=True)
class RepositoryGate:
    name: str
    command: str
    description: str = ""
    badge: str = ""
    action_id: str | None = None


# Copyable gate commands; the console never runs these on a page view.
REPOSITORY_GATES: tuple[RepositoryGate, ...] = (
    RepositoryGate("Lint (core)", "ruff check .",
                   "Fast Python linter checking PEP 8 style, import ordering, and bug patterns.", "Quality"),
    RepositoryGate("Tests (core, ~5 min)", ".venv/bin/python -m pytest",
                   "Full pytest suite across core DCA modules, runners, and risk validation.", "CI Gate"),
    RepositoryGate("Coverage gate", ".venv/bin/python -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90",
                   "Enforces a strict 90%+ test coverage threshold across core modules before merging.", "CI Gate"),
    RepositoryGate("Config invariants", ".venv/bin/python -m pytest tests/test_config_invariants.py -q",
                   "Validates JSON schemas, budget caps, date bounds, and guards against unredacted account numbers.", "Security", "config-invariants"),
    RepositoryGate("Console suites", ".venv/bin/python -m pytest tests/test_dca_console*.py tests/test_console_*.py -q",
                   "Verifies console HTTP and reader routes, authentication policies, and web UI assets.", "Console"),
    RepositoryGate("Scripts suite", ".venv/bin/python -m pytest scripts/tests -q",
                   "Validates candidate generators, run classification helpers, and recovery tools.", "Scripts"),
    RepositoryGate("Dry-run smoke test", "python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo",
                   "Simulates near-close DCA pipeline run with synthetic market data.", "Smoke Test", "demo-dca"),
    RepositoryGate("Dependency audit", "pip-audit --requirement requirements-dev.txt --strict",
                   "Scans environment dependencies against the PyPA vulnerability database for known CVEs.", "Audit"),
    RepositoryGate("Readiness", "python3 from.py --check",
                   "Pre-flight health check verifying interpreters, packages, and model readiness.", "Readiness"),
)

# Root-level residue the repo map already flags; presence only, never content.
STRAY_ROOT_FILES = (
    ("midday_run.log", "Untracked single-line residue; nothing in the repo writes it"),
    ("signal_generation.log", "Untracked residue naming a module that does not exist"),
    ("run_market_open_signals.py", "9:35 signal reproduction harness; docs/repo_map.md 3.2 says the authoritative copy moved to claude/_agent_scratch/"),
    ("build_raw_data.py", "Companion of the 9:35 reproduction harness"),
    ("snap.py", "Companion of the 9:35 reproduction harness"),
)


def _display_interpreter(python: str) -> str:
    from dca_console import display_python

    return display_python(python)


def environment_variables() -> list[dict]:
    """Report each known variable's presence by name; values are never read into the response."""
    rows = []
    for variable in ENV_VARS:
        rows.append({
            "name": variable.name,
            "set": variable.name in os.environ and bool(os.environ.get(variable.name)),
            "sensitive": variable.sensitive or is_sensitive_variable(variable.name),
            "purpose": variable.purpose,
        })
    known = {variable.name for variable in ENV_VARS}
    extra = sorted(name for name in os.environ if name.startswith("DCA_") and name not in known)
    for name in extra:
        rows.append({"name": name, "set": True, "sensitive": is_sensitive_variable(name),
                     "purpose": "Set in this environment but not in the console's known list"})
    return rows


def _probe_root(name: str, path: Path | None, *, follow: bool, note: str = "") -> dict[str, Any]:
    """Describe one root by label; ``follow=False`` uses lstat only (Google Drive mounts)."""
    if path is None:
        return {"name": name, "label": None, "present": None, "symlink": None, "note": note or "not configured"}
    row: dict[str, Any] = {"name": name, "label": root_label(path), "present": None, "symlink": None, "note": note}
    try:
        info = os.lstat(path)
        row["symlink"] = os.path.islink(path)
        row["present"] = True if not row["symlink"] or not follow else os.path.exists(path)
        if not row["symlink"] and follow:
            row["present"] = True
        row["directory"] = bool(info.st_mode & 0o040000) or row["symlink"]
    except OSError:
        row["present"] = False
    return row


def roots_and_mounts(settings) -> list[dict]:
    """Label the trees this console reads; the ticker archive is inspected with lstat only."""
    rows = [
        _probe_root(f"{spec.kind} artifact root", spec.path, follow=True) for spec in roots_from_settings(settings)
    ]
    rows.append(_probe_root("console output", settings.output, follow=True))
    rows.append(_probe_root("config directory", settings.config_dir, follow=True))
    rows.append(_probe_root("octopus run", settings.octopus_run, follow=True, note="from --octopus-run"))
    archive = os.environ.get("DCA_TICKER_HISTORY_DIR")
    from dca_console import REPO

    archive_path = Path(archive).expanduser() if archive else REPO / "ticker_data"
    rows.append(_probe_root("ticker archive", archive_path, follow=False,
                            note="Google Drive symlink on this machine; inspected with lstat only, never scanned on a page view"))
    return rows


def access_policy(access, port: int | None) -> dict:
    """The listener's non-secret network policy, as startup_banner shows it."""
    if access is None:
        return {"bind": None, "port": port, "trusted_hosts": [], "auth": "unknown", "behind_proxy": None}
    return {
        "bind": access.bind,
        "port": port,
        "trusted_hosts": list(access.trusted_hosts),
        "auth": f"basic user={access.username}" if access.credential is not None else "none",
        "behind_proxy": access.behind_proxy,
        "plaintext_warning": access.bind != "127.0.0.1",
    }


def stray_root_files(repo: Path) -> list[dict]:
    reader = BoundedReader(repo, [])
    rows = []
    for name, note in STRAY_ROOT_FILES:
        path = reader.confined(name)
        present = path is not None and path.is_file()
        size = path.stat().st_size if (path is not None and present) else None
        rows.append({"name": name, "present": present, "size": size, "note": note})
    for entry in reader.entries("", limit=500):
        if entry.suffix == ".log" and entry.name not in {name for name, _ in STRAY_ROOT_FILES}:
            rows.append({"name": entry.name, "present": True, "size": (repo / entry).stat().st_size,
                         "note": "Root-level log file"})
    return rows


def build_env_snapshot(app, repo: Path, *, port: int | None = None) -> dict:
    """Compose the Environment page from the console's own state and cheap probes."""
    settings = app.settings
    warnings: list[str] = []
    # Opening/refreshing the Environment page re-runs the package probes.
    if hasattr(app, "reprobe_packages"):
        app.reprobe_packages()
    packages = dict(app.packages)
    dev_packages = dict(app.dev_packages)
    for label, probe in (("research", packages), ("console", dev_packages)):
        if isinstance(probe.get("error"), str):
            warnings.append(f"The {label} interpreter probe failed: {probe['error']}.")
    return {
        "session": {
            "mode": settings.mode,
            "channel": getattr(settings, "channel", "stable"),
            "version": CONSOLE_VERSION,
            "day": settings.day.isoformat() if settings.day else None,
            "day_rule": "pinned" if settings.day else "rolls daily (unpinned local mode)",
            "python": _display_interpreter(settings.python),
            "research_python": _display_interpreter(settings.research_python),
            "octopus_run": root_label(settings.octopus_run) if settings.octopus_run else None,
            "config_dir": root_label(settings.config_dir),
            "roots": [{"label": spec.label, "kind": spec.kind} for spec in roots_from_settings(settings)],
        },
        "interpreters": [
            {"name": "console", "python": _display_interpreter(settings.python), "packages": dev_packages},
            {"name": "research", "python": _display_interpreter(settings.research_python), "packages": packages},
        ],
        "env": environment_variables(),
        "roots": roots_and_mounts(settings),
        "access": access_policy(getattr(app, "access", None), port),
        "gates": [
            {"name": g.name, "command": g.command, "description": g.description, "badge": g.badge, "action_id": g.action_id}
            for g in REPOSITORY_GATES
        ],
        "stray_files": stray_root_files(repo),
        "warnings": warnings,
    }
