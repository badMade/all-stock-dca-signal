"""The 3:30 PM prompt reads the Mac-built Robinhood payload at Gate 5, exactly as the six other slots do.

The 1530ET Gate 5 told the session to fetch Finance and Robinhood bars itself. Scheduled sessions have no Finance
connector and cannot write Robinhood tool output into the checkout without hand transcription, so the slot aborted at
Gate 5 on 2026-10-19, 10-20, 10-21, 10-23 and 10-29. The 15:31 build (com.all-stock-dca.robinhood-payload-1530-shadow)
published a payload in 220-264 s on all five shadow days from 10-23 to 10-29, which is the step-2b go/no-go in
docs/robinhood_payload.md. The payload is Robinhood-only (no sources.finance key), so it relies on the 1530ET
finance_outage_fallback that scripts/tests/test_generate_buy_candidates.py already exercises.
"""

import json
import re
from pathlib import Path

import pytest

from scripts import robinhood_payload as rp

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / "claude" / "dca-executor-prompts" / "live-mirror"

# The 1530ET live-mirror prompt still hand-fetches bars; changing a live prompt needs operator approval. Strict xfail
# keeps CI green until then and fails loudly (XPASS) once the prompt is updated, so the marker must be removed.
PROMPT_NOT_YET_UPDATED = pytest.mark.xfail(
    strict=True, reason="1530ET live prompt not yet switched to the prebuilt Robinhood payload (awaiting operator approval)"
)


def _prompt(slot: str) -> str:
    (path,) = MIRROR.glob(f"{slot}ET-*.txt")
    return path.read_text(encoding="utf-8")


def _errored_rule(text: str) -> str:
    start = text.index("After copying, read its ticker_errors.robinhood")
    return text[start : text.index("\n", start)]


@PROMPT_NOT_YET_UPDATED
def test_1530_gate5_reads_the_prebuilt_payload_and_never_hand_fetches():
    text = _prompt("1530")
    assert "PREBUILT ROBINHOOD PAYLOAD" in text
    assert "scripts.robinhood_payload plan --slot 1530" in text
    assert "scripts.robinhood_payload check" in text
    assert "do NOT hand-fetch bars" in text


@PROMPT_NOT_YET_UPDATED
def test_1530_no_longer_asks_the_session_to_call_the_finance_connector():
    text = _prompt("1530")
    assert "call the Finance connector itself" not in text
    assert "Separately call Robinhood get_equity_historicals" not in text


@PROMPT_NOT_YET_UPDATED
def test_1530_waits_as_long_as_the_six_slots_for_the_payload():
    waits = {slot: re.findall(r"every 30 seconds for up to (\d+) minutes", _prompt(slot)) for slot in (*rp.SLOTS, "1530")}
    assert all(len(found) == 1 for found in waits.values()), waits
    assert len({found[0] for found in waits.values()}) == 1, waits


@PROMPT_NOT_YET_UPDATED
def test_1530_applies_the_same_errored_and_quiet_ticker_rule_as_the_six_slots():
    six = {_errored_rule(_prompt(slot)) for slot in rp.SLOTS}
    assert len(six) == 1
    rule = _errored_rule(_prompt("1530"))
    assert rule in six
    assert '"quiet"' in rule and ".meta.json" in rule


@PROMPT_NOT_YET_UPDATED
def test_1530_still_fails_closed_when_the_payload_is_missing_or_stale():
    text = _prompt("1530")
    gate5 = text[text.index("GATE 5 - BUY LIST") : text.index("GATE 6 - BUDGET")]
    assert "--error-class market_data_unavailable" in gate5


def test_the_robinhood_only_payload_still_has_the_1530_finance_outage_fallback():
    policy = json.loads((ROOT / "config" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "1530ET" in node and isinstance(node["1530ET"], dict) and "finance_outage_fallback" in node["1530ET"]:
                found.append(node["1530ET"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(policy)
    assert [entry["finance_outage_fallback"] for entry in found] == ["robinhood_only"]
