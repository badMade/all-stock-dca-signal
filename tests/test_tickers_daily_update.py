"""Tests for the program-facing Claude ticker-history update wrapper."""

import json
import os
import sys
from pathlib import Path

import pytest

from scripts.run_tickers_daily_update import (
    _default_breadth_fetch_python,
    _fetch_breadth_series,
    _universe_tickers,
    main,
    run_incremental_update,
)


def _write_updater(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_missing_history_dir_error_names_the_override_env_var(tmp_path):
    # Codex review ("Resolve the archive outside the installed package"):
    # DEFAULT_TICKER_HISTORY_DIR is only correct for a repository checkout;
    # installed (non-editable) deployments resolve it under site-packages
    # instead, where it never exists. The failure must name the escape
    # hatch (DCA_TICKER_HISTORY_DIR) rather than leaving the operator to
    # guess.
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    missing_history_dir = tmp_path / "does-not-exist"

    with pytest.raises(FileNotFoundError, match="DCA_TICKER_HISTORY_DIR"):
        run_incremental_update(
            updater_script=updater,
            history_dir=missing_history_dir,
            ticker_config=tmp_path / "tickers.json",
            artifacts_dir=tmp_path / "events",
        )


def test_wrapper_runs_until_the_external_updater_reports_complete(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    state_path = tmp_path / "state.txt"
    ticker_config = tmp_path / "tickers.json"
    artifacts_dir = tmp_path / "events"
    ticker_config.write_text('{"active": []}', encoding="utf-8")
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "from pathlib import Path\n"
        f"state = Path({str(state_path)!r})\n"
        "count = int(state.read_text() if state.exists() else '0') + 1\n"
        "state.write_text(str(count))\n"
        "print('ALL_DONE' if count == 2 else 'DONE_THIS_RUN updated=3 remaining=4')\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        max_invocations=4,
        ticker_config=ticker_config,
        artifacts_dir=artifacts_dir,
    )

    assert result["status"] == "completed"
    assert result["invocations"] == 2
    assert result["updated_total"] == 3
    assert result["remaining"] == 0
    assert result["errors"] == []
    assert (artifacts_dir / "ticker_update_event.json").is_file()
    assert "status=completed invocations=2 updated=3 remaining=0 errors=[]" in (
        history_dir / "_daily_update_log.txt"
    ).read_text(encoding="utf-8")


def test_wrapper_stops_after_repeated_non_decreasing_progress(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('DONE_THIS_RUN updated=0 remaining=4')\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        max_invocations=10,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "partial"
    assert result["invocations"] == 4
    assert result["remaining"] == 4
    assert result["errors"] == [
        "remaining count did not decrease across four consecutive invocations"
    ]


def test_updated_active_ticker_is_published_for_calibration(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    history = history_dir / "ABC.csv"
    history.write_text("Date,Close\n2026-08-01,10\n", encoding="utf-8")
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["ABC", "OTHER"]}', encoding="utf-8")
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "from pathlib import Path\n"
        f"history = Path({str(history)!r})\n"
        "history.write_text('Date,Close\\n2026-08-02,11\\n')\n"
        "print('ALL_DONE')\n",
    )

    events_dir = tmp_path / "events"
    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        ticker_config=ticker_config,
        artifacts_dir=events_dir,
    )

    assert result["changed_tickers"] == ["ABC"]
    assert result["changed_active_tickers"] == ["ABC"]
    event = json.loads((events_dir / "ticker_update_event.json").read_bytes())
    assert event["changed_active_tickers"] == ["ABC"]


def test_updated_whole_share_ticker_is_also_published_for_calibration(tmp_path):
    """Codex review ("Include whole-share symbols in history-update
    events"): a BYDDY-only history update must still land in
    changed_active_tickers, or daily_per_ticker_calibration.py's
    event-driven rebuild (gated on that list being non-empty) can never
    fire for it -- even though generate_per_ticker_sizing.py's own
    build_sizing_payload() already calibrates the active-plus-whole-share
    universe.
    """
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    history = history_dir / "BYDDY.csv"
    history.write_text("Date,Close\n2026-08-01,10\n", encoding="utf-8")
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text(
        '{"active": ["ABC"], "excluded": [], "whole_share": ["BYDDY"]}', encoding="utf-8"
    )
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "from pathlib import Path\n"
        f"history = Path({str(history)!r})\n"
        "history.write_text('Date,Close\\n2026-08-02,11\\n')\n"
        "print('ALL_DONE')\n",
    )

    events_dir = tmp_path / "events"
    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        ticker_config=ticker_config,
        artifacts_dir=events_dir,
    )

    assert result["changed_tickers"] == ["BYDDY"]
    assert result["changed_active_tickers"] == ["BYDDY"]


