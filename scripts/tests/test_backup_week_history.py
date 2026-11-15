"""The Perplexity backups' "hybrid" order history: a Mac-side week envelope when the Mac is up, the full fetch otherwise.

2026-11-13: the 9:35 Claude slot did not fire, so the 10:30 Claude session had no same-day week envelope and fell back
to a 135,748-character full-week ``get_equity_orders`` fetch it could not save (ABORT_GATE6_HISTORY_INCOMPLETE /
output_cap_exceeded, 0 orders). Commit 67d9d617 fixed the Claude prompts. The Perplexity backups fetch SEVEN days
through their sandbox, the same oversized shape, and on 2026-11-14 the operator first pinned that (PR #2317, whose
tests this file replaces) and then chose the hybrid:

* When ``mac_status`` is online and ``mac_path_abandoned`` is false, step 2 runs ``scripts.backup_week_history
  lookup`` on the Mac (``find_order_baseline --kind week``), fetches only the increment since the printed
  ``created_at_gte``, and merges it there (``scripts.merge_order_pages``) into the WEEK FILE. Every planner command that
  reads history (classify, compute, finalize, check-placement) then runs on the Mac with ``--week-envelope``. Poll 2,
  the late-start recheck and every step 7c repull merge onto the previous WEEK FILE the same way.
  ``week_history_mode prior_baseline_merge``.
* Any lookup exit 2, merge refusal, or a Mac that is offline or abandoned: today's full seven-day sandbox fetch,
  unchanged. ``week_history_mode full``.
* The days before Monday (operator decision 2026-11-14, option 1): last week's newest envelope ends before that week's
  last 3:30 orders and any weekend order, and get_equity_orders has no ``created_at_lte`` to fetch just that seam, so
  they are never sourced from it. The planner treats the kill switch as ENGAGED on a week envelope: compute sizes at
  1x at most, finalize refuses an over-1x plan with ``kill_switch_unproven`` and the prompt falls back to the full fetch.
* ``backup_planner._load_rows`` still refuses any envelope not dated today unless ``--week-envelope`` is given, and
  then accepts only a merge_order_pages output pinned to the Mac's own account, with a trusted base writer and an
  unchanged payload hash.

Procedure assertions read what ``backup_planner.pyz procedure`` serves (covered by the pinned ``content_sha256``).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

import dca_config
from scripts import assemble_order_pages as asm
from scripts import backup_planner, backup_procedure
from scripts import backup_week_history as bwh
from scripts import find_order_baseline as fob

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "claude/dca-executor-prompts/perplexity"
TASKS = [backup.task_id for backup in backup_procedure.BACKUPS]

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value to $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
LAST4 = ACCOUNT[-4:]
OTHER_ACCOUNT = "12345"  # a different account; five digits so the account-number guard never flags it
DAY = "2026-11-12"  # a Thursday
TODAY = date.fromisoformat(DAY)
MONDAY = "2026-11-09"
LOOKUP_NOW = datetime.fromisoformat("2026-11-12T15:55:00+00:00")  # 10:55 ET
SNAP1 = "2026-11-12T15:56:00+00:00"  # poll 1 increment's snapshot, 10:56 ET
MERGE_NOW = datetime.fromisoformat("2026-11-12T15:57:00+00:00")


# ---------------------------------------------------------------------------
# Procedure text
# ---------------------------------------------------------------------------


def _procedure(task_id: str) -> str:
    backup = backup_procedure.find_backup(task_id)
    assert backup is not None
    return backup_procedure.render_procedure(backup, backup_procedure.common_values())


def _step(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(next_heading, start)]


@pytest.mark.parametrize("task_id", TASKS)
def test_poll_1_uses_the_mac_week_file_only_while_the_mac_is_up(task_id):
    step2 = _step(_procedure(task_id), "STEP 2 - EVIDENCE POLL 1", "STEP 3 - ")
    assert "Only when mac_status is online and mac_path_abandoned is false" in step2
    assert ("python3 -m scripts.backup_week_history lookup --trading-day <YYYY-MM-DD> --slot <slot> "
            "--account-last4 <the looked-up account's last 4 digits>") in step2
    assert "created_at_gte set to the printed created_at_gte verbatim" in step2
    assert "--label poll1 --base <the printed base> --created-at-gte <the printed created_at_gte>" in step2
    assert "week_history_mode is prior_baseline_merge" in step2
    assert "record its payload_sha256 as week_history_sha256" in step2
    # The merged file never moves into the sandbox (that is the output-size problem itself).
    assert "The WEEK FILE never comes into this sandbox" in step2
    assert "never copy them back into this sandbox or anywhere else" in step2
    # No account number on any command line: only the last four, and the Mac reads its own configured account.
    assert "--account " not in step2.replace("--account-last4", "")


@pytest.mark.parametrize("task_id", TASKS)
def test_any_lookup_or_merge_failure_falls_back_to_the_unchanged_full_poll(task_id):
    step2 = _step(_procedure(task_id), "STEP 2 - EVIDENCE POLL 1", "STEP 3 - ")
    assert ("the lookup or the merge exits non-zero or prints anything but its JSON, or the Mac stops answering), "
            "week_history_mode is full: make the FULL POLL below into orders_pages_poll1.json") in step2
    assert "Once it is full it stays full for the rest of the run." in step2
    assert "Never retype, condense, or move rows by hand, and never reuse a partial increment." in step2
    full = step2[step2.index("FULL POLL: ") :]
    # The full poll is exactly today's seven-day, today-dated fetch.
    assert "created_at_gte set to 00:00 ET seven days before today" in full
    assert "follow EVERY pagination cursor to completion" in full
    assert "NEVER re-query with a different state, date, or window" in full
    assert '"trading_day": "<today YYYY-MM-DD>"' in full
    assert "trading_day stays today" in full


@pytest.mark.parametrize("task_id", TASKS)
def test_week_file_rule_moves_every_history_reader_to_the_mac(task_id):
    step2 = _step(_procedure(task_id), "STEP 2 - EVIDENCE POLL 1", "STEP 3 - ")
    assert ("a planner command that takes --orders-pages or --prior-orders-pages runs on the Mac "
            "(python3 -m scripts.backup_planner ...) with that argument set to the current WEEK FILE and with "
            "--week-envelope --account-last4 <last 4> added") in step2
    assert "fetched from the next_created_at_gte the previous merge printed" in step2
    assert "--base set to the current WEEK FILE" in step2
    assert ("If such a merge or command cannot run on the Mac (it exits non-zero, the Mac stops answering, or "
            "mac_path_abandoned becomes true), switch to week_history_mode full, make the FULL POLL") in step2
    procedure = _procedure(task_id)
    # classify (3d), compute (5) and finalize (6) each point back at the rule.
    assert "(in prior_baseline_merge, the WEEK FILE RULE: on the Mac against the WEEK FILE)" in procedure
    assert "in prior_baseline_merge, the WEEK FILE RULE: on the Mac against the WEEK FILE, plan_source mac_compute" in procedure
    assert "in prior_baseline_merge, the WEEK FILE RULE overrides where it runs: on the Mac against the WEEK FILE" in procedure


@pytest.mark.parametrize("task_id", TASKS)
def test_late_start_recheck_merges_and_classifies_on_the_mac(task_id):
    step3 = _step(_procedure(task_id), "STEP 3 - ", "STEP 4 - ")
    # The guarded late-start sentence is byte-for-byte unchanged (test_backup_procedure); the hybrid is a sub-line.
    assert "redo the broker poll of step 2 from scratch (it replaces orders_pages_poll1.json)" in step3
    assert "means the next increment merged with --label poll1b under the WEEK FILE RULE" in step3
    assert ("anything but status CLEAR with an empty executor_orders list means such an order exists") in step3


@pytest.mark.parametrize("task_id", TASKS)
def test_poll_2_merges_onto_the_poll_1_week_file(task_id):
    step6 = _step(_procedure(task_id), "STEP 6 - EVIDENCE POLL 2", "STEP 7 - ")
    assert "in week_history_mode full the FULL POLL from scratch into orders_pages_poll2.json" in step6
    assert "in prior_baseline_merge an increment merged with --label poll2 under the WEEK FILE RULE" in step6
    assert "--orders-pages orders_pages_poll2.json (in prior_baseline_merge, the WEEK FILE)" in step6


@pytest.mark.parametrize("task_id", TASKS)
def test_kill_switch_unproven_falls_back_to_the_full_fetch_once(task_id):
    step6 = _step(_procedure(task_id), "STEP 6 - EVIDENCE POLL 2", "STEP 7 - ")
    assert "An ABORT whose only error is kill_switch_unproven" in step6
    assert ("switch to week_history_mode full, make the FULL POLL into orders_pages_poll2.json, rerun the CLASSIFY "
            "POLL 2 command and then finalize once in this sandbox on the saved plan.json") in step6
    # The genuine kill-switch abort is untouched.
    assert "kill_switch_engaged_since_plan means spend landed after the plan was sized" in step6


@pytest.mark.parametrize("task_id", TASKS)
def test_per_order_repull_merges_onto_the_previous_week_file(task_id):
    step7 = _step(_procedure(task_id), "STEP 7 - PLACE THE ORDERS", "STEP 8 - ")
    assert "this pull is the next increment under the WEEK FILE RULE (--label order<N>" in step7
    assert ("Judge the rules above only on its rows created at or after the previous order placement timestamp "
            "(for the first order, the time of poll 2)") in step7
    assert ("If that merge or the Mac fails before this order is submitted, switch to week_history_mode full: make "
            "the FULL POLL into orders_pages_poll2.json") in step7
    assert "--prior-orders-pages set to the WEEK FILE written by THIS order's step c merge" in step7
    # After a submission there is no history from before it to fall back to: never re-judge, never resubmit.
    assert "record the order with state unverified, stop the batch, and record status failed_unexpected_error" in step7
    assert "Never retry a failed order." in step7


@pytest.mark.parametrize("task_id", TASKS)
def test_state_record_carries_the_week_history_mode(task_id):
    step8 = _step(_procedure(task_id), "STEP 8 - STATE RECORD", "STEP 9 - ")
    assert "week_history_mode (prior_baseline_merge or full" in step8
    assert "week_history_sha256" in step8
    # Why the merged path sized at 1x is recorded in the run record itself.
    assert "kill_switch_basis (week_envelope_treated_as_engaged when any compute or finalize of this run read a WEEK FILE" in step8
    assert "the days before Monday are not proven" in step8


@pytest.mark.parametrize("task_id", TASKS)
def test_served_parts_carry_each_rule_where_it_is_used(task_id):
    parts, token = {}, None
    for part in (0, 1, 2, 3):
        parts[part] = backup_procedure.render_part(task_id, part, token=token)
        if part < 3:
            token = backup_procedure.part_token(task_id, part)
    assert "WEEK FILE RULE (every later step)" in parts[1]
    assert "--label poll2" in parts[2] and "kill_switch_unproven" in parts[2]
    assert "--label order<N>" in parts[3]
    assert "backup_week_history" not in parts[0]  # part 0 runs before any broker call


@pytest.mark.parametrize("path", sorted(PROMPTS.glob("*-backup.txt")), ids=lambda p: p.name)
def test_pasted_prompts_carry_no_order_history_rule(path):
    # The pasted prompt is only a signal check and preflight; the fetch rule lives in the stamped procedure.
    text = path.read_text(encoding="utf-8")
    for needle in ("backup_week_history", "prior_baseline_merge", "WEEK FILE", "find_order_baseline"):
        assert needle not in text, f"{path.name}: {needle!r}"


# ---------------------------------------------------------------------------
# Real envelopes on a fake Mac cron root
# ---------------------------------------------------------------------------


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-order/{label}"))


def _order(label, created_at, *, symbol="KDP", usd="2.000000", placed_agent="agentic"):
    return {
        "id": _uuid(label), "ref_id": _uuid(f"ref/{label}"), "symbol": symbol, "side": "buy", "type": "market",
        "state": "filled", "quantity": "0.050000", "cumulative_quantity": "0.050000", "price": "36.190000",
        "average_price": "36.190000", "fees": "0.000000", "dollar_based_amount": {"amount": usd, "currency_code": "USD"},
        "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate", "placed_agent": placed_agent,
        "created_at": created_at, "last_transaction_at": created_at,
    }


def _claude_week_envelope(root: Path, *, slot="1530", slot_day="2026-11-11", snapshot_at="2026-11-11T20:40:00+00:00",
                          orders=None) -> Path:
    """A real assemble_order_pages week envelope where Wednesday's 3:30 slot would have saved it."""
    run_dir = root / fob.slot_run_dir_relative(slot, date.fromisoformat(slot_day))
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = orders if orders is not None else [
        _order("mon", "2026-11-09T15:00:00Z", symbol="AAPL"),
        _order("wed", "2026-11-11T20:30:00Z", symbol="MSFT"),
    ]
    page = run_dir / "raw_week.json"
    page.write_text(json.dumps({"orders": rows}), encoding="utf-8")
    envelope, _ = asm.assemble([str(page)], account_number=ACCOUNT, trading_day=MONDAY, snapshot_at=snapshot_at)
    out = run_dir / f"orders_pages_week_{MONDAY}_{slot}.json"
    out.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return out


