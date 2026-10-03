"""Contract tests for ``agent-vault/``: the vendored loop-and-gate Foundation plus this repo's task overlays.

``agent-vault/`` is a self-contained working copy of slogsdon/loop-and-gate-foundation (MIT, pinned in
``agent-vault/UPSTREAM.md``) with three task skills layered on top. A session started *from inside that folder* gets a
SessionStart hook that injects working memory; a session started at the repo root gets nothing, on purpose, so ordinary
DCA coding sessions never pay for (or write into) the vault.

These tests pin the pieces that fail *silently* when they drift:

  * the hook resolving the repo's vault instead of a machine-local one (a wrong path injects nothing, no error);
  * Daily/Reflections filenames the hook's ``ls | sort | tail -1`` can order (a name like ``2026-Q4-capex.md`` sorts
    after every ``2026-10-03.md`` and would be loaded as "the latest note" forever);
  * the Notion write tools and the broker order tools being denied for vault sessions only (a repo-wide deny could
    break the live scheduled tasks that hand ``dca_notion_sync`` payloads to a Notion connector);
  * ``.gitignore`` not swallowing ``agent-vault/.claude/`` (the repo ignores ``.claude/`` at any depth, so the hook
    config and skills would simply never be committed);
  * the vendored core scripts staying byte-identical to the pinned upstream commit.

Standard library only, like the rest of the core suite. Nothing here touches the network or the broker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = REPO_ROOT / "agent-vault"
VAULT = VAULT_ROOT / "vault"
SCRIPTS = VAULT_ROOT / "scripts"
SKILLS = VAULT_ROOT / "skills"

# `ls | sort | tail -1` in session-start-hook.sh only orders correctly when every note is a bare ISO date.
DATE_NOTE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

# Kept from upstream. add-kits is deliberately absent: it installs sibling kits, and the dependency policy says no new
# dependency without a written justification (see test_add_kits_is_not_vendored). setup is absent too: it records a
# machine-wide vault path that outranks the tracked vault (see test_setup_is_not_vendored).
FOUNDATION_SKILLS = (
    "session-start",
    "capture",
    "reflect",
    "verify",
    "improve",
    "inbox-triage",
    "loop",
    "profile-interview",
)

# overlay skill -> (Knowledge note it must read, markers its text must carry)
OVERLAY_SKILLS = {
    "execute-dca": ("all-stock-dca-rules.md", ("run_dca.py --demo", "dry-run")),
    "analyze-capex": ("capex-monitoring-kpis.md", ("UNTESTED", "UNVERIFIED")),
    "backtest-dltp": ("dl-trading-backtests.md", ("UNTESTED", "UNVERIFIED", "seed")),
}

# Upstream's improve skill caps every skill at ~80 lines; an overlay over the cap cannot be edited by /improve cleanly.
SKILL_LINE_CAP = 80

# Core Foundation logic: pinned by sha256 in UPSTREAM.md so a silent edit (or a careless /improve) is a red test.
PINNED_SCRIPTS = ("vault-path.sh", "session-start-hook.sh", "inject-operating-rules.sh")

NOTION_WRITE_TOOLS = (
    "notion-create-pages",
    "notion-update-page",
    "notion-create-database",
    "notion-create-comment",
    "notion-duplicate-page",
    "notion-move-pages",
    "notion-update-data-source",
    "notion-create-view",
    "notion-update-view",
    "notion-create-folder",
    "notion-update-folder",
    "notion-create-file-upload",
    "notion-create-attachment",
    "notion-upload-skill",
    "notion-convert-page-to-skill",
    "notion-spawn-session",
    "notion-send-message-to-session",
    "notion-stop-session",
)

# A vault session analysing filings needs read access only; the library's rule is that nothing outside the injected,
# gated live path ever submits an order.
BROKER_ORDER_TOOLS = (
    "place_equity_order",
    "place_option_order",
    "place_crypto_order",
    "cancel_equity_order",
    "cancel_option_order",
    "cancel_crypto_order",
    "cancel_option_exercise",
    "exercise_option",
)

# Thresholds taken from the original brief rather than from this repo's own safeguards: they must stay flagged until
# someone confirms them (the first /improve target is Knowledge/threshold-review.md).
# capex_delta_flag_pct left this list when the 2026-10-03 baseline run verified it, live_slippage_vs_model_bps
# when it was retired (the DLTP audit found frictionless returns, so there is no model slippage to compare against),
# backtest_sharpe_min when the seeded 5-fold walk-forward run verified it (mean Sharpe 3.56), and
# capex_research_addition_usd_m when the two-tier scale rule was settled ($2,000M hyperscalers / $250M suppliers).
UNVERIFIED_KEYS: tuple[str, ...] = ()

# Any claim of the form "the task runs like this" belongs in a runbook a person can follow without this chat.
RUNBOOKS = {
    "all-stock-dca.md": ("run_dca.py --demo", "chore: memory update"),
    "capex-monitoring.md": ("UNVERIFIED",),
    "dl-trading-backtest.md": ("seed",),
    "notion-export.md": ("manual",),
    "validation.md": ("git log --oneline",),
}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def _front_matter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "no YAML front matter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _flat_yaml(text: str) -> dict[str, str]:
    """First ``key: value`` per key, comments included in the value.

    No YAML library on purpose (stdlib-only core, and upstream's own resolver reads config.yaml with grep).
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$", line)
        if match:
            values.setdefault(match.group(1), match.group(2))
    return values


def _clean_env(home: Path) -> dict[str, str]:
    # HOME is the only machine-local input the vault resolver reads; pointing it at an empty dir is what makes the
    # test hermetic on a machine that has run /setup before.
    return {"HOME": str(home), "PATH": os.environ["PATH"]}


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="hook scripts are bash")