def test_wrapper_passes_the_shared_history_dir_to_the_external_updater(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "import os\n"
        "from pathlib import Path\n"
        f"expected = {str(history_dir.resolve())!r}\n"
        "assert os.environ['TICKERS_DIR'] == expected\n"
        "print('ALL_DONE')\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "completed"


def test_relative_updater_script_still_resolves_after_chdir(tmp_path, monkeypatch):
    """A manifest-supplied relative script path must resolve against the
    process's cwd at call time, not silently double up under the
    subprocess's own cwd (its parent directory) (Codex review: "Resolve
    the updater path before switching directories")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    updater = scripts_dir / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")

    monkeypatch.chdir(tmp_path)
    result = run_incremental_update(
        updater_script=Path("scripts/incremental_update.py"),
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "completed"
    assert result["errors"] == []


def test_all_done_from_a_nonzero_exit_is_never_accepted_as_complete(tmp_path):
    """ALL_DONE printed just before a nonzero exit must not be treated as a
    completed run (Codex review: "Reject ALL_DONE from a failed updater
    process")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "import sys\nprint('ALL_DONE')\nsys.exit(1)\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "partial"
    assert result["errors"] and "exited 1" in result["errors"][0]


def test_a_hung_invocation_degrades_to_partial_instead_of_hanging_forever(tmp_path):
    """A subprocess that never returns must time out into a safe partial
    event rather than block the caller indefinitely (Codex review: "Bound
    each updater subprocess invocation")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "import time\ntime.sleep(30)\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        invocation_timeout_seconds=0.2,
    )

    assert result["status"] == "partial"
    assert result["errors"] and "timeout" in result["errors"][0]


def test_missing_history_dir_error_points_to_supported_override(tmp_path):
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")

    with pytest.raises(FileNotFoundError, match="DCA_TICKER_HISTORY_DIR"):
        run_incremental_update(
            updater_script=updater,
            history_dir=tmp_path / "missing-histories",
            artifacts_dir=tmp_path / "events",
        )


def test_fetch_breadth_series_reports_skipped_for_a_missing_script(tmp_path):
    result = _fetch_breadth_series(
        history_dir=tmp_path,
        breadth_fetch_script=tmp_path / "does-not-exist.py",
        timeout_seconds=5,
    )

    assert result == {"status": "skipped", "detail": f"{tmp_path / 'does-not-exist.py'} not present"}


def test_fetch_breadth_series_reports_completed_on_success(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(
        script,
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--history-dir')\n"
        "args = parser.parse_args()\n"
        "print('wrote 5 rows to ' + args.history_dir)\n",
    )

    result = _fetch_breadth_series(history_dir=history_dir, breadth_fetch_script=script, timeout_seconds=5)

    assert result["status"] == "completed"
    assert "wrote 5 rows" in result["detail"]


def test_fetch_breadth_series_honors_an_explicit_python_override(tmp_path):
    """An explicit breadth_fetch_python bypasses _default_breadth_fetch_python()'s
    own auto-detection entirely."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "print('wrote 5 rows')\n")

    result = _fetch_breadth_series(
        history_dir=history_dir,
        breadth_fetch_script=script,
        timeout_seconds=5,
        breadth_fetch_python=sys.executable,
    )

    assert result["status"] == "completed"
    assert "wrote 5 rows" in result["detail"]


def test_fetch_breadth_series_resolves_a_relative_python_override(tmp_path, monkeypatch):
    """Codex review ("Resolve relative interpreter overrides before changing
    cwd"): a still-relative --breadth-fetch-python/DCA_BREADTH_FETCH_PYTHON
    override that contains a path separator (e.g. "venv/bin/python", valid
    from the CALLER's cwd) must resolve against that cwd, not get looked up
    a second time under the subprocess's own cwd (the breadth script's
    parent directory) -- the same failure mode already fixed for
    --breadth-fetch-script and history_dir above.
    """
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "print('wrote 5 rows')\n")

    monkeypatch.chdir(tmp_path)
    relative_python = os.path.relpath(sys.executable, start=tmp_path)
    assert os.sep in relative_python or (os.altsep and os.altsep in relative_python)

    result = _fetch_breadth_series(
        history_dir=history_dir,
        breadth_fetch_script=script,
        timeout_seconds=5,
        breadth_fetch_python=relative_python,
    )

    assert result["status"] == "completed"
    assert "wrote 5 rows" in result["detail"]


def test_fetch_breadth_series_resolves_a_relative_script_path(tmp_path, monkeypatch):
    """Codex review ("Resolve the overridden breadth script before changing
    cwd"): a still-relative --breadth-fetch-script override must resolve
    against the CALLER's cwd, not get looked up a second time under the
    subprocess's own cwd (the script's parent directory) -- the exact same
    failure mode already fixed for the per-ticker updater's own
    resolved_script (test_relative_updater_script_still_resolves_after_chdir
    above).
    """
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    scripts_dir = tmp_path / "breadth-scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "fake_fetch_tick.py"
    _write_updater(script, "print('wrote 5 rows')\n")

    monkeypatch.chdir(tmp_path)
    result = _fetch_breadth_series(
        history_dir=history_dir,
        breadth_fetch_script=Path("breadth-scripts/fake_fetch_tick.py"),
        timeout_seconds=5,
    )

    assert result["status"] == "completed"
    assert "wrote 5 rows" in result["detail"]


def test_fetch_breadth_series_resolves_a_relative_history_dir(tmp_path, monkeypatch):
    """Codex review ("Resolve the history directory before changing cwd"):
    a still-relative history_dir (e.g. from a relative
    DCA_TICKER_HISTORY_DIR or --history-dir) must resolve against the
    CALLER's cwd, not get reinterpreted relative to the subprocess's own
    cwd (the breadth script's parent directory) -- which would silently
    write to, and report success for, a bogus nested directory instead of
    the intended archive.
    """
    (tmp_path / "histories").mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(
        script,
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--history-dir')\n"
        "args = parser.parse_args()\n"
        "print('received-history-dir:' + args.history_dir)\n",
    )

    monkeypatch.chdir(tmp_path)
    result = _fetch_breadth_series(
        history_dir=Path("histories"), breadth_fetch_script=script, timeout_seconds=5
    )

    assert result["status"] == "completed"
    assert f"received-history-dir:{(tmp_path / 'histories').resolve()}" in result["detail"]


def test_fetch_breadth_series_skips_when_the_archive_no_longer_exists(tmp_path):
    """Codex review ("Preserve the default archive guard in automatic
    refreshes"): if history_dir has disappeared (e.g. the managed Drive
    mount was lost partway through the long per-ticker update loop), this
    step must skip rather than invoke fetch_tick.py with a directory that
    no longer exists -- fetch_tick.py's own default-archive guard never
    fires here since this wrapper always passes an explicit --history-dir.
    """
    missing_history_dir = tmp_path / "vanished-mid-run"
    script = tmp_path / "fake_fetch_tick.py"
    marker = tmp_path / "ran.marker"
    _write_updater(
        script,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nprint('ALL_DONE')\n",
    )

    result = _fetch_breadth_series(
        history_dir=missing_history_dir, breadth_fetch_script=script, timeout_seconds=5
    )

    assert result == {"status": "skipped", "detail": f"{missing_history_dir} does not exist"}
    assert not marker.exists()


@pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf"), float("-inf"), 0.0, -5.0])
def test_fetch_breadth_series_rejects_a_non_finite_or_nonpositive_timeout(tmp_path, bad_timeout):
    """Codex review ("Reject non-finite breadth-fetch timeouts"):
    subprocess.run(timeout=...) raises ValueError/OverflowError for
    nan/inf, escaping this function's own "never raises" contract and
    aborting run_incremental_update() before its log/event summary is
    written. Validated up front instead of ever reaching subprocess.run().
    """
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "print('should never run')\n")

    result = _fetch_breadth_series(history_dir=history_dir, breadth_fetch_script=script, timeout_seconds=bad_timeout)

    assert result["status"] == "failed"
    assert "invalid timeout_seconds" in result["detail"]


