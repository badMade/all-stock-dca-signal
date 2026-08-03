"""Self-tests for the market-open live-execution stream config loader.

Focused on the fail-closed config-loading fix (Codex review: "Keep disabled
streams disabled when config loading fails") rather than full coverage of
market_open_dca.py, which reuses morning_dca.MorningDCAExecutor's already
heavily-tested pipeline.
"""

import os

os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")

import json
from datetime import date, datetime, time, timedelta

from market_open_dca import (
    MarketOpenConfig,
    MarketOpenDCAExecutor,
    default_market_open_prior_allocation_roots,
    load_market_open_config,
)
from morning_dca import EASTERN, EXEC_AUTO_BUY_ELIGIBLE, Bar
from near_close_dca import Action, SourceName, load_prior_same_day_allocations


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


def test_explicit_true_in_a_readable_config_still_overrides_the_fail_closed_default(tmp_path):
    # The fail-closed default must not prevent a real, readable config from
    # explicitly re-enabling live placement once it has been vetted.
    (tmp_path / "market_open_dca.json").write_text(
        json.dumps({"live_execution_enabled": True}), encoding="utf-8"
    )
    cfg = load_market_open_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is True


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
    assert any(str(r).rstrip("/").endswith("market_open_dca") for r in roots)


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
