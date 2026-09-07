"""System-level verification suite: the live-reliability contract.

What this file is for
---------------------
Every other ``test_*.py`` in this repo verifies one module against its own
contract. This one verifies the *system-level* promises that live money rests
on -- the ones written as prose in ``docs/safeguards.md``,
``docs/reliability_spec.md``, and ``config/budgets.json`` -- against the REAL
production functions that would run against a real brokerage account.

It is deliberately cross-cutting. Each section names the clause it pins and
exercises the shipped code path end to end, so a refactor that quietly moves a
guard (or a config edit that quietly relaxes one) fails here even when every
per-module suite still passes.

Safety posture of this file itself
----------------------------------
* **No live orders.** Placement only ever reaches an in-test ``place_fn`` /
  ``DryRunBroker``; both of ``dca_runner``'s live gates are exercised, never
  handed a real connector.
* **No network.** ``_no_network`` below is autouse for the whole module and
  makes ``socket`` / ``urllib`` raise. Every test here therefore *proves*, not
  merely asserts, that the code path it exercises reaches no brokerage.
  ``test_the_no_network_guard_actually_bites`` is the positive control for that
  claim.
* **No secrets.** The account number comes from ``conftest.FIXTURE_ACCOUNT_NUMBER``
  (synthetic, set before any import), so nothing here can pick up a real
  account number from the ambient environment, and
  ``test_config_invariants.py``'s account-shaped-literal scan stays green.

Naming: helpers are prefixed ``_``; every test names the *contract clause* it
pins rather than the function it calls, because the function is an
implementation detail of the clause and the clause is what must not regress.
"""

from __future__ import annotations

import re
import socket
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import dca_runner
from conftest import FIXTURE_ACCOUNT_NUMBER
from near_close_dca import BudgetTier, DryRunBroker, iso_week_key
from scripts import reason_codes as rc
from scripts.broker_reconciliation import (
    EffectivePrior,
    decide_effective_top_up,
    paginate_same_day_orders,
    reconcile_effective_prior,
    same_day_order_snapshot,
)
from scripts.idempotency import build_idempotency_key, deterministic_ref_id
from scripts.run_classification import PriorAllocation
from scripts.run_state_machine import (
    DECIDED,
    PARTIALLY_RECONCILED,
    PREFLIGHT,
    PUBLISHED,
    RECONCILED,
    SUBMITTED,
    VERIFIED,
    IllegalTransitionError,
    RunStateMachine,
)
from scripts.scheduler_lease import SchedulerLease, window_key

ET = ZoneInfo("America/New_York")

# The synthetic account conftest.py pins before any module import. execute_plan
# requires broker.account_number to equal exactly this (Codex review: "Verify
# the authorized account before placing orders").
ACCOUNT = FIXTURE_ACCOUNT_NUMBER

# A Tuesday inside config/trading_window.json's approved window, used as "today"
# throughout so the market-day, holiday, and window gates all pass and the tests
# below isolate the guard each one is actually about.
TRADING_DAY = date(2026, 7, 28)
# 09:43 ET is inside the 9:35 slot's acceptance window; 10:43 ET is inside the
# 10:30 slot's. The later slot is used whenever a test needs same-day prior
# orders that must NOT trip Gate 4's sole-executor interlock.
NOW_0935 = datetime(2026, 7, 28, 9, 43, 14, tzinfo=ET)
NOW_1030 = datetime(2026, 7, 28, 10, 43, 14, tzinfo=ET)

BUYING_POWER = 1000.0
NORMAL_TIER = BudgetTier("normal", 180.0, 900.0)


# ---------------------------------------------------------------------------
# Hermetic guarantee
# ---------------------------------------------------------------------------


