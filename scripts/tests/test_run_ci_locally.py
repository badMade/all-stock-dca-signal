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


def _runner_labels(workflow: str) -> set[str]:
    """The label each job lands on under act.

    A plain ``runs-on: ubuntu-26.04`` is the label itself. PR #2072 makes it
    ``${{ vars.CI_RUNS_ON || 'ubuntu-26.04' }}``; act does not read repository
    variables, so locally that resolves to the quoted fallback, which is what
    must be mapped.
    """
    labels = set()
    for value in re.findall(r"^\s+runs-on:\s*(.+?)\s*$", workflow, flags=re.M):
        if value.startswith("${{"):
            fallback = re.search(r"\|\|\s*'([^']+)'\s*}}$", value)
            assert fallback, f"runs-on expression without a quoted fallback label: {value}"
            labels.add(fallback.group(1))
        else:
            labels.add(value)
    return labels


def test_every_ci_runner_label_is_mapped_to_the_local_image() -> None:
    labels = _runner_labels(CI.read_text(encoding="utf-8"))
    assert labels, "no runs-on labels found in ci.yml"
    assert labels <= _mapped_labels(), f"unmapped runner label(s): {sorted(labels - _mapped_labels())}"


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("    runs-on: ubuntu-26.04", "ubuntu-26.04"),
        ("    runs-on: ${{ vars.CI_RUNS_ON || 'ubuntu-26.04' }}", "ubuntu-26.04"),
    ],
)
def test_runner_label_reads_plain_and_variable_runs_on(line: str, label: str) -> None:
    assert _runner_labels(f"jobs:\n  test:\n{line}\n") == {label}


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


def test_report_refuses_to_describe_a_commit_it_did_not_run() -> None:
    # A status is a claim about one commit: a dirty tree or an unpushed HEAD
    # must be refused before the run, not discovered after it.
    script = SCRIPT.read_text(encoding="utf-8")
    assert "--report needs a clean tree" in script
    assert "--report needs HEAD on GitHub" in script


def test_report_parses_acts_job_end_lines(tmp_path) -> None:
    # The same sed expression the script uses, run on act's real line shapes,
    # including the padded name and a matrix leg suffix.
    script = SCRIPT.read_text(encoding="utf-8")
    expression = re.search(r"sed -n -E '([^']+)'", script).group(1)
    log = tmp_path / "act.log"
    log.write_text(
        "[CI/Detect changed projects      ] 🏁  Job succeeded\n"
        "[CI/Dependency audit (research)-2] 🏁  Job failed\n"
        "[CI/Lint & test (core DCA)       ]   | 13207 passed\n",
        encoding="utf-8",
    )
    result = subprocess.run(["sed", "-n", "-E", expression, str(log)], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [
        "succeeded|Detect changed projects      ",
        "failed|Dependency audit (research)-2",
    ]


def test_one_act_run_per_machine() -> None:
    # Concurrent act runs share container names and delete each other's
    # containers (exit 137 on 2026-10-31); the script must serialize them.
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'while ! mkdir "$lock" 2>/dev/null; do' in script
    assert "trap 'rm -rf \"$work\" \"$lock\"' EXIT" in script


def test_snapshot_keeps_a_loose_ref_so_git_works_inside_the_container() -> None:
    # 2026-10-31: a fresh clone packs every ref, act's copy drops the then-empty .git/refs, and git in the job said
    # "not a git repository": 12 tests that call git failed under act and nowhere else.
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'git -C "$snapshot" update-ref refs/heads/act-ci-snapshot "$head"' in script
    assert script.index("update-ref refs/heads/act-ci-snapshot") < script.index('cd "$snapshot"')
