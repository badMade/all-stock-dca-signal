"""Ticker-to-CIK map and SIC sector/industry reader for the daily universe ranking (feed step 2).

Specified first by tests/test_sec_company_map.py and tests/test_sec_exchange_map.py
(docs/fundamentals_feed_scope.md). Three SEC documents:

* ``company_tickers.json``: ticker to CIK, the key every SEC fact is stored under.
* ``company_tickers_exchange.json`` (feed step 2b): ticker to the exchange the SEC records, which the ranking's pool
  filter reads as "listed on a US exchange". Its columns are found by NAME, so a reordering is harmless.
* a company's ``submissions`` document: its SIC code. The ADR chose SIC for sector and industry because
  GICS is licensed. Industry is the four-digit SIC code and sector is its two-digit major group, so each
  industry belongs to exactly one sector.

The document layouts are those the tests' fixtures describe (``company_tickers.json`` is an object keyed
"0", "1", ... with ``cik_str`` an integer, ``ticker`` and ``title``; a submissions document has a string
``sic`` and a ``sicDescription``). They were NOT confirmed against a live pull when this was written (the SEC
refuses a request without a contact-identifying User-Agent). Confirm with one live pull before relying on it.

Parsing fails closed: a malformed document is refused, never partly read, and a ticker that maps to two
different CIKs is refused rather than guessed. A company with no SIC is ``None`` ("no classification"), which
is normal for funds and trusts. Research ingest only: nothing on the live DCA side imports this
(tests/test_architecture_boundaries.py). It shares the one SEC GET helper and its User-Agent rule with
``edgar_fundamentals``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .edgar_fundamentals import _get_json, _normalize_cik

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_EXCHANGES_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


@dataclass(frozen=True)
class CompanyRef:
    """One listed company: its 10-digit zero-padded CIK and the SEC's name for it."""

    cik: str
    name: str


@dataclass(frozen=True)
class SicInfo:
    """A company's SIC classification. ``sector`` is the 2-digit major group, ``industry`` the 4-digit code."""

    sic: str
    description: str
    sector: str
    industry: str


def _normalize_symbol(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("ticker must be a non-blank string")
    # The SEC writes a share class with a dash (BRK-B); the repo's symbol shape uses a dot (BRK.B).
    return raw.strip().upper().replace("-", ".")


def _cik_from_value(raw: Any) -> str:
    if isinstance(raw, bool):
        raise ValueError("cik_str must be an integer or a digit string, not a bool")
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError("cik_str must not be negative")
        return _normalize_cik(str(raw))
    if isinstance(raw, str):
        return _normalize_cik(raw)
    raise ValueError("cik_str must be an integer or a digit string")


def parse_company_tickers(document: Any) -> dict[str, CompanyRef]:
    """Map each ticker to its :class:`CompanyRef`. Raises ``ValueError`` on any malformed entry."""
    if not isinstance(document, dict):
        raise ValueError("company_tickers document must be an object")
    out: dict[str, CompanyRef] = {}
    for entry in document.values():
        if not isinstance(entry, dict):
            raise ValueError("company_tickers entry must be an object")
        symbol = _normalize_symbol(entry.get("ticker"))
        ref = CompanyRef(cik=_cik_from_value(entry.get("cik_str")), name=str(entry.get("title") or ""))
        existing = out.get(symbol)
        if existing is not None and existing.cik != ref.cik:
            raise ValueError(f"ticker {symbol} maps to two different CIKs")
        out[symbol] = ref
    return out


def cik_for_tickers(
    mapping: Mapping[str, CompanyRef], tickers: Iterable[str]
) -> tuple[dict[str, CompanyRef], list[str]]:
    """Split ``tickers`` into ``(found, missing)``: found by symbol, missing sorted and listed once."""
    wanted = set(tickers)
    found = {t: mapping[t] for t in sorted(wanted) if t in mapping}
    return found, sorted(wanted - set(found))


def sic_sector(sic: Any) -> str:
    """The two-digit major group of a four-digit SIC code. Raises ``ValueError`` otherwise."""
    if not isinstance(sic, str) or len(sic) != 4 or not sic.isdigit():
        raise ValueError("SIC code must be a four-digit string")
    return sic[:2]


def parse_submissions_sic(document: Any) -> SicInfo | None:
    """The SIC classification in a submissions document, or None when the company has none."""
    if not isinstance(document, dict):
        raise ValueError("submissions document must be an object")
    raw = document.get("sic")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError("sic must be a number or digit string")
    text = str(raw).strip()
    if not text.isdigit() or len(text) > 4:
        raise ValueError("sic must be up to four digits")
    sic = text.zfill(4)
    return SicInfo(
        sic=sic,
        description=str(document.get("sicDescription") or ""),
        sector=sic_sector(sic),
        industry=sic,
    )


def fetch_company_tickers(*, user_agent: str | None = None) -> dict[str, CompanyRef]:
    """Download and parse the SEC ticker file. Needs a contact-identifying User-Agent (see ``edgar_fundamentals``)."""
    return parse_company_tickers(_get_json(COMPANY_TICKERS_URL, user_agent=user_agent))


def fetch_sic(cik: str, *, user_agent: str | None = None) -> SicInfo | None:
    """The SIC classification of one company, or None when it has no submissions document or no SIC."""
    padded = _normalize_cik(cik)
    try:
        document = _get_json(SUBMISSIONS_URL.format(cik=padded), user_agent=user_agent)
    except LookupError:
        return None
    return parse_submissions_sic(document)


def parse_company_exchanges(document: Any) -> dict[str, str | None]:
    """Map each ticker to the exchange the SEC records for it (``None`` when blank). ``ValueError`` if malformed.

    The document is ``{"fields": [...column names...], "data": [[...row...], ...]}``. Tickers are normalized like
    ``parse_company_tickers``. The same ticker on two different exchanges is refused rather than guessed.
    """
    if not isinstance(document, dict):
        raise ValueError("company_tickers_exchange document must be an object")
    fields = document.get("fields")
    if not isinstance(fields, list):
        raise ValueError("company_tickers_exchange 'fields' must be a list")
    try:
        ticker_at, exchange_at = fields.index("ticker"), fields.index("exchange")
    except ValueError:
        raise ValueError("company_tickers_exchange 'fields' must name 'ticker' and 'exchange'") from None
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("company_tickers_exchange 'data' must be a list")
    out: dict[str, str | None] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("company_tickers_exchange row does not match 'fields'")
        symbol = _normalize_symbol(row[ticker_at])
        raw = row[exchange_at]
        if raw is not None and not isinstance(raw, str):
            raise ValueError("company_tickers_exchange exchange must be a string")
        exchange = raw.strip() or None if raw is not None else None
        if symbol in out and out[symbol] != exchange:
            raise ValueError(f"ticker {symbol} is listed on two different exchanges")
        out[symbol] = exchange
    return out


def fetch_company_exchanges(*, user_agent: str | None = None) -> dict[str, str | None]:
    """Download and parse the SEC exchange file. A 404 (``LookupError``) is not swallowed."""
    return parse_company_exchanges(_get_json(COMPANY_EXCHANGES_URL, user_agent=user_agent))
