#!/usr/bin/env python3
"""Run the configured resumable ticker-history updater to completion or a safe stop.

This is the program-facing implementation of the project Claude skill
``tickers-daily-update``.  It runs the maintained external updater in bounded
passes, records a concise archive log, and never invents a fallback downloader
when that updater is missing.

After the per-ticker update, this also runs ticker-market-breadth/fetch_tick.py
(NYSE TICK breadth index, data only -- see that subproject's own README) as a
best-effort SUBPROCESS step, so the same daily-update command that refreshes
every per-ticker CSV also keeps the breadth archive current. It is invoked out
of process, never imported, so this module's own runtime stays standard-
library only even though fetch_tick.py depends on yfinance/pandas (CLAUDE.md:
"Standard library only at runtime"). A failure there (yfinance not installed,
network error, ...) is recorded in the summary but never fails the whole
ticker-history-update run -- it is independent, non-tradable reference data,
not part of the per-ticker archive contract this module exists to maintain.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from dca_ticker_paths import configured_ticker_history_dir
from scripts.file_lock import cross_process_lock

_UPDATED_RE = re.compile(r"DONE_THIS_RUN\s+updated=(\d+)\s+remaining=(\d+)")

#: Repo root, resolved the same way ticker-advisor/pricing.py and other
#: sibling subprojects do (this file's own parent's parent), so the default
#: breadth-fetch script location below works from a source checkout without
#: depending on the current working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Default location of the breadth-index fetch script. Not shipped in the
#: installed wheel (ticker-market-breadth/ is a self-contained, non-packaged
#: sibling subproject -- see pyproject.toml), so an installed deployment
#: simply won't find it here and _fetch_breadth_series() degrades to
#: "skipped" rather than failing.
DEFAULT_BREADTH_FETCH_SCRIPT = _REPO_ROOT / "ticker-market-breadth" / "fetch_tick.py"

#: A single yfinance download of ^TICK's full history is much lighter than
#: the per-ticker updater's whole-universe loop above; this is a much
#: shorter ceiling than DEFAULT_INVOCATION_TIMEOUT_SECONDS on purpose.
DEFAULT_BREADTH_FETCH_TIMEOUT_SECONDS = 120.0


def _append_log(history_dir: Path, line: str) -> None:
    """Append a durable, serialized one-line archive-update record."""
    log_path = history_dir / "_daily_update_log.txt"
    with cross_process_lock(log_path.with_suffix(".lock")):
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        atomic_write_text(log_path, existing + line + "\n")


def _history_mtimes(history_dir: Path) -> dict[str, int]:
    """Snapshot CSV modification times so an updated ticker can notify consumers."""
    return {
        path.stem.upper(): path.stat().st_mtime_ns
        for path in history_dir.glob("*.csv")
        if path.is_file()
    }


def _active_tickers(ticker_config: Path) -> set[str]:
    """Read the calibrated DCA symbols without making the archive depend on DCA code.

    Includes ``whole_share`` alongside ``active``: generate_per_ticker_sizing.py's
    build_sizing_payload() calibrates that SAME active-plus-whole-share universe
    (a whole-share ticker like BYDDY is evaluated by the identical graded ladder --
    see docs/whole_share_accumulation.md), but this function feeds
    changed_active_tickers, which gates whether a history-update event even
    triggers daily_per_ticker_calibration.py's rebuild at all. Without this, a
    BYDDY-only history update event could never trigger recalibration, and
    scripts/refresh_active_ticker_histories.py's active_symbols() (this
    function's own sibling for the OTHER maintenance path) would never even
    fetch its missing history in the first place -- leaving whole-share sizing
    permanently on the $1 fallback with no documented manual path to fix it
    (Codex review: "Include whole-share symbols in history-update events").
    """
    data = json.loads(ticker_config.read_bytes())
    active = data.get("active")
    if not isinstance(active, list) or not all(isinstance(ticker, str) for ticker in active):
        raise ValueError(f"{ticker_config} must contain an active ticker list")
    whole_share = data.get("whole_share", [])
    if not isinstance(whole_share, list) or not all(isinstance(ticker, str) for ticker in whole_share):
        raise ValueError(f"{ticker_config}'s whole_share list, if present, must be a list of strings")
    return {ticker.upper() for ticker in active} | {ticker.upper() for ticker in whole_share}


_universe_tickers = _active_tickers


#: Default ceiling on one updater subprocess invocation. The external updater
#: fetches from Yahoo per ticker across the whole active universe; a hung
#: network request must not be able to wedge the nightly task forever.
DEFAULT_INVOCATION_TIMEOUT_SECONDS = 600.0


def _default_breadth_fetch_python(breadth_fetch_script: Path) -> str:
    """Best-effort interpreter selection for the breadth-fetch subprocess.

    `ticker-market-breadth/README.md`'s own documented install path is a
    SEPARATE venv (`ticker-market-breadth/.venv`) holding yfinance/pandas --
    neither of which belongs in this module's own stdlib-only-at-runtime
    environment (CLAUDE.md). Launching the subprocess with `sys.executable`
    (this process's own interpreter) therefore runs it in the WRONG
    environment for the common deployment that followed that README,
    usually producing a `ModuleNotFoundError` "failed" outcome instead of
    actually refreshing the archive (Codex review: "Launch the breadth
    fetch from its dependency environment").

    Resolution order: $DCA_BREADTH_FETCH_PYTHON (checked HERE, inside the
    shared default-resolution function, not only as an argparse default in
    this module's own main() -- scripts/run_ticker_maintenance.py's
    run_update() is a second, equally-canonical entry point into
    run_incremental_update() that never threads a --breadth-fetch-python
    CLI flag through at all, so an env-var check confined to argparse would
    silently never apply there (Codex review: "Honor the interpreter
    override in every entry point")); then the documented venv next to the
    script, if present; then `sys.executable` unchanged (e.g. a deployment
    that installed yfinance/pandas directly into this process's own
    environment instead).
    """
    from_env = os.environ.get("DCA_BREADTH_FETCH_PYTHON")
    if from_env:
        return from_env
    subproject_dir = breadth_fetch_script.parent
    for candidate in (subproject_dir / ".venv" / "bin" / "python", subproject_dir / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _fetch_breadth_series(
    *,
    history_dir: Path,
    breadth_fetch_script: Path,
    timeout_seconds: float,
    breadth_fetch_python: str | None = None,
) -> dict[str, Any]:
    """Best-effort run of the NYSE TICK breadth-index fetch as a subprocess.

    Never raises -- a missing script, a missing history_dir (re-checked
    here; see below), an invalid timeout, a missing yfinance/pandas install
    in whatever environment actually runs the subprocess, a network
    failure, or a timeout are all recorded in the returned dict as
    "skipped"/"failed" rather than propagated, so this optional enrichment
    step can never fail the ticker-history-update run it's attached to (see
    module docstring).

    `breadth_fetch_python` overrides the interpreter used to launch the
    subprocess; None (the default) resolves via
    _default_breadth_fetch_python(), which prefers the subproject's own
    documented venv over this process's own interpreter (see that
    function's docstring).
    """
    if not breadth_fetch_script.is_file():
        return {"status": "skipped", "detail": f"{breadth_fetch_script} not present"}
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        # argparse's type=float accepts "nan"/"inf" without complaint, and
        # subprocess.run(timeout=...) raises ValueError/OverflowError for
        # those rather than the caught TimeoutExpired below -- escaping
        # this function (and this optional step's "never raises" contract)
        # AFTER the primary per-ticker update has already completed, which
        # would abort run_incremental_update() before it ever writes its
        # own log/event summary (Codex review: "Reject non-finite
        # breadth-fetch timeouts").
        return {"status": "failed", "detail": f"invalid timeout_seconds: {timeout_seconds!r}"}
    if not history_dir.is_dir():
        # Re-checked HERE, right before the breadth step, not just once at
        # run_incremental_update()'s own entry: the per-ticker update loop
        # above can run for a long time (up to max_invocations *
        # invocation_timeout_seconds), and this daily-update wrapper always
        # passes an explicit --history-dir to fetch_tick.py below --
        # including when history_dir originated from
        # configured_ticker_history_dir()'s own default. That makes
        # fetch_tick.py's OWN "fail closed on a missing default archive"
        # guard a no-op here, since from its perspective every call looks
        # like an explicit override: if the managed ticker_data/ Drive
        # mount disappears partway through the per-ticker loop, this step
        # would otherwise still run afterward and silently materialize a
        # bogus local ticker_data/_breadth/ stand-in (Codex review:
        # "Preserve the default archive guard in automatic refreshes").
        return {"status": "skipped", "detail": f"{history_dir} does not exist"}
    # Resolved to an absolute path BEFORE either is used for `cwd`/argv --
    # two independent relative-path traps, both because the subprocess
    # below runs with its cwd set to the script's own parent directory:
    #   * a still-relative --breadth-fetch-script override (e.g.
    #     "ticker-market-breadth/fetch_tick.py", valid from the caller's own
    #     cwd) would have Python look for it a SECOND time under that new
    #     cwd (Codex review: "Resolve the overridden breadth script before
    #     changing cwd");
    #   * a still-relative history_dir (e.g. a relative
    #     DCA_TICKER_HISTORY_DIR, or a relative --history-dir) would get
    #     reinterpreted relative to that same new cwd too, silently writing
    #     to (and reporting success for) a bogus nested directory under the
    #     script's own location instead of the intended archive (Codex
    #     review: "Resolve the history directory before changing cwd").
    # Both mirror the per-ticker updater loop's identical resolved_script
    # fix above.
    #   * a still-relative --breadth-fetch-python/DCA_BREADTH_FETCH_PYTHON
    #     override that contains a path separator (e.g. "venv/bin/python",
    #     valid from the caller's own cwd) would get reinterpreted relative
    #     to resolved_script.parent below for the exact same reason -- a
    #     bare command name like "python" is deliberately left alone since
    #     it is meant to be resolved via PATH, not as a path (Codex review:
    #     "Resolve relative interpreter overrides before changing cwd").
    resolved_script = breadth_fetch_script.resolve()
    resolved_history_dir = history_dir.resolve()
    python = (
        _default_breadth_fetch_python(resolved_script)
        if breadth_fetch_python is None
        else breadth_fetch_python
    )
    if os.sep in python or (os.altsep and os.altsep in python):
        python = str(Path(python).resolve())
    guarded_argv = [
        python,
        str(resolved_script),
        "--history-dir",
        str(resolved_history_dir),
        "--require-existing-history-dir",
    ]
    try:
        result = subprocess.run(  # nosec B603 - explicit Python executable and repo-local script
            guarded_argv,
            cwd=resolved_script.parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if _is_unsupported_guard_flag_error(result):
            # --breadth-fetch-script overrode the bundled script with an
            # older or custom fetcher that predates
            # --require-existing-history-dir: argparse rejects the
            # unrecognized flag with exit code 2 on every invocation, which
            # would otherwise report every breadth refresh as "failed"
            # forever. Fall back to the pre-flag invocation instead, and say
            # so in the detail so the archive-disappearance guard's absence
            # is visible rather than silent (Codex review: "Preserve
            # compatibility with overridden breadth scripts").
            result = subprocess.run(  # nosec B603 - explicit Python executable and repo-local script
                guarded_argv[:-1],
                cwd=resolved_script.parent,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            if result.returncode != 0:
                detail = output.strip().splitlines()[-1] if output.strip() else "no process output"
                return {"status": "failed", "detail": f"exited {result.returncode}: {detail}"}
            detail = output.strip().splitlines()[-1] if output.strip() else ""
            return {
                "status": "completed",
                "detail": (
                    f"{detail} (ran without --require-existing-history-dir: {resolved_script} "
                    "does not support it -- upgrade it to keep the archive-disappearance guard "
                    "active)"
                ).strip(),
            }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": f"exceeded the {timeout_seconds:.0f}s timeout"}
    except OSError as exc:
        # A missing/non-executable interpreter (a --breadth-fetch-python /
        # DCA_BREADTH_FETCH_PYTHON configuration typo, or a stale venv path)
        # makes subprocess.run() itself raise FileNotFoundError/PermissionError
        # before any subprocess ever starts -- neither is a
        # subprocess.TimeoutExpired, so it would otherwise escape this
        # function (running AFTER the primary per-ticker update has already
        # completed) and abort run_incremental_update() before its own
        # log/event summary is written (Codex review: "Convert interpreter
        # launch errors into breadth failures").
        return {"status": "failed", "detail": f"could not launch {python!r}: {exc}"}
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        detail = output.strip().splitlines()[-1] if output.strip() else "no process output"
        return {"status": "failed", "detail": f"exited {result.returncode}: {detail}"}
    return {"status": "completed", "detail": output.strip().splitlines()[-1] if output.strip() else ""}


def _is_unsupported_guard_flag_error(result: subprocess.CompletedProcess) -> bool:
    """True when `result` is argparse rejecting an unrecognized `--require-existing-history-dir`.

    Narrow on purpose: only exit code 2 (argparse's own usage-error code)
    with both the flag name and "unrecognized argument" present in stderr
    counts, so a --require-existing-history-dir-aware script that fails for
    an unrelated reason (e.g. a real missing-archive FileNotFoundError,
    which also exits non-zero) is still reported as "failed" rather than
    silently retried without the guard.
    """
    if result.returncode != 2:
        return False
    stderr = result.stderr or ""
    return "--require-existing-history-dir" in stderr and "unrecognized argument" in stderr


def run_incremental_update(
    *,
    updater_script: Path,
    history_dir: Path,
    max_invocations: int = 40,
    ticker_config: Path | None = None,
    artifacts_dir: Path | None = None,
    now: datetime | None = None,
    invocation_timeout_seconds: float = DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    fetch_breadth_series: bool = True,
    breadth_fetch_script: Path | None = None,
    breadth_fetch_timeout_seconds: float = DEFAULT_BREADTH_FETCH_TIMEOUT_SECONDS,
    breadth_fetch_python: str | None = None,
) -> dict[str, Any]:
    """Run the updater until completion, failure, or repeated no-progress.

    The external script remains the sole owner of Yahoo fetch/merge semantics.
    This wrapper only recognizes its documented progress lines and preserves a
    short run summary for analysis.

    ``fetch_breadth_series`` (default True) additionally runs the NYSE TICK
    breadth-index fetch (ticker-market-breadth/fetch_tick.py) as a best-effort
    subprocess after the per-ticker update, so the same daily-update command
    also keeps that archive current -- see the module docstring for why this
    is a subprocess call rather than an import. Its outcome is recorded under
    the returned summary's "breadth_fetch" key and never raises or changes
    this function's own "status"/return code. ``breadth_fetch_python``
    overrides which interpreter launches that subprocess; None (the
    default) auto-detects the subproject's own documented venv (see
    _default_breadth_fetch_python()) rather than assuming this process's
    own interpreter has yfinance/pandas installed.
    """
    if not updater_script.is_file():
        raise FileNotFoundError(f"configured ticker updater does not exist: {updater_script}")
    if not history_dir.is_dir():
        raise FileNotFoundError(
            f"ticker history directory does not exist: {history_dir} -- set "
            "DCA_TICKER_HISTORY_DIR to the shared archive location (required "
            "when this package is not run from a repository checkout; see "
            "dca_ticker_paths.configured_ticker_history_dir)"
        )
    if max_invocations < 1:
        raise ValueError("max_invocations must be at least one")

    started_at = now or datetime.now().astimezone()
    before_mtimes = _history_mtimes(history_dir)
    # Resolved to an absolute path once, up front: a manifest-supplied
    # relative path (e.g. "scripts/update.py") resolves against the CURRENT
    # process's cwd here, but the subprocess below runs with its cwd set to
    # the script's own parent directory. Passing the still-relative path as
    # the subprocess argv would then have Python look for it a second time
    # under that parent (e.g. "scripts/scripts/update.py"), silently failing
    # every invocation (Codex review: "Resolve the updater path before
    # switching directories").
    resolved_script = updater_script.resolve()
    updated_total = 0
    remaining: int | None = None
    stagnant_runs = 0
    completed = False
    errors: list[str] = []
    updater_env = os.environ.copy()
    updater_env["TICKERS_DIR"] = str(history_dir.resolve())
    for invocation in range(1, max_invocations + 1):
        try:
            result = subprocess.run(  # nosec B603 - explicit Python executable and user-configured local script
                [sys.executable, str(resolved_script)],
                cwd=resolved_script.parent,
                capture_output=True,
                text=True,
                check=False,
                env=updater_env,
                timeout=invocation_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            # A hung invocation (e.g. the updater blocked in a Yahoo network
            # request) must degrade to a safe partial-update event rather
            # than leave the nightly task -- and anything gated on its
            # completion -- blocked indefinitely (Codex review: "Bound each
            # updater subprocess invocation").
            errors.append(
                f"invocation {invocation} exceeded the {invocation_timeout_seconds:.0f}s timeout"
            )
            break
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            detail = output.strip().splitlines()[-1] if output.strip() else "no process output"
            errors.append(f"invocation {invocation} exited {result.returncode}: {detail}")
            break
        if "ALL_DONE" in output:
            completed = True
            remaining = 0
            break
        matches = _UPDATED_RE.findall(output)
        if not matches:
            errors.append(f"invocation {invocation} emitted no recognized progress line")
            break
        updated, next_remaining = (int(value) for value in matches[-1])
        updated_total += updated
        if remaining is not None and next_remaining >= remaining:
            stagnant_runs += 1
        else:
            stagnant_runs = 0
        remaining = next_remaining
        if stagnant_runs >= 3:
            errors.append("remaining count did not decrease across four consecutive invocations")
            break

    status = "completed" if completed else "partial"
    after_mtimes = _history_mtimes(history_dir)
    changed_tickers = sorted(
        ticker
        for ticker, mtime in after_mtimes.items()
        if before_mtimes.get(ticker) != mtime
    )
    active_tickers = _active_tickers(ticker_config) if ticker_config is not None else set()
    breadth_fetch = (
        _fetch_breadth_series(
            history_dir=history_dir,
            breadth_fetch_script=breadth_fetch_script or DEFAULT_BREADTH_FETCH_SCRIPT,
            timeout_seconds=breadth_fetch_timeout_seconds,
            breadth_fetch_python=breadth_fetch_python,
        )
        if fetch_breadth_series
        else {"status": "skipped", "detail": "fetch_breadth_series=False"}
    )
    summary = {
        "event_type": "ticker_history_update",
        "status": status,
        "completed_at": started_at.isoformat(),
        "invocations": invocation,
        "updated_total": updated_total,
        "remaining": remaining,
        "errors": errors,
        "changed_tickers": changed_tickers,
        "changed_active_tickers": sorted(set(changed_tickers) & active_tickers),
        "execution_owner": "claude_only",
        "perplexity_involved": False,
        "breadth_fetch": breadth_fetch,
    }
    _append_log(
        history_dir,
        "status={status} invocations={invocations} updated={updated_total} remaining={remaining} errors={errors} "
        "breadth_fetch={breadth_fetch}".format(**summary),
    )
    event_dir = artifacts_dir or Path(
        stream_root("ticker_history_update", started_at.date().isoformat())
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(event_dir / "ticker_update_event.json", json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-dir", type=Path, default=configured_ticker_history_dir()
    )
    env_script = os.environ.get("DCA_TICKER_DAILY_UPDATE_SCRIPT")
    parser.add_argument(
        "--updater-script", type=Path, default=Path(env_script) if env_script else None
    )
    parser.add_argument("--max-invocations", type=int, default=40)
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument(
        "--invocation-timeout-seconds",
        type=float,
        default=DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--skip-breadth-fetch",
        action="store_true",
        help="Don't run the NYSE TICK breadth-index fetch (ticker-market-breadth/fetch_tick.py) "
        "after the per-ticker update. It runs by default, as a best-effort subprocess step that "
        "never fails this command even if yfinance/pandas aren't installed.",
    )
    parser.add_argument(
        "--breadth-fetch-script",
        type=Path,
        default=DEFAULT_BREADTH_FETCH_SCRIPT,
        help="Override the breadth-index fetch script location (default: %(default)s). "
        "Invoked with --require-existing-history-dir; an older or custom script that "
        "doesn't recognize that flag falls back to running without it (and the guard "
        "against recreating a deleted managed archive is not enforced for that run) "
        "rather than being reported as a failure.",
    )
    parser.add_argument(
        "--breadth-fetch-timeout-seconds",
        type=float,
        default=DEFAULT_BREADTH_FETCH_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--breadth-fetch-python",
        type=str,
        help="Interpreter to launch the breadth fetch with (default: $DCA_BREADTH_FETCH_PYTHON, "
        "or auto-detect ticker-market-breadth/.venv, or fall back to this process's own "
        "interpreter -- see _default_breadth_fetch_python()).",
    )
    args = parser.parse_args()
    if args.updater_script is None:
        parser.error(
            "set DCA_TICKER_DAILY_UPDATE_SCRIPT or provide --updater-script; "
            "history defaults to the repository ticker_data archive"
        )
    summary = run_incremental_update(
        updater_script=args.updater_script,
        history_dir=args.history_dir,
        max_invocations=args.max_invocations,
        ticker_config=args.ticker_config,
        invocation_timeout_seconds=args.invocation_timeout_seconds,
        artifacts_dir=args.artifacts_dir,
        fetch_breadth_series=not args.skip_breadth_fetch,
        breadth_fetch_script=args.breadth_fetch_script,
        breadth_fetch_timeout_seconds=args.breadth_fetch_timeout_seconds,
        breadth_fetch_python=args.breadth_fetch_python,
    )
    print(summary)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
