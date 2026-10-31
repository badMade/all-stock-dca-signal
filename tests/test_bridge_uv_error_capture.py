"""The live prompts' BRIDGE PROTOCOL opener must keep the reason ``uv python install 3.11`` failed.

2026-10-21: the 15:30 slot aborted at Gate 2 on Oct 13, 14, 15 and 16 with "bridge shell has no Python
3.11", and every alert could only say the install "produced nothing": the opener ran
``uv python install 3.11 >/dev/null 2>&1``, so a full disk, a blocked download and a TLS failure all looked
identical.

2026-10-29: the inline opener moved into ``scripts/bridge_python.sh`` (its own behaviour, including the
fallback dir, is pinned by ``scripts/tests/test_bridge_python.py``). These tests run each live prompt's
opener EXACTLY as written, against a checkout holding the real script and a fake ``uv``, and require that
the opener sets ``$PY`` on success, surfaces uv's own error on failure, and stops the rest of the call.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIVE_MIRROR = REPO / "claude/dca-executor-prompts/live-mirror"
# The folder also holds a *.template.txt that is not a slot prompt; the seven slots are counted below so a
# prompt that loses its opener cannot silently drop out of coverage.
PROMPTS = sorted(p for p in LIVE_MIRROR.glob("*.txt") if not p.name.endswith(".template.txt"))
OPENER = re.compile(r"begin EVERY call with: `([^`]*)`")


def _opener(prompt: Path) -> str:
    match = OPENER.search(prompt.read_text(encoding="utf-8"))
    assert match, f"{prompt.name}: BRIDGE PROTOCOL opener not found"
    return match.group(1)


def _fake_uv(bin_dir: Path, *, installed: bool, install_ok: bool) -> Path:
    called = bin_dir / "install-called"
    script = bin_dir / "uv"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "python find" ]; then [ -e "$UV_PYTHON_INSTALL_DIR/py" ] && '
        'echo "$UV_PYTHON_INSTALL_DIR/py" && exit 0; exit 1; fi\n'
        f'if [ "$1 $2" = "python install" ]; then touch "{called}"; printf %s "$INSTALL_STDERR" >&2; '
        f'{"ln -s " + sys.executable + " $UV_PYTHON_INSTALL_DIR/py; exit 0" if install_ok else "exit 1"}; fi\n'
        "exit 2\n"
    )
    script.chmod(0o755)
    if installed:
        (bin_dir / "preinstall").write_text("")
    return called


def _run(prompt: Path, tmp_path: Path, tail: str, *, installed: bool, install_ok: bool,
         install_stderr: str = "") -> tuple[subprocess.CompletedProcess, bool]:
    home = tmp_path / "home"
    checkout = home / "mnt/all-stock-dca"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts/bridge_python.sh", checkout / "scripts/bridge_python.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = _fake_uv(bin_dir, installed=installed, install_ok=install_ok)
    base = tmp_path / "tmpbase"
    base.mkdir()
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "INSTALL_STDERR": install_stderr,
           # The script pins /tmp/uv-<uid>; point it at the sandbox so the test is hermetic.
           "DCA_BRIDGE_UV_BASE": str(base)}
    if installed:
        assert subprocess.run(["bash", "-c", _opener(prompt)], env={**env, "INSTALL_STDERR": ""},
                              capture_output=True, check=False).returncode == 0
        called.unlink()
    done = subprocess.run(["bash", "-c", _opener(prompt) + tail], env=env,
                          capture_output=True, text=True, timeout=30, check=False)
    return done, called.exists()


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_the_opener_calls_the_checked_in_script_instead_of_inline_uv(prompt):
    opener = _opener(prompt)
    assert "sh scripts/bridge_python.sh" in opener
    assert opener.startswith("cd $HOME/mnt/all-stock-dca && ")
    assert "uv python" not in opener


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_a_failed_install_prints_the_error_and_stops_the_call(prompt, tmp_path):
    """Each device_bash call is a fresh shell and a failed opener skips the rest of its call, so the error
    must reach the session through the opener's own output."""
    done, _ = _run(prompt, tmp_path, " && echo LATER-STEP-RAN", installed=False, install_ok=False,
                   install_stderr="error: No space left on device (os error 28)\n")
    assert "UV_INSTALL_ERROR: error: No space left on device (os error 28)" in done.stderr
    assert "BRIDGE_PYTHON_ERROR:" in done.stderr
    assert "LATER-STEP-RAN" not in done.stdout  # a missing interpreter still stops the call


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_a_successful_install_sets_py(prompt, tmp_path):
    done, called = _run(prompt, tmp_path, ' && printf "PY=%s\\n" "$PY" && "$PY" -c "print(42)"',
                        installed=False, install_ok=True)
    assert done.returncode == 0, done.stderr
    assert re.search(r"^PY=/.*/py$", done.stdout, re.M)
    assert "42" in done.stdout
    assert called


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_an_existing_interpreter_is_reused_without_installing(prompt, tmp_path):
    done, called = _run(prompt, tmp_path, ' && printf "PY=%s\\n" "$PY"', installed=True, install_ok=True)
    assert done.returncode == 0, done.stderr
    assert "PY=/" in done.stdout
    assert not called


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_install_output_is_no_longer_discarded_and_gate_2_reports_it(prompt):
    text = prompt.read_text(encoding="utf-8")
    assert "uv python install 3.11 >/dev/null 2>&1" not in text
    abort = text.split('abort `Failing gate: Gate 2` with "bridge shell has no Python 3.11"', 1)[1][:600]
    assert "UV_INSTALL_ERROR" in abort and "BRIDGE_PYTHON_ERROR" in abort
    # The no-Python abort writes its durable alert file before anything else (2026-10-29).
    assert "--reason GATE2_ABORT_NO_PYTHON311" in abort


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_off_window_and_no_python_aborts_write_the_alert_file_first_without_python(prompt):
    text = prompt.read_text(encoding="utf-8")
    rule = text.split("TRUNCATION-SAFE EVIDENCE-FREE ABORT RULE", 1)[1].split("\n", 1)[0]
    slot = prompt.name[:4]
    assert f"sh scripts/record_evidence_free_abort.sh --slot {slot} " in rule
    assert "GATE1_OFF_WINDOW" in rule and "GATE2_ABORT_NO_PYTHON311" in rule
    assert "<<'ALERT'" in rule and "FIRST" in rule
    # Truncation-safe: it sits ahead of Gate 0, not buried in Gate 9.
    assert text.index("TRUNCATION-SAFE EVIDENCE-FREE ABORT RULE") < text.index("GATE 0")


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7
