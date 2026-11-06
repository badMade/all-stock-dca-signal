"""Tests for ``scripts/reshape_order_pages.py`` (written before the implementation).

Why this script exists (2026-11-03 and 2026-11-05): the 12:30 slot's Gate 4 asks the session
to reshape every raw ``get_equity_orders`` row of the ISO week BY HAND into the
``orders_week.json`` list its gate scripts read. By Thursday the week is ~150+ orders, so the
hand reshape is both the output-cap abort (2026-11-03) and the transcription risk
``assemble_order_pages`` was written to remove (2026-09-09, 2026-10-05). The other post-9:35
slots now reuse the 9:35 week envelope and merge only the new orders; 12:30 could not,
because its gates read the reshaped list, not the envelope. This script produces that list
from an envelope, so no row is ever retyped.

The contract is the 12:30 prompt's own Gate 4 reshape paragraph: one entry per order with
``symbol``, ``side``, ``dollar_amount``, ``created_at`` (offset-aware, verbatim),
``placed_agent``, ``state``, ``executed_dollar_amount``, ``client_order_id`` and ``order_id``.
The round-trip test below runs the prompt's OWN ``BrokerOrder(**...)`` construction and
``dca_runner.budget_snapshot`` over the output, so a drift between this script and the
prompt's gate code fails here, not in a live slot.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import dca_runner as r  # noqa: E402
from scripts import reshape_order_pages as reshape  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
MONDAY = "2026-11-02"
TODAY = date(2026, 11, 5)

# Copied from the 12:30 prompt's Gate 4/Gate 6 scripts: the fields BrokerOrder accepts.
_BROKER_ORDER_FIELDS = {
    "symbol",
    "side",
    "dollar_amount",
    "created_at",
    "placed_agent",
    "state",
    "executed_dollar_amount",
    "client_order_id",
}


def _to_notional(v):
    # The prompt's own boolean-refusing coercion.
    if isinstance(v, bool):
        raise TypeError(f"boolean notional {v!r} is not a valid dollar amount")
    return float(v)


def _prompt_orders(rows):
    """The 12:30 prompt's Gate 4 ``BrokerOrder(**...)`` construction, verbatim in shape."""
    return [
        r.BrokerOrder(
            **{
                k: v
                for k, v in {
                    **d,
                    "created_at": datetime.fromisoformat(d["created_at"]),
                    "dollar_amount": _to_notional(d["dollar_amount"]),
                    "executed_dollar_amount": _to_notional(d["executed_dollar_amount"])
                    if d.get("executed_dollar_amount") is not None
                    else None,
                }.items()
                if k in _BROKER_ORDER_FIELDS
            }
        )
        for d in rows
    ]


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-order/{label}"))


