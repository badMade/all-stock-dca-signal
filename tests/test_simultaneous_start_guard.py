"""Simultaneous-start guard (DCA Issues keys ``duplicate_fills`` and ``competing_executor``).

Two executors that start at the same moment can both read an empty same-window order history and both
place the same buys. These tests pin the two in-repo layers added for ``dca_runner.execute_plan`` (the one
path that works from an injected, otherwise-stale history snapshot):

* B -- re-pull broker history before EVERY order and skip a ticker another writer already ordered;
* A -- a per-(day, slot) claim taken before the first order, refused when held, released afterwards.

Both fail closed: a re-pull or claim that cannot be proven means no further orders. Design:
``docs/adr-simultaneous-start-guard.md``.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import dca_runner
import dca_slot_guard
from conftest import FIXTURE_ACCOUNT_NUMBER
from dca_runner import BrokerOrder, BudgetTier

ET = ZoneInfo("America/New_York")
DAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 9, 43, tzinfo=ET)  # inside the 0935 window
ACCOUNT = FIXTURE_ACCOUNT_NUMBER


def _other_writer_order(symbol, *, state="filled", executed=None, at=NOW, amount=2.0):
    return BrokerOrder(
        symbol=symbol,
        side="buy",
        dollar_amount=amount,
        created_at=at,
        placed_agent="agentic",
        state=state,
        executed_dollar_amount=executed,
    )


def _batch(tickers, calls, history=None):
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw["symbol"]) or {"id": f"ok-{kw['symbol']}"},
        enable_live_orders=True,
        dry_run=False,
        account_number=ACCOUNT,
    )
    snapshot = dca_runner.budget_snapshot([], DAY)
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate(t, 2.0) for t in tickers]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    return broker, plan, snapshot, tier, candidates


def _run(tickers, calls, *, refetch=None, claim_root=None, holder="run-a"):
    broker, plan, snapshot, tier, candidates = _batch(tickers, calls)
    return dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=DAY, orders=[], tier=tier, buying_power_usd=1000.0,
        orders_account_number=ACCOUNT, now=NOW, now_fn=lambda: NOW, slot_id="0935",
        orders_fetched_at=NOW, candidates=candidates,
        refetch_orders_fn=refetch, slot_claim_root=claim_root, slot_claim_holder=holder,
    )


# ---------------------------------------------------------------- B: re-pull before each order


def test_other_engine_order_landing_mid_batch_skips_that_ticker():
    calls = []
    pulls = []

    def refetch():
        pulls.append(1)
        # The other engine's MSFT buy becomes visible only after our AAPL order went out.
        return [_other_writer_order("MSFT")] if calls else []

    result = _run(["AAPL", "MSFT", "NVDA"], calls, refetch=refetch)
    assert calls == ["AAPL", "NVDA"]
    assert result.submitted == ["AAPL", "NVDA"]
    assert result.skipped_already_ordered == ["MSFT"]
    assert result.halted_on is None
    assert len(pulls) == 3  # one fresh pull per planned order


def test_dead_unfilled_order_does_not_block_the_ticker():
    calls = []
    refetch = lambda: [_other_writer_order("MSFT", state="rejected", executed=None)]  # noqa: E731
    result = _run(["AAPL", "MSFT"], calls, refetch=refetch)
    assert calls == ["AAPL", "MSFT"]
    assert result.skipped_already_ordered == []


def test_partially_filled_dead_order_still_blocks_the_ticker():
    calls = []
    refetch = lambda: [_other_writer_order("MSFT", state="cancelled", executed=0.5)]  # noqa: E731
    result = _run(["AAPL", "MSFT"], calls, refetch=refetch)
    assert calls == ["AAPL"]
    assert result.skipped_already_ordered == ["MSFT"]


def test_order_from_an_earlier_slot_window_does_not_block():
    calls = []
    earlier = datetime(2026, 7, 28, 9, 20, tzinfo=ET)  # before the 0935 window opens (9:31)
    refetch = lambda: [_other_writer_order("MSFT", at=earlier)]  # noqa: E731
    result = _run(["AAPL", "MSFT"], calls, refetch=refetch)
    assert calls == ["AAPL", "MSFT"]
    assert result.skipped_already_ordered == []


def test_unprovable_repull_halts_the_remainder_fail_closed():
    calls = []

    def refetch():
        if calls:
            raise RuntimeError("connector timeout")
        return []

    result = _run(["AAPL", "MSFT", "NVDA"], calls, refetch=refetch)
    assert calls == ["AAPL"]
    assert result.halted_on == "MSFT"
    assert "re-pull" in result.failure_reason
    assert "connector timeout" not in result.failure_reason  # exception text is never persisted


def test_malformed_repull_halts_fail_closed():
    calls = []
    result = _run(["AAPL", "MSFT"], calls, refetch=lambda: None)
    assert calls == []
    assert result.halted_on == "AAPL"


def test_guard_required_flag_refuses_a_live_batch_without_repull(monkeypatch):
    monkeypatch.setattr(dca_runner, "REQUIRE_SIMULTANEOUS_START_GUARD", True)
    calls = []
    result = _run(["AAPL"], calls)
    assert calls == []
    assert "simultaneous-start guard" in (result.validation_failure or result.failure_reason or "")


def test_without_the_flag_legacy_callers_are_unchanged():
    calls = []
    result = _run(["AAPL", "MSFT"], calls)
    assert calls == ["AAPL", "MSFT"]
    assert result.skipped_already_ordered == []


# ---------------------------------------------------------------- A: slot claim


def test_claim_blocks_a_second_holder_and_releases_after_the_batch(tmp_path):
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="run-a"):
        with pytest.raises(dca_slot_guard.SlotClaimUnavailable):
            with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="run-b"):
                pytest.fail("second claimant must not enter")
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="run-b"):
        pass  # released, so a later run may claim


def test_claim_is_per_slot_and_per_day(tmp_path):
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="a"):
        with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "1030", holder="b"):
            with dca_slot_guard.slot_claim(tmp_path, "2026-07-29", "0935", holder="c"):
                pass


def test_claim_released_when_the_body_raises(tmp_path):
    with pytest.raises(ValueError):
        with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="a"):
            raise ValueError("boom")
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="b"):
        pass


def test_unusable_claim_root_fails_closed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(dca_slot_guard.SlotClaimUnavailable):
        with dca_slot_guard.slot_claim(blocker / "sub", "2026-07-28", "0935", holder="a"):
            pytest.fail("must not enter when the claim cannot be proven")


def test_execute_plan_places_nothing_when_the_slot_is_claimed_elsewhere(tmp_path):
    calls = []
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="other-engine"):
        result = _run(["AAPL", "MSFT"], calls, claim_root=tmp_path)
    assert calls == []
    assert result.halted_on == "AAPL"
    assert "slot claim" in result.failure_reason


def test_execute_plan_releases_its_claim_after_the_batch(tmp_path):
    calls = []
    _run(["AAPL"], calls, claim_root=tmp_path)
    assert calls == ["AAPL"]
    with dca_slot_guard.slot_claim(tmp_path, "2026-07-28", "0935", holder="next"):
        pass


def test_simultaneous_start_race_places_each_ticker_once(tmp_path):
    """Two engines hit execute_plan at the same instant with the same empty snapshot."""
    placed = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    results = {}

    def run(name):
        broker = dca_runner.build_runner_broker(
            place_fn=lambda **kw: (lock.acquire(), placed.append(kw["symbol"]), lock.release(), {"id": "ok"})[-1],
            enable_live_orders=True, dry_run=False, account_number=ACCOUNT,
        )
        snapshot = dca_runner.budget_snapshot([], DAY)
        tier = BudgetTier("normal", 180.0, 900.0)
        candidates = [dca_runner.Candidate(t, 2.0) for t in ("AAPL", "MSFT")]
        plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
        barrier.wait()
        results[name] = dca_runner.execute_plan(
            plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot, trading_day=DAY, orders=[],
            tier=tier, buying_power_usd=1000.0, orders_account_number=ACCOUNT, now=NOW,
            now_fn=lambda: NOW, slot_id="0935", orders_fetched_at=NOW, candidates=candidates,
            refetch_orders_fn=lambda: [_other_writer_order(s) for s in placed],
            slot_claim_root=tmp_path, slot_claim_holder=name,
        )

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(placed) == ["AAPL", "MSFT"]  # never a second buy of either
