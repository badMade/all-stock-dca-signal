"""Tests for ``scripts/kill_switch_gap_proof.py`` and the 12:30 prompt block that calls it.

Why (2026-11-10, DCA Issues key ``kill_switch_evidence_incomplete``): on 2026-11-09 the Claude app
was down, both afternoon slots fired at 19:24 ET and aborted ``GATE1_OFF_WINDOW`` before any run
evidence could exist, and the 12:30 slot's Gate 5 then aborted every day until 11-09 left the
trailing 7-day window. A missing pair may now be excused as $0 only on a clean Gate-1 marker AND a
replayed, complete broker history showing no order that afternoon. Every other shape must block.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

from scripts import assemble_order_pages as asm
from scripts import kill_switch_gap_proof as gap
from scripts.run_classification import build_scheduled_classification

REPO = Path(__file__).resolve().parents[2]
PROMPT = REPO / "claude" / "dca-executor-prompts" / "live-mirror" / "1230ET-1230-pm-midday-buying-run.txt"
ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
D = date(2026, 11, 9)
PAIR_1430 = (D, "14:30")
PAIR_1530 = (D, "15:30")


def _marker(slot_id, *, gate="gate1", reason="GATE1_OFF_WINDOW", day=D):
    return {
        "kind": "evidence_free_abort", "slot_id": slot_id, "trading_day": day.isoformat(),
        "gate": gate, "reason_code": reason, "fired_at_utc": "2026-11-10T00:24:28Z",
        "writer": "scripts/record_evidence_free_abort.sh",
    }


def _write_markers(root: Path, slot_id: str, *lines, day=D):
    path = root / "abort_markers" / day.strftime("%Y%m%d") / f"{slot_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
                    encoding="utf-8")


def _order(label, created_at, *, side="buy", state="filled"):
    """A raw get_equity_orders row the assembler accepts (same shape as its own tests)."""
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"gap/{label}")),
        "ref_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"gap/ref/{label}")),
        "symbol": "SPCX", "side": side, "type": "market", "state": state,
        "quantity": "0.006009", "cumulative_quantity": "0.006009", "price": "332.7999",
        "average_price": "332.7999", "fees": "0.000000",
        "dollar_based_amount": {"amount": "2.000000", "currency_code": "USD"},
        "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
        "placed_agent": "agentic", "created_at": created_at, "last_transaction_at": created_at,
    }


def _envelope(tmp_path, orders, *, trading_day="2026-11-09"):
    """Build the envelope through the real assembler, as the prompt does."""
    page = tmp_path / f"page_{uuid.uuid4().hex}.json"
    page.write_text(json.dumps({"orders": orders}), encoding="utf-8")
    envelope, _count = asm.assemble([str(page)], account_number=ACCOUNT, trading_day=trading_day)
    return envelope


def _classify(root, envelope=None, *, pairs=(PAIR_1430, PAIR_1530), claimed=frozenset(), known=None):
    return gap.classify_missing_pairs(pairs, claimed_pairs=set(claimed), marker_root=root,
                                      orders_envelope=envelope, account_number=ACCOUNT, known_order_ids=known)


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "cron"
    _write_markers(base, "1430", _marker("1430"))
    _write_markers(base, "1530", _marker("1530"))
    return base


# --- the three required cases -------------------------------------------------------------------


def test_gate1_marker_with_an_empty_broker_afternoon_is_excused_as_zero(root, tmp_path):
    # A morning order on D and one the next day bracket the window without entering it.
    envelope = _envelope(tmp_path, [_order("am", "2026-11-09T13:55:00-05:00"),
                                    _order("next", "2026-11-10T09:40:00-05:00")])
    verdicts = _classify(root, envelope)
    assert {v.status for v in verdicts.values()} == {gap.EXCUSED}
    assert "broker_orders_in_window=0" in verdicts[PAIR_1430].reason


@pytest.mark.parametrize(("gate", "reason"), [
    ("gate2", "GATE2_ABORT_NO_PYTHON311"),  # pre-broker too, but deliberately not in the allowlist
    ("gate4", "GATE4_COLLISION"),
    ("gate7", "GATE7_PLACEMENT_FAILED"),
    ("gate1", "GATE1_TIME_SANITY"),  # right gate, different reason
])
def test_a_later_gate_or_other_reason_marker_is_rejected(root, tmp_path, gate, reason):
    _write_markers(root, "1430", _marker("1430", gate=gate, reason=reason))
    verdicts = _classify(root, _envelope(tmp_path, []))
    assert verdicts[PAIR_1430].status == gap.BLOCKED
    assert gate in verdicts[PAIR_1430].reason
    assert verdicts[PAIR_1530].status == gap.EXCUSED  # the clean companion is judged on its own


def test_no_marker_still_blocks_even_with_an_empty_broker_afternoon(tmp_path):
    verdicts = _classify(tmp_path / "empty-cron", _envelope(tmp_path, []))
    assert {v.status for v in verdicts.values()} == {gap.BLOCKED}
    assert verdicts[PAIR_1430].reason.startswith("no run_state.json")


# --- marker strictness ----------------------------------------------------------------------------


def test_a_gate1_marker_mixed_with_a_later_gate_marker_blocks(root, tmp_path):
    _write_markers(root, "1530", _marker("1530"), _marker("1530", gate="gate7", reason="GATE7_PLACEMENT_FAILED"))
    assert _classify(root, _envelope(tmp_path, []))[PAIR_1530].status == gap.BLOCKED


@pytest.mark.parametrize("bad_line", [
    "{not json",
    json.dumps(_marker("1430", day=date(2026, 11, 6))),  # another day in this day's file
    json.dumps(_marker("1530")),  # another slot in this slot's file
    json.dumps({**_marker("1430"), "kind": "something_else"}),
    json.dumps({**_marker("1430"), "fired_at_utc": "2026-11-10T00:24:28"}),  # naive
    '{"kind": "evidence_free_abort", "kind": "evidence_free_abort"}',  # duplicate key
])
def test_any_malformed_or_foreign_line_blocks_instead_of_being_skipped(root, tmp_path, bad_line):
    _write_markers(root, "1430", _marker("1430"), bad_line)
    assert _classify(root, _envelope(tmp_path, []))[PAIR_1430].status == gap.BLOCKED


def test_an_empty_marker_file_blocks(root, tmp_path):
    (root / "abort_markers" / "20261109" / "1430.jsonl").write_text("\n", encoding="utf-8")
    assert _classify(root, _envelope(tmp_path, []))[PAIR_1430].status == gap.BLOCKED


def test_a_pair_claimed_by_a_rejected_run_state_blocks(root, tmp_path):
    verdicts = _classify(root, _envelope(tmp_path, []), claimed={PAIR_1430})
    assert verdicts[PAIR_1430].status == gap.BLOCKED
    assert "rejected" in verdicts[PAIR_1430].reason


def test_without_an_orders_file_a_clean_marker_only_asks_for_broker_proof(root):
    verdicts = _classify(root)
    assert {v.status for v in verdicts.values()} == {gap.NEEDS_BROKER_PROOF}
    assert gap.earliest_needing_proof(verdicts) == D


# --- broker proof strictness ----------------------------------------------------------------------


@pytest.mark.parametrize(("created_at", "side", "state"), [
    ("2026-11-09T14:00:00-05:00", "buy", "filled"),  # inclusive window start
    ("2026-11-09T14:31:00-05:00", "buy", "cancelled"),  # any state
    ("2026-11-09T15:35:00-05:00", "sell", "filled"),  # any side
    ("2026-11-09T19:24:30-05:00", "buy", "queued"),  # the off-window fire time itself
    ("2026-11-10T04:59:00+00:00", "buy", "filled"),  # 23:59 ET, written in UTC
])
def test_any_order_in_the_afternoon_window_blocks(root, tmp_path, created_at, side, state):
    envelope = _envelope(tmp_path, [_order("x", created_at, side=side, state=state)])
    verdicts = _classify(root, envelope)
    assert {v.status for v in verdicts.values()} == {gap.BLOCKED}
    assert "1 broker order(s)" in verdicts[PAIR_1430].reason


def test_an_orders_file_starting_after_the_day_blocks(root, tmp_path):
    envelope = _envelope(tmp_path, [], trading_day="2026-11-10")
    assert "after 2026-11-09" in _classify(root, envelope)[PAIR_1430].reason


def test_an_orders_file_starting_earlier_still_covers_the_day(root, tmp_path):
    envelope = _envelope(tmp_path, [_order("thu", "2026-11-06T15:31:00-05:00")], trading_day="2026-11-06")
    assert _classify(root, envelope)[PAIR_1430].status == gap.EXCUSED


def test_an_unpinned_or_foreign_account_orders_file_blocks(root, tmp_path):
    envelope = _envelope(tmp_path, [])
    assert _classify(root, {**envelope, "account_number": "123456789"})[PAIR_1430].status == gap.BLOCKED
    verdicts = gap.classify_missing_pairs([PAIR_1430], claimed_pairs=set(), marker_root=root,
                                          orders_envelope=envelope, account_number="")
    assert verdicts[PAIR_1430].status == gap.BLOCKED


def test_an_incomplete_pagination_chain_blocks(root, tmp_path):
    envelope = _envelope(tmp_path, [])
    # A first page that still points at a next page the file never saved.
    envelope["pages"] = [{"orders": [], "next": "https://api.example.invalid/orders/?cursor=abc"}]
    verdicts = _classify(root, envelope)
    assert verdicts[PAIR_1430].status == gap.BLOCKED
    assert "not provably complete" in verdicts[PAIR_1430].reason


def test_a_hand_built_non_envelope_blocks(root):
    for doc in ({"orders": []}, [], {"account_number": ACCOUNT, "trading_day": "2026-11-09", "pages": []}):
        assert _classify(root, doc)[PAIR_1430].status == gap.BLOCKED


def test_load_orders_envelope_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "o.json"
    path.write_text('{"pages": [], "pages": []}', encoding="utf-8")
    with pytest.raises(SystemExit, match="unreadable"):
        gap.load_orders_envelope(str(path))


# --- review 2026-11-11 fix 1: provenance (the ingest block must still describe the pages) --------


def test_a_well_formed_hand_built_envelope_without_ingest_blocks(root):
    # Replays as one complete, empty page -- exactly what the first draft accepted.
    envelope = {"account_number": ACCOUNT, "trading_day": "2026-11-09", "pages": [{"orders": []}]}
    verdicts = _classify(root, envelope)
    assert {v.status for v in verdicts.values()} == {gap.BLOCKED}
    assert "no assemble_order_pages ingest block" in verdicts[PAIR_1430].reason


def test_a_row_removed_after_assembly_blocks(root, tmp_path):
    envelope = _envelope(tmp_path, [_order("am", "2026-11-09T10:00:00-05:00"),
                                    _order("pm", "2026-11-09T14:31:00-05:00")])
    envelope["pages"][0]["orders"] = [row for row in envelope["pages"][0]["orders"]
                                      if not row["created_at"].startswith("2026-11-09T14")]
    verdicts = _classify(root, envelope)
    assert verdicts[PAIR_1430].status == gap.BLOCKED
    assert "ingest.payload_sha256" in verdicts[PAIR_1430].reason


@pytest.mark.parametrize("tamper", [
    lambda ing: ing.update(tool="hand_written"),
    lambda ing: ing.update(order_count=5),
    lambda ing: ing.update(order_count=True),
    lambda ing: ing.update(pages_sha256=[]),
    lambda ing: ing.update(pages_sha256=["not-a-digest"]),
    lambda ing: ing.pop("payload_sha256"),
])
def test_an_ingest_block_that_no_longer_matches_blocks(root, tmp_path, tamper):
    envelope = _envelope(tmp_path, [])
    tamper(envelope["ingest"])
    assert _classify(root, envelope)[PAIR_1430].status == gap.BLOCKED


def test_a_state_slice_envelope_with_its_ingest_intact_is_accepted(root, tmp_path):
    slices = {}
    for state in asm.ORDER_STATES:
        page = tmp_path / f"slice_{state}.json"
        page.write_text(json.dumps({"orders": []}), encoding="utf-8")
        slices[state] = [str(page)]
    envelope, _count = asm.assemble_state_slices(slices, account_number=ACCOUNT, trading_day="2026-11-09")
    assert _classify(root, envelope)[PAIR_1430].status == gap.EXCUSED


# --- review 2026-11-11 fix 2: the self-reported fetch boundary is cross-checked ------------------


NEXT_DAY_ORDER = _order("tue", "2026-11-10T10:05:00-05:00")


def test_an_envelope_missing_a_known_later_order_blocks(root, tmp_path):
    # The file claims trading_day 11-09 but holds nothing from 11-10, where a run_state recorded an
    # order: it was really fetched later than it says, so its empty 11-09 afternoon proves nothing.
    envelope = _envelope(tmp_path, [])
    verdicts = _classify(root, envelope, known={date(2026, 11, 10): {NEXT_DAY_ORDER["id"]}})
    assert {v.status for v in verdicts.values()} == {gap.BLOCKED}
    assert verdicts[PAIR_1430].reason == (f"orders file does not contain known order {NEXT_DAY_ORDER['id']}; "
                                          "fetch boundary unproven")


def test_an_envelope_holding_every_known_later_order_is_excused(root, tmp_path):
    envelope = _envelope(tmp_path, [NEXT_DAY_ORDER])
    known = {date(2026, 11, 10): {NEXT_DAY_ORDER["id"].upper()},  # case-insensitive, as the replay is
             D: {"same-day-order-id"}}  # same-day ids are not boundary evidence
    verdicts = _classify(root, envelope, known=known)
    assert {v.status for v in verdicts.values()} == {gap.EXCUSED}
    assert "known_later_orders_present=1" in verdicts[PAIR_1430].reason


def test_with_no_known_later_order_the_reason_says_the_boundary_was_not_cross_checked(root, tmp_path):
    verdicts = _classify(root, _envelope(tmp_path, []), known={})
    assert verdicts[PAIR_1430].status == gap.EXCUSED
    assert "no known later order to cross-check the fetch boundary" in verdicts[PAIR_1430].reason


def test_known_order_ids_by_day_reads_buy_candidate_order_ids():
    states = [
        {"trading_day": "2026-11-10", "buy_candidates": [{"order_id": " ABC "}, {"order_id": None}, "junk"]},
        {"trading_day": "2026-11-10", "buy_candidates": [{"order_id": "def"}]},
        {"trading_day": None, "buy_candidates": [{"order_id": "undated"}]},
        {"trading_day": "2026-11-06", "buy_candidates": "not a list"},
    ]
    assert gap.known_order_ids_by_day(states) == {date(2026, 11, 10): {"abc", "def"}}


# --- review 2026-11-11 fix 3: the window runs to the next session's open, not midnight -----------


@pytest.mark.parametrize("created_at", [
    "2026-11-10T00:05:00-05:00",  # just after midnight, queued for the next session
    "2026-11-10T09:29:59-05:00",  # last second before the open
])
def test_an_overnight_order_before_the_next_open_blocks(root, tmp_path, created_at):
    verdicts = _classify(root, _envelope(tmp_path, [_order("night", created_at)]))
    assert {v.status for v in verdicts.values()} == {gap.BLOCKED}
    assert "2026-11-09 14:00 ET - 2026-11-10 09:30 ET" in verdicts[PAIR_1430].reason


def test_the_next_sessions_own_orders_do_not_block(root, tmp_path):
    verdicts = _classify(root, _envelope(tmp_path, [_order("open", "2026-11-10T09:30:00-05:00"),
                                                    _order("next", "2026-11-10T09:40:00-05:00")]))
    assert {v.status for v in verdicts.values()} == {gap.EXCUSED}


def test_a_fridays_window_runs_through_the_weekend_to_mondays_open(tmp_path):
    friday = date(2026, 11, 6)
    base = tmp_path / "cron"
    _write_markers(base, "1430", _marker("1430", day=friday), day=friday)
    start, end = gap.order_window(friday)
    assert (start.isoformat(), end.isoformat()) == ("2026-11-06T14:00:00-05:00", "2026-11-09T09:30:00-05:00")
    envelope = _envelope(tmp_path, [_order("sun", "2026-11-08T20:00:00-05:00")], trading_day="2026-11-06")
    verdict = gap.classify_missing_pairs([(friday, "14:30")], claimed_pairs=set(), marker_root=base,
                                         orders_envelope=envelope, account_number=ACCOUNT)[(friday, "14:30")]
    assert verdict.status == gap.BLOCKED


# --- the live prompt block, executed end to end ---------------------------------------------------


def _prompt_block() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    match = re.search(r"python3 - <<'PY'\n(import datetime, glob, json, os.*?)\nPY\n", text, re.S)
    assert match, "the Gate 5 deployment block moved; update this test's anchor"
    assert "expected_pairs - observed_pairs" in match.group(1)
    return match.group(1)


_FROZEN_CLOCK = """
import datetime as _dt
class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 11, 10, 12, 30, tzinfo=tz)
_dt.datetime = _Frozen
"""


def _afternoon_state(day: str, scheduled_for: str, deployed: float) -> dict:
    """A finalizer-written afternoon run_state the block accepts as validated evidence.

    The 14:30 classification copies the field set of a real finalized 2026-11-06 14:30 intraday
    run_state (every one of those fields is required for it to read as scheduled evidence).
    """
    compact = day.replace("-", "")
    if scheduled_for == "14:30":
        classification = {
            "cron_id": "7be82b97", "manual_run": False, "run_id": f"intraday_live_{compact}_1430ET",
            "run_origin": "scheduled", "run_type": "intraday_dca_live", "scheduled_for_et": "14:30",
            "scheduled_window_start_et": "14:25", "scheduled_window_end_et": "14:35",
            "state_schema_version": "intraday-live-v1", "stream": "intraday", "trading_day": day,
            "triggered_at_utc": f"{day}T19:32:29+00:00", "valid_scheduled_evidence": True,
        }
        generated_at = f"{day}T19:40:00Z"
    else:
        classification = {**build_scheduled_classification(day, f"{day}T20:32:00+00:00"), "trading_day": day}
        generated_at = f"{day}T20:40:00Z"
    return {
        "trading_day": day,
        "generated_at": generated_at,
        "finalized_by": "scripts.finalize_run_evidence",
        "run_classification": classification,
        "buy_candidates": [{"ticker": "AAA", "planned_amount_usd": deployed, "status": "submitted"}],
    }


@pytest.fixture
def outage_tree(tmp_path):
    """The 2026-11-10 picture: 11-04..11-06 fully evidenced, 11-09 only Gate-1 markers."""
    cron = tmp_path / "cron"
    for day in ("2026-11-04", "2026-11-05", "2026-11-06"):
        for slot, sub in (("14:30", "intraday_dca"), ("15:30", "ce16ba6b/scheduled_runs")):
            run = cron / sub / f"{day.replace('-', '')}_{slot.replace(':', '')}ET"
            run.mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps(_afternoon_state(day, slot, 10.0)), encoding="utf-8")
    _write_markers(cron, "1430", _marker("1430"))
    _write_markers(cron, "1530", _marker("1530"))
    return cron


def _run_block(cron: Path, orders_file: Path | None = None, *, clock: str = _FROZEN_CLOCK) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "KILL_SWITCH_GAP_ORDERS"}
    env.update(DCA_CRON_TRACKING_ROOT=str(cron), DCA_DISABLE_DOTENV="1", ROBINHOOD_AGENTIC_ACCOUNT_NUMBER=ACCOUNT)
    if orders_file is not None:
        env["KILL_SWITCH_GAP_ORDERS"] = str(orders_file)
    return subprocess.run([sys.executable, "-c", clock + _prompt_block()], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def _save_envelope(tmp_path, orders) -> Path:
    path = tmp_path / "kill_switch_gap_orders.json"
    path.write_text(json.dumps(_envelope(tmp_path, orders)), encoding="utf-8")
    return path


def test_prompt_block_asks_for_broker_proof_on_the_11_09_outage(outage_tree):
    result = _run_block(outage_tree)
    assert result.returncode == 3, result.stderr
    assert "KILL_SWITCH_GAP_NEEDS_BROKER_PROOF 2026-11-09" in result.stderr
    assert result.stdout == ""


def test_prompt_block_prints_the_unchanged_figure_once_the_broker_proves_zero(outage_tree, tmp_path):
    result = _run_block(outage_tree, _save_envelope(tmp_path, []))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60.0"  # six $10 afternoon runs; 11-09 adds exactly $0
    assert result.stderr.count("KILL_SWITCH_GAP_EXCUSED 2026-11-09") == 2


def test_prompt_block_aborts_when_the_broker_shows_an_afternoon_order(outage_tree, tmp_path):
    result = _run_block(outage_tree, _save_envelope(tmp_path, [_order("pm", "2026-11-09T14:31:00-05:00")]))
    assert result.returncode == 1
    assert "incomplete trailing-7-day afternoon evidence" in result.stderr
    assert result.stdout == ""


def test_prompt_block_aborts_without_a_marker_exactly_as_before(outage_tree):
    (outage_tree / "abort_markers" / "20261109" / "1530.jsonl").unlink()
    result = _run_block(outage_tree)
    assert result.returncode == 1
    assert "2026-11-09 15:30: no run_state.json" in result.stderr
    assert result.stdout == ""


def _add_11_10_afternoon(cron: Path, order_id: str) -> None:
    """11-10 fully evidenced, its 15:30 run recording one placed order, so a 2026-11-11 12:30 run
    has a later known order to cross-check the 11-09 fetch boundary against."""
    for slot, sub in (("14:30", "intraday_dca"), ("15:30", "ce16ba6b/scheduled_runs")):
        state = _afternoon_state("2026-11-10", slot, 10.0)
        if slot == "15:30":
            state["buy_candidates"][0]["order_id"] = order_id
        run = cron / sub / f"20261110_{slot.replace(':', '')}ET"
        run.mkdir(parents=True)
        (run / "run_state.json").write_text(json.dumps(state), encoding="utf-8")


def test_prompt_block_blocks_an_orders_file_that_omits_a_known_11_10_order(outage_tree, tmp_path):
    _add_11_10_afternoon(outage_tree, NEXT_DAY_ORDER["id"])
    clock = _FROZEN_CLOCK.replace("2026, 11, 10, 12, 30", "2026, 11, 11, 12, 30")
    result = _run_block(outage_tree, _save_envelope(tmp_path, []), clock=clock)
    assert result.returncode == 1
    assert f"orders file does not contain known order {NEXT_DAY_ORDER['id']}; fetch boundary unproven" in result.stderr
    assert result.stdout == ""
    # The same history WITH that order excuses both pairs and prints the unchanged figure.
    result = _run_block(outage_tree, _save_envelope(tmp_path, [NEXT_DAY_ORDER]), clock=clock)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "60.0"  # 11-05, 11-06, 11-10 at $10 each (11-04 left the window); 11-09 adds $0
    assert result.stderr.count("known_later_orders_present=1") == 2
