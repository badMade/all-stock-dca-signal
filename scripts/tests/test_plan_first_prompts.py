"""Draft prompt size and secret/tool boundary checks."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2] / "claude/dca-executor-prompts/drafts/plan-first"
READERS = sorted(ROOT.glob("reader-*.draft.txt"))


def test_all_drafts_are_compact_and_price_blind():
    for path in ROOT.glob("*.draft.txt"):
        text = path.read_text()
        assert (len(text) + 3) // 4 <= 2000
        assert "999000111" not in text
        assert "yfinance" not in text.lower()


def test_there_is_one_reader_per_live_slot():
    assert [re.search(r"reader-(\d{4})", p.name).group(1) for p in READERS] == [
        "0935", "1030", "1130", "1230", "1330", "1430", "1530"]


def test_readers_gate_every_order_and_save_the_check_verdict():
    # PR #1667 review: re-check the clock and fractional tradability before EACH order, and save
    # validate_native_placement's verdict, which `finish` requires before counting an order.
    for path in READERS:
        slot = re.search(r"reader-(\d{4})", path.name).group(1)
        text = path.read_text()
        assert f"plan_first_slot clock --slot {slot}" in text
        assert "get_equity_tradability" in text
        assert "> <save_to>/<TICKER>.check.json" in text
        assert "get_equity_quotes" not in text and "get_equity_historicals" not in text
