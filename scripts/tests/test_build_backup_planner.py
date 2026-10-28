"""Tests for ``scripts/build_backup_planner.py``: the sandbox build of the backup planner.

Written test-first, no implementation yet. The problem: when the Mac is offline the Perplexity task has
to run the planner in its own sandbox, which holds no checkout, and it must not do the sizing itself
(that is the credit cost this design exists to remove). So the repo builds ONE file, a zipapp
(``backup_planner.pyz``), that carries the real repo modules plus a config snapshot and runs
``python3 backup_planner.pyz <plan|compute|finalize> ...``.

Why a zipapp and not one hand-condensed ``.py``: the planner's closure is 24 modules / 5.6 MB of source
(1.5 MB zipped). A trimmed single script would be a second copy of the sizing, cap and order-history
logic, which drifts. The zipapp runs the same code the rest of the suite already tests.

Contract pinned here:

* ``build(out, *, repo_root, source_commit, min_python)`` writes the file and returns its build-info dict;
  ``main(argv)`` is the CLI (``--out PATH [--check] [--source-commit SHA]``), one JSON document on stdout,
  exit 0 on success and 2 on any failure.
* Deterministic: same sources give byte-identical output, whatever the mtimes or file order.
* Stamped: ``BUILD_INFO.json`` inside carries the source commit and a content hash; ``--build-info`` prints it.
* Self-contained: runs alone in an empty directory with no repo, no PYTHONPATH and no config on the host,
  and ignores a host ``DCA_CONFIG_DIR`` (a stamped build must behave the same everywhere).
* Same answers as the repo planner, byte for byte, on all three subcommands.
* Fails closed with ONE JSON document and exit 2 (never a traceback) on a too-old Python.
* Read-only friendly: writes nothing beside itself or into the working directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import build_backup_planner as bbp

REPO = Path(__file__).resolve().parents[2]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:49:30-04:00"
MAX_BYTES = 3 * 1024 * 1024  # the measured closure is 1.5 MB zipped; this is a runaway guard

# Everything the planner needs at run time. If one of these is absent the sandbox build cannot size.
REQUIRED_MODULES = {
    "dca_order_plan.py", "dca_ladder_core.py", "dca_config.py", "dca_runner.py", "near_close_dca.py",
    "dca_paths.py", "dca_fileio.py", "scripts/__init__.py", "scripts/backup_planner.py",
    "scripts/same_day_top_up_gate.py", "scripts/broker_reconciliation.py", "scripts/assemble_order_pages.py",
    # The safety gate: the primary's own 15:30 engine, and the assembler that feeds it.
    "scripts/generate_buy_candidates.py", "scripts/assemble_market_data.py", "dca_live_adapters.py",
    # The stream slots (0935, 1130, 1330, 1430): the primary's executor-class Gate 5.
    "scripts/stream_candidates.py", "scripts/generate_market_open_candidates.py", "scripts/generate_intraday_candidates.py",
    "morning_dca.py", "market_open_dca.py", "intraday_dca.py", "midday_dca.py",
    "scripts/generate_morning_candidates.py", "scripts/generate_midday_candidates.py",
}
REQUIRED_CONFIG = {"config/tickers.json", "config/budgets.json", "config/sizing_ladder.json", "config/per_ticker_sizing.json",
                   "config/market_calendar.json",  # the generators refuse to score without the calendar
                   "config/market_open_dca.json", "config/intraday_dca.json",
                   "config/morning_dca.json", "config/midday_dca.json"}  # the stream configs the wired slots load
# Trees that must never ship: tests, research, docs, local state and secrets.
FORBIDDEN_PREFIXES = ("tests/", "scripts/tests/", "docs/", "analysis/", "trading-strategies/", "ticker-advisor/",
                      "ml-forecaster/", "wondrous-octopus/", "claude/", "cron_tracking/", "artifacts/", ".venv/",
                      ".git/", ".github/", "agent-vault/")


@pytest.fixture(scope="module")
def pyz(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("build") / "backup_planner.pyz"
    bbp.build(out, repo_root=REPO, source_commit=COMMIT)
    return out


def _names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def _clean_env(tmp: Path, **extra) -> dict:
    """An environment like a bare sandbox: PATH only, nothing from the repo or the operator's shell."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp)}
    env.update(extra)
    return env


