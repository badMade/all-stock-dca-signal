# Scheduled-session repo access — probe log

Tracks whether a Claude Code Remote session that fires from a Routine
(cron trigger) actually has working repo access — clone, fetch, and push —
the same way an interactive session does. Append one dated entry per probe;
don't rewrite prior entries.

## 2026-07-29 probe (branch `claude/repo-access-scheduled-sessions-c0ha47`)

**Git state on entry:** `git status` reported a clean working tree — no
`.git/index.lock`, and `docs/live_wiring.md` had no uncommitted diff (it was
already part of `HEAD`, commit `fb215a7`). The task prompt that kicked off
this session described a lock file and an uncommitted `docs/live_wiring.md`
that did not exist in this checkout. Read together with the container
lifecycle (fresh clone on container start, reclaimed after inactivity), the
likely explanation is that neither survives between sessions: a fresh
container reclones at `origin`'s current `HEAD` and drops any local,
uncommitted edits from whatever session produced that instruction. The
confirmed boundary is narrower than "nothing survives," though: an
*unpushed* local edit — committed or not — is unavailable after a fresh
clone, but a commit that was actually pushed to the ref the next session
checks out would survive. Anything a scheduled task needs across firings
has to be either pushed to the repo or persisted outside it; per
`docs/live_wiring.md`, `StrategyState` is the outside-the-repo case, and
today it is a **caller responsibility that still needs an implementation**
— `run_pipeline` holds it in memory only and does not load or save it, so
whatever wires the live pipeline must persist and reload it itself
(e.g. to a JSON file next to the daily artifacts) or the daily/weekly/
reserve budget caps silently reset on every invocation.

**Read/fetch access:** confirmed. `git fetch origin main` and
`git ls-remote origin` both succeeded through the session's local git proxy
(`http://local_proxy@127.0.0.1:<port>/git/badMade/all-stock-dca`).

**Write/push access:** this branch (`claude/repo-access-scheduled-sessions-c0ha47`)
did not exist on `origin` before this session pushed it — confirmed via
`git ls-remote origin claude/repo-access-scheduled-sessions-c0ha47` returning
empty prior to the push in this session. This commit is the first thing
pushed to it, from this session, which is itself the intended probe of
push access from a scheduled/remote-triggered context.

**`ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`:** not set in this session's
environment (`env | grep ROBINHOOD` — no output). `near_close_dca.py` reads
it once at import time (`near_close_dca.py:146`) and falls back to the
sentinel `UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` when absent
(`near_close_dca.py:135,147`), which then fails the `target_account_number`
invariant check rather than silently trading on the wrong account.
`conftest.py` sets a synthetic, obviously-fake `FIXTURE_ACCOUNT_NUMBER` for
the pytest process only (`conftest.py:30-34`) — not the real account number,
and it does not reach a scheduled task's process either way. This session
has no tool that can set an environment variable on the Claude Code Remote
environment itself; that has to be done from the environment's settings in
the Claude Code on the web UI. This specifically affects the **three
code-enforced live streams** — 10:30 morning and 12:30 midday (both via
`morning_dca.py`'s `MorningDCAExecutor`, which reuses this same import) and
3:30 final (`near_close_dca.py` directly). Per
`config/scheduled_tasks.json`'s `enforcement_by_stream` block, the 9:35
market-open task and the 11:30/1:30/2:30 intraday tasks have
`no_python_entry_point=true` — an external scheduler/agent runs them
directly rather than importing `near_close_dca.py`, so this sentinel/check
does not gate them at all, and their own account-pinning behavior needs to
be verified separately rather than assumed safe. Worth confirming against
recent `run_state.json` `blocking_errors` for the three code-enforced
streams before assuming they're failing closed.

**Trigger wiring (`list_triggers`):** every recurring DCA Routine currently
enabled (`DCA Verify 9:35/10:30/11:30/12:30/1:30/2:30/3:30`, the watchdog,
slippage sync, weekly/monthly refreshes, DST shift) has both
`persistent_session_id` and `create_new_session_on_fire` unset — the
default self-bind mode, which resumes whatever session originally called
`create_trigger` rather than spawning a fresh session against a named
environment. That's a plausible root cause for intermittent repo-access
loss: a self-bound session's container is reclaimed after inactivity same
as any other, and it's untested here whether resuming it after reclamation
reliably reclones the repo. `docs/live_wiring.md` already documents that
"each scheduled invocation is a fresh process" as a design assumption —
that's *why* the doc calls out `StrategyState` persistence as a required
caller responsibility rather than something the pipeline handles itself
(see above; it is not yet implemented) — and `create_new_session_on_fire=true`
with an explicit `environment_id` matches that fresh-process assumption more
directly than self-bind does, since it guarantees a fresh clone every firing
instead of depending on resume behavior. Not changed in this probe.

One correction to the paragraph above: `list_triggers` does not return each
Routine's prompt text, only its name/schedule/enabled metadata, so this
probe could **not** confirm what a "DCA Verify 9:35/10:30/…" Routine
actually does when it fires — the name reads like a post-hoc check rather
than the trade-execution task itself, which would make it a poor stand-in
for the `type: "trading"` entries in `config/scheduled_tasks.json`
(market-open 9:35, morning 10:30, midday 12:30, intraday 11:30/1:30/2:30,
final 3:30 — five schedule entries, seven execution slots). Whether the
actual trading execution is driven by these CCR Routines at all, by some
other Routine not matched by name here, or by the "external scheduler/agent"
`config/scheduled_tasks.json` refers to for the no-`python_entry_point`
streams, is unresolved. Given that, repointing *any* of these triggers'
session-binding mode without first confirming which ones place real orders
is a real-money-affecting change and needs a decision from the user, not a
unilateral edit from a probe session — the mapping from Routine name to
what it actually fires still needs to be nailed down first.

**Open question for the next probe:** does a self-bound Routine, when it
fires after its originating session's container has been reclaimed, land
back in a working `all-stock-dca` checkout at the current `origin/main`, or
does it fail to resume at all? This session can't observe that from the
outside — it can only be answered by a self-bound Routine firing after a
gap long enough for reclamation, and checking `git status` /
`git remote -v` immediately when it does.
