"""``deploy/launchd/install_recommendations_publish.sh``: the one command that installs the publish agent on the Mac.

The agent itself is pinned by ``test_recommendations_publish_launchd.py``. These pin the installer around it: it renders
that template with the checkout, interpreter and cron root, refuses anything that would leave a broken or silent agent,
and is idempotent.

``launchctl`` and ``plutil`` exist only on macOS. Dry runs need neither. The real install path runs here against fakes
on ``PATH`` (``uname`` reporting Darwin, a ``git`` and a ``launchctl`` that log their calls) and a logging wrapper
around the interpreter, so the order of unload, publish and load is pinned without a Mac (PR #1972 review).
"""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy/launchd/install_recommendations_publish.sh"
LABEL = "com.all-stock-dca.recommendations-publish"
SECRET = "ghp_supersecrettoken123"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "DCA_PUBLISH_PYTHON": sys.executable,
        "DCA_DISABLE_DOTENV": "1",
        **extra,
    }


def _run(tmp_path: Path, *args: str, env: dict[str, str] | None = None, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], env=env or _env(tmp_path, **extra), capture_output=True,
                          text=True, timeout=120)


def _sandbox_plist(tmp_path: Path) -> Path:
    return tmp_path / "tmp" / "dca-launch-agents-dry-run" / f"{LABEL}.plist"


def _cron(tmp_path: Path, name: str = "cron tracking") -> Path:
    root = tmp_path / name  # a space, as in ~/Library/Mobile Documents/...
    (root / "recommendations").mkdir(parents=True)
    return root


# --- dry runs -------------------------------------------------------------------------------------------------------

def test_dry_run_renders_the_agent_with_this_checkout_interpreter_and_cron_root(tmp_path):
    cron = _cron(tmp_path)
    done = _run(tmp_path, "--dry-run", "--cron-root", str(cron))
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads(_sandbox_plist(tmp_path).read_bytes())
    assert agent["Label"] == LABEL
    assert agent["WorkingDirectory"] == str(REPO)
    assert agent["ProgramArguments"] == [str(Path(sys.executable)), "-m", "scripts.snapshot_cycle", "publish", "--notify"]
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(cron)
    assert "launchctl bootstrap" in done.stdout  # printed, not run


def test_the_cron_root_defaults_to_the_environment(tmp_path):
    cron = _cron(tmp_path)
    done = _run(tmp_path, "--dry-run", DCA_CRON_TRACKING_ROOT=str(cron))
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads(_sandbox_plist(tmp_path).read_bytes())
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(cron)


def test_a_cron_root_without_a_recommendation_store_is_refused(tmp_path):
    """Pointing the agent at the wrong tree would publish nothing forever while logging NONE."""
    empty = tmp_path / "empty"
    empty.mkdir()
    done = _run(tmp_path, "--dry-run", "--cron-root", str(empty))
    assert done.returncode != 0
    assert "recommendations" in done.stderr
    assert not _sandbox_plist(tmp_path).exists()


def test_a_path_that_cannot_be_written_into_xml_is_refused(tmp_path):
    done = _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path, "a&b")))
    assert done.returncode != 0
    assert not _sandbox_plist(tmp_path).exists()


def test_a_missing_python_is_refused(tmp_path):
    done = _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path)),
                DCA_PUBLISH_PYTHON=str(tmp_path / "no-such-python"))
    assert done.returncode != 0
    assert "python" in done.stderr.lower()


def test_installing_twice_replaces_the_agent(tmp_path):
    assert _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path))).returncode == 0
    other = _cron(tmp_path, "other")
    done = _run(tmp_path, "--dry-run", "--cron-root", str(other))
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads(_sandbox_plist(tmp_path).read_bytes())
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(other)
    assert "launchctl bootout" in done.stdout  # an already-loaded agent is unloaded first


def test_dry_run_uninstall_removes_only_the_sandbox_copy(tmp_path):
    assert _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path))).returncode == 0
    done = _run(tmp_path, "--dry-run", "--uninstall")
    assert done.returncode == 0, done.stderr
    assert not _sandbox_plist(tmp_path).exists()
    assert "launchctl bootout" in done.stdout


def test_a_dry_run_never_touches_the_real_launch_agents_folder(tmp_path):
    """launchd loads every plist in ~/Library/LaunchAgents at login, so a 'dry' render there would be a real install."""
    done = _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path)))
    assert done.returncode == 0, done.stderr
    assert not list((tmp_path / "home").rglob("*.plist"))
    assert _sandbox_plist(tmp_path).is_file()