# --- layout -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "CLAUDE.md",
        "config.yaml",
        "LICENSE",
        "UPSTREAM.md",
        "README.md",
        ".claude/settings.json",
        "scripts/vault-path.sh",
        "scripts/session-start-hook.sh",
        "scripts/inject-operating-rules.sh",
    ],
)
def test_foundation_files_are_vendored(rel):
    assert (VAULT_ROOT / rel).is_file(), f"missing agent-vault/{rel}"


def test_foundation_scripts_are_executable():
    # settings.json invokes them directly; a lost exec bit means the hook silently never runs.
    for name in PINNED_SCRIPTS:
        assert os.access(SCRIPTS / name, os.X_OK), f"agent-vault/scripts/{name} is not executable"


@pytest.mark.parametrize(
    "rel",
    [
        "MEMORY.md",
        "Daily/.gitkeep",
        "Inbox/.gitkeep",
        "Reflections/.gitkeep",
        "Profiles/voice.md",
        "Profiles/taste.md",
        "Knowledge/all-stock-dca-rules.md",
        "Knowledge/capex-monitoring-kpis.md",
        "Knowledge/dl-trading-backtests.md",
        "Knowledge/threshold-review.md",
    ],
)
def test_vault_scaffold_exists(rel):
    assert (VAULT / rel).is_file(), f"missing agent-vault/vault/{rel}"


@pytest.mark.parametrize("folder", ["Daily", "Reflections"])
def test_daily_and_reflection_notes_are_bare_iso_dates(folder):
    directory = VAULT / folder
    assert directory.is_dir(), f"missing agent-vault/vault/{folder}"
    bad = [p.name for p in directory.iterdir() if p.name != ".gitkeep" and not DATE_NOTE.match(p.name)]
    assert not bad, f"{folder}/ names must be YYYY-MM-DD.md so the SessionStart hook can order them: {bad}"


def test_memory_index_stays_under_the_foundation_line_cap():
    # Upstream's rule: MEMORY.md over ~40 lines means consolidate. It is also the prompt-cache prefix.
    assert len(_read(VAULT / "MEMORY.md").splitlines()) <= 40


# --- skills -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FOUNDATION_SKILLS)
def test_foundation_skills_are_vendored(name):
    assert _front_matter(_read(SKILLS / name / "SKILL.md"))["name"] == name


def test_add_kits_is_not_vendored():
    assert not (SKILLS / "add-kits").exists(), "add-kits installs sibling kits: a new dependency needing sign-off"