def _page(tmp_path: Path, name: str, rows: list) -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"orders": rows}), encoding="utf-8")
    return str(path)


def _lookup(root: Path) -> dict:
    return bwh.lookup(trading_day=DAY, slot="1030", account_last4=LAST4, now=LOOKUP_NOW, root=root)


def _merged_poll1(root: Path, tmp_path: Path) -> dict:
    found = _lookup(root)
    page = _page(tmp_path, "inc1.json", [_order("thu", "2026-11-12T14:40:00Z", symbol="KO")])
    return bwh.merge(trading_day=DAY, slot="1030", account_last4=LAST4, label="poll1", base=found["base"],
                     created_at_gte=found["created_at_gte"], snapshot_at=SNAP1, page_files=[page], now=MERGE_NOW,
                     root=root)


def test_lookup_finds_an_earlier_day_week_envelope_and_prints_no_account(tmp_path):
    root = tmp_path / "cron"
    base = _claude_week_envelope(root)
    found = _lookup(root)
    assert found["ok"] and found["week_history_mode"] == "prior_baseline_merge"
    assert found["base"] == str(base) and found["base_day"] == "2026-11-11"
    assert found["created_at_gte"] == "2026-11-11T20:38:00Z"  # snapshot minus 120 s, one UTC instant
    assert Path(found["work_dir"]) == root / "perplexity_backup" / "20261112_1030ET"
    assert Path(found["work_dir"]).is_dir()
    assert ACCOUNT not in json.dumps(found)


