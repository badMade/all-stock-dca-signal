"""Tests for ``scripts/merge_order_pages.py`` (written before the implementation).

Why this script exists (2026-10-28 and 2026-10-29, 09:35 ET): the live prompt re-reads the
whole day's order history before EVERY order. The page grows with each order, so 18 orders
cost roughly 360,000 tokens of tool output, the session was compacted mid-batch on 10-29,
and on both days the run quietly narrowed its own fetches to cope (a ``symbol``/``state``
filter, then a ``created_at_gte`` of "13:52 UTC"). The run reported each narrowing as a
deviation, because the prompt had no legitimate way to read less.

The incremental form: one full fetch at batch start, saved with ``assemble_order_pages``;
before each later order, fetch only orders created since the last snapshot (with an
overlap) and hand the saved page files to THIS script, which merges them into the base
envelope. Caps, the Gate 4 rescan and the top-up guard then read the merged envelope, so
they still see the complete history. The merge must fail CLOSED (exit 2, write nothing) on
anything that could hide an order, because an undercount of spend is the failure that costs
money:

  * the increment's ``created_at_gte`` is later than ``snapshot_at`` minus the overlap, so
    an order created in the gap could be missing from both;
  * an order the base already holds inside the increment's window is absent from the
    increment, so the two views of the same period disagree;
  * the increment has an unfollowed pagination cursor, a placeholder id, or a condensed row;
  * the account or trading day does not match the base.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import merge_order_pages as merge  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
# Derived, never written as a literal: tests/test_config_invariants.py takes no account-shaped literal other than
# the synthetic ACCOUNT (use conftest.FIXTURE_ACCOUNT_NUMBER or keep a number to 5 digits or fewer).
OTHER_ACCOUNT = str(int(ACCOUNT) + 1)
DAY = "2026-10-29"
OVERLAP = 120


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-order/{label}"))


def _order(label, hhmmss, *, symbol="SPCX", usd="2.000000", state="filled"):
    """A raw ``get_equity_orders`` row with every creation-time field and a UUID id."""
    return {
        "id": _uuid(label),
        "ref_id": _uuid(f"ref/{label}"),
        "symbol": symbol,
        "side": "buy",
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
        "created_at": f"{DAY}T{hhmmss}+00:00",
        "last_transaction_at": f"{DAY}T{hhmmss}+00:00",
    }


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def _base(tmp_path, orders, *, snapshot_at=f"{DAY}T14:10:00+00:00", account=ACCOUNT, day=DAY):
    envelope = {
        "account_number": account,
        "trading_day": day,
        "snapshot_at": snapshot_at,
        "pages": [{"orders": orders}],
    }
    return _write(tmp_path, "base.json", envelope)


def _run(tmp_path, base, pages, *, gte=f"{DAY}T14:08:00+00:00", snapshot_at=f"{DAY}T14:12:00+00:00", **extra):
    out = tmp_path / "merged.json"
    argv = [
        "--base", base,
        "--account", extra.get("account", ACCOUNT),
        "--trading-day", extra.get("day", DAY),
        "--created-at-gte", gte,
        "--snapshot-at", snapshot_at,
        "--overlap-seconds", str(OVERLAP),
        "--out", str(out),
        *pages,
    ]
    return merge.main(argv), out


def _ids(envelope):
    return [row["id"] for page in envelope["pages"] for row in page["orders"]]


def test_new_orders_are_added_to_the_base_and_the_snapshot_moves_forward(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00"), _order("o2", "14:05:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o3", "14:09:00")]})

    code, out = _run(tmp_path, base, [page])

    assert code == 0
    merged = json.loads(out.read_text())
    assert sorted(_ids(merged)) == sorted(_uuid(x) for x in ("o1", "o2", "o3"))
    assert merged["snapshot_at"] == f"{DAY}T14:12:00+00:00"
    assert merged["account_number"] == ACCOUNT and merged["trading_day"] == DAY


def test_merged_envelope_is_a_valid_input_for_the_top_up_gate(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o2", "14:09:00")]})
    code, out = _run(tmp_path, base, [page], gte=f"{DAY}T14:08:00+00:00")
    assert code == 0
    merged = json.loads(out.read_text())
    # Same shape the gates already consume: no cursor left open on any page.
    assert set(merged) >= {"account_number", "trading_day", "snapshot_at", "pages"}
    assert all("next" not in page or not page["next"] for page in merged["pages"])


def test_an_order_in_both_views_is_kept_once_and_the_newer_state_wins(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00"), _order("o2", "14:09:00", state="unconfirmed")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o2", "14:09:00", state="filled")]})

    code, out = _run(tmp_path, base, [page])

    assert code == 0
    merged = json.loads(out.read_text())
    ids = _ids(merged)
    assert ids.count(_uuid("o2")) == 1
    row = next(r for p in merged["pages"] for r in p["orders"] if r["id"] == _uuid("o2"))
    assert row["state"] == "filled"


def test_merge_records_that_it_is_a_merge_and_what_it_merged(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o2", "14:09:00")]})
    code, out = _run(tmp_path, base, [page])
    assert code == 0
    ingest = json.loads(out.read_text())["ingest"]
    assert ingest["merged"] is True
    assert ingest["base_order_count"] == 1 and ingest["increment_order_count"] == 1
    assert ingest["created_at_gte"] == f"{DAY}T14:08:00+00:00"


def test_output_is_deterministic_for_the_same_inputs(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00"), _order("o2", "14:05:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o3", "14:09:00")]})
    _, out1 = _run(tmp_path, base, [page])
    first = out1.read_bytes()
    out1.unlink()
    _, out2 = _run(tmp_path, base, [page])
    assert out2.read_bytes() == first


# --- refusals: each exits 2 and writes nothing ---------------------------------------


def test_refuses_an_increment_window_that_starts_after_the_overlap(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])  # snapshot_at 14:10:00
    page = _write(tmp_path, "p1.json", {"orders": []})
    # 14:09:00 is later than snapshot_at - 120 s (14:08:00): an order at 14:08:30 could be missed.
    code, out = _run(tmp_path, base, [page], gte=f"{DAY}T14:09:00+00:00")
    assert code == 2
    assert not out.exists()


def test_accepts_an_increment_window_that_starts_exactly_at_the_overlap_boundary(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": []})
    code, _ = _run(tmp_path, base, [page], gte=f"{DAY}T14:08:00+00:00")
    assert code == 0


def test_refuses_when_a_base_order_inside_the_window_is_missing_from_the_increment(tmp_path):
    # o2 (14:09) is in the base and inside the 14:08 window, but the increment does not list it.
    base = _base(tmp_path, [_order("o1", "14:00:00"), _order("o2", "14:09:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o3", "14:11:00")]})
    code, out = _run(tmp_path, base, [page])
    assert code == 2
    assert not out.exists()


def test_refuses_an_increment_with_an_unfollowed_cursor(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": [_order("o2", "14:09:00")], "next": "https://api.example/orders/?cursor=P2"})
    code, out = _run(tmp_path, base, [page])
    assert code == 2
    assert not out.exists()


def test_refuses_a_placeholder_order_id(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    row = _order("o2", "14:09:00")
    row["id"] = "transcribed-00-AAPL"
    page = _write(tmp_path, "p1.json", {"orders": [row]})
    code, out = _run(tmp_path, base, [page])
    assert code == 2
    assert not out.exists()


def test_refuses_a_condensed_row(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    row = _order("o2", "14:09:00")
    del row["ref_id"]
    page = _write(tmp_path, "p1.json", {"orders": [row]})
    code, out = _run(tmp_path, base, [page])
    assert code == 2
    assert not out.exists()


def test_refuses_an_unparseable_page(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    bad = tmp_path / "p1.json"
    bad.write_text('{"orders": [', encoding="utf-8")
    code, out = _run(tmp_path, base, [str(bad)])
    assert code == 2
    assert not out.exists()


@pytest.mark.parametrize("override", [{"account": OTHER_ACCOUNT}, {"day": "2026-10-28"}])
def test_refuses_an_account_or_trading_day_that_does_not_match_the_base(tmp_path, override):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": []})
    code, out = _run(tmp_path, base, [page], **override)
    assert code == 2
    assert not out.exists()


def test_a_refusal_leaves_an_existing_output_file_untouched(tmp_path):
    base = _base(tmp_path, [_order("o1", "14:00:00")])
    page = _write(tmp_path, "p1.json", {"orders": [], "next": "https://api.example/?cursor=x"})
    out = tmp_path / "merged.json"
    out.write_text("previous good content", encoding="utf-8")
    code, _ = _run(tmp_path, base, [page])
    assert code == 2
    assert out.read_text() == "previous good content"
