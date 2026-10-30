"""Tests for scripts/bridge_python.sh: the one checked-in copy of the bridge VM's Python 3.11 bootstrap.

2026-10-28: the 11:30 and 3:30 slots aborted GATE2_ABORT_NO_PYTHON311 because uv's pinned /tmp cache
was owned by `nobody`. These tests run the real script against a fake ``uv`` on PATH and pin the way
out it now has: a pinned dir that is foreign-owned, read-only or not a directory falls back to a
``mktemp -d`` dir this uid owns, a later fresh shell reuses that dir instead of reinstalling, and every
failure exits nonzero with a reason (and uv's own last error lines) on stderr and nothing on stdout.

Ownership cannot be faked with chown as an ordinary user, so a fake ``ls`` reports uid 65534 (nobody)
for the paths named in $FAKE_FOREIGN -- the script reads owners through ``ls -ldn`` precisely because
``stat``'s flags differ between GNU and BSD.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "bridge_python.sh"
# dash is /bin/sh on the Ubuntu bridge VM; bash and the host's sh cover the rest.
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]

FAKE_UV = r"""#!/bin/sh
echo "uv $*" >> "$FAKE_UV_LOG"
if [ "$1 $2" = "python find" ]; then
    [ -e "$UV_PYTHON_INSTALL_DIR/py311" ] && { echo "$UV_PYTHON_INSTALL_DIR/py311"; exit 0; }
    exit 1