def test_claude_skills_symlink_points_at_the_editable_skills_folder():
    # Same layout as upstream: skills/ is the real working copy that /improve edits, .claude/skills makes it load.
    link = VAULT_ROOT / ".claude" / "skills"
    assert link.is_symlink() and link.resolve() == SKILLS.resolve()
    for name in OVERLAY_SKILLS:
        assert (link / name / "SKILL.md").is_file(), f"{name} not visible through .claude/skills"


@pytest.mark.parametrize("name", sorted(OVERLAY_SKILLS))
def test_overlay_skill_contract(name):
    note, markers = OVERLAY_SKILLS[name]
    text = _read(SKILLS / name / "SKILL.md")
    fields = _front_matter(text)
    assert fields["name"] == name
    assert fields.get("description"), "a skill with no description is never selected"
    assert len(text.splitlines()) <= SKILL_LINE_CAP, "over the cap /improve enforces on every skill"
    # Reads its Knowledge note, logs to Daily/, closes through the Foundation's reflect skill.
    assert note in text and (VAULT / "Knowledge" / note).is_file()
    assert "Daily/" in text
    assert "reflect" in text
    for marker in markers:
        assert marker in text, f"{name} must carry {marker!r}"


# --- provenance and tamper detection ----------------------------------------------------------------------------


def test_upstream_provenance_is_recorded():
    text = _read(VAULT_ROOT / "UPSTREAM.md")
    for fact in ("slogsdon/loop-and-gate-foundation", "0.5.0", "2f0fe52", "MIT"):
        assert fact in text, f"UPSTREAM.md must record {fact}"
    assert "Shane Logsdon" in _read(VAULT_ROOT / "LICENSE")


def test_vendored_core_scripts_match_their_pinned_hashes():
    pins = dict(
        (path, digest)
        for digest, path in re.findall(r"^sha256\s+([0-9a-f]{64})\s+(\S+)$", _read(VAULT_ROOT / "UPSTREAM.md"), re.M)
    )
    for name in PINNED_SCRIPTS:
        rel = f"scripts/{name}"
        assert rel in pins, f"UPSTREAM.md has no sha256 pin for {rel}"
        assert hashlib.sha256((VAULT_ROOT / rel).read_bytes()).hexdigest() == pins[rel], f"{rel} drifted from upstream"


# --- hook wiring, permissions, git ------------------------------------------------------------------------------


def _settings() -> dict:
    return json.loads(_read(VAULT_ROOT / ".claude" / "settings.json"))


def test_settings_register_both_session_start_hooks():
    commands = [
        hook["command"]
        for group in _settings()["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    for script in ("inject-operating-rules.sh", "session-start-hook.sh"):
        assert any(c.endswith(f"/scripts/{script}") and "$CLAUDE_PROJECT_DIR" in c for c in commands), script


def test_settings_deny_notion_writes_and_broker_orders():
    deny = set(_settings().get("permissions", {}).get("deny", []))
    wanted = {f"mcp__Notion__{t}" for t in NOTION_WRITE_TOOLS} | {f"mcp__Robinhood__{t}" for t in BROKER_ORDER_TOOLS}
    assert wanted <= deny, f"not denied for vault sessions: {sorted(wanted - deny)}"


def test_deny_rules_are_scoped_to_the_vault_not_the_repo():
    # A repo-wide deny could stop the live scheduled tasks that write Notion through their own connector access.
    root_settings = REPO_ROOT / ".claude" / "settings.json"
    if root_settings.is_file():
        assert "mcp__Notion__" not in root_settings.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel",
    [
        "agent-vault/.claude/settings.json",
        "agent-vault/skills/execute-dca/SKILL.md",
        "agent-vault/vault/Daily/.gitkeep",
        "agent-vault/.claude/skills",
        "agent-vault/vault/Daily/2026-10-03.md",
        "agent-vault/vault/Reflections/2026-10-03.md",
    ],
)
def test_vault_paths_are_not_gitignored(rel):
    # check-ignore exits 0 when ignored, 1 when not, 128 on error. The repo-level `.claude/` rule needs a negation.
    # --no-index: these paths are tracked, and check-ignore skips tracked files unless told to read the rules alone.
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO_ROOT, check=False)
    assert result.returncode == 1, f"{rel} is gitignored (exit {result.returncode})"


