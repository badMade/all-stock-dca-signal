"""PMStore: append-only markets, point-in-time reads, license gate, liquidity flag (ADR §6, §7, D4, D7, D9)."""

from __future__ import annotations

import dataclasses

import pytest

from .conftest import (
    INTL,
    NVDA_INTL_SLUG,
    NVDA_US_SLUG,
    US,
    FakeResponse,
    FakeSession,
    pm,
    quote,
    response_body,
    utc,
)

INTL_EVENT = "intl_gamma_event_next-ceo-of-nvidia.json"


def _intl_batch(config, clock):
    session = FakeSession([FakeResponse.from_fixture(INTL_EVENT)])
    src = pm("polymarket_intl").PolymarketIntlSource(session, config=config, clock=clock.now, sleep=clock.sleep)
    return src.fetch_event(NVDA_INTL_SLUG), session


def _market(**overrides):
    fields = {
        "venue": INTL,
        "market_id": "0xabc",
        "title": "Will X be the next CEO?",
        "rules_text": "Resolves Yes if X is announced.",
        "created_ts": utc(2026, 11, 10, 2),
        "close_ts": utc(2028, 12, 31, 23),
        "resolved_ts": None,
        "resolution": None,
        "status": "active",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [0.3, 0.7],
        "token_ids": ["1", "2"],
    }
    fields.update(overrides)
    return pm("models").MarketRecord(**fields)


def _store_ok_everywhere(config_dict, write_config):
    config_dict["sources"][US]["license_scope"] = "store_ok"
    return pm("config").load_config(write_config(config_dict))


def test_thin_market_stored_but_flagged(config, clock, store_factory):
    """Below $50K liquidity is stored with usable = false, never dropped (D4).

    Real numbers from the fixture: Tim Cook $78,152 (usable); Colette Kress $45,182 and the
    inactive placeholder 'Candidate S' $0 (stored, unusable).
    """
    batch, _ = _intl_batch(config, clock)
    store = store_factory(config)
    pm("source").run_ingestion(store, INTL, lambda: batch, clock=clock.now)

    raw = {m["groupItemTitle"]: m["conditionId"] for m in response_body(INTL_EVENT)["markets"]}
    stored = {q.market_id: q for q in store.quotes(venue=INTL) if q.outcome == "Yes"}

    assert set(stored) == set(raw.values()), "every market is stored, thin or not"
    assert stored[raw["Tim Cook"]].usable is True
    assert stored[raw["Colette Kress"]].usable is False
    assert stored[raw["Colette Kress"]].liquidity_usd == pytest.approx(45182.33698)
    assert stored[raw["Candidate S"]].usable is False


def test_thin_market_threshold_read_from_config(config_dict, write_config, clock):
    config_dict["liquidity_threshold_usd"] = 40000
    cfg = pm("config").load_config(write_config(config_dict))
    batch, _ = _intl_batch(cfg, clock)
    kress_id = next(m["conditionId"] for m in response_body(INTL_EVENT)["markets"]
                    if m["groupItemTitle"] == "Colette Kress")
    (kress,) = [q for q in batch.quotes if q.market_id == kress_id and q.outcome == "Yes"]
    assert kress.usable is True


def test_quote_rows_carry_ts_and_ingested_at_after_storage(config, clock, store_factory):
    batch, _ = _intl_batch(config, clock)
    store = store_factory(config)
    pm("source").run_ingestion(store, INTL, lambda: batch, clock=clock.now)
    rows = store.quotes(venue=INTL)
    assert rows
    assert all(r.ts_utc is not None and r.ingested_at is not None for r in rows)


def test_pit_query_excludes_future_markets(config, store_factory):
    store = store_factory(config)
    early = _market(market_id="0xearly", created_ts=utc(2026, 11, 1))
    late = _market(market_id="0xlate", created_ts=utc(2026, 11, 10, 2))
    store.write_markets(INTL, [early], run_id="r1", ingested_at=utc(2026, 11, 1, 1))
    # Ingested (wrongly) before its own creation time: created_ts alone must still exclude it.
    store.write_markets(INTL, [late], run_id="r2", ingested_at=utc(2026, 11, 5))

    seen = {m.market_id for m in store.markets_as_of(utc(2026, 11, 9))}
    assert seen == {"0xearly"}
    assert {m.market_id for m in store.markets_as_of(utc(2026, 11, 11))} == {"0xearly", "0xlate"}


def test_pit_query_never_uses_close_or_resolved_ts(config, store_factory):
    """A market resolved later was still known before it resolved (ADR §6 PIT rules)."""
    store = store_factory(config)
    m = _market(resolved_ts=utc(2026, 11, 12), resolution="Yes", status="resolved", close_ts=utc(2026, 11, 12))
    store.write_markets(INTL, [m], run_id="r1", ingested_at=utc(2026, 11, 10, 3))
    assert [x.market_id for x in store.markets_as_of(utc(2026, 11, 11))] == ["0xabc"]


def test_rules_hash_change_creates_new_version(config, store_factory):
    store = store_factory(config)
    v1 = _market(rules_text="Resolves Yes if X is announced.")
    store.write_markets(INTL, [v1], run_id="r1", ingested_at=utc(2026, 11, 10, 3))
    store.write_markets(INTL, [v1], run_id="r2", ingested_at=utc(2026, 11, 10, 4))
    assert [v.version for v in store.market_versions(INTL, "0xabc")] == [1], "same rules: no new version"

    v2 = dataclasses.replace(v1, rules_text="Resolves Yes if X is announced. Interim CEOs do not count.")
    store.write_markets(INTL, [v2], run_id="r3", ingested_at=utc(2026, 11, 11, 9))

    versions = store.market_versions(INTL, "0xabc")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].rules_hash != versions[1].rules_hash
    assert versions[0].rules_text == v1.rules_text, "version 1 is never rewritten"


