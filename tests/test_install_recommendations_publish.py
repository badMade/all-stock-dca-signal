"""``deploy/launchd/install_recommendations_publish.sh``: the one command that installs the publish agent on the Mac.

The agent itself is pinned by ``test_recommendations_publish_launchd.py``. These pin the installer around it: it renders
that template with the checkout and cron root, refuses anything that would leave a broken or silent agent, and is
idempotent. ``launchctl`` and ``plutil`` exist only on macOS, so the tests run ``--dry-run``, which renders and
validates the plist and prints the commands it would run, but loads nothing and pushes nothing.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy/launchd/install_recommendations_publish.sh"
LABEL = "com.all-stock-dca.recommendations-publish"


def _run(tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "DCA_PUBLISH_PYTHON": sys.executable,
        "DCA_LAUNCH_AGENTS_DIR": str(tmp_path / "agents"),
        "DCA_DISABLE_DOTENV": "1",
        **(env_extra or {}),
    }
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def _cron(tmp_path: Path) -> Path:
    root = tmp_path / "cron tracking"  # a space, as in ~/Library/Mobile Documents/...
    (root / "recommendations").mkdir(parents=True)
    return root


def test_dry_run_renders_the_agent_with_this_checkout_and_the_given_cron_root(tmp_path):
    cron = _cron(tmp_path)
    done = _run(tmp_path, "--dry-run", "--cron-root", str(cron))
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads((tmp_path / "agents" / f"{LABEL}.plist").read_bytes())
    assert agent["Label"] == LABEL
    assert agent["WorkingDirectory"] == str(REPO)
    assert agent["ProgramArguments"][1:] == ["-m", "scripts.snapshot_cycle", "publish", "--notify"]
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(cron)
    assert "launchctl bootstrap" in done.stdout  # printed, not run


def test_the_cron_root_defaults_to_the_environment(tmp_path):
    cron = _cron(tmp_path)
    done = _run(tmp_path, "--dry-run", env_extra={"DCA_CRON_TRACKING_ROOT": str(cron)})
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads((tmp_path / "agents" / f"{LABEL}.plist").read_bytes())
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(cron)


def test_a_cron_root_without_a_recommendation_store_is_refused(tmp_path):
    """Pointing the agent at the wrong tree would publish nothing forever while logging NONE."""
    empty = tmp_path / "empty"
    empty.mkdir()
    done = _run(tmp_path, "--dry-run", "--cron-root", str(empty))
    assert done.returncode != 0
    assert "recommendations" in done.stderr
    assert not (tmp_path / "agents" / f"{LABEL}.plist").exists()


def test_a_path_that_cannot_be_written_into_xml_is_refused(tmp_path):
    bad = tmp_path / "a&b"
    (bad / "recommendations").mkdir(parents=True)
    done = _run(tmp_path, "--dry-run", "--cron-root", str(bad))
    assert done.returncode != 0
    assert not (tmp_path / "agents" / f"{LABEL}.plist").exists()


def test_a_missing_python_is_refused(tmp_path):
    done = _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path)),
                env_extra={"DCA_PUBLISH_PYTHON": str(tmp_path / "no-such-python")})
    assert done.returncode != 0
    assert "python" in done.stderr.lower()


def test_installing_twice_replaces_the_agent(tmp_path):
    cron = _cron(tmp_path)
    assert _run(tmp_path, "--dry-run", "--cron-root", str(cron)).returncode == 0
    other = tmp_path / "other"
    (other / "recommendations").mkdir(parents=True)
    done = _run(tmp_path, "--dry-run", "--cron-root", str(other))
    assert done.returncode == 0, done.stderr
    agent = plistlib.loads((tmp_path / "agents" / f"{LABEL}.plist").read_bytes())
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(other)
    assert "launchctl bootout" in done.stdout  # an already-loaded agent is unloaded first


def test_uninstall_removes_the_agent(tmp_path):
    assert _run(tmp_path, "--dry-run", "--cron-root", str(_cron(tmp_path))).returncode == 0
    done = _run(tmp_path, "--dry-run", "--uninstall")
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "agents" / f"{LABEL}.plist").exists()
    assert "launchctl bootout" in done.stdout


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


def test_a_dry_run_never_writes_into_the_real_launch_agents_folder(tmp_path):
    """launchd loads every plist in ~/Library/LaunchAgents at login, so a 'dry' render there would be a real install."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "DCA_PUBLISH_PYTHON": sys.executable,
           "DCA_DISABLE_DOTENV": "1", "TMPDIR": str(tmp_path / "tmp")}
    (tmp_path / "tmp").mkdir()
    done = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--cron-root", str(_cron(tmp_path))], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not list((tmp_path / "home").rglob("*.plist"))
    assert (tmp_path / "tmp" / "dca-launch-agents-dry-run" / f"{LABEL}.plist").is_file()
