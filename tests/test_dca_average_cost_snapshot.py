"""Tests for dca_average_cost_snapshot.py."""

import json
from pathlib import Path

from dca_average_cost_snapshot import (
    AVERAGE_COST_SNAPSHOT_FILENAME,
    build_average_cost_snapshot,
    write_average_cost_snapshot,
)

ACCOUNTS = [
    {
        "account_number": "999000111",
        "nickname": None,
        "brokerage_account_type": "individual",
        "type": "margin",
        "is_default": True,
    },
    {
        "account_number": "123456789",
        "nickname": "Agentic",
        "brokerage_account_type": "individual",
        "type": "cash",
        "is_default": False,
    },
]


def test_build_average_cost_snapshot_combines_across_accounts():
    positions_by_account = {
        "999000111": [{"symbol": "aapl", "quantity": "2", "average_buy_price": "100.00"}],
        "123456789": [{"symbol": "AAPL", "quantity": "1", "average_buy_price": "130.00"}],
    }

    snapshot = build_average_cost_snapshot(
        ACCOUNTS, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
    )

    assert snapshot["generated_at"] == "2026-08-18T09:00:00-04:00"
    assert snapshot["account_count"] == 2

    default_account = snapshot["accounts"]["999000111"]
    assert default_account["is_default"] is True
    assert default_account["positions"] == [
        {"ticker": "AAPL", "quantity": 2.0, "average_cost_usd": 100.0}
    ]

    agentic_account = snapshot["accounts"]["123456789"]
    assert agentic_account["nickname"] == "Agentic"
    assert agentic_account["positions"] == [
        {"ticker": "AAPL", "quantity": 1.0, "average_cost_usd": 130.0}
    ]

    # Quantity-weighted: (2*100 + 1*130) / 3 = 110.0
    combined = snapshot["combined_by_ticker"]["AAPL"]
    assert combined["total_quantity"] == 3.0
    assert combined["average_cost_usd"] == 110.0
    assert combined["account_count"] == 2


def test_build_average_cost_snapshot_skips_closed_and_unresolvable_positions():
    positions_by_account = {
        "999000111": [
            {"symbol": "MSFT", "quantity": "0", "average_buy_price": "300.00"},
            {"symbol": None, "quantity": "5", "average_buy_price": "10.00"},
            {"symbol": "TSLA", "quantity": "not-a-number", "average_buy_price": "200.00"},
        ]
    }

    snapshot = build_average_cost_snapshot(
        ACCOUNTS, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
    )

    assert snapshot["accounts"]["999000111"]["positions"] == []
    assert snapshot["combined_by_ticker"] == {}


def test_build_average_cost_snapshot_counts_unpriced_quantity_but_not_its_average_cost():
    positions_by_account = {
        "999000111": [{"symbol": "GME", "quantity": "3", "average_buy_price": None}],
    }

    snapshot = build_average_cost_snapshot(
        ACCOUNTS, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
    )

    assert snapshot["accounts"]["999000111"]["positions"] == [
        {"ticker": "GME", "quantity": 3.0, "average_cost_usd": None}
    ]
    # total_quantity/account_count reflect real held shares even with no
    # resolvable price; average_cost_usd has nothing priced to divide, so
    # it is None rather than a fabricated 0.0 or a ZeroDivisionError.
    combined = snapshot["combined_by_ticker"]["GME"]
    assert combined["total_quantity"] == 3.0
    assert combined["average_cost_usd"] is None
    assert combined["account_count"] == 1


def test_build_average_cost_snapshot_excludes_unpriced_quantity_from_weighted_average_only():
    positions_by_account = {
        "999000111": [{"symbol": "IBM", "quantity": "2", "average_buy_price": "100.00"}],
        "123456789": [{"symbol": "IBM", "quantity": "3", "average_buy_price": None}],
    }

    snapshot = build_average_cost_snapshot(
        ACCOUNTS, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
    )

    combined = snapshot["combined_by_ticker"]["IBM"]
    # Held quantity and account_count count BOTH accounts (5 shares, 2
    # accounts) even though only one account's shares are priced.
    assert combined["total_quantity"] == 5.0
    assert combined["account_count"] == 2
    # The weighted average is over the priced 2 shares only: 2*100 / 2 = 100.
    assert combined["average_cost_usd"] == 100.0


def test_build_average_cost_snapshot_rejects_non_finite_numbers():
    positions_by_account = {
        "999000111": [
            {"symbol": "NAN1", "quantity": "NaN", "average_buy_price": "100.00"},
            {"symbol": "INF1", "quantity": "2", "average_buy_price": "Infinity"},
        ],
    }

    snapshot = build_average_cost_snapshot(
        ACCOUNTS, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
    )

    # NaN quantity is treated as unresolvable, so the position is dropped
    # (never silently treated as "0 shares" or passed through `<= 0`).
    assert snapshot["accounts"]["999000111"]["positions"] == [
        {"ticker": "INF1", "quantity": 2.0, "average_cost_usd": None}
    ]
    assert "NAN1" not in snapshot["combined_by_ticker"]
    # Infinite cost is treated the same as unresolvable: listed with the
    # real quantity but no average_cost_usd, never an "Infinity" JSON token.
    combined = snapshot["combined_by_ticker"]["INF1"]
    assert combined["total_quantity"] == 2.0
    assert combined["average_cost_usd"] is None


def test_build_average_cost_snapshot_omits_unfetched_accounts():
    snapshot = build_average_cost_snapshot(
        ACCOUNTS, {"999000111": []}, generated_at="2026-08-18T09:00:00-04:00"
    )

    assert snapshot["account_count"] == 1
    assert "123456789" not in snapshot["accounts"]


def test_write_average_cost_snapshot_writes_json_atomically(tmp_path: Path):
    snapshot = build_average_cost_snapshot(
        ACCOUNTS,
        {"999000111": [{"symbol": "AAPL", "quantity": "1", "average_buy_price": "100.0"}]},
        generated_at="2026-08-18T09:00:00-04:00",
    )

    written_path = write_average_cost_snapshot(snapshot, output_dir=tmp_path)

    assert written_path == tmp_path / AVERAGE_COST_SNAPSHOT_FILENAME
    on_disk = json.loads(written_path.read_text(encoding="utf-8"))
    assert on_disk == snapshot


def test_write_average_cost_snapshot_creates_output_dir(tmp_path: Path):
    nested = tmp_path / "nested" / "dir"
    snapshot = build_average_cost_snapshot(ACCOUNTS, {}, generated_at="2026-08-18T09:00:00-04:00")

    written_path = write_average_cost_snapshot(snapshot, output_dir=nested)

    assert written_path.exists()
