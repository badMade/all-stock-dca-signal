"""The ``--account`` argument shared by the gate scripts, defaulting to ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``.

Why this exists (2026-11-14, the follow-up #2290's review left open): every live prompt passed the brokerage
account number on the command line, as ``--account <the resolved account>`` or
``--account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}``, to ``assemble_order_pages``, ``merge_order_pages``,
``find_order_baseline``, ``reshape_order_pages``, ``same_day_top_up_gate``, ``gate_failure_diagnostics`` and
``primary_gates budget``. The first form puts the number in the session transcript; both put it in the process
list (``ps``) for the life of the command. ``primary_gates clock`` already read it from the environment, so the
prompts disagreed with themselves about how the secret travels.

Each script now takes ``--account`` as OPTIONAL. Omitted, the number comes from the environment, after a lazy
``dca_paths`` import so the gitignored mode-0600 ``.env`` counts exactly as it does for ``primary_gates clock``
(the bridge shell does not inherit the operator's exports; docs/local_env_file.md). Passed, it still wins:
the prompts already pasted into the scheduled tasks keep passing it until the operator re-pastes, and a run must
not abort over that.

Fail closed: with ``--account`` omitted, an unset, empty, unexpanded or malformed (not 6 to 12 digits) environment
value is a usage error (exit
2, argparse's own convention and the exit a missing ``required=True`` argument gave before), never an empty-string
account that would only surface later as an ``orders_pages_account_mismatch``. An explicit ``--account`` is
validated by each script against its envelope, exactly as before. The number is never printed, in the error or anywhere else
(AGENTS.md secrets policy).
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Optional

ACCOUNT_ENV = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
# Mirrors dca_config.ACCOUNT_NUMBER_SHAPE; scripts/tests/test_account_arg.py pins the two together.
_ACCOUNT_SHAPE = re.compile(r"[0-9]{6,12}")

_DEFAULT_NOTE = (
    f"Optional: defaults to ${ACCOUNT_ENV} (the gitignored .env counts). Prefer the default; a number on the "
    "command line lands in the transcript and the process list."
)


def add_account_argument(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Register ``--account`` on ``parser`` as optional, with the environment as its default source."""
    parser.add_argument("--account", default=None, help=f"{help_text} {_DEFAULT_NOTE}")


def _environment_account() -> str:
    # Imported here, not at module top: dca_paths loads the local .env as an import side effect, and the
    # scripts that import this helper (some of them bundled into backup_planner.pyz) should pay for that only
    # when they actually need the environment's account number.
    import dca_paths  # noqa: F401  (import for its .env side effect)

    return os.environ.get(ACCOUNT_ENV, "")


def _unusable_reason(value: str) -> Optional[str]:
    if not value:
        return "is unset or empty"
    if "${" in value:
        # An unexpanded placeholder copied from config or a prompt; it would only mismatch every envelope.
        return "is an unexpanded ${...} placeholder"
    if not _ACCOUNT_SHAPE.fullmatch(value):
        # 2026-11-15 (#2323): "set and not a placeholder" let a stray word or the morning modes' ``read_only``
        # literal through, to surface only later as a confusing ``orders_pages_account_mismatch``. Same shape as
        # dca_config.ACCOUNT_NUMBER_SHAPE, restated rather than imported: dca_config pulls in near_close_dca, and
        # this helper is bundled into backup_planner.pyz, so it stays standard-library only.
        return "is not 6 to 12 digits"
    return None


def resolve_account(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """Fill ``args.account`` from the environment when it was omitted; exit 2 when that is unusable.

    An explicit ``--account`` is returned untouched: each script already validates the value it is given against
    the envelope (a blank one is refused as ``missing_account``, a different one as an account mismatch), and
    that refusal shape is what the existing evidence and tests read. Only the new environment path is checked
    here, so an unset variable fails at the command line instead of as a confusing mismatch downstream.
    ``parser.error`` raises ``SystemExit(2)``.
    """
    if args.account is not None:
        return args.account
    value = _environment_account().strip()
    reason = _unusable_reason(value)
    if reason is not None:
        parser.error(f"no usable account number: --account omitted and ${ACCOUNT_ENV} {reason}")
    args.account = value
    return value