def _order(label, created_at, *, symbol="SPCX", usd="2.000000", state="filled", side="buy", **extra):
    """A raw ``get_equity_orders`` row with every creation-time field and a UUID id."""
    row = {
        "id": _uuid(label),
        "ref_id": _uuid(f"ref/{label}"),
        "symbol": symbol,
        "side": side,
        "type": "market",
        "state": state,
        "quantity": "0.006009",
        "cumulative_quantity": "0.006009",
        "price": "332.7999",
        "average_price": "332.7999",
        "fees": "0.000000",
        "dollar_based_amount": {"amount": usd, "currency_code": "USD"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "trigger": "immediate",
        "placed_agent": "agentic",
        "created_at": created_at,
        "last_transaction_at": created_at,
    }
    row.update(extra)
    return row


def _envelope(tmp_path, orders, *, account=ACCOUNT, day=MONDAY, pages=None, name="week.json"):
    envelope = {
        "account_number": account,
        "trading_day": day,
        "snapshot_at": "2026-11-05T14:36:00+00:00",
        "pages": pages if pages is not None else [{"orders": orders}],
    }
    path = tmp_path / name
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return str(path)


def _run(tmp_path, envelope, *, account=ACCOUNT, day=MONDAY):
    out = tmp_path / "orders_week.json"
    code = reshape.main(["--envelope", envelope, "--account", account, "--trading-day", day, "--out", str(out)])
    return code, out


def _week_rows():
    return [
        _order("mon", "2026-11-02T14:40:00+00:00", symbol="AAPL", usd="3.00"),
        _order("tue", "2026-11-03T15:00:00.123456+00:00", symbol="MSFT", usd="2.50"),
        _order("today-1", "2026-11-05T09:30:00-05:00", symbol="NVDA", usd="4.25"),
        _order("today-2", "2026-11-05T14:35:00Z", symbol="SPCX", usd="1.75"),
    ]


def test_round_trip_through_the_prompts_own_construction_and_budget_snapshot(tmp_path):
    code, out = _run(tmp_path, _envelope(tmp_path, _week_rows()))
    assert code == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    snap = r.budget_snapshot(_prompt_orders(rows), TODAY)
    assert snap.day_notional_usd == pytest.approx(4.25 + 1.75)
    assert snap.week_notional_usd == pytest.approx(3.00 + 2.50 + 4.25 + 1.75)
    assert snap.day_order_count == 2


def test_every_row_carries_exactly_the_gate4_contract_keys(tmp_path):
    code, out = _run(tmp_path, _envelope(tmp_path, _week_rows()))
    assert code == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == 4
    for row in rows:
        assert set(row) == reshape.CONTRACT_FIELDS


def test_values_are_mapped_from_the_raw_row(tmp_path):
    raw = _order("one", "2026-11-05T13:45:00.5-05:00", symbol="nvda", usd="4.25")
    code, out = _run(tmp_path, _envelope(tmp_path, [raw]))
    assert code == 0
    [row] = json.loads(out.read_text(encoding="utf-8"))
    assert row == {
        "symbol": "NVDA",
        "side": "buy",
        "dollar_amount": 4.25,
        # Verbatim: not reformatted, not converted to UTC.
        "created_at": "2026-11-05T13:45:00.5-05:00",
        "placed_agent": "agentic",
        "state": "filled",
        "executed_dollar_amount": None,
        "client_order_id": raw["ref_id"],
        "order_id": raw["id"],
    }


def test_a_partially_filled_cancelled_buy_keeps_its_executed_notional(tmp_path):
    rows = [
        _order("c1", "2026-11-05T14:00:00+00:00", usd="5.00", state="cancelled",
               executed_notional={"amount": "1.20", "currency_code": "USD"}),
        _order("c2", "2026-11-05T14:01:00+00:00", usd="5.00", state="cancelled", executed_notional=None,
               cumulative_quantity="0.01", average_price="100.00"),
        _order("c3", "2026-11-05T14:02:00+00:00", usd="5.00", state="rejected", executed_notional=None,
               cumulative_quantity="0", average_price=None),
    ]
    code, out = _run(tmp_path, _envelope(tmp_path, rows))
    assert code == 0
    reshaped = {row["order_id"]: row for row in json.loads(out.read_text(encoding="utf-8"))}
    assert reshaped[_uuid("c1")]["executed_dollar_amount"] == pytest.approx(1.20)
    assert reshaped[_uuid("c2")]["executed_dollar_amount"] == pytest.approx(1.00)
    assert reshaped[_uuid("c3")]["executed_dollar_amount"] is None
    snap = r.budget_snapshot(_prompt_orders(list(reshaped.values())), TODAY)
    assert snap.day_notional_usd == pytest.approx(2.20)


def test_a_quantity_based_buy_is_valued_not_read_as_zero(tmp_path):
    raw = _order("qty", "2026-11-05T14:00:00+00:00", quantity="2", price="10.50")
    raw["dollar_based_amount"] = None
    code, out = _run(tmp_path, _envelope(tmp_path, [raw]))
    assert code == 0
    [row] = json.loads(out.read_text(encoding="utf-8"))
    assert row["dollar_amount"] == pytest.approx(21.0)


def test_a_missing_client_reference_is_kept_as_null(tmp_path):
    rows = [
        _order("none", "2026-11-05T14:00:00+00:00", ref_id=None),
        _order("empty", "2026-11-05T14:01:00+00:00", ref_id=""),
    ]
    code, out = _run(tmp_path, _envelope(tmp_path, rows))
    assert code == 0
    reshaped = json.loads(out.read_text(encoding="utf-8"))
    assert [row["client_order_id"] for row in reshaped] == [None, None]
    assert {row["order_id"] for row in reshaped} == {_uuid("none"), _uuid("empty")}


def test_order_id_falls_back_to_the_raw_order_id_field_and_otherwise_is_null():
    # assemble_order_pages' proof refuses a row without a UUID ``id``, so a null order_id can
    # only come from the row mapping itself; exercise it there.
    row = _order("x", "2026-11-05T14:00:00+00:00")
    alt = dict(row)
    del alt["id"]
    alt["order_id"] = _uuid("alt")
    assert reshape.reshape_row(alt, 0)["order_id"] == _uuid("alt")
    bare = dict(row)
    del bare["id"]
    assert reshape.reshape_row(bare, 0)["order_id"] is None


def test_a_multi_page_chain_is_reshaped_in_fetch_order(tmp_path):
    first = _order("p1", "2026-11-05T14:00:00+00:00")
    second = _order("p2", "2026-11-04T14:00:00+00:00")
    pages = [{"orders": [first], "next": "https://api.example/orders/?cursor=P2"}, {"orders": [second]}]
    code, out = _run(tmp_path, _envelope(tmp_path, None, pages=pages))
    assert code == 0
    assert [row["order_id"] for row in json.loads(out.read_text(encoding="utf-8"))] == [_uuid("p1"), _uuid("p2")]


def test_an_order_repeated_across_pages_is_written_once(tmp_path):
    row = _order("dup", "2026-11-05T14:00:00+00:00")
    pages = [{"orders": [row], "next": "https://api.example/orders/?cursor=P2"}, {"orders": [row]}]
    code, out = _run(tmp_path, _envelope(tmp_path, None, pages=pages))
    assert code == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 1


@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_reshape_row_refuses_an_unusable_symbol(value):
    row = _order("x", "2026-11-05T14:00:00+00:00", symbol=value)
    with pytest.raises(reshape.asm.AssemblyRefused) as exc:
        reshape.reshape_row(row, 3)
    assert exc.value.reason == "malformed_row"
    assert exc.value.detail == "order 3: symbol is not a non-empty string"


def test_refuses_a_naive_timestamp_and_writes_nothing(tmp_path, capsys):
    rows = _week_rows()
    rows[1]["created_at"] = "2026-11-03T15:00:00"
    code, out = _run(tmp_path, _envelope(tmp_path, rows))
    assert code == 2
    assert not out.exists()
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "reason": "naive_timestamp", "detail": result["detail"]}


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("created_at", "not a time", "malformed_row"),
        ("created_at", None, "malformed_row"),
        # The shared pagination replay already refuses a row with no usable symbol, before
        # the row mapping sees it; either way the run writes nothing.
        ("symbol", "", "history_incomplete"),
        ("symbol", None, "history_incomplete"),
        ("side", None, "malformed_row"),
        ("side", 7, "malformed_row"),
        ("state", None, "malformed_row"),
        ("ref_id", 12345, "malformed_row"),
        ("placed_agent", {"agent": "x"}, "malformed_row"),
        ("executed_notional", {"amount": True}, "malformed_row"),
        ("dollar_based_amount", {"amount": "NaN"}, "non_finite_value"),
        ("dollar_based_amount", {"amount": True}, "non_finite_value"),
    ],
)
def test_refuses_a_malformed_row_and_writes_nothing(tmp_path, capsys, field, value, reason):
    rows = _week_rows()
    rows[2] = dict(rows[2], state="cancelled") if field == "executed_notional" else rows[2]
    rows[2][field] = value
    code, out = _run(tmp_path, _envelope(tmp_path, rows))
    assert code == 2
    assert not out.exists()
    assert json.loads(capsys.readouterr().out)["reason"] == reason


