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
    assert expected_hash in text and f"({len(config.tickers)} symbols)" in text
    versions = [json.loads((CONFIG / n).read_text())["version"] for n in ("sizing_ladder.json", "per_ticker_sizing.json")]
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
    assert "never places an order" in text  # cloud is calculation-only
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
def test_step_0_compares_the_stamp_before_any_planner_command_runs(prompts, prefix, built_stamp):
    text = _by_prefix(prompts, prefix)
    step0 = text[text.index("STEP 0"):text.index("STEP 1")]
    assert step0.index(built_stamp) < step0.index("backup_planner.pyz universe"), "compare first, then run the planner"
    assert "content_sha256" in step0 and "exactly" in step0
    # A mismatch is a hard stop with the same fail-closed vocabulary the other STEP 0 refusals use.
    compare = step0[step0.index(built_stamp):step0.index("backup_planner.pyz universe")]
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
