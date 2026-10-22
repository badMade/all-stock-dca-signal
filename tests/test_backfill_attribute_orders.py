"""Backfill with operator-attributed orders: real buys inside an aborted fire's window.

2026-10-21: the Gate 2 aborts of the Oct 14 and Oct 15 15:30 fires said "no orders placed", yet the agentic
account shows agent-initiated buys inside both acceptance windows (Oct 14 15:48 DFTX $1.24; Oct 15 TRIN,
SPCX, IRDM $1.00 each). ``backfill_afternoon_evidence`` rightly refuses any in-window order ("needs a human"),
which leaves those fires with no evidence at all and the deployed total understated. A human can decide that
named orders belong to a fire; these tests pin what the tool must then do, and what it must still refuse:

* only orders the operator NAMES (``--attribute-order``) are attributed; any other in-window order still refuses;
* attributed orders become real deployment in the run_state (so the 7-day kill-switch total counts them);
* the state never invents a Gate 6 budget (tier and caps) for a slot that aborted before Gate 6;
* the reconstruction block records who attributed what, so the record stays distinguishable from a live fire.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_backfill_afternoon_evidence import _run, _setup

import dca_risk

OCT14_1530 = "2026-10-14:1530"
OCT15_1530 = "2026-10-15:1530"
OCT14_ALERT = "ce16ba6b/scheduled_runs/alerts/2026-10-14-1530-gate2.txt"
OCT15_ALERT = "ce16ba6b/scheduled_runs/alerts/2026-10-15-1530-gate2.txt"
STATE_14 = "ce16ba6b/scheduled_runs/20261014_1530ET"
STATE_15 = "ce16ba6b/scheduled_runs/20261015_1530ET"
# Backfill's own COVER (2026-10-13 .. 2026-10-21) spans both windows.


def _order(order_id, symbol, created_at, amount, state="filled", side="buy"):
    """A broker order in the shape get_equity_orders returns (only the fields the tool may read)."""
    return {
        "id": order_id, "symbol": symbol, "side": side, "type": "market", "state": state,
        "created_at": created_at, "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "placed_agent": "agentic", "agent_initiated": True,
    }


DFTX = _order("order-dftx-oct14", "DFTX", "2026-10-14T19:48:24.000000Z", "1.240000")
TRIN = _order("order-trin-oct15", "TRIN", "2026-10-15T19:49:52.000000Z", "1.000000")
SPCX = _order("order-spcx-oct15", "SPCX", "2026-10-15T19:51:59.000000Z", "1.000000")
IRDM = _order("order-irdm-oct15", "IRDM", "2026-10-15T19:54:50.000000Z", "1.000000")
MIDDAY = _order("order-gs-midday", "GS", "2026-10-14T16:30:00.000000Z", "3.930000")  # 12:30 ET


def _state(tmp_path: Path, rel: str) -> dict:
    return json.loads((tmp_path / rel / "run_state.json").read_text(encoding="utf-8"))


def _attribute(*order_ids):
    args = []
    for order_id in order_ids:
        args += ["--attribute-order", order_id]
    return args


def test_an_unattributed_in_window_order_is_still_refused_and_nothing_is_written(tmp_path, capsys):
    orders = _setup(tmp_path, [DFTX], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, fire=OCT14_1530) == 2

    assert "needs a human" in capsys.readouterr().out
    assert not (tmp_path / STATE_14).exists()


def test_attributing_the_order_records_it_as_real_deployment(tmp_path):
    orders = _setup(tmp_path, [DFTX], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, *_attribute(DFTX["id"]), fire=OCT14_1530) == 0

    state = _state(tmp_path, STATE_14)
    assert dca_risk.is_afternoon_scheduled_evidence(state)
    assert state["order_submit_status"] == "submitted"
    assert state["counts"]["submitted"] == 1
    assert dca_risk.deployed_usd(state) == 1.24
    csv = (tmp_path / STATE_14 / "submitted_orders.csv").read_text(encoding="utf-8")
    assert DFTX["id"] in csv and "DFTX" in csv


def test_attributed_deployment_lands_on_the_fire_day_in_the_seven_day_total(tmp_path):
    orders = _setup(tmp_path, [TRIN, SPCX, IRDM], alerts=(OCT15_ALERT,))

    assert _run(tmp_path, orders, *_attribute(TRIN["id"], SPCX["id"], IRDM["id"]), fire=OCT15_1530) == 0

    by_day = dca_risk.deployed_by_day([_state(tmp_path, STATE_15)])
    assert by_day == {date(2026, 10, 15): 3.0}
    assert dca_risk.total_deployed(by_day, date(2026, 10, 20), 7) == 3.0


def test_no_gate_6_budget_is_invented_for_a_slot_that_aborted_before_gate_6(tmp_path):
    orders = _setup(tmp_path, [DFTX], alerts=(OCT14_ALERT,))

    _run(tmp_path, orders, *_attribute(DFTX["id"]), fire=OCT14_1530)

    budget = _state(tmp_path, STATE_14).get("budget") or {}
    assert not {"budget_tier", "daily_cap_usd", "weekly_cap_usd"} & set(budget)
    assert budget.get("budget_evaluated") is False


def test_the_reconstruction_records_the_attribution_for_audit(tmp_path):
    orders = _setup(tmp_path, [DFTX], alerts=(OCT14_ALERT,))

    _run(tmp_path, orders, *_attribute(DFTX["id"]), fire=OCT14_1530)

    reconstruction = _state(tmp_path, STATE_14)["gate9_report"]["reconstruction"]
    assert reconstruction["backfilled"] is True
    assert reconstruction["attributed_order_ids"] == [DFTX["id"]]
    assert reconstruction["attributed_by"] == "operator"
    assert reconstruction["broker_orders_in_window"] == 1


def test_naming_only_some_of_the_in_window_orders_still_refuses_the_rest(tmp_path, capsys):
    orders = _setup(tmp_path, [TRIN, SPCX, IRDM], alerts=(OCT15_ALERT,))

    assert _run(tmp_path, orders, *_attribute(TRIN["id"], SPCX["id"]), fire=OCT15_1530) == 2

    out = capsys.readouterr().out
    assert IRDM["id"] in out and "needs a human" in out
    assert not (tmp_path / STATE_15).exists()


@pytest.mark.parametrize(
    "bad_id, reason",
    [
        ("order-not-in-the-file", "not in the order file"),
        (MIDDAY["id"], "not inside this fire's window"),
    ],
)
def test_an_id_that_is_not_an_in_window_order_is_refused(tmp_path, capsys, bad_id, reason):
    orders = _setup(tmp_path, [DFTX, MIDDAY], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, *_attribute(DFTX["id"], bad_id), fire=OCT14_1530) == 2

    assert bad_id in capsys.readouterr().out, reason
    assert not (tmp_path / STATE_14).exists()


@pytest.mark.parametrize("state", ["cancelled", "rejected", "failed"])
def test_an_order_that_never_deployed_capital_cannot_be_attributed_as_deployment(tmp_path, capsys, state):
    dead = _order(DFTX["id"], "DFTX", DFTX["created_at"], "1.240000", state=state)
    orders = _setup(tmp_path, [dead], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, *_attribute(dead["id"]), fire=OCT14_1530) == 2

    assert state in capsys.readouterr().out
    assert not (tmp_path / STATE_14).exists()


def test_a_sell_cannot_be_attributed_as_deployment(tmp_path, capsys):
    sell = _order(DFTX["id"], "DFTX", DFTX["created_at"], "1.240000", side="sell")
    orders = _setup(tmp_path, [sell], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, *_attribute(sell["id"]), fire=OCT14_1530) == 2

    assert "sell" in capsys.readouterr().out
    assert not (tmp_path / STATE_14).exists()


def test_a_dry_run_with_attribution_writes_nothing_and_names_the_attributed_orders(tmp_path, capsys):
    orders = _setup(tmp_path, [DFTX], alerts=(OCT14_ALERT,))

    assert _run(tmp_path, orders, *_attribute(DFTX["id"]), "--dry-run", fire=OCT14_1530) == 0

    assert DFTX["id"] in capsys.readouterr().out
    assert not (tmp_path / STATE_14).exists()


@pytest.mark.parametrize("amount", [True, None, "", "NaN", "Infinity", "-1", "0", [], {}, 10**400])
def test_malformed_attributed_amount_is_refused_before_writing(tmp_path, amount):
    order = _order(DFTX["id"], "DFTX", DFTX["created_at"], amount)
    orders = _setup(tmp_path, [order], alerts=(OCT14_ALERT,))
    assert _run(tmp_path, orders, *_attribute(order["id"]), fire=OCT14_1530) == 2
    assert not (tmp_path / STATE_14).exists()


@pytest.mark.parametrize("field, value", [("symbol", ""), ("dollar_based_amount", None), ("state", "pending")])
def test_incomplete_or_unfilled_attributed_order_is_refused(tmp_path, field, value):
    orders = _setup(tmp_path, [{**DFTX, field: value}], alerts=(OCT14_ALERT,))
    assert _run(tmp_path, orders, *_attribute(DFTX["id"]), fire=OCT14_1530) == 2
    assert not (tmp_path / STATE_14).exists()


def test_duplicate_broker_order_ids_are_refused(tmp_path):
    orders = _setup(tmp_path, [DFTX, DFTX], alerts=(OCT14_ALERT,))
    assert _run(tmp_path, orders, *_attribute(DFTX["id"]), fire=OCT14_1530) == 2
    assert not (tmp_path / STATE_14).exists()


@pytest.mark.parametrize("payload", ['[null]', '[{"id":"first","id":"last"}]', '{}'])
def test_ambiguous_or_nonobject_history_is_refused(tmp_path, payload):
    orders = _setup(tmp_path, alerts=(OCT14_ALERT,))
    orders.write_text(payload, encoding="utf-8")
    assert _run(tmp_path, orders, fire=OCT14_1530) == 2
    assert not (tmp_path / STATE_14).exists()


def test_attributions_for_multiple_fires_are_partitioned_by_window(tmp_path):
    orders = _setup(tmp_path, [DFTX, TRIN], alerts=(OCT14_ALERT, OCT15_ALERT))
    assert _run(
        tmp_path, orders, "--fire", OCT15_1530, *_attribute(DFTX["id"], TRIN["id"]), fire=OCT14_1530,
    ) == 0
    for rel, order in ((STATE_14, DFTX), (STATE_15, TRIN)):
        state = _state(tmp_path, rel)
        assert state["gate9_report"]["reconstruction"]["attributed_order_ids"] == [order["id"]]
        assert dca_risk.deployed_usd(state) == float(order["dollar_based_amount"]["amount"])


def test_invalid_attribution_for_later_fire_prevents_all_writes(tmp_path):
    dead = {**TRIN, "state": "cancelled"}
    orders = _setup(tmp_path, [DFTX, dead], alerts=(OCT14_ALERT, OCT15_ALERT))
    assert _run(
        tmp_path, orders, "--fire", OCT15_1530, *_attribute(DFTX["id"], TRIN["id"]), fire=OCT14_1530,
    ) == 2
    assert not (tmp_path / STATE_14).exists()
    assert not (tmp_path / STATE_15).exists()
