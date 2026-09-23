from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "launchd" / "prefetch_1530.sh"


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
    tmp_path: Path, *, day: str, weekday_number: int, et_time: str = "15:31", **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    fakebin = _write_fake_date(tmp_path, day=day, weekday_number=weekday_number, et_time=et_time)
    env = os.environ.copy()
    inherited_path = env.get("PATH", "")
    env["PATH"] = f"{fakebin}:{inherited_path}" if inherited_path else str(fakebin)
    env["DCA_REPO"] = str(REPO)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
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
    env = os.environ.copy()
    env["DCA_CRON_TRACKING_ROOT"] = str(cron_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import dca_paths; "
                f"print(dca_paths.stream_root('ce16ba6b', 'scheduled_runs', '{day}_1530ET'))"
            ),
        ],
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
