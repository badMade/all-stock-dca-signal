"""Self-tests for the midday graded-DCA executor (12:30 PM ET live execution).

Covers the midday stream identity (the shared $2-base 0x-to-5x ladder used by
all live streams: morning, midday, and afternoon), the run-state
path/classification, the live-mode confidence rule (inherited), the same-day
top-up guard against morning/manual prior allocations, and the guarantee that a
midday run stays isolated from the morning and afternoon artifact trees.

Runnable with ``pytest`` or ``python test_midday_dca.py``.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
from dca_paths import CRON_TRACKING_ROOT_ENV, stream_root
from midday_dca import (
    DEFAULT_MIDDAY_LIVE_STATE_ROOT,
    MIDDAY_LIVE_CRON_ID,
    MIDDAY_SIZING_TIERS,
    MiddayConfig,
    MiddayDCAExecutor,
    load_midday_config,
)
from morning_dca import (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    EXEC_AUTO_BUY_ELIGIBLE,
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MorningSignal,
)
from near_close_dca import (
    EASTERN,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_BASE,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    Action,
    Bar,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    graded_buy_multiplier,
)


def _now_midday():
    """Decision timestamp for the 12:30 PM ET live execution."""
    return datetime.combine(date(2026, 7, 8), time(12, 30), tzinfo=EASTERN)


class FakeLiveBroker:
    """Minimal successful broker for scheduled midday live tests."""

    def account_number(self):
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        # No duplicate: this fake broker never has prior orders on file, so
        # guarded top-ups always proceed as fresh placements.
        return None

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        return f"ORDER-{ticker}"


class FixedDropProvider:
    """Deterministic provider: each ticker drops a fixed fraction vs. prior close."""

    def __init__(self, source: SourceName, previous_close: float, drops: dict) -> None:
        self.source = source
        self._prev = previous_close
        self._drops = drops

    def get_previous_close_bar(self, ticker: str, as_of: datetime):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        bars = []
        cursor = start
        while cursor <= end:
            bars.append(Bar(timestamp=cursor, open=price, high=price, low=price, close=price, volume=100.0))
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_bid_ask(self, ticker, as_of):
        """A tight, well-inside-tolerance quote (0.1% spread).

        A scheduled live run fails a candidate closed when its spread cannot be
        proven, so a provider double that exposes no bid/ask would block every
        placement. Tests that specifically exercise an unavailable or wide spread
        override this.
        """
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        return (price * 0.9995, price * 1.0005)


class DecliningBarsProvider(FixedDropProvider):
    """Provider that generates strictly declining intraday bars.

    Bars start at the midpoint between previous close and the final dropped price,
    declining linearly to the final price. Satisfies the strict downward-trend
    requirement in `_trend_validated` (last close < first close).
    """

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        drop = self._drops.get(ticker, 0.0)
        final_price = self._prev * (1.0 - drop)
        start_price = self._prev * (1.0 - drop * 0.5) if drop > 0 else final_price
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        n = len(timestamps)
        # Interpolate so the bar at end-interval_minutes (the completed-bar anchor
        # returned by latest_bar_at_or_before) is exactly final_price.  The last
        # bar (at end) is also set to final_price.
        anchor_idx = n - 2 if n >= 2 else 0
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars


def _write_prior_morning_run(
    root: Path,
    *,
    ticker: str,
    planned_amount: float,
    fill_price=None,
    quantity=None,
    trading_day=date(2026, 7, 8),
    generated_at="2026-07-08T10:30:00-04:00",
    selected_base_amount_usd=None,
):
    """Write a synthetic morning/manual prior-allocation artifact under ``root``.

    When ``fill_price``/``quantity`` are given an ``orders.csv`` fill is written
    too, so ``load_prior_same_day_allocations`` resolves a known reference price;
    otherwise the reference price stays unknown (manual-review path).

    ``selected_base_amount_usd`` mirrors a real artifact's recorded
    first-allocation base; omitting it (the default) simulates a legacy
    artifact that predates the field, which now routes the top-up guard to
    missing_prior_base/manual review (Codex review: "Fail closed when the
    first allocation base is unknown") rather than silently recomputing from
    this run's own price.
    """
    run_dir = root / f"{trading_day.strftime('%Y%m%d')}_1030ET"
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate = {"ticker": ticker, "status": "filled", "planned_amount_usd": planned_amount}
    if selected_base_amount_usd is not None:
        candidate["selected_base_amount_usd"] = selected_base_amount_usd
    run_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": generated_at,
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "morning",
            "run_id": "manual_morning_live_test",
        },
        "buy_candidates": [candidate],
    }
    (run_dir / "run_state.json").write_text(json.dumps(run_state), encoding="utf-8")
    if fill_price is not None and quantity is not None:
        (run_dir / "orders.csv").write_text(
            "ticker,status,avg_fill_price,quantity_filled,order_id\n"
            f"{ticker},filled,{fill_price},{quantity},ord_test_1\n",
            encoding="utf-8",
        )
    return run_dir


# --- Ladder identity --------------------------------------------------------
def test_midday_ladder_matches_afternoon_and_morning():
    from morning_dca import MORNING_SIZING_TIERS

    ex = MiddayDCAExecutor(MiddayConfig())
    # As of the Jul 10 2026 update all live streams (morning, midday, afternoon)
    # share the SAME $2-base 0x-to-5x ladder.
    assert MIDDAY_SIZING_TIERS == StrategyConfig().sizing_tiers
    assert ex.multiplier_for_drop(0.08) == 5.0
    assert graded_buy_multiplier(0.08, StrategyConfig()) == 5.0
    assert MIDDAY_SIZING_TIERS == MORNING_SIZING_TIERS


def test_midday_default_prior_allocation_roots_include_market_open_and_intraday():
    # Codex review ("Feed market-open fills into later top-up guards"; "Include
    # the 11:30 intraday run in midday context"): the midday guard's own
    # hardcoded default prior_allocation_roots must scan the 9:35 market-open
    # and 11:30/13:30/14:30 intraday roots in addition to morning/afternoon,
    # so those earlier same-day fills offset the 12:30 desired amount instead
    # of being re-bought as first allocations.
    from morning_dca import (
        DEFAULT_AFTERNOON_TASK_ROOT,
        DEFAULT_INTRADAY_TASK_ROOT,
        DEFAULT_MARKET_OPEN_TASK_ROOT,
        DEFAULT_MORNING_LIVE_STATE_ROOT,
    )

    roots = MiddayConfig().prior_allocation_roots
    assert DEFAULT_MARKET_OPEN_TASK_ROOT in roots
    assert DEFAULT_MORNING_LIVE_STATE_ROOT in roots
    assert DEFAULT_INTRADAY_TASK_ROOT in roots
    assert DEFAULT_AFTERNOON_TASK_ROOT in roots


def test_midday_default_prior_allocation_roots_resolve_at_construction_time():
    """MiddayConfig.prior_allocation_roots used to default to a literal tuple
    binding DEFAULT_MARKET_OPEN_TASK_ROOT/etc at CLASS-DEFINITION (import)
    time. A $DCA_CRON_TRACKING_ROOT set afterward left every MiddayConfig()
    built without an explicit override -- including load_midday_config's
    config-missing fallback -- scanning the OLD tree, and
    _compute_top_up's exact-tuple staleness check then read that mismatch as
    "the caller explicitly configured a different root," skipping the
    same-day manual top-up merge and risking a duplicate allocation (Codex
    review, PR #219). Now a default_factory, so it resolves fresh on every
    construction instead."""
    probe = (
        "import os\n"
        "import midday_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "print(midday_dca.MiddayConfig().prior_allocation_roots)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/midday-prior-allocation-probe"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == (
        "('/srv/midday-prior-allocation-probe/market_open_dca', "
        "'/srv/midday-prior-allocation-probe/morning_dca', "
        "'/srv/midday-prior-allocation-probe/intraday_dca', "
        "'/srv/midday-prior-allocation-probe/ce16ba6b')"
    ), out.stdout


def test_midday_ladder_brackets():
    ex = MiddayDCAExecutor(MiddayConfig(base_buy_amount_usd=2.0))
    cases = {
        0.0005: 0.0,
        0.001: 1.0,
        0.019: 1.0,
        0.02: 1.5,
        0.039: 1.5,
        0.04: 2.0,
        0.05: 2.5,
        0.06: 3.0,
        0.065: 3.5,
        0.07: 4.0,
        0.075: 4.5,
        0.08: 5.0,
        0.20: 5.0,
    }
    for drop, expected in cases.items():
        assert ex.multiplier_for_drop(drop) == expected, (drop, expected)


def test_midday_augment_buy_candidate_row_persists_effective_base():
    # Codex review ("Persist the effective base in midday candidate rows"):
    # MiddayDCAExecutor overrides _augment_buy_candidate_row without calling
    # super() or assigning outcome.effective_base_amount_usd, unlike
    # MorningDCAExecutor's own override. An earlier same-day run bought using
    # the $1 base; this run's own (deeper) price would freshly select the $2
    # base, but the guard must persist the PRESERVED $1 base it actually used,
    # not the freshly recomputed $2 -- otherwise later artifact consumers
    # (and the graded-vs-fixed dashboard) reconstruct the wrong base.
    ex = MiddayDCAExecutor(MiddayConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=2.0,
        reference_price=98.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=94.0,
        previous_close=100.0,
        drop_pct=6.0,
        multiplier=4.0,
        hypothetical_amount_usd=8.0,  # recomputed (incorrectly) at this run's own $2 base: 2.0 * 4.0
        selected_base_amount_usd=2.0,
    )
    outcome = ex._evaluate_top_up(signal, prior)
    assert outcome.effective_base_amount_usd == 1.0

    row = signal.to_row()
    ex._augment_buy_candidate_row(row, signal, {"AAPL": outcome})
    assert row["selected_base_amount_usd"] == 1.0


# --- Run-state path + classification ----------------------------------------
def test_midday_run_state_path_and_classification(tmp_path):
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.parent.name == "20260708_1230ET"
    assert run_state_path.exists()

    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "midday_dca_live"
    assert rc["stream"] == "midday"
    assert rc["trades"] is True
    assert rc["scheduled_for_et"] == "12:30"
    assert rc["scheduled_window_start_et"] == "12:25"
    assert rc["scheduled_window_end_et"] == "12:35"
    assert rc["state_schema_version"] == "midday-live-v1"
    assert rc["run_id"] == "midday_live_20260708_1230ET"
    assert rc["cron_id"] == MIDDAY_LIVE_CRON_ID
    assert rc["supersedes_cron_id"] is None
    assert rc["valid_afternoon_evidence"] is False
    # Codex review (PR #78): MiddayDCAExecutor inherits run_classification()
    # from MorningDCAExecutor unchanged; valid_morning_evidence must be scoped
    # to self.stream == "morning", not hardcoded True, or a midday artifact
    # would misreport itself as valid MORNING evidence.
    assert rc["valid_morning_evidence"] is False

    art = res["artifacts"]
    assert art["mode"] == "live"
    assert art["trades"] is True
    assert art["ladder"]["name"] == "midday_0x_to_5x"
    assert art["budget"]["ledger_namespace"] == "midday"
    assert "midday_daily_cap_usd" in art["budget"]
    # Scheduled pointer uses the midday-specific filename.
    assert "latest_midday_run" in res["paths"]
    assert (Path(tmp_path) / "latest_midday_run.json").exists()


def test_midday_refreshes_only_its_own_pointer_with_guard_fields(tmp_path):
    # Requirement 7: midday refreshes ONLY latest_midday_run.json and must never
    # touch the morning-only latest_morning_live.json pointer.
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    assert (Path(tmp_path) / "latest_midday_run.json").exists()
    assert not (Path(tmp_path) / "latest_morning_live.json").exists()
    assert "latest_morning_live" not in res["paths"]

    pdata = json.loads((Path(tmp_path) / "latest_midday_run.json").read_bytes())
    assert pdata["stream"] == "midday"
    assert pdata["trading_day"] == "2026-07-08"
    assert pdata["pointer_status"] == "valid"
    assert pdata["same_day_required"] is True
    assert pdata["valid_scheduled_evidence"] is True
    assert pdata["updated_at_utc"].endswith("Z")


# --- Cross-stream shared budget pooling (added 2026-08-02) ------------------
def test_midday_cap_ledger_seeded_from_cross_stream_morning_fill(tmp_path):
    """The midday budget ledger's cap must be genuinely pooled with other
    streams, not an isolated $90/day half-share.

    Before the 2026-08-02 pooling fix, MiddayConfig.cap_seed_roots had no
    default and midday_dca.json shipped none either, so this cap_seed_roots
    scan (added to _compute_top_up alongside morning's own) never ran for
    midday at all -- a $170 prior MORNING fill would have been invisible to
    midday's cap ledger, which stayed isolated at its own (then-$90) cap.
    Now the cap is the shared $180 Normal-tier ceiling and cap_seed_roots
    covers the morning root, so a $170 prior morning fill leaves only $10 of
    headroom for the midday run.
    """
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    morning_root = tmp_path / "morning_prior"
    # $170 prior MORNING fill on a different ticker, well below TOPUP's own
    # decision price so the per-ticker top-up guard isn't what blocks TOPUP --
    # only the shared daily cap should be the binding constraint here.
    _write_prior_morning_run(
        morning_root, ticker="PRIOR_TICKER", planned_amount=170.0, generated_at="2026-07-08T10:30:00-04:00"
    )
    cfg = MiddayConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        cap_seed_roots=(str(morning_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    assert cfg.morning_daily_cap_usd == 180.0  # the shared Normal-tier ceiling, not an isolated 90.0
    ex = MiddayDCAExecutor(cfg, mode="live")
    broker = _Broker()
    # 3% drop: TOPUP desired = 1.5x * $3 = $4.50; remaining shared cap = $180 - $170 = $10.
    # $4.50 fits under $10, proving the fill was seen (a stale isolated-$90 cap
    # with no seeding at all would ALSO have allowed this by coincidence, so the
    # cap-exceeded case below is the one that actually proves seeding occurred).
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_midday(), data, broker=broker)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "submitted"
    assert broker.placed.get("TOPUP") == pytest.approx(4.50)

    # Now push the prior morning fill up to $177: remaining shared cap = $3,
    # which TOPUP's $4.50 desired amount exceeds.
    morning_root2 = tmp_path / "morning_prior_2"
    _write_prior_morning_run(
        morning_root2, ticker="PRIOR_TICKER", planned_amount=177.0, generated_at="2026-07-08T10:30:00-04:00"
    )
    cfg2 = MiddayConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live2"),
        cap_seed_roots=(str(morning_root2),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex2 = MiddayDCAExecutor(cfg2, mode="live")
    broker2 = _Broker()
    res2 = ex2.run(_now_midday(), data, broker=broker2)
    broker_outcomes2 = res2["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes2.get("TOPUP", {}).get("status") == "cap_exceeded", (
        "TOPUP ($4.50 desired) should be cap-gated once a $177 prior MORNING fill, "
        "seeded cross-stream via cap_seed_roots, leaves only $3 of the shared $180 cap"
    )
    assert "TOPUP" not in broker2.placed


def test_midday_manual_run_id_and_no_pointer(tmp_path):
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, manual=True)
    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["run_id"].startswith("manual_midday_live_")
    assert "latest_midday_run" not in res["paths"]
    assert not (Path(tmp_path) / "latest_midday_run.json").exists()


# --- Live confidence rule (inherited) ---------------------------------------
def test_midday_confidence_rule_auto_buys_with_validated_trend():
    ex = MiddayDCAExecutor(MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0))
    # Use declining bars so the strict downward-trend check (last close < first close) passes.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is True
    assert sig.execution_status == EXEC_AUTO_BUY_ELIGIBLE
    # 0x-to-5x ladder: 10% drop -> 5x -> $10.
    assert sig.hypothetical_amount_usd == 10.0


class FewBarsProvider(FixedDropProvider):
    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        anchor = end - timedelta(minutes=interval_minutes)
        return [
            Bar(timestamp=anchor - timedelta(minutes=interval_minutes),
                open=price, high=price, low=price, close=price, volume=100.0),
            Bar(timestamp=anchor, open=price, high=price, low=price, close=price, volume=100.0),
        ]


def test_midday_confidence_rule_withholds_when_trend_unvalidated(tmp_path):
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0,
                       morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FewBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is False
    assert sig.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE

    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    art = res["artifacts"]
    assert art["counts"]["manual_review_candidates"] == 1
    assert art["manual_review_tickers"] == ["BIG"]


# --- Same-day top-up guard --------------------------------------------------
def test_top_up_no_prior_buys_full_amount(tmp_path):
    prior_root = tmp_path / "morning"
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN
    assert row["incremental_order_amount"] == 10.0
    # Ledger recorded the full (un-offset) amount.
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 10.0


def test_top_up_incremental_when_price_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning bought ~$2 at a fill price of 95.0.
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # Midday price 90.0 is strictly below the 95.0 morning reference.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert row["prior_same_day_amount"] == 2.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["midday_decision_price"] == 90.0
    # Desired $10 minus prior $2 = $8 incremental.
    assert row["incremental_order_amount"] == 8.0
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 8.0


def test_top_up_loads_manual_csv_only_context(tmp_path):
    # Codex review r3626294840 ("Wire manual CSV context into the midday guard"):
    # a manual run whose fills live ONLY in a sibling orders CSV -- no
    # buy_candidates, no recorded artifacts.orders_csv_path (the Jul 21 manual
    # midday shape) -- is a shape near_close_dca.load_prior_same_day_allocations_
    # multi alone cannot read (no buy_candidates to read, no artifacts block to
    # fall back to). Only scripts.run_classification.load_same_day_top_up_context,
    # wired into the shared _compute_top_up, can see it.
    #
    # A bare manual CSV row (the ONLY shape this loader exists for) never
    # carries a selected_base_amount_usd column, so the guard now fails
    # closed on the unresolved base (Codex review: "Fail closed when the
    # first allocation base is unknown") instead of silently recomputing the
    # full-day target from this run's own price -- it still surfaces the
    # loaded prior amount/reference for the manual reviewer, just with a $0
    # order and manual_review flagged rather than a live incremental buy.
    now = _now_midday()
    trading_day_iso = now.astimezone(EASTERN).date().isoformat()
    manual_dir = tmp_path / "midday_dca" / "manual_runs" / "20260708_1145ET"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(json.dumps({
        "trading_day": trading_day_iso,
        "generated_at": f"{trading_day_iso}T11:45:00-04:00",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "run_id": "manual_midday_20260708_1145ET",
            "trading_day": trading_day_iso,
            "same_day_top_up_context_only": True,
            "valid_scheduled_evidence": False,
            "valid_afternoon_scheduled_evidence": False,
        },
    }))
    # avg_fill_price is the codebase-standard fill-price column (near_close_dca.
    # _orders_csv_fills, _CSV_FIELDS) -- also exercises the avg_fill_price alias.
    (manual_dir / "submitted_orders.csv").write_text(
        "symbol,amount,order_id,status,avg_fill_price,quantity\n"
        "BIG,2.0,MANUAL-BIG-1,filled,95.0,0.021\n"
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
    )
    ex = MiddayDCAExecutor(cfg)
    # Midday price 90.0 is strictly below the 95.0 manual reference.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(now, data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_MISSING_PRIOR_BASE
    assert row["prior_same_day_amount"] == 2.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_manual_review"] is True


def test_top_up_manual_csv_context_respects_explicit_prior_allocation_roots(tmp_path):
    # Codex review r3626569750 ("Honor explicit prior-allocation roots in
    # shared executors"): when a caller configures prior_allocation_roots to a
    # specific, isolated tree, the manual CSV-only context merge added in
    # test_top_up_loads_manual_csv_only_context above must not ALSO scan
    # unrelated sibling cron-tracking trees relative to state_root. Seed a
    # CSV-only manual fill under a default sibling location (final_manual_review)
    # that would leak in if isolation weren't honored.
    leak_dir = tmp_path / "final_manual_review" / "20260708_1145ET"
    leak_dir.mkdir(parents=True)
    now = _now_midday()
    trading_day_iso = now.astimezone(EASTERN).date().isoformat()
    (leak_dir / "run_state.json").write_text(json.dumps({
        "trading_day": trading_day_iso,
        "generated_at": f"{trading_day_iso}T11:45:00-04:00",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "run_id": "leak-run",
            "trading_day": trading_day_iso,
            "same_day_top_up_context_only": True,
            "valid_scheduled_evidence": False,
            "valid_afternoon_scheduled_evidence": False,
        },
    }))
    (leak_dir / "submitted_orders.csv").write_text(
        "symbol,amount,order_id,status,avg_fill_price,quantity\n"
        "BIG,2.0,LEAK-1,filled,90.0,0.02\n"
    )

    explicit_root = tmp_path / "custom_prior_root"
    explicit_root.mkdir(parents=True)
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(explicit_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(now, data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    # Isolation honored: the leaked fill under final_manual_review must not
    # offset the guard just because it happens to sit next to state_root.
    assert row["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN


class _WideSpreadProvider(DecliningBarsProvider):
    """A provider exposing a live bid/ask quote with an excessive spread."""

    def __init__(self, *args, spread_pct=5.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._spread_pct = spread_pct

    def get_bid_ask(self, ticker, as_of):
        midpoint = 100.0
        half_spread = midpoint * (self._spread_pct / 100.0) / 2.0
        return (midpoint - half_spread, midpoint + half_spread)


def test_midday_spread_breaker_blocks_wide_spread(tmp_path):
    # Codex review #12 on PR #22: the spread circuit breaker added to the
    # afternoon stream (PR #37) and the morning stream must also cover
    # midday, since MiddayDCAExecutor shares _place_broker_orders with
    # MorningDCAExecutor.
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        max_bid_ask_spread_pct=1.0,
    )
    ex = MiddayDCAExecutor(cfg)
    data = _WideSpreadProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10}, spread_pct=5.0)
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["status"] == "blocked"
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert "spread" in outcome["skip_reason"]


class _TopUpDuplicateBroker:
    def __init__(self, duplicate: bool) -> None:
        self.duplicate = duplicate
        self.broad_duplicate_calls = 0
        self.same_stream_calls = 0
        self.placed = []

    def account_number(self):
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        self.broad_duplicate_calls += 1
        return True

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        self.same_stream_calls += 1
        assert ref_id_prefixes[0] == f"midday-dca-topup-{trading_day.isoformat()}-{ticker}"
        if self.duplicate:
            return {
                "id": f"EXISTING-ORDER-{ticker}",
                "state": "confirmed",
                "dollar_amount": "12.00",
                "ref_id": ref_id_prefixes[0],
            }
        return None

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed.append((ticker, notional_usd, ref_id))
        return f"ORDER-{ticker}"


def test_guarded_midday_top_up_checks_same_stream_duplicate_before_placing(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=True)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    assert broker.broad_duplicate_calls == 0
    assert broker.same_stream_calls == 1
    assert broker.placed == []
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert outcome["status"] == "submitted"
    assert outcome["skip_reason"] == "same-stream broker order exists"
    assert outcome["order_id"] == "EXISTING-ORDER-BIG"
    assert outcome["submitted_amount_usd"] == 12.0
    assert res["artifacts"]["buy_candidates"][0]["status"] == "submitted"


def test_manual_retry_of_guarded_top_up_duplicate_is_skipped_not_submitted(tmp_path):
    # Codex review on PR #22: a manual/recovery rerun always writes to its own
    # fresh, uuid-suffixed artifact directory (see run_id()) rather than
    # overwriting the original scheduled run's run_state.json. If a same-stream
    # duplicate found on a MANUAL retry were still recorded as a fresh
    # "submitted" candidate (the way a same-path SCHEDULED retry legitimately
    # is), load_prior_same_day_allocations() would find both files and
    # double-count the same broker order as two separate same-day allocations.
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=True)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker, manual=True)

    assert broker.same_stream_calls == 1
    assert broker.placed == []
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert outcome["status"] == "skipped"
    assert "same-stream broker order exists" in outcome["skip_reason"]
    assert "submitted_amount_usd" not in outcome
    assert res["artifacts"]["buy_candidates"][0]["status"] == "skipped"


class _TopUpBrokerWithoutSameStreamCheck(_TopUpDuplicateBroker):
    same_day_strategy_order_exists_with_ref_id_prefix = None


def test_guarded_midday_top_up_blocks_when_same_stream_checker_unavailable(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpBrokerWithoutSameStreamCheck(duplicate=False)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert broker.placed == []
    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"].startswith("duplicate-check failed: broker does not support same-stream")


def test_guarded_midday_top_up_ignores_cross_stream_duplicate_for_incremental_order(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=False)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    assert broker.broad_duplicate_calls == 0
    assert broker.same_stream_calls == 1
    assert broker.placed == [("BIG", 8.0, "midday-dca-topup-2026-07-08-BIG")]
    assert res["artifacts"]["broker_outcomes"]["BIG"]["status"] == "submitted"


def test_top_up_zero_when_price_not_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning fill price 50.0 (below the midday price of 90.0).
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=50.0, quantity=round(2.0 / 50.0, 6), selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # DecliningBarsProvider (not the flat FixedDropProvider): the intraday trend
    # gate added on base rejects flat-or-rising sessions, and this test needs a
    # trend-validated candidate so it lands in buy_candidates rather than
    # manual_review_candidates. broker=FakeLiveBroker() satisfies the newer
    # "missing broker adapter" gate for scheduled live runs.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_PRICE_NOT_BELOW
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_skipped"] is True
    assert res["artifacts"]["top_up_offset_tickers"] == ["BIG"]
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 0.0


def test_top_up_manual_review_when_prior_price_unknown(tmp_path):
    prior_root = tmp_path / "morning"
    # Prior amount known but no orders.csv -> unknown reference price.
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0, selected_base_amount_usd=2.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # Same trend-validated-provider + broker rationale as the test above.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_MISSING_PRIOR_PRICE
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_manual_review"] is True
    assert res["artifacts"]["top_up_manual_review_tickers"] == ["BIG"]


def test_augment_artifacts_appends_top_up_manual_review_candidate_row():
    """_augment_artifacts must promote the full buy_candidates row to manual_review_candidates."""
    from morning_dca import TopUpOutcome
    cfg = MiddayConfig(tickers=("BIG",))
    ex = MiddayDCAExecutor(cfg)
    outcome = TopUpOutcome(
        ticker="BIG",
        desired_amount=6.0,
        decision_price=99.0,
        prior_same_day_amount=3.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id="sched_am",
        prior_same_day_source="morning",
        incremental_order_amount=0.0,
        top_up_guard_result="missing_prior_price",
        top_up_skipped=True,
        top_up_manual_review=True,
    )
    candidate_row = {
        "ticker": "BIG",
        "prior_same_day_amount": 3.0,
        "prior_same_day_reference_price": None,
        "top_up_guard_result": "missing_prior_price",
        "incremental_order_amount": 0.0,
        "execution_status": EXEC_MANUAL_REVIEW_CANDIDATE,
    }
    artifacts = {
        "counts": {"manual_review_candidates": 0},
        "manual_review_tickers": [],
        "manual_review_candidates": [],
        "buy_candidates": [candidate_row],
    }
    ex._augment_artifacts(artifacts, {"BIG": outcome})
    assert artifacts["counts"]["manual_review_candidates"] == 1
    assert artifacts["manual_review_tickers"] == ["BIG"]
    assert len(artifacts["manual_review_candidates"]) == 1
    row = artifacts["manual_review_candidates"][0]
    assert row["ticker"] == "BIG"
    assert row["top_up_guard_result"] == "missing_prior_price"
    assert row["prior_same_day_amount"] == pytest.approx(3.0)


def test_manual_review_candidate_row_keeps_top_up_context_when_trend_unvalidated(tmp_path):
    # Codex review on the PR #43 merge: a trend-unvalidated BUY can still have a
    # same-day top-up outcome (_compute_top_up only checks action/multiplier, not
    # trend_validated). Withholding the row from buy_candidates must not also
    # strip the prior-allocation/top-up fields from the manual_review_candidates
    # row -- otherwise a reviewer looking at a 12:30 manual-review case after an
    # earlier morning buy has no way to tell what incremental top-up amount, if
    # any, would be safe.
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=95.0, quantity=round(3.0 / 95.0, 6), selected_base_amount_usd=3.0,
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # FixedDropProvider is flat, so _trend_validated() (>= rejects flat-or-rising)
    # withholds this candidate to manual_review_candidates -- but the ticker still
    # has a resolvable prior fill at $95.0, current price $90.0 is below that
    # reference, so _compute_top_up still produces a real TOP_UP_INCREMENTAL outcome.
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    assert res["artifacts"]["buy_candidates"] == []
    [row] = res["artifacts"]["manual_review_candidates"]
    assert row["ticker"] == "BIG"
    assert row["prior_same_day_amount"] == 3.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["top_up_guard_result"] == TOP_UP_INCREMENTAL
    # 10% drop is beyond the top 8% sizing tier -> capped at 5.0x * $3 base = $15
    # desired; incremental = $15 - the prior $3 fill = $12.
    assert row["incremental_order_amount"] == pytest.approx(12.0)


# --- Isolation from other streams -------------------------------------------
def test_midday_root_is_distinct_from_morning_and_afternoon():
    assert DEFAULT_MIDDAY_LIVE_STATE_ROOT != DEFAULT_MORNING_LIVE_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MIDDAY_LIVE_STATE_ROOT
    assert "midday_dca" in DEFAULT_MIDDAY_LIVE_STATE_ROOT


def test_midday_does_not_write_afternoon_or_morning_artifacts(tmp_path):
    afternoon_root = tmp_path / "ce16ba6b"
    (afternoon_root / "scheduled_runs" / "20260708_1520ET").mkdir(parents=True)
    latest = afternoon_root / "latest_scheduled_run.json"
    latest.write_text(json.dumps({"run_id": "scheduled_20260708_1520ET"}), encoding="utf-8")
    before = latest.read_text(encoding="utf-8")

    midday_root = tmp_path / "midday_dca"
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(midday_root),
                       prior_allocation_roots=(str(tmp_path / "nonexistent_morning"),))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    assert latest.read_text(encoding="utf-8") == before
    assert list(afternoon_root.rglob("run_state*.json")) == []
    for path in res["paths"].values():
        assert str(midday_root) in path


def test_midday_scheduled_run_counts_as_afternoon_prior_context():
    from near_close_dca import _prior_run_is_scheduled

    cfg = MiddayConfig(tickers=("SMALL",))
    midday_rc = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    assert midday_rc["run_origin"] == "scheduled"
    assert _prior_run_is_scheduled({"run_classification": midday_rc}) is False


# --- Config loading ---------------------------------------------------------
def test_load_midday_config_reads_config_file(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(
        json.dumps({
            "sizing_ladder": {
                "base_amount_usd": 4.0,
                "base_amount_below_close_usd": 1.5,
                "no_buy_below_pct": 0.2,
                "tiers": [
                    {"min_drop_pct": 0.2, "max_drop_pct": 3.0, "multiplier": 1.0},
                    {"min_drop_pct": 3.0, "max_drop_pct": None, "multiplier": 2.0},
                ],
            },
            "budget_ledger": {
                "midday_daily_cap_usd": 70.0,
                "midday_weekly_cap_usd": 350.0,
            },
            "artifact_paths": {
                "live_run_state_root": "/custom/midday/root",
                "prior_allocation_roots": ["/custom/morning/root"],
            },
        }),
        encoding="utf-8",
    )
    cfg = load_midday_config(str(cfg_dir))
    assert cfg.base_buy_amount_usd == 4.0
    # Codex review ("Route the reduced base through the midday configuration"):
    # base_amount_below_close_usd must be forwarded like base_amount_usd is, not
    # silently left at MiddayConfig's hardcoded $1 default.
    assert cfg.base_buy_amount_below_close_usd == 1.5
    assert abs(cfg.minimum_drop_vs_previous_close - 0.002) < 1e-9
    assert cfg.morning_daily_cap_usd == 70.0
    assert cfg.morning_weekly_cap_usd == 350.0
    assert cfg.morning_run_state_root == "/custom/midday/root"
    assert cfg.prior_allocation_roots == ("/custom/morning/root",)


def test_load_midday_config_rejects_a_quoted_live_execution_enabled(tmp_path):
    # Codex review ("Reject non-boolean values for the midday live switch"):
    # bool("false") is True in Python, so an operator who tried to disable
    # live placement with a quoted JSON string would silently leave it
    # enabled -- the opposite of what the config says, in the master switch
    # that gates real broker orders. A malformed type must fail closed.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(
        json.dumps({"live_execution_enabled": "false"}), encoding="utf-8"
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.live_execution_enabled is False


def test_manual_run_does_not_consult_the_scheduled_artifact_guard(tmp_path):
    # Codex review ("Bypass the scheduled artifact guard for manual
    # credits"): _credit_decline_already_recorded_this_slot always reads the
    # FIXED scheduled run_state_dir(now), regardless of the `manual` flag
    # (morning's own manual path returns before ever reaching this check, so
    # this is scoped to midday/intraday, which share the same
    # _compute_top_up implementation). A manual poll's genuinely new decline
    # for a ticker must not be silently dropped just because an UNRELATED
    # scheduled slot already recorded its own (different) decline for that
    # same ticker -- the manual ledger key is per-invocation-unique, so this
    # is not a real retry of that scheduled slot.
    #
    # _evaluate_top_up is monkeypatched to return a fixed stays-pending
    # decline directly, isolating the guard-bypass fix from the unrelated
    # prior-allocation/base-selection plumbing that would otherwise need to
    # be wired up just to reach a sub-dollar ladder increment.
    from dca_cumulative import CumulativeCreditLedger
    from morning_dca import TOP_UP_NO_INCREMENTAL, TopUpOutcome

    cfg = MiddayConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(tmp_path / "live"),
        # A non-empty (but nonexistent, so nothing real is scanned) root:
        # an EMPTY tuple would explicitly disable the whole per-ticker top-up
        # guard and return {} before ever reaching the loop this test exercises.
        prior_allocation_roots=(str(tmp_path / "no_prior"),),
        cap_seed_roots=(),
    )
    ex = MiddayDCAExecutor(cfg, mode="live")
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    ex._evaluate_top_up = lambda signal, prior, *, existing_pending_credit_usd=0.0: TopUpOutcome(
        ticker=signal.ticker,
        desired_amount=1.0,
        decision_price=signal.current_price,
        prior_same_day_amount=0.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id=None,
        prior_same_day_source=None,
        incremental_order_amount=0.0,
        top_up_guard_result=TOP_UP_NO_INCREMENTAL,
        top_up_skipped=True,
        cumulative_credit_decline_usd=0.30,
    )
    now = datetime(2026, 7, 8, 12, 30, tzinfo=EASTERN)
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.95,
        previous_close=100.0,
        drop_pct=0.05,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )

    # Simulate the SCHEDULED slot's own dated artifact already showing a
    # recorded decline for AAPL -- as if an earlier scheduled run this same
    # slot already credited it (unrelated to the manual run below).
    run_dir = ex.run_state_dir(now)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "buy_candidates": [{"ticker": "AAPL", "cumulative_credit_decline_usd": 0.30}],
        }),
        encoding="utf-8",
    )

    # A manual run evaluating the SAME ticker must still record its own
    # decline: it is not a retry of the scheduled slot above.
    ex._compute_top_up(now, [signal], manual=True)
    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == 0.30


def test_load_midday_config_accepts_real_json_booleans(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(
        json.dumps({"live_execution_enabled": False}), encoding="utf-8"
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.live_execution_enabled is False


def test_load_midday_config_reads_shared_max_orders_per_day(tmp_path):
    # Codex review ("Load the daily order cap for midday runs"): unlike
    # load_morning_config/load_stream_config, load_midday_config never called
    # load_max_orders_per_day, so MiddayConfig.max_orders_per_day stayed None
    # and _place_broker_orders' shared eight-attempt cap never applied to the
    # midday stream, letting it keep placing orders after the roster-wide
    # daily attempt cap was already exhausted by an earlier stream.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(json.dumps({}), encoding="utf-8")
    (cfg_dir / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": 8}}), encoding="utf-8"
    )
    cfg = load_midday_config(str(cfg_dir))
    assert cfg.max_orders_per_day == 8


def test_load_midday_config_falls_back_on_invalid_utf8(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_bytes(b"\xff")

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.model_usage_log_root == stream_root("model_usage")
    assert cfg.base_buy_amount_usd == MiddayConfig().base_buy_amount_usd


# --- PR #22 bot-review regression coverage (batch: midday config loading) ----


def test_load_midday_config_reads_configured_ticker_whitelist(tmp_path):
    # Codex review #71: the midday live stream must load its ticker whitelist
    # from config/tickers.json, not only the hardcoded module-level
    # ACTIVE_TICKERS/EXCLUDED_TICKERS defaults.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "tickers.json").write_text(
        json.dumps({"active": ["ZZZZ"], "excluded": ["YYYY"]}),
        encoding="utf-8",
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.tickers == ("ZZZZ",)
    assert cfg.excluded_tickers == frozenset({"YYYY"})


def test_load_midday_config_raises_on_invalid_configured_order_cap(tmp_path):
    # Codex review #85: an explicitly configured but invalid
    # circuit_breakers.max_per_order_usd must fail closed -- raise -- rather
    # than silently keep the default cap for a live trading stream.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_per_order_usd": float("nan")}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_midday_config(str(cfg_dir))


def test_load_midday_config_reads_approved_dates_from_trading_window(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "trading_window.json").write_text(
        json.dumps({"start_date": "2026-07-02", "end_date": "2026-07-06", "weekdays_only": True}),
        encoding="utf-8",
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.approved_test_dates == frozenset({date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6)})
    assert cfg.decision_time_et is None


def test_load_midday_config_enables_model_usage_log_by_default(tmp_path):
    # Mirrors load_morning_config: the normal configuration path must resolve
    # a working model_usage_log_root by default so the audit log isn't
    # silently disabled in production.
    from dca_paths import stream_root

    cfg = load_midday_config(str(tmp_path / "nonexistent"))
    assert cfg.model_usage_log_root == stream_root("model_usage")

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(
        json.dumps({"artifact_paths": {"model_usage_log_root": "/custom/model_usage"}}),
        encoding="utf-8",
    )
    cfg = load_midday_config(str(cfg_dir))
    assert cfg.model_usage_log_root == "/custom/model_usage"


def test_midday_executor_default_init_loads_shipped_config():
    ex = MiddayDCAExecutor()
    assert ex.config.base_buy_amount_usd == 2.0
    # Shared Normal-tier ceiling (near_close_dca.BUDGET_TIERS[0]), not the old
    # isolated 90.0 per-stream half-share -- see midday_dca.py's
    # DEFAULT_MIDDAY_DAILY_CAP_USD comment (pooling fix, 2026-08-02).
    assert ex.config.morning_daily_cap_usd == 180.0
    assert date(2026, 7, 2) in ex.config.approved_test_dates
    assert date(2026, 8, 28) in ex.config.approved_test_dates
    assert ex.state_root == DEFAULT_MIDDAY_LIVE_STATE_ROOT
    assert ex.config.sizing_tiers == StrategyConfig().sizing_tiers


def test_midday_ledger_namespace_isolated_from_morning_and_afternoon(tmp_path):
    from near_close_dca import StrategyState

    afternoon = StrategyState()
    day = _now_midday().astimezone(EASTERN).date()
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0,
                       morning_run_state_root=str(tmp_path),
                       prior_allocation_roots=(str(tmp_path / "none"),))
    ex = MiddayDCAExecutor(cfg)
    assert ex.ledger.namespace == "midday"
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex.run(_now_midday(), data, broker=FakeLiveBroker())
    assert afternoon.day_notional(day) == 0.0
    assert ex.ledger.day_planned(day) > 0.0


def test_midday_seeding_ignores_manual_reruns(tmp_path):
    # Codex review (follow-up): manual_reruns/ under the midday root are
    # documented (near_close_dca.py) as post-12:30 backfill artifacts that
    # feed ONLY the afternoon top-up guard, never the midday stream's own
    # scheduled cap ledger. MiddayDCAExecutor inherits
    # _seed_ledger_from_prior_runs from MorningDCAExecutor unchanged, so it
    # was also folding manual_reruns/*/run_state.json into the midday
    # ledger -- meaning a scheduled midday retry could see its own daily cap
    # already partly consumed by an unrelated manual override, wrongly
    # blocking buys the original 12:30 run would have allowed.
    cfg = MiddayConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=200.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MiddayDCAExecutor(cfg)
    trading_day = _now_midday().astimezone(EASTERN).date()

    manual_dir = tmp_path / "manual_reruns" / "manual_run_1"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "manual_run_1",
            "trading_day": trading_day.isoformat(),
            "buy_candidates": [
                {"ticker": "ZZZ", "status": "submitted", "planned_amount_usd": 15.0}
            ],
        }),
        encoding="utf-8",
    )

    ex._seed_ledger_from_prior_runs(_now_midday())

    assert ex.ledger.day_planned(trading_day) == 0.0
    assert ex.ledger.remaining_daily(trading_day) == 20.0


def test_manual_midday_run_with_wrong_broker_account_raises_before_ordering(tmp_path):
    import pytest

    class _WrongAccountBroker:
        def __init__(self):
            self.buying_power_checked = False
            self.placed = False

        def account_number(self):
            return "not-the-approved-account"

        def available_buying_power(self):
            self.buying_power_checked = True
            raise AssertionError("buying power should not be checked for wrong account")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed for wrong account")

    cfg = MiddayConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "midday"),
        target_account_number="999000111",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="broker account mismatch: expected 999000111, got not-the-approved-account"):
        ex.run(_now_midday(), data, manual=True, broker=broker)

    assert broker.buying_power_checked is False
    assert broker.placed is False


# --- Schema validation ------------------------------------------------------
def test_midday_classification_validates_against_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parent / "schemas" / "midday_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    cfg = MiddayConfig(tickers=("SMALL",))
    scheduled = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    manual = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=True)
    for rc in (scheduled, manual):
        jsonschema.validate(rc, schema)


# --- Raw-input persistence (live_schedule_hardening_2026_07_20, inherited) --
def test_midday_scheduled_live_run_persists_raw_inputs(tmp_path):
    # MiddayDCAExecutor inherits MorningDCAExecutor.run() unchanged, so the
    # raw-input persistence / failure-state guarantee applies to the 12:30
    # scheduled live task too, under its OWN state root.
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0, morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    artifacts = res["artifacts"]
    assert artifacts.get("status") != "blocked"
    raw_dir = Path(artifacts["raw_input_dir"])
    assert raw_dir.is_dir()
    assert raw_dir.parent.name == "20260708_1230ET"
    assert artifacts["raw_inputs_saved"]
    assert "raw_persistence_error" not in artifacts


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    with tempfile.TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)


# --- The midday schema defines the account-pinning contract ------------------
# The midday executor shares MorningDCAExecutor's classification builder and so
# emits account_pinning_failed, but the midday schema defined neither that field
# nor account_number. With additionalProperties true, the intended blocked
# document, a truncated live document missing both, and a contradictory one all
# validated identically -- so the marker was not machine-checkable at all on
# this stream (Codex review, PR #219).

def _midday_schema():
    path = Path(__file__).resolve().parent / "schemas" / "midday_run_classification.schema.json"
    return json.loads(path.read_bytes())


def _a_valid_midday_live_classification():
    cfg = MiddayConfig(tickers=("SMALL",), target_account_number=FIXTURE_ACCOUNT_NUMBER)
    return MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)


def test_a_blocked_midday_classification_is_schema_valid():
    jsonschema = pytest.importorskip("jsonschema")
    cfg = MiddayConfig(tickers=("SMALL",), target_account_number=None)
    rc = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    assert rc["account_pinning_failed"] is True
    assert "account_number" not in rc
    jsonschema.validate(rc, _midday_schema())


def test_a_truncated_midday_live_document_is_rejected():
    jsonschema = pytest.importorskip("jsonschema")
    rc = _a_valid_midday_live_classification()
    truncated = {k: v for k, v in rc.items() if k != "account_number"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(truncated, _midday_schema())


def test_a_contradictory_midday_document_is_rejected():
    """A run cannot both report that no account was pinnable and name one."""
    jsonschema = pytest.importorskip("jsonschema")
    rc = _a_valid_midday_live_classification()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**rc, "account_pinning_failed": True}, _midday_schema())


def test_a_pinning_failed_document_asserting_valid_scheduled_evidence_is_rejected():
    """A run that could not pin an account never placed an order, so it can
    never be valid scheduled evidence -- the builder always ties these
    together (account_pinning_failure() routes through _blocked_live_result(),
    which denies scheduled evidence), but nothing enforced it at the schema
    layer. generate_credit_usage_proxy_report.py's consumer rejects only an
    explicit valid_scheduled_evidence=False, so an artifact from another
    producer (or a corrupted document) asserting both would have been counted
    as valid scheduled evidence for a run that never traded (Codex review,
    PR #219: "Deny scheduled evidence when account pinning fails")."""
    jsonschema = pytest.importorskip("jsonschema")
    cfg = MiddayConfig(tickers=("SMALL",), target_account_number=None)
    rc = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    assert rc["account_pinning_failed"] is True
    jsonschema.validate(rc, _midday_schema())  # the real value still passes

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**rc, "valid_scheduled_evidence": True}, _midday_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {k: v for k, v in rc.items() if k != "valid_scheduled_evidence"}, _midday_schema()
        )


def test_a_blocked_midday_document_still_requires_the_broker():
    jsonschema = pytest.importorskip("jsonschema")
    cfg = MiddayConfig(tickers=("SMALL",), target_account_number=None)
    rc = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {k: v for k, v in rc.items() if k != "execution_broker"}, _midday_schema()
        )


@pytest.mark.parametrize("bad", [None, 42, "alpaca", ""])
def test_the_midday_schema_constrains_the_execution_broker(bad):
    """Requiring the key did not validate its VALUE: execution_broker had no
    entry in properties and additionalProperties is true, so a corrupted or
    wrong-broker artifact validated as genuine evidence (Codex review,
    PR #219)."""
    jsonschema = pytest.importorskip("jsonschema")
    rc = _a_valid_midday_live_classification()
    jsonschema.validate(rc, _midday_schema())  # the real value still passes
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**rc, "execution_broker": bad}, _midday_schema())


def test_unconfigured_state_root_resolves_at_executor_construction_time():
    """MiddayConfig.morning_run_state_root stays None when a stream's own
    config doesn't set it (including the config-missing fallback -- see
    load_midday_config, which deliberately does NOT bake a stream_root()
    value into this field, because run_dca.py's cross-stream containment
    check reads its None-ness as "not explicitly configured"; mirrors
    morning_dca.load_morning_config's equivalent fallback). The staleness
    this could otherwise cause -- MorningDCAExecutor.__init__ (inherited by
    MiddayDCAExecutor) falling back to self.profile["state_root"], an
    import-time constant -- is fixed at that call site instead: it now
    resolves stream_root(state_root_stream) fresh on every construction
    (Codex review, PR #219)."""
    probe = (
        "import os\n"
        "import midday_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "cfg = midday_dca.MiddayConfig(tickers=('SMALL',))\n"
        "print(midday_dca.MiddayDCAExecutor(cfg).state_root)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/midday-fallback-probe"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "/srv/midday-fallback-probe/midday_dca", out.stdout
