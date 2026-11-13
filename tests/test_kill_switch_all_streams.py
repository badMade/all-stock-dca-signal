"""The 7-day deployment kill switch, proven on every executor stream.

``tests/test_morning_dca.py`` already pins ``apply_deployment_risk_guard`` on the morning executor. The other
three streams (market-open, midday, intraday) are thin subclasses of ``MorningDCAExecutor``, and the production
readiness review (``PRODUCTION_READINESS.md``, gap 1) records that the kill switch has structurally never been
armed on these streams' own ``run()`` path: ``MorningConfig.as_strategy_config()`` omits the three fields
``near_close_dca.kill_switch_engaged`` reads. Nothing proved that the *opt-in* arming works for each subclass,
and nothing would notice if a subclass override silently broke it. This file does both halves, per stream:

* the guard CAN be armed, engages above the threshold, and caps the ladder at 1x (the capability a producer
  such as ``scripts/stream_candidates`` relies on);
* the guard is NOT armed by default (a characterization test). If someone later wires the guard into a stream's
  live ``run()`` path, ``test_default_executor_carries_no_deployment_figure`` fails on purpose: that is a
  live-path behaviour change that needs its own review and acceptance run (CLAUDE.md "Consolidating is a
  decision, not a cleanup"), and the failure is the prompt to update this file deliberately.

No broker, no network, and no order is placed; every executor here is constructed dry.
"""

from __future__ import annotations

import pytest

from intraday_dca import IntradayConfig, IntradayDCAExecutor
from market_open_dca import MarketOpenConfig, MarketOpenDCAExecutor
from midday_dca import MiddayConfig, MiddayDCAExecutor
from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor
from near_close_dca import kill_switch_engaged

_STREAMS = [
    pytest.param(lambda: MorningDCAExecutor(MorningConfig(), mode=MORNING_LIVE_MODE), id="morning"),
    pytest.param(lambda: MarketOpenDCAExecutor(MarketOpenConfig()), id="market_open"),
    pytest.param(lambda: MiddayDCAExecutor(MiddayConfig()), id="midday"),
    pytest.param(lambda: IntradayDCAExecutor(IntradayConfig()), id="intraday"),
]

# 40% of a $3,600 monthly allocation is $1,440, so $1,500 is over and $100 is well under.
_ALLOCATION = 3600.0
_FRACTION = 0.40


def _deepest_drop(executor) -> float:
    return max(threshold for threshold, _multiplier in executor.sizing_config.sizing_tiers)


@pytest.mark.parametrize("make", _STREAMS)
def test_default_executor_carries_no_deployment_figure(make):
    """Characterization of the known gap: an unarmed executor never trips the switch (see module docstring)."""
    executor = make()
    assert executor.sizing_config.deployed_last_7d_usd == 0.0
    assert kill_switch_engaged(executor.sizing_config) is False


@pytest.mark.parametrize("make", _STREAMS)
def test_guard_engages_above_threshold_and_caps_the_ladder(make):
    executor = make()
    deep_drop = _deepest_drop(executor)
    uncapped = executor.multiplier_for_drop(deep_drop)
    assert uncapped > 1.0, "the fixture must have a ladder that actually scales up on a deep drop"

    engaged = executor.apply_deployment_risk_guard(
        deployed_last_7d_usd=1500.0,
        monthly_cash_allocation_usd=_ALLOCATION,
        kill_switch_7d_deploy_fraction=_FRACTION,
    )

    assert engaged is True
    assert kill_switch_engaged(executor.sizing_config) is True
    assert executor.multiplier_for_drop(deep_drop) == 1.0


@pytest.mark.parametrize("make", _STREAMS)
def test_guard_below_threshold_leaves_sizing_alone(make):
    executor = make()
    deep_drop = _deepest_drop(executor)
    uncapped = executor.multiplier_for_drop(deep_drop)

    engaged = executor.apply_deployment_risk_guard(
        deployed_last_7d_usd=100.0,
        monthly_cash_allocation_usd=_ALLOCATION,
        kill_switch_7d_deploy_fraction=_FRACTION,
    )

    assert engaged is False
    assert executor.multiplier_for_drop(deep_drop) == uncapped


@pytest.mark.parametrize("make", _STREAMS)
@pytest.mark.parametrize(
    "bad",
    [
        {"deployed_last_7d_usd": float("nan")},
        {"deployed_last_7d_usd": float("inf")},
        {"deployed_last_7d_usd": -1.0},
        {"monthly_cash_allocation_usd": float("nan")},
        {"monthly_cash_allocation_usd": -1.0},
        {"kill_switch_7d_deploy_fraction": float("nan")},
        {"kill_switch_7d_deploy_fraction": 1.5},
        {"deployed_last_7d_usd": True},
    ],
    ids=lambda case: next(iter(case)) + "=" + repr(next(iter(case.values()))),
)
def test_guard_rejects_values_that_would_silently_disable_it(make, bad):
    """A NaN compares False against every threshold and would DISABLE the guard; each stream must refuse it
    and leave sizing untouched (a rejected call arms nothing)."""
    executor = make()
    before = executor.sizing_config
    kwargs = {
        "deployed_last_7d_usd": 1500.0,
        "monthly_cash_allocation_usd": _ALLOCATION,
        "kill_switch_7d_deploy_fraction": _FRACTION,
    }
    kwargs.update(bad)

    with pytest.raises(ValueError):
        executor.apply_deployment_risk_guard(**kwargs)

    assert executor.sizing_config == before
