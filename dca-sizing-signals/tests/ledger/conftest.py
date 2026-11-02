"""Shared fixtures for the Phase 0 ledger tests. No network, no broker, no real accounts.

Every row built here is SYNTHETIC: hand-picked round numbers chosen so each expected
result can be checked with pencil and paper (the arithmetic is spelled out next to each
assertion). None of it is real market data, a real fill, or a real account. Tickers are
deliberately fake (``ZZZA``, ``ZZZB`` ...) so nobody mistakes a fixture for a quote.

The seams the implementation is written against:

* ``LedgerStore(path, clock=...)`` opens (or creates) one DuckDB file and owns a
  ``PriceStore`` on the SAME connection (``ledger.prices``), so the ledger's outcome macros
  can read ``bars_known_at`` / ``actions_known_at`` from the existing price store.
* Every ``ingest_*`` helper takes already-fetched plain Python dicts plus
  ``source=``, ``run_id=``, ``as_of=`` (aware UTC). Callers fetch; the ledger never does.
* Account identifiers become aliases through ``ledger.accounts.account_alias(raw, salt=...)``
  BEFORE any row reaches the store; the salt comes from ``$DCA_LEDGER_ACCOUNT_SALT``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_data_ingest.prices.models import CorporateAction, DailyBar

# A recognisable, obviously fake salt. Long enough for the MIN_SALT_LENGTH check.
TEST_SALT = "ledger-test-salt-not-a-secret-0f3e"

# The ledger ingest time for the "first look" at a batch. A later correction uses LATER.
FIRST_SEEN = datetime(2026, 4, 30, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 5, 1, 1, tzinfo=timezone.utc)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def weekdays(start: date, n: int) -> list[date]:
    """The first ``n`` weekdays from ``start`` inclusive -- a toy market calendar."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# 2026-03-02 is a Monday. SESSIONS[0] is the fill session; SESSIONS[5] is t+5;
# SESSIONS[20] is t+20. 30 sessions keep both horizons inside the calendar.
SESSIONS = weekdays(date(2026, 3, 2), 30)
D0 = SESSIONS[0]


def bar(symbol: str, session: date, close: float, *, source: str = "massive") -> DailyBar:
    # Flat OHLC around ``close`` so the price store's OHLC validation passes; only the
    # close matters to the ledger.
    return DailyBar(
        source=source,
        symbol=symbol,
        session=session,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1_000.0,
    )


def forward_split(symbol: str, ex_date: date, split_from: float, split_to: float) -> CorporateAction:
    return CorporateAction(
        source="massive",
        action_type="forward_split",
        symbol=symbol,
        ex_date=ex_date,
        split_from=split_from,
        split_to=split_to,
    )


def fill(order_id: str, *, run_kind: str = "scheduled", symbol: str = "ZZZA", session: date = D0,
         price: float = 10.0, notional: float = 100.0, multiplier: float = 2.0, slot: str = "15:30") -> dict:
    """One synthetic fill row in the shape ``LedgerStore.ingest_fills`` accepts."""
    return {
        "order_id": order_id,
        "dca_run_id": f"{run_kind}_{session:%Y%m%d}_1530ET" if run_kind == "scheduled" else "manual_x",
        "run_kind": run_kind,
        "slot": slot,
        "symbol": symbol,
        "session": session,
        "filled_at": datetime(session.year, session.month, session.day, 19, 30, tzinfo=timezone.utc),
        "price": price,
        "quantity": notional / price,
        "notional": notional,
        "multiplier": multiplier,
    }


def baseline(decision_id: str, *, run_kind: str = "scheduled", symbol: str = "ZZZA", session: date = D0,
             price: float = 10.0, base_notional: float = 50.0, slot: str = "15:30") -> dict:
    """One synthetic slot decision in the shape ``LedgerStore.ingest_baseline_fills`` accepts."""
    return {
        "decision_id": decision_id,
        "dca_run_id": f"{run_kind}_{session:%Y%m%d}_1530ET" if run_kind == "scheduled" else "manual_x",
        "run_kind": run_kind,
        "slot": slot,
        "symbol": symbol,
        "session": session,
        "decided_at": datetime(session.year, session.month, session.day, 19, 30, tzinfo=timezone.utc),
        "price": price,
        "base_notional": base_notional,
    }


@pytest.fixture
def salt(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DCA_LEDGER_ACCOUNT_SALT", TEST_SALT)
    return TEST_SALT


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.duckdb"


@pytest.fixture
def ledger(db_path: Path):
    from market_data_ingest.ledger.store import LedgerStore

    s = LedgerStore(db_path, clock=lambda: utc(2026, 5, 2, 0))
    yield s
    s.close()


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def ingest_calendar(ledger, sessions=SESSIONS, *, as_of=FIRST_SEEN, run_id="cal-1") -> int:
    return ledger.ingest_market_sessions(sessions, source="test_calendar", run_id=run_id, as_of=as_of)


def ingest_bars(ledger, staging_dir, bars, *, as_of=FIRST_SEEN, run_id="bars-1") -> int:
    return ledger.prices.ingest_daily_bars(bars, run_id=run_id, as_of=as_of, staging_dir=staging_dir)