def test_default_breadth_fetch_python_prefers_the_subprojects_own_venv(tmp_path):
    """Codex review ("Launch the breadth fetch from its dependency
    environment"): ticker-market-breadth/README.md documents a SEPARATE
    venv holding yfinance/pandas -- neither of which belongs in this
    stdlib-only-at-runtime module's own environment. If that venv exists
    next to the script, it must be preferred over this process's own
    interpreter.
    """
    script = tmp_path / "fetch_tick.py"
    script.write_text("", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    assert _default_breadth_fetch_python(script) == str(venv_python)


def test_default_breadth_fetch_python_falls_back_to_this_interpreter(tmp_path):
    """Without a subproject venv present, fall back to sys.executable
    unchanged (e.g. a deployment that installed yfinance/pandas directly
    into this process's own environment instead)."""
    script = tmp_path / "fetch_tick.py"
    script.write_text("", encoding="utf-8")

    assert _default_breadth_fetch_python(script) == sys.executable


def test_default_breadth_fetch_python_honors_the_env_var_from_any_entry_point(tmp_path, monkeypatch):
    """Codex review ("Honor the interpreter override in every entry
    point"): scripts/run_ticker_maintenance.py's run_update() calls
    run_incremental_update() without ever threading a --breadth-fetch-python
    CLI flag through, so DCA_BREADTH_FETCH_PYTHON must be checked inside
    the shared default-resolution function itself -- not only as an
    argparse default in this module's own main() -- or the override
    silently never applies there. Takes priority even over an auto-
    detected subproject venv.
    """
    script = tmp_path / "fetch_tick.py"
    script.write_text("", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("DCA_BREADTH_FETCH_PYTHON", "/custom/env/python")

    assert _default_breadth_fetch_python(script) == "/custom/env/python"


def test_fetch_breadth_series_reports_failed_when_the_interpreter_cannot_launch(tmp_path):
    """Codex review ("Convert interpreter launch errors into breadth
    failures"): a missing/non-executable --breadth-fetch-python /
    DCA_BREADTH_FETCH_PYTHON override makes subprocess.run() itself raise
    FileNotFoundError, not subprocess.TimeoutExpired -- this must be
    recorded as a "failed" outcome, not escape the function.
    """
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "print('should never run')\n")

    result = _fetch_breadth_series(
        history_dir=history_dir,
        breadth_fetch_script=script,
        timeout_seconds=5,
        breadth_fetch_python=str(tmp_path / "does-not-exist" / "python"),
    )

    assert result["status"] == "failed"
    assert "could not launch" in result["detail"]


def test_fetch_breadth_series_reports_failed_on_nonzero_exit(tmp_path):
    """A missing yfinance/pandas install (or a real fetch failure) must
    degrade to a recorded "failed" status, never an exception -- this is
    optional enrichment data, not part of the per-ticker archive contract
    run_incremental_update() exists to guarantee."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n")

    result = _fetch_breadth_series(history_dir=history_dir, breadth_fetch_script=script, timeout_seconds=5)

    assert result["status"] == "failed"
    assert "exited 1" in result["detail"]
    assert "boom" in result["detail"]


def test_fetch_breadth_series_reports_failed_on_timeout(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    script = tmp_path / "fake_fetch_tick.py"
    _write_updater(script, "import time\ntime.sleep(30)\n")

    result = _fetch_breadth_series(history_dir=history_dir, breadth_fetch_script=script, timeout_seconds=0.2)

    assert result["status"] == "failed"
    assert "timeout" in result["detail"]


def test_run_incremental_update_wires_the_breadth_fetch_outcome_into_the_summary(tmp_path):
    """The daily per-ticker update and the breadth-index fetch are tied
    together in one command by default -- see the module docstring's
    "tying it to the daily downloads" rationale."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    breadth_script = tmp_path / "fake_fetch_tick.py"
    _write_updater(breadth_script, "print('wrote 5 rows')\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        breadth_fetch_script=breadth_script,
    )

    assert result["breadth_fetch"]["status"] == "completed"
    assert "wrote 5 rows" in result["breadth_fetch"]["detail"]
    logged = (history_dir / "_daily_update_log.txt").read_text(encoding="utf-8")
    assert "breadth_fetch=" in logged


def test_run_incremental_update_skips_the_breadth_fetch_when_disabled(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    breadth_script = tmp_path / "fake_fetch_tick.py"
    _write_updater(breadth_script, "print('should never run')\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        breadth_fetch_script=breadth_script,
        fetch_breadth_series=False,
    )

    assert result["breadth_fetch"] == {"status": "skipped", "detail": "fetch_breadth_series=False"}


def test_run_incremental_update_breadth_fetch_failure_does_not_fail_the_run(tmp_path):
    """A broken/absent yfinance install for the breadth fetch must not turn
    an otherwise-successful per-ticker update into a failed run."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    breadth_script = tmp_path / "fake_fetch_tick.py"
    _write_updater(breadth_script, "import sys\nsys.exit(1)\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        breadth_fetch_script=breadth_script,
    )

    assert result["status"] == "completed"
    assert result["breadth_fetch"]["status"] == "failed"


def test_universe_tickers_requires_active_list(tmp_path):
    cfg_file = tmp_path / "tickers.json"
    cfg_file.write_text(json.dumps({"active": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain an active ticker list"):
        _universe_tickers(cfg_file)


def test_universe_tickers_requires_whole_share_to_be_list_of_strings(tmp_path):
    cfg_file = tmp_path / "tickers.json"
    cfg_file.write_text(json.dumps({"active": ["AAPL"], "whole_share": [123]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list of strings"):
        _universe_tickers(cfg_file)


def test_run_incremental_update_rejects_missing_updater_script(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    nonexistent = tmp_path / "does_not_exist.py"
    with pytest.raises(FileNotFoundError, match="configured ticker updater does not exist"):
        run_incremental_update(updater_script=nonexistent, history_dir=history_dir)


def test_run_incremental_update_rejects_nonpositive_max_invocations(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "updater.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    with pytest.raises(ValueError, match="max_invocations must be at least one"):
        run_incremental_update(updater_script=updater, history_dir=history_dir, max_invocations=0)


def test_run_incremental_update_emits_error_when_no_progress_line(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "updater.py"
    _write_updater(updater, "print('something random without expected pattern')\n")
    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        fetch_breadth_series=False,
    )
    assert result["status"] == "partial"
    assert any("emitted no recognized progress line" in err for err in result["errors"])


def test_main_cli_missing_updater_script(monkeypatch):
    monkeypatch.delenv("DCA_TICKER_DAILY_UPDATE_SCRIPT", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_tickers_daily_update.py"])
    with pytest.raises(SystemExit):
        main()


def test_main_cli_success(monkeypatch, tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "updater.py"
    _write_updater(updater, "print('ALL_DONE')\n")
    ticker_cfg = tmp_path / "tickers.json"
    ticker_cfg.write_text(json.dumps({"active": ["AAPL"]}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_tickers_daily_update.py",
            "--updater-script",
            str(updater),
            "--history-dir",
            str(history_dir),
            "--ticker-config",
            str(ticker_cfg),
            "--artifacts-dir",
            str(tmp_path / "events"),
            "--skip-breadth-fetch",
        ],
    )
    assert main() == 0


def test_main_cli_failure(monkeypatch, tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "updater.py"
    _write_updater(updater, "import sys\nsys.exit(2)\n")
    ticker_cfg = tmp_path / "tickers.json"
    ticker_cfg.write_text(json.dumps({"active": ["AAPL"]}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_tickers_daily_update.py",
            "--updater-script",
            str(updater),
            "--history-dir",
            str(history_dir),
            "--ticker-config",
            str(ticker_cfg),
            "--artifacts-dir",
            str(tmp_path / "events"),
            "--skip-breadth-fetch",
        ],
    )
    assert main() == 1