class _NetworkAttempted(RuntimeError):
    """Raised instead of opening a socket, so a leak is a loud test failure."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Make every outbound network primitive raise, for every test in this file.

    This is what turns "no test here contacts a brokerage" from a claim in a
    docstring into something the suite enforces. It is autouse rather than
    opt-in precisely so a test added later cannot forget it.
    """

    def _blocked(*args, **kwargs):
        raise _NetworkAttempted(
            "this suite is hermetic: no test may open a network connection"
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tickers(n, *, skip=0):
    """``n`` real whitelisted tickers, deterministically chosen.

    Taken from the live ``ACTIVE_TICKERS`` set rather than invented, so a
    whitelist edit that removes a ticker surfaces here instead of leaving the
    test passing against a symbol the strategy would never buy.

    ``skip`` carves out disjoint bands so a test's prior-order history and its
    candidates never accidentally overlap. Under-delivery raises rather than
    returning a short list: a silently shorter batch makes a cap test pass for
    the wrong reason (it never reached the boundary), which is the worst
    possible failure mode for this file.
    """
    pool = sorted(dca_runner.ACTIVE_TICKERS)
    chosen = pool[skip : skip + n]
    if len(chosen) != n:
        raise AssertionError(
            f"the active whitelist has only {len(pool)} tickers; this suite asked for "
            f"{n} starting at index {skip}. Re-band the _tickers() slices rather than "
            "letting a cap test run against a short batch."
        )
    return chosen


def _cands(n, *, amount=2.0, multiplier=1.0, skip=0):
    return [dca_runner.Candidate(t, amount, multiplier) for t in _tickers(n, skip=skip)]


def _broker_order(symbol, amount, *, hour_et=9, minute_et=40, state="filled", side="buy"):
    """A same-day broker history row, timestamped in ET and stored in UTC."""
    moment = datetime(
        TRADING_DAY.year, TRADING_DAY.month, TRADING_DAY.day, hour_et, minute_et, tzinfo=ET
    ).astimezone(timezone.utc)
    return dca_runner.BrokerOrder(
        symbol=symbol,
        side=side,
        dollar_amount=amount,
        created_at=moment,
        placed_agent="agentic",
        state=state,
    )


def _recorder():
    """A ``place_fn`` that records its kwargs and returns a minimal success.

    The response carries only ``id``/``state`` -- the shape
    ``_placement_failure_reason`` treats as a clean submission -- so these
    tests exercise the placement path itself rather than the response-shape
    checks ``test_dca_runner.py`` already covers exhaustively.
    """
    calls = []

    def place_fn(**kwargs):
        calls.append(kwargs)
        return {"id": f"ORD-{len(calls)}", "state": "queued"}

    return calls, place_fn


def _live_broker(place_fn, *, dry_run=False, enable_live_orders=True, account_number=ACCOUNT):
    return dca_runner.build_runner_broker(
        place_fn=place_fn,
        enable_live_orders=enable_live_orders,
        dry_run=dry_run,
        account_number=account_number,
    )


def _execute(plan, broker, *, candidates, orders=(), snapshot=None, tier=NORMAL_TIER, **over):
    """Call the real ``execute_plan`` with every gate satisfied by default.

    Everything a live submission genuinely requires -- run id, fresh clock,
    slot, fetch age, account pinning, verified candidates -- is supplied here
    so each test can override exactly the one input it is about.
    """
    kwargs = dict(
        run_id=f"contract-{TRADING_DAY.isoformat()}",
        snapshot=snapshot if snapshot is not None else dca_runner.budget_snapshot(list(orders), TRADING_DAY),
        tier=tier,
        trading_day=TRADING_DAY,
        orders=list(orders),
        orders_account_number=ACCOUNT,
        now=NOW_0935,
        now_fn=lambda: NOW_0935,
        slot_id="0935",
        orders_fetched_at=NOW_0935,
        buying_power_usd=BUYING_POWER,
        candidates=list(candidates),
    )
    kwargs.update(over)
    return dca_runner.execute_plan(plan, broker, **kwargs)


def _assess(**over):
    kwargs = dict(
        slot_id="0935",
        orders=[],
        candidates=[],
        now=NOW_0935,
        buying_power_usd=BUYING_POWER,
    )
    kwargs.update(over)
    return dca_runner.assess(**kwargs)


# ===========================================================================
# 1. Hermetic guarantee -- nothing in this suite reaches a brokerage
# ===========================================================================


def test_the_no_network_guard_actually_bites():
    """Positive control for every other test in this file.

    Without this, ``_no_network`` could silently stop patching (a stdlib
    rename, a monkeypatch typo) and every "proves no network" claim below
    would quietly become vacuous.
    """
    with pytest.raises(_NetworkAttempted):
        socket.socket()
    with pytest.raises(_NetworkAttempted):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(_NetworkAttempted):
        urllib.request.urlopen("https://example.invalid")


def test_the_whole_decision_path_runs_without_a_network():
    """``assess`` is the runner's decision entry point. Under the guard above,
    a full PROCEED run proves the decision is computed purely from injected
    inputs -- no quote fetch, no order-history call, no hidden connector."""
    result = _assess(candidates=[(t, 2.0, 1.0) for t in _tickers(3)])
    assert result["verdict"] == "PROCEED"
    assert result["plan"]["to_place"]


def test_a_live_placement_run_reaches_only_the_injected_callable():
    """The placement path too: both gates flipped, a real submission recorded,
    and still no socket opened. Placement talks to ``place_fn`` and nothing
    else."""
    calls, place_fn = _recorder()
    cands = _cands(2)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    result = _execute(plan, _live_broker(place_fn), candidates=cands)
    assert result.submitted == [o.ticker for o in plan.to_place]
    assert len(calls) == 2


# ===========================================================================
# 2. Both live gates default OFF
# ===========================================================================
#
# docs/live_wiring.md's two-gate design: a wired placement callable is not, by
# itself, permission to trade. dry_run mirrors StrategyConfig.dry_run and
# enable_live_orders is the separate live switch; BOTH must be flipped.


def test_a_freshly_built_runner_broker_cannot_trade():
    broker = dca_runner.build_runner_broker(place_fn=lambda **kw: None)
    assert broker.dry_run is True
    assert broker.enable_live_orders is False


@pytest.mark.parametrize(
    "dry_run,enable_live_orders",
    [(True, False), (True, True), (False, False)],
)
def test_one_gate_alone_never_submits(dry_run, enable_live_orders):
    """Three of the four gate combinations must place nothing. Only
    ``dry_run=False`` AND ``enable_live_orders=True`` may submit."""
    calls, place_fn = _recorder()
    cands = _cands(2)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    result = _execute(
        plan,
        _live_broker(place_fn, dry_run=dry_run, enable_live_orders=enable_live_orders),
        candidates=cands,
    )
    assert calls == []
    assert result.submitted == []
    assert result.placement_enabled is False


def test_a_blocked_gate_is_distinguishable_from_an_empty_plan():
    """Both cases submit nothing, but they mean opposite things: "nothing
    qualified today" versus "two orders qualified and the live switch is off".
    ``placement_enabled`` is what separates them, and a watchdog that cannot
    tell them apart will either alert every day or never."""
    cands = _cands(2)
    qualified = dca_runner.plan_orders(
        cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER
    )
    assert len(qualified.to_place) == 2

    gated = _execute(qualified, _live_broker(lambda **kw: None, dry_run=True), candidates=cands)
    nothing_qualified = _execute(
        dca_runner.Plan(to_place=[]), _live_broker(lambda **kw: None), candidates=[]
    )
    assert gated.submitted == nothing_qualified.submitted == []
    assert gated.placement_enabled is False
    assert nothing_qualified.placement_enabled is True


# ===========================================================================
# 3. Buys only, market, gfd, notional -- docs/safeguards.md "Trading constraints"
# ===========================================================================


def test_every_submitted_order_is_a_market_buy_with_the_configured_shape():
    """"No sells. Buys only." plus "market buys with a dollar_amount notional
    and time_in_force = gfd". Pinned against what actually reaches the broker
    callable, not against the plan's intent."""
    calls, place_fn = _recorder()
    cands = _cands(3)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    _execute(plan, _live_broker(place_fn), candidates=cands)

    assert calls, "expected the batch to reach the broker callable"
    for call in calls:
        assert call["side"] == "buy"
        assert call["type"] == "market"
        assert call["time_in_force"] == "gfd"
        assert call["account_number"] == ACCOUNT
        # A notional dollar amount, cent-rounded exactly as the broker receives it.
        assert re.fullmatch(r"\d+\.\d{2}", call["dollar_amount"])
        assert float(call["dollar_amount"]) <= dca_runner.MAX_PER_ORDER_USD


def test_the_submitted_shape_matches_the_committed_order_rules_config():
    """The order type and time-in-force are not free-floating literals in the
    runner: they must equal ``config/budgets.json``'s ``order_rules``, so a
    config edit and the code can never silently disagree."""
    rules = dca_runner._read_config("budgets.json")["order_rules"]
    calls, place_fn = _recorder()
    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    _execute(plan, _live_broker(place_fn), candidates=cands)

    assert calls[0]["type"] == rules["order_type"]
    assert calls[0]["time_in_force"] == rules["time_in_force"]
    assert rules["notional_field"] == "dollar_amount"
    assert "dollar_amount" in calls[0]


def test_a_sell_side_order_never_reaches_the_broker():
    """``PlannedOrder`` is public and constructible independently of
    ``plan_orders``. A hand-built sell must be refused at the placement
    boundary -- "no sells" is an account-level rule, not a planner
    convention."""
    calls, place_fn = _recorder()
    ticker = _tickers(1)[0]
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder(ticker, 2.0, side="sell")])
    with pytest.raises(ValueError):
        _execute(plan, _live_broker(place_fn), candidates=[dca_runner.Candidate(ticker, 2.0)])
    assert calls == []


