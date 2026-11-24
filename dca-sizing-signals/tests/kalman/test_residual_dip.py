"""residual_dip_shadow: point-in-time use, labels, and every fail-closed reason (ADR D1, D4, D6)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from kalman_signals.residual_dip import (
    LABEL_CLUSTER,
    LABEL_IDIOSYNCRATIC,
    LABEL_MARKET,
    LABEL_NONE,
    ResidualDipConfig,
    ResidualDipInputs,
    residual_dip_shadow,
)
from tests.kalman._synth import MKT, SMALL, generate, replace_bar

BETAS = {"S1": 1.4, "S2": 0.6, "S3": 1.0}
SESSIONS, ROWS = generate(320, BETAS)
AS_OF = SESSIONS[-1]


def _run(rows=ROWS, as_of=AS_OF, config=SMALL, **kwargs):
    return residual_dip_shadow(ResidualDipInputs(close_rows=rows, **kwargs), as_of, config)


def _by_ticker(result):
    return {r.payload["ticker"]: r for r in result.records}


def test_happy_path_scores_every_stock():
    result = _run()
    assert result.ok and result.reason == "ok"
    records = _by_ticker(result)
    assert sorted(records) == ["S1", "S2", "S3"]
    for ticker, record in records.items():
        market = record.payload["market"]
        assert record.as_of == record.inputs_known_at == AS_OF
        assert market["beta"] == pytest.approx(BETAS[ticker], abs=0.35)
        assert market["steps_since_init"] >= SMALL.burn_in
        assert market["params_version"].startswith("kalman_beta/v1")
        assert all(math.isfinite(v) for v in market.values() if isinstance(v, float))
        assert record.payload["cluster"] is None and record.payload["cluster_reason"] == "no_cluster_peers"


def test_overnight_and_intraday_parts_add_up_to_the_total():
    market = _by_ticker(_run())["S1"].payload["market"]
    assert market["z_overnight"] + market["z_intraday"] == pytest.approx(market["z_total"], abs=1e-9)


def test_future_rows_change_nothing():
    later = [{**r, "date": r["date"] + timedelta(days=1000), "close": r["close"] * 3} for r in ROWS]
    base, with_future = _run(), _run(rows=ROWS + later)
    assert [r.payload for r in base.records] == [r.payload for r in with_future.records]


def test_a_non_final_session_bar_is_never_used():
    rows = [{**r, "final": False} if r["date"] == AS_OF else r for r in ROWS]
    result = _run(rows=rows)
    assert not result.ok and result.reason == "market_missing_session"


def test_idiosyncratic_dip_label():
    rows = replace_bar(ROWS, "S2", AS_OF, open_ret=0.0, intraday_ret=-0.09)
    rows = replace_bar(rows, MKT, AS_OF, open_ret=0.0, intraday_ret=0.0)
    record = _by_ticker(_run(rows=rows))["S2"]
    assert record.payload["label"] == LABEL_IDIOSYNCRATIC
    assert record.payload["market"]["z_intraday"] <= -2.0


def test_market_selloff_label_wins_over_a_residual_dip():
    rows = replace_bar(ROWS, MKT, AS_OF, open_ret=0.0, intraday_ret=-0.04)
    rows = replace_bar(rows, "S2", AS_OF, open_ret=0.0, intraday_ret=-0.12)
    labels = {t: r.payload["label"] for t, r in _by_ticker(_run(rows=rows)).items()}
    assert set(labels.values()) == {LABEL_MARKET}


def test_cluster_selloff_label():
    sessions, rows = generate(320, {"C1": 1.0, "C2": 1.0, "C3": 1.0, "S9": 1.0}, seed=5,
                              common={"C1": "g", "C2": "g", "C3": "g"}, common_sd=0.012)
    as_of = sessions[-1]
    rows = replace_bar(rows, MKT, as_of, open_ret=0.0, intraday_ret=0.0)
    for t in ("C2", "C3"):
        rows = replace_bar(rows, t, as_of, open_ret=0.0, intraday_ret=-0.06)
    rows = replace_bar(rows, "C1", as_of, open_ret=0.0, intraday_ret=-0.06)
    clusters = {t: ["C1", "C2", "C3"] for t in ("C1", "C2", "C3")}
    record = _by_ticker(_run(rows=rows, as_of=as_of, cluster_members=clusters))["C1"]
    assert record.payload["cluster"] is not None
    assert record.payload["label"] == LABEL_CLUSTER


def test_cluster_factor_excludes_the_ticker_itself():
    clusters = {"S1": ["S1"]}
    record = _by_ticker(_run(cluster_members=clusters))["S1"]
    assert record.payload["cluster"] is None and record.payload["cluster_reason"] == "no_cluster_peers"


def test_quiet_session_is_labelled_none_or_a_real_label():
    rows = replace_bar(ROWS, MKT, AS_OF, open_ret=0.0, intraday_ret=0.0)
    for t in BETAS:
        rows = replace_bar(rows, t, AS_OF, open_ret=0.0, intraday_ret=0.0)
    assert {r.payload["label"] for r in _run(rows=rows).records} == {LABEL_NONE}


def test_missing_opens_give_no_label():
    rows = [{**r, "open": None} if r["date"] == AS_OF else r for r in ROWS]
    for record in _run(rows=rows).records:
        assert record.payload["label"] is None and record.payload["label_reason"] == "no_open"
        assert record.payload["market"]["z_intraday"] is None
        assert math.isfinite(record.payload["market"]["z_total"])


@pytest.mark.parametrize("kwargs, reason", [
    ({"non_equity": frozenset({"S1"})}, "not_applicable"),
    ({"earnings_window": frozenset({"S1"})}, "earnings_window"),
])
def test_caller_gates(kwargs, reason):
    result = _run(**kwargs)
    assert result.skipped["S1"] == reason and "S1" not in _by_ticker(result)


def test_missing_session_bar_and_missing_prior_close():
    rows = [r for r in ROWS if not (r["ticker"] == "S1" and r["date"] == AS_OF)]
    rows = [r for r in rows if not (r["ticker"] == "S2" and r["date"] == SESSIONS[-2])]
    result = _run(rows=rows)
    assert result.skipped == {"S1": "no_session_price", "S2": "no_prior_close"}


def test_short_history():
    # 59 observed pairs < max(min_train=80, 2 * burn_in=80). Because a fit needs at least
    # twice the burn-in, a fitted model always has burn_in filtered steps; the "burn_in"
    # reason is a defensive check that only bites if those rules ever drift apart.
    late = SESSIONS[-60]
    rows = [r for r in ROWS if r["ticker"] != "S1" or r["date"] >= late]
    assert _run(rows=rows).skipped["S1"] == "insufficient_history"


def test_uncertain_beta_is_refused():
    result = _run(config=replace(SMALL, max_beta_sd=1e-6))
    assert set(result.skipped.values()) == {"beta_uncertain"}


def test_stale_market_fails_the_run():
    rows = [r for r in ROWS if r["date"] < SESSIONS[-1]]
    late = AS_OF + timedelta(days=10)
    rows += [{**r, "date": late} for r in ROWS if r["date"] == AS_OF]
    result = _run(rows=rows, as_of=late)
    assert not result.ok and result.reason == "stale_market"


@pytest.mark.parametrize("bad", [
    {"ticker": "S1", "date": "2023-02-01", "close": -1.0},
    {"ticker": "S1", "date": "2023-02-01", "close": 10.0, "open": float("nan")},
    {"ticker": "S1", "date": "not-a-date", "close": 10.0},
    {"ticker": "S1", "date": "2023-02-01", "close": "10"},
    {"ticker": "S1", "date": "2023-02-01", "close": 10.0, "final": "yes"},
])
def test_a_malformed_row_fails_the_run(bad):
    result = _run(rows=ROWS + [bad])
    assert not result.ok and result.reason.startswith("invalid_input")


def test_duplicate_row_fails_the_run():
    assert _run(rows=ROWS + [ROWS[-1]]).reason.startswith("invalid_input")


def test_interpolated_rows_are_ignored_even_if_junk():
    junk = {"ticker": "S1", "date": SESSIONS[5], "close": None, "interpolated": True}
    assert [r.payload for r in _run(rows=ROWS + [junk]).records] == [r.payload for r in _run().records]


def test_default_config_runs_on_a_long_history():
    sessions, rows = generate(560, {"S1": 1.2})
    result = residual_dip_shadow(ResidualDipInputs(rows), sessions[-1], ResidualDipConfig(market_ticker=MKT))
    assert result.ok and len(result.records) == 1
    assert result.records[0].payload["history_sessions"] == 504
