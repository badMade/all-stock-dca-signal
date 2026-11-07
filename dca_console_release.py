"""Console release identity: version, pinned UI-asset digest, and channel names.

The operator runs a *stable* console (a pinned, tagged checkout under launchd)
and may run a *sandbox* console on another port to try GUI ideas.  This module
is the single place both read their version from.  ``ASSET_DIGEST`` pins the
bytes of ``console/``; ``tests/test_console_release.py`` fails whenever the
assets change without a ``CONSOLE_VERSION`` bump, a ``CHANGELOG`` entry and a
re-pinned digest -- the "GUI changes only via planned releases" rule.
Stdlib only; reads files, never writes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parent

CONSOLE_VERSION = "1.1.0"
# Regenerate with: python -m dca_console_release   (see docs/local_console.md#release-process)
ASSET_DIGEST = "4f5bd686fdc24c8e"

CHANNEL_STABLE = "stable"
CHANNEL_SANDBOX = "sandbox"
CHANGELOG_PATH = REPO / "docs" / "console_changelog.md"


def digest_files(files: dict[str, bytes]) -> str:
    """Short SHA-256 over ``{name: bytes}`` (sorted); shared by the disk and git-ref digests."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        # Normalize CRLF so a Windows checkout does not read as a UI change.
        digest.update(files[name].replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


def asset_digest(console_dir: Path | None = None) -> str:
    """Short SHA-256 over every file under ``console/``."""
    directory = console_dir or REPO / "console"
    return digest_files({p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})


def changelog_versions(path: Path = CHANGELOG_PATH) -> list[str]:
    """Versions with a ``## <version>`` heading in the changelog, newest first."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line[3:].split()[0] for line in text.splitlines() if line.startswith("## ") and line[3:].strip()]


def release_info(channel: str) -> dict:
    """The version block the snapshot and /api/health expose to the page."""
    return {"version": CONSOLE_VERSION, "channel": channel, "assets": ASSET_DIGEST}


if __name__ == "__main__":
    print(asset_digest())
