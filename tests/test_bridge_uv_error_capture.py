"""The live prompts' BRIDGE PROTOCOL opener must keep the reason ``uv python install 3.11`` failed.

2026-10-21: the 15:30 slot aborted at Gate 2 on Oct 13, 14, 15 and 16 with "bridge shell has no Python
3.11", and every alert could only say the install "produced nothing": the opener runs
``uv python install 3.11 >/dev/null 2>&1``, so a full disk, a blocked download and a TLS failure all look
identical. These tests run the opener from each live prompt against a fake ``uv`` and require the last
lines of the install's own error to survive in ``$UV_ERR`` for the Gate 2 alert.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

LIVE_MIRROR = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
# The folder also holds a *.template.txt that is not a slot prompt; the seven slots are counted below so a
# prompt that loses its opener cannot silently drop out of coverage.
PROMPTS = sorted(p for p in LIVE_MIRROR.glob("*.txt") if not p.name.endswith(".template.txt"))
OPENER = re.compile(r"begin EVERY call with: `([^`]*)`")


def _opener(prompt: Path, sandbox: Path) -> str:
    match = OPENER.search(prompt.read_text(encoding="utf-8"))
    assert match, f"{prompt.name}: BRIDGE PROTOCOL opener not found"
    # The opener hardcodes /tmp/uv-*; point it at the sandbox so the test is hermetic.
    return match.group(1).replace("/tmp/", f"{sandbox}/")


def _fake_uv(bin_dir: Path, *, installed: bool, install_ok: bool, install_stderr: str = "") -> Path:
    marker = bin_dir / "installed"
    called = bin_dir / "install-called"
    if installed:
        marker.write_text("x")
    script = bin_dir / "uv"
    script.write_text(
        "#!/bin/bash\n"
        f'if [ "$1 $2" = "python find" ]; then [ -e "{marker}" ] && echo /fake/python3.11 && exit 0; exit 1; fi\n'
        f'if [ "$1 $2" = "python install" ]; then touch "{called}"; printf %s "$INSTALL_STDERR" >&2; '
        f'{"touch " + str(marker) + "; exit 0" if install_ok else "exit 1"}; fi\n'
        "exit 2\n"
    )
    script.chmod(0o755)
    return called


def _run(prompt: Path, tmp_path: Path, **uv) -> tuple[str, str, bool]:
    home = tmp_path / "home"
    (home / "mnt/all-stock-dca").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = _fake_uv(bin_dir, **uv)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    probe = '; printf "PY=%s\\n" "$PY"; printf "UV_ERR<<%s>>\\n" "$UV_ERR"'
    out = subprocess.run(
        ["bash", "-c", _opener(prompt, sandbox) + probe],
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "INSTALL_STDERR": uv.get("install_stderr", "")},
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout
    py = re.search(r"^PY=(.*)$", out, re.M).group(1)
    err = re.search(r"UV_ERR<<(.*?)>>", out, re.S).group(1)
    return py, err, called.exists()


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_a_failed_install_keeps_the_error_text(prompt, tmp_path):
    py, err, _ = _run(prompt, tmp_path, installed=False, install_ok=False,
                      install_stderr="error: No space left on device (os error 28)\n")
    assert py == ""
    assert "No space left on device" in err


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_a_successful_install_sets_py_and_leaves_no_error(prompt, tmp_path):
    py, err, called = _run(prompt, tmp_path, installed=False, install_ok=True)
    assert py == "/fake/python3.11"
    assert err == ""
    assert called


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_an_existing_interpreter_is_reused_without_installing(prompt, tmp_path):
    py, err, called = _run(prompt, tmp_path, installed=True, install_ok=True)
    assert py == "/fake/python3.11"
    assert err == ""
    assert not called


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_the_kept_error_is_the_last_few_lines_only(prompt, tmp_path):
    noisy = "".join(f"line {n}\n" for n in range(1, 101))
    _, err, _ = _run(prompt, tmp_path, installed=False, install_ok=False, install_stderr=noisy)
    assert "line 100" in err
    assert "line 1\n" not in err and "line 50\n" not in err
    assert len(err.strip().splitlines()) <= 5


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_install_output_is_no_longer_discarded_and_gate_2_reports_it(prompt):
    text = prompt.read_text(encoding="utf-8")
    assert "uv python install 3.11 >/dev/null 2>&1" not in text
    abort = text.split('abort `Failing gate: Gate 2` with "bridge shell has no Python 3.11"', 1)[1][:400]
    assert "UV_ERR" in abort


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_a_failed_install_prints_the_error_itself(prompt, tmp_path):
    """Each device_bash call is a fresh shell and a failed opener skips the rest of its call, so the error
    must reach the session through the opener's own output, not only through $UV_ERR."""
    home = tmp_path / "home"
    (home / "mnt/all-stock-dca").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_uv(bin_dir, installed=False, install_ok=False)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    done = subprocess.run(
        ["bash", "-c", _opener(prompt, sandbox) + " && echo LATER-STEP-RAN"],
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin",
             "INSTALL_STDERR": "error: No space left on device (os error 28)\n"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert "No space left on device" in done.stdout + done.stderr
    assert "LATER-STEP-RAN" not in done.stdout  # a missing interpreter still stops the call


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7
