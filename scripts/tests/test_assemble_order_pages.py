"""Tests for ``scripts/assemble_order_pages.py``.

Why this script exists (DCA Issues ``output_cap_exceeded``, 2026-09-09 12:30
Gate 6 abort): a single ``get_equity_orders`` page came back larger than the
session's tool-output cap, the session transcribed the rows out of the saved
output BY HAND, and two transcription passes disagreed on the array length.
The run failed closed, which was correct -- but the slot was lost. The fix is
to never transcribe: the session hands the saved page file(s) to this script,
which parses them as JSON, proves the pagination chain is complete with the
SAME replay ``scripts/same_day_top_up_gate.py`` already trusts, and writes the
``orders_pages_<day>_<slot>.json`` envelope every downstream gate consumes.

Every failure must exit 2 and write NOTHING: a half-written or partial
envelope must never be readable as a clean, complete history.

2026-10-05 addendum (five of seven slots): the same hand-transcription failure hit the
order history when the response was NOT oversized, so no saved file existed and the
session re-keyed it. The 10:30 envelope kept 12 of the ~19 raw fields (no ``ref_id``) and
the 14:30 one carried ids like ``transcribed-00-AAPL``; the top-up guard returned PROCEED
on both. This script is now the one validated path in, so it also refuses placeholder ids
and condensed captures, can cross-check the response's own count and dollar total, and
records provenance plus content hashes in an ``ingest`` block inside the envelope (the
block the Gate 6.5 guard will later be made to require; that enforcement is a separate
step with its own tests).
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import uuid

import pytest

from scripts import assemble_order_pages as asm
from scripts import same_day_top_up_gate as gate

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
DAY = "2026-09-09"


def _uuid(label):
    """A deterministic canonical UUID standing in for a broker-issued order id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-order/{label}"))


def _order(order_id, symbol="SPCX", usd="2.000000"):
    """A raw ``get_equity_orders`` row: every creation-time field, UUID id and ref_id.

    ``order_id`` is only a label; the real id is derived from it so a fixture can never be
    mistaken for a placeholder by the validation below.
    """
    return {
        "id": _uuid(order_id),
        "ref_id": _uuid(f"ref/{order_id}"),
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "state": "filled",
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
        "created_at": f"{DAY}T14:00:00+00:00",
        "last_transaction_at": f"{DAY}T14:00:00+00:00",
    }


def _page(orders, *, next_url=None):
    body = {"orders": orders}
    if next_url is not None:
        body["next"] = next_url
    return body


def _write(tmp_path, name, obj_or_text):
    path = tmp_path / name
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run(tmp_path, page_files, *, account=ACCOUNT, day=DAY, extra=()):
    out = tmp_path / "orders_pages_out.json"
    argv = ["--account", account, "--trading-day", day, "--out", str(out), *extra, *page_files]
    code = asm.main(argv)
    return code, out


# --- happy paths ----------------------------------------------------------------


def test_complete_two_page_chain_writes_the_envelope_verbatim(tmp_path):
    p1 = _page([_order("o1"), _order("o2")], next_url="https://api.example/orders/?cursor=P2")
    p2 = _page([_order("o3")])
    files = [_write(tmp_path, "p1.txt", p1), _write(tmp_path, "p2.txt", p2)]

    code, out = _run(tmp_path, files)

    assert code == 0
    envelope = json.loads(out.read_bytes())
    # The pages stay verbatim; the only addition is the ingest evidence block.
    assert envelope.pop("ingest")["order_count"] == 3
    assert envelope == {"account_number": ACCOUNT, "trading_day": DAY, "pages": [p1, p2]}


def test_single_final_page_is_a_complete_history(tmp_path):
    p1 = _page([_order("o1")])
    code, out = _run(tmp_path, [_write(tmp_path, "p1.txt", p1)])
    assert code == 0
    assert json.loads(out.read_bytes())["pages"] == [p1]


def test_genuinely_empty_day_is_a_complete_history(tmp_path):
    code, out = _run(tmp_path, [_write(tmp_path, "p1.txt", _page([]))])
    assert code == 0
    assert json.loads(out.read_bytes())["pages"] == [{"orders": []}]


def test_mcp_text_content_block_wrapper_is_unwrapped(tmp_path):
    """The saved-output format is unverified; the one wrapper MCP itself
    defines (a list holding exactly one ``{"type": "text", "text": ...}``
    block) is unwrapped. Anything else still fails closed below."""
    p1 = _page([_order("o1")])
    wrapped = [{"type": "text", "text": json.dumps(p1)}]
    code, out = _run(tmp_path, [_write(tmp_path, "p1.txt", wrapped)])
    assert code == 0
    assert json.loads(out.read_bytes())["pages"] == [p1]


