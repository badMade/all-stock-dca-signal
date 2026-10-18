"""Shared fakes for the price-source tests. No network, no real clock, no real keys.

The seams below are the contract the implementation is written against:

* HTTP: providers take a ``session`` object and call
  ``session.get(url, params=..., headers=..., timeout=...)``, then read the response's
  ``.status_code``, ``.text`` and ``.headers``. Transport failures surface as
  ``requests.exceptions.Timeout`` / ``ConnectionError``, exactly as a real
  ``requests.Session`` raises them. Reading ``.text`` (not ``.json()``) is deliberate: the
  provider must parse strictly itself, so a truncated body is detectable.
* Time: anything that reads the time or waits takes ``clock`` (returns an aware UTC
  ``datetime``) and ``sleep`` (seconds). ``FakeClock`` provides both and ``sleep``
  advances ``now``, so rate limiting is observable and every run is deterministic.
* Calendar: anything that needs the market calendar takes an object with
  ``is_session(d) -> bool`` and ``previous_sessions(d, n) -> list[date]`` (most recent
  first). ``FixedCalendar`` is that object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# A recognisable fake secret. test_no_secrets.py asserts it never escapes into logs,
# exception text, request URLs/params or the database file.
TEST_MASSIVE_KEY = "MASSIVE-TEST-SECRET-7f3a9c"
TEST_ALPACA_KEY_ID = "ALPACA-TEST-KEYID-41b2"
TEST_ALPACA_SECRET = "ALPACA-TEST-SECRET-e8d0"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "contract: opt-in live test against the real Massive/Alpaca APIs (PRICE_CONTRACT=1 + keys)",
    )


def load_fixture(name: str) -> Any:
    """Parse a recorded/synthetic JSON fixture. Every fixture carries ``_provenance``."""
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = "{}"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json_body(cls, payload: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> "FakeResponse":
        return cls(status_code=status_code, text=json.dumps(payload), headers=headers or {})


@dataclass
class RecordedCall:
    url: str
    params: dict[str, Any]
    headers: dict[str, str]
    timeout: Any


class FakeSession:
    """Stands in for ``requests.Session``.

    ``responses`` is either a list consumed in order (each item a ``FakeResponse`` or an
    exception instance to raise) or a callable ``(url, params) -> FakeResponse``.
    """

    def __init__(self, responses: list[Any] | Callable[[str, dict[str, Any]], Any]):
        self._responses = responses
        self.calls: list[RecordedCall] = []

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        **_: Any,
    ) -> FakeResponse:
        params = dict(params or {})
        self.calls.append(RecordedCall(url=url, params=params, headers=dict(headers or {}), timeout=timeout))
        if callable(self._responses):
            item = self._responses(url, params)
        else:
            if not self._responses:
                raise AssertionError(f"FakeSession: unexpected extra request to {url}")
            item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """Deterministic clock. ``sleep`` advances ``now`` and is recorded."""

    def __init__(self, start: datetime):
        assert start.tzinfo is not None, "FakeClock needs an aware datetime"
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FixedCalendar:
    """A market calendar given as an explicit set of open sessions."""

    def __init__(self, sessions: Iterable[date]):
        self.sessions = sorted(set(sessions))

    def is_session(self, d: date) -> bool:
        return d in self.sessions

    def previous_sessions(self, d: date, n: int) -> list[date]:
        earlier = [s for s in self.sessions if s < d]
        return list(reversed(earlier[-n:]))


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def grouped_payload(session: date, n_rows: int, *, results_count: int | None = None) -> dict[str, Any]:
    """A Massive grouped-daily body with ``n_rows`` synthetic, valid rows.

    Built from the shape of fixtures/massive_grouped_2024-05-31.json so the threshold
    tests can vary the row count without hand-writing thousands of rows.
    """
    template = load_fixture("massive_grouped_2024-05-31.json")["results"][0]
    # Daily aggregates are stamped at 00:00 US/Eastern of the session; 04:00Z in EDT.
    t_ms = int(datetime(session.year, session.month, session.day, 4, tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    for i in range(n_rows):
        row = dict(template)
        row["T"] = f"SYM{i:05d}"
        row["t"] = t_ms
        rows.append(row)
    return {
        "_provenance": "synthetic-from-docs (generated by conftest.grouped_payload)",
        "adjusted": False,
        "queryCount": n_rows,
        "resultsCount": n_rows if results_count is None else results_count,
        "request_id": "synthetic",
        "status": "OK",
        "results": rows,
    }


@pytest.fixture
def clock() -> FakeClock:
    # 2024-06-11 22:00 ET (02:00Z next day): after the 2024-06-11 session closed.
    return FakeClock(utc(2024, 6, 12, 2, 0, 0))


@pytest.fixture
def store(tmp_path: Path):
    from market_data_ingest.prices.store import PriceStore

    s = PriceStore(tmp_path / "prices.duckdb")
    yield s
    s.close()


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d