# ===========================================================================
# 4. Account pinning -- docs/reliability_spec.md §3 "Pin the account"
# ===========================================================================


@pytest.mark.parametrize("wrong_account", [None, "", "123456789"])
def test_placement_is_refused_against_an_unpinned_or_foreign_account(wrong_account):
    """"No reconciliation or submission against an unpinned account." An unset
    account makes every submission fail against nothing; a *different* valid
    account places real buys somewhere the strategy was never authorized."""
    calls, place_fn = _recorder()
    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    with pytest.raises(ValueError):
        _execute(
            plan,
            _live_broker(place_fn, account_number=wrong_account),
            candidates=cands,
        )
    assert calls == []


def test_the_order_history_must_come_from_the_same_pinned_account():
    """Caps are recomputed at placement time from ``orders``. History fetched
    for a different account would authorize spend against a balance and a
    day's exposure that are not this account's."""
    calls, place_fn = _recorder()
    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    with pytest.raises(ValueError):
        _execute(plan, _live_broker(place_fn), candidates=cands, orders_account_number="123456789")
    assert calls == []


def test_the_reconciliation_snapshot_refuses_an_empty_account():
    """The same rule on the reconciliation side of the spec."""
    with pytest.raises(ValueError):
        same_day_order_snapshot(
            lambda **kw: {"results": []},
            account_number="",
            created_at_gte="2026-07-28T00:00:00Z",
        )
    with pytest.raises(ValueError):
        paginate_same_day_orders(
            lambda **kw: {"results": []},
            account_number="",
            created_at_gte="2026-07-28T00:00:00Z",
        )


# ===========================================================================
# 5. Circuit breakers hold at the PLACEMENT boundary, not just in the planner
# ===========================================================================
#
# docs/safeguards.md "Circuit breakers". Every case below builds a Plan by
# hand -- the way a buggy or malicious caller could -- and asserts nothing
# reaches the broker. Planner-side coverage lives in test_dca_runner.py; what
# is pinned here is that the LAST line of defence also holds.


def test_a_non_whitelisted_ticker_never_reaches_the_broker():
    """"Whitelisted (active-universe) tickers only.\""""
    calls, place_fn = _recorder()
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("BYDDY", 2.0)])
    with pytest.raises(ValueError, match="non-whitelisted"):
        _execute(plan, _live_broker(place_fn), candidates=_cands(1))
    assert calls == []


def test_an_over_ceiling_order_never_reaches_the_broker():
    """"Max per order: $10" -- the absolute ceiling across all live streams.
    Refused, never clamped: clamping would hide the sizing bug that produced
    it."""
    calls, place_fn = _recorder()
    ticker = _tickers(1)[0]
    over = dca_runner.MAX_PER_ORDER_USD + 0.01
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder(ticker, over)])
    with pytest.raises(ValueError, match="over-cap"):
        _execute(plan, _live_broker(place_fn), candidates=[dca_runner.Candidate(ticker, over)])
    assert calls == []


def test_the_per_order_ceiling_equals_the_configured_circuit_breaker():
    """The $10 ceiling the placement boundary enforces must be the number in
    ``config/budgets.json``, not a constant that drifted away from it."""
    breakers = dca_runner._read_config("budgets.json")["circuit_breakers"]
    assert dca_runner.MAX_PER_ORDER_USD == breakers["max_per_order_usd"]


def test_a_plan_built_against_a_stale_cheaper_snapshot_is_refused():
    """The day's spend is recomputed at placement time from the fresh order
    history, never trusted from the snapshot the plan was built with.

    A plan built when the day looked empty, submitted after $178 of the $180
    Normal daily cap was already spent, must not ride the stale view through
    to the broker. (The prior spend is carried on two large rows rather than
    many small ones so this isolates the *notional* cap from the separate
    order-count cap tested below.)
    """
    calls, place_fn = _recorder()
    cands = _cands(3, skip=10)
    empty_day = dca_runner.budget_snapshot([], TRADING_DAY)
    plan = dca_runner.plan_orders(cands, empty_day, NORMAL_TIER)
    assert len(plan.to_place) == 3  # the stale view authorized all three

    already_spent = [_broker_order(t, 89.0) for t in _tickers(2, skip=30)]
    with pytest.raises(ValueError):
        _execute(plan, _live_broker(place_fn), candidates=cands, orders=already_spent, snapshot=empty_day)
    assert calls == []