def test_snapshot_at_is_passed_through_when_supplied(tmp_path):
    """The 09:35 prompt's envelope carries ``snapshot_at``; the others do not."""
    stamp = f"{DAY}T13:40:05-04:00"
    code, out = _run(tmp_path, [_write(tmp_path, "p1.txt", _page([]))], extra=("--snapshot-at", stamp))
    assert code == 0
    assert json.loads(out.read_bytes())["snapshot_at"] == stamp


def test_summary_line_reports_page_and_order_counts(tmp_path, capsys):
    p1 = _page([_order("o1"), _order("o2")], next_url="https://api.example/orders/?cursor=P2")
    p2 = _page([_order("o3")])
    _run(tmp_path, [_write(tmp_path, "p1.txt", p1), _write(tmp_path, "p2.txt", p2)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["page_count"] == 2
    assert summary["order_count"] == 3


def test_output_round_trips_through_the_top_up_gate_binding(tmp_path):
    """The envelope must be exactly what same_day_top_up_gate accepts."""
    p1 = _page([_order("o1")])
    code, out = _run(tmp_path, [_write(tmp_path, "p1.txt", p1)])
    assert code == 0
    pages, err = gate._bind_saved_pages(json.loads(out.read_bytes()), account_number=ACCOUNT, trading_day=DAY)
    assert err is None
    assert pages == [p1]


# --- fail closed: every case exits 2 and writes nothing ---------------------------


def _assert_refused(tmp_path, page_files, reason, capsys, **kwargs):
    code, out = _run(tmp_path, page_files, **kwargs)
    assert code == 2
    assert not out.exists(), "a refused run must not leave an envelope behind"
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False
    assert summary["reason"] == reason


def test_truncated_page_file_is_refused(tmp_path, capsys):
    full = json.dumps(_page([_order("o1"), _order("o2")]))
    truncated = full[: len(full) // 2]
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", truncated)], "page_file_unparseable", capsys)


def test_duplicate_json_keys_are_refused(tmp_path, capsys):
    text = '{"orders": [], "orders": [{"id": "o1"}]}'
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", text)], "page_file_unparseable", capsys)


def test_bare_order_array_is_not_a_page(tmp_path, capsys):
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", [_order("o1")])], "page_not_an_orders_page", capsys)


def test_page_without_an_orders_list_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", {"result": "ok"})], "page_not_an_orders_page", capsys)


def test_text_block_wrapper_holding_non_json_is_refused(tmp_path, capsys):
    wrapped = [{"type": "text", "text": "Error: output too large"}]
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", wrapped)], "page_file_unparseable", capsys)


def test_missing_page_file_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, [str(tmp_path / "nope.txt")], "page_file_unreadable", capsys)


def test_chain_ending_on_a_continuation_pointer_is_incomplete(tmp_path, capsys):
    """The last supplied page still says there is more: pages were skipped."""
    p1 = _page([_order("o1")], next_url="https://api.example/orders/?cursor=P2")
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", p1)], "history_incomplete", capsys)


def test_pages_after_a_final_page_are_refused(tmp_path, capsys):
    """Out-of-order or duplicated saved pages must not read as a clean history."""
    final = _page([_order("o1")])
    extra = _page([_order("o2")])
    files = [_write(tmp_path, "p1.txt", final), _write(tmp_path, "p2.txt", extra)]
    _assert_refused(tmp_path, files, "unconsumed_saved_order_pages", capsys)


def test_no_page_files_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, [], "no_page_files", capsys)


@pytest.mark.parametrize("day", ["", "2026-9-9", "2026-13-01", "today"])
def test_malformed_trading_day_is_refused(tmp_path, capsys, day):
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([]))], "invalid_trading_day", capsys, day=day)


def test_blank_account_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([]))], "missing_account", capsys, account="  ")


def test_refusal_leaves_an_existing_envelope_untouched(tmp_path, capsys):
    """A failed re-assembly must not clobber (or truncate) earlier evidence."""
    out = tmp_path / "orders_pages_out.json"
    out.write_text('{"sentinel": true}', encoding="utf-8")
    code = asm.main(["--account", ACCOUNT, "--trading-day", DAY, "--out", str(out), _write(tmp_path, "p1.txt", "{")])
    assert code == 2
    assert out.read_text(encoding="utf-8") == '{"sentinel": true}'


