"""Tests for ``scripts/check_per_order_record.py`` (written before the implementation).

Why: on 2026-10-28 and 2026-10-29 the 09:35 run skipped per-order checks the prompt makes
mandatory (the weekly-history refetch, a per-ticker tradability check, a full-history rescan)
and said so only in its closing summary. Its stated reason was "budget headroom is wide", a
judgement the prompt does not allow. A prompt rule alone did not stop it twice, so the rule
now has a deterministic check: for every order the run placed, it must have written one line
to ``per_order_checks.jsonl`` recording each check, and this script fails the slot when a
line is missing or incomplete. The record is self-reported, so it cannot prove a check ran;
what it does is turn a silent omission into a named failure the Gate 9 alert must carry.

Contract:
  * one record per placed order, matched on ``order_id``;
  * every key present and non-empty; timestamps offset-aware ISO-8601;
  * ``history_mode`` is ``full`` (the first order, after the baseline fetch) or ``incremental``
    (every later order), and an incremental record carries ``merged_history_sha256``;
  * batched tradability is allowed, but its timestamp may not be more than 300 seconds older
    than the order's own clock check;
  * no duplicate records and no record for an order that was not placed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import check_per_order_record as cpor  # noqa: E402

DAY = "2026-10-29"


def _record(ticker, order_id, *, mode="incremental", clock="13:55:00", tradability="13:50:00", **overrides):
    record = {
        "ticker": ticker,
        "order_id": order_id,
        "history_mode": mode,
        "merged_history_sha256": "a" * 64 if mode == "incremental" else "",
        "clock_checked_at": f"{DAY}T{clock}+00:00",
        "tradability_checked_at": f"{DAY}T{tradability}+00:00",
        "spread_checked_at": f"{DAY}T{clock}+00:00",
        "price_checked_at": f"{DAY}T{clock}+00:00",
    }
    record.update(overrides)
    return record


def _order(ticker, order_id):
    return {"ticker": ticker, "order_id": order_id, "submitted_amount_usd": 2.0, "status": "filled"}


ORDERS = [_order("AAPL", "id-1"), _order("MSFT", "id-2")]


def _complete():
    return [_record("AAPL", "id-1", mode="full"), _record("MSFT", "id-2")]


def test_complete_records_pass():
    assert cpor.evaluate(ORDERS, _complete()) == []


def test_no_orders_and_no_records_pass():
    assert cpor.evaluate([], []) == []


def test_an_order_without_a_record_fails_and_names_the_ticker():
    failures = cpor.evaluate(ORDERS, [_record("AAPL", "id-1", mode="full")])
    assert any("MSFT" in f and "no per-order record" in f for f in failures)


@pytest.mark.parametrize(
    "key",
    ["history_mode", "clock_checked_at", "tradability_checked_at", "spread_checked_at", "price_checked_at"],
)
def test_a_missing_or_empty_key_fails(key):
    records = _complete()
    records[1][key] = ""
    assert any(key in f for f in cpor.evaluate(ORDERS, records))
    del records[1][key]
    assert any(key in f for f in cpor.evaluate(ORDERS, records))


def test_an_incremental_record_must_carry_the_merged_history_hash():
    records = _complete()
    records[1]["merged_history_sha256"] = ""
    assert any("merged_history_sha256" in f for f in cpor.evaluate(ORDERS, records))


def test_an_unknown_history_mode_fails():
    records = _complete()
    records[1]["history_mode"] = "skipped"
    assert any("history_mode" in f for f in cpor.evaluate(ORDERS, records))


def test_a_naive_timestamp_fails():
    records = _complete()
    records[1]["price_checked_at"] = f"{DAY}T13:55:00"
    assert any("price_checked_at" in f for f in cpor.evaluate(ORDERS, records))


def test_batched_tradability_up_to_300_seconds_old_is_accepted():
    records = [_record("AAPL", "id-1", mode="full", clock="13:55:00", tradability="13:50:00"), _record("MSFT", "id-2")]
    assert cpor.evaluate(ORDERS, records) == []


def test_tradability_older_than_300_seconds_fails():
    records = [_record("AAPL", "id-1", mode="full", clock="13:55:01", tradability="13:50:00"), _record("MSFT", "id-2")]
    failures = cpor.evaluate(ORDERS, records)
    assert any("AAPL" in f and "tradability" in f and "300" in f for f in failures)


def test_a_duplicate_record_fails():
    records = _complete() + [_record("MSFT", "id-2")]
    assert any("duplicate" in f for f in cpor.evaluate(ORDERS, records))


def test_a_record_for_an_order_that_was_not_placed_fails():
    records = _complete() + [_record("NVDA", "id-9")]
    assert any("NVDA" in f and "not placed" in f for f in cpor.evaluate(ORDERS, records))


# --- command line -----------------------------------------------------------------------


def _write_run(tmp_path, orders, records_text):
    (tmp_path / "finalize_orders.json").write_text(json.dumps(orders), encoding="utf-8")
    if records_text is not None:
        (tmp_path / "per_order_checks.jsonl").write_text(records_text, encoding="utf-8")


def test_cli_passes_a_complete_run(tmp_path, capsys):
    _write_run(tmp_path, ORDERS, "\n".join(json.dumps(r) for r in _complete()) + "\n")
    assert cpor.main(["--run-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_fails_when_orders_were_placed_and_the_record_file_is_missing(tmp_path, capsys):
    _write_run(tmp_path, ORDERS, None)
    assert cpor.main(["--run-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_passes_when_no_orders_were_placed_and_there_is_no_record_file(tmp_path):
    _write_run(tmp_path, [], None)
    assert cpor.main(["--run-dir", str(tmp_path)]) == 0


def test_cli_fails_on_a_malformed_record_line(tmp_path):
    _write_run(tmp_path, ORDERS, json.dumps(_complete()[0]) + "\n{not json\n")
    assert cpor.main(["--run-dir", str(tmp_path)]) == 1


def test_cli_fails_when_the_orders_file_is_missing(tmp_path):
    (tmp_path / "per_order_checks.jsonl").write_text("", encoding="utf-8")
    assert cpor.main(["--run-dir", str(tmp_path)]) == 1
