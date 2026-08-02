"""Deployment-relative artifact roots for every scheduled DCA stream.

The live deployment writes all run artifacts under a single tree
(``/home/user/workspace/cron_tracking`` by default), one subdirectory per
scheduled stream. Every stream module used to hardcode its own absolute path
into a module constant, which put 15 copies of one deployment detail across
``morning_dca``, ``midday_dca``, ``dca_alerts``, ``scripts/run_classification``,
``scripts/same_day_pointer``, and ``scripts/model_usage_log`` -- against the
"do not hard-code local absolute paths" rule in ``AGENTS.md``, and with no way
to relocate the tree short of editing source.

They now derive from :func:`cron_tracking_root`, which honours
``$DCA_CRON_TRACKING_ROOT`` and otherwise returns the same bundled default as
before. With the variable unset, every path this module produces is byte-for-byte
what the previous hardcoded constants produced, so this is a portability change
and not a behavior change.

**Resolution timing.** The stream constants below are module-level and therefore
resolved once, at import. That matches the deployment (a cron entry exports the
variable before Python starts) and it is why callers that need a *late* override
-- ``dca_alerts.write_failed_start_marker``, the per-stream
``DCA_*_LIVE_STATE_ROOT`` overrides in ``scripts/run_classification`` -- keep
reading the environment at call time instead of relying on these.

Standard library only; imports nothing from the rest of the project, so any
module (including ``scripts/*``) can use it without creating a cycle. Mirrors
:mod:`dca_fileio` in that respect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The bundled production tree serialized in config/*.json. This must remain
# stable so rebase_bundled_root() can detect and rewrite those paths.
BUNDLED_CRON_TRACKING_ROOT = "/home/user/workspace/cron_tracking"

# Local default for an unconfigured checkout: keep run artifacts inside this
# repository so the root exists on any machine and remains gitignored, rather
# than falling back to a deployment-specific literal like
# BUNDLED_CRON_TRACKING_ROOT (AGENTS.md: "never from a literal `/home/user/...`
# string"). Production sets $DCA_CRON_TRACKING_ROOT explicitly.
DEFAULT_CRON_TRACKING_ROOT = str((Path(__file__).resolve().parent / "cron_tracking").resolve())

# Deployment override, following the DCA_* convention already used by
# DCA_CONFIG_DIR, DCA_ATTEMPT_LOG_ROOT, and DCA_INTRADAY_LIVE_STATE_ROOT.
CRON_TRACKING_ROOT_ENV = "DCA_CRON_TRACKING_ROOT"


def cron_tracking_root() -> str:
    """Root directory holding every per-stream artifact tree.

    ``$DCA_CRON_TRACKING_ROOT`` when set and non-empty, else
    :data:`DEFAULT_CRON_TRACKING_ROOT`. An empty variable is treated as unset so
    a blank export cannot silently redirect artifacts to the filesystem root.

    Trailing separators are stripped so every consumer derives the same string.
    :func:`stream_root` joins through ``os.path.join``, which collapses them,
    while :func:`rebase_bundled_root` concatenates -- so an export of
    ``/srv/dca/`` produced ``/srv/dca/morning_dca`` from one and
    ``/srv/dca//morning_dca`` from the other. Those compare unequal as strings,
    and ``MorningDCAExecutor`` decides whether ``prior_allocation_roots`` is
    still at its class default by exact tuple equality: a mismatch there makes a
    default config look explicitly overridden, which skips the same-day manual
    top-up context merge and lets a midday run re-buy a ticker an earlier manual
    allocation already covered (Codex review, PR #219).

    A root that is nothing but its root prefix keeps that prefix rather than
    being trimmed away: ``"/"`` stays ``"/"``, and a Windows drive or UNC root
    keeps its separator (``"C:\\"`` stays ``"C:\\"``). Trimming those would
    change what the path MEANS rather than just tidying it -- bare ``"C:"`` is
    drive-relative, so ``os.path.join("C:", "morning_dca")`` yields
    ``"C:morning_dca"``, resolved against the process's current directory on
    that drive instead of the drive root (Codex review, PR #219).
    """
    configured = os.environ.get(CRON_TRACKING_ROOT_ENV) or DEFAULT_CRON_TRACKING_ROOT
    stripped = configured.rstrip("/" + os.sep)
    # Nothing left after the drive/UNC prefix means the whole value was a root.
    if not os.path.splitdrive(stripped)[1]:
        drive = os.path.splitdrive(configured)[0]
        return drive + os.sep if drive else configured[:1]
    return stripped


def stream_root(*parts: str) -> str:
    """Path under :func:`cron_tracking_root`, e.g. ``stream_root("morning_dca")``."""
    return os.path.join(cron_tracking_root(), *parts)


def rebase_bundled_root(value: Any) -> Any:
    """Re-root a bundled config path onto the configured artifact tree.

    ``config/*.json`` records artifact locations as absolute paths under
    :data:`BUNDLED_CRON_TRACKING_ROOT` (``artifact_paths.live_run_state_root``,
    ``prior_allocation_roots``, the preflight resolution roots, …). Those values
    are loaded straight into the executors, so they *override* the roots derived
    from :func:`stream_root` -- meaning a deployment that set
    ``$DCA_CRON_TRACKING_ROOT`` still had its executors read and write the
    bundled production tree while everything reading ``dca_paths`` moved
    (Codex review, PR #219: "Route loaded stream roots through the shared
    override").

    Rewriting the bundled prefix here keeps one source of truth without
    templating ~40 config values. A path that does NOT sit under the bundled
    root is deliberately returned untouched: an operator who wrote an explicit
    absolute path into config meant that exact path.

    The match is anchored at a path boundary -- the root itself, or the root
    followed by a separator. A bare ``startswith`` also swallowed sibling
    directories that merely share the prefix, so an explicitly configured
    ``…/cron_tracking_backup/morning_dca`` was rewritten to
    ``<root>_backup/morning_dca`` and the executor read or wrote a tree the
    operator never named (Codex review, PR #219).

    Both ``/`` and ``os.sep`` count as that boundary. The bundled paths are
    serialized in ``config/*.json`` with forward slashes regardless of platform,
    so keying the check on ``os.sep`` alone meant that on Windows -- where
    ``os.sep`` is a backslash -- no descendant path ever matched, and setting the
    override silently moved the module-derived constants while leaving the
    executors on the bundled tree: readers and writers split across two roots
    (Codex review, PR #219).

    Typed ``Any`` rather than ``str`` on purpose: :func:`rebase_bundled_roots_in`
    maps this over whole parsed config documents, so it is handed the ints,
    bools, and ``None``s that sit alongside the paths and returns them
    untouched. Annotating it ``str -> str`` described a narrower contract than
    the function actually implements and than its only caller relies on
    (Copilot review, PR #219).
    """
    if not isinstance(value, str):
        return value
    boundaries = {"/", os.sep}
    if value != BUNDLED_CRON_TRACKING_ROOT and not any(
        value.startswith(BUNDLED_CRON_TRACKING_ROOT + sep) for sep in boundaries
    ):
        return value
    matched_root = BUNDLED_CRON_TRACKING_ROOT
    configured = cron_tracking_root()
    if configured == matched_root:
        return value
    suffix = value[len(matched_root) :]
    # Rebuild with the PLATFORM separator instead of concatenating. The bundled
    # values are serialized with forward slashes, so concatenation produced
    # "C:\dca/morning_dca" on Windows while stream_root() -- which joins through
    # os.path.join -- produced "C:\dca\morning_dca". Windows resolves both, but
    # MorningDCAExecutor decides whether prior_allocation_roots is still at its
    # class default by EXACT string equality, so the mismatch made a default
    # config look explicitly overridden, skipping the same-day manual top-up
    # merge and letting a midday run re-buy a ticker an earlier manual
    # allocation already covered (Codex review, PR #219).
    trailing = os.sep if suffix.endswith(("/", os.sep)) else ""
    parts = [part for part in suffix.replace(os.sep, "/").split("/") if part]
    if not parts:
        return configured + trailing
    return os.path.join(configured, *parts) + trailing


def rebase_bundled_roots_in(node):
    """Recursively apply :func:`rebase_bundled_root` to every string in ``node``.

    Applied to a whole parsed config document rather than to the handful of keys
    a loader happens to read today, so a path added to ``config/*.json`` later
    follows ``$DCA_CRON_TRACKING_ROOT`` without anyone remembering to wire it up.
    Non-path strings are untouched — only values starting with the bundled root
    are rewritten. Returns a new structure; the input is not mutated.
    """
    if isinstance(node, dict):
        return {key: rebase_bundled_roots_in(value) for key, value in node.items()}
    if isinstance(node, list):
        return [rebase_bundled_roots_in(value) for value in node]
    return rebase_bundled_root(node)


__all__ = [
    "BUNDLED_CRON_TRACKING_ROOT",
    "CRON_TRACKING_ROOT_ENV",
    "DEFAULT_CRON_TRACKING_ROOT",
    "cron_tracking_root",
    "rebase_bundled_root",
    "rebase_bundled_roots_in",
    "stream_root",
]