def test_unwritable_out_path_is_refused(tmp_path, capsys):
    out = tmp_path / "no_such_dir" / "orders_pages_out.json"
    code = asm.main(
        ["--account", ACCOUNT, "--trading-day", DAY, "--out", str(out), _write(tmp_path, "p1.txt", _page([]))]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "out_unwritable"


# --- 2026-10-05: validated ingest, provenance and content hashes -------------------
#
# Contract added here (the implementation must match it):
#
# * Raw-field validation (placeholder ids, condensed captures, non-finite amounts) runs
#   BEFORE the pagination replay, so a hand-built capture is refused for what it is.
# * ``--expect-count N`` / ``--expect-total-usd X`` cross-check the history against the
#   response's own totals (the sum of ``dollar_based_amount.amount``, to half a cent);
#   a mismatch is refused. Omitted, the envelope records that nothing was cross-checked.
# * A page file of ``-`` reads stdin. Provenance is derived, never asserted: ``spilled_file``
#   when every page was a file, ``stdin_paste`` when any page came from stdin.
# * The envelope gains an ``ingest`` block; the pages themselves stay byte-for-byte what the
#   session supplied.

# The ~12 fields the 2026-10-05 10:30 session kept when it re-keyed the response by hand.
_CONDENSED_1030_KEYS = (
    "id", "symbol", "side", "type", "state", "quantity", "cumulative_quantity", "average_price",
    "dollar_based_amount", "market_hours", "placed_agent", "created_at",
)


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _ingest_block(tmp_path, page_files, **kwargs):
    code, out = _run(tmp_path, page_files, **kwargs)
    assert code == 0
    return json.loads(out.read_bytes())["ingest"]


def test_ingest_block_records_tool_counts_total_and_hashes(tmp_path, capsys):
    p1 = _page([_order("o1", usd="2.000000"), _order("o2", usd="3.500000")], next_url="https://api.example/orders/?cursor=P2")
    p2 = _page([_order("o3", usd="1.000000")])
    files = [_write(tmp_path, "p1.txt", p1), _write(tmp_path, "p2.txt", p2)]
    block = _ingest_block(tmp_path, files)
    assert block["tool"] == "scripts.assemble_order_pages"
    assert block["provenance"] == "spilled_file"
    assert block["order_count"] == 3
    assert block["total_usd"] == pytest.approx(6.5)
    assert block["pages_sha256"] == [hashlib.sha256(open(f, "rb").read()).hexdigest() for f in files]
    digest = hashlib.sha256(_canonical([p1, p2])).hexdigest()
    assert block["payload_sha256"] == digest
    summary = json.loads(capsys.readouterr().out)
    assert summary["payload_sha256"] == digest
    assert summary["provenance"] == "spilled_file"


def test_payload_hash_changes_when_one_order_value_changes(tmp_path):
    first = _ingest_block(tmp_path, [_write(tmp_path, "p1.txt", _page([_order("o1", usd="2.000000")]))])
    other = tmp_path / "other"
    other.mkdir()
    second = _ingest_block(other, [_write(other, "p1.txt", _page([_order("o1", usd="2.010000")]))])
    assert first["payload_sha256"] != second["payload_sha256"]


def test_stdin_page_is_recorded_as_stdin_paste(tmp_path, monkeypatch):
    page = _page([_order("o1")])
    text = json.dumps(page)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code, out = _run(tmp_path, ["-"])
    assert code == 0
    envelope = json.loads(out.read_bytes())
    assert envelope["pages"] == [page]
    assert envelope["ingest"]["provenance"] == "stdin_paste"
    assert envelope["ingest"]["pages_sha256"] == [hashlib.sha256(text.encode("utf-8")).hexdigest()]


def test_any_stdin_page_downgrades_a_mixed_chain_to_stdin_paste(tmp_path, monkeypatch):
    p1 = _page([_order("o1")], next_url="https://api.example/orders/?cursor=P2")
    p2 = _page([_order("o2")])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p2)))
    block = _ingest_block(tmp_path, [_write(tmp_path, "p1.txt", p1), "-"])
    assert block["provenance"] == "stdin_paste"


def test_two_stdin_pages_are_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_page([]))))
    _assert_refused(tmp_path, ["-", "-"], "multiple_stdin_inputs", capsys)


# --- placeholder ids ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["transcribed-00-AAPL", "PLACEHOLDER-1", "placeholder", "order-123", "o1", "", None, 12345],
)
def test_a_non_uuid_order_id_is_refused_as_a_placeholder(tmp_path, capsys, bad_id):
    """Real broker ids are canonical UUIDs; anything else was typed by a person or a model."""
    row = _order("o1")
    row["id"] = bad_id
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([_order("o0"), row]))], "placeholder_order_id", capsys)


