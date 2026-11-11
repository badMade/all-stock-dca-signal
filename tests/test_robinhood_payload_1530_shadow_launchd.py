"""The shadow launchd agent that builds the 3:30 PM Robinhood payload for measurement only (2026-10-22).

Pins it next to ``com.all-stock-dca.robinhood-payload.plist.template`` (the six-slot job): same placeholder
convention and venv-Python launch, but a separate Label and log so loading it can never touch the live job, an
explicit ``1530`` slot (``auto`` never selects it), and a build deadline.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

from scripts import robinhood_payload as rp

DIR = Path(__file__).resolve().parent.parent / "deploy/launchd"
SHADOW = DIR / "com.all-stock-dca.robinhood-payload-1530-shadow.plist.template"
LIVE = DIR / "com.all-stock-dca.robinhood-payload.plist.template"


def _load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    for placeholder in ("REPLACE_WITH_REPO_PATH", "REPLACE_WITH_CRON_TRACKING_ROOT", "REPLACE_WITH_CODEX_PATH"):
        text = text.replace(placeholder, "/x")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_shadow_agent_cannot_collide_with_the_live_six_slot_agent():
    shadow, live = _load(SHADOW), _load(LIVE)
    assert shadow["Label"] == "com.all-stock-dca.robinhood-payload-1530-shadow" != live["Label"]
    assert shadow["StandardOutPath"] != live["StandardOutPath"] and shadow["StandardOutPath"].startswith("/tmp/")
    assert shadow["StandardErrorPath"] == shadow["StandardOutPath"]


def test_shadow_agent_names_the_1530_slot_explicitly_never_auto():
    args = _load(SHADOW)["ProgramArguments"]
    assert args[0] == "/x/.venv/bin/python" and args[3] == "/x/deploy/launchd/robinhood_payload.sh"
    assert '"1530"' in args[2] and "auto" not in args[2]
    assert "1530" not in rp.SLOTS  # `auto` would never pick it, which is why the slot is named here
    assert '"auto"' in _load(LIVE)["ProgramArguments"][2]  # the live job is untouched


def test_shadow_agent_fires_one_minute_after_the_decision_time_on_weekdays_only():
    entries = _load(SHADOW)["StartCalendarInterval"]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert {(e["Hour"], e["Minute"]) for e in entries} == {(15, 31)}
    live = _load(LIVE)["StartCalendarInterval"]
    assert all((e["Hour"], e["Minute"]) != (15, 31) for e in live)  # the live job still never fires at 3:31


def test_shadow_agent_sets_a_build_deadline_that_fits_before_the_slot_window_closes():
    env = _load(SHADOW)["EnvironmentVariables"]
    deadline = int(env["DCA_BUILD_DEADLINE_SECONDS"])
    assert 240 <= deadline <= 600  # above a real 2-4 minute build, well inside the 15:25-15:55 window
    # The live job serves six slots, so it carries a relative deadline only (2026-11-02), never the 15:40 ET cutoff below.
    assert "DCA_BUILD_DEADLINE_ET" not in _load(LIVE)["EnvironmentVariables"]
    assert env["DCA_CRON_TRACKING_ROOT"] == "/x"  # must match the live sessions


def test_shadow_template_holds_no_secret_or_account_shaped_literal():
    text = SHADOW.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text) and "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text


def test_1530_agent_publishes_or_refuses_before_the_live_prompt_stops_re_checking():
    # 2026-10-30: the first live 3:30 build published at 15:34:48, and the shadow builds of 10-23 to 10-30 at 15:34:45 to
    # 15:35:26 (220-264 s). The run-classification window's 15:35 end is a reporting bound on order attribution, not a
    # gate on the payload: the session waits on `check`, re-checking for 12 minutes, and its session fires at 15:30, so
    # 15:42 is the earliest it can give up. Both deadlines must end the fetch before then whatever time launchd starts it.
    import datetime as dt

    prompt = (DIR.parent.parent / "claude/dca-executor-prompts/live-mirror/1530ET-330-final-buying-run.txt").read_text(
        encoding="utf-8"
    )
    wait = re.search(r"re-run check every 30 seconds for up to (\d+) minutes", prompt)
    assert wait, "the 1530ET prompt's payload wait changed; re-derive the build cutoff"
    gives_up = dt.datetime(2026, 10, 30, 15, 30) + dt.timedelta(minutes=int(wait.group(1)))

    env = _load(SHADOW)["EnvironmentVariables"]
    cutoff = dt.datetime.combine(dt.date(2026, 10, 30), dt.time.fromisoformat(env["DCA_BUILD_DEADLINE_ET"]))
    relative_end = dt.datetime(2026, 10, 30, 15, 31) + dt.timedelta(seconds=int(env["DCA_BUILD_DEADLINE_SECONDS"]))
    # At least one 30-second re-check, plus the merge, after the fetch stops.
    assert cutoff <= gives_up - dt.timedelta(minutes=2), (cutoff, gives_up)
    assert relative_end <= cutoff  # an on-time start is bounded by the relative deadline alone
    # And neither may cut a normal build short: the slowest measured build (264 s from 15:31) with room to spare.
    assert cutoff >= dt.datetime(2026, 10, 30, 15, 31) + dt.timedelta(seconds=2 * 264)
    assert "DCA_BUILD_DEADLINE_ET" not in _load(LIVE)["EnvironmentVariables"]


def test_shadow_agent_runs_enough_workers_to_publish_by_about_15_35():
    # 2026-11-07: the expanded 121-ticker plan is 31 chunks of about 60 s each, all clean on the first attempt.
    # At 8 workers it fits in 4 waves (about 4.5 minutes) before the 3:30 session's placement window.
    env = _load(SHADOW)["EnvironmentVariables"]
    parallel = int(env["DCA_CODEX_PARALLEL"])
    import dca_config

    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    universe = [t for t in config.tickers if t not in config.excluded_tickers]  # what build_plan("1530", ...) plans
    chunks = -(-len(universe) // rp.DEFAULT_CHUNK_SIZE)
    waves = -(-chunks // parallel)
    assert waves * 65 <= 400, (chunks, parallel, waves)  # ~65 s per wave observed; 6 workers = 6 waves, ready by about 15:38 (2026-11-10: 8 -> 6)


def test_live_agent_runs_enough_workers_to_finish_in_about_four_and_a_half_minutes():
    # 2026-11-07: the six live builds need the same 8 workers as the shadow build because both now plan 31 chunks.
    # At about 65 s per wave, 4 waves fit within the 4.5-minute budget and leave room for a retry under the 600 s deadline.
    env = _load(LIVE)["EnvironmentVariables"]
    parallel = int(env["DCA_CODEX_PARALLEL"])
    import dca_config

    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    universe = [t for t in config.tickers if t not in config.excluded_tickers]
    chunks = -(-len(universe) // rp.DEFAULT_CHUNK_SIZE)
    waves = -(-chunks // parallel)
    assert waves * 65 <= 400, (chunks, parallel, waves)  # 2026-11-10: 8 -> 6 workers, 6 waves
    # And the 600 s build deadline still leaves room for one full retry wave on top of a normal build.
    assert waves * 65 + 65 <= int(env["DCA_BUILD_DEADLINE_SECONDS"]), (waves, env["DCA_BUILD_DEADLINE_SECONDS"])


def test_the_worker_count_is_no_more_than_the_four_wave_budget_needs():
    # 2026-11-06: the universe grew from 91 to 122 symbols (#2175/#2177), 31 four-ticker chunks: 6 workers took 6
    # waves (~390 s), so both jobs move to the count that fits 4 waves. Never more than that: every extra worker is
    # another concurrent Codex/Robinhood call (rate-limit risk) for no speed gain inside the same wave count. When the
    # universe changes, this and the two budget tests above say which way to move the number.
    import dca_config

    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    universe = [t for t in config.tickers if t not in config.excluded_tickers]
    chunks = -(-len(universe) // rp.DEFAULT_CHUNK_SIZE)
    needed = -(-chunks // 4)  # the fewest workers that fit the plan in 4 waves
    for template in (LIVE, SHADOW):
        assert int(_load(template)["EnvironmentVariables"]["DCA_CODEX_PARALLEL"]) <= needed, (template.name, needed)


def test_live_and_shadow_agents_use_the_same_worker_count():
    # One number to tune: a later change to one job must not silently leave the other behind.
    assert _load(LIVE)["EnvironmentVariables"]["DCA_CODEX_PARALLEL"] == _load(SHADOW)["EnvironmentVariables"]["DCA_CODEX_PARALLEL"]


def test_no_payload_agent_sets_an_environment_variable_twice():
    # 2026-11-07: the shadow template set DCA_CODEX_PARALLEL twice. plistlib (and launchd) keep the LAST value, so a
    # later edit to the first entry alone would change nothing while reading as if it had; one key, one place.
    for path in (SHADOW, LIVE):
        text = path.read_text(encoding="utf-8")
        env_block = text[text.index("<key>EnvironmentVariables</key>"):]
        env_block = env_block[: env_block.index("</dict>")]
        keys = re.findall(r"<key>([^<]+)</key>", env_block)[1:]  # drop the EnvironmentVariables key itself
        assert len(keys) == len(set(keys)), (path.name, sorted(k for k in set(keys) if keys.count(k) > 1))


def test_shadow_comments_no_longer_say_the_live_slots_keep_the_default_workers():
    # Since 2026-11-04 the live agent sets DCA_CODEX_PARALLEL too, and a test keeps the two equal (now 6).
    assert "six live slots keep the default" not in SHADOW.read_text(encoding="utf-8")
