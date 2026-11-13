"""Composite score (D13): log-odds pool across venues, weighted by liquidity and spread, no extremizing.

The pool's inputs here are constructed numbers, not fixtures: the function under test is pure
arithmetic, and the captured NVIDIA quotes are all below the $50K threshold except one, so they
cannot exercise a two-venue pool.
"""

from __future__ import annotations

import math

import pytest

from .conftest import INTL, US, pm, quote, utc


def logit(p):
    return math.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + math.exp(-x))


def _inp(prob, liquidity_usd, spread=0.01, usable=True):
    return pm("composite").CompositeInput(prob=prob, liquidity_usd=liquidity_usd, spread=spread, usable=usable)


def test_composite_logodds_pool_weighted_by_liquidity():
    """Equal spreads: weights are proportional to liquidity. 100k vs 300k -> 0.25 / 0.75."""
    pooled = pm("composite").pool_logodds([_inp(0.20, 100_000), _inp(0.40, 300_000)])
    expected = inv_logit(0.25 * logit(0.20) + 0.75 * logit(0.40))
    assert pooled == pytest.approx(expected, abs=1e-12)
    assert pooled != pytest.approx((0.20 + 0.40) / 2), "log-odds, not a plain average"


def test_composite_narrower_spread_gets_more_weight():
    pool = pm("composite").pool_logodds
    tight_on_low = pool([_inp(0.20, 100_000, spread=0.005), _inp(0.40, 100_000, spread=0.05)])
    tight_on_high = pool([_inp(0.20, 100_000, spread=0.05), _inp(0.40, 100_000, spread=0.005)])
    assert tight_on_low < inv_logit((logit(0.20) + logit(0.40)) / 2) < tight_on_high


@pytest.mark.parametrize(
    "inputs",
    [
        [(0.02, 60_000), (0.03, 900_000)],
        [(0.97, 60_000), (0.99, 70_000), (0.95, 2_000_000)],
        [(0.50, 60_000), (0.50, 60_000)],
        [(0.10, 60_000), (0.90, 60_000)],
    ],
)
def test_composite_no_extremizing(inputs):
    """The pool never lands outside [min input, max input]: agreeing venues are not 'more sure'."""
    probs = [p for p, _ in inputs]
    pooled = pm("composite").pool_logodds([_inp(p, liq) for p, liq in inputs])
    assert min(probs) - 1e-12 <= pooled <= max(probs) + 1e-12
    if len(set(probs)) == 1:
        assert pooled == pytest.approx(probs[0])


def test_composite_excludes_unusable_quotes():
    pool = pm("composite").pool_logodds
    with_thin = pool([_inp(0.20, 100_000), _inp(0.90, 10_000, usable=False)])
    assert with_thin == pytest.approx(0.20)
    assert pool([_inp(0.90, 10_000, usable=False)]) is None, "no usable input -> no composite, not a guess"


# --- compute_composite over the store ------------------------------------------------------


@pytest.fixture
def cfg(config_dict, write_config):
    config_dict["sources"][US]["license_scope"] = "store_ok"
    return pm("config").load_config(write_config(config_dict))


def _seed(store, *, links, quotes):
    store.write_event("nvda-next-ceo-kress", title="Colette Kress named next NVIDIA CEO", kind="corporate",
                      created_at=utc(2026, 11, 10))
    for link in links:
        store.write_event_link("nvda-next-ceo-kress", **link)
    for venue in {q.venue for q in quotes}:
        store.write_quotes(venue, [q for q in quotes if q.venue == venue], run_id=f"seed-{venue}")


LINKS_VALID_FROM = utc(2026, 11, 10)


def _link(venue, market_id, *, relation="same", valid_from=LINKS_VALID_FROM, outcome="Yes"):
    return {"venue": venue, "market_id": market_id, "outcome": outcome, "relation": relation,
            "valid_from": valid_from, "valid_to": None, "reviewed": True}


def test_composite_pit_uses_only_past_quotes_and_links(cfg, store_factory):
    store = store_factory(cfg)
    t = utc(2026, 11, 12, 18)
    _seed(
        store,
        links=[_link(INTL, "0xkress"), _link(US, "colkre", valid_from=utc(2026, 11, 12, 19))],  # US link not yet valid
        quotes=[
            quote(venue=INTL, market_id="0xkress", ts_utc=utc(2026, 11, 12, 17), mid=0.20,
                  ingested_at=utc(2026, 11, 12, 17)),
            quote(venue=INTL, market_id="0xkress", ts_utc=utc(2026, 11, 12, 18, 30), mid=0.70,  # after t
                  ingested_at=utc(2026, 11, 12, 18, 30)),
            quote(venue=US, market_id="colkre", ts_utc=utc(2026, 11, 12, 17), mid=0.60,
                  ingested_at=utc(2026, 11, 12, 17)),
        ],
    )

    (row,) = pm("composite").compute_composite(store, "nvda-next-ceo-kress", at=t, computed_at=utc(2026, 11, 13))

    assert row.prob == pytest.approx(0.20)
    assert row.n_venues == 1
    assert row.ts_utc == t


def test_composite_never_overwrites_raw_quotes(cfg, store_factory):
    store = store_factory(cfg)
    _seed(
        store,
        links=[_link(INTL, "0xkress"), _link(US, "colkre")],
        quotes=[quote(venue=INTL, market_id="0xkress", mid=0.20), quote(venue=US, market_id="colkre", mid=0.40)],
    )
    before = sorted(map(repr, store.quotes()))

    rows = pm("composite").compute_composite(store, "nvda-next-ceo-kress", at=utc(2026, 11, 13),
                                             computed_at=utc(2026, 11, 13, 1))

    assert sorted(map(repr, store.quotes())) == before
    assert store.table_count("pm_quotes") == 2
    assert store.table_count("pm_composite") == len(rows) == 1
    assert rows[0].method_version, "every composite row records the method version that made it"


def test_event_links_inverse_relation_flips_probability(cfg, store_factory):
    """An 'inverse' link contributes 1 - p: a venue quoting 'Kress NOT named' at 0.70 means 0.30."""
    store = store_factory(cfg)
    _seed(
        store,
        links=[_link(INTL, "0xkress"), _link(US, "notkress", relation="inverse")],
        quotes=[quote(venue=INTL, market_id="0xkress", mid=0.30), quote(venue=US, market_id="notkress", mid=0.70)],
    )

    (row,) = pm("composite").compute_composite(store, "nvda-next-ceo-kress", at=utc(2026, 11, 13),
                                               computed_at=utc(2026, 11, 13, 1))

    assert row.prob == pytest.approx(0.30)
    assert row.n_venues == 2


def test_composite_ignores_unreviewed_links(cfg, store_factory):
    store = store_factory(cfg)
    unreviewed = {**_link(US, "colkre"), "reviewed": False}
    _seed(
        store,
        links=[_link(INTL, "0xkress"), unreviewed],
        quotes=[quote(venue=INTL, market_id="0xkress", mid=0.20), quote(venue=US, market_id="colkre", mid=0.80)],
    )
    (row,) = pm("composite").compute_composite(store, "nvda-next-ceo-kress", at=utc(2026, 11, 13),
                                               computed_at=utc(2026, 11, 13, 1))
    assert row.prob == pytest.approx(0.20)
