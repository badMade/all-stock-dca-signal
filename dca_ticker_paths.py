"""Portable locations for the shared local ticker-history archive."""

from __future__ import annotations

import os
from pathlib import Path

# Only correct for a repository checkout: this module's own file location is
# the source tree root there. Installed as a non-editable package (e.g. a
# wheel), Path(__file__).parent resolves to site-packages instead, and the
# gitignored ticker_data/ Google Drive link (CLAUDE.md: "Do not modify" --
# operational data, never shipped in a wheel) does not exist under it either
# way. DCA_TICKER_HISTORY_DIR MUST be set for such deployments; DO NOT rely
# on this default outside a checkout (Codex review: "Resolve the archive
# outside the installed package").
REPOSITORY_ROOT = Path(__file__).resolve().parent
DEFAULT_TICKER_HISTORY_DIR = REPOSITORY_ROOT / "ticker_data"


def configured_ticker_history_dir() -> Path:
    """Return the configured ticker-history archive directory.

    ``DCA_TICKER_HISTORY_DIR`` overrides the default outright and MUST be
    set when this package is installed rather than run from a repository
    checkout -- see the module-level comment on ``DEFAULT_TICKER_HISTORY_DIR``.
    Without it, callers that require the archive to exist (e.g.
    ``dca_intraday_archive.append_observations``,
    ``scripts/run_tickers_daily_update.py``) fail closed with a
    ``FileNotFoundError`` naming this environment variable rather than
    silently creating or reading from a bogus package-relative path.
    """
    override = os.environ.get("DCA_TICKER_HISTORY_DIR")
    return Path(override).expanduser() if override else DEFAULT_TICKER_HISTORY_DIR