def test_a_plan_is_refused_once_the_days_order_count_is_exhausted():
    """The count cap is shared across all seven slots and manual buying, and
    it binds at the placement boundary too -- a plan computed before the day
    filled up cannot be submitted after it did."""
    calls, place_fn = _recorder()
    cands = _cands(2, skip=10)
    empty_day = dca_runner.budget_snapshot([], TRADING_DAY)
    plan = dca_runner.plan_orders(cands, empty_day, NORMAL_TIER)

    full_day = [
        _broker_order(t, 2.0) for t in _tickers(dca_runner.MAX_ORDERS_PER_DAY, skip=20)
    ]
    with pytest.raises(ValueError):
        _execute(plan, _live_broker(place_fn), candidates=cands, orders=full_day, snapshot=empty_day)
    assert calls == []


def test_buying_power_is_an_independent_ceiling_on_top_of_the_caps():
    """"Never exceed ... available buying power." A day with full cap headroom
    but $3 of cash may place one $2 order, not three."""
    cands = _cands(3)
    plan = dca_runner.plan_orders(
        cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER, buying_power_usd=3.0
    )
    assert len(plan.to_place) == 1
    assert dca_runner.SKIPPED_INSUFFICIENT_BUYING_POWER in {r for _, r in plan.skipped}


def test_assess_refuses_to_decide_without_confirmed_buying_power():
    """Unknown buying power is not "assume enough". With no confirmed balance
    the run aborts rather than planning against an unverified ceiling."""
    result = _assess(candidates=[(t, 2.0, 1.0) for t in _tickers(2)], buying_power_usd=None)
    assert result["verdict"] == "ABORT"


# ===========================================================================
# 6. The shared daily order-count cap, end to end through assess()
# ===========================================================================
#
# config/budgets.json circuit_breakers.max_orders_per_day -- THE single knob
# for how many orders a day the roster may place across all seven slots and
# manual buying.


def test_the_configured_count_cap_is_what_the_runner_enforces():
    breakers = dca_runner._read_config("budgets.json")["circuit_breakers"]
    assert dca_runner.MAX_ORDERS_PER_DAY == breakers["max_orders_per_day"]
    assert dca_runner.MAX_ORDERS_PER_DAY >= 1


def test_assess_stops_a_batch_at_the_days_shared_order_count():
    """A later slot sees the whole day's attempts, not just its own. With the
    cap already consumed by earlier slots, this slot plans nothing and says
    why."""
    earlier = [
        _broker_order(t, 2.0, hour_et=9, minute_et=40)
        for t in _tickers(dca_runner.MAX_ORDERS_PER_DAY, skip=20)
    ]
    result = _assess(
        slot_id="1030",
        now=NOW_1030,
        orders=earlier,
        candidates=[(t, 2.0, 1.0) for t in _tickers(3)],
    )
    assert result["plan"]["to_place"] == []
    assert {r for _, r in result["plan"]["skipped"]} == {
        dca_runner.SKIPPED_DAILY_ORDER_COUNT_CAP
    }
    assert result["budget"]["day_order_count"] == dca_runner.MAX_ORDERS_PER_DAY


def test_a_cancelled_attempt_still_consumes_one_of_the_days_orders():
    """Counted as buy *attempts*, not filled spend: over-counting is the safe
    direction for a count ceiling, and a cancelled order did occupy a slot."""
    earlier = [
        _broker_order(t, 2.0, hour_et=9, minute_et=40, state="cancelled")
        for t in _tickers(dca_runner.MAX_ORDERS_PER_DAY, skip=20)
    ]
    snapshot = dca_runner.budget_snapshot(earlier, TRADING_DAY)
    assert snapshot.day_order_count == dca_runner.MAX_ORDERS_PER_DAY
    assert snapshot.day_notional_usd == pytest.approx(0.0)  # no spend committed
    plan = dca_runner.plan_orders(_cands(2), snapshot, NORMAL_TIER)
    assert plan.to_place == []


def test_the_run_evidence_names_the_limits_that_were_in_force():
    """A run's own evidence must be enough to explain its outcome later,
    without re-reading config that may since have changed."""
    result = _assess(candidates=[(t, 2.0, 1.0) for t in _tickers(2)])
    budget = result["budget"]
    assert budget["max_orders_per_day"] == dca_runner.MAX_ORDERS_PER_DAY
    assert budget["one_order_per_ticker_per_day"] == dca_runner.ONE_ORDER_PER_TICKER_PER_DAY
    assert budget["daily_cap_usd"] == NORMAL_TIER.daily_cap_usd
    assert budget["day_order_count"] == 0


# ===========================================================================
# 7. Fail closed on unproven broker history -- reliability_spec §3/§4
# ===========================================================================
#
# The 2026-07-23 incident in one sentence: artifacts showed $0 prior, the
# guard failed OPEN to the full desired amount, and the re-buy collided with a
# real live fill. These pin the fixed direction.


@pytest.mark.parametrize(
    "page",
    [
        {"error": "upstream unavailable"},
        {"success": False},
        "not-a-dict",
    ],
)
def test_an_unusable_orders_page_makes_the_snapshot_incomplete(page):
    result = paginate_same_day_orders(
        lambda **kw: page,
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
    )
    assert result.complete is False


def test_a_paging_outage_makes_the_snapshot_incomplete():
    def _boom(**kwargs):
        raise RuntimeError("connector timeout")

    result = same_day_order_snapshot(
        _boom, account_number=ACCOUNT, created_at_gte="2026-07-28T00:00:00Z"
    )
    assert result.complete is False


