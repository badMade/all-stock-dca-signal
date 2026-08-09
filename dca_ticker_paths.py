"""Portable locations for the shared local ticker-history archive."""

from __future__ import annotations

import os
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent
DEFAULT_TICKER_HISTORY_DIR = REPOSITORY_ROOT / "ticker_data"


def configured_ticker_history_dir() -> Path:
    """Return the repository archive path unless an explicit override is set."""
    override = os.environ.get("DCA_TICKER_HISTORY_DIR")
    return Path(override).expanduser() if override else DEFAULT_TICKER_HISTORY_DIR
