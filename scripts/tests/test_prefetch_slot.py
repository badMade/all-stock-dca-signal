from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "launchd" / "prefetch_slot.sh"
WRAPPER_1530 = REPO / "deploy" / "launchd" / "prefetch_1530.sh"


def _write_fake_date(tmp_path: Path, *, day: str, weekday_number: int, et_time: str = "15:31") -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    date_path = fakebin / "date"
    date_path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"+%F\" ]; then\n"
        f"  printf '{day}\\n'\n"
        "elif [ \"$1\" = \"+%H:%M\" ]; then\n"
        f"  printf '{et_time}\\n'\n"
        "elif [ \"$1\" = \"+%u\" ]; then\n"
        f"  printf '{weekday_number}\\n'\n"
        "else\n"
        "  /bin/date \"$@\"\n"
        "fi\n",
        encoding="utf-8",
    )
    date_path.chmod(date_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return fakebin


def _run_script(
    tmp_path: Path,
    *,
    day: str,
    weekday_number: int,
    et_time: str = "15:31",
    args: tuple[str, ...] = (),
    script: Path = WRAPPER_1530,
    **env_overrides: str,
) -> subprocess.CompletedProcess[str]:
    fakebin = _write_fake_date(tmp_path, day=day, weekday_number=weekday_number, et_time=et_time)
    env = os.environ.copy()
    inherited_path = env.get("PATH", "")
    env["PATH"] = f"{fakebin}:{inherited_path}" if inherited_path else str(fakebin)
    env["DCA_REPO"] = str(REPO)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_prefetch_stub(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "prefetch-python"
    stub.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _stream_root(cron_root: Path, day: str) -> Path:
    """The prefetch tree for ``day`` -- deliberately NOT the slot's run directory
    (see prefetch_slot.sh: an early run directory would read as the slot having run)."""
    env = os.environ.copy()
    env["DCA_CRON_TRACKING_ROOT"] = str(cron_root)
    result = subprocess.run(
        [sys.executable, "-c", f"import dca_paths; print(dca_paths.stream_root('yfinance_prefetch', '{day}'))"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
    )
    return Path(result.stdout.strip())


def test_prefetch_skips_cleanly_outside_the_approved_window(tmp_path: Path) -> None:
    result = _run_script(tmp_path, day="2028-01-03", weekday_number=1)

    assert result.returncode == 0
    assert "outside config/trading_window.json" in result.stdout


def test_prefetch_skips_when_a_publish_is_already_in_progress(tmp_path: Path) -> None:
    cron_root = tmp_path / "cron"
    lockdir = _stream_root(cron_root, "2026-09-23") / ".prefetch_1530.lock"
    lockdir.mkdir(parents=True)

    result = _run_script(
        tmp_path,
        day="2026-09-23",
        weekday_number=2,
        DCA_CRON_TRACKING_ROOT=str(cron_root),
    )

    assert result.returncode == 0
    assert "another run is already publishing" in result.stdout
    assert lockdir.is_dir(), "a skipped run must not remove the lock another run holds"


def test_prefetch_leaves_an_existing_fallback_file_in_place(tmp_path: Path) -> None:
    cron_root = tmp_path / "cron"
    out = _stream_root(cron_root, "2026-09-23") / "yfinance_fallback_2026-09-23_1530.json"
    out.parent.mkdir(parents=True)
    out.write_text("existing", encoding="utf-8")

    result = _run_script(
        tmp_path,
        day="2026-09-23",
        weekday_number=2,
        DCA_CRON_TRACKING_ROOT=str(cron_root),
    )

    assert result.returncode == 0
    assert "already exists; leaving the existing fallback in place" in result.stdout
    assert out.read_text(encoding="utf-8") == "existing"


def test_prefetch_uses_the_explicit_trading_day_for_publish_and_renames_atomically(tmp_path: Path) -> None:
    cron_root = tmp_path / "cron"
    stub = _write_prefetch_stub(
        tmp_path,
        "shift\n"
        "out=''\n"
        "day=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --out) out=\"$2\"; shift 2 ;;\n"
        "    --trading-day) day=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "printf '{\"trading_day\":\"%s\"}\\n' \"$day\" > \"$out\"\n",
    )

    result = _run_script(
        tmp_path,
        day="2026-09-26",
        weekday_number=6,
        et_time="00:15",
        DCA_TRADING_DAY="2026-09-25",
        DCA_CRON_TRACKING_ROOT=str(cron_root),
        DCA_PREFETCH_PYTHON=str(stub),
        DCA_PREFETCH_FETCH_SCRIPT=str(tmp_path / "ignored-fetch.py"),
    )

    out = _stream_root(cron_root, "2026-09-25") / "yfinance_fallback_2026-09-25_1530.json"
    assert result.returncode == 0
    assert out.exists()
    assert out.read_text(encoding="utf-8") == '{"trading_day":"2026-09-25"}\n'
    assert list(out.parent.glob("yfinance_fallback_2026-09-25_1530.json.*")) == []


def test_prefetch_surfaces_fetch_failures_without_publishing(tmp_path: Path) -> None:
    cron_root = tmp_path / "cron"
    stub = _write_prefetch_stub(tmp_path, "exit 9\n")

    result = _run_script(
        tmp_path,
        day="2026-09-23",
        weekday_number=2,
        DCA_CRON_TRACKING_ROOT=str(cron_root),
        DCA_PREFETCH_PYTHON=str(stub),
        DCA_PREFETCH_FETCH_SCRIPT=str(tmp_path / "ignored-fetch.py"),
    )

    out = _stream_root(cron_root, "2026-09-23") / "yfinance_fallback_2026-09-23_1530.json"
    assert result.returncode == 9
    assert not out.exists()


# --- every live slot (2026-09-22) ---------------------------------------------


def _recording_stub(tmp_path: Path) -> Path:
    """A fetch stand-in that writes its own argv as the payload, so a test can see
    exactly which tickers/decision time/interval the slot plan chose."""
    return _write_prefetch_stub(
        tmp_path,
        "shift\n"
        "out=''\n"
        "for a in \"$@\"; do case \"$prev\" in --out) out=\"$a\" ;; esac; prev=\"$a\"; done\n"
        "printf '%s\\n' \"$*\" > \"$out\"\n",
    )


def _fetch_args(tmp_path: Path, slot: str, et_time: str, *args: str) -> str:
    cron_root = tmp_path / "cron"
    result = _run_script(
        tmp_path,
        day="2026-09-23",
        weekday_number=3,
        et_time=et_time,
        args=args or (slot,),
        script=SCRIPT,
        DCA_CRON_TRACKING_ROOT=str(cron_root),
        DCA_PREFETCH_PYTHON=str(_recording_stub(tmp_path)),
        DCA_PREFETCH_FETCH_SCRIPT=str(tmp_path / "ignored-fetch.py"),
    )
    assert result.returncode == 0, result.stderr
    out = _stream_root(cron_root, "2026-09-23") / f"yfinance_fallback_2026-09-23_{slot}.json"
    assert out.exists(), result.stdout
    return out.read_text(encoding="utf-8")


def _arg(argv: str, flag: str) -> str:
    parts = argv.split()
    return parts[parts.index(flag) + 1]


def test_robinhood_slots_fetch_five_minute_bars_at_their_own_decision_time(tmp_path: Path) -> None:
    argv = _fetch_args(tmp_path, "1130", "11:31")
    assert _arg(argv, "--decision-time-et") == "11:30"
    assert _arg(argv, "--interval-minutes") == "5"
    assert _arg(argv, "--trading-day") == "2026-09-23"
    # Robinhood-primary slots route nothing elsewhere, so GFUZ is fetched too.
    assert "GFUZ" in _arg(argv, "--tickers").split(",")


def test_midday_fetches_the_native_five_minute_cadence_for_the_whole_universe(tmp_path: Path) -> None:
    # 12:30 is Robinhood-primary like every other executor slot: no Finance cadence, no ticker routed elsewhere.
    argv = _fetch_args(tmp_path, "1230", "12:31")
    assert _arg(argv, "--decision-time-et") == "12:30"
    assert _arg(argv, "--interval-minutes") == "5"
    assert "GFUZ" in _arg(argv, "--tickers").split(",")


def test_auto_picks_the_slot_whose_decision_time_just_passed(tmp_path: Path) -> None:
    argv = _fetch_args(tmp_path, "0935", "09:36", "auto")
    assert _arg(argv, "--decision-time-et") == "09:35"


def test_auto_skips_when_no_slot_is_due(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path, day="2026-09-23", weekday_number=3, et_time="12:00", args=("auto",), script=SCRIPT,
        DCA_CRON_TRACKING_ROOT=str(tmp_path / "cron"),
    )
    assert result.returncode == 0
    assert "no slot is due" in result.stdout
    assert not (tmp_path / "cron").exists()


def test_prefetch_never_creates_the_slots_run_directory(tmp_path: Path) -> None:
    _fetch_args(tmp_path, "1430", "14:31")
    assert sorted(p.name for p in (tmp_path / "cron").iterdir()) == ["yfinance_prefetch"]
