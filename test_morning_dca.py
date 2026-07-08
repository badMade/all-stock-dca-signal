"""Self-tests for the morning graded-DCA dry-run executor.

Covers the morning ladder threshold brackets, the separate budget ledger
namespace, the run-state path/classification, and the guarantee that a morning
run never interacts with the afternoon ce16ba6b scheduled evidence.

Runnable with ``pytest`` or ``python test_morning_dca.py``.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from morning_dca import (
    DEFAULT_MORNING_STATE_ROOT,
    MORNING_CRON_ID,
    MORNING_SIZING_TIERS,
    DCAExecutor,
    MorningConfig,
    MorningDCAExecutor,
    MorningLedger,
    nyse_holidays,
)
from near_close_dca import EASTERN, Action, Bar, SourceName


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


def _now():
    return datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)


# --- NYSE holiday calendar ---------------------------------------------------
def test_nyse_holidays_2026_matches_documented_observed_dates():
    holidays = nyse_holidays(2026)
    assert date(2026, 1, 1) in holidays  # New Year's Day
    assert date(2026, 1, 19) in holidays  # MLK Day
    assert date(2026, 2, 16) in holidays  # Washington's Birthday
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 5, 25) in holidays  # Memorial Day
    assert date(2026, 6, 19) in holidays  # Juneteenth
    # Independence Day (Jul 4) falls on a Saturday in 2026 -> observed Friday,
    # matching near_close_dca.MARKET_HOLIDAYS' hand-documented value.
    assert date(2026, 7, 3) in holidays
    assert date(2026, 9, 7) in holidays  # Labor Day
    assert date(2026, 11, 26) in holidays  # Thanksgiving
    assert date(2026, 12, 25) in holidays  # Christmas
    assert len(holidays) == 10


def test_nyse_holidays_shifts_weekend_fixed_dates():
    # 2027: Jul 4 falls on a Sunday -> observed Monday Jul 5.
    assert date(2027, 7, 5) in nyse_holidays(2027)
    assert date(2027, 7, 4) not in nyse_holidays(2027)


# --- Threshold brackets -----------------------------------------------------
def test_ladder_brackets():
    ex = MorningDCAExecutor(MorningConfig(base_buy_amount_usd=3.0))
    cases = {
        0.0005: 0.0,   # < 0.1% -> 0x
        0.001: 1.0,    # 0.1%   -> 1x
        0.019: 1.0,    # < 2%   -> 1x
        0.020: 1.5,    # 2%     -> 1.5x
        0.049: 1.5,    # < 5%   -> 1.5x
        0.050: 2.0,    # 5%     -> 2x
        0.079: 2.0,    # < 8%   -> 2x
        0.080: 3.0,    # 8%     -> 3x
        0.20: 3.0,     # >= 8%  -> 3x (no upper cutoff)
    }
    for drop, expected in cases.items():
        assert ex.multiplier_for_drop(drop) == expected, (drop, expected)


def test_ladder_is_distinct_from_afternoon():
    # Morning tops out at 3x, afternoon at 5x. An 8% drop must differ.
    from near_close_dca import StrategyConfig, graded_buy_multiplier

    morning = MorningDCAExecutor(MorningConfig())
    assert morning.multiplier_for_drop(0.08) == 3.0
    assert graded_buy_multiplier(0.08, StrategyConfig()) == 5.0
    assert MORNING_SIZING_TIERS != StrategyConfig().sizing_tiers


def test_signal_amounts_and_actions():
    cfg = MorningConfig(tickers=("UP", "FLAT", "TINY", "SMALL", "BIG"), base_buy_amount_usd=3.0)
    drops = {"UP": -0.01, "FLAT": 0.0, "TINY": 0.0005, "SMALL": 0.03, "BIG": 0.10}
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    sigs = {s.ticker: s for s in ex.evaluate(_now(), data)}

    assert sigs["UP"].action == Action.SKIP  # price above prior close
    assert sigs["UP"].multiplier == 0.0
    assert sigs["FLAT"].action == Action.SKIP  # exactly at prior close, no weakness
    assert sigs["TINY"].action == Action.NO_ACTION  # below prior close but under 0.1% min drop
    assert sigs["TINY"].multiplier == 0.0
    assert sigs["SMALL"].action == Action.BUY
    assert sigs["SMALL"].multiplier == 1.5
    assert sigs["SMALL"].hypothetical_amount_usd == 4.5
    assert sigs["BIG"].multiplier == 3.0
    assert sigs["BIG"].hypothetical_amount_usd == 9.0


class FixedQuoteProvider(FixedDropProvider):
    """FixedDropProvider that also exposes a live quote at a fixed timestamp."""

    def __init__(self, source, previous_close, drops, *, quote_price, quote_time):
        super().__init__(source, previous_close, drops)
        self._quote_price = quote_price
        self._quote_time = quote_time

    def get_quote(self, ticker, as_of):
        return (self._quote_price, self._quote_time)


def test_stale_quote_falls_back_to_completed_bar():
    # A quote captured after the 09:45 decision anchor must not size the run.
    decision_dt = _now()
    stale_quote_time = decision_dt + timedelta(minutes=5)
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=stale_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0  # completed-bar close, not the stale live quote
    assert any("falling back to completed bar" in w for w in sig.warnings)


def test_fresh_quote_at_or_before_decision_is_used():
    decision_dt = _now()
    fresh_quote_time = decision_dt - timedelta(minutes=1)
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=91.0, quote_time=fresh_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 91.0


def test_quote_older_than_completed_bar_anchor_falls_back_to_bar():
    # A quote at/before the decision anchor but *older* than the completed-bar
    # anchor (e.g. a stale prior-session or 09:31 quote) is not fresher than
    # the completed bar, so it must not be trusted for sizing.
    decision_dt = _now()
    stale_quote_time = decision_dt - timedelta(minutes=10)  # older than the 5-min bar anchor
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=stale_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0  # completed-bar close, not the stale quote
    assert any("older than the completed bar anchor" in w for w in sig.warnings)


def test_non_positive_quote_price_falls_back_to_completed_bar():
    # A fresh, well-timestamped quote of $0 or negative must not be trusted --
    # evaluate_signal would otherwise treat it as a 100%+ drop and size a
    # max-multiplier buy off a clearly-bad price.
    decision_dt = _now()
    fresh_quote_time = decision_dt - timedelta(minutes=1)
    for bad_price in (0.0, -5.0):
        data = FixedQuoteProvider(
            SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
            quote_price=bad_price, quote_time=fresh_quote_time,
        )
        ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
        sig = ex.evaluate(decision_dt, data)[0]
        assert sig.current_price == 90.0  # completed-bar close, not the bad quote
        assert any("is not positive" in w for w in sig.warnings)


def test_non_positive_completed_bar_close_is_rejected():
    # A provider passing through a placeholder/bad completed bar with a
    # non-positive close (e.g. a 100%+ synthetic drop) must not be trusted as
    # the current price either -- evaluate_signal only checks
    # current_price is None, so an unrejected $0 close would read as a 100%
    # drop and size a max-multiplier BUY off a clearly-bad price.
    decision_dt = _now()
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 1.0})  # 100% drop -> close 0.0
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price is None
    assert sig.action == Action.NO_DATA
    assert any("is not positive" in w for w in sig.warnings)


def test_negative_previous_close_bar_rejected():
    # A provider-backed previous-close bar with a negative close must not be
    # trusted for sizing -- current_price=90 vs. previous_close=-100 would
    # otherwise compute a nonsensical 190% "drop" and size a 3x BUY.
    decision_dt = _now()

    class NegativePrevCloseProvider(FixedDropProvider):
        def get_previous_close_bar(self, ticker, as_of):
            ts = datetime.combine(
                as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
            )
            return Bar(timestamp=ts, open=-100.0, high=-100.0, low=-100.0, close=-100.0, volume=0.0)

    data = NegativePrevCloseProvider(SourceName.ROBINHOOD, 90.0, {"BIG": 0.0})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert sig.previous_close is None
    assert any("non-positive" in w for w in sig.warnings)


def test_untimestamped_quote_falls_back_to_completed_bar():
    # A quote with no usable timestamp cannot be proven to be at/before the
    # decision anchor, so it must not be trusted for sizing.
    decision_dt = _now()
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=None,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0
    assert any("no usable timestamp" in w for w in sig.warnings)


def test_future_previous_close_bar_rejected():
    # A previous-close lookup that returns a bar at/after the decision anchor
    # (e.g. same-day 16:00 close from a live-quote-backed provider) must be
    # rejected rather than used to compute the morning drop.
    decision_dt = _now()

    class FuturePrevCloseProvider(FixedDropProvider):
        def get_previous_close_bar(self, ticker, as_of):
            p = self._prev
            return Bar(timestamp=as_of, open=p, high=p, low=p, close=p, volume=0.0)

    data = FuturePrevCloseProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert any("rejecting" in w for w in sig.warnings)


def test_in_progress_bar_excluded_from_sizing():
    # The 09:45-09:50 bar is not yet complete at the 09:45 decision anchor; a
    # provider that only supplies that bar (no completed 09:40 bar) must yield
    # NO_DATA rather than sizing off the in-progress bar.
    decision_dt = _now()

    class InProgressBarProvider(FixedDropProvider):
        def get_intraday_bars(self, ticker, start, end, interval_minutes):
            price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
            return [Bar(timestamp=end, open=price, high=price, low=price, close=price, volume=100.0)]

    data = InProgressBarProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert any("missing completed intraday bar" in w for w in sig.warnings)


# --- Separate ledger namespace ----------------------------------------------
def test_ledger_namespace_and_caps():
    ledger = MorningLedger(daily_cap_usd=10.0, weekly_cap_usd=20.0)
    day = date(2026, 7, 8)
    assert ledger.namespace == "morning"
    ledger.record_hypothetical(day, 6.0)
    ledger.record_hypothetical(day, 6.0)
    assert ledger.day_planned(day) == 12.0
    assert ledger.over_daily_cap(day) is True
    assert ledger.remaining_daily(day) == 0.0
    assert ledger.week_planned(day) == 12.0
    assert ledger.over_weekly_cap(day) is False


def test_morning_ledger_does_not_touch_afternoon_state(tmp_path):
    from near_close_dca import StrategyState

    afternoon = StrategyState()
    day = date(2026, 7, 8)
    cfg = MorningConfig(
        tickers=("SMALL", "BIG"),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03, "BIG": 0.10})
    ex.run(_now(), data)

    # Afternoon ledger is untouched: no notional, no buys recorded.
    assert afternoon.day_notional(day) == 0.0
    assert afternoon.week_notional(day) == 0.0
    assert afternoon.buys_by_ticker_by_day == {}
    # Morning ledger recorded the hypothetical spend.
    assert ex.ledger.day_planned(day) > 0.0


def test_over_cap_emits_warning(tmp_path):
    cfg = MorningConfig(
        tickers=tuple(f"T{i}" for i in range(5)),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=5.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    drops = {f"T{i}": 0.10 for i in range(5)}  # each 3x -> $9
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    res = ex.run(_now(), data)
    assert any("exceeds morning daily cap" in w for w in res["artifacts"]["warnings"])


def test_build_artifacts_does_not_double_count_ledger(tmp_path):
    # build_artifacts must be side-effect-free: calling it more than once (e.g.
    # inspecting artifacts before writing) must not re-record spend against the
    # ledger. Only run()'s single record_buy_candidates() call should count.
    cfg = MorningConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    now = _now()
    day = now.astimezone(EASTERN).date()

    signals = ex.evaluate(now, data)
    ex.build_artifacts(now, signals, "robinhood")
    ex.build_artifacts(now, signals, "robinhood")
    assert ex.ledger.day_planned(day) == 0.0  # build_artifacts alone never mutates the ledger

    ex.run(now, data)
    assert ex.ledger.day_planned(day) == 9.0  # exactly one run recorded once


def test_scheduled_run_skips_artifacts_on_market_holiday(tmp_path):
    from near_close_dca import MARKET_HOLIDAYS

    holiday = next(iter(MARKET_HOLIDAYS))
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data)

    assert res["artifacts"]["status"] == "blocked"
    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.exists()
    assert json.loads(run_state_path.read_text())["status"] == "blocked"
    # The scheduled pointer must never advance to a non-trading-day placeholder.
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()


def test_scheduled_run_skips_artifacts_on_future_holiday_beyond_afternoon_window(tmp_path):
    # near_close_dca.MARKET_HOLIDAYS is hand-maintained and scoped to the
    # afternoon live-test window (2026-06-29 through 2026-08-28), so it does
    # NOT include Labor Day 2026-09-07. The morning stream runs indefinitely
    # with no end date, so it must catch this via the computed nyse_holidays()
    # calendar instead of the narrower afternoon list.
    holiday = date(2026, 9, 7)  # Labor Day: 1st Monday of September
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data)

    assert res["artifacts"]["status"] == "blocked"
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()


def test_manual_run_ignores_market_holiday_gate(tmp_path):
    from near_close_dca import MARKET_HOLIDAYS

    holiday = next(iter(MARKET_HOLIDAYS))
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data, manual=True)

    assert "status" not in res["artifacts"] or res["artifacts"].get("status") != "blocked"
    assert res["artifacts"]["signal_rows"]


# --- Run-state path + classification ----------------------------------------
def test_run_state_path_and_classification(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data)

    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.parent.name == "20260708_0945ET"
    assert run_state_path.exists()

    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "morning_dca_dry_run"
    assert rc["stream"] == "morning"
    assert rc["scheduled_for_et"] == "09:45"
    assert rc["manual_run"] is False
    assert rc["state_schema_version"] == "run-classification-v1"
    assert rc["run_id"] == "morning_20260708_0945ET"
    assert rc["valid_afternoon_evidence"] is False
    assert rc["cron_id"] == MORNING_CRON_ID
    assert rc["supersedes_cron_id"] is None

    # Dry-run markers present; no order fields.
    art = res["artifacts"]
    assert art["mode"] == "dry_run"
    assert art["trades"] is False
    for row in art["signal_rows"]:
        assert "order_id" not in row
        assert set(
            ["ticker", "current_price", "previous_close", "drop_pct", "multiplier",
             "hypothetical_amount_usd", "source", "warnings"]
        ).issubset(row.keys())
    assert art["data_source"] == SourceName.ROBINHOOD.value


def test_manual_run_id_and_no_pointer(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data, manual=True)
    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["run_id"].startswith("manual_morning_")
    # Manual runs never write the latest-morning pointer and must not overwrite the scheduled folder.
    assert "latest_morning_run" not in res["paths"]
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()
    run_state_path = Path(res["paths"]["run_state"])
    assert "manual_runs" in run_state_path.parts
    assert rc["run_id"] in run_state_path.parts


def test_manual_runs_at_same_timestamp_do_not_collide(tmp_path):
    # Two manual replays sharing the same decision timestamp (down to the
    # second) must get distinct run_ids/paths so the second never overwrites
    # the first's artifact.
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()

    res1 = ex.run(now, data, manual=True)
    res2 = ex.run(now, data, manual=True)

    run_id_1 = res1["artifacts"]["run_classification"]["run_id"]
    run_id_2 = res2["artifacts"]["run_classification"]["run_id"]
    assert run_id_1 != run_id_2
    assert Path(res1["paths"]["run_state"]) != Path(res2["paths"]["run_state"])
    assert Path(res1["paths"]["run_state"]).exists()
    assert Path(res2["paths"]["run_state"]).exists()


def test_classification_matches_morning_schema(tmp_path):
    schema_path = Path(__file__).resolve().parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    required = set(schema["required"])
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    rc = ex.run_classification(_now(), manual=False)
    assert required.issubset(rc.keys())
    # Schema consts we hardcode must line up.
    assert rc["run_type"] == schema["properties"]["run_type"]["const"]
    assert rc["stream"] == schema["properties"]["stream"]["const"]


# --- No interaction with afternoon scheduled evidence -----------------------
def test_does_not_write_afternoon_artifacts(tmp_path):
    afternoon_root = tmp_path / "ce16ba6b"
    (afternoon_root / "scheduled_runs" / "20260708_1520ET").mkdir(parents=True)
    latest = afternoon_root / "latest_scheduled_run.json"
    latest.write_text(json.dumps({"run_id": "scheduled_20260708_1520ET"}), encoding="utf-8")
    before = latest.read_text(encoding="utf-8")

    morning_root = tmp_path / "morning_dca"
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(morning_root))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data)

    # Afternoon pointer untouched; nothing written under the afternoon root.
    assert latest.read_text(encoding="utf-8") == before
    afternoon_files = [p for p in afternoon_root.rglob("run_state*.json")]
    assert afternoon_files == []
    # Everything the morning run wrote is under the morning root.
    for path in res["paths"].values():
        assert str(morning_root) in path


def test_default_root_is_separate_from_afternoon():
    assert "morning_dca" in DEFAULT_MORNING_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MORNING_STATE_ROOT


def test_default_tickers_mirror_active_universe():
    from near_close_dca import ACTIVE_TICKERS, EXCLUDED_TICKERS

    cfg = MorningConfig()
    assert tuple(cfg.tickers) == tuple(ACTIVE_TICKERS)
    assert cfg.excluded_tickers == frozenset(EXCLUDED_TICKERS)


def test_base_executor_is_abstract():
    base = DCAExecutor()
    for call in (
        lambda: base.sizing_config,
        lambda: base.run_state_dir(_now()),
        lambda: base.run_classification(_now()),
    ):
        try:
            call()
        except NotImplementedError:
            continue
        raise AssertionError("expected NotImplementedError from abstract base hook")


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
