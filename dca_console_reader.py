"""Bounded, confined filesystem reads shared by every console reader module.

Extracted from ``dca_console_octopus``'s private ``_Reader`` (GUI expansion,
Phase 0) so the run, config, state, alert and report readers that follow reuse
one confinement policy instead of each growing its own: every path resolves
below one root, one file is read to a 32 MiB ceiling, one directory scan stops
at 20,000 entries, and problems become warnings on the caller's list rather
than exceptions. Nothing here imports another console module, so any reader
can use it without a cycle.

There is deliberately no ``st_nlink`` anti-aliasing check anywhere in this
module: Google Drive mirrors the checkout and hard-links every new file for
7-11 seconds after a write, so a link count of 2 proves nothing on this
machine.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 20_000


def iso_date(value: object) -> str | None:
    """Return a ten-character ISO date unchanged, or None for anything else."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def root_label(path: object) -> str:
    """Name a filesystem root for the browser without exposing its absolute path.

    The console's snapshot used to echo ``str(settings.root)`` verbatim, which
    put an operator's home directory into every JSON response and every saved
    summary (GUI expansion Phase 0 leak fix). A basename plus a short digest is
    stable across refreshes, distinguishes two roots that share a name, and
    still lets the operator recognise which directory a panel is reading.
    """
    text = str(path)
    digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    name = Path(text).name or text
    return f"{name} ({digest})"


class BoundedReader:
    """Read artifacts below one root with fixed size and count limits."""

    def __init__(self, root: Path, warnings: list[str]) -> None:
        self.root = root
        self.warnings = warnings

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def confined(self, relative: Path | str) -> Path | None:
        """Resolve one relative path and refuse anything that escapes the root."""
        try:
            resolved = (self.root / relative).resolve()
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(self.root):
            self.warn("An artifact outside the selected directory was ignored.")
            return None
        return resolved

    def modified_at(self, relative: Path | str) -> float | None:
        """Read bounded artifact metadata without following an external symlink."""
        try:
            path = (self.root / relative).resolve()
            if path.is_relative_to(self.root) and path.is_file():
                return path.stat().st_mtime
        except (OSError, RuntimeError):
            pass
        return None

    def text(self, relative: Path | str) -> str | None:
        path = self.root / relative
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.root):
                self.warn("An artifact outside the selected run directory was ignored.")
                return None
            if not resolved.is_file():
                return None
            with resolved.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                self.warn("An artifact exceeds the 32 MiB read limit and was skipped.")
                return None
            return data.decode("utf-8-sig")
        except (OSError, UnicodeError, RuntimeError):
            self.warn("An artifact could not be read; its data is unavailable.")
            return None

    def json(self, relative: Path | str) -> dict:
        source = self.text(relative)
        if source is None:
            return {}
        try:
            result = json.loads(source)
        except (ValueError, RecursionError):
            self.warn("A JSON artifact is corrupt; its data is unavailable.")
            return {}
        if not isinstance(result, dict):
            self.warn("A JSON artifact has an unsupported shape and was ignored.")
            return {}
        return result

    def entries(self, relative: Path | str = "", *, limit: int = MAX_FILES) -> list[Path]:
        """List one confined directory, relative to the root, stopping at ``limit``."""
        directory = self.confined(relative)
        if directory is None or not directory.is_dir():
            return []
        found: list[Path] = []
        try:
            for index, candidate in enumerate(directory.iterdir()):
                if index >= limit:
                    self.warn("A directory exceeds the file scan limit; later entries were not read.")
                    break
                found.append(candidate.relative_to(self.root))
        except (OSError, RuntimeError):
            self.warn("A directory could not be read.")
        return found

    def latest_in(self, directory: Path | str, suffix: str) -> Path | None:
        """Return the lexically latest ISO-dated ``*suffix`` file in one directory."""
        folder = self.confined(directory)
        if folder is None:
            return None
        if not folder.is_dir():
            return None
        latest = None
        try:
            for index, candidate in enumerate(folder.iterdir()):
                if index >= MAX_FILES:
                    self.warn("The daily directory exceeds the file scan limit; the latest file may be unavailable.")
                    break
                if candidate.name.endswith(suffix) and iso_date(candidate.name[:10]):
                    if latest is None or candidate.name > latest.name:
                        latest = candidate
        except (OSError, RuntimeError):
            self.warn("The daily artifact directory could not be read.")
            return None
        return latest.relative_to(self.root) if latest is not None else None
