"""Shared fakes for the Phase 5a prediction-market tests (docs/adr-prediction-markets.md §13).

No network, no real clock, no real keys. Written before any implementation exists: these
seams ARE the contract ``market_data_ingest.prediction_markets`` is to be written against.

Package layout the tests import (lazily, through ``pm()``, so a missing module fails each
test on its own instead of aborting collection of the whole dca-sizing-signals suite):

* ``config``     ``load_config(path) -> PMConfig``, ``DEFAULT_CONFIG_PATH``, ``ConfigError``
* ``models``     ``MarketRecord``, ``QuoteRow``, ``IngestBatch``
* ``source``     ``PredictionMarketSource`` (ABC), ``run_ingestion(store, venue, fetch, *, clock)``
* ``polymarket_intl`` / ``polymarket_us``   one adapter per venue (D2)
* ``throttle``   ``RequestBudget``;  ``cadence``  ``interval_for``, ``captures_orderbook``
* ``alignment``  ``daily_value``;  ``composite``  ``pool_logodds``, ``compute_composite``
* ``store``      ``PMStore`` (DuckDB), ``LicenseScopeError``

Seams:

* HTTP: adapters take a ``session`` and call ``session.get(url, params=, headers=, timeout=)``
  or ``session.post(url, json=, headers=, timeout=)``, then read ``.status_code``, ``.text``
  and ``.headers`` -- ``.text``, not ``.json()``, so parsing stays strict and in the adapter
  (same rule as tests/prices/conftest.py).
* Time: ``clock()`` returns an aware UTC datetime; ``sleep(seconds)`` advances ``FakeClock``,
  so throttling is observable and every run is deterministic.
* Fixtures: every file under ``fixtures/`` is a real response captured 2026-11-13 from a
  documented, keyless endpoint, wrapped as ``{"_provenance": {...}, "response": <body>}`` so
  the body itself is untouched. Two event fixtures are subsets (``markets`` trimmed); the
  provenance note says so.
"""

from __future__ import annotations

import copy
import importlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PM_PACKAGE = "market_data_ingest.prediction_markets"

INTL = "polymarket_intl"
US = "polymarket_us"

# Recognisable fake credentials. test_pm_credentials.py asserts they never escape into
# logs, exception text, run results or the ingestion_runs table.
TEST_PMUS_KEY_ID = "PMUS-TEST-KEYID-5c1e"
TEST_PMUS_SECRET = "PMUS-TEST-SECRET-9b7d42"
TEST_PMUS_SIGNATURE = "PMUS-TEST-SIGNATURE-0a8f"

# Real identifiers from the captured fixtures.
NVDA_INTL_SLUG = "next-ceo-of-nvidia"
NVDA_US_SLUG = "nvidiaceo-2028-12-31"
KRESS_US_SLUG = "nphc-nvidiaceo-2028-12-31-colkre"


def pm(module: str) -> Any:
    """Import ``market_data_ingest.prediction_markets.<module>`` inside the test body."""
    return importlib.import_module(f"{PM_PACKAGE}.{module}")


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def response_body(name: str) -> Any:
    """The venue's response exactly as served (the ``response`` half of a fixture)."""
    return copy.deepcopy(load_fixture(name)["response"])


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = "{}"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json_body(cls, payload: Any, status_code: int = 200) -> "FakeResponse":
        return cls(status_code=status_code, text=json.dumps(payload))

    @classmethod
    def from_fixture(cls, name: str) -> "FakeResponse":
        doc = load_fixture(name)
        body = doc["response"]
        text = body if isinstance(body, str) else json.dumps(body)
        return cls(status_code=doc["_provenance"]["http_status"], text=text)


@dataclass
class RecordedCall:
    method: str
    url: str
    params: dict[str, Any]
    json: Any
    headers: dict[str, str]
    at: datetime | None


