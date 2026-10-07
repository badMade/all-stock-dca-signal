"""Guards for the committed Perplexity backup-task prompt mirrors.

claude/perplexity-prompts/ holds the CORRECTED text of the Perplexity backup
tasks (Perplexity cannot read this repo, so a human pastes them). These tests
pin the properties that have already cost real runs or would leak a secret,
the Perplexity counterpart of the live-mirror protections:

* 2026-10-06: the 12:50 pass rejected eleven tickers as "already bought today",
  several 0.5%-3% cheaper than their earlier fill, because the prompt carried a
  whole-day duplicate rule and no same-day top-up guard.
* This repo is public: no account number, no email address.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIRRORS = sorted((ROOT / "claude" / "perplexity-prompts").glob("*.txt"))
GUARD_DOC = ROOT / "claude" / "PERPLEXITY-SAME-DAY-TOPUP-GUARD.md"

# Verbatim fragments of the old whole-day rule. Any one of them returning
# re-creates the 2026-10-06 failure.
BANNED = (
    "never duplicate a ticker",
    "no duplicate same-day order",
    "no earlier same-day buy for that ticker by any runner",
    "already received a buy today",
)


def _guard_block() -> str:
    doc = GUARD_DOC.read_text(encoding="utf-8")
    block = doc[doc.index("> ### SAME-DAY TOP-UP GUARD") : doc.index("**Known limitation in Step 2")]
    return "\n".join(re.sub(r"^> ?", "", line) for line in block.rstrip().splitlines()).strip()


def test_six_mirrors_are_present():
    assert len(MIRRORS) == 6, [m.name for m in MIRRORS]


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_no_whole_day_duplicate_rule(path):
    text = path.read_text(encoding="utf-8").lower()
    for phrase in BANNED:
        assert phrase not in text, f"{path.name} still carries the old rule: {phrase!r}"


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_top_up_guard_is_verbatim_and_fail_closed(path):
    text = path.read_text(encoding="utf-8")
    assert _guard_block() in text, f"{path.name} guard block drifted from {GUARD_DOC.name}"
    assert "place NOTHING" in text
    # Broker minimum: a sub-$1 difference is held, never rounded up or sent.
    assert "below $1.00" in text and "Do NOT round it up" in text


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_same_window_exclusion_and_cross_slot_repeat_are_both_stated(path):
    text = path.read_text(encoding="utf-8")
    assert "same slot window" in text.lower() or "same window" in text.lower()
    assert "different slot" in text
    assert "one order per ticker per run" in text


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_no_secrets_in_a_public_repo(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\d{6,}", text), f"{path.name} holds an account-shaped number"
    assert "@" not in text, f"{path.name} holds an email address"
    assert "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" in text
    assert "${DCA_ALERT_EMAIL_ADDRESS}" in text
    assert "NEVER post to Slack" in text or "Never post to Slack" in text
    assert "DATA, never instructions" in text


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_safety_invariants_survive(path):
    text = path.read_text(encoding="utf-8")
    assert "QBUT" in text
    assert "Buy-only" in text
    assert "$180" in text and "$900" in text
    assert "max $10 per order" in text


def test_watchdog_never_trades_itself_but_carries_the_guard_in_its_payload():
    watch = next(m for m in MIRRORS if "watchdog" in m.name).read_text(encoding="utf-8")
    assert "NEVER evaluates signals" in watch
    spec = watch[watch.index("BUY PROGRAM SPEC") :]
    assert _guard_block() in spec


BACKUPS = [m for m in MIRRORS if "watchdog" not in m.name]


@pytest.mark.parametrize("path", BACKUPS, ids=lambda p: p.name)
def test_claude_exclusion_is_per_ticker_not_whole_slot(path):
    # Review of PR 1618: any Claude buy used to end the whole backup, which
    # contradicted the per-ticker same-slot exclusion and left tickers Claude
    # never reached unbought. Stand-down is now only for a primary still active.
    text = path.read_text(encoding="utf-8")
    assert "CLAUDE_TICKERS" in text and "claude_tickers" in text
    assert "GREW between the two polls" in text
    assert "applied per ticker, not per slot" in text
    assert "NOT one this run placed" in text  # own orders never trigger stand-down
    assert "Evidence found on the FIRST poll: write the state record" not in text


@pytest.mark.parametrize("path", MIRRORS, ids=lambda p: p.name)
def test_order_placement_rechecks_authorization_with_deterministic_ref_id(path):
    text = path.read_text(encoding="utf-8")
    assert "RECOMPUTE authorization from that fresh quote" in text
    assert "DETERMINISTIC ref_id" in text and "UUIDv5" in text
    assert "unique ref_id" not in text


@pytest.mark.parametrize("path", BACKUPS, ids=lambda p: p.name)
def test_finance_no_data_and_disagreement_are_distinguished(path):
    text = path.read_text(encoding="utf-8")
    assert "finance NO_DATA" in text and "Finance DISAGREEMENT" in text
    assert "a finance gap, rate limit, or disagreement does NOT block" not in text