def test_lookup_with_no_envelope_exits_2_and_names_the_full_fallback(tmp_path, capsys):
    code = bwh.main(["lookup", "--trading-day", DAY, "--slot", "1030", "--account-last4", LAST4,
                     "--cron-root", str(tmp_path), "--now", LOOKUP_NOW.isoformat()])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out == {"ok": False, "week_history_mode": "full", "reason": "no_valid_baseline",
                                 "detail": out["detail"]}


def test_lookup_refuses_an_account_that_is_not_the_resolved_one(tmp_path, monkeypatch):
    _claude_week_envelope(tmp_path)
    with pytest.raises(bwh.Refused) as refused:
        bwh.lookup(trading_day=DAY, slot="1030", account_last4="0000", now=LOOKUP_NOW, root=tmp_path)
    assert refused.value.reason == "account_last4_mismatch"
    monkeypatch.delenv(bwh.ACCOUNT_ENV)
    with pytest.raises(bwh.Refused) as refused:
        bwh.lookup(trading_day=DAY, slot="1030", account_last4=LAST4, now=LOOKUP_NOW, root=tmp_path)
    assert refused.value.reason == "account_unset"


def test_merge_writes_the_week_file_and_the_next_boundary(tmp_path):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    merged = _merged_poll1(root, tmp_path)
    out = Path(merged["out"])
    assert out.parent == root / "perplexity_backup" / "20261112_1030ET"
    assert out.name == f"orders_pages_week_{MONDAY}_backup_1030_poll1.json"
    assert merged["order_count"] == 3 and merged["increment_order_count"] == 1
    assert merged["next_created_at_gte"] == "2026-11-12T15:54:00Z"
    envelope = json.loads(out.read_text(encoding="utf-8"))
    assert envelope["trading_day"] == MONDAY
    assert envelope["ingest"]["tool"] == "scripts.merge_order_pages"
    assert envelope["ingest"]["base_ingest_tool"] == "scripts.assemble_order_pages"
    assert ACCOUNT not in json.dumps(merged)


