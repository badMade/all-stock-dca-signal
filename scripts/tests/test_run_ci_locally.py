"""Guards for scripts/run_ci_locally.sh (docs/local_ci.md).

The script maps exactly one runner label to its act image. If ci.yml moves a job
to another label, act would fall back to its own default image (or prompt for
one) and the local run would stop matching CI without any error saying so.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/run_ci_locally.sh"
CI = REPO_ROOT / ".github/workflows/ci.yml"


def _mapped_labels() -> set[str]:
    return set(re.findall(r"-P ([A-Za-z0-9._-]+)=", SCRIPT.read_text(encoding="utf-8")))


def test_every_ci_runner_label_is_mapped_to_the_local_image() -> None:
    labels = set(re.findall(r"^\s+runs-on:\s*(\S+)\s*$", CI.read_text(encoding="utf-8"), flags=re.M))
    assert labels, "no runs-on labels found in ci.yml"
    assert labels <= _mapped_labels(), f"unmapped runner label(s): {sorted(labels - _mapped_labels())}"


def test_runner_image_dockerfile_exists_and_adds_gh() -> None:
    dockerfile = (REPO_ROOT / "deploy/act/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("#") and "FROM catthehacker/ubuntu:act-24.04" in dockerfile
    assert "apt-get install -y --no-install-recommends gh" in dockerfile
    # Steps run as this user (the script's --user 1001:1001), as on GitHub's runners.
    assert "useradd -m -u 1001 -s /bin/bash runner" in dockerfile


def test_script_runs_act_from_a_clean_clone_as_the_runner_user() -> None:
    # A linked worktree's `.git` file points outside the container (14 of 17
    # failures on the first local run); root ignored read-only modes (3 more).
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'git clone -q "$repo_root" "$snapshot"' in script
    assert 'cd "$snapshot"' in script
    assert '--container-options "--user 1001:1001"' in script


@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_script_parses_under_posix_shells(shell: str) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    subprocess.run([shell, "-n", str(SCRIPT)], check=True)


def test_help_exits_zero_before_touching_any_tool() -> None:
    # --help is handled before the tool check, so it works on a machine without act.
    result = subprocess.run(["sh", str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert "act + Colima" in result.stdout


def test_bad_pr_flag_is_a_usage_error() -> None:
    result = subprocess.run(["sh", str(SCRIPT), "--pr"], capture_output=True, text=True)
    assert result.returncode == 2