def test_a_dry_run_ignores_an_agents_override_that_points_at_the_real_folder(tmp_path):
    """PR #1972 review: the override is for real-path tests; a dry run must not write or delete through it."""
    real = tmp_path / "home" / "Library" / "LaunchAgents"
    real.mkdir(parents=True)
    existing = real / f"{LABEL}.plist"
    existing.write_text("the operator's live agent\n")
    for args in (("--dry-run", "--cron-root", str(_cron(tmp_path))), ("--dry-run", "--uninstall")):
        done = _run(tmp_path, *args, DCA_LAUNCH_AGENTS_DIR=str(real))
        assert done.returncode == 0, done.stderr
        assert existing.read_text() == "the operator's live agent\n"
        assert [path.name for path in real.iterdir()] == [existing.name]


def test_an_unknown_option_is_refused(tmp_path):
    done = _run(tmp_path, "--bogus")
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


def test_without_dry_run_it_refuses_to_run_off_macos(tmp_path):
    if sys.platform == "darwin":
        return  # on a Mac a real run would load the agent; this check only guards other systems
    done = _run(tmp_path, "--cron-root", str(_cron(tmp_path)))
    assert done.returncode != 0
    assert "macOS" in done.stderr


# --- the real install path, against fakes ---------------------------------------------------------------------------

def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _mac(tmp_path: Path, *, loaded: bool = False, git_exit: int = 0, publish_exit: int | None = None):
    """A fake Mac: Darwin ``uname``, logging ``git``/``launchctl``, and a logging interpreter wrapper."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    state = tmp_path / "loaded"
    if loaded:
        state.touch()
    _script(bin_dir / "uname", 'echo Darwin\n')
    _script(bin_dir / "git", f'echo "git $*" >> "{log}"\necho "fatal: https://user:{SECRET}@github.com denied" >&2\n'
                             f'exit {git_exit}\n')
    _script(bin_dir / "launchctl", f'echo "launchctl $*" >> "{log}"\n'
                                   f'case "$1" in print) [ -f "{state}" ] ;; bootout) rm -f "{state}" ;;'
                                   f' bootstrap) touch "{state}" ;; esac\n')
    fail = f'[ "$*" = "-m scripts.snapshot_cycle publish" ] && exit {publish_exit}\n' if publish_exit is not None else ""
    python = _script(bin_dir / "python", f'echo "python $*" >> "{log}"\n{fail}exec "{sys.executable}" "$@"\n')
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    env = _env(tmp_path, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", DCA_PUBLISH_PYTHON=str(python),
               DCA_LAUNCH_AGENTS_DIR=str(agents))
    return env, log, state, agents / f"{LABEL}.plist", python


def _calls(log: Path) -> list[str]:
    return [line.split(" ", 2)[0] + " " + line.split(" ", 2)[1] for line in log.read_text().splitlines()]


def test_a_real_install_loads_an_agent_that_runs_the_checked_interpreter(tmp_path):
    """PR #1972 review: preflight and the agent must use the same interpreter, or an install can pass and never start."""
    env, log, state, target, python = _mac(tmp_path)
    done = _run(tmp_path, "--cron-root", str(_cron(tmp_path)), env=env)
    assert done.returncode == 0, done.stderr
    assert plistlib.loads(target.read_bytes())["ProgramArguments"][0] == str(python)
    assert state.exists()
    assert "launchctl bootstrap" in _calls(log)


def test_a_reinstall_stops_the_old_agent_before_its_manual_publish(tmp_path):
    """PR #1972 review: two publishers force-pushing at once could leave the branch on the old root's data."""
    env, log, state, target, _ = _mac(tmp_path, loaded=True)
    target.parent.mkdir(parents=True)
    target.write_text("old agent\n")
    done = _run(tmp_path, "--cron-root", str(_cron(tmp_path)), env=env)
    assert done.returncode == 0, done.stderr
    calls = _calls(log)
    publish = next(i for i, line in enumerate(log.read_text().splitlines()) if "snapshot_cycle publish" in line)
    assert calls.index("launchctl bootout") < publish < calls.index("launchctl bootstrap")


def test_a_failed_manual_publish_restores_the_old_agent(tmp_path):
    env, log, state, target, _ = _mac(tmp_path, loaded=True, publish_exit=2)
    target.parent.mkdir(parents=True)
    target.write_text("old agent\n")
    done = _run(tmp_path, "--cron-root", str(_cron(tmp_path)), env=env)
    assert done.returncode != 0
    assert target.read_text() == "old agent\n"
    assert state.exists()  # bootstrapped again: the old agent keeps publishing
    assert _calls(log)[-1] == "launchctl bootstrap"


def test_a_failed_push_check_never_prints_gits_own_output(tmp_path):
    """PR #1972 review: git's stderr can carry a token embedded in the remote URL (dca_rec_publish captures it too)."""
    env, log, state, target, _ = _mac(tmp_path, git_exit=128)
    done = _run(tmp_path, "--cron-root", str(_cron(tmp_path)), env=env)
    assert done.returncode != 0
    assert SECRET not in done.stdout + done.stderr
    assert "credential" in done.stderr
    assert not target.exists() and not state.exists()