def test_poll_2_and_each_order_merge_onto_the_previous_week_file(tmp_path):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    poll1 = _merged_poll1(root, tmp_path)
    page = _page(tmp_path, "inc2.json", [_order("thu", "2026-11-12T14:40:00Z", symbol="KO")])
    poll2 = bwh.merge(trading_day=DAY, slot="1030", account_last4=LAST4, label="poll2", base=poll1["out"],
                      created_at_gte=poll1["next_created_at_gte"], snapshot_at="2026-11-12T16:05:00+00:00",
                      page_files=[page], now=datetime.fromisoformat("2026-11-12T16:06:00+00:00"), root=root)
    assert poll2["order_count"] == 3 and poll2["out"].endswith("_poll2.json")
    page = _page(tmp_path, "inc3.json", [_order("o1", "2026-11-12T16:06:30Z", symbol="PEP")])
    order1 = bwh.merge(trading_day=DAY, slot="1030", account_last4=LAST4, label="order1", base=poll2["out"],
                       created_at_gte=poll2["next_created_at_gte"], snapshot_at="2026-11-12T16:07:00+00:00",
                       page_files=[page], now=datetime.fromisoformat("2026-11-12T16:07:30+00:00"), root=root)
    assert order1["order_count"] == 4
    envelope = json.loads(Path(order1["out"]).read_text(encoding="utf-8"))
    assert envelope["ingest"]["base_ingest_tool"] == "scripts.merge_order_pages"


