"""Self-tests for the market-open graded-DCA executor (9:35 AM ET live stream).

Scoped to the market-open stream's own deltas, the same way test_midday_dca.py
is scoped relative to test_morning_dca.py: the shared evaluate/build/write
pipeline is already covered by test_morning_dca.py and is inherited unchanged.

What is market-open specific and therefore tested here:

    - the trend-validation exemption (only ~5 minutes of session data exist at
      9:35, so a completed 60-minute downtrend can never validate),
    - the slot evaluating its full configured universe with no upstream
      artifact dependency (the retired 9:45 pre-flight is gone entirely),
    - the ships-disabled placement switch,
    - stream identity, artifact roots, and root-resolution timing.

Runnable with ``pytest`` or ``python test_market_open_dca.py``.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest
from test_morning_dca import DecliningBarsProvider, FixedDropProvider

from conftest import FIXTURE_ACCOUNT_NUMBER
from dca_paths import CRON_TRACKING_ROOT_ENV, stream_root
from market_open_dca import (
    DEFAULT_MARKET_OPEN_DAILY_CAP_USD,
    DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD,
    MARKET_OPEN_LIVE_CRON_ID,
    MARKET_OPEN_SIZING_TIERS,
    MARKET_OPEN_STREAM,
    MarketOpenConfig,
    MarketOpenDCAExecutor,
    default_market_open_cap_seed_roots,
    default_market_open_prior_allocation_roots,
    load_market_open_config,
)
from morning_dca import EXEC_AUTO_BUY_ELIGIBLE, Bar
from near_close_dca import EASTERN, Action, SourceName, StrategyConfig, load_prior_same_day_allocations

TRADING_DAY = date(2026, 7, 8)


# Live simulations use their supplied historical clock; session/calendar gates stay enabled.
pytestmark = pytest.mark.usefixtures("historical_live_clock")


def _now_market_open():
    """Decision timestamp for the 9:35 AM ET live execution."""
    return datetime.combine(TRADING_DAY, time(9, 35), tzinfo=EASTERN)


class _Broker:
    """Minimal successful broker that records every placement."""
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self.placed = {}

    def account_number(self) -> str:
        return FIXTURE_ACCOUNT_NUMBER

    def available_buying_power(self) -> float:
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Optional[dict]:
        return None

    def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes) -> Optional[dict]:
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[dict]:
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return 0.0

    supports_concurrent_fill_lookup: bool = False

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def is_tradable_for_whole_share_buy(self, ticker) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> tuple[bool, str]:
        return True, "ok"

    def review_whole_share_buy(self, ticker, quantity) -> tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self.placed[ticker] = notional_usd
        return f"ORDER-{ticker}"

    def place_whole_share_buy(self, ticker, quantity, ref_id) -> str:
        return f"ORDER-WS-{ticker}"

    def get_order_fill(self, order_id) -> Any:
        return None

    def get_raw_placement_responses(self) -> list:
        return []


def _cfg(tmp_path, **overrides):
    base = dict(
        tickers=("TICK",),
        morning_run_state_root=str(tmp_path / "market_open"),
        target_account_number=FIXTURE_ACCOUNT_NUMBER,
        approved_test_dates=frozenset({TRADING_DAY}),
        prior_allocation_roots=(),
        cap_seed_roots=(),
    )
    base.update(overrides)
    return MarketOpenConfig(**base)


# --- Ladder and caps are the shared ones -----------------------------------
def test_market_open_ladder_is_the_shared_live_ladder():
    assert MARKET_OPEN_SIZING_TIERS == StrategyConfig().sizing_tiers
    ex = MarketOpenDCAExecutor(MarketOpenConfig())
    # 2026-10-04 ladder rescale: log-scaled, 8% -> 5.5x (max is 8x at 16%+).
    assert ex.multiplier_for_drop(0.08) == 5.5
    assert ex.multiplier_for_drop(0.0005) == 0.0


def test_market_open_caps_are_the_shared_normal_tier_ceiling():
    # Not an isolated per-stream share: the same $180/$900 every live stream
    # draws from, so cross-stream seeding is meaningful.
    assert DEFAULT_MARKET_OPEN_DAILY_CAP_USD == 180.0
    assert DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD == 900.0
    ex = MarketOpenDCAExecutor(MarketOpenConfig())
    assert ex.ledger.daily_cap_usd == 180.0
    assert ex.ledger.weekly_cap_usd == 900.0
    assert ex.ledger.namespace == MARKET_OPEN_STREAM


# --- Stream identity --------------------------------------------------------
def test_market_open_stream_identity_and_artifact_naming(tmp_path):
    ex = MarketOpenDCAExecutor(_cfg(tmp_path))
    now = _now_market_open()
    assert ex.stream == "market_open"
    assert ex.run_state_dir(now).name == "20260708_0935ET"
    assert ex.run_id(now, manual=False) == "market_open_live_20260708_0935ET"
    assert ex.signals_csv_name == "market_open_signals.csv"
    assert ex.latest_pointer_name == "latest_market_open_run.json"


def test_market_open_classification_fields(tmp_path):
    rc = MarketOpenDCAExecutor(_cfg(tmp_path)).run_classification(_now_market_open(), manual=False)
    assert rc["stream"] == "market_open"
    assert rc["run_type"] == "market_open_dca_live"
    assert rc["cron_id"] == MARKET_OPEN_LIVE_CRON_ID == "2ba19526"
    assert rc["scheduled_for_et"] == "09:35"
    assert rc["state_schema_version"] == "market-open-live-v1"
    assert rc["trades"] is True
    # Never evidence for any other stream.
    assert rc["valid_morning_evidence"] is False
    assert rc["valid_afternoon_evidence"] is False


def test_market_open_classification_validates_against_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "market_open_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    ex = MarketOpenDCAExecutor(_cfg(tmp_path))
    for manual in (False, True):
        jsonschema.validate(ex.run_classification(_now_market_open(), manual=manual), schema)


# --- The trend-validation exemption (market-open specific) ------------------
def test_market_open_is_exempt_from_trend_validation(tmp_path):
    # config/scheduled_tasks.json:2ba19526.trend_validation_exemption, user-confirmed
    # 2026-07-27: at 9:35 only ~5 minutes of session data exist, so requiring a
    # completed 60-minute downtrend would make every market-open buy unbuyable.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path))
    assert ex.profile["validate_trend"] is False


def test_market_open_auto_buys_without_a_validatable_trend(tmp_path):
    # FixedDropProvider emits FLAT bars, so no downward trend can be proven. A
    # trend-validating stream would flag manual_review_candidate; this one must
    # still reach an auto-buy-eligible BUY.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path))
    flat = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    signal = ex.evaluate(_now_market_open(), flat)[0]
    assert signal.action == Action.BUY
    assert signal.multiplier == 4.5  # 2026-10-04 ladder rescale: 5% drop was 2.5x
    assert signal.execution_status != "manual_review_candidate"


# --- No upstream artifact dependency ----------------------------------------
def test_market_open_evaluates_the_full_universe_with_no_upstream_artifact(tmp_path):
    # This slot fires at 9:35 and reads no artifact from any earlier run (the
    # 9:45 pre-flight that used to be a morning-stream input was retired). It
    # must still evaluate and surface its ticker from a bare state root.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    signals = ex.evaluate(_now_market_open(), data)
    assert [s.ticker for s in signals] == ["TICK"]
    assert not hasattr(ex, "preflight_gate")


# --- The live placement switch ----------------------------------------------
def test_shipped_market_open_config_has_execution_enabled():
    # Operator-promoted on 2026-10-04 for live Robinhood execution.
    cfg = load_market_open_config(allow_unpinned_account=True)
    assert cfg.live_execution_enabled is True


def test_market_open_disabled_writes_full_artifacts_and_places_nothing(tmp_path):
    ex = MarketOpenDCAExecutor(_cfg(tmp_path, live_execution_enabled=False))
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_market_open(), data, broker=broker)

    # Nothing was placed...
    assert broker.placed == {}
    # ...but the run still evaluated and still wrote a complete artifact set.
    artifacts = res["artifacts"]
    assert artifacts["live_execution_enabled"] is False
    assert artifacts["signal_rows"], "a shadow run must still record its signals"
    out = Path(res["paths"]["run_state"]).parent
    for name in ("run_state.json", "run_summary.json", "market_open_signals.csv", "submitted_orders.csv"):
        assert (out / name).exists(), f"{name} missing from a disabled run"
    # submitted_orders.csv is header-only rather than absent.
    assert (out / "submitted_orders.csv").read_text(encoding="utf-8").strip().splitlines()[0]


def test_market_open_enabled_does_place(tmp_path):
    # The complement of the test above: with the switch on, the same inputs do
    # reach the broker -- proving the disabled case above is the switch working,
    # not the fixture silently failing to produce a candidate.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path, live_execution_enabled=True))
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    ex.run(_now_market_open(), data, broker=broker)
    assert broker.placed.get("TICK") == 9.0  # 4.5x * $2 base (2026-10-04 ladder rescale; was 2.5x)


def test_market_open_max_orders_per_day_blocks_attempts_beyond_the_shared_cap(tmp_path):
    # DCA Issues row ``max_orders_per_day_unenforced`` (triage re-verification):
    # this stream places through the inherited MorningDCAExecutor
    # ._place_broker_orders, whose test_morning_dca.py pin uses the base class
    # only. Pin it per subclass so a stream that overrode the placement loop
    # (or whose loader dropped the cap) cannot silently place past the shared
    # roster-wide daily attempt cap. Cap 2, three qualifying tickers: exactly
    # two submit, the third is blocked on the attempt ceiling.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path, live_execution_enabled=True, tickers=("AAA", "BBB", "CCC"), max_orders_per_day=2))
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05, "CCC": 0.05})
    res = ex.run(_now_market_open(), data, broker=broker)
    outcomes = res["artifacts"]["broker_outcomes"]
    assert sorted(t for t, oc in outcomes.items() if oc["status"] == "submitted") == sorted(broker.placed)
    assert len(broker.placed) == 2
    [blocked] = [t for t, oc in outcomes.items() if oc["status"] == "blocked"]
    assert "order daily attempt cap" in outcomes[blocked]["skip_reason"]


def test_shipped_market_open_config_carries_the_budgets_json_order_count_cap():
    # The loader must overlay circuit_breakers.max_orders_per_day, else the
    # test above would pass only for a hand-built config and the live stream
    # would run uncapped.
    import dca_runner

    cfg = load_market_open_config(allow_unpinned_account=True)
    assert cfg.max_orders_per_day == dca_runner.MAX_ORDERS_PER_DAY


# --- Root resolution timing (Codex review, PR #219) -------------------------
def test_market_open_default_roots_resolve_at_construction_not_import(monkeypatch, tmp_path):
    # A literal module-level tuple would bind $DCA_CRON_TRACKING_ROOT at import
    # time, leaving an executor built after the variable is set scanning the old
    # tree and silently finding no prior allocations.
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, str(tmp_path / "relocated"))
    roots = default_market_open_prior_allocation_roots()
    cap_roots = default_market_open_cap_seed_roots()
    assert all(str(tmp_path / "relocated") in r for r in roots)
    assert all(str(tmp_path / "relocated") in r for r in cap_roots)
    assert MarketOpenConfig().prior_allocation_roots == roots


def test_market_open_prior_allocation_roots_are_deliberately_narrow():
    # Nothing scheduled runs before 9:35, so only hand-placed manual fills can
    # precede it. morning_dca is included because the prior-allocation loader
    # recurses and so catches morning_dca/manual_recovery/YYYYMMDD_*.
    roots = default_market_open_prior_allocation_roots()
    assert roots == (stream_root("market_open_dca"), stream_root("morning_dca"), stream_root("final_manual_review"))


def test_market_open_cap_seed_roots_span_every_sibling_stream():
    cap_roots = default_market_open_cap_seed_roots()
    assert cap_roots == (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("midday_dca"),
        stream_root("intraday_dca"),
        stream_root("ce16ba6b"),
        stream_root("final_manual_review"),
    )


# --- Artifact-root separation ----------------------------------------------
def test_market_open_writes_only_under_its_own_root(tmp_path):
    root = tmp_path / "market_open"
    ex = MarketOpenDCAExecutor(_cfg(tmp_path, live_execution_enabled=False))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_market_open(), data, broker=_Broker())
    for path in res["paths"].values():
        assert str(root) in str(path), f"{path} escaped the market-open root"
    # Never touches the afternoon pointer.
    assert not (tmp_path / "latest_scheduled_run.json").exists()


def test_market_open_blocked_outside_the_approved_window_still_writes_state(tmp_path):
    # The gate this stream previously had no code-level equivalent of at all:
    # before market_open_dca.py existed it would have kept placing orders past
    # config/trading_window.json's end date.
    ex = MarketOpenDCAExecutor(_cfg(tmp_path, approved_test_dates=frozenset({date(2026, 7, 9)})))
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.05})
    res = ex.run(_now_market_open(), data, broker=broker)
    assert broker.placed == {}
    assert res["artifacts"]["status"] == "blocked"
    assert Path(res["paths"]["run_state"]).exists()


def test_missing_config_file_fails_closed_to_shadow_mode(tmp_path):
    # config/market_open_dca.json ships live_execution_enabled=false ("SHIPS
    # DISABLED") -- a missing config file must not silently fall back to
    # MorningConfig's inherited live_execution_enabled=True default, which
    # would turn a shadow-only stream live precisely when its config cannot
    # be trusted.
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert isinstance(cfg, MarketOpenConfig)
    assert cfg.live_execution_enabled is False


def test_unparseable_config_file_fails_closed_to_shadow_mode(tmp_path):
    (tmp_path / "market_open_dca.json").write_text("{not valid json", encoding="utf-8")
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def test_non_object_json_config_file_fails_closed_to_shadow_mode(tmp_path):
    # Copilot review ("load_stream_config() assumes the parsed JSON is a
    # dict"): a file can be valid JSON but not an object -- "[]" parses
    # without error, but every overlay in load_stream_config calls
    # raw.get(...)/raw["..."], which raises AttributeError on a list instead
    # of falling back to shadow-mode defaults like every other malformed-file
    # case here.
    (tmp_path / "market_open_dca.json").write_text("[]", encoding="utf-8")
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def test_explicit_true_in_a_readable_config_still_overrides_the_fail_closed_default(tmp_path):
    # The fail-closed default must not prevent a real, readable config from
    # explicitly re-enabling live placement once it has been vetted.
    (tmp_path / "market_open_dca.json").write_text(
        json.dumps({"live_execution_enabled": True}), encoding="utf-8"
    )
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is True


def test_quoted_live_execution_enabled_fails_closed_instead_of_coercing_truthy(tmp_path):
    # Codex review ("Reject non-boolean values for the midday live switch" --
    # the same load_stream_config() bug affects every stream built on it,
    # including this one): bool("false") is True in Python, so an operator
    # who tried to disable live placement with a quoted JSON string would
    # silently leave it enabled -- the opposite of what the config says, in
    # the master switch that gates real broker orders.
    (tmp_path / "market_open_dca.json").write_text(
        json.dumps({"live_execution_enabled": "false"}), encoding="utf-8"
    )
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def _write_manual_recovery_run(root, subdir, ticker, amount_usd, decision_price):
    run_dir = root / "manual_runs" / subdir
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:20:00-04:00",
            "run_classification": {
                "run_origin": "manual",
                "run_id": f"manual_market_open_live_{subdir}",
            },
            "buy_candidates": [
                {
                    "ticker": ticker,
                    "status": "filled",
                    "planned_amount_usd": amount_usd,
                    "target_price": decision_price,
                }
            ],
        }),
        encoding="utf-8",
    )


def test_default_roots_include_market_opens_own_manual_run_tree(tmp_path):
    # Codex review ("Scan market-open manual runs as prior allocations"):
    # MarketOpenDCAExecutor.run(..., manual=True) writes a successful manual
    # recovery fill under this stream's OWN market_open_dca/manual_runs tree.
    # default_market_open_prior_allocation_roots() previously omitted its own
    # root entirely, so a recovery order placed shortly before 9:35 was
    # invisible to the scheduled invocation's own top-up guard -- the guard
    # reported no prior allocation and the broad duplicate check skipped the
    # ticker instead of submitting the intended incremental top-up.
    root = tmp_path / "cron_tracking" / "market_open_dca"
    _write_manual_recovery_run(root, "20260629_0920ET", "AAPL", 3.0, 98.25)

    # Scan exactly the root this stream's own state_root resolves to (as
    # default_market_open_prior_allocation_roots() now includes).
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price == 98.25


def test_default_market_open_prior_allocation_roots_includes_own_root():
    roots = default_market_open_prior_allocation_roots()
    assert any(r.rstrip("/").endswith("market_open_dca") for r in roots)


def test_bundled_config_prior_allocation_roots_includes_own_root():
    # Codex review ("Include market-open manual runs in the loaded roots"):
    # config/market_open_dca.json's checked-in artifact_paths.
    # prior_allocation_roots is an explicit list that REPLACES
    # default_market_open_prior_allocation_roots() for a normal
    # load_market_open_config() call -- fixing only the Python default was
    # not enough, since the real bundled config still overrode it with a
    # list omitting the stream's own manual_runs root.
    cfg = load_market_open_config(allow_unpinned_account=True)
    assert any(str(r).rstrip("/").endswith("market_open_dca") for r in cfg.prior_allocation_roots)


class _FixedDropProvider:
    """Minimal provider: a ticker drops a fixed fraction vs. prior close."""

    def __init__(self, source, previous_close, drops):
        self.source = source
        self._prev = previous_close
        self._drops = drops

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
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


def test_trend_exempt_buy_is_marked_auto_buy_eligible(tmp_path):
    # Codex review ("Mark trend-exempt market-open buys as auto-buy
    # eligible"): market-open's live profile skips trend validation
    # (validate_trend=False, only 5 minutes of session data exist at 9:35),
    # but a qualifying signal is still auto-bought once
    # live_execution_enabled is turned on. Leaving execution_status at its
    # None default would serialize auto_buy_signal=false for a candidate the
    # placement path actually attempts, blinding scripts/drift_detection.py's
    # missing-order check to a lost/failed order.
    cfg = MarketOpenConfig(tickers=("AAPL",), morning_run_state_root=str(tmp_path))
    ex = MarketOpenDCAExecutor(cfg)
    data = _FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.03})

    sig = ex.evaluate_signal("AAPL", datetime(2026, 6, 29, 9, 35, tzinfo=EASTERN), data)

    assert sig.action == Action.BUY
    assert sig.execution_status == EXEC_AUTO_BUY_ELIGIBLE
    assert sig.to_row()["auto_buy_signal"] is True


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


# --- 2026-09-07 diagnostic T:F13 ---------------------------------------------
def test_market_open_default_config_tolerates_an_unresolvable_account_and_fails_closed(tmp_path, monkeypatch):
    """MarketOpenDCAExecutor._load_default_config is the override that decides
    how the 9:35 stream behaves when no explicit config is passed and the live
    account cannot be resolved: it must construct (allow_unpinned_account) and
    then fail closed at run time with blocked evidence, never raise before an
    executor exists and leave the slot with no artifact at all."""
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    (tmp_path / "market_open_dca.json").write_text(
        json.dumps({
            "live_task": {"broker_account": "robinhood:${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"},
            "artifact_paths": {"live_run_state_root": str(tmp_path / "market_open")},
        }),
        encoding="utf-8",
    )
    ex = MarketOpenDCAExecutor(config_dir=str(tmp_path))
    assert ex.config.target_account_number is None
    assert ex.config.live_execution_enabled is False  # ships disabled, and a missing key must not flip it


@pytest.mark.parametrize(
    ("raw_text", "match"),
    [
        ('{"live_execution_enabled": false, "live_execution_enabled": true}', "duplicate"),
        ('{"budget_ledger": {"market_open_daily_cap_usd": Infinity}}', "finite"),
        (f'{{"budget_ledger": {{"market_open_weekly_cap_usd": {10**400}}}}}', "finite"),
        ('{"budget_ledger": {"market_open_daily_cap_usd": true}}', "boolean"),
        ('{"sizing_ladder": {"base_amount_usd": NaN}}', "finite"),
        ('{"trend_min_bars": true}', "non-negative integer"),
    ],
)
def test_load_stream_config_rejects_malformed_stream_config_values(tmp_path, raw_text, match):
    (tmp_path / "market_open_dca.json").write_text(raw_text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
