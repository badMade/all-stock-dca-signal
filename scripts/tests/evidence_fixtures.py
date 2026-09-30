"""Local-git fixtures shared by the cloud preflight and evidence publisher tests.

Every remote here is a bare repository on the local disk, so no test touches the
network and a run is deterministic. Git identity and config are pinned through
the environment, so the suite behaves the same on a machine with a global git
config and in a bare CI container (docs/reliability_spec.md: environment parity).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "dca-test",
    "GIT_AUTHOR_EMAIL": "dca-test@example.invalid",
    "GIT_COMMITTER_NAME": "dca-test",
    "GIT_COMMITTER_EMAIL": "dca-test@example.invalid",
}


def base_env(**overrides: str) -> Dict[str, str]:
    """A complete environment for calling a script's ``main(argv, env=...)``."""
    env = {**os.environ, **GIT_ENV, "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER": ACCOUNT}
    env.pop("DCA_PINNED_SHA", None)
    env.pop("DCA_CRON_TRACKING_ROOT", None)
    env.update(overrides)
    return env


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
        capture_output=True, text=True, check=check,
    )
    return result.stdout.strip()


def make_code_origin(tmp_path: Path) -> Tuple[Path, Path, str]:
    """A bare code origin, a checkout of its ``main``, and that checkout's HEAD SHA."""
    origin = tmp_path / "code_origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    checkout = tmp_path / "all-stock-dca"
    git(tmp_path, "clone", str(origin), str(checkout))
    git(checkout, "checkout", "-B", "main")
    (checkout / "README.md").write_text("code\n", encoding="utf-8")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "initial")
    git(checkout, "push", "-u", "origin", "main")
    return origin, checkout, git(checkout, "rev-parse", "HEAD")


def make_evidence_origin(tmp_path: Path, *, manifest: Optional[dict] = None, seed_manifest: bool = True) -> Path:
    """A bare evidence remote whose ``main`` holds a valid, empty MANIFEST.json."""
    origin = tmp_path / "evidence_origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    seed = tmp_path / "evidence_seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "checkout", "-B", "main")
    if seed_manifest:
        payload = manifest if manifest is not None else {"version": 1, "days": {}}
        (seed / "MANIFEST.json").write_text(json.dumps(payload), encoding="utf-8")
    else:
        (seed / "README.md").write_text("evidence\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "-u", "origin", "main")
    return origin


def remote_head(origin: Path) -> str:
    return git(origin, "rev-parse", "main")


def remote_file(origin: Path, path: str) -> str:
    return git(origin, "show", f"main:{path}")