def test_merge_refuses_a_gap_a_relabel_and_an_edited_base(tmp_path):
    root = tmp_path / "cron"
    base = _claude_week_envelope(root)
    page = _page(tmp_path, "inc.json", [])
    late = "2026-11-12T15:00:00Z"  # far after the base snapshot minus overlap: orders could hide in the gap
    with pytest.raises(asm.AssemblyRefused) as refused:
        bwh.merge(trading_day=DAY, slot="1030", account_last4=LAST4, label="poll1", base=str(base),
                  created_at_gte=late, snapshot_at=SNAP1, page_files=[page], now=MERGE_NOW, root=root)
    assert refused.value.reason == "increment_window_too_late"
    _merged_poll1(root, tmp_path)
    with pytest.raises(bwh.Refused) as relabel:
        _merged_poll1(root, tmp_path)
    assert relabel.value.reason == "out_exists"
    envelope = json.loads(base.read_text(encoding="utf-8"))
    envelope["pages"][0]["orders"].pop()
    base.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(bwh.Refused) as edited:
        bwh.merge(trading_day=DAY, slot="1030", account_last4=LAST4, label="poll1x", base=str(base),
                  created_at_gte="2026-11-11T20:38:00Z", snapshot_at=SNAP1, page_files=[page], now=MERGE_NOW,
                  root=root)
    assert edited.value.reason == "base_payload_sha256_mismatch"


