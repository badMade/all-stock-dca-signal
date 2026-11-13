"""Fail closed (D6) and Polymarket US credential handling (D12).

A failed fetch writes no rows, logs one failed run in ingestion_runs, and returns a result
instead of raising. Credentials come only from the vault or the environment and never
appear in logs, errors, results or the run table.
"""

from __future__ import annotations

import logging

import pytest
import requests

from .conftest import (
    INTL,
    NVDA_INTL_SLUG,
    NVDA_US_SLUG,
    TEST_PMUS_KEY_ID,
    TEST_PMUS_SECRET,
    TEST_PMUS_SIGNATURE,
    US,
    FakeResponse,
    FakeSession,
    pm,
    response_body,
)

SECRETS = (TEST_PMUS_KEY_ID, TEST_PMUS_SECRET, TEST_PMUS_SIGNATURE)
PM_TABLES = ("pm_markets", "pm_quotes", "pm_trades", "pm_orderbook_snapshots")


def _store_ok_everywhere(config_dict, write_config):
    config_dict["sources"][US]["license_scope"] = "store_ok"
    return pm("config").load_config(write_config(config_dict))


def _intl(session, cfg, clock):
    return pm("polymarket_intl").PolymarketIntlSource(session, config=cfg, clock=clock.now, sleep=clock.sleep)


def _us(session, cfg, clock, **kw):
    return pm("polymarket_us").PolymarketUSSource(session, config=cfg, clock=clock.now, sleep=clock.sleep, **kw)


def _assert_failed_and_empty(store, result, *, reason_contains):
    assert result.ok is False and result.status == "failed"
    assert reason_contains in result.reason
    for table in PM_TABLES:
        assert store.table_count(table) == 0, table
    (run,) = store.ingestion_runs()
    assert run["status"] == "failed" and reason_contains in run["reason"]
    assert run["rows_written"] == 0


FAILURES = [
    pytest.param(FakeResponse(status_code=503, text="upstream unavailable"), "http_503", id="5xx"),
    pytest.param(FakeResponse(status_code=500, text="{}"), "http_500", id="500"),
    pytest.param(requests.exceptions.Timeout("read timed out"), "timeout", id="timeout"),
    pytest.param(requests.exceptions.ConnectionError("reset"), "connection", id="connection"),
    pytest.param(FakeResponse(status_code=200, text='{"markets": ['), "parse", id="truncated-json"),
    pytest.param(FakeResponse(status_code=200, text='{"a": 1, "a": 2}'), "parse", id="duplicate-key"),
    pytest.param(FakeResponse(status_code=200, text="[]"), "parse", id="not-an-object"),
]


@pytest.mark.parametrize(("failure", "reason"), FAILURES)
@pytest.mark.parametrize("venue", [INTL, US])
def test_http_failure_fails_closed(config_dict, write_config, clock, store_factory, failure, reason, venue):
    cfg = _store_ok_everywhere(config_dict, write_config)
    session = FakeSession([failure])
    store = store_factory(cfg)
    if venue == INTL:
        fetch = lambda: _intl(session, cfg, clock).fetch_event(NVDA_INTL_SLUG)  # noqa: E731
    else:
        fetch = lambda: _us(session, cfg, clock).fetch_event(NVDA_US_SLUG)  # noqa: E731

    result = pm("source").run_ingestion(store, venue, fetch, clock=clock.now)  # must not raise

    _assert_failed_and_empty(store, result, reason_contains=reason)
    assert len(session.calls) == 1, "no silent retry"


def test_http_failure_mid_batch_writes_nothing(config, clock, store_factory):
    """One batch = one transaction: a history fetch that fails on its second chunk stores no rows
    from the first chunk either."""
    good = FakeResponse.from_fixture("intl_clob_batch_prices_history_2_tokens.json")
    session = FakeSession([good, FakeResponse(status_code=502, text="bad gateway")])
    tokens = [str(10**70 + i) for i in range(25)]  # 25 ids -> two POSTs of 20 and 5
    store = store_factory(config)

    result = pm("source").run_ingestion(
        store, INTL, lambda: _intl(session, config, clock).fetch_price_history(tokens, interval="1d", fidelity=60),
        clock=clock.now,
    )

    _assert_failed_and_empty(store, result, reason_contains="http_502")


def test_rejected_batch_size_fails_closed(config, clock, store_factory):
    """The venue's real 400 for 21 tokens is a failure, not an empty success."""
    session = FakeSession([FakeResponse.from_fixture("intl_clob_batch_prices_history_21_tokens_rejected.json")])
    store = store_factory(config)
    result = pm("source").run_ingestion(
        store, INTL, lambda: _intl(session, config, clock).fetch_price_history(["1", "2"], interval="1d", fidelity=60),
        clock=clock.now,
    )
    _assert_failed_and_empty(store, result, reason_contains="http_400")
    assert "exceeds maximum" not in result.reason, "venue response bodies are never echoed (.jules/sentinel.md)"


# --- Polymarket US credentials (D12) ---------------------------------------------------


def _creds():
    return pm("polymarket_us").PMUSCredentials(key_id=TEST_PMUS_KEY_ID, secret_key=TEST_PMUS_SECRET)


def _signer(secret, message):
    assert secret == TEST_PMUS_SECRET
    return TEST_PMUS_SIGNATURE


