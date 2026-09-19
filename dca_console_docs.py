"""The documentation catalog behind the console's in-app reader (GUI expansion Phase 5).

``/api/docs`` used to serve only the ``doc_ref`` values the program directory
already named, which left most of ``docs/`` and every governance note under
``claude/`` unreachable from the page. The catalog below widens that allowlist
without ever widening it into a directory walk: it is built from bounded,
non-recursive listings of four fixed locations, every entry is a checkout-
relative POSIX path, and :func:`read_doc` re-checks membership and
confinement before a single byte is read.

The registration triangle (``docs/README.md`` table, ``CLAUDE.md`` index, a
``doc_ref`` in ``scripts/agent_entry_points.ENTRY_POINTS``) is reported per
page rather than enforced here, so the Docs page can show which pages the
repo's own indexes have not caught up with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dca_console_reader import BoundedReader

# Root-level pages worth reading in the console, by name: no glob at the root,
# where generated fetch reports and scratch notes also live.
ROOT_DOCS = (
    "README.md", "CLAUDE.md", "AGENTS.md", "DEFERRED.md", "PRODUCTION_READINESS.md",
    "HANDOFF.md", "SELF_HEAL_SETUP.md", "scripts/README.md",
)
# The sibling subprojects' front pages, which the program directory already
# links to; listed by name so no subproject tree is ever walked.
SUBPROJECT_DOCS = (
    "ml-forecaster/README.md", "wondrous-octopus/README.md", "ticker-advisor/README.md",
    "dca-sizing-signals/README.md", "production-safe-agent-loop/README.md",
    "ticker-market-breadth/README.md", "trading-strategies/README.md",
)
DOCS_DIR = "docs"
GOVERNANCE_DIR = "claude"
MIRROR_DIR = "claude/dca-executor-prompts/live-mirror"
MAX_DOC_BYTES = 512 * 1024
MAX_TITLE_BYTES = 4096
_DOC_NAME = re.compile(r"[A-Za-z0-9._-]+\.(?:md|txt)")


@dataclass(frozen=True)
class DocEntry:
    path: str
    group: str
    mirror: bool = False


def _listing(reader: BoundedReader, directory: str, suffix: str) -> list[Path]:
    return sorted(path for path in reader.entries(directory, limit=500)
                  if path.suffix == suffix and _DOC_NAME.fullmatch(path.name) and (reader.root / path).is_file())


def allowed_docs(repo: Path) -> dict[str, DocEntry]:
    """Return every servable page keyed by its checkout-relative POSIX path."""
    reader = BoundedReader(repo, [])
    entries: dict[str, DocEntry] = {}
    for name in ROOT_DOCS:
        if (repo / name).is_file():
            entries[name] = DocEntry(name, "root")
    for name in SUBPROJECT_DOCS:
        if (repo / name).is_file():
            entries[name] = DocEntry(name, "subproject")
    for path in _listing(reader, DOCS_DIR, ".md"):
        entries[path.as_posix()] = DocEntry(path.as_posix(), "docs")
    for path in _listing(reader, GOVERNANCE_DIR, ".md"):
        entries[path.as_posix()] = DocEntry(path.as_posix(), "governance")
    for path in _listing(reader, MIRROR_DIR, ".txt"):
        # A mirror of the scheduled prompts; the source of truth lives outside
        # this repository, and the page says so on every such entry.
        entries[path.as_posix()] = DocEntry(path.as_posix(), "live-mirror", mirror=True)
    return entries


def read_doc(repo: Path, name: str) -> bytes | None:
    """Return one allowlisted page's bytes, or None for anything else."""
    if name not in allowed_docs(repo):
        return None
    path = (repo / name).resolve()
    if not path.is_relative_to(repo) or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        return None
    return data


def _title(repo: Path, name: str) -> str:
    try:
        with (repo / name).open("rb") as handle:
            head = handle.read(MAX_TITLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return name
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:160] or name
    return name


def docs_index(repo: Path, catalog_refs: set[str]) -> dict:
    """List every servable page with its title, group and registration status.

    ``catalog_refs`` are the ``doc_ref`` values the program directory already
    serves; ``in_readme`` and ``in_claude_md`` come from a bounded text scan of
    the two index files. A page missing from any of the three is not an error
    here, only a fact the Docs page shows so the triangle can be closed.
    """
    warnings: list[str] = []
    readme = _text(repo, "docs/README.md", warnings)
    claude_md = _text(repo, "CLAUDE.md", warnings)
    rows = []
    for name, entry in sorted(allowed_docs(repo).items(), key=lambda item: (item[1].group, item[0])):
        basename = Path(name).name
        rows.append({
            "path": name,
            "title": _title(repo, name),
            "group": entry.group,
            "mirror": entry.mirror,
            "in_readme": (name == "docs/README.md" or f"({basename})" in readme or f"/{basename}" in readme)
            if entry.group == "docs" else None,
            "in_claude_md": f"`{name}`" in claude_md if entry.group == "docs" else None,
            "in_catalog": name in catalog_refs,
        })
    # docs/README.md is the documentation index proper; CLAUDE.md's table is a
    # curated subset, so absence there is reported per row but is not a gap.
    unregistered = [row["path"] for row in rows
                    if row["group"] == "docs" and row["in_readme"] is False and row["path"] != "docs/README.md"]
    if unregistered:
        warnings.append(f"{len(unregistered)} docs page(s) are missing from the docs/README.md index.")
    return {"docs": rows, "count": len(rows), "unregistered": unregistered, "warnings": warnings}


def _text(repo: Path, name: str, warnings: list[str]) -> str:
    data = read_doc(repo, name)
    if data is None:
        warnings.append(f"{name} could not be read for registration checks.")
        return ""
    return data.decode("utf-8", errors="replace")
