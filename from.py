#!/usr/bin/env python3
"""Launch the local DCA console: python3 from.py (observes real local artifacts by default; --demo for a synthetic sandbox)."""

from pathlib import Path

from dca_bootstrap import ensure_venv, linked_worktree_main_checkout, resolve_venv_dir

REPO_ROOT = Path(__file__).resolve().parent
# In a linked worktree, reuse the main checkout's .venv and never create one
# (a worktree-local .venv dies with the worktree and leaves the console probing
# a bare interpreter).  A normal checkout behaves exactly as before.
_IN_WORKTREE = linked_worktree_main_checkout(REPO_ROOT) is not None
ensure_venv(
    requirements_files=(REPO_ROOT / "requirements-dev.txt",),
    venv_dir=resolve_venv_dir(REPO_ROOT),
    create_if_missing=not _IN_WORKTREE,
)

from dca_console import main  # noqa: E402

if __name__ == "__main__":
    import sys

    argv = list(sys.argv[1:])
    if "--demo" not in argv and not any(arg.startswith("--octopus-run") for arg in argv):
        default_octopus = REPO_ROOT / "analysis" / "real-octopus-run"
        if default_octopus.is_dir():
            argv = ["--octopus-run", str(default_octopus), *argv]
    raise SystemExit(main(argv))

