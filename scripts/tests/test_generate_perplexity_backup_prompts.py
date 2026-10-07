"""Tests for ``scripts/generate_perplexity_backup_prompts.py`` and the committed backup prompts.

Phase 5 of docs/adr-plan-first-backup-executor.md. The six Perplexity backup prompts are thin
I/O prompts generated from repo config, so they cannot drift from it and cannot silently lose a
hard rule. Perplexity cannot read this repo, so every hard rule must be INLINE in each prompt.

Expected behavior is derived from the ADR and ``claude/PERPLEXITY-CLAUDE-COORDINATION.md``, not
from the generator's output.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import dca_config
import dca_runner
from scripts import generate_perplexity_backup_prompts as gen

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "claude" / "dca-executor-prompts" / "perplexity"
CONFIG = REPO / "config"

# (file stem prefix, backup task id, fire times ET, slot ids it backs up, latest placement ET)
EXPECTED = [
    ("0955ET", "4412c3c4", ("09:55",), ("0935",), "10:30"),
    ("1050ET", "e028361e", ("10:50",), ("1030",), "11:30"),
    ("1150ET", "e3590042", ("11:50",), ("1130",), "12:30"),
    ("1250ET", "062605c6", ("12:50",), ("1230",), "13:30"),
    ("1350-1450ET", "54ab24d2", ("13:50", "14:50"), ("1330", "1430"), "14:30 for the 13:50 fire, 15:30 for the 14:50 fire"),
    ("1545ET", "f83b8d62", ("15:45",), ("1530",), "16:00"),
]


@pytest.fixture(scope="module")
def prompts():
    return gen.build_prompts()


def _by_prefix(prompts, prefix):
    matches = [text for name, text in prompts.items() if name.startswith(prefix + "-")]
    assert len(matches) == 1, prefix
    return matches[0]


def test_exactly_six_prompts_with_the_documented_names(prompts):
    assert len(prompts) == 6
    assert all(name.endswith("-backup.txt") for name in prompts)
    assert sorted(name.split("-")[0] for name in prompts) == sorted(e[0].split("-")[0] for e in EXPECTED)


def test_committed_prompts_match_the_generator_byte_for_byte(prompts):
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in OUT_DIR.glob("*-backup.txt")}
    assert on_disk == prompts, "regenerate with: python3 -m scripts.generate_perplexity_backup_prompts --write"


def test_generation_is_deterministic():
    assert gen.build_prompts() == gen.build_prompts()


@pytest.mark.parametrize(("prefix", "task_id", "fires", "slots", "latest"), EXPECTED, ids=lambda v: str(v)[:12])
def test_each_prompt_names_its_task_fire_times_slots_and_deadline(prompts, prefix, task_id, fires, slots, latest):
    text = _by_prefix(prompts, prefix)
    assert task_id in text
    for fire in fires:
        assert fire in text
    for slot in slots:
        assert f"slot {slot}" in text
    assert f"Latest placement: {latest}" in text


def test_only_the_final_prompt_carries_the_accepted_time_limitation(prompts):
    final = _by_prefix(prompts, "1545ET")
    assert "KNOWN LIMITATION (accepted 2026-10-06)" in final
    for prefix in ("0955ET", "1050ET", "1150ET", "1250ET", "1350-1450ET"):
        assert "KNOWN LIMITATION" not in _by_prefix(prompts, prefix)


# -- hard rules are inline, and agree with live config -----------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_hard_rules_are_inline_in_every_prompt(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    for needle in (
        "BUY-ONLY",
        "No sells, no cancels",
        "no review_equity_order",
        "gfd",
        "regular_hours",
        "QBUT",
        "ONE ORDER PER TICKER PER SLOT",
        "BROKER ORDER HISTORY IS THE ONLY EVIDENCE",
        "Mac content is never purchase evidence",
        "FAIL CLOSED",
        "failed_unexpected_error",
        "place NOTHING",
        "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}",
    ):
        assert needle in text, needle


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_cap_figures_match_live_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    normal = next(t for t in __import__("near_close_dca").BUDGET_TIERS if t.name == "normal")
    assert f"at most ${dca_runner.MAX_PER_ORDER_USD:g}" in text
    assert f"at most ${normal.daily_cap_usd:g} across ALL orders" in text
    assert f"at most ${normal.weekly_cap_usd:g}" in text
    assert f"at most {dca_runner.MAX_ORDERS_PER_DAY} buy attempts" in text
    breakers = json.loads((CONFIG / "budgets.json").read_text())["circuit_breakers"]
    assert f"{breakers['max_consecutive_placement_failures']} consecutive" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_universe_and_version_stamps_match_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    expected_hash = hashlib.sha256(("\n".join(sorted(set(config.tickers))) + "\n").encode()).hexdigest()
    assert expected_hash in text
    for ticker in config.tickers:
        assert re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text), ticker
    versions = [json.loads((CONFIG / n).read_text())["version"] for n in ("sizing_ladder.json", "per_ticker_sizing.json")]
    assert "+".join(versions) in text
    # QBUT may appear only as the do-not-buy symbol, never inside the universe list.
    universe_line = next(line for line in text.splitlines() if line.startswith("UNIVERSE ("))
    assert "QBUT" not in universe_line


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_market_calendar_is_embedded_from_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    calendar = json.loads((CONFIG / "market_calendar.json").read_text())
    for day in (*calendar["holidays_closed"], *calendar["early_close_1300"]):
        assert day in text, day


# -- Mac-first flow ----------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_mac_first_replaces_the_old_never_check_the_mac_rule(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "NEVER check the Mac" not in text
    assert "pc CLI" not in text
    assert "MAC-FIRST" in text
    assert "list_devices" in text
    assert '"Mac Studio"' in text
    assert "run_local_subagent" in text
    assert "60 seconds" in text
    assert "3 minutes" in text
    for value in ("mac_plan", "mac_compute", "cloud_compute"):
        assert value in text
    for mac_status in ("not_found", "offline", "timeout", "device_error"):
        assert mac_status in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_two_evidence_polls_bracket_planning_and_a_planner_decides_the_order_list(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert text.index("EVIDENCE POLL 1") < text.index("MAC-FIRST") < text.index("EVIDENCE POLL 2") < text.index("PLACE")
    for command in ("scripts.backup_planner plan", "scripts.backup_planner compute", "scripts.backup_planner finalize"):
        assert command in text, command
    assert "Never resize" in text
    assert "NEVER compute ladder multipliers" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_state_record_and_notification_are_specified(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "plan_source" in text
    for status in ("placed", "partial", "nothing_to_place", "stood_down_unrecognized_order",
                   "skipped_market_closed", "skipped_outside_window", "failed_unexpected_error"):
        assert status in text, status
    assert "${DCA_ALERT_EMAIL_ADDRESS}" in text
    assert "never a hardcoded address" in text


# -- hygiene -----------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_no_em_dash_no_account_shaped_literal_no_secret(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "—" not in text
    assert not re.search(r"(?<![.\w])\d{6,12}(?![\w.])", text)
    assert "@" not in text.replace("${DCA_ALERT_EMAIL_ADDRESS}", "")  # no literal email address


def test_the_two_fire_prompt_resolves_its_slot_from_the_clock(prompts):
    text = _by_prefix(prompts, "1350-1450ET")
    assert "13:40" in text and "14:20" in text and "14:40" in text and "15:20" in text
    assert "slot 1330" in text and "slot 1430" in text


def test_single_fire_prompts_do_not_mention_other_slots_windows(prompts):
    text = _by_prefix(prompts, "1050ET")
    assert "slot 1330" not in text and "slot 1430" not in text


def test_write_and_check_cli_round_trip(tmp_path, capsys):
    assert gen.main(["--write", "--out-dir", str(tmp_path)]) == 0
    assert gen.main(["--check", "--out-dir", str(tmp_path)]) == 0
    victim = next(tmp_path.glob("*-backup.txt"))
    victim.write_text(victim.read_text() + "tampered")
    assert gen.main(["--check", "--out-dir", str(tmp_path)]) == 1
    capsys.readouterr()


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_both_polls_fetch_from_the_iso_week_monday_so_the_weekly_cap_is_provable(prompts, prefix):
    """Copilot review on PR 1619: budget_snapshot sums the whole ISO week, so a today-only fetch
    understates week_notional_usd and can approve a batch past the weekly cap."""
    text = _by_prefix(prompts, prefix)
    assert "00:00 ET on the Monday of the current ISO week" in text
    assert "created_at_gte set to today" not in text
    assert "repeat the full poll of step 2" in text  # poll 2 inherits the same window


# -- review gaps found 2026-10-07 (ADR "Open design gaps") ------------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_ticker_rule_follows_the_top_up_policy_not_a_day_wide_ban(prompts, prefix):
    """config/budgets.json one_order_per_ticker_per_day is false: a later slot may top a ticker up."""
    text = _by_prefix(prompts, prefix)
    assert "ONE ORDER PER TICKER PER DAY" not in text
    assert "an earlier slot" in text and "resized top-up" in text
    assert "a computed plan never tops up" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_buying_power_and_spreads_are_fetched_and_passed_to_the_planner(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    compute = next(line for line in text.splitlines() if "scripts.backup_planner compute" in line)
    finalize = next(line for line in text.splitlines() if "scripts.backup_planner finalize" in line)
    assert "--buying-power" in compute
    assert "--buying-power" in finalize and "--fresh-spreads" in finalize
    assert "fresh_spreads.json" in text and "buying power" in text.lower()
    assert "never place with a missing or unreadable buying power" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_placement_happens_only_on_proceed_and_computed_plans_are_shadow_only(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "Place orders ONLY when the status is PROCEED" in text
    assert "SHADOW_ONLY" in text
    assert "would_place" in text
    # step 7 re-checks the spread immediately before each order, like the primaries' Gate 6.7
    assert "spread" in text.split("STEP 7 - PLACE THE ORDERS")[1].split("STEP 8")[0]


def test_latest_placement_times_come_from_the_library_table(prompts):
    import dca_order_plan

    for prefix, _, _, slots, _latest in EXPECTED:
        text = _by_prefix(prompts, prefix)
        for slot in slots:
            assert dca_order_plan.LATEST_PLACEMENT_ET[slot] in text