# --- operating rules, config, runbooks ---------------------------------------------------------------------------


def test_agent_vault_claude_md_contract():
    text = _read(VAULT_ROOT / "CLAUDE.md")
    assert len(text.encode("utf-8")) <= 9000, "upstream's injection guard: keep CLAUDE.md well under the 10k hook cap"
    for needle in ("## Hard rules", "../CLAUDE.md", "Notion", "dry-run"):
        assert needle in text, f"agent-vault/CLAUDE.md must mention {needle!r}"


def test_config_points_dca_thresholds_at_the_repos_own_safeguards():
    values = _flat_yaml(_read(VAULT_ROOT / "config.yaml"))
    assert values["vault"], "upstream's resolver greps for a `vault:` line"
    for key in ("safeguards_doc", "budgets_config"):
        target = REPO_ROOT / values[key].split("#")[0].strip()
        assert target.is_file(), f"config.yaml {key} -> {target.relative_to(REPO_ROOT)} does not exist"


@pytest.mark.parametrize("key", UNVERIFIED_KEYS)
def test_brief_derived_thresholds_stay_flagged_unverified(key):
    assert "UNVERIFIED" in _flat_yaml(_read(VAULT_ROOT / "config.yaml"))[key]


@pytest.mark.parametrize("name", sorted(RUNBOOKS))
def test_runbook_exists_and_carries_its_key_commands(name):
    text = _read(VAULT_ROOT / "runbooks" / name)
    for needle in RUNBOOKS[name]:
        assert needle in text, f"runbooks/{name} must mention {needle!r}"


# --- behaviour of the vendored hook scripts ---------------------------------------------------------------------


@needs_bash
def test_vault_path_prefers_the_repo_vault_without_any_home_state(tmp_path):
    # The whole reason the vault is vendored with a vault/ subfolder: cloud and CI sessions have an empty HOME.
    out = subprocess.run(
        [str(SCRIPTS / "vault-path.sh")], env=_clean_env(tmp_path), capture_output=True, text=True, check=True
    )
    assert Path(out.stdout.strip()).resolve() == VAULT.resolve()


@needs_bash
def test_a_recorded_vault_path_still_outranks_the_repo_vault(tmp_path):
    # Pins upstream's precedence so a changelog review notices if it ever flips: this is what a prior /setup on a
    # developer machine would do to us, and why docs/ tell people not to run /setup here.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    recorded = tmp_path / ".config" / "loop-and-gate" / "vault"
    recorded.parent.mkdir(parents=True)
    recorded.write_text(f"{elsewhere}\n", encoding="utf-8")
    out = subprocess.run(
        [str(SCRIPTS / "vault-path.sh")], env=_clean_env(tmp_path), capture_output=True, text=True, check=True
    )
    assert Path(out.stdout.strip()).resolve() == elsewhere.resolve()


def _vault_copy(tmp_path: Path) -> Path:
    # Run against a copy: the hook writes today's Daily note, and the suite must never dirty the checkout.
    # Resolved up front so the path the hook prints (it derives it from $0) compares byte-for-byte on macOS too.
    copy = tmp_path.resolve() / "agent-vault"
    shutil.copytree(VAULT_ROOT, copy, symlinks=True)
    return copy