# ---------------------------------------------------------------------------
# Planner binding (--week-envelope)
# ---------------------------------------------------------------------------


def test_planner_still_refuses_a_monday_dated_envelope_without_the_flag(tmp_path):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    merged = _merged_poll1(root, tmp_path)
    with pytest.raises(backup_planner._Refused) as refused:
        backup_planner._load_rows(Path(merged["out"]), TODAY)
    assert refused.value.errors[0].code == "orders_pages_trading_day_mismatch"


def test_planner_reads_the_merged_week_file_with_the_flag(tmp_path):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    merged = _merged_poll1(root, tmp_path)
    rows = backup_planner._load_rows(Path(merged["out"]), TODAY, LAST4)
    assert sorted(row["symbol"] for row in rows) == ["AAPL", "KO", "MSFT"]


def _week_refusal(path: Path, last4: str = LAST4) -> str:
    with pytest.raises(backup_planner._Refused) as refused:
        backup_planner._load_rows(path, TODAY, last4)
    return refused.value.errors[0].code


def _rewrite(path: Path, edit) -> Path:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    edit(envelope)
    out = path.with_name("edited.json")
    out.write_text(json.dumps(envelope), encoding="utf-8")
    return out


def test_the_flag_never_accepts_a_hand_built_or_assembled_file(tmp_path):
    hand = tmp_path / "hand.json"
    hand.write_text(json.dumps({"account_number": ACCOUNT, "trading_day": MONDAY,
                                "pages": [{"results": [], "next": None}]}), encoding="utf-8")
    assert _week_refusal(hand) == "week_envelope_not_merged"
    # An assembled (unmerged) Claude envelope is a base, never this run's history.
    assert _week_refusal(_claude_week_envelope(tmp_path / "cron")) == "week_envelope_not_merged"


def test_the_flag_pins_account_week_writer_hash_and_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    out = Path(_merged_poll1(root, tmp_path)["out"])
    assert _week_refusal(out, "0000") == "week_envelope_last4_mismatch"
    assert _week_refusal(_rewrite(out, lambda e: e.update(account_number=OTHER_ACCOUNT))) == "orders_pages_account_mismatch"
    assert _week_refusal(_rewrite(out, lambda e: e.update(trading_day=DAY))) == "week_envelope_not_this_week"
    assert _week_refusal(_rewrite(out, lambda e: e["ingest"].update(base_ingest_tool=None))) == "week_envelope_untrusted_base"
    assert _week_refusal(_rewrite(out, lambda e: e["pages"][0]["orders"].pop())) == "week_envelope_payload_mismatch"
    assert _week_refusal(_rewrite(out, lambda e: e.update(snapshot_at="2026-11-11T20:40:00+00:00"))) == "week_envelope_snapshot_not_today"
    monkeypatch.setenv(backup_planner.ACCOUNT_ENV, OTHER_ACCOUNT)
    assert _week_refusal(out) == "orders_pages_account_mismatch"
    monkeypatch.delenv(backup_planner.ACCOUNT_ENV)
    assert _week_refusal(out) == "week_envelope_account_unset"


