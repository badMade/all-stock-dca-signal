"""Build ``backup_planner.pyz``: the backup planner as ONE file for the Perplexity sandbox.

When the Mac is offline the Perplexity backup task has no checkout, and it must not do the sizing
itself (that is the credit cost this design removes). This script packs the real planner modules plus a
snapshot of the config they read into a zipapp, so the task only fetches broker data into files, runs

    python3 backup_planner.pyz <plan|compute|finalize> ...

and acts on the one JSON document it prints. Design and the reasons for a zipapp rather than a
hand-condensed script: ``docs/adr-plan-first-backup-executor.md`` and the module docstring of
``scripts/tests/test_build_backup_planner.py``.

Properties (each pinned by that test file):

* **Deterministic.** Entries are sorted, carry a fixed timestamp and mode, and nothing records the build
  time, so the same sources always give the same bytes.
* **Stamped.** ``BUILD_INFO.json`` holds the source commit and a content hash; ``--build-info`` prints it.
* **Self-contained.** The repo modules come from a static import closure (so a lazy import is shipped
  too), the config is extracted to a temporary directory at start-up, and a host ``DCA_CONFIG_DIR`` is
  overridden so a stamped build behaves the same everywhere.
* **Fails closed.** A too-old Python or an unexpected error prints one JSON document and exits 2.

CLI: ``python -m scripts.build_backup_planner --out PATH [--check] [--source-commit SHA]``. One JSON
document on stdout; exit 0 on success, 2 on anything else. ``--check`` never writes: it rebuilds in
memory and reports ``UP_TO_DATE``, ``DRIFT`` or ``MISSING``. With no ``--source-commit`` it reuses the
commit stamped in the existing file, because an artifact cannot carry the hash of the commit that adds it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# What the sandbox file can run: the planner, and the assembler that feeds its safety gate. The closure of
# both ships (the gate also pulls in the primary's 15:30 candidate generator).
ENTRY_MODULES = ("scripts/backup_planner.py", "scripts/assemble_market_data.py")
DEFAULT_MIN_PYTHON = (3, 11)  # the repo's floor (pyproject requires-python)
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # the earliest a zip can record; keeps the bytes stable
_COMMIT = re.compile(r"[0-9a-f]{40}")

# Config the planner opens at run time, found by tracing open() across the three subcommands, the
# safety gate and the assembler. Listed
# rather than globbed so notifications, schedules and alert settings never ride along to the sandbox.
RUNTIME_CONFIG = (
    "config/budgets.json",
    "config/intraday_dca.json",
    "config/inverse_martingale.json",
    "config/market_calendar.json",
    "config/market_open_dca.json",
    "config/midday_dca.json",
    "config/ml_sizing_adjustment.json",
    "config/morning_dca.json",
    "config/near_close_dca.json",
    "config/per_ticker_sizing.json",
    "config/sizing_ladder.json",
    "config/tickers.json",
    "config/trading_window.json",
)

# Runs first inside the zipapp, before any repo module is imported (they read the environment at import
# time). Kept compatible with old interpreters so the version check can speak before a syntax error does.
_BOOTSTRAP = '''\
import json
import os
import sys

MIN_PYTHON = %(min_python)r


def _abort(code, detail):
    print(json.dumps({"status": "ABORT", "plan": None, "errors": [{"code": code, "detail": detail}]}, indent=2))
    sys.exit(2)


def main():
    if sys.version_info < MIN_PYTHON:
        _abort("python_too_old", "python>=%%d.%%d required, found %%d.%%d" %% (MIN_PYTHON + tuple(sys.version_info[:2])))
    import atexit
    import shutil
    import tempfile
    import zipfile

    archive = os.path.dirname(os.path.abspath(__file__))
    if sys.argv[1:] == ["--build-info"]:
        with zipfile.ZipFile(archive) as z:
            sys.stdout.write(z.read("BUILD_INFO.json").decode("utf-8"))
        return 0
    scratch = tempfile.mkdtemp(prefix="backup-planner-")
    atexit.register(shutil.rmtree, scratch, True)
    config_dir = os.path.join(scratch, "config")
    os.makedirs(config_dir)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.startswith("config/") and name.endswith(".json"):
                with open(os.path.join(scratch, name), "wb") as handle:
                    handle.write(z.read(name))
    # Overridden, not defaulted: a stamped build must not pick up whatever config the host has.
    os.environ["DCA_CONFIG_DIR"] = config_dir
    os.environ["DCA_DISABLE_DOTENV"] = "1"
    if not os.environ.get("DCA_CRON_TRACKING_ROOT"):
        os.environ["DCA_CRON_TRACKING_ROOT"] = os.path.join(scratch, "cron_tracking")  # the default sits inside the zip
    try:
        if sys.argv[1:2] == ["assemble-market-data"]:
            from scripts import assemble_market_data

            return assemble_market_data.main(sys.argv[2:])
        from scripts import backup_planner

        return backup_planner.main(sys.argv[1:])
    except Exception as exc:  # fail closed with one JSON document, never a traceback
        _abort("unexpected_error", type(exc).__name__)


sys.exit(main())
'''


class BuildError(Exception):
    """The build cannot produce a trustworthy artifact; nothing is written."""


# ---------------------------------------------------------------------------
# Which files ship
# ---------------------------------------------------------------------------


def _module_file(root: Path, dotted: str) -> str | None:
    """Repo-relative path of ``dotted`` if it is a module or package inside ``root``, else None."""
    base = dotted.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if (root / candidate).is_file():
            return candidate
    return None


def _imported_names(source: str) -> set[str]:
    """Every dotted name a module imports, including ``from pkg import submodule`` candidates."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _with_parent_packages(path: str) -> list[str]:
    """``scripts/x.py`` also needs ``scripts/__init__.py`` to import as ``scripts.x``."""
    parts = path.split("/")[:-1]
    return [f"{'/'.join(parts[:i])}/__init__.py" for i in range(1, len(parts) + 1)]


