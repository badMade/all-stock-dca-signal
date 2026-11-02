"""Ledger exceptions. Messages name fields and row positions, never the offending value,
so a raw account identifier passed by mistake is not echoed into a log or traceback."""

from __future__ import annotations


class LedgerValidationError(ValueError):
    """A batch failed validation; nothing from it was written."""


class AccountIdentifierError(LedgerValidationError):
    """A raw account identifier (or something shaped like one) tried to reach the ledger,
    or no usable alias salt was configured."""