def _run_session_start(copy: Path, tmp_path: Path) -> str:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    out = subprocess.run(
        [str(copy / "scripts" / "session-start-hook.sh")],
        env=_clean_env(home),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


# Far in the past so these notes always sort before whatever "today" is when the suite runs.
PRIOR_NOTE = "# 2020-01-01\n\n## Sessions\n### 09:00 - prior\n- Open: {marker}\n"


@needs_bash
def test_session_start_hook_injects_memory_and_creates_todays_note(tmp_path):
    copy = _vault_copy(tmp_path)
    stdout = _run_session_start(copy, tmp_path)
    assert "=== WORKING MEMORY" in stdout
    assert f"--- {copy}/vault/MEMORY.md ---" in stdout, "hook must resolve the vault shipped beside it"
    assert len(stdout) < 10_000, "over the SessionStart cap Claude Code offloads the block and truncates MEMORY.md"
    created = [p for p in (copy / "vault" / "Daily").iterdir() if DATE_NOTE.match(p.name)]
    assert created, "hook should create today's Daily note"
    assert "## Sessions" in created[0].read_text(encoding="utf-8")


@needs_bash
def test_first_session_of_a_day_still_sees_the_previous_days_open_items(tmp_path):
    # The hook creates today's empty scaffold, which sorts after yesterday's note. Picking "latest" by name alone
    # would inject the empty scaffold and drop yesterday's Open items, and the session-start skill then skips its
    # fallback reads because the memory block is present.
    copy = _vault_copy(tmp_path)
    (copy / "vault" / "Daily" / "2020-01-01.md").write_text(PRIOR_NOTE.format(marker="FINISH-THE-LEDGER"))
    assert "FINISH-THE-LEDGER" in _run_session_start(copy, tmp_path)


@needs_bash
def test_the_newest_populated_note_wins_over_older_ones(tmp_path):
    copy = _vault_copy(tmp_path)
    daily = copy / "vault" / "Daily"
    (daily / "2020-01-01.md").write_text(PRIOR_NOTE.format(marker="OLDER-MARKER"))
    (daily / "2020-01-02.md").write_text(PRIOR_NOTE.format(marker="NEWER-MARKER"))
    stdout = _run_session_start(copy, tmp_path)
    assert "NEWER-MARKER" in stdout and "OLDER-MARKER" not in stdout


@needs_bash
def test_an_oversized_reflection_is_bounded_and_points_at_the_full_file(tmp_path):
    # reflect appends every session of a day to one file, so it grows without bound; an unbounded cat pushes the whole
    # block past the 10K cap, and Claude Code then keeps only a preview (truncating MEMORY.md, which is printed first).
    copy = _vault_copy(tmp_path)
    reflection = copy / "vault" / "Reflections" / "2020-01-01.md"
    reflection.write_text("".join(f"## Session {n}\nSENTINEL-{n}-END " + "x" * 900 + "\n" for n in range(40)))
    stdout = _run_session_start(copy, tmp_path)
    assert len(stdout) < 10_000
    assert "# Agent Working Memory" in stdout, "MEMORY.md must survive whole"
    assert "SENTINEL-39-END" in stdout and "SENTINEL-0-END" not in stdout, "the most recent sessions are the ones kept"
    assert "truncated" in stdout and str(reflection) in stdout, "tell the agent where the full audit file is"


@needs_bash
def test_the_block_stays_under_the_cap_when_both_notes_are_oversized(tmp_path):
    # Worst case: three headers and two truncation markers each carry the absolute vault path.
    copy = _vault_copy(tmp_path)
    (copy / "vault" / "Daily" / "2020-01-01.md").write_text(PRIOR_NOTE.format(marker="y" * 30_000))
    (copy / "vault" / "Reflections" / "2020-01-01.md").write_text("z" * 30_000)
    assert len(_run_session_start(copy, tmp_path)) < 10_000


# --- review follow-ups: runbooks, loop skill, patch provenance --------------------------------------------------

DRY_RUN_DOCS = ("skills/execute-dca/SKILL.md", "runbooks/all-stock-dca.md", "runbooks/validation.md")


@pytest.mark.parametrize("rel", DRY_RUN_DOCS)
def test_dry_run_artifact_dirs_fall_back_when_tmpdir_is_unset(rel):
    # Unset TMPDIR (ordinary on Linux and in cloud sessions) would expand "$TMPDIR/dca-demo" to "/dca-demo".
    text = _read(VAULT_ROOT / rel)
    assert "${TMPDIR:-/tmp}" in text
    assert '"$TMPDIR/' not in text


@pytest.mark.parametrize("rel", ("skills/execute-dca/SKILL.md", "runbooks/validation.md"))
def test_the_determinism_check_compares_runs_made_at_one_output_path(rel):
    # The run records its own output directory, so two different directory names differ by construction. The first
    # result is moved aside, the second run reuses the path, and only then are the trees compared.
    text = _read(VAULT_ROOT / rel)
    assert "mv " in text and "diff -r" in text


def test_loop_skill_restores_from_a_start_snapshot_not_the_index():
    # `git checkout -- paths` restores from the index: it erases earlier uncommitted memory and leaves any new
    # (untracked) file the failed worker wrote. Daily and Reflections notes are the audit trail and are never deleted.
    text = _read(SKILLS / "loop" / "SKILL.md")
    assert "stash create" in text
    assert "checkout -- <those same paths>" not in text
    assert "FAILED ITERATION" in text


def test_the_patched_hook_records_the_upstream_hash_it_diverged_from():
    text = _read(VAULT_ROOT / "UPSTREAM.md")
    assert re.search(r"^upstream-sha256\s+2ce182560bc5e3eef612c12928784f3ec3ebb77acfbf1b74441995a381bb019b\s+"
                     r"scripts/session-start-hook\.sh$", text, re.M)
    for patched in ("scripts/session-start-hook.sh", "skills/loop/SKILL.md"):
        assert patched in text.split("## Changes from upstream", 1)[1], f"{patched} patch not documented"


# --- second review pass (/code-review) ---------------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("missing", ["Daily", "Reflections"])
def test_the_hook_still_injects_memory_when_a_note_folder_is_missing(tmp_path, missing):
    # Under `set -euo pipefail` an `ls` of a missing folder aborted the hook (exit 2) before it printed anything, so
    # the session silently started without memory. A worktree where a .gitkeep was removed is enough to trigger it.
    copy = _vault_copy(tmp_path)
    shutil.rmtree(copy / "vault" / missing)
    stdout = _run_session_start(copy, tmp_path)
    assert "=== WORKING MEMORY" in stdout and "# Agent Working Memory" in stdout


def test_loop_skill_also_protects_files_that_were_already_untracked():
    # `git stash create` only holds tracked files, and today's Daily note is created untracked by the hook. A worker
    # appending to it would otherwise leave an unmarked, unverified log in the audit trail.
    text = _read(SKILLS / "loop" / "SKILL.md")
    assert "already untracked" in text and "copy aside" in text


def test_execute_dca_logs_live_run_aggregates_only():
    # Daily notes are committed; live run output stays in the gitignored cron tree (root CLAUDE.md).
    text = _read(SKILLS / "execute-dca" / "SKILL.md")
    assert "aggregates only" in text and "order IDs" in text


def test_the_operating_rules_say_improve_release_step_does_not_apply():
    # improve step 6 tells the agent to bump plugin.json and run `claude plugin update`; this copy is not a plugin, and
    # installing the upstream plugin would let a machine-local vault outrank this one.
    text = _read(VAULT_ROOT / "CLAUDE.md")
    assert "step 6" in text and "claude plugin" in text
    assert len(text.encode("utf-8")) <= 9000


def test_readme_explains_the_untracked_daily_scaffold():
    text = _read(VAULT_ROOT / "README.md")
    assert "untracked" in text and "git add -A" in text


def test_the_hook_patch_row_mentions_the_missing_folder_fix():
    text = _read(VAULT_ROOT / "UPSTREAM.md").split("## Changes from upstream", 1)[1]
    assert "mkdir" in text


# --- setup removed (second review pass, finding 1) ---------------------------------------------------------------


def test_setup_is_not_vendored():
    # setup.sh records ~/.config/loop-and-gate/vault, which vault-path.sh ranks above the tracked vault: one stray
    # "set up my vault" would silently move every later session's memory out of the repo. The vault is already
    # scaffolded, so nothing needs the skill.
    assert not (SKILLS / "setup").exists()
    assert not (SCRIPTS / "setup.sh").exists()


@pytest.mark.parametrize("rel", ("CLAUDE.md", "README.md", "scripts/session-start-hook.sh"))
def test_nothing_points_at_the_removed_setup_skill(rel):
    text = _read(VAULT_ROOT / rel)
    # Backticks included: upstream writes "the `setup` skill", which a plain substring check would miss.
    assert not re.search(r"setup`?\s+skill|/setup|setup\.sh", text), f"{rel} still points at the removed setup skill"