def test_an_order_with_no_id_key_is_refused_as_a_placeholder(tmp_path, capsys):
    row = _order("o1")
    del row["id"]
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([row]))], "placeholder_order_id", capsys)


def test_an_uppercase_uuid_is_a_real_id(tmp_path):
    row = _order("o1")
    row["id"] = row["id"].upper()
    code, _ = _run(tmp_path, [_write(tmp_path, "p1.txt", _page([row]))])
    assert code == 0


def test_2026_10_05_1430_transcribed_ids_are_refused(tmp_path, capsys):
    """The 14:30 envelope: 36 full-width rows whose ids were ``transcribed-NN-SYMBOL``."""
    rows = []
    for index, symbol in enumerate(["AAPL", "TRLV", "GLW"]):
        row = _order(f"o{index}", symbol=symbol)
        row["id"] = f"transcribed-{index:02d}-{symbol}"
        rows.append(row)
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page(rows))], "placeholder_order_id", capsys)


# --- condensed captures ------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_key",
    ["ref_id", "time_in_force", "trigger", "market_hours", "placed_agent", "type", "symbol", "side", "state", "created_at"],
)
def test_an_order_missing_a_creation_time_field_is_a_condensed_capture(tmp_path, capsys, missing_key):
    """Fields fixed when an order is created are present on every raw row, filled or not."""
    row = _order("o1")
    del row[missing_key]
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([_order("o0"), row]))], "condensed_capture", capsys)
    # (the detail naming the field is asserted separately, once, below)


def test_the_refusal_detail_names_the_missing_field(tmp_path, capsys):
    row = _order("o1")
    del row["ref_id"]
    code, _ = _run(tmp_path, [_write(tmp_path, "p1.txt", _page([row]))])
    assert code == 2
    assert "ref_id" in json.loads(capsys.readouterr().out)["detail"]


def test_2026_10_05_1030_condensed_capture_shape_is_refused(tmp_path, capsys):
    """The 10:30 envelope: real UUID ids, but only the ~12 fields the session chose to re-key."""
    condensed = [{k: v for k, v in _order(f"o{i}").items() if k in _CONDENSED_1030_KEYS} for i in range(3)]
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page(condensed))], "condensed_capture", capsys)


def test_a_null_ref_id_is_still_a_raw_row(tmp_path):
    """The key must exist (a condensed capture drops it); its value may be null."""
    row = _order("o1")
    row["ref_id"] = None
    code, _ = _run(tmp_path, [_write(tmp_path, "p1.txt", _page([row]))])
    assert code == 0


# --- non-finite amounts ------------------------------------------------------------


# ``True``/``False`` are JSON booleans, not amounts: ``bool`` subclasses ``int``, so a bare
# ``float(amount)`` would read ``true`` as $1 and fold it into the evidence total (Copilot
# review, PR #1609).
@pytest.mark.parametrize("amount", ["NaN", "nan", "inf", "-inf", "abc", True, False])
def test_a_non_finite_dollar_amount_is_refused(tmp_path, capsys, amount):
    _assert_refused(
        tmp_path, [_write(tmp_path, "p1.txt", _page([_order("o1", usd=amount)]))], "non_finite_value", capsys
    )


def test_a_bare_nan_token_in_a_page_file_is_unparseable(tmp_path, capsys):
    text = json.dumps(_page([_order("o1")])).replace('"amount": "2.000000"', '"amount": NaN')
    assert "NaN" in text
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", text)], "page_file_unparseable", capsys)


# --- cross-check against the response's own count and total -------------------------


def _three_orders(tmp_path):
    page = _page([_order("o1", usd="2.000000"), _order("o2", usd="3.500000"), _order("o3", usd="1.000000")])
    return [_write(tmp_path, "p1.txt", page)]


def test_without_expectations_the_envelope_records_that_nothing_was_cross_checked(tmp_path):
    block = _ingest_block(tmp_path, _three_orders(tmp_path))
    assert block["cross_check"] == {"expected_count": None, "expected_total_usd": None, "matched": None}


def test_matching_count_and_total_are_recorded_as_a_passed_cross_check(tmp_path):
    block = _ingest_block(
        tmp_path, _three_orders(tmp_path), extra=("--expect-count", "3", "--expect-total-usd", "6.50")
    )
    assert block["cross_check"] == {"expected_count": 3, "expected_total_usd": 6.5, "matched": True}


def test_a_count_mismatch_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, _three_orders(tmp_path), "count_mismatch", capsys, extra=("--expect-count", "4"))


