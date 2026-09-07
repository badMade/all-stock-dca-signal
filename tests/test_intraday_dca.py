"""Self-tests for the intraday graded-DCA executor (11:30 / 1:30 / 2:30 ET).

Scoped to the intraday stream's own deltas; the shared evaluate/build/write
pipeline is covered by test_morning_dca.py and inherited unchanged.

What is intraday specific and therefore tested here:

    - one stream and one cron id across THREE fire times, with per-slot run ids
      and artifact folders but a constant ``stream`` value,
    - one pooled budget ledger shared by all three slots,
    - the prior-allocation chain that the now-removed GATE5 proposal doc's
      section 1.4 identified as missing for these slots (see
      PRODUCTION_READINESS.md gap #1),
    - the ships-disabled placement switch.

Runnable with ``pytest`` or ``python test_intraday_dca.py``.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest
from test_morning_dca import DecliningBarsProvider

from conftest import FIXTURE_ACCOUNT_NUMBER
from dca_paths import CRON_TRACKING_ROOT_ENV, stream_root
from intraday_dca import (
    DEFAULT_INTRADAY_DAILY_CAP_USD,
    DEFAULT_INTRADAY_WEEKLY_CAP_USD,
    INTRADAY_LIVE_CRON_ID,
    INTRADAY_SIZING_TIERS,
    INTRADAY_SLOTS,
    INTRADAY_STREAM,
    IntradayConfig,
    IntradayDCAExecutor,
    default_intraday_cap_seed_roots,
    default_intraday_prior_allocation_roots,
    load_intraday_config,
)
from morning_dca import Bar
from near_close_dca import EASTERN, ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, SourceName, StrategyConfig

TRADING_DAY = date(2026, 7, 8)

SLOT_TIMES = {"1130": time(11, 30), "1330": time(13, 30), "1430": time(14, 30)}


def _now_slot(slot):
    return datetime.combine(TRADING_DAY, SLOT_TIMES[slot], tzinfo=EASTERN)


class _Broker:
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self.placed = {}

    def account_number(self) -> str:
        return FIXTURE_ACCOUNT_NUMBER

    def available_buying_power(self) -> float:
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: tuple[str, ...]
    ) -> Optional[dict[str, Any]]:
        return None

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker: str, trading_day: date, ref_id_prefixes: tuple[str, ...]
    ) -> Optional[dict[str, Any]]:
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[dict]:
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return 0.0

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def is_tradable_for_whole_share_buy(self, ticker: str) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> tuple[bool, str]:
        return True, "ok"

    def review_whole_share_buy(self, ticker: str, quantity: int) -> tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self.placed[ticker] = notional_usd
        return f"ORDER-{ticker}"

    def place_whole_share_buy(self, ticker: str, quantity: int, ref_id: str) -> str:
        return f"ORDER-WHOLE-{ticker}"

    def get_order_fill(self, order_id) -> Any:
        return None

    def get_raw_placement_responses(self) -> list:
        return []


def test_missing_config_file_fails_closed_to_shadow_mode(tmp_path):
    # config/intraday_dca.json ships live_execution_enabled=false ("SHIPS
    # DISABLED") -- a missing config file must not silently fall back to
    # MorningConfig's inherited live_execution_enabled=True default, which
    # would turn a shadow-only stream live precisely when its config cannot
    # be trusted.
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert isinstance(cfg, IntradayConfig)
    assert cfg.live_execution_enabled is False


def test_unparseable_config_file_fails_closed_to_shadow_mode(tmp_path):
    (tmp_path / "intraday_dca.json").write_text("{not valid json", encoding="utf-8")
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def test_explicit_true_in_a_readable_config_still_overrides_the_fail_closed_default(tmp_path):
    # The fail-closed default must not prevent a real, readable config from
    # explicitly re-enabling live placement once it has been vetted.
    (tmp_path / "intraday_dca.json").write_text(
        json.dumps({"live_execution_enabled": True}), encoding="utf-8"
    )
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is True


class _DecliningBarsProvider:
    """Provider generating strictly declining intraday bars for one ticker."""

    def __init__(self, previous_close, drop):
        self.source = "robinhood"
        self._prev = previous_close
        self._drop = drop

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        final_price = self._prev * (1.0 - self._drop)
        start_price = self._prev * (1.0 - self._drop * 0.5)
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        n = len(timestamps)
        anchor_idx = n - 2 if n >= 2 else 0
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars

    def get_bid_ask(self, ticker, as_of):
        price = self._prev * (1.0 - self._drop)
        return (price * 0.9995, price * 1.0005)


class _RefIdTrackingBroker:
    """Broker double that remembers placed ref_ids for real duplicate checks."""
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self.placed = {}
        self._ref_ids = []

    def account_number(self) -> str:
        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self) -> float:
        return 1000.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return any(t == ticker for t, _ in self._ref_ids)

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: tuple[str, ...]
    ) -> Optional[dict[str, Any]]:
        for t, rid in self._ref_ids:
            if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                return {"id": f"ORDER-{ticker}", "dollar_amount": self.placed.get(ticker, 0.0)}
        return None

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker: str, trading_day: date, ref_id_prefixes: tuple[str, ...]
    ) -> Optional[dict[str, Any]]:
        for t, rid in self._ref_ids:
            if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                return {"id": f"ORDER-{ticker}", "dollar_amount": self.placed.get(ticker, 0.0)}
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[dict]:
        for t, rid in self._ref_ids:
            if t == ticker and rid == ref_id:
                return {"id": f"ORDER-{ticker}", "dollar_amount": self.placed.get(ticker, 0.0)}
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return sum(amt for t, amt in self.placed.items() if t == ticker)

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def is_tradable_for_whole_share_buy(self, ticker: str) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> tuple[bool, str]:
        return True, "ok"

    def review_whole_share_buy(self, ticker: str, quantity: int) -> tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self.placed[ticker] = notional_usd
        self._ref_ids.append((ticker, ref_id))
        return f"ORDER-{ticker}-{len(self._ref_ids)}"

    def place_whole_share_buy(self, ticker: str, quantity: int, ref_id: str) -> str:
        return f"ORDER-WHOLE-{ticker}-{len(self._ref_ids)}"

    def get_order_fill(self, order_id) -> Any:
        return None

    def get_raw_placement_responses(self) -> list:
        return []


def _cfg(tmp_path, **overrides):
    base = dict(
        tickers=("TICK",),
        morning_run_state_root=str(tmp_path / "intraday"),
        target_account_number=FIXTURE_ACCOUNT_NUMBER,
        approved_test_dates=frozenset({TRADING_DAY}),
        prior_allocation_roots=(),
        cap_seed_roots=(),
    )
    base.update(overrides)
    return IntradayConfig(**base)


# --- Ladder and caps --------------------------------------------------------
def test_intraday_ladder_is_the_shared_live_ladder():
    assert INTRADAY_SIZING_TIERS == StrategyConfig().sizing_tiers
    ex = IntradayDCAExecutor(IntradayConfig())
    assert ex.multiplier_for_drop(0.08) == 5.0
    assert ex.multiplier_for_drop(0.0005) == 0.0


def test_intraday_caps_are_shared_across_all_three_slots():
    # $180/$900 TOTAL across the three slots, not per slot: cap_seed_roots
    # includes this stream's own root so a later slot sees an earlier one's spend.
    assert DEFAULT_INTRADAY_DAILY_CAP_USD == 180.0
    assert DEFAULT_INTRADAY_WEEKLY_CAP_USD == 900.0
    for slot in INTRADAY_SLOTS:
        ex = IntradayDCAExecutor(IntradayConfig(), slot=slot)
        assert ex.ledger.daily_cap_usd == 180.0
        assert ex.ledger.namespace == INTRADAY_STREAM
    assert stream_root("intraday_dca") in default_intraday_cap_seed_roots()


# --- One stream, one cron id, three slots ----------------------------------
def test_each_slot_has_its_own_folder_run_id_and_fire_time(tmp_path):
    expected = {
        "1130": ("20260708_1130ET", "intraday_live_20260708_1130ET", "11:30"),
        "1330": ("20260708_1330ET", "intraday_live_20260708_1330ET", "13:30"),
        "1430": ("20260708_1430ET", "intraday_live_20260708_1430ET", "14:30"),
    }
    for slot, (folder, run_id, fire_time) in expected.items():
        ex = IntradayDCAExecutor(_cfg(tmp_path), slot=slot)
        now = _now_slot(slot)
        assert ex.run_state_dir(now).name == folder
        assert ex.run_id(now, manual=False) == run_id
        assert ex.profile["scheduled_for_et"] == fire_time
        assert ex.slot == slot


def test_stream_and_cron_id_are_constant_across_slots(tmp_path):
    # schemas/intraday_run_classification.schema.json pins stream to the constant
    # "intraday" -- explicitly NOT a slot-suffixed variant like "intraday_1430".
    for slot in INTRADAY_SLOTS:
        rc = IntradayDCAExecutor(_cfg(tmp_path), slot=slot).run_classification(
            _now_slot(slot), manual=False
        )
        assert rc["stream"] == "intraday"
        assert rc["cron_id"] == INTRADAY_LIVE_CRON_ID == "7be82b97"
        assert rc["run_type"] == "intraday_dca_live"
        assert rc["state_schema_version"] == "intraday-live-v1"
        assert rc["valid_morning_evidence"] is False
        assert rc["valid_afternoon_evidence"] is False


def test_unknown_slot_is_rejected():
    with pytest.raises(ValueError, match="unknown intraday slot"):
        IntradayDCAExecutor(IntradayConfig(), slot="1200")


def test_slot_profiles_do_not_leak_between_instances(tmp_path):
    # The executor shadows the class-level PROFILES with a per-instance mapping;
    # a bug there would let one slot's profile bleed into the next instance.
    a = IntradayDCAExecutor(_cfg(tmp_path), slot="1130")
    b = IntradayDCAExecutor(_cfg(tmp_path), slot="1430")
    assert a.profile["scheduled_for_et"] == "11:30"
    assert b.profile["scheduled_for_et"] == "14:30"
    assert a.profile["folder_suffix"] == "1130ET"


def test_intraday_classification_validates_against_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "intraday_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    for slot in INTRADAY_SLOTS:
        ex = IntradayDCAExecutor(_cfg(tmp_path), slot=slot)
        for manual in (False, True):
            jsonschema.validate(ex.run_classification(_now_slot(slot), manual=manual), schema)


# --- Trend validation applies to all three slots ---------------------------
def test_all_intraday_slots_validate_trend(tmp_path):
    # Unlike the 9:35 market-open slot, each of these has a full 60-minute
    # regular-session window available, so none is trend-exempt.
    for slot in INTRADAY_SLOTS:
        assert IntradayDCAExecutor(_cfg(tmp_path), slot=slot).profile["validate_trend"] is True


# --- The ships-disabled placement switch ------------------------------------
def test_shipped_intraday_config_has_execution_disabled():
    cfg = load_intraday_config(allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def test_intraday_disabled_writes_full_artifacts_and_places_nothing(tmp_path):
    ex = IntradayDCAExecutor(_cfg(tmp_path, live_execution_enabled=False), slot="1330")
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_slot("1330"), data, broker=broker)

    assert broker.placed == {}
    artifacts = res["artifacts"]
    assert artifacts["live_execution_enabled"] is False
    assert artifacts["signal_rows"]
    out = Path(res["paths"]["run_state"]).parent
    assert out.name == "20260708_1330ET"
    for name in ("run_state.json", "run_summary.json", "intraday_signals.csv", "submitted_orders.csv"):
        assert (out / name).exists(), f"{name} missing from a disabled run"


def test_intraday_enabled_does_place(tmp_path):
    ex = IntradayDCAExecutor(_cfg(tmp_path, live_execution_enabled=True), slot="1130")
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    ex.run(_now_slot("1130"), data, broker=broker)
    assert broker.placed.get("TICK") == 5.0  # 2.5x * $2 base


# --- The prior-allocation chain the now-removed GATE5 proposal's section 1.4 flagged --------
def test_prior_allocation_roots_cover_every_earlier_same_day_stream():
    # The specific gap: a ticker bought at an earlier slot used to be invisible
    # to the guard at the next one, so it was re-bought full size at a higher
    # price. intraday_dca is its OWN root, which is what lets 1:30 see 11:30.
    roots = default_intraday_prior_allocation_roots()
    for earlier in ("market_open_dca", "morning_dca", "midday_dca", "intraday_dca"):
        assert stream_root(earlier) in roots, f"{earlier} missing from the top-up chain"
    # Scheduled afternoon runs are never prior context, and 3:30 fires after all
    # three of these anyway.
    assert stream_root("ce16ba6b") not in roots


def test_a_later_slot_sees_an_earlier_slots_fill_as_prior_allocation(tmp_path):
    # End-to-end version of the rule above, through the real executor: the 11:30
    # slot's own artifact must offset the 1:30 slot's buy for the same ticker.
    root = tmp_path / "intraday"
    from test_morning_dca import _write_prior_run_state

    _write_prior_run_state(
        root / "prior", "TICK", amount_usd=3.0, decision_price=97.0, selected_base_amount_usd=2.0
    )
    ex = IntradayDCAExecutor(
        _cfg(tmp_path, prior_allocation_roots=(str(root / "prior"),), live_execution_enabled=True),
        slot="1330",
    )
    broker = _Broker()
    # 5% drop -> price 95.0, below the $97.0 prior reference, so the guard tops
    # up only the difference rather than buying the full $5.00 again.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_slot("1330"), data, broker=broker)

    rows = {c["ticker"]: c for c in res["artifacts"].get("buy_candidates", [])}
    assert "TICK" in rows, "candidate row missing"
    assert rows["TICK"]["prior_same_day_amount"] == 3.0
    placed = broker.placed.get("TICK")
    assert placed is not None and placed < 5.0, (
        "the 1:30 slot re-bought the full amount instead of topping up -- the "
        "prior-allocation gap (PRODUCTION_READINESS.md gap #1) has reopened"
    )


# --- Root resolution timing (Codex review, PR #219) -------------------------
def test_intraday_default_roots_resolve_at_construction_not_import(monkeypatch, tmp_path):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, str(tmp_path / "relocated"))
    roots = default_intraday_prior_allocation_roots()
    assert all(str(tmp_path / "relocated") in r for r in roots)
    assert IntradayConfig().prior_allocation_roots == roots


# --- Artifact-root separation ----------------------------------------------
def test_intraday_writes_only_under_its_own_root(tmp_path):
    root = tmp_path / "intraday"
    ex = IntradayDCAExecutor(_cfg(tmp_path, live_execution_enabled=False), slot="1430")
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_slot("1430"), data, broker=_Broker())
    for path in res["paths"].values():
        assert str(root) in str(path), f"{path} escaped the intraday root"


def test_intraday_blocked_outside_the_approved_window_still_writes_state(tmp_path):
    ex = IntradayDCAExecutor(
        _cfg(tmp_path, approved_test_dates=frozenset({date(2026, 7, 9)})), slot="1130"
    )
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_slot("1130"), data, broker=broker)
    assert broker.placed == {}
    assert res["artifacts"]["status"] == "blocked"
    assert Path(res["paths"]["run_state"]).exists()


def test_idempotency_stream_differs_per_slot_but_matches_a_same_slot_retry():
    # Codex review, P1 ("Scope intraday idempotency keys to each slot"): all
    # three slots share self.stream == "intraday", so the idempotency
    # namespace used for the ref_id / same-stream duplicate check must
    # include the fired slot too, or a later slot's legitimate top-up
    # collides with an earlier slot's ref id.
    ex_1130 = IntradayDCAExecutor(slot="1130")
    ex_1330 = IntradayDCAExecutor(slot="1330")
    ex_1130_retry = IntradayDCAExecutor(slot="1130")

    assert ex_1130._idempotency_stream() != ex_1330._idempotency_stream()
    assert ex_1130._idempotency_stream() == ex_1130_retry._idempotency_stream()


def test_later_slot_top_up_is_not_treated_as_earlier_slots_duplicate(tmp_path):
    # Integration-level regression: an 11:30 fill must not block a genuine,
    # lower-price 13:30 incremental top-up for the same ticker just because
    # both slots share stream="intraday" and would otherwise collide on the
    # same ref_id/duplicate-check namespace.
    live_root = tmp_path / "live"
    trading_day = date(2026, 7, 8)
    cfg = IntradayConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        prior_allocation_roots=(str(live_root),),
        approved_test_dates=frozenset({trading_day}),
    )
    broker = _RefIdTrackingBroker()

    ex_1130 = IntradayDCAExecutor(cfg, slot="1130")
    now_1130 = datetime.combine(trading_day, time(11, 30), tzinfo=EASTERN)
    data_1130 = _DecliningBarsProvider(100.0, 0.03)
    res_1130 = ex_1130.run(now_1130, data_1130, broker=broker)
    assert broker.placed.get("NVDA") == pytest.approx(4.5)  # 1.5x * $3
    first_ref_ids = list(broker._ref_ids)

    # Confirm the fill so the next slot's top-up guard has a known reference
    # price to top up against (mirrors production reconciliation).
    state_path = Path(res_1130["paths"]["run_state"])
    state = json.loads(state_path.read_bytes())
    for row in state["buy_candidates"]:
        if row["ticker"] == "NVDA":
            row["status"] = "filled"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    ex_1330 = IntradayDCAExecutor(cfg, slot="1330")
    now_1330 = datetime.combine(trading_day, time(13, 30), tzinfo=EASTERN)
    data_1330 = _DecliningBarsProvider(100.0, 0.05)  # deeper drop -> lower price
    ex_1330.run(now_1330, data_1330, broker=broker)

    assert len(broker._ref_ids) == 2, (
        "the 13:30 slot must place its own incremental order, not be skipped "
        "as a duplicate of the 11:30 slot's ref id"
    )
    assert broker._ref_ids[1][1] != first_ref_ids[0][1]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
