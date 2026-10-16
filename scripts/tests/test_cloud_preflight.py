"""Tests for scripts/cloud_preflight.py: the one command a cloud-run Gate 2 calls.

Written first, from the behaviour the plan specifies, before any implementation.
A cloud container starts with a fresh checkout and an empty cron_tracking tree,
so this gate has to prove two things before a slot may trade: the code is the
release a human pinned (DCA_PINNED_SHA, held outside the repo), and the evidence
store was pulled and is complete enough to trust. Every refusal is a Gate 2
abort; nothing here ever installs a package.

Contract under test (exit 0 = ok, exit 2 = refused; the last stdout line is
``PREFLIGHT_OK <sha>`` or ``GATE2_ABORT: <one-line reason>``):

    main(["--repo-root", R, "--evidence-remote", URL, "--evidence-root", ROOT,
          "--trading-day", "YYYY-MM-DD", ["--init"], ["--expect-prior-slots", "0935,1030"]],
         env=ENV) -> int

The evidence remote's ``main`` carries ``MANIFEST.json``:
``{"version": 1, "days": {"<day>": {"slots": ["0935", ...]}}}``.
"""

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evidence_fixtures import (  # noqa: E402
    ACCOUNT,
    base_env,
    git,
    make_code_origin,
    make_evidence_origin,
)

import dca_paths  # noqa: E402
from scripts import cloud_preflight, evidence_publish  # noqa: E402
from scripts import finalize_run_evidence as fre  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-09-29"
ABBREVIATED = "abc1234"  # a short SHA; kept non-numeric so no account-shaped literal lands in the tree


@pytest.fixture
def world(tmp_path):
    code_origin, checkout, sha = make_code_origin(tmp_path)
    evidence_origin = make_evidence_origin(tmp_path)
    return SimpleNamespace(
        tmp=tmp_path,
        code_origin=code_origin,
        checkout=checkout,
        sha=sha,
        evidence_origin=evidence_origin,
        evidence_root=tmp_path / "workspace" / "cron_tracking",
    )


def run_preflight(world, *extra, env=None, remote=None):
    argv = [
        "--repo-root", str(world.checkout),
        "--evidence-remote", str(remote if remote is not None else world.evidence_origin),
        "--evidence-root", str(world.evidence_root),
        "--trading-day", DAY,
        *extra,
    ]
    return cloud_preflight.main(argv, env=env if env is not None else base_env(DCA_PINNED_SHA=world.sha))


def last_line(capsys):
    return capsys.readouterr().out.strip().splitlines()[-1]


def test_passes_when_head_matches_the_pin_and_the_evidence_pull_is_complete(world, capsys):
    assert run_preflight(world) == 0
    assert last_line(capsys) == f"PREFLIGHT_OK {world.sha}"
    # git does not track empty directories, so a fresh pull has none of the stream
    # roots prior_reference_bindings aborts on; a verified pull must create them.
    for directory in dca_paths.STREAM_ARTIFACT_DIRS.values():
        assert (world.evidence_root / directory).is_dir(), directory
    assert (world.evidence_root / "MANIFEST.json").is_file()


def test_a_second_call_in_the_same_container_still_passes(world, capsys):
    assert run_preflight(world) == 0
    capsys.readouterr()
    assert run_preflight(world) == 0


@pytest.mark.parametrize("pin", [None, "", ABBREVIATED, "z" * 40, "A" * 40])
def test_refuses_a_missing_or_malformed_pin(world, capsys, pin):
    overrides = {} if pin is None else {"DCA_PINNED_SHA": pin}
    assert run_preflight(world, env=base_env(**overrides)) == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "DCA_PINNED_SHA" in line
    assert not world.evidence_root.exists()


def test_refuses_when_head_is_not_the_pinned_sha(world, capsys):
    other = "0" * 40
    assert run_preflight(world, env=base_env(DCA_PINNED_SHA=other)) == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "pinned" in line
    assert not world.evidence_root.exists()


def test_refuses_a_pinned_commit_that_is_not_on_origin_main(world, capsys):
    git(world.checkout, "checkout", "-b", "side")
    (world.checkout / "side.txt").write_text("unreviewed\n", encoding="utf-8")
    git(world.checkout, "add", "-A")
    git(world.checkout, "commit", "-m", "unreviewed local work")
    side_sha = git(world.checkout, "rev-parse", "HEAD")

    assert run_preflight(world, env=base_env(DCA_PINNED_SHA=side_sha)) == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "origin/main" in line


def test_refuses_a_modified_tracked_file(world, capsys):
    (world.checkout / "README.md").write_text("edited after the pin\n", encoding="utf-8")

    assert run_preflight(world) == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "modified" in line