class FakeSession:
    """Stands in for ``requests.Session`` (GET and POST).

    ``responses`` is a list consumed in order (``FakeResponse`` or an exception instance to
    raise) or a callable ``(method, url, params, json) -> FakeResponse``. With ``clock``
    set, each call records the fake time it was made, so throttling can be checked.
    """

    def __init__(self, responses: list[Any] | Callable[..., Any], clock: "FakeClock | None" = None):
        self._responses = responses
        self._clock = clock
        self.calls: list[RecordedCall] = []

    def _dispatch(self, method: str, url: str, params: Any, body: Any, headers: Any) -> FakeResponse:
        self.calls.append(
            RecordedCall(
                method=method,
                url=url,
                params=dict(params or {}),
                json=copy.deepcopy(body),
                headers=dict(headers or {}),
                at=self._clock.now() if self._clock else None,
            )
        )
        if callable(self._responses):
            item = self._responses(method, url, dict(params or {}), body)
        else:
            if not self._responses:
                raise AssertionError(f"FakeSession: unexpected extra {method} to {url}")
            item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url: str, params: Any = None, headers: Any = None, timeout: Any = None, **_: Any) -> FakeResponse:
        return self._dispatch("GET", url, params, None, headers)

    def post(
        self, url: str, json: Any = None, headers: Any = None, timeout: Any = None, params: Any = None, **_: Any
    ) -> FakeResponse:
        return self._dispatch("POST", url, params, json, headers)


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


# The config shape the loader must accept. Values mirror ADR §3/§4/§7 except where a test
# overrides one to prove it is read rather than hard-coded.
BASE_CONFIG: dict[str, Any] = {
    "liquidity_threshold_usd": 50000,
    "cadence": {"default_interval_s": 3600, "watchlist_interval_s": 300},
    "watchlist": [],
    "db_path_env": "DCA_PM_DB_PATH",
    "sources": {
        INTL: {
            "license_scope": "store_ok",
            "hosts": {
                "gamma": "https://gamma-api.polymarket.com",
                "clob": "https://clob.polymarket.com",
                "data": "https://data-api.polymarket.com",
            },
            "budgets_per_10s": {
                "gamma": {"*": 4000, "/markets": 300},
                "clob": {"*": 9000, "/prices-history": 1000},
                "data": {"*": 1000, "/v2/prices-history": 200},
            },
            "max_history_batch": 20,
        },
        US: {
            "license_scope": "live_only",
            "hosts": {"gateway": "https://gateway.polymarket.us", "api": "https://api.polymarket.us"},
            "budgets_per_10s": {"gateway": {"*": 200}, "api": {"*": 250}},
            "credential_env": {"key_id": "POLYMARKET_US_KEY_ID", "secret_key": "POLYMARKET_US_SECRET_KEY"},
        },
    },
}


@pytest.fixture
def config_dict() -> dict[str, Any]:
    return copy.deepcopy(BASE_CONFIG)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    def _write(cfg: dict[str, Any]) -> Path:
        path = tmp_path / "prediction_markets.json"
        path.write_text(json.dumps(cfg))
        return path

    return _write


@pytest.fixture
def config(config_dict, write_config):
    return pm("config").load_config(write_config(config_dict))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(utc(2026, 11, 13, 3, 0))


@pytest.fixture
def store_factory(tmp_path: Path):
    def _make(cfg):
        return pm("store").PMStore(tmp_path / "pm.duckdb", config=cfg)

    return _make


def quote(**overrides: Any):
    """A ``QuoteRow`` with neutral defaults, for tests that construct rows directly."""
    fields: dict[str, Any] = {
        "venue": INTL,
        "market_id": "0xmarket",
        "outcome": "Yes",
        "ts_utc": utc(2026, 11, 12, 15),
        "bid": 0.29,
        "ask": 0.31,
        "mid": 0.30,
        "last": 0.30,
        "volume_24h": None,
        "liquidity_usd": 100000.0,
        "usable": True,
        "resolution_sec": None,
        "backfilled": False,
        "source_api": "test",
        "ingested_at": utc(2026, 11, 12, 15, 0, 5),
    }
    fields.update(overrides)
    return pm("models").QuoteRow(**fields)