def test_an_artifact_order_id_the_broker_never_surfaced_fails_closed():
    """"Fall back per order_id ... an artifact fill is never silently
    dropped." An id that neither paging nor the bounded single-order lookup
    resolves must poison completeness rather than be assumed absent."""
    result = same_day_order_snapshot(
        lambda **kw: {"orders": []},
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
        expected_order_ids=["order-that-vanished"],
    )
    assert result.unresolved_order_ids == ["order-that-vanished"]
    assert result.complete is False


def test_every_raw_page_is_persisted_before_any_derived_output():
    """The raw-input contract: raw broker input must be durable before
    anything is derived from it, so a decision is always replayable."""
    sink = []
    pages = [
        {"orders": [], "next_cursor": "c2"},
        {"orders": []},
    ]
    calls = {"n": 0}

    def _orders_fn(**kwargs):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    result = same_day_order_snapshot(
        _orders_fn,
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
        raw_sink=sink.append,
    )
    assert len(sink) == 2
    assert result.raw_responses == pages


def test_an_unproven_history_buys_zero_rather_than_the_full_desired_amount():
    """The headline regression. Coverage not provably complete must yield
    ``BROKER_HISTORY_INCOMPLETE`` and a $0 buy -- never a fall back to the
    full desired amount on an unproven-empty history."""
    effective = EffectivePrior(
        symbol="TXN",
        artifact_prior_usd=0.0,
        broker_prior_usd=0.0,
        effective_prior_usd=0.0,
        reference_price=None,
        coverage_complete=False,
    )
    decision = decide_effective_top_up("TXN", desired_usd=6.0, effective=effective, current_price=50.0)
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.BROKER_HISTORY_INCOMPLETE
    assert rc.is_fail_closed(decision.reason)


def test_a_broker_fill_the_artifacts_missed_is_folded_into_the_prior():
    """The other half of the incident: a real same-day fill absent from the
    artifacts must still count as prior allocation, and be flagged as
    broker-only so the gap is visible."""
    missed_fill = {
        "id": "rh-1",
        "symbol": "TXN",
        "side": "buy",
        "state": "filled",
        "cumulative_quantity": "0.12",
        "average_price": "50.00",
        "created_at": "2026-07-28T16:30:00Z",
    }
    # Built through the real paging + normalization path, not a hand-made
    # SnapshotResult, so the test would also catch a normalizer that dropped the
    # fill before the reconciler ever saw it.
    snapshot = same_day_order_snapshot(
        lambda **kw: {"orders": [missed_fill]},
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
    )
    assert snapshot.complete is True
    effective = reconcile_effective_prior({}, snapshot)["TXN"]
    assert effective.broker_only is True
    assert effective.effective_prior_usd == pytest.approx(6.0)

    decision = decide_effective_top_up("TXN", desired_usd=6.0, effective=effective, current_price=49.0)
    assert decision.incremental_usd == pytest.approx(0.0)  # already met, not re-bought


def test_an_order_both_sources_recorded_is_not_counted_twice():
    """The opposite failure to the incident, and just as expensive in the other
    direction: double-counting one order inflates the prior, so a legitimate
    top-up is suppressed and the ladder silently stops buying real dips."""
    same_order = {
        "id": "rh-1",
        "symbol": "TXN",
        "side": "buy",
        "state": "filled",
        "cumulative_quantity": "0.12",
        "average_price": "50.00",
    }
    snapshot = same_day_order_snapshot(
        lambda **kw: {"orders": [same_order]},
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
    )
    artifact = {
        "TXN": PriorAllocation(
            ticker="TXN", prior_usd=6.0, reference_price=50.0, order_ids=["rh-1"]
        )
    }
    effective = reconcile_effective_prior(artifact, snapshot)["TXN"]
    assert effective.effective_prior_usd == pytest.approx(6.0)  # not $12
    assert effective.broker_only is False


def test_a_live_but_unconfirmed_order_poisons_the_reference_price():
    """"Any live-but-unconfirmed order poisons the reference to unknown." A
    price that is not a real fill must never become the bar a later top-up is
    compared against -- the strict-below invariant would be measuring against
    a number the account never paid."""
    unconfirmed = {
        "id": "rh-2",
        "symbol": "TXN",
        "side": "buy",
        "state": "queued",
        # A sizable working order, so the snapshot stays COMPLETE and this test
        # isolates reference-price poisoning from the separate unsized-exposure
        # fail-closed path.
        "total_notional": "6.00",
    }
    snapshot = same_day_order_snapshot(
        lambda **kw: {"orders": [unconfirmed]},
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
    )
    artifact = {
        "TXN": PriorAllocation(
            ticker="TXN", prior_usd=6.0, reference_price=50.0, order_ids=["rh-other"]
        )
    }
    effective = reconcile_effective_prior(artifact, snapshot)["TXN"]
    assert effective.reference_price is None

    decision = decide_effective_top_up(
        "TXN", desired_usd=12.0, effective=effective, current_price=49.0
    )
    assert decision.incremental_usd == pytest.approx(0.0)
    assert decision.reason == rc.MISSING_PRIOR_PRICE


def test_a_live_order_that_cannot_be_sized_fails_the_whole_snapshot_closed():
    """Exposure known to exist but impossible to value must not be read as $0.

    Treating it as zero is exactly the shape of the 2026-07-23 bug: the
    reconciler would report ``effective_prior=0`` and authorize a full first
    allocation on top of an order that is still working on the book.
    """
    unsized = {"id": "rh-3", "symbol": "TXN", "side": "buy", "state": "queued"}
    snapshot = same_day_order_snapshot(
        lambda **kw: {"orders": [unsized]},
        account_number=ACCOUNT,
        created_at_gte="2026-07-28T00:00:00Z",
    )
    assert snapshot.unknown_exposure_order_ids == ["rh-3"]
    assert snapshot.complete is False

    effective = reconcile_effective_prior({}, snapshot)["TXN"]
    decision = decide_effective_top_up(
        "TXN", desired_usd=6.0, effective=effective, current_price=49.0
    )
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.BROKER_HISTORY_INCOMPLETE


