"""Point-in-time XBRL facts from SEC EDGAR's ``companyfacts`` API.

Specified first by tests/test_edgar_fundamentals.py (TDD-first, fixtures trimmed from a
live CIK0000320193 pull on 2026-08-22). The rules those fixtures pinned:

* ``filed`` and ``end`` stay separate dates. ``filed`` is when the number became public;
  collapsing it into ``end`` reintroduces lookahead bias in any backtest.
* One 10-Q carries both a ~3-month window (usually with a ``frame`` like "CY2025Q2") and a
  ~9-month year-to-date window (``frame`` null) for the same concept, ``filed`` and
  ``accn``. ``select_single_quarter_facts`` keeps only the single-quarter windows, chosen
  by the span between ``start`` and ``end`` rather than by ``frame``, which EDGAR omits
  on some genuine quarters.
* A missing concept or a 404 CIK is an empty list (``fundamentals_data_available = False``
  on the ``MarketDataSnapshot`` side), never an exception. Malformed entries still fail.
* SEC requires a contact-identifying User-Agent. ``build_headers`` refuses a blank one, and
  no placeholder default ships: the caller passes ``user_agent`` or sets
  ``SEC_EDGAR_USER_AGENT`` (the same variable ticker-advisor/signals.py reads).

Research ingest only: nothing on the live DCA side imports this
(tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from ._json_get import get_json_object

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
_SOURCE = "SEC EDGAR companyfacts"
# A fiscal quarter is 13 weeks (91 days), or 14 in a 53-week year; the 9-month YTD window
# in the same 10-Q spans ~272 days, so this band cannot admit it.
SINGLE_QUARTER_SPAN_DAYS = range(80, 101)
# A fiscal year is 52 or 53 weeks (364 or 371 days) or a calendar year (365 or 366); the 9-month YTD window
# (~273 days) and a quarter (~91) cannot fall in this band.
ANNUAL_SPAN_DAYS = range(350, 381)


@dataclass(frozen=True)
class PointInTimeFact:
    """One reported value, with the date it became public kept apart from its period end.

    ``start`` is None for instant (balance-sheet) concepts, which have no period.
    """

    start: date | None
    end: date
    val: int | float
    fy: int | None
    fp: str | None
    form: str
    filed: date
    frame: str | None
    accn: str


def build_headers(*, user_agent: str | None) -> dict[str, str]:
    """Request headers for EDGAR. Raises ``ValueError`` on a missing or blank User-Agent."""
    if user_agent is None or not user_agent.strip():
        raise ValueError(
            f"SEC EDGAR needs a contact-identifying User-Agent (name + email); pass user_agent or set {USER_AGENT_ENV}"
        )
    return {"User-Agent": user_agent.strip(), "Accept": "application/json"}


def _normalize_cik(cik: str) -> str:
    digits = cik.strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError("CIK must be up to 10 digits")
    return digits.zfill(10)


def _get_json(url: str, *, user_agent: str | None) -> dict[str, Any]:
    """GET one EDGAR document. Raises ``LookupError`` on 404 (see ``_json_get``)."""
    # Headers are built here, before any network call, so a missing User-Agent fails fast.
    resolved = user_agent if user_agent is not None else os.environ.get(USER_AGENT_ENV)
    return get_json_object(url, source=_SOURCE, headers=build_headers(user_agent=resolved))


def _optional_date(entry: dict[str, Any], key: str) -> date | None:
    raw = entry.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{_SOURCE} field {key!r} is not a date string")
    return date.fromisoformat(raw)


def _required_date(entry: dict[str, Any], key: str) -> date:
    value = _optional_date(entry, key)
    if value is None:
        raise ValueError(f"{_SOURCE} field {key!r} is missing")
    return value


def _optional_str(entry: dict[str, Any], key: str) -> str | None:
    raw = entry.get(key)
    if raw is not None and not isinstance(raw, str):
        raise ValueError(f"{_SOURCE} field {key!r} is not a string")
    return raw


def _required_str(entry: dict[str, Any], key: str) -> str:
    value = _optional_str(entry, key)
    if not value:
        raise ValueError(f"{_SOURCE} field {key!r} is missing")
    return value


def _parse_fact(entry: Any) -> PointInTimeFact:
    if not isinstance(entry, dict):
        raise ValueError(f"{_SOURCE} fact entry is not an object")
    val = entry.get("val")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{_SOURCE} field 'val' is not numeric")
    fy = entry.get("fy")
    if fy is not None and (isinstance(fy, bool) or not isinstance(fy, int)):
        raise ValueError(f"{_SOURCE} field 'fy' is not an integer")
    return PointInTimeFact(
        start=_optional_date(entry, "start"),
        end=_required_date(entry, "end"),
        val=val,
        fy=fy,
        fp=_optional_str(entry, "fp"),
        form=_required_str(entry, "form"),
        filed=_required_date(entry, "filed"),
        frame=_optional_str(entry, "frame"),
        accn=_required_str(entry, "accn"),
    )


def fetch_point_in_time_facts(
    *,
    cik: str,
    concept: str,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    user_agent: str | None = None,
) -> list[PointInTimeFact]:
    """Every ``unit`` fact the filer reported for ``taxonomy:concept``, in EDGAR's order.

    Returns an empty list when the CIK has no companyfacts document (404) or the filer
    never tagged ``concept`` in ``unit``.

    Raises:
        ValueError: blank User-Agent, a non-numeric CIK, or a malformed document/entry.
        OSError / urllib.error.HTTPError: transport failures other than 404.
    """
    url = COMPANYFACTS_URL.format(cik=_normalize_cik(cik))
    try:
        document = _get_json(url, user_agent=user_agent)
    except LookupError:
        return []
    facts = document.get("facts")
    if not isinstance(facts, dict):
        raise ValueError(f"{_SOURCE} document has no facts object")
    concept_block = (facts.get(taxonomy) or {}).get(concept)
    if concept_block is None:
        return []
    entries = (concept_block.get("units") or {}).get(unit) if isinstance(concept_block, dict) else None
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{_SOURCE} unit entries are not a list")
    return [_parse_fact(entry) for entry in entries]


def select_single_quarter_facts(facts: list[PointInTimeFact]) -> list[PointInTimeFact]:
    """Keep only duration facts spanning one fiscal quarter, dropping YTD and annual windows."""
    return [f for f in facts if f.start is not None and (f.end - f.start).days in SINGLE_QUARTER_SPAN_DAYS]


def select_annual_facts(facts: list[PointInTimeFact]) -> list[PointInTimeFact]:
    """Keep only duration facts spanning a fiscal year, dropping quarters, YTD windows and instants."""
    return [f for f in facts if f.start is not None and (f.end - f.start).days in ANNUAL_SPAN_DAYS]


def first_filed_facts(facts: list[PointInTimeFact], *, as_of: date) -> list[PointInTimeFact]:
    """One fact per reporting period: the value as FIRST reported, and only if public on ``as_of``.

    A period is its ``(start, end)`` window (an instant has ``start=None``). EDGAR repeats a period in
    every later filing that shows it as a comparative column, and again whenever it is restated, so a
    company's facts hold many copies. Only copies with ``filed <= as_of`` count (inclusive: it was public
    that day); of those the earliest ``filed`` wins, an original form beating an amendment (``10-K/A``)
    filed the same day, then the lowest accession number, so input order never matters. A period first
    filed after ``as_of`` is simply absent: no later copy may stand in for it, since that value was not
    known yet (lookahead bias). Output is ordered by ``(end, start)``, oldest period first.
    """
    best: dict[tuple[date | None, date], PointInTimeFact] = {}
    for fact in facts:
        if fact.filed > as_of:
            continue
        key = (fact.start, fact.end)
        current = best.get(key)
        if current is None or _first_filed_rank(fact) < _first_filed_rank(current):
            best[key] = fact
    return sorted(best.values(), key=lambda f: (f.end, f.start or date.min))


def _first_filed_rank(fact: PointInTimeFact) -> tuple[date, bool, str]:
    return (fact.filed, fact.form.endswith("/A"), fact.accn)


def latest_fact(facts: list[PointInTimeFact]) -> PointInTimeFact | None:
    """The fact whose period ends last, or None for no facts."""
    return max(facts, key=lambda f: f.end, default=None)
