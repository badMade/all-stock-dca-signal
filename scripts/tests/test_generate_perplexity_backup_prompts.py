"""Tests for ``scripts/generate_perplexity_backup_prompts.py`` and the committed backup prompts.

The six Perplexity prompts are generated from repo config and paired with the stamped planner.
The pasteable prompt is signal-first; the full reference prompt includes the planner-served hard rules and procedure.

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
    # The committed files are the PASTEABLE instructions; `prompts` (the full reference text) is what the
    # content tests below assert against.
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in OUT_DIR.glob("*-backup.txt")}
    assert on_disk == gen.build_instructions(), "regenerate with: python3 -m scripts.generate_perplexity_backup_prompts --write"


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
def test_every_prompt_carries_the_no_unprescribed_retries_rule(prompts, prefix):
    # The 2026-10-20 09:55 backup run burned its whole call budget re-querying order history
    # and writing ad-hoc scripts; the rule is inline (rule 11) so it binds before the planner
    # procedure is even fetched, and it must stay well inside the 20,000-character box.
    text = _by_prefix(prompts, prefix)
    assert "11. NO UNPRESCRIBED RETRIES OR LOOPS." in text
    # PR #1819 review: the rule must not forbid what the served procedure requires, and must not cap a batch by count.
    for prescribed in ("every pagination cursor", "second evidence poll", "late-start heartbeat recheck",
                       "check-placement steps", "NO_VALID_PLAN (exit 2) moves on to the next plan source",
                       "stop BETWEEN orders (never after a submission and before its check-placement)"):
        assert prescribed in text, prescribed
    assert "at 80 calls" not in text and "well under 60" not in text
    # 2026-10-30: the rules moved from the paste box into the stamped procedure's part 0 (credit plan, step 3).
    from scripts import backup_procedure

    task_id = next(e[1] for e in EXPECTED if e[0] == prefix)
    assert "11. NO UNPRESCRIBED RETRIES OR LOOPS." in backup_procedure.render_part(task_id, 0)
    assert len(_by_prefix(gen.build_instructions(), prefix)) < 20_000


def test_the_served_procedure_forbids_requerying_to_double_check_the_order_pull():
    from scripts import backup_procedure

    text = backup_procedure.PROCEDURE_TEMPLATE
    assert "make ONE call for the first page, then one call per cursor" in text
    assert "NEVER re-query with a different state, date, or window" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_hard_rules_are_inline_in_every_prompt(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    for needle in (
        "BUY-ONLY",
        "No sells, no cancels",
        "no review_equity_order",
        "gfd",
        "regular_hours",
        "ONE ORDER PER TICKER PER SLOT",
        "BROKER ORDER HISTORY IS THE ONLY EVIDENCE",
        "Mac content is never purchase evidence",
        "FAIL CLOSED",
        "failed_unexpected_error",
        "place NOTHING",
        "${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}",
    ):
        assert needle in text, needle

@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_cap_figures_match_live_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    tiers = __import__("near_close_dca").BUDGET_TIERS
    normal = next(t for t in tiers if t.name == "normal")
    assert f"at most ${dca_runner.MAX_PER_ORDER_USD:g}" in text
    # Normal-only (2026-10-22): only the Normal caps are stated, no other tier's, and an over-cap batch is trimmed.
    assert f"at most ${normal.daily_cap_usd:g} across ALL orders" in text
    assert f"at most ${normal.weekly_cap_usd:g}." in text
    for tier in tiers:
        if tier.name != "normal":
            assert f"${tier.daily_cap_usd:g} {tier.name.replace('_', '-').capitalize()}" not in text, tier.name
    assert "you never choose or escalate a tier yourself" in text
    assert "Place exactly to_place" in text and "do not back-fill" in text
    assert f"at most {dca_runner.MAX_ORDERS_PER_DAY} buy attempts" in text
    breakers = json.loads((CONFIG / "budgets.json").read_bytes())["circuit_breakers"]
    assert f"{breakers['max_consecutive_placement_failures']} consecutive" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_universe_and_version_stamps_match_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    expected_hash = hashlib.sha256(("\n".join(sorted(set(config.tickers))) + "\n").encode()).hexdigest()
    assert expected_hash in text and f"({len(config.tickers)} symbols)" in text
    versions = [json.loads((CONFIG / n).read_bytes())["version"] for n in ("sizing_ladder.json", "per_ticker_sizing.json")]
    assert "+".join(versions) in text


def _all_symbols():
    """Every symbol the program knows about: buyable, excluded, and whole-share."""
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    return set(config.tickers) | set(config.excluded_tickers) | set(getattr(config, "whole_share_tickers", ()) or ())


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_no_stock_is_named_in_any_prompt_the_universe_lives_in_config(prompts, prefix):
    """Owner direction 2026-10-08: stocks live in config. A list in a prompt is a second copy that drifts."""
    text = _by_prefix(prompts, prefix)
    found = sorted(s for s in _all_symbols() if re.search(rf"(?<![A-Za-z0-9_]){re.escape(s)}(?![A-Za-z0-9_])", text))
    assert not found, found


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_task_asks_the_planner_for_the_symbols_to_fetch(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "python3 backup_planner.pyz universe" in text and "python3 -m scripts.backup_planner universe" in text
    assert "UNIVERSE (" not in text  # no inline list; the command's output is the list
    assert "do-not-buy" in text.lower() and "planner" in text  # the exclusion is enforced by the planner, not by a name here


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_yahoo_is_never_mentioned_or_required(prompts, prefix):
    text = _by_prefix(prompts, prefix).lower()
    assert "yahoo" not in text and "yfinance" not in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_market_calendar_is_embedded_from_config(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    calendar = json.loads((CONFIG / "market_calendar.json").read_bytes())
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
def test_every_mac_command_is_gated_on_mac_path_abandoned_being_false(prompts, prefix):
    # Review of PR 1669: the budget branch abandoned the Mac path, but mac_status stayed "online" and
    # the later Mac commands keyed on it alone, so a timed-out run could re-enter the abandoned path.
    text = _by_prefix(prompts, prefix)
    assert "mac_path_abandoned" in text
    # (a) initialises it false, (b) sets it true when the budget is spent.
    assert "mac_path_abandoned to false" in text
    assert "set mac_path_abandoned to true" in text
    # Every line that invokes the Mac must also require the flag to be false.
    mac_lines = [
        line
        for line in text.splitlines()
        if "mac_status online" in line or "mac_status is online" in line
        or "run_local_subagent on the Mac" in line
        or "on the Mac (the same step 3 probe path)" in line
    ]
    assert mac_lines
    for line in mac_lines:
        if "(a)" in line[:4] or line.startswith("a."):
            continue
        assert "mac_path_abandoned" in line, line[:100]


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


# -- pre-procedure stand-down gate -------------------------------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_stand_down_gate_runs_after_setup_before_procedure_fetch(prefix):
    # ADR amendment 2026-10-25: a normal day (Claude succeeded) must stand down before the account lookup, the
    # 7-day get_equity_orders pull and any procedure fetch, which is where the Perplexity credits went.
    # 2026-10-30: the gate moved into the stamped procedure's part 0; the pasted prompt checks the build stamp
    # before it fetches part 0 (scripts/tests/test_backup_signal_prompt.py).
    from scripts import backup_procedure

    task_id = next(e[1] for e in EXPECTED if e[0] == prefix)
    pasted = _by_prefix(gen.build_instructions(), prefix)
    assert pasted.index("--build-info") < pasted.index(f"procedure --task {task_id} --part 0")
    text = backup_procedure.render_part(task_id, 0)
    heading = text.find("\nSTAND-DOWN GATE")
    assert heading != -1, "part 0 has no STAND-DOWN GATE section"
    section_end = text.index("PROCEDURE (steps 1-9)")
    section = text[heading:section_end]

    assert text.index("python3 backup_planner.pyz universe") < heading
    assert heading < text.index("PROCEDURE (steps 1-9)")
    assert section.index("gates --task ") < section.index("list_devices")
    assert section.index("list_devices") < section.index("scripts.local_heartbeat status")
    assert "If local_status is in_progress or succeeded" in section
    assert "stood_down_local_active" in section
    assert "no procedure part" in section
    assert "no get_equity_orders call" in section
    assert "${PPLX_RUN_STATE_DIR}/run_state_" in section
    for key in (
        "trading_day", "slot", "task_id", "account_last4", "fired_at_et", "plan_source", "mac_status",
        "mac_path_abandoned", "executor_orders", "routing", "mac_elapsed_seconds", "planner_build", "status",
        "orders_planned", "orders_placed", "skipped", "errors", "universe_hash", "sizing_version", "run_origin",
    ):
        assert key in section, key
    assert "scheduled_backup" in section
    assert "send no notification" in section
    assert "Any other result" in section
    assert "continue with the PROCEDURE below" in section
    assert text.index("get_equity_orders") > heading
    assert text.index("procedure --task") > heading
    assert "@@" not in text


# -- hygiene -----------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_no_em_dash_no_account_shaped_literal_no_secret(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "—" not in text
    # The part token is eight hex digits (see backup_procedure.part_token); it is all digits for about 2% of
    # builds, which is not an account number, so it is removed before the account-shape check.
    without_tokens = re.sub(r"--token [0-9a-f]{8}\b", "--token <hex>", text)
    assert not re.search(r"(?<![.\w])\d{6,12}(?![\w.])", without_tokens)
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
def test_both_polls_fetch_seven_days_so_the_weekly_cap_and_the_kill_switch_figure_are_both_provable(prompts, prefix):
    """The ISO-week cap needs Monday onward; the kill switch needs the trailing seven days. One window of seven
    full days back covers both, so the planner derives the figure itself (`--deployed-last-7d-usd auto`) and the
    task never sums anything. Copilot review on PR 1619 (weekly cap) still holds: never a today-only fetch."""
    text = _by_prefix(prompts, prefix)
    assert "00:00 ET seven days before today" in text
    assert "created_at_gte set to today" not in text
    assert "Monday of the current ISO week" not in text
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


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_local_first_routing_is_inline_and_unknown_stands_down(prompts, prefix):
    text = next(t for name, t in prompts.items() if name.startswith(prefix))
    assert "LOCAL FIRST" in text
    assert "scripts.local_heartbeat status" in text
    assert "300 seconds" in text
    # Operator decisions 2026-10-08: no lease means act; unknown means stand down and notify.
    assert "stood_down_local_unknown" in text and "Never act on unknown" in text
    assert "stood_down_local_active" in text
    assert "A cloud run places nothing by itself" in text  # 2026-10-13: step 7 may place, only on PROCEED
    assert "routing {path perplexity-fallback or none" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_account_is_looked_up_never_stored(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
    assert "exactly one account qualifies" in text
    assert "account_last4" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_late_start_guard_and_per_order_repull_are_inline(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "LATE-START GUARD" in text and "wait 5 minutes" in text
    assert "PER-ORDER REPULL" in text and "Never use a midnight window" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_operational_lines_are_back(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    for needle in ("${PPLX_RUN_STATE_DIR}", "submitted_orders.csv", "pplx project files sync",
                   "pplx automation suppress-run-notification", "NEVER post to Slack"):
        assert needle in text, needle


# -- the sandbox file and the safety gate (2026-10-08) --------------------------------------------------------


def _gated():
    from scripts import backup_planner

    return set(backup_planner.GATED_SLOTS)


def _is_gated_prompt(prefix):
    slots = {"0955ET": {"0935"}, "1050ET": {"1030"}, "1150ET": {"1130"}, "1250ET": {"1230"}, "1350-1450ET": {"1330", "1430"},
             "1545ET": {"1530"}}[prefix]
    return slots <= _gated()


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_cloud_planner_is_the_stamped_sandbox_file_not_a_vendored_script(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "backup_planner.pyz" in text
    assert "python3 backup_planner.py " not in text and "vendored single-file" not in text
    assert "--build-info" in text and "source_commit" in text  # the state record names the exact build that ran


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_task_does_no_sizing_of_its_own_and_says_so(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "NEVER compute ladder multipliers" in text
    assert "--deployed-last-7d-usd auto" in text or not _is_gated_prompt(prefix)  # the figure is derived, never summed by hand


@pytest.mark.parametrize("prefix", ["0955ET", "1150ET", "1350-1450ET", "1545ET"])
def test_a_gated_slot_dumps_raw_responses_and_lets_the_assembler_build_the_payload(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert _is_gated_prompt(prefix)
    assert "assemble-market-data" in text and "raw.json" in text and "market.json" in text
    assert "get_equity_historicals" in text and "5minute" in text and "get_equity_quotes" in text
    assert '{"historicals": [' in text and '"quotes": [' in text
    assert "--market-data market.json --deployed-last-7d-usd auto" in text
    assert "--quotes quotes.json" not in text and '{"quotes": [{"symbol"' not in text  # no hand-shaped quotes for a gated slot


def test_all_six_prompts_take_the_safety_gate_path():
    assert _gated() == {"0935", "1030", "1130", "1230", "1330", "1430", "1530"}
    assert all(_is_gated_prompt(e[0]) for e in EXPECTED)


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_every_prompt_uses_the_gated_commands_and_no_hand_shaped_quotes(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "--market-data market.json --deployed-last-7d-usd auto" in text and "assemble-market-data" in text
    assert "--quotes quotes.json" not in text and "signal gate is not wired" not in text


def test_a_slot_dropped_from_the_gated_set_falls_back_to_the_quotes_path_and_says_so(monkeypatch):
    """The fallback is a real state (a slot's gate disabled), not dead code: it reports shadow_only and places nothing."""
    from scripts import backup_planner

    monkeypatch.setattr(backup_planner, "GATED_SLOTS", {k: v for k, v in backup_planner.GATED_SLOTS.items() if k != "1230"})
    text = _by_prefix(gen.build_prompts(), "1250ET")
    assert "--quotes quotes.json" in text and "--market-data" not in text
    assert "signal gate is not wired for this slot" in text and "shadow_only" in text
    # review on #1664: a quotes-only plan has no Gate 5, so its prompt must never authorize placement
    assert "--local-status" not in text
    assert "--market-data market.json" in _by_prefix(gen.build_prompts(), "1150ET")  # the others are untouched


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_mac_path_runs_the_same_commands_from_the_checkout(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "python3 -m scripts.backup_planner plan --source mac_plan" in text
    if _is_gated_prompt(prefix):
        assert "python3 -m scripts.assemble_market_data" in text and "python3 -m scripts.backup_planner compute" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_how_the_planner_file_reaches_the_task_is_stated_as_a_setup_step_not_guessed(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "backup_planner.pyz is attached to this task's project files" in text
    assert "if the file is missing or the command fails, place NOTHING" in text


# ---------------------------------------------------------------------------
# Build stamp: STEP 0 must COMPARE the attached planner's build, not just record it.
# Written test-first (Copilot's high finding on #1624, thread PRRT_kwDOTH69qM6q8WGN). The expected value is
# derived from an independent real build of the zipapp, not from the generator's own output.
# ---------------------------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def built_stamp(tmp_path_factory):
    """The content_sha256 a real build of the current checkout reports in its own BUILD_INFO."""
    from scripts import build_backup_planner as bbp

    out = tmp_path_factory.mktemp("stamp") / "backup_planner.pyz"
    return bbp.build(out, source_commit="0123456789abcdef0123456789abcdef01234567")["content_sha256"]


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_every_prompt_embeds_the_expected_build_stamp(prompts, prefix, built_stamp):
    text = _by_prefix(prompts, prefix)
    assert text.count(built_stamp) == 1, "the expected content_sha256 appears once, in STEP 0"


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_step_b_compares_the_stamp_before_fetching_part_0(prompts, prefix, built_stamp):
    text = _by_prefix(prompts, prefix)
    step_b = text[text.index("STEP B"):text.index("PROCEDURE FOR TASK")]
    assert step_b.index("--build-info") < step_b.index(built_stamp) < step_b.index("--part 0")
    assert text.index("--part 0") < text.index("backup_planner.pyz universe"), "check first, then request part 0"
    assert "content_sha256" in step_b and "exactly" in step_b
    compare = step_b[step_b.index(built_stamp):step_b.index("--part 0")]
    assert "place NOTHING" in compare and "failed_unexpected_error" in compare and "notify" in compare


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_commit_is_recorded_but_never_compared(prompts, prefix):
    """A prompt cannot embed the hash of the commit that regenerates it (circular), so only the content hash gates."""
    text = _by_prefix(prompts, prefix)
    assert not re.search(r"\b[0-9a-f]{40}\b", text), "no commit hash is embedded"
    assert "source_commit" in text, "the state record still names the build's commit"


def test_a_changed_bundle_changes_every_prompt_so_check_reports_drift(monkeypatch):
    from scripts import build_backup_planner as bbp

    before = gen.build_prompts()
    monkeypatch.setattr(bbp, "content_sha256", lambda *a, **k: "f" * 64)
    after = gen.build_prompts()
    assert all(after[name] != before[name] and "f" * 64 in after[name] for name in before)


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "a" * 63])
def test_a_malformed_stamp_fails_closed_instead_of_rendering_an_unverifiable_prompt(monkeypatch, bad):
    from scripts import build_backup_planner as bbp

    monkeypatch.setattr(bbp, "content_sha256", lambda *a, **k: bad)
    with pytest.raises(ValueError):
        gen.build_prompts()


def test_the_generator_is_not_in_the_bundle_so_the_stamp_is_not_circular():
    from scripts import build_backup_planner as bbp

    shipped = set(bbp.collect_files(REPO))
    assert "scripts/generate_perplexity_backup_prompts.py" not in shipped
    assert not any(name.startswith("claude/") for name in shipped), "the prompts are not part of what they stamp"


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_finalize_is_given_the_clock_so_the_poll_2_kill_switch_recheck_has_a_window(prompts, prefix):
    """Copilot's second review on #1624: finalize recomputes the seven-day figure from poll 2, so it needs --now."""
    finalize = next(line for line in _by_prefix(prompts, prefix).splitlines() if "scripts.backup_planner finalize" in line)
    assert "--now <ET ISO timestamp>" in finalize


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_prompt_says_a_kill_switch_abort_means_place_nothing(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    step6 = text[text.index("STEP 6"):text.index("STEP 7")]
    assert "kill_switch_engaged_since_plan" in step6 and "place NOTHING" in step6


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_claim_is_unconfirmed_and_confirm_follows_the_first_accepted_order(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "records an unconfirmed claim" in text and "it is not a buy" in text
    assert "scripts.local_heartbeat confirm" in text
    assert "the claim expires and a later run may claim again" in text


@pytest.mark.parametrize("prefix", ["0955ET", "1050ET", "1150ET", "1250ET", "1350-1450ET", "1545ET"])
def test_the_task_also_fetches_the_previous_sessions_daily_bar_and_saves_it_under_daily(prompts, prefix):
    """Copilot's review of #1643: without the daily bar the assembled payload has no real prior low."""
    text = _by_prefix(prompts, prefix)
    step4 = text[text.index("STEP 4"):text.index("STEP 5")]
    assert "interval day" in step4 or "interval=day" in step4 or "daily bar" in step4
    assert '"daily": [' in step4


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_state_record_includes_planner_build(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    state = text[text.index("STEP 8 - STATE RECORD"):text.index("STEP 9 - NOTIFY")]
    assert "planner_build" in state



# -- 2026-10-13: a backup that cannot reach the Mac places its own computed plan -----------------------------------


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_a_missing_device_tool_counts_as_the_mac_not_found(prompts, prefix):
    """The 2026-10-12 15:45 run found no list_devices tool at all and failed closed on a case the prompt never named."""
    text = _by_prefix(prompts, prefix)
    step3 = text.split("STEP 3")[1].split("STEP 4")[0]
    assert "no device-listing tool" in step3
    assert "mac_status not_found" in step3.split("no device-listing tool")[1][:200]


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_mac_claim_and_confirm_apply_only_when_the_mac_is_online(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    step7 = text.split("STEP 7 - PLACE THE ORDERS")[1].split("STEP 8")[0]
    assert "only when mac_status is online" in step7
    assert "claim: skipped_mac_unreachable" in step7
    assert "scripts.local_heartbeat claim" in step7 and "scripts.local_heartbeat confirm" in step7
    # skipping the Mac lock is never skipping the broker evidence
    for kept in ("PER-ORDER REPULL", "strictly below the prior close"):
        assert kept in step7 or kept in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_finalize_is_told_the_local_status_the_step_3_routing_decided(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "--local-status <off|errored|unreachable>" in text
    assert "never pass --local-status when local_status is unknown, in_progress or succeeded" in text


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_prompt_no_longer_says_a_computed_plan_is_shadow_only_unconditionally(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    assert "SHADOW_ONLY" in text  # still the answer when the Mac may be acting
    assert "a cloud run is calculation-only" not in text.lower()
    assert "never places an order" not in text


# -- PR #1667 follow-up: the backups read the plan-first recommendation and recognise its orders -------------------


def _step(text, start, end):
    return text[text.index(start):text.index(end)]


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_claudes_plan_first_orders_are_recognised_by_order_id_not_by_a_runner_reference(prompts, prefix):
    """Claude's reader sends a UUID and the broker echoes its own, so a dca-runner- prefix can never name its orders."""
    text = _by_prefix(prompts, prefix)
    assert "It is expected executor traffic when its client order id starts with dca-runner-" not in text
    assert "client order id starting dca-runner-<slot>-" not in text  # step 7c's repull no longer leans on it
    step3 = _step(text, "STEP 3", "STEP 4")
    assert "python3 -m scripts.backup_planner executor-ids --trading-day <YYYY-MM-DD> --slot <slot>" in step3
    assert "python3 backup_planner.pyz classify" in step3 and "--executor-ids executor_ids.json" in step3
    assert "--published published.json" in step3
    assert "UNRECOGNIZED: place nothing, record stood_down_unrecognized_order, and notify" in step3  # still fail closed


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_published_set_is_read_untouched_from_the_configured_branch(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    branch = json.loads((CONFIG / "recommendations.json").read_text())["publish"]["github_branch"]["branch"]
    step2 = _step(text, "STEP 2", "STEP 3")
    assert f'branch "{branch}"' in step2 and gen.GITHUB_REPOSITORY in step2 and "recommendations.json" in step2
    assert "published.json" in step2 and "untouched" in step2
    assert "not an error" in step2  # an absent copy falls back; it does not fail the run


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_plan_first_set_comes_first_and_the_old_sources_remain_the_fallback(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    mac_rec = text.index("plan --source mac_rec")
    published = text.index("plan --source published_rec --published published.json")
    mac_plan = text.index("plan --source mac_plan")
    assert mac_rec < published < mac_plan < text.index("STEP 4")
    for value in ("mac_rec", "published_rec", "mac_plan", "mac_compute", "cloud_compute"):
        assert value in _step(text, "STEP 8 - STATE RECORD", "STEP 9 - NOTIFY"), value
    assert "NEVER compute ladder multipliers" in text  # reading a set never licenses sizing by hand


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_the_freshness_rule_is_the_readers_and_comes_from_config(prompts, prefix):
    minutes = json.loads((CONFIG / "recommendations.json").read_text())["max_age_minutes"]
    step3 = _step(_by_prefix(prompts, prefix), "STEP 3", "STEP 4")
    assert f"at most {minutes} minutes old" in step3
    assert "stamped at or before now on today's ET date" in step3


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_a_plan_first_plan_is_finalized_against_a_fresh_prior_close(prompts, prefix):
    step6 = _step(_by_prefix(prompts, prefix), "STEP 6", "STEP 7")
    assert "fresh_prior_closes.json" in step6 and "--fresh-prior-closes fresh_prior_closes.json" in step6
    assert "Claude or plan-first plan" in step6


def test_a_notion_page_is_named_only_once_configured_and_the_freshness_follows_config(tmp_path, monkeypatch):
    import shutil

    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG, config_dir)
    settings = json.loads((config_dir / "recommendations.json").read_text())
    assert not settings["publish"]["notion"]["page_id"]
    page_id = "0123456789abcdef0123456789abcdef"
    settings["publish"]["notion"] = {"enabled": True, "page_id": page_id}
    settings["max_age_minutes"] = 10
    (config_dir / "recommendations.json").write_text(json.dumps(settings))
    text = _by_prefix(gen.build_prompts(config_dir), "1150ET")
    assert f"Notion page {page_id}" in text and "at most 10 minutes old" in text
    assert "Notion page" not in _by_prefix(gen.build_prompts(), "1150ET")


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_poll_2_is_classified_before_finalize_review_on_1702(prompts, prefix):
    """An executor that started between the polls is visible only in poll 2, and step 7c's repull starts there."""
    step6 = _step(_by_prefix(prompts, prefix), "STEP 6", "STEP 7")
    classify = step6.index("CLASSIFY POLL 2")
    assert classify < step6.index("scripts.backup_planner finalize")
    assert "--orders-pages orders_pages_poll2.json" in step6 and "executor-ids" in step6
    assert "UNRECOGNIZED: place nothing, record stood_down_unrecognized_order" in step6[classify:]


@pytest.mark.parametrize("prefix", [e[0] for e in EXPECTED])
def test_step_7e_uses_the_native_placement_check_not_a_ref_id_echo_review_on_1702(prompts, prefix):
    text = _by_prefix(prompts, prefix)
    step7 = _step(text, "STEP 7 - PLACE THE ORDERS", "STEP 8")
    assert "and ref_id that match the request" not in step7
    assert "python3 backup_planner.pyz check-placement" in step7
    for arg in ("--submitted-after <submitted_after>", "--prior-orders-pages orders_pages_poll2.json",
                "--placed-responses", "--sent-ref-id <ref_id>"):
        assert arg in step7, arg
    assert "placement_<SYMBOL>.json" in text.split("STEP 0")[0]  # rule 2 deletes them with the order evidence