def test_pmus_credentials_loaded_from_vault_or_env_only(config):
    load = pm("polymarket_us").load_credentials
    env = {"POLYMARKET_US_KEY_ID": TEST_PMUS_KEY_ID, "POLYMARKET_US_SECRET_KEY": TEST_PMUS_SECRET}

    from_env = load(config, env=env)
    assert (from_env.key_id, from_env.secret_key) == (TEST_PMUS_KEY_ID, TEST_PMUS_SECRET)

    vault = {"POLYMARKET_US_KEY_ID": "VAULT-ID", "POLYMARKET_US_SECRET_KEY": "VAULT-SECRET"}
    from_vault = load(config, env={}, vault=vault.get)
    assert (from_vault.key_id, from_vault.secret_key) == ("VAULT-ID", "VAULT-SECRET")
    assert load(config, env=env, vault=vault.get).key_id == "VAULT-ID", "the vault wins over the environment"

    assert load(config, env={}) is None
    assert load(config, env={"POLYMARKET_US_KEY_ID": TEST_PMUS_KEY_ID}) is None, "half a key pair is no key"


@pytest.mark.parametrize("field", ["api_key", "secret_key", "key_id", "private_key", "password", "token"])
def test_pmus_credentials_never_accepted_from_config(config_dict, write_config, field):
    config_dict["sources"][US][field] = TEST_PMUS_SECRET
    with pytest.raises(pm("config").ConfigError) as excinfo:
        pm("config").load_config(write_config(config_dict))
    assert TEST_PMUS_SECRET not in str(excinfo.value)


def test_pmus_credentials_repr_hides_secret():
    text = repr(_creds()) + str(_creds())
    assert TEST_PMUS_SECRET not in text


def test_pmus_signed_request_carries_documented_headers(config, clock):
    """Docs (authentication.md): X-PM-Access-Key, X-PM-Timestamp (ms), X-PM-Signature over
    timestamp + method + path. Ed25519 has no stdlib implementation, so signing is injected."""
    session = FakeSession([FakeResponse.from_fixture("us_gateway_event_nvidiaceo-2028-12-31.json")])
    messages = []

    def signer(secret, message):
        messages.append(message)
        return TEST_PMUS_SIGNATURE

    _us(session, config, clock, credentials=_creds(), signer=signer, use_authenticated_api=True).fetch_event(
        NVDA_US_SLUG
    )

    (call,) = session.calls
    assert call.url.startswith("https://api.polymarket.us/v1/")
    ts = str(int(clock.now().timestamp() * 1000))
    assert call.headers["X-PM-Access-Key"] == TEST_PMUS_KEY_ID
    assert call.headers["X-PM-Timestamp"] == ts
    assert call.headers["X-PM-Signature"] == TEST_PMUS_SIGNATURE
    assert messages == [f"{ts}GET/v1/events/slug/{NVDA_US_SLUG}"]


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"credentials": None, "signer": _signer}, id="no-credentials"),
        pytest.param({"credentials": "creds", "signer": None}, id="no-signer"),
    ],
)
def test_pmus_missing_credentials_fails_closed(config_dict, write_config, clock, store_factory, kw):
    cfg = _store_ok_everywhere(config_dict, write_config)
    kw = {**kw, "credentials": _creds() if kw["credentials"] == "creds" else None}
    session = FakeSession([])
    store = store_factory(cfg)

    result = pm("source").run_ingestion(
        store, US, lambda: _us(session, cfg, clock, use_authenticated_api=True, **kw).fetch_event(NVDA_US_SLUG),
        clock=clock.now,
    )

    _assert_failed_and_empty(store, result, reason_contains="credentials")
    assert session.calls == [], "no unauthenticated request is sent to the authenticated API"


def test_pmus_rejected_credentials_fail_closed(config_dict, write_config, clock, store_factory):
    """The real 401 body from api.polymarket.us is a failed run, never a raise."""
    cfg = _store_ok_everywhere(config_dict, write_config)
    session = FakeSession([FakeResponse.from_fixture("us_api_markets_no_key_401.json")])
    store = store_factory(cfg)
    src = _us(session, cfg, clock, credentials=_creds(), signer=_signer, use_authenticated_api=True)

    result = pm("source").run_ingestion(store, US, lambda: src.fetch_event(NVDA_US_SLUG), clock=clock.now)

    _assert_failed_and_empty(store, result, reason_contains="http_401")


def test_pmus_credentials_never_logged(config_dict, write_config, clock, store_factory, caplog):
    cfg = _store_ok_everywhere(config_dict, write_config)
    session = FakeSession([FakeResponse.from_fixture("us_api_markets_no_key_401.json")])
    store = store_factory(cfg)
    src = _us(session, cfg, clock, credentials=_creds(), signer=_signer, use_authenticated_api=True)

    with caplog.at_level(logging.DEBUG):
        result = pm("source").run_ingestion(store, US, lambda: src.fetch_event(NVDA_US_SLUG), clock=clock.now)

    surfaces = [caplog.text, repr(result), str(result.reason), repr(store.ingestion_runs()), repr(src)]
    for secret in SECRETS:
        for surface in surfaces:
            assert secret not in surface
    assert response_body("us_api_markets_no_key_401.json").strip() not in result.reason