def test_a_total_mismatch_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, _three_orders(tmp_path), "total_mismatch", capsys, extra=("--expect-total-usd", "7.00"))


def test_a_total_off_by_a_cent_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, _three_orders(tmp_path), "total_mismatch", capsys, extra=("--expect-total-usd", "6.51"))


def test_the_2026_10_05_1030_thirty_orders_for_82_dollars_cross_check(tmp_path):
    """The figure the 10:30 session verified by hand: 30 orders, $82 total."""
    amounts = [5] + [4] * 6 + [3] * 7 + [2] * 16  # the real ladder: 30 rows summing to 82 dollars
    assert (len(amounts), sum(amounts)) == (30, 82)
    page = _page([_order(f"o{i}", symbol=f"T{i:02d}", usd=f"{a}.000000") for i, a in enumerate(amounts)])
    block = _ingest_block(
        tmp_path, [_write(tmp_path, "p1.txt", page)], extra=("--expect-count", "30", "--expect-total-usd", "82")
    )
    assert block["order_count"] == 30
    assert block["total_usd"] == pytest.approx(82.0)
    assert block["cross_check"]["matched"] is True


def _ten_dime_orders(tmp_path):
    """Ten $0.10 buys: the float sum is 0.9999999999999999, the decimal sum exactly 1.00."""
    page = _page([_order(f"o{i}", symbol=f"T{i:02d}", usd="0.100000") for i in range(10)])
    return [_write(tmp_path, "p1.txt", page)]


def test_a_total_that_only_matches_in_decimal_arithmetic_is_accepted(tmp_path):
    """Summing the amounts as floats gives 0.9999999999999999; the cross-check must not care."""
    block = _ingest_block(tmp_path, _ten_dime_orders(tmp_path), extra=("--expect-total-usd", "1.00"))
    assert block["cross_check"]["matched"] is True


@pytest.mark.parametrize("expected", ["1.004", "0.996"])
def test_a_total_within_half_a_cent_matches(tmp_path, expected):
    """The documented tolerance is half a cent: a difference below $0.005 is a match."""
    block = _ingest_block(tmp_path, _ten_dime_orders(tmp_path), extra=("--expect-total-usd", expected))
    assert block["cross_check"]["matched"] is True


@pytest.mark.parametrize("expected", ["1.006", "0.994"])
def test_a_total_beyond_half_a_cent_is_refused(tmp_path, capsys, expected):
    """A looser one-cent tolerance would accept these; the contract is half a cent."""
    _assert_refused(
        tmp_path, _ten_dime_orders(tmp_path), "total_mismatch", capsys, extra=("--expect-total-usd", expected)
    )


# --- nested pages, odd rows and argument parsing ----------------------------------------


def _nested(orders):
    return {"data": {"orders": orders}}


def test_orders_nested_under_data_are_validated_too(tmp_path, capsys):
    """The raw tool response wraps its rows in ``data``; a bad row there must not slip past."""
    bad = _order("o1")
    bad["id"] = "transcribed-00-AAPL"
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _nested([bad]))], "placeholder_order_id", capsys)


def test_a_healthy_nested_page_is_accepted_and_counted(tmp_path):
    block = _ingest_block(tmp_path, [_write(tmp_path, "p1.txt", _nested([_order("o1"), _order("o2", usd="3.000000")]))])
    assert block["order_count"] == 2
    assert block["total_usd"] == pytest.approx(5.0)


def test_an_order_row_that_is_not_an_object_is_refused(tmp_path, capsys):
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page(["AAPL"]))], "page_not_an_orders_page", capsys)


def test_a_dollar_amount_object_with_no_amount_is_refused(tmp_path, capsys):
    row = _order("o1")
    row["dollar_based_amount"] = {"currency_code": "USD"}
    _assert_refused(tmp_path, [_write(tmp_path, "p1.txt", _page([row]))], "non_finite_value", capsys)


def test_a_quantity_based_order_counts_but_adds_no_dollars(tmp_path):
    row = _order("o2")
    row["dollar_based_amount"] = None
    block = _ingest_block(tmp_path, [_write(tmp_path, "p1.txt", _page([_order("o1", usd="2.000000"), row]))])
    assert block["order_count"] == 2
    assert block["total_usd"] == pytest.approx(2.0)


@pytest.mark.parametrize("value", ["NaN", "inf", "abc"])
def test_a_non_finite_expected_total_is_a_usage_error(tmp_path, value):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, [_write(tmp_path, "p1.txt", _page([]))], extra=("--expect-total-usd", value))
    assert exc.value.code == 2