def collect_files(repo_root: Path) -> list[str]:
    """Sorted repo-relative paths that ship: the static import closure of the entry modules, plus config.

    Static (ast), not a runtime probe: it executes nothing and also finds imports made inside functions,
    which a probe would miss if that path did not run.
    """
    root = Path(repo_root)
    for entry in ENTRY_MODULES:
        if not (root / entry).is_file():
            raise BuildError(f"entry module missing: {entry}")
    found: set[str] = set()
    pending = list(ENTRY_MODULES)
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.add(path)
        for init in _with_parent_packages(path):
            if (root / init).is_file() and init not in found:
                pending.append(init)
        for name in _imported_names((root / path).read_text(encoding="utf-8")):
            target = _module_file(root, name)
            if target and target not in found:
                pending.append(target)
    missing = [c for c in RUNTIME_CONFIG if not (root / c).is_file()]
    if missing:
        raise BuildError(f"required config file(s) missing: {', '.join(missing)}")
    return sorted(found | set(RUNTIME_CONFIG))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _resolve_commit(root: Path, source_commit: str | None) -> str:
    if source_commit is None:
        try:
            done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BuildError("cannot read the source commit from git; pass --source-commit") from exc
        source_commit = done.stdout.strip()
    if not _COMMIT.fullmatch(source_commit):
        raise BuildError(f"source commit must be 40 lowercase hex characters, got {source_commit!r}")
    return source_commit


def _entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # unix, so the mode below is read the same everywhere
    info.external_attr = 0o644 << 16
    return info, data


def _bootstrap(min_python: tuple[int, int]) -> bytes:
    return (_BOOTSTRAP % {"min_python": tuple(min_python)}).encode()


def _digest(bootstrap: bytes, files: list[str], payload: dict[str, bytes]) -> str:
    # The bootstrap is executable behaviour (version check, config isolation, dispatch) and embeds min_python,
    # so hashing its bytes covers the build parameter as well; a file with a different startup path cannot
    # report this stamp. Hashed first, under its zip name, ahead of the sorted repo files.
    digest = hashlib.sha256()
    digest.update(b"__main__.py\0" + bootstrap + b"\0")
    for name in files:
        digest.update(name.encode() + b"\0" + payload[name] + b"\0")
    return digest.hexdigest()