def test_a_window_that_could_not_be_reconciled_can_never_reach_submission():
    """The run-state expression of the same rule: ``partially_reconciled`` is
    sticky for the window's whole life, so no later transition can walk it
    back into a submission."""
    machine = RunStateMachine()
    machine.advance(PREFLIGHT).advance(RECONCILED).advance(PARTIALLY_RECONCILED).advance(DECIDED)
    assert machine.can_advance(SUBMITTED) is False
    with pytest.raises(IllegalTransitionError):
        machine.advance(SUBMITTED)
    # It may still fail closed to a clean no-buy publish.
    machine.advance(VERIFIED).advance(PUBLISHED)
    assert machine.state == PUBLISHED


def test_a_window_cannot_publish_before_it_reconciles():
    machine = RunStateMachine()
    with pytest.raises(IllegalTransitionError):
        machine.advance(PUBLISHED)


def test_a_terminal_window_cannot_be_resumed():
    machine = RunStateMachine()
    machine.advance(PREFLIGHT).advance(RECONCILED).advance(DECIDED).advance(VERIFIED).advance(PUBLISHED)
    with pytest.raises(IllegalTransitionError):
        machine.advance(SUBMITTED)


# ===========================================================================
# 8. Idempotency -- a run and its recovery must not double-buy
# ===========================================================================


def test_the_same_intended_order_always_produces_the_same_key():
    """"Two runs (or a run and its recovery) computing the same intended order
    produce the same key, so the broker's dedup rejects the duplicate.\""""
    fields = dict(
        trade_date="2026-07-28",
        window="1230et",
        symbol="TXN",
        strategy_version="v2-preauthorized",
        tier="elevated",
    )
    first = build_idempotency_key(**fields)
    recovery = build_idempotency_key(**fields)
    assert first == recovery
    assert deterministic_ref_id(first) == deterministic_ref_id(recovery)


def test_a_first_allocation_and_a_top_up_are_keyed_distinctly():
    """Same symbol, same day, same window -- but a top-up is a different
    intended order and must not be deduped against the first allocation."""
    common = dict(
        trade_date="2026-07-28",
        window="1230et",
        symbol="TXN",
        strategy_version="v2-preauthorized",
        tier="elevated",
    )
    assert build_idempotency_key(**common, kind="first") != build_idempotency_key(
        **common, kind="topup"
    )


def test_the_placement_ref_id_is_derived_from_the_slot_and_day_not_the_run_id():
    """Two concurrent invocations of the same slot pass different caller-
    generated ``run_id``s. If ``ref_id`` were keyed off ``run_id``, each would
    derive a different idempotency key for the same ticker and the broker
    could accept both. Deriving from slot + trading day makes them identical,
    so broker-side dedup collapses them."""
    ticker = _tickers(1)[0]
    cands = [dca_runner.Candidate(ticker, 2.0)]
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)

    ref_ids = []
    for run_id in (f"runner-a-{TRADING_DAY.isoformat()}", f"runner-b-{TRADING_DAY.isoformat()}"):
        calls, place_fn = _recorder()
        _execute(plan, _live_broker(place_fn), candidates=cands, run_id=run_id)
        ref_ids.append(calls[0]["ref_id"])

    assert ref_ids[0] == ref_ids[1]
    assert ref_ids[0] == f"dca-runner-0935-{TRADING_DAY.isoformat()}-{ticker}"


def test_two_slots_on_the_same_ticker_get_different_ref_ids():
    """The flip side: the same ticker in a different slot is a genuinely
    different intended order and must not be deduped away by the broker."""
    ticker = _tickers(1)[0]
    cands = [dca_runner.Candidate(ticker, 2.0)]
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)

    ref_ids = []
    for slot_id, now in (("0935", NOW_0935), ("1030", NOW_1030)):
        calls, place_fn = _recorder()
        _execute(
            plan,
            _live_broker(place_fn),
            candidates=cands,
            slot_id=slot_id,
            now=now,
            now_fn=lambda now=now: now,
            orders_fetched_at=now,
        )
        ref_ids.append(calls[0]["ref_id"])

    assert ref_ids[0] != ref_ids[1]


# ===========================================================================
# 9. Scheduler leases -- a scheduled run and its recovery never overlap
# ===========================================================================


def test_a_second_runner_cannot_hold_the_same_window(tmp_path):
    key = window_key(TRADING_DAY.isoformat(), "claude_roster", "09:35 ET")
    scheduled = SchedulerLease(tmp_path / "leases", key)
    recovery = SchedulerLease(tmp_path / "leases", key)

    assert scheduled.acquire("scheduled-runner", now=1000.0).acquired is True
    blocked = recovery.acquire("recovery-runner", now=1001.0)
    assert blocked.acquired is False
    assert blocked.current_holder is not None
    assert blocked.current_holder.holder_id == "scheduled-runner"


def test_a_crashed_holders_lease_is_only_reclaimable_after_its_ttl(tmp_path):
    key = window_key(TRADING_DAY.isoformat(), "claude_roster", "09:35 ET")
    lease = SchedulerLease(tmp_path / "leases", key)
    other = SchedulerLease(tmp_path / "leases", key)

    lease.acquire("crashed-runner", ttl_seconds=300, now=1000.0)
    assert other.acquire("recovery-runner", now=1200.0).acquired is False
    assert other.acquire("recovery-runner", now=1400.0).acquired is True


@pytest.mark.parametrize("bad_ttl", [0, -1, 0.5])
def test_a_ttl_that_would_make_a_lease_instantly_stale_is_refused(tmp_path, bad_ttl):
    """A non-positive or fractional TTL lets a second runner reclaim a lease
    the first still believes it holds -- defeating same-window exclusion."""
    lease = SchedulerLease(tmp_path / "leases", "w")
    with pytest.raises(ValueError):
        lease.acquire("runner", ttl_seconds=bad_ttl, now=1000.0)