def test_refuses_an_untracked_file_that_could_shadow_the_pinned_code(world, capsys):
    (world.checkout / "sitecustomize.py").write_text("raise SystemExit\n", encoding="utf-8")

    assert run_preflight(world) == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "untracked" in line


def test_accepts_an_ignored_file_in_the_checkout(world, capsys):
    # git's own exclude file stands in for .gitignore without changing the pinned commit.
    (world.checkout / ".git" / "info" / "exclude").write_text("*.log\n", encoding="utf-8")
    (world.checkout / "run.log").write_text("noise\n", encoding="utf-8")

    assert run_preflight(world) == 0


def test_refuses_when_the_evidence_remote_is_unreachable_and_creates_nothing(world, capsys):
    assert run_preflight(world, remote=world.tmp / "does-not-exist.git") == 2
    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "evidence" in line
    # An unreachable store must never look like an empty one.
    assert not world.evidence_root.exists()


def test_refuses_a_store_with_no_manifest_unless_a_human_passes_init(tmp_path, capsys):
    code_origin, checkout, sha = make_code_origin(tmp_path)
    empty_store = make_evidence_origin(tmp_path, seed_manifest=False)
    world = SimpleNamespace(
        checkout=checkout, sha=sha, evidence_origin=empty_store,
        evidence_root=tmp_path / "workspace" / "cron_tracking",
    )

    assert run_preflight(world) == 2
    assert "manifest" in last_line(capsys)
    assert not world.evidence_root.exists()

    assert run_preflight(world, "--init") == 0
    for directory in dca_paths.STREAM_ARTIFACT_DIRS.values():
        assert (world.evidence_root / directory).is_dir(), directory


@pytest.mark.parametrize(
    "manifest",
    [
        "not json",
        json.dumps([]),
        json.dumps({"version": 2, "days": {}}),
        json.dumps({"version": 1}),
        # A duplicate key is ambiguous completeness evidence, not the last one written (PR #1571 review).
        '{"version": 1, "days": {}, "days": {"2026-09-29": {"slots": ["0935"]}}}',
        '{"version": 1, "days": {"2026-09-29": {"slots": ["0935"], "slots": []}}}',
    ],
)
def test_refuses_an_unusable_manifest(tmp_path, capsys, manifest):
    code_origin, checkout, sha = make_code_origin(tmp_path)
    store = make_evidence_origin(tmp_path, seed_manifest=False)
    seed = tmp_path / "corrupt_seed"
    git(tmp_path, "clone", str(store), str(seed))
    git(seed, "checkout", "-B", "main")
    (seed / "MANIFEST.json").write_text(manifest, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "corrupt manifest")
    git(seed, "push", "-u", "origin", "main")
    world = SimpleNamespace(
        checkout=checkout, sha=sha, evidence_origin=store,
        evidence_root=tmp_path / "workspace" / "cron_tracking",
    )

    assert run_preflight(world) == 2
    assert "manifest" in last_line(capsys)


def previous_container_publishes(world, slot, tmp_name="first"):
    """What an earlier slot's container did: pull, run Gate 9 locally, publish to the store."""
    root = world.tmp / tmp_name / "cron_tracking"
    assert cloud_preflight.main(
        ["--repo-root", str(world.checkout), "--evidence-remote", str(world.evidence_origin),
         "--evidence-root", str(root), "--trading-day", DAY],
        env=base_env(DCA_PINNED_SHA=world.sha),
    ) == 0
    started = datetime(2026, 9, 29, int(slot[:2]), int(slot[2:]) + 2, tzinfo=ET)
    result = fre.finalize(slot, started, outcome="NO_ORDERS", order_submit_status="no_orders_submitted",
                          root=root, now=datetime(2026, 9, 29, 17, 0, tzinfo=ET))
    assert result["verified"] is True, result["errors"]
    assert evidence_publish.main(
        ["publish", "--evidence-root", str(root), "--remote", str(world.evidence_origin),
         "--trading-day", DAY, "--slot", slot, "--code-sha", world.sha,
         "--max-attempts", "1", "--backoff-seconds", "0"],
        env=base_env(DCA_PINNED_SHA=world.sha),
    ) == 0


def test_expected_prior_slots_must_all_be_published_with_verified_artifacts(world, capsys):
    previous_container_publishes(world, "0935")
    capsys.readouterr()

    assert run_preflight(world, "--expect-prior-slots", "0935") == 0
    capsys.readouterr()
    for missing in ("0935,1030", "1330"):
        assert run_preflight(world, "--expect-prior-slots", missing) == 2
        line = last_line(capsys)
        assert line.startswith("GATE2_ABORT:") and "prior slot" in line


