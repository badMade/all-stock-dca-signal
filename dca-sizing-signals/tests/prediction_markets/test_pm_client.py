"""Request shaping: the 20-token history cap, per-endpoint rate budgets, cadence (ADR §4, D3)."""

from __future__ import annotations

import pytest

from .conftest import INTL, US, FakeClock, FakeResponse, FakeSession, pm, response_body, utc

BATCH_FIXTURE = "intl_clob_batch_prices_history_2_tokens.json"


def _history_responder():
    """Answer each batch POST with the real captured points, keyed by whatever tokens were asked for."""
    real = list(response_body(BATCH_FIXTURE)["history"].values())

    def respond(method, url, params, body):
        assert method == "POST" and url == "https://clob.polymarket.com/batch-prices-history"
        return FakeResponse.json_body({"history": {tok: real[i % len(real)] for i, tok in enumerate(body["markets"])}})

    return respond


def _intl(session, cfg, clock):
    return pm("polymarket_intl").PolymarketIntlSource(session, config=cfg, clock=clock.now, sleep=clock.sleep)


@pytest.mark.parametrize("n_tokens", [1, 20, 21, 45])
def test_prices_history_batches_max_20_tokens(config, clock, n_tokens):
    """The venue rejects 21 ids with HTTP 400 (captured); the adapter must never send more than 20."""
    rejected = response_body("intl_clob_batch_prices_history_21_tokens_rejected.json")
    assert "maximum of 20" in rejected["error"], "fixture premise"

    tokens = [str(10**70 + i) for i in range(n_tokens)]
    session = FakeSession(_history_responder())

    batch = _intl(session, config, clock).fetch_price_history(tokens, interval="1d", fidelity=60)

    sent = [call.json["markets"] for call in session.calls]
    assert all(1 <= len(chunk) <= 20 for chunk in sent)
    assert [t for chunk in sent for t in chunk] == tokens, "every token once, in order"
    assert len(sent) == -(-n_tokens // 20)
    assert all(call.json["interval"] == "1d" and call.json["fidelity"] == 60 for call in session.calls)
    assert {q.market_id for q in batch.quotes} or n_tokens == 0


def test_prices_history_batch_cap_read_from_config(config_dict, write_config, clock):
    config_dict["sources"][INTL]["max_history_batch"] = 7
    cfg = pm("config").load_config(write_config(config_dict))
    session = FakeSession(_history_responder())
    _intl(session, cfg, clock).fetch_price_history([str(i) for i in range(15)], interval="1d", fidelity=60)
    assert [len(c.json["markets"]) for c in session.calls] == [7, 7, 1]


def test_prices_history_batch_cap_above_venue_limit_is_a_config_error(config_dict, write_config):
    config_dict["sources"][INTL]["max_history_batch"] = 21
    with pytest.raises(pm("config").ConfigError):
        pm("config").load_config(write_config(config_dict))


def _max_in_any_window(times, window_s=10.0):
    secs = sorted(t.timestamp() for t in times)
    best, lo = 0, 0
    for hi in range(len(secs)):
        while secs[hi] - secs[lo] >= window_s:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def test_request_budget_blocks_until_window_frees():
    clock = FakeClock(utc(2026, 11, 13, 3))
    budget = pm("throttle").RequestBudget({"gamma:/markets": 3}, clock=clock.now, sleep=clock.sleep, window_s=10.0)
    stamps = []
    for _ in range(7):
        budget.acquire("gamma:/markets")
        stamps.append(clock.now())
    assert _max_in_any_window(stamps) <= 3
    assert clock.sleeps, "over budget means waiting, not sending"


def test_client_respects_rate_limit_budget(config_dict, write_config):
    """Requests per 10 s stay under the configured per-endpoint budget (ADR §4 limits)."""
    config_dict["sources"][INTL]["budgets_per_10s"]["clob"] = {"*": 9000, "/batch-prices-history": 2}
    cfg = pm("config").load_config(write_config(config_dict))
    clock = FakeClock(utc(2026, 11, 13, 3))
    session = FakeSession(_history_responder(), clock=clock)

    _intl(session, cfg, clock).fetch_price_history([str(i) for i in range(100)], interval="1d", fidelity=60)

    assert len(session.calls) == 5
    assert _max_in_any_window([c.at for c in session.calls]) <= 2
    assert clock.sleeps


def test_endpoint_budget_is_tighter_than_host_budget(config_dict, write_config):
    """Gamma allows 4,000/10 s overall but only 300 on /markets; the tighter one binds."""
    cfg = pm("config").load_config(write_config(config_dict))
    clock = FakeClock(utc(2026, 11, 13, 3))
    budget = pm("throttle").RequestBudget.from_config(cfg, INTL, clock=clock.now, sleep=clock.sleep)
    assert budget.limit_for("gamma", "/markets") == 300
    assert budget.limit_for("gamma", "/events/slug/next-ceo-of-nvidia") == 4000
    assert budget.limit_for("data", "/v2/prices-history") == 200


def test_cadence_read_from_config(config_dict, write_config):
    config_dict["cadence"]["default_interval_s"] = 1234  # not the 3600 default: proves it is read
    cfg = pm("config").load_config(write_config(config_dict))
    cadence = pm("cadence")
    assert cadence.interval_for(cfg, INTL, "0xnot-watched") == 1234
    assert cadence.interval_for(cfg, US, "some-slug") == 1234
    assert cadence.captures_orderbook(cfg, INTL, "0xnot-watched") is False


def test_watchlist_cadence_overrides_default(config_dict, write_config):
    kress = response_body("intl_gamma_event_next-ceo-of-nvidia.json")["markets"][0]["conditionId"]
    config_dict["cadence"]["watchlist_interval_s"] = 120
    config_dict["watchlist"] = [{"venue": INTL, "market_id": kress}]
    cfg = pm("config").load_config(write_config(config_dict))
    cadence = pm("cadence")

    assert cadence.interval_for(cfg, INTL, kress) == 120
    assert cadence.captures_orderbook(cfg, INTL, kress) is True
    assert cadence.interval_for(cfg, US, kress) == 3600, "a watchlist entry is venue-scoped"
    assert cadence.captures_orderbook(cfg, US, kress) is False


def test_watchlist_interval_slower_than_default_is_a_config_error(config_dict, write_config):
    config_dict["cadence"] = {"default_interval_s": 300, "watchlist_interval_s": 3600}
    with pytest.raises(pm("config").ConfigError):
        pm("config").load_config(write_config(config_dict))