def test_merge_onto_a_hand_built_base_is_refused_by_the_planner(tmp_path):
    # merge_order_pages itself accepts any envelope with valid rows; the planner refuses its output when the base
    # was not script-built, so a hand-built file cannot be laundered through a merge.
    root = tmp_path / "cron"
    base = _claude_week_envelope(root)
    envelope = json.loads(base.read_text(encoding="utf-8"))
    del envelope["ingest"]
    hand = tmp_path / "hand_base.json"
    hand.write_text(json.dumps(envelope), encoding="utf-8")
    from scripts import merge_order_pages

    merged = merge_order_pages.merge(str(hand), [_page(tmp_path, "inc.json", [])], account_number=ACCOUNT,
                                     trading_day=MONDAY, created_at_gte="2026-11-11T20:38:00Z", snapshot_at=SNAP1,
                                     overlap_seconds=120)
    out = tmp_path / "laundered.json"
    out.write_text(json.dumps(merged), encoding="utf-8")
    assert merged["ingest"]["base_ingest_tool"] is None
    assert _week_refusal(out) == "week_envelope_untrusted_base"


def test_classify_cli_runs_on_the_week_file(tmp_path, capsys):
    root = tmp_path / "cron"
    _claude_week_envelope(root)
    out = _merged_poll1(root, tmp_path)["out"]
    code = backup_planner.main(["classify", "--trading-day", DAY, "--slot", "1030", "--orders-pages", out,
                                "--week-envelope", "--account-last4", LAST4])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["status"] == "CLEAR"


@pytest.mark.parametrize("argv", [["--week-envelope"], ["--week-envelope", "--account-last4", "12"],
                                  ["--account-last4", LAST4]])
def test_the_flag_and_last4_come_together(tmp_path, argv):
    with pytest.raises(SystemExit) as exited:
        backup_planner.main(["classify", "--trading-day", DAY, "--slot", "1030", "--orders-pages",
                             str(tmp_path / "x.json"), *argv])
    assert exited.value.code == 2


# ---------------------------------------------------------------------------
# Kill switch: treated as engaged on a week envelope (operator option 1)
# ---------------------------------------------------------------------------

NOW_ET = "2026-11-12T10:56:00-05:00"


def test_engaged_floor_engages_the_kill_switch():
    config = dca_config.load_strategy_config()
    floor = backup_planner._engaged_floor(config)
    assert floor is not None
    import dataclasses

    assert backup_planner.kill_switch_engaged(dataclasses.replace(config, deployed_last_7d_usd=floor))
    assert not backup_planner.kill_switch_engaged(dataclasses.replace(config, deployed_last_7d_usd=0.0))


def test_finalize_refuses_an_over_1x_computed_plan_on_a_week_file():
    config = dca_config.load_strategy_config()
    error = backup_planner._kill_switch_error([{"multiplier": 2.0}], [], config, TODAY, NOW_ET, trailing_proven=False)
    assert error is not None and error.code == "kill_switch_unproven"
    assert backup_planner._kill_switch_error([{"multiplier": 1.0}], [], config, TODAY, NOW_ET, trailing_proven=False) is None
    # Today's behavior (full seven-day history) is unchanged.
    assert backup_planner._kill_switch_error([{"multiplier": 2.0}], [], config, TODAY, NOW_ET) is None


def test_finalize_refuses_an_over_1x_plan_first_set_on_a_week_file():
    config = dca_config.load_strategy_config()
    plan = {"sizing_guard": {"max_multiplier": 2.0, "deployed_7d_usd": 0.0}, "orders": [{"ref_id": "r"}]}
    error = backup_planner._plan_first_kill_switch_error(plan, [], config, TODAY, NOW_ET, trailing_proven=False)
    assert error is not None and error.code == "kill_switch_unproven"
    assert backup_planner._plan_first_kill_switch_error(plan, [], config, TODAY, NOW_ET) is None
    at_1x = {**plan, "sizing_guard": {"max_multiplier": 1.0, "deployed_7d_usd": 0.0}}
    assert backup_planner._plan_first_kill_switch_error(at_1x, [], config, TODAY, NOW_ET, trailing_proven=False) is None


def test_freshness_bound_and_caps_are_untouched():
    import dca_runner

    assert dca_runner.MAX_ORDERS_FETCH_AGE_SECONDS == 120
    assert bwh.OVERLAP_SECONDS == 120