def test_refuses_a_quantity_based_buy_it_cannot_value(tmp_path, capsys):
    raw = _order("qty", "2026-11-05T14:00:00+00:00", quantity=None, price=None, average_price=None)
    raw["dollar_based_amount"] = None
    code, out = _run(tmp_path, _envelope(tmp_path, [raw]))
    assert code == 2
    assert not out.exists()
    assert json.loads(capsys.readouterr().out)["reason"] == "malformed_row"


def test_refuses_an_incomplete_page_chain(tmp_path, capsys):
    pages = [{"orders": [_order("a", "2026-11-05T14:00:00+00:00")], "next": "https://api.example/orders/?cursor=P2"}]
    code, out = _run(tmp_path, _envelope(tmp_path, None, pages=pages))
    assert code == 2
    assert not out.exists()


def test_refuses_a_condensed_capture_via_the_shared_proof(tmp_path, capsys):
    row = _order("a", "2026-11-05T14:00:00+00:00")
    del row["ref_id"]
    code, out = _run(tmp_path, _envelope(tmp_path, [row]))
    assert code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "condensed_capture"


@pytest.mark.parametrize(
    "override,reason",
    [({"account": "999000222"}, "orders_pages_account_mismatch"), ({"day": "2026-10-26"}, "orders_pages_trading_day_mismatch")],
)
def test_refuses_an_envelope_for_another_account_or_week(tmp_path, capsys, override, reason):
    code, out = _run(tmp_path, _envelope(tmp_path, _week_rows()), **override)
    assert code == 2
    assert not out.exists()
    assert json.loads(capsys.readouterr().out)["reason"] == reason