fi
if [ "$1 $2" = "python install" ]; then
    echo "install $UV_PYTHON_INSTALL_DIR" >> "$FAKE_UV_LOG"
    case "$UV_PYTHON_INSTALL_DIR" in "$FAKE_UV_FAIL_UNDER"/*)
        [ -n "$FAKE_UV_FAIL_UNDER" ] && { printf '%s' "$FAKE_UV_STDERR" >&2; exit 1; } ;;
    esac
    # uv refuses a cache it cannot write -- the 2026-10-28 failure.
    : > "$UV_CACHE_DIR/.probe" 2>/dev/null || { echo "error: failed to open $UV_CACHE_DIR: Permission denied" >&2; exit 2; }
    ln -s "$FAKE_PY_TARGET" "$UV_PYTHON_INSTALL_DIR/py311"
    exit 0
fi
exit 2
"""

# Reports each colon-separated path in $FAKE_FOREIGN (and anything under it) as owned by uid 65534;
# everything else goes to the real ls.
FAKE_LS = r"""#!/bin/sh
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


class Env:
    def __init__(self, tmp_path: Path, *, with_uv: bool = True):
        self.base = tmp_path / "tmpbase"
        self.base.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "uv.log"
        self.log.touch()
        if with_uv:
            self._exe("uv", FAKE_UV)
        self._exe("ls", FAKE_LS.replace("REAL_LS", shutil.which("ls")))
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DCA_BRIDGE_UV_BASE": str(self.base),
            "FAKE_UV_LOG": str(self.log),
            "FAKE_PY_TARGET": sys.executable,
            "TMPDIR": str(tmp_path / "systmp"),
        }
        (tmp_path / "systmp").mkdir()
        self.pinned = self.base / f"uv-{os.getuid()}"

    def _exe(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def run(self, shell: str = "sh", **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([shell, str(SCRIPT)], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=30, check=False)

    def installs(self) -> list[str]:
        return [line.split(" ", 1)[1] for line in self.log.read_text().splitlines() if line.startswith("install ")]

    def fallbacks(self) -> list[Path]:
        return sorted(self.base.glob(f"uv-{os.getuid()}-*"))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.mark.parametrize("shell", SHELLS)
def test_fresh_vm_installs_into_the_pinned_dir_and_prints_only_the_path(env, shell):
    done = env.run(shell)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{env.pinned}/python/py311\n"
    assert env.installs() == [str(env.pinned / "python")]
    assert env.fallbacks() == []
    assert "BRIDGE_PYTHON_ERROR" not in done.stderr


def test_an_existing_interpreter_is_reused_without_installing(env):
    assert env.run().returncode == 0
    second = env.run()
    assert second.returncode == 0
    assert second.stdout == f"{env.pinned}/python/py311\n"
    assert len(env.installs()) == 1


@pytest.mark.parametrize("shell", SHELLS)
def test_pinned_dir_owned_by_nobody_falls_back_to_an_owned_mktemp_dir(env, shell):
    """The 2026-10-28 shape: the pinned dir exists, is world-writable, and belongs to someone else."""
    env.pinned.mkdir()
    done = env.run(shell, FAKE_FOREIGN=str(env.pinned))
    assert done.returncode == 0, done.stderr
    [fallback] = env.fallbacks()
    assert done.stdout == f"{fallback}/python/py311\n"
    assert "owned by uid 65534" in done.stderr
    assert f"using {fallback}" in done.stderr
    assert env.installs() == [str(fallback / "python")]


def test_a_read_only_pinned_dir_falls_back(env):
    env.pinned.mkdir()
    env.pinned.chmod(0o555)
    try:
        done = env.run()
    finally:
        env.pinned.chmod(0o755)
    assert done.returncode == 0, done.stderr
    [fallback] = env.fallbacks()
    assert done.stdout.strip().startswith(str(fallback))
    assert "cannot be created" in done.stderr or "not writable" in done.stderr


def test_a_read_only_pinned_subdir_falls_back(env):
    """The cache dir alone being unwritable is enough to break uv, so every subdir is checked."""
    for sub in ("python", "cache", "tmp"):
        (env.pinned / sub).mkdir(parents=True)
    (env.pinned / "cache").chmod(0o555)
    try:
        done = env.run()
    finally:
        (env.pinned / "cache").chmod(0o755)
    assert done.returncode == 0, done.stderr
    assert f"{env.pinned}/cache is not writable" in done.stderr
    assert env.fallbacks()


def test_a_pinned_path_that_is_a_file_falls_back(env):
    env.pinned.write_text("not a dir")
    done = env.run()
    assert done.returncode == 0, done.stderr
    assert "is not a directory" in done.stderr


def test_a_later_fresh_shell_reuses_the_fallback_instead_of_reinstalling(env):
    env.pinned.mkdir()
    first = env.run(FAKE_FOREIGN=str(env.pinned))
    second = env.run(FAKE_FOREIGN=str(env.pinned))
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert len(env.fallbacks()) == 1
    assert len(env.installs()) == 1


def test_a_foreign_fallback_dir_is_not_reused(env):
    """A planted uv-<uid>-* dir someone else owns must not be picked up as this uid's fallback."""
    env.pinned.mkdir()
    planted = env.base / f"uv-{os.getuid()}-planted"
    planted.mkdir()
    done = env.run(FAKE_FOREIGN=f"{env.pinned}:{planted}")
    assert done.returncode == 0, done.stderr
    assert not done.stdout.startswith(str(planted))
    assert len(env.fallbacks()) == 2  # the planted one, plus the fresh mktemp dir actually used


def test_uv_failing_in_a_healthy_looking_pinned_dir_retries_once_in_a_fallback(env):
    done = env.run(FAKE_UV_FAIL_UNDER=str(env.pinned), FAKE_UV_STDERR="error: corrupt archive\n")
    assert done.returncode == 0, done.stderr
    [fallback] = env.fallbacks()
    assert done.stdout == f"{fallback}/python/py311\n"
    assert env.installs() == [str(env.pinned / "python"), str(fallback / "python")]


@pytest.mark.parametrize("shell", SHELLS)
def test_install_failing_everywhere_exits_5_with_uvs_last_lines_and_no_stdout(env, shell):
    noisy = "".join(f"line {n}\n" for n in range(1, 41)) + "error: No space left on device (os error 28)\n"
    done = env.run(shell, FAKE_UV_FAIL_UNDER=str(env.base), FAKE_UV_STDERR=noisy)
    assert done.returncode == 5
    assert done.stdout == ""
    assert "BRIDGE_PYTHON_ERROR: uv could not find or install Python 3.11" in done.stderr
    kept = [line for line in done.stderr.splitlines() if line.startswith("UV_INSTALL_ERROR: ")]
    assert kept[-1] == "UV_INSTALL_ERROR: error: No space left on device (os error 28)"
    assert len(kept) <= 5
    assert "line 1\n" not in done.stderr


def test_no_uv_on_path_exits_3(tmp_path):
    env = Env(tmp_path, with_uv=False)
    done = env.run(PATH=f"{env.bin}:/usr/bin:/bin")
    if shutil.which("uv", path="/usr/bin:/bin"):
        pytest.skip("host has a real uv on /usr/bin or /bin")
    assert done.returncode == 3
    assert done.stdout == ""
    assert "BRIDGE_PYTHON_ERROR: uv is not on PATH" in done.stderr


def test_no_writable_dir_anywhere_exits_4(env):
    env.base.chmod(0o555)
    try:
        done = env.run()
    finally:
        env.base.chmod(0o755)
    assert done.returncode == 4
    assert done.stdout == ""
    assert "BRIDGE_PYTHON_ERROR: pinned dir unusable" in done.stderr


def test_an_interpreter_older_than_3_11_exits_6(env, tmp_path):
    old = tmp_path / "python3.10"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    done = env.run(FAKE_PY_TARGET=str(old))
    assert done.returncode == 6
    assert done.stdout == ""
    assert "is not Python 3.11 or newer" in done.stderr


def test_the_script_is_posix_sh_with_no_bashisms():
    if not shutil.which("dash"):
        pytest.skip("dash not installed")
    assert subprocess.run(["dash", "-n", str(SCRIPT)], check=False).returncode == 0
    text = SCRIPT.read_text()
    for bashism in ("[[", "local ", "function ", "$'", "<<<", "pipefail"):
        assert bashism not in text, bashism