def _run(argv, *, cwd: Path, env: dict):
    done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=120)
    return done.returncode, done.stdout, done.stderr


def _write(tmp: Path, name: str, obj) -> str:
    path = tmp / name
    path.write_text(json.dumps(obj))
    return str(path)


# ---------------------------------------------------------------------------
# 1. Deterministic, minimal, stamped
# ---------------------------------------------------------------------------


def test_the_build_is_byte_identical_across_runs_and_mtimes(pyz, tmp_path):
    """Copy the source files, scramble every mtime, rebuild: the bytes must not change."""
    root = tmp_path / "copy"
    for rel in bbp.collect_files(REPO):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
        os.utime(target, (1_000_000_000 + hash(rel) % 10_000, 1_000_000_000 + hash(rel) % 10_000))
    again = tmp_path / "again.pyz"
    bbp.build(again, repo_root=root, source_commit=COMMIT)
    assert again.read_bytes() == pyz.read_bytes()


def test_the_zip_carries_no_timestamps_and_no_bytecode(pyz):
    with zipfile.ZipFile(pyz) as z:
        assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert not [n for n in z.namelist() if n.endswith(".pyc") or "__pycache__" in n]


def test_it_ships_the_planner_its_whole_import_closure_and_the_live_config(pyz):
    names = _names(pyz)
    assert REQUIRED_MODULES <= names, sorted(REQUIRED_MODULES - names)
    assert REQUIRED_CONFIG <= names, sorted(REQUIRED_CONFIG - names)
    assert "__main__.py" in names and "BUILD_INFO.json" in names


def test_every_repo_module_the_planner_really_imports_is_in_the_file(pyz):
    """A stale hand-kept list would ship a build that crashes in the sandbox; derive it from reality."""
    code = (
        "import os, sys\n"
        "sys.argv = ['x']\n"
        "import scripts.backup_planner, scripts.assemble_market_data\n"
        "root = os.getcwd()\n"
        "for m in sys.modules.values():\n"
        "    f = getattr(m, '__file__', None)\n"
        "    if f and f.startswith(root) and '.venv' not in f and 'site-packages' not in f:\n"
        "        print(os.path.relpath(f, root))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
                          env={**os.environ, "PYTHONPATH": str(REPO), "DCA_DISABLE_DOTENV": "1"})
    imported = {line for line in done.stdout.splitlines() if line}
    assert imported, "the probe found nothing"
    assert imported <= _names(pyz), sorted(imported - _names(pyz))


def test_it_ships_nothing_that_is_not_needed_and_no_secrets(pyz):
    names = _names(pyz)
    assert not [n for n in names if n.startswith(FORBIDDEN_PREFIXES)], sorted(names)
    assert ".env" not in names and not [n for n in names if n.endswith(".env")]
    with zipfile.ZipFile(pyz) as z:
        text = b"\n".join(z.read(n) for n in names if n.startswith("config/"))
    assert b"999000111" not in text
    # tests/test_config_invariants.py already bars account literals from tracked config; this checks that the
    # snapshot kept the placeholder and did not expand it (an expanded value would be a real account number).
    assert not re.search(rb"(?i)(?:account|acct|broker|robinhood)[^0-9A-Za-z]{0,3}\d{6,12}", text), "a labelled account literal"


def test_the_file_stays_inside_the_size_budget(pyz):
    assert 100_000 < pyz.stat().st_size <= MAX_BYTES


def test_build_info_stamps_the_commit_and_a_content_hash_that_matches_the_files(pyz):
    with zipfile.ZipFile(pyz) as z:
        info = json.loads(z.read("BUILD_INFO.json"))
        names = sorted(n for n in z.namelist() if n not in {"BUILD_INFO.json", "__main__.py"})
        # The stamp covers the executable bootstrap (version check, config isolation, dispatch) as well as the
        # shipped files; its bytes embed min_python, so that build parameter is covered with it.
        digest = hashlib.sha256()
        digest.update(b"__main__.py\0" + z.read("__main__.py") + b"\0")
        for name in names:
            digest.update(name.encode() + b"\0" + z.read(name) + b"\0")
    assert info["source_commit"] == COMMIT
    assert info["content_sha256"] == digest.hexdigest()
    assert info["python_requires"] == ">=3.11"
    assert info["files"] == names
    assert "built_at" not in info and "timestamp" not in json.dumps(info).lower()  # would break determinism