# ===========================================================================
# 10. The dry-run broker is inert
# ===========================================================================
#
# DryRunBroker is what every demo/CI path runs against. If it could ever reach
# a real venue, "dry run" would be a lie in exactly the situation nobody
# double-checks.


def test_the_dry_run_broker_returns_a_clearly_synthetic_order_id():
    broker = DryRunBroker(account=ACCOUNT)
    order_id = broker.place_market_buy("AAPL", 2.0, ref_id="dca-runner-0935-2026-07-28-AAPL")
    assert order_id.startswith("DRY-RUN-")
    assert "AAPL" in order_id


def test_the_dry_run_broker_reports_no_real_placement_responses():
    """Nothing it does may be persisted as evidence of a real broker
    interaction."""
    broker = DryRunBroker(account=ACCOUNT)
    broker.place_market_buy("AAPL", 2.0, ref_id="ref")
    assert broker.get_raw_placement_responses() == []


def test_the_dry_run_broker_needs_no_network_for_any_of_its_surface():
    """Exercised under the module-wide network guard: every method a live run
    would call is answerable locally."""
    broker = DryRunBroker(account=ACCOUNT, reference_prices={"AAPL": 100.0})
    assert broker.account_number() == ACCOUNT
    assert broker.available_buying_power() > 0
    assert broker.is_tradable_for_fractional_buy("AAPL") is True
    assert broker.review_market_buy("AAPL", 2.0)[0] is True
    assert broker.same_day_strategy_order_exists("AAPL", TRADING_DAY) is False
    order_id = broker.place_market_buy("AAPL", 2.0, ref_id="ref")
    assert broker.get_order_fill(order_id) is not None


# ===========================================================================
# 11. Secrets never leak into failure evidence
# ===========================================================================
#
# failure_reason is explicitly sent to alerts and written to durable run
# artifacts, so whatever a broker wrapper puts in an error string ends up
# stored. Broker error text is arbitrary external data.


def test_a_token_shaped_secret_in_a_broker_error_is_redacted():
    token = "sk-live-AbCdEf0123456789XyZwVu"

    def place_fn(**kwargs):
        return {"success": False, "error": f"auth rejected: {token}"}

    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    result = _execute(plan, _live_broker(place_fn), candidates=cands)

    # Isolated, not halting (2026-08-24) -- but the per-ticker reason reaches
    # the same alerts and durable evidence, so it is scrubbed identically.
    assert [f.ticker for f in result.failures] == [plan.to_place[0].ticker]
    assert result.halted_on is None
    assert token not in result.failures[0].reason
    assert "[REDACTED-TOKEN]" in result.failures[0].reason


def test_the_account_number_is_redacted_from_failure_evidence():
    def place_fn(**kwargs):
        return {"success": False, "error": f"account {ACCOUNT} not permitted"}

    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    result = _execute(plan, _live_broker(place_fn), candidates=cands)

    assert result.failures
    assert ACCOUNT not in result.failures[0].reason
    assert "[REDACTED-ACCOUNT]" in result.failures[0].reason


def test_a_structured_broker_error_object_is_never_serialized_verbatim():
    """A nested object can carry far more than a short human-readable reason
    (headers, tokens, the full request), so it is reported by type only."""
    reason = dca_runner._redacted_envelope_error(
        {"error": {"token": "sk-live-AbCdEf0123456789XyZwVu", "headers": {}}}
    )
    assert reason is not None
    assert "sk-live" not in reason
    assert "non-string error field" in reason


# ===========================================================================
# 12. A failed order costs only its own ticker -- no retry, no collateral damage
# ===========================================================================


def test_a_rejection_is_isolated_and_the_rest_of_the_batch_still_places():
    """The 2026-08-24 policy change. This contract used to read "a broker
    refusing one order is evidence the run's assumptions are stale, not an
    invitation to keep going", and halted the batch -- which is what produced
    "GS, EVR: never submitted -- batch halted after RVII rejection" and "34 of
    37 planned buys placed and filled, 3 tickers not bought".

    Every order still in the plan was independently validated against the
    caps, the whitelist, and buying power before the first placement, so one
    order's rejection tells the next one nothing. The refused ticker is
    recorded and never retried; the rest are placed. The already-placed orders
    are still reported either way: they are real, committed spend."""
    cands = _cands(3)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    attempted = []

    def place_fn(**kwargs):
        attempted.append(kwargs["symbol"])
        if len(attempted) == 2:
            return {"success": False, "error": "insufficient funds"}
        return {"id": f"ORD-{len(attempted)}", "state": "queued"}

    result = _execute(plan, _live_broker(place_fn), candidates=cands)

    placed = [o.ticker for o in plan.to_place]
    assert attempted == placed  # the third is no longer collateral damage
    assert result.submitted == [placed[0], placed[2]]
    assert [f.ticker for f in result.failures] == [placed[1]]
    assert result.failures[0].reason
    assert result.halted_on is None
    assert attempted.count(placed[1]) == 1  # no retry


def test_the_window_closing_mid_batch_halts_the_remainder():
    """"No trading outside regular market hours" is rechecked before EVERY
    placement, not once per batch: a slow multi-order batch could otherwise
    straddle the close. (The first reading is the batch's one-time history
    freshness check; each placement then samples the clock again.)"""
    cands = _cands(3)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    calls, place_fn = _recorder()
    clock = iter([NOW_0935, NOW_0935, datetime(2026, 7, 28, 16, 5, tzinfo=ET)])

    result = _execute(
        plan,
        _live_broker(place_fn),
        candidates=cands,
        now_fn=lambda: next(clock, datetime(2026, 7, 28, 16, 5, tzinfo=ET)),
    )

    assert len(calls) == 1
    assert result.submitted == [plan.to_place[0].ticker]
    assert result.halted_on == plan.to_place[1].ticker


