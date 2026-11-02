"""Account aliasing and the account-identifier guard.

Proposed default (ADR open question 3, not operator-approved): an account is known to the
ledger only as ``alias-<24 hex>``, the first 96 bits of HMAC-SHA256(salt, raw identifier).
The salt comes from ``$DCA_LEDGER_ACCOUNT_SALT`` at runtime, is never stored, and is
required: no salt means no alias, never an unsalted fallback (fail closed).

The guard mirrors the shapes tests/test_config_invariants.py treats as account-shaped --
a bare 6-12 digit run, or one glued to an account-ish label -- so a value the repo's own
secrets scan would flag in a committed file can never land in a ledger table either. It has
no allowlist and no prefix exemptions on purpose (project rule: an escape hatch in a
secrets guard is a security regression).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from .defaults import ACCOUNT_SALT_ENV, MIN_SALT_LENGTH
from .errors import AccountIdentifierError

# The only account-named fields a ledger row may carry: the alias and a descriptive kind.
_ACCOUNT_FIELDS = frozenset({"account_alias", "account_kind"})

ALIAS_PATTERN = re.compile(r"alias-[0-9a-f]{24}")

# Same two shapes as tests/test_config_invariants.py (_ACCOUNT_SHAPED, _LABELED_ACCOUNT).
_ACCOUNT_SHAPED = re.compile(r"(?<![.\w])\d{6,12}(?![\w.])")
_LABELED_ACCOUNT = re.compile(r"(?i)(?:account|acct|broker|robinhood|alpaca|rh)[^0-9A-Za-z]{0,3}(\d{6,12})")


def looks_like_account_identifier(value: str) -> bool:
    """True when ``value`` contains anything the repo secrets scan would call account-shaped."""
    return bool(_ACCOUNT_SHAPED.search(value) or _LABELED_ACCOUNT.search(value))


def _salt(salt: str | None) -> bytes:
    value = os.environ.get(ACCOUNT_SALT_ENV) if salt is None else salt
    if not value:
        raise AccountIdentifierError(f"no account alias salt: set ${ACCOUNT_SALT_ENV}")
    if len(value) < MIN_SALT_LENGTH:
        raise AccountIdentifierError(f"account alias salt shorter than {MIN_SALT_LENGTH} characters")
    return value.encode()


def account_alias(raw_identifier: str, *, salt: str | None = None) -> str:
    """Alias for one raw account identifier. Call this BEFORE building a ledger row; the raw
    value must never be put in a row itself. Deterministic for a given salt, so tax lots
    from different snapshots of the same account join on the alias."""
    if not isinstance(raw_identifier, str) or not raw_identifier.strip():
        raise AccountIdentifierError("raw account identifier must be a non-empty string")
    digest = hmac.new(_salt(salt), raw_identifier.strip().encode(), hashlib.sha256).hexdigest()
    return f"alias-{digest[:24]}"


def require_alias(value: object, *, where: str) -> str:
    """``value`` if it is a well-formed alias; otherwise raise without echoing it."""
    if not isinstance(value, str) or not ALIAS_PATTERN.fullmatch(value):
        raise AccountIdentifierError(f"{where}: account_alias must be an alias from account_alias(), not a raw value")
    return value


def guard_account_keys(row: dict[str, object], *, where: str) -> None:
    """Refuse a row with any account-named key other than the alias and the kind."""
    for key in row:
        if "account" in str(key).lower() and key not in _ACCOUNT_FIELDS:
            raise AccountIdentifierError(f"{where}: field {key!r} would carry a raw account identifier")


def guard_text_fields(row: dict[str, object], *, where: str) -> None:
    """Refuse a row in which any key, or any text value, looks like a raw account identifier.
    Used on the accounts and tax_lots ingests, the two tables about accounts."""
    guard_account_keys(row, where=where)
    for key, value in row.items():
        if isinstance(value, str) and looks_like_account_identifier(value):
            raise AccountIdentifierError(f"{where}: field {key!r} contains an account-shaped value")
