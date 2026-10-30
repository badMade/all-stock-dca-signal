"""Regression coverage for the sourced bridge Python bootstrap (Key bridge_python_alert_channels, PR #2032)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/bridge_bootstrap.sh"
SHELLS = ["sh"] + (["dash"] if shutil.which("dash") else [])


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\n"
        "case $* in\n"
        "  *'print('* ) printf '%s\\n' \"${FAKE_VERSION:-3.11.8}\" ;;\n"
        "  * ) case ${FAKE_VERSION:-3.11.8} in 3.10*) exit 1 ;; esac ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    uv = bindir / "uv"
    uv.write_text("#!/bin/sh\nif test \"$2\" = find; then printf '%s\\n' \"$FAKE_PYTHON\"; test -n \"$FAKE_PYTHON\"; else exit 0; fi\n", encoding="utf-8")
    uv.chmod(0o755)
    return bindir, python


def _run(tmp_path: Path, shell: str, *, find_path: Path | None, version: str = "3.11.8", prelude: str = ""):
    bindir, python = _fixture(tmp_path)
    env = os.environ.copy()
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        DCA_BRIDGE_UV_BASE=str(tmp_path / "base"),
        FAKE_PYTHON=str(find_path or ""),
        FAKE_VERSION=version,
    )
    code = f'{prelude}\n. "{SCRIPT}"; status=$?; printf "PY:%s\\n" "${{PY-}}"; printf "ERROR:%s\\n" "${{BRIDGE_BOOTSTRAP_ERROR-}}"; printf "UVPY:%s\\n" "${{UV_PYTHON_INSTALL_DIR-}}"; exit "$status"'
    return subprocess.run([shell, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True), python, env


@pytest.mark.parametrize("shell", SHELLS)
def test_happy_path_exports_paths_and_python(tmp_path: Path, shell: str) -> None:
    result, python, _ = _run(tmp_path, shell, find_path=tmp_path / "python")
    assert result.returncode == 0, result.stderr
    assert f"BRIDGE_PY: {python} 3.11.8" in result.stdout
    assert f"PY:{python}" in result.stdout
    assert "ERROR:\n" in result.stdout
    assert "PYTHONDONTWRITEBYTECODE" not in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_find_miss_installs_then_finds_python(tmp_path: Path, shell: str) -> None:
    bindir, python = _fixture(tmp_path)
    uv = bindir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if test \"$2\" = find; then test -f \"$INSTALL_MARKER\" || exit 1; printf '%s\\n' \"$FAKE_PYTHON\"; else touch \"$INSTALL_MARKER\"; fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", DCA_BRIDGE_UV_BASE=str(tmp_path / "base"), FAKE_PYTHON=str(python), INSTALL_MARKER=str(tmp_path / "installed"))
    result = subprocess.run([shell, "-c", f'. "{SCRIPT}"'], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"BRIDGE_PY: {python}" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_install_failure_reports_error_and_clears_python(tmp_path: Path, shell: str) -> None:
    bindir, _ = _fixture(tmp_path)
    (bindir / "uv").write_text("#!/bin/sh\nif test \"$2\" = find; then exit 1; fi\nprintf 'a\\nb\\nc\\nd\\ne\\nf\\n'\nexit 1\n", encoding="utf-8")
    (bindir / "uv").chmod(0o755)
    result, _, _ = _run_custom_base(tmp_path, shell, bindir, tmp_path / "python")
    assert result.returncode != 0
    assert "UV_INSTALL_ERROR:" in result.stderr and "f" in result.stderr
    assert "PY:\n" in result.stdout


def _run_custom_base(tmp_path: Path, shell: str, bindir: Path, python: Path, base: Path | None = None, version: str = "3.11.8"):
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", DCA_BRIDGE_UV_BASE=str(base or tmp_path / "base"), FAKE_PYTHON=str(python), FAKE_VERSION=version)
    code = f'. "{SCRIPT}"; status=$?; printf "PY:%s\\n" "${{PY-}}"; printf "ERROR:%s\\n" "${{BRIDGE_BOOTSTRAP_ERROR-}}"; exit "$status"'
    return subprocess.run([shell, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True), python, env


@pytest.mark.parametrize("shell", SHELLS)
def test_python_below_floor_fails_closed(tmp_path: Path, shell: str) -> None:
    bindir, python = _fixture(tmp_path)
    result, _, _ = _run_custom_base(tmp_path, shell, bindir, python, version="3.10.14")
    assert result.returncode != 0
    assert "BRIDGE_PY:" not in result.stdout
    assert "PY:\n" in result.stdout
    assert "ERROR:Python 3.11 or newer is required" in result.stdout


@pytest.mark.skipif(os.getuid() == 0, reason="root can write through mode 0555")
@pytest.mark.parametrize("shell", SHELLS)
def test_unwritable_base_falls_back_and_announces_path(tmp_path: Path, shell: str) -> None:
    base = tmp_path / "base"
    locked = base / f"uv-{os.getuid()}"
    locked.mkdir(parents=True)
    locked.chmod(0o555)
    result, _, _ = _run(tmp_path, shell, find_path=tmp_path / "python")
    assert result.returncode == 0, result.stderr
    assert f"UVPY:{base}/uv-{os.getuid()}-alt/python" in result.stdout


@pytest.mark.skipif(os.getuid() == 0, reason="root can write through mode 0555")
@pytest.mark.parametrize("shell", SHELLS)
def test_locked_base_reuses_stable_alt_when_sourced_twice(tmp_path: Path, shell: str) -> None:
    base = tmp_path / "base"
    (base / f"uv-{os.getuid()}").mkdir(parents=True)
    (base / f"uv-{os.getuid()}").chmod(0o555)
    bindir, python = _fixture(tmp_path)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", DCA_BRIDGE_UV_BASE=str(base), FAKE_PYTHON=str(python))
    code = f'. "{SCRIPT}" || exit; first=$UV_PYTHON_INSTALL_DIR; . "{SCRIPT}" || exit; printf "FIRST:%s\\nSECOND:%s\\n" "$first" "$UV_PYTHON_INSTALL_DIR"'
    result = subprocess.run([shell, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    stable = base / f"uv-{os.getuid()}-alt/python"
    assert result.returncode == 0, result.stderr
    assert f"FIRST:{stable}" in result.stdout and f"SECOND:{stable}" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_locked_base_and_alt_use_mktemp_fallback(tmp_path: Path, shell: str) -> None:
    base = tmp_path / "base"
    for name in (f"uv-{os.getuid()}", f"uv-{os.getuid()}-alt"):
        locked = base / name
        locked.mkdir(parents=True)
        locked.chmod(0o555)
    result, _, _ = _run(tmp_path, shell, find_path=tmp_path / "python")
    assert result.returncode == 0, result.stderr
    assert "BRIDGE_UV_FALLBACK:" in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_impossible_base_fails_with_uv_error(tmp_path: Path, shell: str) -> None:
    base = tmp_path / "base"
    base.write_text("file blocks directory creation", encoding="utf-8")
    bindir, python = _fixture(tmp_path)
    result, _, _ = _run_custom_base(tmp_path, shell, bindir, python, base=base)
    assert result.returncode == 1
    assert "BRIDGE_UV_ERROR: no writable uv directory" in result.stderr
    assert "ERROR:no writable uv directory" in result.stdout
    assert "PY:\n" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_syntax_and_option_preservation(tmp_path: Path, shell: str) -> None:
    syntax = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
    text = SCRIPT.read_text(encoding="utf-8")
    for token in ("[[", "$'", "local "):
        assert token not in text
    bindir, python = _fixture(tmp_path)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", DCA_BRIDGE_UV_BASE=str(tmp_path / "base"), FAKE_PYTHON=str(python))
    result = subprocess.run([shell, "-c", f'set -e; . "{SCRIPT}"; case $- in *e*) : ;; *) exit 9 ;; esac'], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# 2026-10-30: `test -w` alone accepted a world-writable dir another uid owns -- the 2026-10-28 `nobody`
# cache shape -- whose owner could swap the interpreter `uv python find` returns. Ownership cannot be faked
# with chown as an ordinary user, so a fake `ls` reports uid 65534 for each colon-separated $FAKE_FOREIGN
# path (and anything under it); everything else goes to the real ls.
_FAKE_LS = r"""#!/bin/sh
last=
for a in "$@"; do last=$a; done
rest=${FAKE_FOREIGN-}
while [ -n "$rest" ]; do
    dir=${rest%%:*}
    case "$rest" in *:*) rest=${rest#*:} ;; *) rest= ;; esac
    case "$last" in "$dir"|"$dir"/*) echo "drwxrwxrwx 2 65534 65534 64 Oct 28 15:30 $last"; exit 0 ;; esac
done
exec REAL_LS "$@"
"""


def _run_foreign(tmp_path: Path, shell: str, foreign: list[Path]):
    bindir, python = _fixture(tmp_path)
    fake_ls = bindir / "ls"
    fake_ls.write_text(_FAKE_LS.replace("REAL_LS", shutil.which("ls")), encoding="utf-8")
    fake_ls.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", DCA_BRIDGE_UV_BASE=str(tmp_path / "base"), FAKE_PYTHON=str(python),
               FAKE_FOREIGN=":".join(map(str, foreign)))
    code = f'. "{SCRIPT}"; status=$?; printf "UVPY:%s\\n" "${{UV_PYTHON_INSTALL_DIR-}}"; exit "$status"'
    return subprocess.run([shell, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("shell", SHELLS)
def test_writable_pinned_dir_owned_by_another_uid_falls_back_to_alt(tmp_path: Path, shell: str) -> None:
    pinned = tmp_path / "base" / f"uv-{os.getuid()}"
    pinned.mkdir(parents=True)
    result = _run_foreign(tmp_path, shell, [pinned])
    assert result.returncode == 0, result.stderr
    assert f"UVPY:{pinned}-alt/python" in result.stdout
    assert f"BRIDGE_UV_FALLBACK: {pinned}-alt" in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_foreign_subdir_under_an_owned_pinned_dir_is_rejected(tmp_path: Path, shell: str) -> None:
    pinned = tmp_path / "base" / f"uv-{os.getuid()}"
    (pinned / "python").mkdir(parents=True)
    result = _run_foreign(tmp_path, shell, [pinned / "python"])
    assert result.returncode == 0, result.stderr
    assert f"UVPY:{pinned}-alt/python" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_foreign_pinned_and_alt_use_an_owned_mktemp_dir(tmp_path: Path, shell: str) -> None:
    """A planted world-writable uv-<uid>-alt must not be adopted either."""
    pinned = tmp_path / "base" / f"uv-{os.getuid()}"
    alt = tmp_path / "base" / f"uv-{os.getuid()}-alt"
    pinned.mkdir(parents=True)
    alt.mkdir()
    result = _run_foreign(tmp_path, shell, [pinned, alt])
    assert result.returncode == 0, result.stderr
    used = result.stdout.split("UVPY:", 1)[1].strip()
    assert used.startswith(f"{pinned}-") and not used.startswith(str(alt))
    assert "BRIDGE_UV_FALLBACK:" in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_owned_pinned_dir_is_used_without_fallback(tmp_path: Path, shell: str) -> None:
    pinned = tmp_path / "base" / f"uv-{os.getuid()}"
    pinned.mkdir(parents=True)
    result = _run_foreign(tmp_path, shell, [])
    assert result.returncode == 0, result.stderr
    assert f"UVPY:{pinned}/python" in result.stdout
    assert "BRIDGE_UV_FALLBACK" not in result.stderr