@pytest.mark.parametrize("artifacts", ["absent", "empty_run_dir"])
def test_a_manifest_entry_without_the_slots_artifacts_is_refused(tmp_path, capsys, artifacts):
    # The manifest alone is not proof: load_reference_bindings() treats an existing but empty stream
    # root as a complete scan, so a later slot would trade without the prior allocation evidence.
    code_origin, checkout, sha = make_code_origin(tmp_path)
    store = make_evidence_origin(tmp_path, manifest={"version": 1, "days": {DAY: {"slots": ["0935"]}}})
    if artifacts == "empty_run_dir":
        seed = tmp_path / "seed"
        git(tmp_path, "clone", str(store), str(seed))
        git(seed, "checkout", "-B", "main")
        run_dir = seed / "market_open_dca" / "20260929_0935ET"
        run_dir.mkdir(parents=True)
        (run_dir / ".gitkeep").write_text("", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "empty run dir")
        git(seed, "push", "-u", "origin", "main")
    world = SimpleNamespace(
        checkout=checkout, sha=sha, evidence_origin=store,
        evidence_root=tmp_path / "workspace" / "cron_tracking",
    )

    assert run_preflight(world, "--expect-prior-slots", "0935") == 2

    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "0935" in line and "artifact" in line
    assert not world.evidence_root.exists()


def test_refuses_below_the_python_floor(world, capsys, monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 10, 14, "final", 0))

    assert run_preflight(world) == 2
    assert "python" in last_line(capsys).lower()


def _fake_disk_usage(free_bytes, seen=None):
    """Stand-in for shutil.disk_usage that reports ``free_bytes`` free and records the path asked about."""
    def disk_usage(path):
        if seen is not None:
            seen.append(Path(path))
        return SimpleNamespace(total=10 * cloud_preflight.MIN_FREE_BYTES, used=0, free=free_bytes)
    return disk_usage


def test_refuses_when_free_disk_is_below_the_floor_and_creates_nothing(world, capsys, monkeypatch):
    # 2026-10-16: a host with ~76 MB free could not install Python 3.11 and the 10:30 slot died
    # at Gate 2 with a version error. A full disk must be named as the disk, before any clone.
    monkeypatch.setattr(cloud_preflight.shutil, "disk_usage", _fake_disk_usage(cloud_preflight.MIN_FREE_BYTES - 1))

    assert run_preflight(world) == 2

    line = last_line(capsys)
    assert line.startswith("GATE2_ABORT:") and "disk" in line.lower() and "MB" in line
    assert not world.evidence_root.exists()


def test_passes_when_free_disk_is_exactly_the_floor(world, capsys, monkeypatch):
    monkeypatch.setattr(cloud_preflight.shutil, "disk_usage", _fake_disk_usage(cloud_preflight.MIN_FREE_BYTES))

    assert run_preflight(world) == 0
    assert last_line(capsys) == f"PREFLIGHT_OK {world.sha}"


def test_measures_the_nearest_existing_ancestor_of_a_not_yet_created_evidence_root(world, capsys, monkeypatch):
    # The evidence root does not exist on a fresh container, and disk_usage raises on a missing path.
    seen = []
    monkeypatch.setattr(cloud_preflight.shutil, "disk_usage", _fake_disk_usage(cloud_preflight.MIN_FREE_BYTES, seen))
    assert not world.evidence_root.exists()

    assert run_preflight(world) == 0

    assert seen and all(path.exists() for path in seen)
    assert world.evidence_root.parent.parent in seen or world.evidence_root.parent in seen


def test_an_unreadable_disk_is_a_refusal_not_a_pass(world, capsys, monkeypatch):
    def broken(path):
        raise OSError("statvfs failed")
    monkeypatch.setattr(cloud_preflight.shutil, "disk_usage", broken)

    assert run_preflight(world) == 2
    assert last_line(capsys).startswith("GATE2_ABORT:")


def test_the_python_floor_is_reported_before_the_disk_check(world, capsys, monkeypatch):
    # Both fail on the 2026-10-16 host; the version error stays first so existing alerts keep their reason.
    monkeypatch.setattr(sys, "version_info", (3, 10, 14, "final", 0))
    monkeypatch.setattr(cloud_preflight.shutil, "disk_usage", _fake_disk_usage(0))

    assert run_preflight(world) == 2
    assert "python" in last_line(capsys).lower()


def test_never_invokes_an_installer(world, tmp_path, capsys):
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir()
    marker = tmp_path / "installer-was-called"
    for name in ("pip", "pip3", "uv", "npm"):
        shim = bin_dir / name
        shim.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = base_env(DCA_PINNED_SHA=world.sha, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    assert run_preflight(world, env=env) == 0
    assert not marker.exists()


def test_a_refusal_never_prints_the_account_number(world, capsys):
    assert run_preflight(world, remote=world.tmp / "does-not-exist.git") == 2
    captured = capsys.readouterr()
    assert ACCOUNT not in captured.out and ACCOUNT not in captured.err
