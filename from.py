#!/usr/bin/env python3
"""Launch the local DCA console: python3 from.py (demo by default)."""

from pathlib import Path

from dca_bootstrap import ensure_venv

REPO_ROOT = Path(__file__).resolve().parent
ensure_venv(
    requirements_files=(REPO_ROOT / "requirements-dev.txt",),
)

from dca_console import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