def content_sha256(repo_root: Path = REPO, min_python: tuple[int, int] = DEFAULT_MIN_PYTHON) -> str:
    """The hash a build of ``repo_root`` stamps as ``content_sha256``.

    Covers the shipped files, the bootstrap and ``min_python``. Independent of the stamped commit and of
    zip/zlib versions, so the prompt generator can embed it and the sandbox task can compare it against
    ``--build-info`` (a stale or wrongly built file then fails closed).
    """
    root = Path(repo_root)
    files = collect_files(root)
    return _digest(_bootstrap(min_python), files, {name: (root / name).read_bytes() for name in files})


def _render(root: Path, source_commit: str, min_python: tuple[int, int]) -> tuple[bytes, dict]:
    commit = _resolve_commit(root, source_commit)
    files = collect_files(root)
    payload = {name: (root / name).read_bytes() for name in files}
    info = {
        "schema_version": 1,
        "tool": "scripts/build_backup_planner.py",
        "source_commit": commit,
        "content_sha256": _digest(_bootstrap(min_python), files, payload),
        "python_requires": f">={min_python[0]}.{min_python[1]}",
        "files": files,
    }
    entries = {
        "__main__.py": _bootstrap(min_python),
        "BUILD_INFO.json": (json.dumps(info, indent=2, sort_keys=True) + "\n").encode(),
        **payload,
    }
    buffer = io.BytesIO()
    buffer.write(b"#!/usr/bin/env python3\n")  # zipapp convention: Python skips the prefix and reads the zip
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            archive.writestr(*_entry(name, entries[name]))
    return buffer.getvalue(), info


def build(out: Path, *, repo_root: Path = REPO, source_commit: str | None = None,
          min_python: tuple[int, int] = DEFAULT_MIN_PYTHON) -> dict:
    """Write the sandbox file to ``out`` and return its build info. Nothing is left behind on failure."""
    data, info = _render(Path(repo_root), source_commit, min_python)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, 0o755)
        os.replace(temp, out)  # atomic: a reader never sees a half-written artifact
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return info


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _stamped_commit(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("BUILD_INFO.json"))["source_commit"]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def _check(out: Path, source_commit: str | None) -> tuple[int, dict]:
    if not out.is_file():
        return 2, {"status": "MISSING", "path": str(out)}
    commit = source_commit or _stamped_commit(out)
    if commit is None:
        return 2, {"status": "DRIFT", "path": str(out), "errors": [{"code": "unreadable_build_info"}]}
    data, info = _render(REPO, commit, DEFAULT_MIN_PYTHON)
    if out.read_bytes() != data:
        return 2, {"status": "DRIFT", "path": str(out), "content_sha256": info["content_sha256"]}
    return 0, {"status": "UP_TO_DATE", "path": str(out), "content_sha256": info["content_sha256"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="where to write backup_planner.pyz")
    parser.add_argument("--source-commit", help="40-hex commit to stamp (default: git HEAD, or the existing file's with --check)")
    parser.add_argument("--check", action="store_true", help="do not write; report UP_TO_DATE, DRIFT or MISSING")
    args = parser.parse_args(argv)
    out = Path(args.out)
    try:
        if args.check:
            code, report = _check(out, args.source_commit)
        else:
            info = build(out, source_commit=args.source_commit)
            report = {"status": "BUILT", "path": str(out), "bytes": out.stat().st_size,
                      "content_sha256": info["content_sha256"], "source_commit": info["source_commit"]}
            code = 0
    except BuildError as exc:
        code, report = 2, {"status": "FAILED", "errors": [{"code": "build_failed", "detail": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only, a message can carry paths
        code, report = 2, {"status": "FAILED", "errors": [{"code": "unexpected_error", "detail": type(exc).__name__}]}
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
