"""Tests for ``scripts/budget_exhaustion_count.py`` (written before the implementation; awaiting operator approval).

Why: the Markov category-1 ideas (MDP cash pacing, absorbing tier chain) only pay off if scheduled slots often had a
live-sized buy that the remaining daily or weekly budget could not fund. docs/adr-budget-exhaustion-replay.md
(revision 3, operator decisions 2026-11-23) counts that from each slot's own Gate 5 document instead of replaying
sizing: ``desired_usd`` (3:30: ``suggested_amount_usd``) is already what live code sized.

Fixtures are REAL slot files from the live cron tracking tree, copied 2026-11-23 into
``scripts/tests/fixtures/budget_exhaustion/`` with the tree's own layout, and scrubbed: each envelope's
``account_number`` is the synthetic ``999000111``; envelope rows keep only the fields the row parser reads (``ref_id``
dropped, its all-digit UUID tails trip the account-shape guard); Gate 5 documents drop the unused ``candidates``
evaluation list (3:30 keeps only its BUY rows, which ARE its buy list) and ``planner_candidates``. Slots:

  * market_open_dca/20261120_0935ET  weekly cap binds ($849.18 of $900 spent before the slot); live
    ``budget_authorized_cands`` names the 6 that fit; Gate 9 recorded 54 ``daily_cap`` drops.
  * ce16ba6b/scheduled_runs/20261120_1530ET  the 3:30 shape; stress tier, recorded effective weekly cap $1,000;
    15 tickers already bought earlier that day; Gate 9 notes "APH 10.00 first not fitting remaining 2.87".
  * morning_dca/20261113_1030ET  ``ABORT_GATE6_HISTORY_INCOMPLETE`` with 70 candidates, no envelope.
  * intraday_dca/20261110_1330ET  ``NO_ORDERS_DAILY_CAP_EXHAUSTED``, no week envelope of its own.
  * market_open_dca/20261111_0935ET  neighbour envelope only (no Gate 5 document): the earliest week-of-11-09
    envelope snapshotted after 11-10 13:30's trigger, used for the derived figure.

Contract (operator decisions 2026-11-23, ADR section 8):
  * ``find_slots(root)`` -> sorted slot directories under the five stream roots that hold a Gate 5 document. Manual
    runs (a ``manual`` directory name, or ``run_origin`` other than ``scheduled``) are excluded.
  * ``load_gate5_candidates(slot_dir)`` -> ``list[dca_runner.Candidate]`` from ``cands`` (``desired_usd``,
    ``multiplier``, ``drop_pct``) or, for 3:30, ``candidates`` rows with ``action == "BUY"``
    (``suggested_amount_usd``, ``buy_multiplier``, ``drop_vs_previous_close_pct``). The exact
    ``candidates_<YYYY-MM-DD>_<HHMM>.json`` name wins; otherwise one ``candidates*.json`` (or midday's
    ``gen_out.json``); several with no exact name raises ``SlotUnresolved("ambiguous_gate5")``.
  * ``pre_slot_rows(raw_rows, triggered_at, own_order_ids)`` -> ``(kept, disagreements)``. Q3 (c): a row is the
    slot's own when its id is in the slot's ``finalize_orders.json`` / ``submitted_orders.csv`` OR it was created at
    or after the trigger; both are dropped, and every row where the two tests disagree is listed by order id.
  * ``allocate(candidates, snapshot, *, daily_cap_usd, weekly_cap_usd)`` -> ``(placed, skipped)``: plan_orders' own
    allocation order (``dca_runner._allocation_priority_key``) and its stop-at-first-boundary rule with its
    ``SKIPPED_*`` reasons, but WITHOUT plan_orders' per-candidate refusals (those read today's ACTIVE_TICKERS and
    today's calibration config, which refuse historical per-ticker sizing; see the parity test).
  * ``count_slot(slot_dir, root)`` -> ``SlotResult`` with ``outcome`` in counted / aborted / unresolved.
    Q2: an abort at or before Gate 6 for a non-budget reason is ``aborted`` (candidates and their dollars kept).
    Q5: caps from ``finalize_budget.json`` (the recorded, effective dollars, not the tier name); missing -> Normal
    $180/$900 (``caps_source == "default_normal"``) except 3:30, which is unresolved ``no_caps``. Buying power ignored.
    Q6: a candidate whose ticker had a same-day buy before the slot is a ``repeat``: own bucket, not allocated.
    Q7: ``daily_only_missed`` re-runs the allocation with the weekly cap lifted.
    Q4 (changed): a slot without its own week envelope is ``unresolved`` / ``no_week_envelope`` in the headline, and
    ``derived`` holds a second result (``evidence == "derived"``) built from the earliest same-ISO-week envelope,
    from any stream, snapshotted at or after the trigger, rows filtered to before the trigger.
  * ``summarize(results)`` -> dict: ``headline`` (direct evidence only), ``derived`` (derived only), ``aborted``,
    ``unresolved`` (by reason) and ``unresolved_share``.
  * CLI ``main(["--root", path, "--json"])``: read-only, exit 0, never prints an account number.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import dca_runner

# Test-first: skip (not error) until the module exists, so the rest of scripts/tests still collects.
bec = pytest.importorskip("scripts.budget_exhaustion_count")

FIXTURES = Path(__file__).parent / "fixtures" / "budget_exhaustion"
A = FIXTURES / "market_open_dca" / "20261120_0935ET"
B = FIXTURES / "ce16ba6b" / "scheduled_runs" / "20261120_1530ET"
C = FIXTURES / "morning_dca" / "20261113_1030ET"
D = FIXTURES / "intraday_dca" / "20261110_1330ET"
NEIGHBOUR = FIXTURES / "market_open_dca" / "20261111_0935ET"


def _tickers(pairs):
    return [pair[0] for pair in pairs]


def _usd(items):
    return round(sum(item[1] for item in items), 2)


# ---------------------------------------------------------------- real fixtures: discovery and reading


def test_find_slots_lists_scheduled_slots_with_a_gate5_document():
    assert bec.find_slots(FIXTURES) == sorted([A, B, C, D])  # NEIGHBOUR has no Gate 5 document


def test_reads_cands_shape():
    cands = bec.load_gate5_candidates(A)
    assert len(cands) == 60
    tsm = next(c for c in cands if c.ticker == "TSM")
    assert (tsm.amount_usd, tsm.multiplier) == (8.3, 8.3025)
    # The slot's own per-ticker sizing evidence travels with the row (plan_orders checks it in the parity test).
    assert (tsm.sizing_base_usd, tsm.sizing_min_drop_pct, tsm.sizing_max_drop_pct) == (1.0, 4.577, 4.73)


def test_reads_1530_candidates_shape_buy_rows_only():
    cands = bec.load_gate5_candidates(B)
    assert len(cands) == 82
    assert all(c.amount_usd > 0 for c in cands)
    mu = next(c for c in cands if c.ticker == "MU")
    assert mu.amount_usd == 10.0


# ---------------------------------------------------------------- real fixtures: counted slots


def test_weekly_cap_slot_matches_live_budget_authorized_list():
    result = bec.count_slot(A, FIXTURES)
    assert (result.outcome, result.evidence, result.stream) == ("counted", "direct", "market_open")
    assert result.caps == (180.0, 900.0) and result.caps_source == "finalize_budget"
    # The slot's own Gate 6 survivors, in allocation order.
    assert _tickers(result.placed) == ["TSM", "SPCX", "AAAU", "ANET", "VCX", "AMD"]
    assert result.authorized_tickers == _tickers(result.placed)
    assert len(result.missed) == 54 == result.gate9_daily_cap
    assert {m.cap for m in result.missed} == {dca_runner.SKIPPED_WEEKLY_CAP}
    assert result.missed[0].ticker == "NVDA"
    assert _usd([(m.ticker, m.usd) for m in result.missed]) == pytest.approx(208.59)


def test_pre_slot_spend_excludes_the_slots_own_orders():
    result = bec.count_slot(A, FIXTURES)
    # The envelope was snapshotted at 09:47:05 after this slot's AAAU order; the 261-row week minus it.
    assert result.pre_slot_day_usd == pytest.approx(0.0)
    assert result.pre_slot_week_usd == pytest.approx(849.18)
    assert result.own_order_disagreements == []


def test_1530_uses_recorded_effective_weekly_cap_and_buckets_same_day_repeats():
    result = bec.count_slot(B, FIXTURES)
    assert (result.outcome, result.stream) == ("counted", "final_1530")
    assert result.caps == (360.0, 1000.0)  # stress tier, effective weekly cap with the rollover reserve
    assert result.pre_slot_day_usd == pytest.approx(86.85)
    assert result.pre_slot_week_usd == pytest.approx(936.03)
    assert len(result.repeats) == 15 and _usd(result.repeats) == pytest.approx(119.37)
    assert {"NVDA", "TSM", "ANET", "AVGO", "GS"} <= set(_tickers(result.repeats))
    assert _tickers(result.placed) == ["MU", "AMD", "DRAM", "RGTI", "AMAT", "QUBT"]
    assert result.missed[0].ticker == "APH" and result.missed[0].usd == 10.0  # matches the live Gate 9 note
    assert len(result.missed) == 61
    assert {m.cap for m in result.missed} == {dca_runner.SKIPPED_WEEKLY_CAP}
    assert _usd([(m.ticker, m.usd) for m in result.missed]) == pytest.approx(396.02)
    assert not set(_tickers(result.repeats)) & {m.ticker for m in result.missed}


def test_daily_only_counterfactual_lifts_the_weekly_cap():
    a, b = bec.count_slot(A, FIXTURES), bec.count_slot(B, FIXTURES)
    assert len(a.daily_only_missed) == 30
    assert _usd([(m.ticker, m.usd) for m in a.daily_only_missed]) == pytest.approx(78.55)
    assert len(b.daily_only_missed) == 39
    assert _usd([(m.ticker, m.usd) for m in b.daily_only_missed]) == pytest.approx(189.95)
    assert {m.cap for m in a.daily_only_missed + b.daily_only_missed} == {dca_runner.SKIPPED_DAILY_CAP}


# ---------------------------------------------------------------- real fixtures: aborted and derived


def test_gate6_history_abort_is_its_own_bucket():
    result = bec.count_slot(C, FIXTURES)
    assert (result.outcome, result.reason) == ("aborted", "ABORT_GATE6_HISTORY_INCOMPLETE")
    assert result.candidate_count == 70
    assert result.candidate_usd == pytest.approx(281.44)
    assert result.missed == [] and result.derived is None


def test_slot_without_week_envelope_is_unresolved_with_a_derived_figure():
    result = bec.count_slot(D, FIXTURES)
    assert (result.outcome, result.reason, result.missed) == ("unresolved", "no_week_envelope", [])
    derived = result.derived
    assert (derived.outcome, derived.evidence) == ("counted", "derived")
    assert derived.envelope == "market_open_dca/20261111_0935ET/orders_pages_week_2026-11-09_0935.json"
    assert derived.pre_slot_day_usd == pytest.approx(195.89)  # already over $180: the operator waived it at 11:30
    assert derived.pre_slot_week_usd == pytest.approx(235.98)
    assert len(derived.repeats) == 39 and _usd(derived.repeats) == pytest.approx(139.05)
    assert len(derived.missed) == 10 and derived.missed[0].ticker == "DFTX"
    assert {m.cap for m in derived.missed} == {dca_runner.SKIPPED_DAILY_CAP}
    assert _usd([(m.ticker, m.usd) for m in derived.missed]) == pytest.approx(20.70)


def test_summary_over_the_fixture_tree():
    summary = bec.summarize([bec.count_slot(slot, FIXTURES) for slot in bec.find_slots(FIXTURES)])
    head = summary["headline"]
    assert head["slots_counted"] == 2
    assert head["sessions_observed"] == 1 and head["sessions_with_missed"] == 1
    assert head["missed_count"] == 115 and head["missed_usd"] == pytest.approx(604.61)
    assert head["by_cap"][dca_runner.SKIPPED_WEEKLY_CAP] == {"count": 115, "usd": pytest.approx(604.61)}
    assert head["by_stream"]["market_open"]["count"] == 54 and head["by_stream"]["final_1530"]["count"] == 61
    assert head["repeats"] == {"count": 15, "usd": pytest.approx(119.37)}
    assert head["daily_only"] == {"count": 69, "usd": pytest.approx(268.50)}
    assert summary["derived"]["slots_counted"] == 1
    assert summary["derived"]["missed_count"] == 10 and summary["derived"]["missed_usd"] == pytest.approx(20.70)
    assert summary["aborted"]["slots"] == 1
    assert summary["aborted"]["candidate_usd"] == pytest.approx(281.44)
    assert summary["aborted"]["by_status"] == {"ABORT_GATE6_HISTORY_INCOMPLETE": 1}
    assert summary["unresolved"] == {"no_week_envelope": 1}
    assert summary["unresolved_share"] == pytest.approx(1 / 3)  # unresolved / (counted + unresolved)


# ---------------------------------------------------------------- CLI


def test_cli_prints_json_summary_and_never_the_account(capsys):
    assert bec.main(["--root", str(FIXTURES), "--json"]) == 0
    out = capsys.readouterr().out
    document = json.loads(out)
    assert document["headline"]["missed_count"] == 115
    assert len(document["slots"]) == 4
    assert "999000111" not in out


def test_cli_text_report_names_each_bucket(capsys):
    assert bec.main(["--root", str(FIXTURES)]) == 0
    out = capsys.readouterr().out
    for label in ("headline", "derived", "aborted", "unresolved", "weekly_cap_reached"):
        assert label in out


def test_cli_is_read_only():
    def snapshot():
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in FIXTURES.rglob("*") if p.is_file()}

    before = snapshot()
    bec.main(["--root", str(FIXTURES), "--json"])
    assert snapshot() == before


# ---------------------------------------------------------------- live-code parity


def test_allocate_matches_plan_orders_on_the_candidates_plan_orders_accepts():
    """allocate() differs from plan_orders only by skipping its per-candidate refusals.

    On the real 09:35 candidates, minus whatever today's config refuses, the two must agree exactly across
    a weekly-binding, a daily-binding, an order-count-binding and a non-binding snapshot.
    """
    cands = bec.load_gate5_candidates(A)
    tier = next(t for t in dca_runner.BUDGET_TIERS if t.name == "normal")
    day = date(2026, 11, 20)
    # Uncapped probe: once a cap stops the batch, plan_orders skips the rest BEFORE its refusal checks, so a
    # capped probe would hide refusals that a later, roomier snapshot then hits.
    uncapped = dataclasses.replace(tier, daily_cap_usd=1e9, weekly_cap_usd=1e9)
    probe = dca_runner.plan_orders(cands, dca_runner.BudgetSnapshot(day, 0.0, 0.0, 0), uncapped)
    assert probe.skipped == []
    # Measured 2026-11-23: with the slot's own sizing bounds, today's config refuses only dated no-buy windows
    # (7 tickers, VCX among them, although live bought VCX that morning). That is why allocate() skips refusals.
    # Only the sizing-consistency refusal is pinned: dated windows and the universe move with today's config.
    assert dca_runner.REFUSED_INCONSISTENT_MULTIPLIER not in {reason for _, reason in probe.refused}
    refused = {ticker for ticker, _ in probe.refused}
    accepted = [c for c in cands if c.ticker not in refused]
    assert len(accepted) >= 50  # keeps the comparison meaningful
    snapshots = [
        dca_runner.BudgetSnapshot(day, 0.0, 849.18, 0),  # weekly binds
        dca_runner.BudgetSnapshot(day, 150.0, 150.0, 5),  # daily binds
        dca_runner.BudgetSnapshot(day, 0.0, 0.0, dca_runner.MAX_ORDERS_PER_DAY - 3),  # order count binds
        dca_runner.BudgetSnapshot(day, 0.0, 0.0, 0),  # nothing binds before the daily cap
    ]
    for snap in snapshots:
        plan = dca_runner.plan_orders(accepted, snap, tier)
        placed, skipped = bec.allocate(accepted, snap, daily_cap_usd=tier.daily_cap_usd,
                                       weekly_cap_usd=tier.weekly_cap_usd)
        assert [(o.ticker, o.amount_usd) for o in plan.to_place] == placed
        assert plan.skipped == [(ticker, reason) for ticker, _usd_amount, reason in skipped]
        assert plan.refused == []


def test_count_slot_calls_the_live_snapshot_and_priority_functions(monkeypatch):
    calls = {"snapshot": 0, "priority": 0}
    real_snapshot, real_key = dca_runner.budget_snapshot, dca_runner._allocation_priority_key

    def spy_snapshot(*args, **kwargs):
        calls["snapshot"] += 1
        return real_snapshot(*args, **kwargs)

    def spy_key(cand):
        calls["priority"] += 1
        return real_key(cand)

    monkeypatch.setattr(dca_runner, "budget_snapshot", spy_snapshot)
    monkeypatch.setattr(dca_runner, "_allocation_priority_key", spy_key)
    bec.count_slot(A, FIXTURES)
    assert calls["snapshot"] >= 1 and calls["priority"] >= 60


# ---------------------------------------------------------------- synthetic edge cases

TRIGGER = "2026-11-18T16:40:00+00:00"  # Wednesday 11:40 ET
MONDAY = "2026-11-16"


def _row(order_id, symbol, created_at, usd, state="filled"):
    return {"id": order_id, "symbol": symbol, "side": "buy", "state": state, "created_at": created_at,
            "dollar_based_amount": {"amount": f"{usd:.2f}", "currency_code": "USD"}, "placed_agent": "agentic"}


def _envelope(path, rows, *, monday=MONDAY, snapshot_at="2026-11-18T11:50:00-05:00"):
    path.write_text(json.dumps({"account_number": "999000111", "trading_day": monday, "snapshot_at": snapshot_at,
                                "pages": [{"orders": rows}]}))


def _slot(root, rel, *, cands=(("ZZA", 5.0, 5.0, 3.0),), status="ORDERS_PLACED", origin="scheduled",
          trigger=TRIGGER, budget=(180, 900), own=(), gate5_name="candidates_2026-11-18_1130.json"):
    slot = root / rel
    slot.mkdir(parents=True)
    if gate5_name:
        rows = [{"ticker": t, "desired_usd": usd, "multiplier": m, "drop_pct": dp} for t, usd, m, dp in cands]
        (slot / gate5_name).write_text(json.dumps({"trading_day": "2026-11-18", "cands": rows}))
    if trigger is not None:
        (slot / "run_summary.json").write_text(json.dumps({"status": status, "run_classification": {
            "run_origin": origin, "triggered_at_utc": trigger, "trading_day": "2026-11-18"}}))
    if budget is not None:
        (slot / "finalize_budget.json").write_text(json.dumps(
            {"budget_tier": "normal", "daily_cap_usd": budget[0], "weekly_cap_usd": budget[1]}))
    (slot / "finalize_orders.json").write_text(json.dumps([{"order_id": i, "ticker": "ZZA"} for i in own]))
    return slot


def test_own_order_exclusion_flags_id_and_time_disagreements():
    trigger = datetime(2026, 11, 18, 16, 40, tzinfo=timezone.utc)
    rows = [
        _row("before-other", "ZZA", "2026-11-18T15:00:00Z", 4.0),  # kept
        _row("before-own", "ZZB", "2026-11-18T16:00:00Z", 3.0),  # own id but before the trigger: dropped, flagged
        _row("after-other", "ZZC", "2026-11-18T16:45:00Z", 2.0),  # after the trigger, not own: dropped, flagged
        _row("after-own", "ZZD", "2026-11-18T16:46:00Z", 1.0),  # both tests agree: dropped, not flagged
    ]
    kept, disagreements = bec.pre_slot_rows(rows, trigger, {"before-own", "after-own"})
    assert [r["id"] for r in kept] == ["before-other"]
    assert sorted(disagreements) == ["after-other", "before-own"]


def test_manual_runs_are_not_slots(tmp_path):
    _slot(tmp_path, "intraday_dca/manual_noorder_20261118")
    _slot(tmp_path, "intraday_dca/20261118_1330ET", origin="manual")
    kept = _slot(tmp_path, "intraday_dca/20261118_1130ET")
    assert bec.find_slots(tmp_path) == [kept]


def test_missing_caps_default_to_normal_except_at_1530(tmp_path):
    intraday = _slot(tmp_path, "intraday_dca/20261118_1130ET", budget=None)
    _envelope(intraday / f"orders_pages_week_{MONDAY}_1130.json", [])
    result = bec.count_slot(intraday, tmp_path)
    assert (result.outcome, result.caps, result.caps_source) == ("counted", (180.0, 900.0), "default_normal")
    final = _slot(tmp_path, "ce16ba6b/scheduled_runs/20261118_1530ET", budget=None,
                  gate5_name="candidates_2026-11-18_1530.json")
    _envelope(final / f"orders_pages_week_{MONDAY}_1530.json", [])
    assert (bec.count_slot(final, tmp_path).outcome, bec.count_slot(final, tmp_path).reason) == (
        "unresolved", "no_caps")


def test_week_envelope_for_another_week_is_unresolved(tmp_path):
    slot = _slot(tmp_path, "intraday_dca/20261118_1130ET")
    _envelope(slot / "orders_pages_week_2026-11-09_1130.json", [], monday="2026-11-09")
    result = bec.count_slot(slot, tmp_path)
    assert (result.outcome, result.reason) == ("unresolved", "week_envelope_wrong_week")


def test_gate5_document_choice(tmp_path):
    exact = _slot(tmp_path, "intraday_dca/20261118_1130ET")
    (exact / "candidates_pretty.json").write_text(json.dumps({"cands": []}))
    assert [c.ticker for c in bec.load_gate5_candidates(exact)] == ["ZZA"]  # the exact name wins
    ambiguous = _slot(tmp_path, "intraday_dca/20261118_1330ET", gate5_name="candidates_raw.json")
    (ambiguous / "candidates_pretty.json").write_text(json.dumps({"cands": []}))
    with pytest.raises(bec.SlotUnresolved, match="ambiguous_gate5"):
        bec.load_gate5_candidates(ambiguous)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ABORT_GATE6_HISTORY_INCOMPLETE", "aborted"),
        ("ABORT_GATE6_HISTORY_NOT_PERSISTABLE", "aborted"),
        ("ABORT_GATE4_HISTORY_UNSAVEABLE", "aborted"),
        ("ABORT_GATE5", "aborted"),
        ("ABORT_GATE6", "aborted"),
        ("ABORT_GATE6_DAILY_CAP_EXHAUSTED", "counted"),  # a budget abort IS the thing being counted
        ("ABORT_GATE6_BUDGET", "counted"),
        ("NO_ORDERS_WEEKLY_CAP_EXHAUSTED", "counted"),
        ("ABORT_GATE6_5_TIME_BUDGET", "counted"),  # after Gate 6: its budget decision stands
        ("ABORT_GATE7_PLATFORM_REFUSAL", "counted"),
        ("ORDERS_PLACED", "counted"),
    ],
)
def test_status_classification(status, expected):
    assert bec.classify_status(status) == expected


def test_derived_needs_a_same_week_envelope_snapshotted_at_or_after_the_trigger(tmp_path):
    slot = _slot(tmp_path, "intraday_dca/20261118_1130ET")
    earlier = tmp_path / "market_open_dca" / "20261118_0935ET"
    earlier.mkdir(parents=True)
    _envelope(earlier / f"orders_pages_week_{MONDAY}_0935.json", [], snapshot_at="2026-11-18T09:50:00-05:00")
    other_week = tmp_path / "market_open_dca" / "20261123_0935ET"
    other_week.mkdir(parents=True)
    _envelope(other_week / "orders_pages_week_2026-11-23_0935.json", [], monday="2026-11-23",
              snapshot_at="2026-11-23T09:50:00-05:00")
    result = bec.count_slot(slot, tmp_path)
    assert (result.outcome, result.reason) == ("unresolved", "no_week_envelope")
    assert (result.derived.outcome, result.derived.reason) == ("unresolved", "no_neighbour_envelope")

    later = tmp_path / "intraday_dca" / "20261118_1330ET"
    later.mkdir(parents=True)
    _envelope(later / f"orders_pages_week_{MONDAY}_1330.json",
              [_row("x1", "ZZB", "2026-11-17T15:00:00Z", 7.0), _row("x2", "ZZC", "2026-11-18T17:00:00Z", 9.0)],
              snapshot_at="2026-11-18T13:50:00-05:00")
    derived = bec.count_slot(slot, tmp_path).derived
    assert (derived.outcome, derived.evidence) == ("counted", "derived")
    assert derived.pre_slot_week_usd == pytest.approx(7.0)  # x2 was created after the trigger


def test_unusable_envelope_row_is_unresolved(tmp_path):
    slot = _slot(tmp_path, "intraday_dca/20261118_1130ET")
    bad = _row("bad", "ZZA", "2026-11-17T15:00:00Z", 1.0)
    bad["dollar_based_amount"] = {"amount": "NaN"}
    _envelope(slot / f"orders_pages_week_{MONDAY}_1130.json", [bad])
    result = bec.count_slot(slot, tmp_path)
    assert (result.outcome, result.reason) == ("unresolved", "envelope_unusable")


def test_missing_trigger_time_is_unresolved(tmp_path):
    slot = _slot(tmp_path, "intraday_dca/20261118_1130ET", trigger=None)
    _envelope(slot / f"orders_pages_week_{MONDAY}_1130.json", [])
    result = bec.count_slot(slot, tmp_path)
    assert (result.outcome, result.reason) == ("unresolved", "no_trigger_time")
