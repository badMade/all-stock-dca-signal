#!/usr/bin/env python3
"""Tests for validating a native ``place_equity_order`` response.

Derived from observed broker behavior, not from an implementation:

* 2026-10-12 10:43 ET (1030ET slot): ``place_equity_order`` rejected the
  prompt's literal ``dca-runner-1030-2026-10-12-XLG`` ref_id ("ref_id must be
  a UUID"). Re-sent with a deterministic UUID, the order was accepted and
  filled, but the response echoed a broker-assigned ``ref_id``
  (``01a13091-...``), not the one sent. Every agentic order in that day's
  history carries a ``01a1...`` ref_id, so the echo never matches. The prompt's
  "returned ref_id must equal the sent ref_id" rule therefore halted the batch
  after its first order and TOPT was never placed.

The replacement identity proof: the response names a NEW order (its id is not
in the order history fetched before the call), created at or after the call
started, for the requested symbol, side and dollar amount, in a live or filled
state. Every existing failure shape (rejected state, wrong symbol, wrong
amount, no id) still halts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from scripts.idempotency import deterministic_ref_id
from scripts.validate_native_placement import (
    main,
    native_ref_id,
    validate_native_placement,
)

SENT_REF = native_ref_id("1030", "2026-10-12", "XLG")
CALL_START = "2026-10-12T14:43:10Z"


def _response(**order_overrides):
    """The real 2026-10-12 XLG response shape (no account number in it)."""
    order = {
        "id": "6accf201-232f-4c07-a725-d7ede7005be7",
        "ref_id": "01a13091-55e2-7524-836f-18ae3e78a922",
        "symbol": "XLG",
        "side": "buy",
        "type": "market",
        "state": "unconfirmed",
        "quantity": "0.029350",
        "cumulative_quantity": "0.000000",
        "dollar_based_amount": {"amount": "1.900000", "currency_code": "USD"},
        "placed_agent": "agentic",
        "created_at": "2026-10-12T14:43:13.631118Z",
        "executions": [],
    }
    order.update(order_overrides)
    return {"data": {"order": order}}


def _check(response, **kw):
    args = dict(
        expected_symbol="XLG",
        expected_side="buy",
        expected_amount_usd=1.90,
        sent_ref_id=SENT_REF,
        prior_order_ids=frozenset({"6acce93a-afc6-4c7c-8b8f-7481505a5b7e"}),
        submitted_after=CALL_START,
        response_received_at=datetime(2026, 10, 12, 14, 43, 20, tzinfo=timezone.utc),
    )
    args.update(kw)
    return validate_native_placement(response, **args)


# --- ref_id derivation -------------------------------------------------------

def test_native_ref_id_is_a_uuid():
    uuid.UUID(SENT_REF)


def test_native_ref_id_is_deterministic_per_slot_day_ticker():
    assert native_ref_id("1030", "2026-10-12", "XLG") == SENT_REF
    assert native_ref_id("1030", "2026-10-12", "xlg") == SENT_REF
    assert native_ref_id("1130", "2026-10-12", "XLG") != SENT_REF
    assert native_ref_id("1030", "2026-10-13", "XLG") != SENT_REF
    assert native_ref_id("1030", "2026-10-12", "TOPT") != SENT_REF


def test_native_ref_id_matches_repo_idempotency_helper():
    assert SENT_REF == deterministic_ref_id("dca-runner-1030-2026-10-12-XLG")


# --- the regression ----------------------------------------------------------

def test_broker_assigned_ref_id_on_a_new_matching_order_is_confirmed():
    result = _check(_response())
    assert result["ok"] is True, result
    assert result["order_id"] == "6accf201-232f-4c07-a725-d7ede7005be7"


def test_echoed_sent_ref_id_is_also_confirmed():
    assert _check(_response(ref_id=SENT_REF))["ok"] is True


def test_filled_state_is_confirmed():
    assert _check(_response(state="filled", cumulative_quantity="0.029353"))["ok"] is True


# --- identity proof still halts on stale or foreign responses ---------------

def test_order_id_already_in_prior_history_halts():
    result = _check(_response(), prior_order_ids=frozenset({"6accf201-232f-4c07-a725-d7ede7005be7"}))
    assert result["ok"] is False
    assert "prior" in result["reason"]


def test_order_created_before_the_call_halts():
    result = _check(_response(created_at="2026-10-12T14:05:46.4915Z"))
    assert result["ok"] is False
    assert "created" in result["reason"]


def test_order_created_after_response_clock_halts():
    result = _check(
        _response(created_at="2026-10-12T14:43:30Z"),
        response_received_at=datetime(2026, 10, 12, 14, 43, 20, tzinfo=timezone.utc),
    )
    assert result["ok"] is False
    assert "after" in result["reason"]


def test_missing_created_at_halts():
    result = _check(_response(created_at=None))
    assert result["ok"] is False


def test_naive_submitted_after_is_refused():
    with pytest.raises(ValueError):
        _check(_response(), submitted_after="2026-10-12T14:43:10")


def test_returned_ref_id_that_is_not_a_uuid_halts():
    assert _check(_response(ref_id="dca-runner-1030-2026-10-12-TOPT"))["ok"] is False


# --- existing failure shapes still halt -------------------------------------

def test_symbol_mismatch_halts():
    assert _check(_response(symbol="TOPT"))["ok"] is False


def test_side_mismatch_halts():
    assert _check(_response(side="sell"))["ok"] is False


def test_amount_mismatch_halts():
    resp = _response(dollar_based_amount={"amount": "100.000000", "currency_code": "USD"})
    assert _check(resp)["ok"] is False


@pytest.mark.parametrize("state", ["rejected", "failed", "cancelled"])
def test_terminal_state_halts(state):
    assert _check(_response(state=state))["ok"] is False


def test_missing_order_id_halts():
    assert _check(_response(id=None))["ok"] is False


def test_approval_instead_of_order_halts():
    assert _check({"data": {"approval": {"id": "APPROVAL"}}})["ok"] is False


def test_error_envelope_halts():
    assert _check({"isError": True, "error": "timeout"})["ok"] is False


def test_second_order_in_sibling_branch_halts():
    response = _response()
    response["orders"] = [{"id": "SECOND", "state": "queued"}]
    result = _check(response)
    assert result["ok"] is False
    assert "additional" in result["reason"]


# --- CLI ---------------------------------------------------------------------

def test_cli_ref_id_prints_the_uuid(capsys):
    assert main(["ref-id", "--slot", "1030", "--trading-day", "2026-10-12", "--ticker", "XLG"]) == 0
    assert capsys.readouterr().out.strip() == SENT_REF


def _cli_check(tmp_path, response, prior_ids):
    resp_path = tmp_path / "resp.json"
    resp_path.write_text(json.dumps(response))
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(sorted(prior_ids)))
    return main([
        "check", "--response", str(resp_path), "--symbol", "XLG", "--side", "buy",
        "--amount", "1.90", "--sent-ref-id", SENT_REF,
        "--prior-order-ids", str(prior_path), "--submitted-after", CALL_START,
    ])


def test_cli_check_exit_0_on_confirmed(tmp_path, capsys):
    assert _cli_check(tmp_path, _response(), {"OLD"}) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_check_exit_1_on_halt(tmp_path, capsys):
    assert _cli_check(tmp_path, _response(symbol="TOPT"), {"OLD"}) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_rejects_duplicate_json_keys_in_response(tmp_path):
    response_path = tmp_path / "response.json"
    response_path.write_text('{"data":{"order":{"id":"ONE","id":"TWO"}}}')
    prior_path = tmp_path / "prior.json"
    prior_path.write_text('["OLD"]')
    with pytest.raises(SystemExit) as exc:
        main([
            "check", "--response", str(response_path), "--symbol", "XLG", "--side", "buy",
            "--amount", "1.90", "--sent-ref-id", SENT_REF, "--prior-order-ids", str(prior_path),
            "--submitted-after", CALL_START,
        ])
    assert exc.value.code == 2


@pytest.mark.parametrize("raw", ['{"data":{"order":{"id":"ONE"}},"amount":NaN}', '{"value":Infinity}'])
def test_cli_rejects_nonstandard_constants_in_response(tmp_path, raw):
    response_path = tmp_path / "response.json"
    response_path.write_text(raw)
    prior_path = tmp_path / "prior.json"
    prior_path.write_text('["OLD"]')
    with pytest.raises(SystemExit) as exc:
        main([
            "check", "--response", str(response_path), "--symbol", "XLG", "--side", "buy",
            "--amount", "1.90", "--sent-ref-id", SENT_REF, "--prior-order-ids", str(prior_path),
            "--submitted-after", CALL_START,
        ])
    assert exc.value.code == 2


@pytest.mark.parametrize("raw", ['["OLD","OLD"]', '["OLD",NaN]', '["OLD",Infinity]'])
def test_cli_rejects_ambiguous_prior_order_history(tmp_path, raw):
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(_response()))
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(raw)
    with pytest.raises(SystemExit) as exc:
        main([
            "check", "--response", str(response_path), "--symbol", "XLG", "--side", "buy",
            "--amount", "1.90", "--sent-ref-id", SENT_REF, "--prior-order-ids", str(prior_path),
            "--submitted-after", CALL_START,
        ])
    assert exc.value.code == 2


def test_cli_persists_only_account_redacted_response(tmp_path, monkeypatch, capsys):
    account_number = "999000111"
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", account_number)
    response = _response(
        account_number=account_number,
        numeric_account=999000111,
        nested={f"account={account_number}": account_number},
    )
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(response))
    prior_path = tmp_path / "prior.json"
    prior_path.write_text('["OLD"]')
    artifact_path = tmp_path / "place.json"
    assert main([
        "check", "--response", str(response_path), "--symbol", "XLG", "--side", "buy",
        "--amount", "1.90", "--sent-ref-id", SENT_REF, "--prior-order-ids", str(prior_path),
        "--submitted-after", CALL_START, "--redacted-response", str(artifact_path),
    ]) == 0
    capsys.readouterr()
    artifact = artifact_path.read_text()
    assert account_number not in artifact
    assert artifact.count("[REDACTED-ACCOUNT]") >= 4


def test_cli_refuses_to_replace_raw_response_with_redacted_copy(tmp_path):
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(_response()))
    prior_path = tmp_path / "prior.json"
    prior_path.write_text('["OLD"]')
    with pytest.raises(SystemExit) as exc:
        main([
            "check", "--response", str(response_path), "--symbol", "XLG", "--side", "buy",
            "--amount", "1.90", "--sent-ref-id", SENT_REF, "--prior-order-ids", str(prior_path),
            "--submitted-after", CALL_START, "--redacted-response", str(response_path),
        ])
    assert exc.value.code == 2
    assert json.loads(response_path.read_text()) == _response()