def test_pit_query_uses_rules_version_as_of(config, store_factory):
    store = store_factory(config)
    v1 = _market(rules_text="old rules")
    store.write_markets(INTL, [v1], run_id="r1", ingested_at=utc(2026, 11, 10, 3))
    store.write_markets(INTL, [dataclasses.replace(v1, rules_text="clarified rules")], run_id="r2",
                        ingested_at=utc(2026, 11, 11, 9))

    (before,) = store.markets_as_of(utc(2026, 11, 11, 8, 59))
    (after,) = store.markets_as_of(utc(2026, 11, 11, 9))
    assert (before.version, before.rules_text) == (1, "old rules")
    assert (after.version, after.rules_text) == (2, "clarified rules")


@pytest.mark.parametrize("new_status", ["voided", "delisted", "resolved", "archived"])
def test_voided_and_delisted_markets_never_deleted(config, store_factory, new_status):
    store = store_factory(config)
    m = _market()
    store.write_markets(INTL, [m], run_id="r1", ingested_at=utc(2026, 11, 10, 3))
    store.write_quotes(INTL, [quote(market_id="0xabc")], run_id="r1")
    before_markets, before_quotes = store.table_count("pm_markets"), store.table_count("pm_quotes")

    store.write_markets(INTL, [dataclasses.replace(m, status=new_status)], run_id="r2",
                        ingested_at=utc(2026, 11, 12))

    assert store.table_count("pm_markets") >= before_markets
    assert store.table_count("pm_quotes") == before_quotes
    (latest,) = store.markets_as_of(utc(2026, 11, 13))
    assert latest.status == new_status
    (earlier,) = store.markets_as_of(utc(2026, 11, 11))
    assert earlier.status == "active"
    assert not any(hasattr(store, name) for name in ("delete_market", "delete_markets", "purge"))


def test_polymarket_venue_labels_never_merge(config_dict, write_config, clock, store_factory):
    """Intl and US Kress rows stay apart under distinct venue labels (ADR §3)."""
    cfg = _store_ok_everywhere(config_dict, write_config)
    intl_batch, _ = _intl_batch(cfg, clock)
    us_session = FakeSession([FakeResponse.from_fixture("us_gateway_event_nvidiaceo-2028-12-31.json")])
    us_batch = pm("polymarket_us").PolymarketUSSource(
        us_session, config=cfg, clock=clock.now, sleep=clock.sleep
    ).fetch_event(NVDA_US_SLUG)
    store = store_factory(cfg)
    pm("source").run_ingestion(store, INTL, lambda: intl_batch, clock=clock.now)
    pm("source").run_ingestion(store, US, lambda: us_batch, clock=clock.now)

    assert pm("polymarket_intl").PolymarketIntlSource.venue == INTL
    assert pm("polymarket_us").PolymarketUSSource.venue == US
    assert {q.venue for q in store.quotes(venue=INTL)} == {INTL}
    assert {q.venue for q in store.quotes(venue=US)} == {US}
    assert len(store.quotes()) == len(store.quotes(venue=INTL)) + len(store.quotes(venue=US))
    assert {m.venue for m in store.markets_as_of(utc(2026, 11, 14))} == {INTL, US}


def test_venue_label_mismatch_is_rejected(config, store_factory):
    """A row whose venue disagrees with the venue it is written under is refused, not relabelled."""
    store = store_factory(config)
    with pytest.raises(ValueError):
        store.write_quotes(INTL, [quote(venue=US)], run_id="r1")
    assert store.table_count("pm_quotes") == 0


@pytest.mark.parametrize("scope", ["live_only", "manual_only"])
def test_non_store_ok_source_never_persists(config_dict, write_config, clock, store_factory, scope):
    """D7: fetch and parse may run; nothing reaches pm_* tables while the source is not store_ok."""
    config_dict["sources"][INTL]["license_scope"] = scope
    cfg = pm("config").load_config(write_config(config_dict))
    batch, _ = _intl_batch(cfg, clock)
    assert batch.quotes, "parsing still works under any scope"
    store = store_factory(cfg)

    result = pm("source").run_ingestion(store, INTL, lambda: batch, clock=clock.now)

    for table in ("pm_markets", "pm_quotes", "pm_trades", "pm_orderbook_snapshots"):
        assert store.table_count(table) == 0, table
    assert result.status == "not_persisted"
    (run,) = store.ingestion_runs()
    assert run["status"] == "not_persisted" and scope in run["reason"]
    with pytest.raises(pm("store").LicenseScopeError):
        store.write_quotes(INTL, [quote()], run_id="direct")
    assert store.table_count("pm_quotes") == 0


def test_us_is_live_only_by_default(config, clock, store_factory):
    """Polymarket US starts live_only (ADR §3), so a US run writes zero rows."""
    session = FakeSession([FakeResponse.from_fixture("us_gateway_event_nvidiaceo-2028-12-31.json")])
    batch = pm("polymarket_us").PolymarketUSSource(
        session, config=config, clock=clock.now, sleep=clock.sleep
    ).fetch_event(NVDA_US_SLUG)
    store = store_factory(config)
    pm("source").run_ingestion(store, US, lambda: batch, clock=clock.now)
    assert store.table_count("pm_quotes") == 0 and store.table_count("pm_markets") == 0


def test_unknown_license_scope_is_a_config_error(config_dict, write_config):
    config_dict["sources"][INTL]["license_scope"] = "probably_fine"
    with pytest.raises(pm("config").ConfigError):
        pm("config").load_config(write_config(config_dict))