def test_the_commit_defaults_to_git_head_and_a_malformed_one_is_refused(tmp_path):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    info = bbp.build(tmp_path / "a.pyz", repo_root=REPO)
    assert info["source_commit"] == head
    with pytest.raises(bbp.BuildError, match="commit"):
        bbp.build(tmp_path / "b.pyz", repo_root=REPO, source_commit="not-a-sha")


# ---------------------------------------------------------------------------
# 2. Self-contained and read-only friendly
# ---------------------------------------------------------------------------


def test_it_prints_its_build_info_without_importing_the_planner(pyz, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    shutil.copy(pyz, sandbox / "backup_planner.pyz")
    code, out, _ = _run([sys.executable, "-I", "backup_planner.pyz", "--build-info"], cwd=sandbox, env=_clean_env(tmp_path))
    assert code == 0
    assert json.loads(out)["source_commit"] == COMMIT


def test_it_runs_in_an_empty_directory_with_no_repo_and_no_host_config(pyz, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    shutil.copy(pyz, sandbox / "backup_planner.pyz")
    cron = tmp_path / "cron"
    before = sorted(p.name for p in sandbox.iterdir())
    code, out, err = _run(
        [sys.executable, "-I", "backup_planner.pyz", "plan", "--source", "mac_plan", "--trading-day", DAY, "--slot", SLOT],
        cwd=sandbox, env=_clean_env(tmp_path, DCA_CRON_TRACKING_ROOT=str(cron)),
    )
    report = json.loads(out)
    assert (code, report["status"]) == (2, "NO_VALID_PLAN"), err
    assert [e["code"] for e in report["errors"]] == ["plan_missing"]
    assert sorted(p.name for p in sandbox.iterdir()) == before, "it wrote into the working directory"


def test_it_works_with_no_cron_root_set_and_leaves_no_files_behind(pyz, tmp_path):
    """The default cron root would sit inside the zip; the bootstrap must not try to create it there."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    shutil.copy(pyz, sandbox / "backup_planner.pyz")
    code, out, _ = _run(
        [sys.executable, "-I", "backup_planner.pyz", "plan", "--source", "mac_plan", "--trading-day", DAY, "--slot", SLOT],
        cwd=sandbox, env=_clean_env(tmp_path),
    )
    assert code == 2 and json.loads(out)["status"] == "NO_VALID_PLAN"
    assert sorted(p.name for p in sandbox.iterdir()) == ["backup_planner.pyz"]


def test_a_host_config_dir_is_ignored_so_a_stamped_build_behaves_the_same_everywhere(pyz, tmp_path):
    rogue = tmp_path / "rogue"
    rogue.mkdir()
    (rogue / "tickers.json").write_text('{"active": ["ZZZZ"]}')
    quotes = _write(tmp_path, "quotes.json", {"quotes": [{"symbol": "VT", "prior_close": 1000.0, "prior_low": None, "decision_price": 950.0}]})
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    argv = ["compute", "--trading-day", DAY, "--slot", SLOT, "--quotes", quotes, "--orders-pages", pages, "--now", NOW, "--buying-power", "1000"]
    plain = _run([sys.executable, "-I", str(pyz), *argv], cwd=tmp_path, env=_clean_env(tmp_path))
    hosted = _run([sys.executable, "-I", str(pyz), *argv], cwd=tmp_path, env=_clean_env(tmp_path, DCA_CONFIG_DIR=str(rogue)))
    assert plain[0] == 0 and plain == hosted


# ---------------------------------------------------------------------------
# 3. Same answers as the repo planner
# ---------------------------------------------------------------------------


def _both(pyz: Path, tmp: Path, argv):
    """Run one argv through the repo planner and through the sandbox file; return both results."""
    env = _clean_env(tmp, PYTHONPATH=str(REPO), DCA_DISABLE_DOTENV="1", DCA_CRON_TRACKING_ROOT=str(tmp / "cron"))
    repo = _run([sys.executable, "-m", "scripts.backup_planner", *argv], cwd=REPO, env=env)
    box = _run([sys.executable, "-I", str(pyz), *argv], cwd=tmp, env=_clean_env(tmp, DCA_CRON_TRACKING_ROOT=str(tmp / "cron")))
    return repo, box


def _quote(symbol, drop_pct, prior_close=1000.0):
    return {"symbol": symbol, "prior_close": prior_close, "prior_low": None, "decision_price": round(prior_close * (1 - drop_pct / 100.0), 6)}


def test_compute_gives_byte_identical_output_to_the_repo_planner(pyz, tmp_path):
    quotes = _write(tmp_path, "quotes.json", {"quotes": [_quote("VT", 5.0), _quote("MSFT", 1.2), _quote("AMD", 0.0)]})
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    argv = ["compute", "--trading-day", DAY, "--slot", SLOT, "--quotes", quotes, "--orders-pages", pages, "--now", NOW, "--buying-power", "1000"]
    repo, box = _both(pyz, tmp_path, argv)
    assert repo[0] == 0 and json.loads(repo[1])["status"] == "VALID_PLAN"
    assert box == repo


def test_finalize_gives_byte_identical_output_and_stays_shadow_only(pyz, tmp_path):
    quotes = _write(tmp_path, "quotes.json", {"quotes": [_quote("VT", 5.0), _quote("MSFT", 1.2)]})
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    code, out, _ = _run(
        [sys.executable, "-m", "scripts.backup_planner", "compute", "--trading-day", DAY, "--slot", SLOT, "--quotes", quotes,
         "--orders-pages", pages, "--now", NOW, "--buying-power", "1000"],
        cwd=REPO, env=_clean_env(tmp_path, PYTHONPATH=str(REPO), DCA_DISABLE_DOTENV="1"),
    )
    assert code == 0
    plan = json.loads(out)["plan"]
    plan_file = _write(tmp_path, "plan.json", plan)
    fresh = _write(tmp_path, "fresh.json", {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]})
    spreads = _write(tmp_path, "spreads.json", {o["symbol"]: {"bid": o["prior_close"] * 0.989, "ask": o["prior_close"] * 0.991} for o in plan["orders"]})
    argv = ["finalize", "--trading-day", DAY, "--slot", SLOT, "--plan", plan_file, "--orders-pages", pages, "--fresh-quotes", fresh,
            "--buying-power", "1000", "--fresh-spreads", spreads, "--now", NOW]
    repo, box = _both(pyz, tmp_path, argv)
    assert json.loads(repo[1])["status"] == "SHADOW_ONLY" and json.loads(repo[1])["to_place"] == []
    assert box == repo


def test_bad_input_fails_closed_identically_in_the_sandbox(pyz, tmp_path):
    quotes = tmp_path / "quotes.json"
    quotes.write_text("{not json")
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    argv = ["compute", "--trading-day", DAY, "--slot", SLOT, "--quotes", str(quotes), "--orders-pages", pages, "--now", NOW, "--buying-power", "1000"]
    repo, box = _both(pyz, tmp_path, argv)
    assert repo[0] == 2 and box == repo
    assert "Traceback" not in box[2] and len(box[1].strip().splitlines()) >= 1


def test_the_safety_gate_gives_byte_identical_output_from_the_sandbox_file(pyz, tmp_path):
    """`compute --market-data` is the whole point of the sandbox file: the same plan the repo planner gives."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_data_fixtures import market_data

    data = _write(tmp_path, "market.json", market_data({"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "AAPL": (100.0, 100.0)}))
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    argv = ["compute", "--trading-day", DAY, "--slot", "1530", "--market-data", data, "--orders-pages", pages,
            "--now", "2026-10-07T15:31:00-04:00", "--buying-power", "1000", "--deployed-last-7d-usd", "0"]
    repo, box = _both(pyz, tmp_path, argv)
    assert repo[0] == 0 and {o["symbol"] for o in json.loads(repo[1])["plan"]["orders"]} == {"VT", "MSFT"}
    assert box == repo


_NOW = {"0935": "2026-10-07T09:56:00-04:00", "1030": "2026-10-07T10:51:00-04:00", "1130": "2026-10-07T11:51:00-04:00",
        "1230": "2026-10-07T12:51:00-04:00", "1330": "2026-10-07T13:51:00-04:00", "1430": "2026-10-07T14:51:00-04:00"}


@pytest.mark.parametrize("slot", sorted(_NOW))
def test_a_stream_slot_gives_byte_identical_output_from_the_sandbox_file(pyz, tmp_path, slot):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_data_fixtures import market_data

    data = _write(tmp_path, "market.json", market_data({"VT": (100.0, 97.0), "MSFT": (100.0, 98.5)}, slot=slot))
    pages = _write(tmp_path, "pages.json", {"account_number": "999000111", "trading_day": DAY, "pages": [{"orders": []}]})
    argv = ["compute", "--trading-day", DAY, "--slot", slot, "--market-data", data, "--orders-pages", pages, "--now", _NOW[slot],
            "--buying-power", "1000", "--deployed-last-7d-usd", "auto"]
    repo, box = _both(pyz, tmp_path, argv)
    assert repo[0] == 0 and {o["symbol"] for o in json.loads(repo[1])["plan"]["orders"]} == {"VT", "MSFT"}
    assert box == repo


def test_the_universe_command_gives_byte_identical_output_from_the_sandbox_file(pyz, tmp_path):
    """Prompts name no stock; the sandbox file is where the task learns which symbols to fetch."""
    repo, box = _both(pyz, tmp_path, ["universe"])
    assert repo[0] == 0 and json.loads(repo[1])["count"] > 0
    assert box == repo


def test_the_assembler_runs_from_the_sandbox_file_and_writes_the_same_bytes(pyz, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_data_fixtures import raw_bundle

    raw = _write(tmp_path, "raw.json", raw_bundle({"VT": (100.0, 97.0), "MSFT": (100.0, 98.5)}))
    in_repo, in_box = tmp_path / "repo.json", tmp_path / "box.json"
    args = ["--trading-day", DAY, "--raw", raw]
    env = _clean_env(tmp_path, PYTHONPATH=str(REPO), DCA_DISABLE_DOTENV="1")
    done = _run([sys.executable, "-m", "scripts.assemble_market_data", *args, "--out", str(in_repo)], cwd=REPO, env=env)
    boxed = _run([sys.executable, "-I", str(pyz), "assemble-market-data", *args, "--out", str(in_box)], cwd=tmp_path, env=_clean_env(tmp_path))
    assert done[0] == 0 and boxed[0] == 0, (done, boxed)
    assert in_box.read_bytes() == in_repo.read_bytes()
    assert json.loads(boxed[1]) == json.loads(done[1]) | {"path": str(in_box)}


# ---------------------------------------------------------------------------
# 4. Fails closed: too-old Python, drift, missing inputs
# ---------------------------------------------------------------------------


def test_a_too_old_python_prints_one_json_abort_and_exits_2_without_a_traceback(tmp_path):
    out = tmp_path / "future.pyz"
    bbp.build(out, repo_root=REPO, source_commit=COMMIT, min_python=(99, 0))
    code, stdout, stderr = _run([sys.executable, "-I", str(out), "--build-info"], cwd=tmp_path, env=_clean_env(tmp_path))
    report = json.loads(stdout)
    assert code == 2 and report["status"] == "ABORT"
    assert "99.0" in json.dumps(report["errors"]) and "Traceback" not in stderr


def test_the_build_refuses_when_a_required_source_file_is_missing(tmp_path):
    root = tmp_path / "copy"
    for rel in bbp.collect_files(REPO):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    (root / "config" / "tickers.json").unlink()
    with pytest.raises(bbp.BuildError, match="tickers.json"):
        bbp.build(tmp_path / "x.pyz", repo_root=root, source_commit=COMMIT)
    assert not (tmp_path / "x.pyz").exists(), "a failed build must not leave a half-written artifact"


def _cli(capsys, argv):
    code = bbp.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_the_cli_builds_and_reports_one_json_document(capsys, tmp_path):
    out = tmp_path / "backup_planner.pyz"
    code, report = _cli(capsys, ["--out", str(out), "--source-commit", COMMIT])
    assert (code, report["status"]) == (0, "BUILT")
    assert report["path"] == str(out) and report["bytes"] == out.stat().st_size and len(report["content_sha256"]) == 64


def test_check_passes_on_a_fresh_build_and_fails_on_drift_without_touching_the_file(capsys, tmp_path, pyz):
    committed = tmp_path / "backup_planner.pyz"
    shutil.copy(pyz, committed)
    code, report = _cli(capsys, ["--out", str(committed), "--source-commit", COMMIT, "--check"])
    assert (code, report["status"]) == (0, "UP_TO_DATE")

    drifted = committed.read_bytes() + b"x"
    committed.write_bytes(drifted)
    code, report = _cli(capsys, ["--out", str(committed), "--source-commit", COMMIT, "--check"])
    assert (code, report["status"]) == (2, "DRIFT")
    assert committed.read_bytes() == drifted, "--check must never rewrite the file"

    missing = tmp_path / "nope.pyz"
    code, report = _cli(capsys, ["--out", str(missing), "--source-commit", COMMIT, "--check"])
    assert (code, report["status"]) == (2, "MISSING") and not missing.exists()


def test_the_build_script_is_stdlib_only_and_carries_no_broker_code():
    import ast

    source = (REPO / "scripts" / "build_backup_planner.py").read_text()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names), sorted(modules - set(sys.stdlib_module_names))
    for forbidden in ("place_equity_order", "robinhood", "requests", "urllib.request", "socket"):
        assert forbidden not in source.lower(), forbidden


# ---------------------------------------------------------------------------
# One JSON document and exit 2 on ANY failure, not only BuildError (Copilot review on PR 1624)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [PermissionError("/secret/path/denied"), OSError("disk full at /tmp/x"), RuntimeError("boom")])
def test_an_unexpected_failure_while_building_is_one_json_report_with_only_the_error_type(capsys, tmp_path, monkeypatch, error):
    def explode(*_a, **_k):
        raise error

    monkeypatch.setattr(bbp, "_render", explode)
    code, report = _cli(capsys, ["--out", str(tmp_path / "x.pyz"), "--source-commit", COMMIT])
    assert code == 2 and report["status"] == "FAILED"
    assert report["errors"] == [{"code": "unexpected_error", "detail": type(error).__name__}]
    assert "/secret" not in json.dumps(report) and "/tmp/x" not in json.dumps(report)  # a message can carry paths


def test_an_unexpected_failure_during_check_is_also_one_json_report(capsys, tmp_path, monkeypatch):
    target = tmp_path / "backup_planner.pyz"
    target.write_bytes(b"x")
    monkeypatch.setattr(bbp, "_render", lambda *_a, **_k: (_ for _ in ()).throw(OSError("unreadable")))
    code, report = _cli(capsys, ["--out", str(target), "--source-commit", COMMIT, "--check"])
    assert (code, report["status"]) == (2, "FAILED") and report["errors"][0]["code"] == "unexpected_error"


def test_a_failed_write_leaves_no_partial_file_and_still_reports_json(capsys, tmp_path, monkeypatch):
    import os as _os

    monkeypatch.setattr(_os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("nope")))
    out = tmp_path / "x.pyz"
    code, report = _cli(capsys, ["--out", str(out), "--source-commit", COMMIT])
    assert (code, report["status"]) == (2, "FAILED") and not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "a temp file was left behind"


def test_content_sha256_is_the_hash_a_build_stamps_and_ignores_the_commit(tmp_path):
    """The prompt generator embeds this value, so it must equal BUILD_INFO's and not depend on the stamped commit."""
    first = bbp.build(tmp_path / "a.pyz", source_commit=COMMIT)
    second = bbp.build(tmp_path / "b.pyz", source_commit="f" * 40)
    assert first["source_commit"] != second["source_commit"]
    assert first["content_sha256"] == second["content_sha256"] == bbp.content_sha256(REPO)
    assert re.fullmatch(r"[0-9a-f]{64}", bbp.content_sha256(REPO))


def test_content_sha256_changes_when_a_bundled_file_changes(tmp_path):
    """Copy the shipped tree, edit one bundled module, and the hash must move (so a stale zipapp is detectable)."""
    root = tmp_path / "tree"
    for name in bbp.collect_files(REPO):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / name, root / name)
    baseline = bbp.content_sha256(root)
    assert baseline == bbp.content_sha256(REPO)
    target = root / "dca_ladder_core.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert bbp.content_sha256(root) != baseline


# --- Copilot's second review on #1624: the stamp must cover the bootstrap and the build parameters ---------


def test_the_stamp_covers_the_bootstrap_so_a_different_startup_logic_cannot_report_the_same_stamp(monkeypatch):
    baseline = bbp.content_sha256(REPO)
    monkeypatch.setattr(bbp, "_BOOTSTRAP", bbp._BOOTSTRAP + "\n# a different startup path\n")
    assert bbp.content_sha256(REPO) != baseline


def test_the_stamp_covers_min_python_so_a_differently_configured_build_reports_another_stamp():
    assert bbp.content_sha256(REPO, min_python=(3, 12)) != bbp.content_sha256(REPO)
    assert bbp.content_sha256(REPO, min_python=bbp.DEFAULT_MIN_PYTHON) == bbp.content_sha256(REPO)


def test_a_build_with_a_non_default_min_python_stamps_the_matching_hash(tmp_path):
    info = bbp.build(tmp_path / "c.pyz", source_commit=COMMIT, min_python=(3, 12))
    assert info["content_sha256"] == bbp.content_sha256(REPO, min_python=(3, 12))
    assert info["content_sha256"] != bbp.content_sha256(REPO)


# -- the committed file must match its sources (2026-10-23: main at 26204ac3 reported DRIFT) -------------------------
#
# The Perplexity task compares the attached file's content_sha256 with the stamp in its prompt and stands down on any
# difference. #1898 changed the planner sources without rebuilding backup_planner.pyz, so the committed file was
# stale while the prompts (generated from the sources) were current: repasting them would have made every backup do
# nothing. `--check` compares raw zip bytes, which can vary with the zlib version, so CI compares the hash the file
# stamps into itself with `content_sha256()` of the sources, which is independent of zip details by design.

COMMITTED_PLANNER = REPO / "backup_planner.pyz"
REBUILD = "python -m scripts.build_backup_planner --out backup_planner.pyz, then regenerate the Perplexity prompts"


def _stamped_content_sha256(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("BUILD_INFO.json"))["content_sha256"]


def test_the_committed_planner_file_was_built_from_this_checkouts_sources():
    assert _stamped_content_sha256(COMMITTED_PLANNER) == bbp.content_sha256(REPO), f"stale file; run {REBUILD}"


def test_every_perplexity_prompt_requires_the_hash_the_committed_planner_file_carries():
    prompts = sorted((REPO / "claude" / "dca-executor-prompts" / "perplexity").glob("*-backup.txt"))
    assert len(prompts) == 6
    stamped = _stamped_content_sha256(COMMITTED_PLANNER)
    for prompt in prompts:
        required = re.search(r"content_sha256 must equal ([0-9a-f]{64})", prompt.read_text(encoding="utf-8"))
        assert required, prompt.name
        assert required.group(1) == stamped, f"{prompt.name}: the attached file would be refused at step 0; run {REBUILD}"


# --- Line endings: the same sources must build the same file on every OS --------------------------------
# A rebuild on a Windows checkout with autocrlf reads every bundled file with CRLF line endings, so the
# hash and the zip differed from Linux/CI for byte-identical git content (PR #1911: f4bbe2f9 vs 675d66b7).

def _checkout_copy(tmp_path: Path, name: str, newline: bytes) -> Path:
    """Copy the files the planner bundles into ``tmp_path/name``, rewriting line endings to ``newline``."""
    dest = tmp_path / name
    for rel in bbp.collect_files(REPO):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (REPO / rel).read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(text.replace(b"\n", newline))
    return dest


def test_the_content_hash_ignores_line_endings(tmp_path):
    lf = _checkout_copy(tmp_path, "lf", b"\n")
    crlf = _checkout_copy(tmp_path, "crlf", b"\r\n")
    assert bbp.content_sha256(crlf) == bbp.content_sha256(lf)


def test_the_built_file_is_byte_identical_whatever_the_line_endings(tmp_path):
    lf = _checkout_copy(tmp_path, "lf", b"\n")
    crlf = _checkout_copy(tmp_path, "crlf", b"\r\n")
    bbp.build(tmp_path / "lf.pyz", repo_root=lf, source_commit=COMMIT)
    bbp.build(tmp_path / "crlf.pyz", repo_root=crlf, source_commit=COMMIT)
    assert (tmp_path / "crlf.pyz").read_bytes() == (tmp_path / "lf.pyz").read_bytes()
