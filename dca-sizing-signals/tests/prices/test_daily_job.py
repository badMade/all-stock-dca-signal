"""The nightly job: calendar check, run record, fetch, stage, validate, insert, re-pull
T-1..T-5, corporate actions since T-30, mark ok/failed, exit non-zero on failure.

Expected behaviour (PLAN-price-source.md §4 and VTO success criterion 1): each batch is
atomic; batches are independent; the run status, not the presence of rows, says whether
the job finished; a closed market is a clean skip; nothing is fetched for a session that
has not closed yet; reruns are idempotent.
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import date
from pathlib import Path

import pytest

from market_data_ingest.prices.daily_job import main, resolve_db_path, run_daily_job
from market_data_ingest.prices.errors import ProviderError, RouterExhaustedError
from market_data_ingest.prices.models import CorporateAction, DailyBar

from .conftest import FakeClock, FixedCalendar, utc, weekdays

CAL = FixedCalendar(weekdays(date(2024, 5, 1), date(2024, 6, 30)))
S = date(2024, 6, 11)
PRIOR_5 = [date(2024, 6, 10), date(2024, 6, 7), date(2024, 6, 6), date(2024, 6, 5), date(2024, 6, 4)]


def bar(symbol, session, close, source="massive"):
    return DailyBar(
        source=source,
        symbol=symbol,
        session=session,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000.0,
        vwap=None,
        trades=None,
    )


class FakeRouter:
    """Returns ``bars[session]``; raises an error configured per session or for actions."""

    def __init__(self, bars=None, actions=(), errors=None, action_error=None):
        self.bars = (
            bars if bars is not None else {d: [bar("NVDA", d, 100.0), bar("AAPL", d, 200.0)] for d in [S, *PRIOR_5]}
        )
        self.actions, self.errors, self.action_error = list(actions), errors or {}, action_error
        self.bar_calls: list[date] = []
        self.action_calls: list[date] = []

    def daily_bars(self, session):
        self.bar_calls.append(session)
        if session in self.errors:
            raise self.errors[session]
        return self.bars.get(session, [])

    def corporate_actions(self, since):
        self.action_calls.append(since)
        if self.action_error is not None:
            raise self.action_error
        return self.actions

    def last_trade(self, symbols):  # pragma: no cover - the daily job never asks for trades
        raise AssertionError("daily job must not request last trades")


@pytest.fixture
def run_ids():
    counter = itertools.count(1)
    return lambda: f"run-{next(counter):03d}"


def run(store, staging_dir, router, run_ids, *, session=S, clock=None, calendar=CAL):
    clock = clock or FakeClock(utc(2024, 6, 12, 2))  # 22:00 ET on S
    return run_daily_job(
        session=session,
        store=store,
        router=router,
        calendar=calendar,
        clock=clock.now,
        staging_dir=staging_dir,
        run_id_factory=run_ids,
    )


def test_closed_market_is_a_clean_skip(store, staging_dir, run_ids):
    router = FakeRouter()

    result = run(store, staging_dir, router, run_ids, session=date(2024, 6, 8), clock=FakeClock(utc(2024, 6, 9, 2)))

    assert (result.status, result.exit_code) == ("skipped_closed", 0)
    assert store.run_status(result.run_id) == "skipped_closed"
    assert router.bar_calls == [] and router.action_calls == []
    assert store.row_count("daily_bars_raw") == 0


def test_happy_path_fetches_session_repulls_five_prior_and_actions_since_t_minus_30(store, staging_dir, run_ids):
    actions = [
        CorporateAction(
            source="massive",
            action_type="forward_split",
            symbol="NVDA",
            ex_date=date(2024, 6, 10),
            split_from=1,
            split_to=10,
        )
    ]
    router = FakeRouter(actions=actions)

    result = run(store, staging_dir, router, run_ids)

    assert (result.status, result.exit_code) == ("ok", 0)
    assert router.bar_calls[0] == S
    assert sorted(router.bar_calls[1:]) == sorted(PRIOR_5)
    assert router.action_calls == [date(2024, 5, 12)]
    assert store.row_count("daily_bars_raw") == 12
    assert store.row_count("corporate_actions") == 1
    assert [e["status"] for e in store.run_events(result.run_id)] == ["running", "ok"]
    assert any((staging_dir / result.run_id).glob("*.parquet"))


def test_rerunning_the_same_night_is_idempotent(store, staging_dir, run_ids):
    first = run(store, staging_dir, FakeRouter(), run_ids)
    rows = store.row_count("daily_bars_raw")
    second = run(store, staging_dir, FakeRouter(), run_ids)

    assert first.run_id != second.run_id
    assert (first.status, second.status) == ("ok", "ok")
    assert store.row_count("daily_bars_raw") == rows


def test_t_minus_1_repull_captures_a_vendor_correction(store, staging_dir, run_ids):
    monday = date(2024, 6, 10)
    run(
        store,
        staging_dir,
        FakeRouter(bars={monday: [bar("NVDA", monday, 121.79)]}),
        run_ids,
        session=monday,
        clock=FakeClock(utc(2024, 6, 11, 2)),
    )
    corrected = {S: [bar("NVDA", S, 120.91)], monday: [bar("NVDA", monday, 121.80)]}
    result = run(store, staging_dir, FakeRouter(bars=corrected), run_ids)

    assert result.status == "ok"
    monday_rows = [r for r in store.raw_bars("NVDA") if r["session"] == monday]
    assert sorted(r["close"] for r in monday_rows) == pytest.approx([121.79, 121.80])
    latest = [r for r in store.adjusted_bars("NVDA", as_of_date=S) if r.session == monday][0]
    assert latest.close_raw == pytest.approx(121.80)


def test_invalid_session_batch_fails_the_run_and_writes_nothing_from_it(store, staging_dir, run_ids):
    bad = dataclasses.replace(bar("NVDA", S, 100.0), low=-1.0)
    router = FakeRouter(bars={S: [bar("AAPL", S, 200.0), bad]})

    result = run(store, staging_dir, router, run_ids)

    assert (result.status, result.exit_code) == ("failed", 1)
    assert result.error_class == "ValidationError"
    assert [r for r in store.raw_bars("AAPL") if r["session"] == S] == []


def test_failure_in_a_later_batch_keeps_earlier_batches_and_fails_the_run(store, staging_dir, run_ids):
    router = FakeRouter(action_error=ProviderError("splits endpoint down", code="http_5xx"))

    result = run(store, staging_dir, router, run_ids)

    assert (result.status, result.exit_code) == ("failed", 1)
    assert result.error_class == "ProviderError"
    assert [r for r in store.raw_bars("NVDA") if r["session"] == S], "the complete session batch stays committed"
    events = store.run_events(result.run_id)
    assert [e["status"] for e in events] == ["running", "failed"]
    assert events[-1]["error_class"] == "ProviderError"


def test_router_exhausted_fails_the_run(store, staging_dir, run_ids):
    exhausted = RouterExhaustedError(ProviderError("p", code="http_5xx"), ProviderError("f", code="timeout"))
    router = FakeRouter(errors={S: exhausted})

    result = run(store, staging_dir, router, run_ids)

    assert (result.status, result.exit_code, result.error_class) == ("failed", 1, "RouterExhaustedError")
    assert [r for r in store.raw_bars("NVDA") if r["session"] == S] == []


def test_a_session_that_has_not_closed_is_not_fetched(store, staging_dir, run_ids):
    router = FakeRouter()

    result = run(store, staging_dir, router, run_ids, clock=FakeClock(utc(2024, 6, 11, 23)))  # 19:00 ET

    assert (result.status, result.exit_code, result.error_class) == ("failed", 1, "SessionNotClosed")
    assert router.bar_calls == []


def test_calendar_outage_fails_instead_of_skipping(store, staging_dir, run_ids):
    class DownCalendar:
        def is_session(self, d):
            raise ProviderError("calendar down", code="http_5xx")

        def previous_sessions(self, d, n):
            raise ProviderError("calendar down", code="http_5xx")

    result = run(store, staging_dir, FakeRouter(), run_ids, calendar=DownCalendar())

    assert (result.status, result.exit_code) == ("failed", 1)


# --- CLI ----------------------------------------------------------------------------------


def test_db_path_precedence_is_flag_then_env_then_repo_default(tmp_path):
    repo = tmp_path / "repo"
    assert resolve_db_path("/x/cli.duckdb", {"DCA_PRICE_DB": "/x/env.duckdb"}, repo) == Path("/x/cli.duckdb")
    assert resolve_db_path(None, {"DCA_PRICE_DB": "/x/env.duckdb"}, repo) == Path("/x/env.duckdb")
    assert resolve_db_path(None, {}, repo) == repo / "data" / "backfill.duckdb"


def test_cli_exits_non_zero_without_credentials(tmp_path, capsys):
    code = main(["--session", "2024-06-11", "--db", str(tmp_path / "p.duckdb")], env={})

    assert code != 0
    err = capsys.readouterr().err
    assert "MASSIVE_API_KEY" in err


def test_an_ambiguous_action_fails_the_run_but_other_actions_are_stored(store, staging_dir, run_ids):
    """PR #2282 review: one ambiguous identity must not block every symbol for the 30-day window."""
    ex = date(2024, 6, 10)

    def split(symbol, split_to):
        return CorporateAction(
            source="massive", action_type="forward_split", symbol=symbol, ex_date=ex, split_from=1, split_to=split_to
        )

    router = FakeRouter(actions=[split("NVDA", 10), split("NVDA", 20), split("AAPL", 4), split("AAPL", 4)])

    result = run(store, staging_dir, router, run_ids)

    assert (result.status, result.exit_code, result.error_class) == ("failed", 1, "AmbiguousActionsError")
    assert store.con.execute("SELECT symbol FROM corporate_actions").fetchall() == [("AAPL",)]
    assert store.run_events(result.run_id)[-1]["row_count"] == 12 + 1