def test_a_stale_order_history_is_refused_rather_than_trusted():
    """Placement-time caps are only as good as the history they recompute
    from. A fetch older than ``MAX_ORDERS_FETCH_AGE_SECONDS`` may predate
    another slot's spend, so it is refused rather than silently trusted."""
    calls, place_fn = _recorder()
    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), NORMAL_TIER)
    stale = NOW_0935.timestamp() - dca_runner.MAX_ORDERS_FETCH_AGE_SECONDS - 60
    with pytest.raises(ValueError):
        _execute(
            plan,
            _live_broker(place_fn),
            candidates=cands,
            orders_fetched_at=datetime.fromtimestamp(stale, tz=ET),
        )
    assert calls == []


# ===========================================================================
# 13. Gates that must end a run before any of the above matters
# ===========================================================================


def test_a_weekend_fire_ends_silently_without_planning_anything():
    """Not an alert: the schedule firing on a non-trading day is expected
    noise, and an alert here would train the operator to ignore alerts."""
    result = _assess(now=datetime(2026, 8, 1, 9, 43, tzinfo=ET))
    assert result["verdict"] == "END_SILENTLY"


def test_a_market_holiday_ends_silently():
    result = _assess(now=datetime(2026, 9, 7, 9, 43, tzinfo=ET))  # Labor Day
    assert result["verdict"] == "END_SILENTLY"


def test_a_fire_past_the_approved_test_window_aborts():
    """config/trading_window.json stops before the 2026-11-01 EDT->EST change,
    because the live tasks' cron schedules are fixed-UTC. Trading past it
    would fire at the wrong ET time."""
    result = _assess(now=datetime(2026, 11, 2, 9, 43, tzinfo=ET))
    assert result["verdict"] == "ABORT"


def test_an_off_schedule_fire_aborts_rather_than_running_late():
    result = _assess(now=datetime(2026, 7, 28, 11, 15, tzinfo=ET))
    assert result["verdict"] == "ABORT"
    assert "window" in result["reason"].lower()


def test_another_executor_in_this_slots_window_aborts_the_run():
    """The sole-executor interlock. An order already in this slot's window is
    evidence something else is trading the account right now; two executors
    sharing one cap is how a cap gets doubled."""
    intruder = [_broker_order(_tickers(1)[0], 2.0, hour_et=9, minute_et=45)]
    result = _assess(orders=intruder)
    assert result["verdict"] == "ABORT"
    assert "executor" in result["reason"].lower()


# ===========================================================================
# 14. Config <-> code coherence for the numbers live money rests on
# ===========================================================================
#
# test_config_invariants.py already pins the budget TIERS against config. What
# is pinned here is the set of scalars this suite's other sections rely on, so
# a config edit can never make those sections vacuously pass.


def test_the_weekly_rollover_reserve_cap_matches_config():
    budgets = dca_runner._read_config("budgets.json")
    assert dca_runner.WEEKLY_ROLLOVER_RESERVE_CAP_USD == budgets["weekly_rollover_reserve_cap_usd"]


def test_the_stress_tier_is_the_only_one_gated_on_the_rollover_reserve():
    """Stress capacity is usable only to the extent unused reserve exists. If
    another tier ever acquired that flag, its cap would silently become
    conditional too."""
    gated = [t.name for t in dca_runner.BUDGET_TIERS if t.uses_rollover_reserve]
    assert gated == ["stress"]


def test_a_stress_tier_run_cannot_exceed_the_reserve_it_actually_earned():
    """The weekly ceiling for Stress is derived from the verified reserve, not
    from the tier's nominal cap: an unearned reserve cannot raise it."""
    stress = next(t for t in dca_runner.BUDGET_TIERS if t.name == "stress")
    calls, place_fn = _recorder()
    cands = _cands(1)
    plan = dca_runner.plan_orders(cands, dca_runner.budget_snapshot([], TRADING_DAY), stress)
    with pytest.raises(ValueError):
        _execute(
            plan,
            _live_broker(place_fn),
            candidates=cands,
            tier=stress,
            weekly_rollover_reserve_usd=900.0,
            weekly_rollover_reserve_week_key=iso_week_key(TRADING_DAY),
            finalized_budget_days=set(),
            submitted_notional_by_week={},
        )
    assert calls == []


def test_the_base_amount_never_falls_back_to_a_broker_default():
    """config/budgets.json names $50 as a forbidden fallback: a broker/account
    default silently replacing the $2 base would multiply every order 25x."""
    policy = dca_runner._read_config("budgets.json")["base_amount_policy"]
    forbidden = policy["forbidden_fallback_values_usd"]
    assert 50.0 in forbidden, "the $50 broker/account default must stay named as forbidden"
    assert dca_runner._SIZING_LADDER["base_amount_usd"] == policy["base_amount_usd"]
    assert dca_runner._SIZING_LADDER["base_amount_usd"] not in forbidden
    # The whole ladder, not just the base: a forbidden value reachable via any
    # multiplier would be just as wrong as one reached directly.
    for multiplier in dca_runner._SIZING_LADDER["multipliers"]:
        assert dca_runner._SIZING_LADDER["base_amount_usd"] * multiplier not in forbidden


def test_no_planned_order_can_exceed_the_per_order_ceiling_at_any_tier():
    """Swept across every shipped tier and every ladder multiplier: the $10
    ceiling is absolute across all live streams, not a per-tier allowance."""
    base = dca_runner._SIZING_LADDER["base_amount_usd"]
    for tier in dca_runner.BUDGET_TIERS:
        for multiplier in dca_runner._SIZING_LADDER["multipliers"]:
            amount = base * multiplier
            cands = [dca_runner.Candidate(t, amount, float(multiplier)) for t in _tickers(2)]
            plan = dca_runner.plan_orders(
                cands, dca_runner.budget_snapshot([], TRADING_DAY), tier
            )
            for order in plan.to_place:
                assert order.amount_usd <= dca_runner.MAX_PER_ORDER_USD
                assert order.side == "buy"
