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
"""

from __future__ import annotations

import json

import pytest

from scripts import assemble_order_pages as asm
from scripts import same_day_top_up_gate as gate

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
DAY = "2026-09-09"


def _order(order_id, symbol="SPCX", usd="2.000000"):
    return {
        "id": order_id,
        "symbol": symbol,
        "side": "buy",
        "state": "filled",
        "dollar_based_amount": {"amount": usd, "currency_code": "USD"},
        "created_at": f"{DAY}T14:00:00+00:00",
        "placed_agent": "agentic",
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
