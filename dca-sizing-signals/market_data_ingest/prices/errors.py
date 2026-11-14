"""Exceptions for the price source. Every failure is one of these, never a silent empty result.

``ProviderError`` is the base a router catches to decide on failover. Its ``code`` is a short
machine-readable reason (``timeout``, ``http_5xx``, ``not_authorized``, ``below_median``...)
that run records and tests key on. Messages are written by this package from codes and
counts only: no vendor response body is ever copied into one, because a vendor can echo an
API key back in an error body (tests/prices/test_no_secrets.py).
"""

from __future__ import annotations


class ProviderError(Exception):
    """A data provider could not supply what was asked for."""

    default_code = "provider_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class StaleDataError(ProviderError):
    """Data arrived but is too old (or otherwise implausible) to use."""

    default_code = "stale"


class IncompleteBatchError(ProviderError):
    """A batch is missing rows: truncated, short against its baseline, or empty on an open day."""

    default_code = "incomplete"


class RouterExhaustedError(ProviderError):
    """Primary and fallback both failed. Both causes are kept for the run record."""

    default_code = "router_exhausted"

    def __init__(self, primary_error: ProviderError, fallback_error: ProviderError) -> None:
        super().__init__(
            "primary failed ({}: {}); fallback failed ({}: {})".format(
                type(primary_error).__name__, primary_error.code, type(fallback_error).__name__, fallback_error.code
            )
        )
        self.primary_error = primary_error
        self.fallback_error = fallback_error


class ValidationError(ValueError):
    """A batch (or a legacy file) failed schema or sanity checks; none of it is written."""


class AmbiguousActionsError(ValidationError):
    """Some corporate actions shared one version identity with different values and were held back.

    Unlike its parent, the rest of the batch WAS written (``inserted`` rows) before this was
    raised: one ambiguous symbol must not stop every other symbol's splits and dividends for the
    whole re-pull window (PR #2282 review). The run still fails loudly, naming what was held back.
    """

    def __init__(self, message: str, *, inserted: int) -> None:
        super().__init__(message)
        self.inserted = inserted


class SpliceNotValidatedError(Exception):
    """Refused to join yahoo_legacy and vendor history without a passing reconciliation."""