def test_refuses_an_unreadable_envelope(tmp_path, capsys):
    path = tmp_path / "week.json"
    path.write_text('{"account_number": "1", "account_number": "2"}', encoding="utf-8")
    code, out = _run(tmp_path, str(path))
    assert code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "envelope_unreadable"


def test_a_refusal_leaves_an_existing_output_untouched(tmp_path):
    out = tmp_path / "orders_week.json"
    out.write_text("[]\n", encoding="utf-8")
    rows = _week_rows()
    rows[0]["created_at"] = "2026-11-02T14:40:00"
    code, _ = _run(tmp_path, _envelope(tmp_path, rows))
    assert code == 2
    assert out.read_text(encoding="utf-8") == "[]\n"


def test_reshapes_a_merged_envelope_from_merge_order_pages(tmp_path):
    from scripts import merge_order_pages as merge

    base_rows = _week_rows()
    base = _envelope(tmp_path, base_rows, name="base.json")
    new = _order("today-3", "2026-11-05T14:50:00+00:00", symbol="AMD", usd="2.00")
    page = tmp_path / "inc.json"
    # The increment repeats the base's in-window order and adds the new one.
    page.write_text(json.dumps({"orders": [new, base_rows[3]]}), encoding="utf-8")
    merged = tmp_path / "merged.json"
    assert merge.main([
        "--base", base, "--account", ACCOUNT, "--trading-day", MONDAY,
        "--created-at-gte", "2026-11-05T14:34:00+00:00", "--snapshot-at", "2026-11-05T15:00:00+00:00",
        "--overlap-seconds", "120", "--out", str(merged), str(page),
    ]) == 0
    code, out = _run(tmp_path, str(merged))
    assert code == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert r.budget_snapshot(_prompt_orders(rows), TODAY).day_notional_usd == pytest.approx(4.25 + 1.75 + 2.00)
