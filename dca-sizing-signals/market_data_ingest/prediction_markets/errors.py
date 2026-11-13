"""Exception types. Every message is a short code plus our own words, never a venue response
body (.jules/sentinel.md: a venue may echo a key back, and narrative text is not data)."""

from __future__ import annotations


class FetchError(Exception):
    """A fetch that must be recorded as a failed run. ``code`` is what lands in ``ingestion_runs``."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class CredentialsError(FetchError):
    """Polymarket US authenticated call without credentials or a signer (D12). Raised before
    any request is sent, so nothing unauthenticated ever reaches the authenticated API."""

    def __init__(self, detail: str = "credentials or signer missing"):
        super().__init__("credentials_missing", detail)
