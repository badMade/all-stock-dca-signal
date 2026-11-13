"""Tests for ``scripts/find_order_baseline.py``.

2026-11-12 14:30 ET: the 2:30 slot aborted ``ABORT_GATE6_HISTORY_NOT_PERSISTABLE`` /
``output_cap_exceeded`` with 57 BUY candidates and 0 orders, because TODAY's order page alone
(~35 KB, 37 orders) could not be saved verbatim and ``assemble_order_pages`` refused the
condensed copy. The fix lets a later slot merge a small increment onto the newest envelope an
EARLIER slot saved today. These tests pin what that base must be (script-built, unmodified,
this account/day, from today, not in the future) and prove that what the script prints feeds
``merge_order_pages`` cleanly -- the merge, not the base choice, is what keeps it fail-closed.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import assemble_order_pages as asm  # noqa: E402
from scripts import find_order_baseline as fob  # noqa: E402
from scripts import merge_order_pages as merge  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
DAY = "2026-11-12"  # a Thursday
MONDAY = "2026-11-09"
NOW = datetime.fromisoformat(f"{DAY}T14:37:00-05:00")


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-order/{label}"))


def _order(label, utc_hhmmss, *, day=DAY, symbol="KDP", usd="2.000000", state="filled"):
    return {
        "id": _uuid(label),
        "ref_id": _uuid(f"ref/{label}"),
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "state": state,
        "quantity": "0.050000",
        "cumulative_quantity": "0.050000",
        "price": "36.190000",
        "average_price": "36.190000",
        "fees": "0.000000",
        "dollar_based_amount": {"amount": usd, "currency_code": "USD"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "trigger": "immediate",
        "placed_agent": "agentic",
        "created_at": f"{day}T{utc_hhmmss}Z",
        "last_transaction_at": f"{day}T{utc_hhmmss}Z",
    }


def _run_dir(root: Path, slot: str) -> Path:
    path = root / fob.slot_run_dir_relative(slot, datetime.fromisoformat(DAY).date())
    path.mkdir(parents=True, exist_ok=True)
    return path


def _assembled(root, slot, orders, *, kind="day", snapshot_at, account=ACCOUNT, tamper=None):
    """Write a REAL assemble_order_pages envelope where slot ``slot`` would have saved it."""
    run_dir = _run_dir(root, slot)
    page = run_dir / f"raw_{kind}.json"
    page.write_text(json.dumps({"orders": orders}), encoding="utf-8")
    env_day = DAY if kind == "day" else MONDAY
    envelope, _ = asm.assemble([str(page)], account_number=account, trading_day=env_day, snapshot_at=snapshot_at)
    if tamper:
        tamper(envelope)
    name = f"orders_pages_{env_day}_{slot}.json" if kind == "day" else f"orders_pages_week_{MONDAY}_{slot}.json"
    out = run_dir / name
    out.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return out


def _find(root, *, kind="day", slot="1430", account=ACCOUNT, now=NOW):
    return fob.find_baseline(kind=kind, slot=slot, trading_day=DAY, account=account, now=now, root=root)


def test_newest_earlier_slot_wins_over_the_9_35_file(tmp_path):
    _assembled(tmp_path, "0935", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T09:49:07-05:00")
    newest = _assembled(
        tmp_path, "1330", [_order("a", "14:44:00"), _order("b", "18:52:09")], snapshot_at=f"{DAY}T13:52:41-05:00"
    )

    result = _find(tmp_path)

    assert result["ok"] is True
    assert result["base"] == str(newest)
    assert result["base_slot"] == "1330"
    assert result["order_count"] == 2
    assert result["created_at_gte"] == "2026-11-12T13:50:41-05:00"  # snapshot minus 120 s
    assert result["rejected"] == []


def test_this_slot_and_later_slots_are_never_a_base(tmp_path):
    _assembled(tmp_path, "1130", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T11:46:02-05:00")
    # A 2:30 file (e.g. a re-run's own earlier output) and a 3:30 file must not be picked by 2:30.
    _assembled(tmp_path, "1430", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T14:31:00-05:00")
    _assembled(tmp_path, "1530", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T14:32:00-05:00")

    result = _find(tmp_path)

    assert result["ok"] and result["base_slot"] == "1130"


def test_a_hand_built_or_condensed_file_is_skipped_not_trusted(tmp_path):
    _assembled(tmp_path, "0935", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T09:49:07-05:00")
    # The shape the 2026-11-12 2:30 session wrote and the assembler refused: no ingest, condensed rows.
    run_dir = _run_dir(tmp_path, "1330")
    condensed = {"account_number": ACCOUNT, "trading_day": DAY, "snapshot_at": f"{DAY}T13:52:41-05:00",
                 "pages": [{"orders": [{"id": _uuid("a"), "symbol": "KDP", "state": "filled"}]}]}
    (run_dir / f"orders_pages_{DAY}_1330.json").write_text(json.dumps(condensed), encoding="utf-8")

    result = _find(tmp_path)

    assert result["ok"] and result["base_slot"] == "0935"
    assert [r["reason"] for r in result["rejected"]] == ["not_script_built"]


def test_a_script_built_file_edited_afterwards_is_rejected(tmp_path):
    def drop_a_row(envelope):
        envelope["pages"][0]["orders"].pop()

    _assembled(
        tmp_path, "1330", [_order("a", "14:44:00"), _order("b", "18:52:09")],
        snapshot_at=f"{DAY}T13:52:41-05:00", tamper=drop_a_row,
    )
    result = _find(tmp_path)
    assert result["ok"] is False and result["reason"] == "no_valid_baseline"
    assert result["rejected"][0]["reason"] == "payload_sha256_mismatch"


def test_a_condensed_row_inside_an_otherwise_valid_envelope_is_rejected(tmp_path):
    import hashlib

    def condense(envelope):
        del envelope["pages"][0]["orders"][0]["time_in_force"]
        # Re-stamp the hash so only the row check can catch it.
        envelope["ingest"]["payload_sha256"] = hashlib.sha256(fob._canonical(envelope["pages"])).hexdigest()

    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T13:52:41-05:00", tamper=condense)
    result = _find(tmp_path)
    assert result["ok"] is False and result["rejected"][0]["reason"] == "bad_rows"


@pytest.mark.parametrize(
    "snapshot_at, reason",
    [
        ("2026-11-11T15:41:00-05:00", "snapshot_not_today"),  # yesterday's 3:30
        ("2026-11-12T15:00:00-05:00", "snapshot_in_future"),  # later than --now
        ("2026-11-12T13:52:41", "bad_snapshot_at"),  # naive
    ],
)
def test_snapshot_must_be_today_aware_and_not_in_the_future(tmp_path, snapshot_at, reason):
    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], snapshot_at=snapshot_at)
    result = _find(tmp_path)
    assert result["ok"] is False and result["rejected"][0]["reason"] == reason


def test_snapshot_date_is_judged_in_eastern_time_not_utc(tmp_path):
    # 00:30 UTC on the 13th is 19:30 ET on the 12th: still today.
    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], snapshot_at="2026-11-13T00:30:00+00:00")
    result = _find(tmp_path, now=datetime.fromisoformat("2026-11-13T01:00:00+00:00"))
    assert result["ok"] and result["base_slot"] == "1330"


def test_another_account_is_rejected(tmp_path):
    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T13:52:41-05:00", account="OTHER-ACCOUNT")
    result = _find(tmp_path)
    assert result["ok"] is False and result["rejected"][0]["reason"] == "account_mismatch"


def test_week_kind_reads_the_week_file_keyed_by_monday(tmp_path):
    _assembled(tmp_path, "0935", [_order("m", "15:00:00", day=MONDAY)], kind="week", snapshot_at=f"{DAY}T09:49:07-05:00")
    week = _assembled(
        tmp_path, "1330", [_order("m", "15:00:00", day=MONDAY), _order("b", "18:52:09")],
        kind="week", snapshot_at=f"{DAY}T13:52:41-05:00",
    )
    result = _find(tmp_path, kind="week")
    assert result["ok"] and result["base"] == str(week)
    assert result["envelope_trading_day"] == MONDAY


def test_a_week_envelope_last_written_on_an_earlier_day_is_rejected(tmp_path):
    _assembled(tmp_path, "1330", [_order("m", "15:00:00", day=MONDAY)], kind="week", snapshot_at="2026-11-11T13:52:41-05:00")
    result = _find(tmp_path, kind="week")
    assert result["ok"] is False and result["rejected"][0]["reason"] == "snapshot_not_today"


def test_nothing_saved_yet_is_a_clean_no_valid_baseline(tmp_path):
    result = _find(tmp_path)
    assert result == {
        "ok": False,
        "reason": "no_valid_baseline",
        "detail": "no usable day envelope from a slot earlier than 1430 today",
        "rejected": [],  # absent files are normal, not listed
    }


def test_duplicate_keys_are_rejected(tmp_path):
    run_dir = _run_dir(tmp_path, "1330")
    (run_dir / f"orders_pages_{DAY}_1330.json").write_text(
        '{"account_number": "999000111", "account_number": "999000111", "pages": []}', encoding="utf-8"
    )
    result = _find(tmp_path)
    assert result["ok"] is False and result["rejected"][0]["reason"] == "not_json"


def test_cli_exit_codes_and_the_account_is_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    monkeypatch.setattr(fob.dca_paths, "cron_tracking_root", lambda: str(tmp_path))
    argv = ["--kind", "day", "--slot", "1430", "--trading-day", DAY, "--account", ACCOUNT, "--now", NOW.isoformat()]

    assert fob.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "no_valid_baseline"

    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T13:52:41-05:00")
    assert fob.main(argv) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["base_slot"] == "1330"
    assert ACCOUNT not in out


def test_unknown_slot_is_refused():
    with pytest.raises(SystemExit):
        fob.main(["--kind", "day", "--slot", "1445", "--trading-day", DAY, "--account", ACCOUNT])


def test_printed_base_and_gte_feed_merge_order_pages_end_to_end(tmp_path):
    """The 2026-11-12 shape: 1:30 saved 37 orders; 2:30 fetches only the tail and merges it."""
    base_orders = [_order(f"o{i}", f"14:{i:02d}:00") for i in range(37)]
    in_overlap = _order("p1", "18:51:30")  # placed by 1:30 inside the 120 s overlap: in BOTH views
    base_path = _assembled(tmp_path, "1330", [*base_orders, in_overlap], snapshot_at=f"{DAY}T13:52:41-05:00")
    found = _find(tmp_path)
    assert found["ok"]

    increment = tmp_path / "inc.json"
    increment.write_text(json.dumps({"orders": [_order("p2", "19:31:00"), in_overlap]}), encoding="utf-8")
    out = tmp_path / "orders_pages_1430.json"
    code = merge.main([
        "--base", found["base"], "--account", ACCOUNT, "--trading-day", DAY,
        "--created-at-gte", found["created_at_gte"], "--snapshot-at", f"{DAY}T14:33:00-05:00",
        "--overlap-seconds", "120", "--out", str(out), str(increment),
    ])
    assert code == 0
    merged = json.loads(out.read_text())
    assert merged["ingest"]["order_count"] == 39
    # The base file was read, never written.
    assert json.loads(base_path.read_text())["ingest"]["tool"] == "scripts.assemble_order_pages"


def test_an_increment_that_omits_a_base_order_in_the_overlap_still_fails_closed(tmp_path):
    in_overlap = _order("p1", "18:51:30")
    _assembled(tmp_path, "1330", [_order("o1", "14:44:00"), in_overlap], snapshot_at=f"{DAY}T13:52:41-05:00")
    found = _find(tmp_path)
    increment = tmp_path / "inc.json"
    increment.write_text(json.dumps({"orders": [_order("p2", "19:31:00")]}), encoding="utf-8")
    code = merge.main([
        "--base", found["base"], "--account", ACCOUNT, "--trading-day", DAY,
        "--created-at-gte", found["created_at_gte"], "--snapshot-at", f"{DAY}T14:33:00-05:00",
        "--overlap-seconds", "120", "--out", str(tmp_path / "out.json"), str(increment),
    ])
    assert code == 2
    assert not (tmp_path / "out.json").exists()


# --- --kind plan: one UTC fetch boundary set by the DAY base (review of PR 2246) ---------------


def _plan(root, **kw):
    return fob.fetch_plan(slot=kw.pop("slot", "1430"), trading_day=DAY, account=ACCOUNT, now=NOW, root=root, **kw)


def test_plan_merges_both_kinds_from_one_utc_boundary_when_both_bases_are_fresh(tmp_path):
    _assembled(tmp_path, "1330", [_order("a", "18:44:00")], snapshot_at=f"{DAY}T13:52:41-05:00")
    _assembled(tmp_path, "1330", [_order("a", "18:44:00")], kind="week", snapshot_at=f"{DAY}T13:52:41-05:00")

    plan = _plan(tmp_path)

    assert plan["ok"] and plan["boundary_kind"] == "day"
    assert plan["fetch_created_at_gte"] == "2026-11-12T18:50:41Z"  # 13:52:41 ET - 120 s, in UTC
    assert sorted(plan["merge"]) == ["day", "week"] and plan["fallback_full"] == []


def test_a_stale_week_base_never_widens_the_fetch_it_falls_back_alone(tmp_path):
    # Only the 9:35 week file survived; the 1:30 day file is fresh. The old rule fetched from the
    # EARLIER gte -- 9:35 -- which is the whole day's page again and the 11-12 abort.
    _assembled(tmp_path, "1330", [_order("a", "18:44:00")], snapshot_at=f"{DAY}T13:52:41-05:00")
    _assembled(tmp_path, "0935", [_order("a", "14:44:00")], kind="week", snapshot_at=f"{DAY}T09:49:07-05:00")

    plan = _plan(tmp_path)

    assert plan["fetch_created_at_gte"] == "2026-11-12T18:50:41Z"  # still the day base's
    assert list(plan["merge"]) == ["day"]
    assert plan["fallback_full"] == ["week"]


def test_a_week_base_fresher_than_the_day_base_still_merges(tmp_path):
    _assembled(tmp_path, "1130", [_order("a", "14:44:00")], snapshot_at=f"{DAY}T11:46:02-05:00")
    _assembled(tmp_path, "1330", [_order("a", "14:44:00")], kind="week", snapshot_at=f"{DAY}T13:52:41-05:00")
    plan = _plan(tmp_path)
    assert plan["fetch_created_at_gte"] == "2026-11-12T16:44:02Z"
    assert sorted(plan["merge"]) == ["day", "week"]


def test_mixed_offsets_are_compared_as_instants_not_text(tmp_path):
    # Day base written with 'Z', week base with '-05:00'. As TEXT "2026-11-12T18:..Z" sorts after
    # "2026-11-12T13:..-05:00", so a text comparison would call the week base newer than it is.
    _assembled(tmp_path, "1330", [_order("a", "18:44:00")], snapshot_at="2026-11-12T18:52:41Z")
    _assembled(tmp_path, "1230", [_order("a", "18:44:00")], kind="week", snapshot_at=f"{DAY}T13:52:40-05:00")

    plan = _plan(tmp_path)

    assert plan["fetch_created_at_gte"] == "2026-11-12T18:50:41Z"
    # The week snapshot is ONE second older in real time, so it cannot cover the boundary.
    assert list(plan["merge"]) == ["day"] and plan["fallback_full"] == ["week"]

    # One second the other way and it merges, whatever its offset says as text.
    _assembled(tmp_path, "1230", [_order("a", "18:44:00")], kind="week", snapshot_at=f"{DAY}T13:52:42-05:00")
    assert sorted(_plan(tmp_path)["merge"]) == ["day", "week"]


def test_plan_without_a_day_base_takes_the_week_boundary_and_day_falls_back(tmp_path):
    _assembled(tmp_path, "1330", [_order("a", "18:44:00")], kind="week", snapshot_at=f"{DAY}T13:52:41-05:00")
    plan = _plan(tmp_path)
    assert plan["boundary_kind"] == "week" and list(plan["merge"]) == ["week"]
    assert plan["fallback_full"] == ["day"]


def test_plan_with_no_base_at_all_is_exit_2_and_both_fall_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fob.dca_paths, "cron_tracking_root", lambda: str(tmp_path))
    code = fob.main(["--kind", "plan", "--slot", "1430", "--trading-day", DAY, "--account", ACCOUNT,
                     "--now", NOW.isoformat()])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["reason"] == "no_valid_baseline" and out["fallback_full"] == ["day", "week"]


def test_plan_boundary_feeds_both_merges_end_to_end(tmp_path):
    in_overlap = _order("p1", "18:51:30")
    _assembled(tmp_path, "1330", [_order("o1", "15:00:00"), in_overlap], snapshot_at="2026-11-12T18:52:41Z")
    _assembled(tmp_path, "1330", [_order("m", "15:00:00", day=MONDAY), _order("o1", "15:00:00"), in_overlap],
               kind="week", snapshot_at=f"{DAY}T13:52:41-05:00")
    plan = _plan(tmp_path)
    increment = tmp_path / "inc.json"
    increment.write_text(json.dumps({"orders": [_order("p2", "19:31:00"), in_overlap]}), encoding="utf-8")
    for kind, env_day, count in (("day", DAY, 3), ("week", MONDAY, 4)):
        out = tmp_path / f"{kind}.json"
        code = merge.main([
            "--base", plan["merge"][kind]["base"], "--account", ACCOUNT, "--trading-day", env_day,
            "--created-at-gte", plan["fetch_created_at_gte"], "--snapshot-at", f"{DAY}T14:33:00-05:00",
            "--overlap-seconds", "120", "--out", str(out), str(increment),
        ])
        assert code == 0, kind
        assert json.loads(out.read_text())["ingest"]["order_count"] == count
