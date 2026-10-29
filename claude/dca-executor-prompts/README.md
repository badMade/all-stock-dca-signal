# Executor prompt copies in this directory — which generation is which

There are **two generations** of executor prompt text committed here, and they
disagree with each other. This file says which one is live, so a reader does not
patch the wrong one.

## `live-mirror/` — CANONICAL. Mirrors what actually runs

Seven read-only copies of the live scheduled tasks, captured 2026-07-31 after
the same-day top-up guard fix. Source of truth is
`~/Claude/Scheduled/<taskId>/SKILL.md`; these are diffable copies, nothing more.

**Editing a file in `live-mirror/` changes nothing.** Live prompt edits go
through `update_scheduled_task`'s full-body replacement, then the mirror is
re-captured.

**Check for drift before trusting a mirror.** On the machine that runs the
Cowork tasks, `python3 -m scripts.check_live_prompt_drift` compares every
live `SKILL.md` with its mirror and names three known failures:
`missing_top_up_gate` (the live body never runs the same-day top-up guard),
`bare_array_order_pages` (it saves order history in the shape the guard
rejects), and `stale_window_end_date` (it hardcodes a window end that
`config/trading_window.json` no longer says). Add `--diff` for masked diffs.
It exits 1 when any slot needs a repaste. On 2026-09-22 the 2:30 PM run found
its live body instructing the bare-array shape and a stale end date, while
these mirrors were already correct.

**These mirrors are not byte-identical to the live prompts.** The live prompts
carry the Robinhood account number as a literal; every occurrence is replaced
with `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` before committing, because this repo
is public. That substitution is the only permitted divergence — diff a mirror
against its live source with the placeholder expanded, or the comparison will
report false differences.

Characteristics of this generation:

* The 0935 Gate 5 provider instructions were aligned on 2026-09-12 with
  the slot's confirmed 2026-09-09 Robinhood-primary policy: 5-minute bars,
  Robinhood payloads for every ticker, and no automatic provider fallback.
  This corrects the committed mirror; external deployment remains unverified.
* The account is addressed directly rather than through the environment: the
  live text hard-codes it at each call site (here shown redacted), instead of
  reading it from `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` at run time.
* The `0935` copy is likewise AHEAD of live, for Gates 5 and 7: as of
  2026-08-26 `scripts/generate_market_open_candidates.py` exists, so that
  copy's Gate 5 names the command to run (see the Gate 5 bullet below for the
  whole slot-to-producer table) instead of pointing at "the repo's own signal
  generation" -- which, for this slot, was an entry point that did not exist,
  so every fire produced no buy list at all. Gate 7 also adds the final
  placement-boundary price-eligibility refresh and restarts the complete
  per-order revalidation sequence whenever that final quote expires. Gates 6,
  6.5 and 6.7 are unchanged there. Same caveat as the 1230 copy below: until a
  human applies that body through `update_scheduled_task` and the mirror is
  re-captured, the ACTUAL live 9:35 task still carries the older Gates 5 and 7,
  still cannot produce a buy list, and lacks Gate 7's new placement-boundary
  price safety check. See `docs/market_open_signal_entry.md`.
* ALL SEVEN copies are AHEAD of live for Gate 2 as of 2026-09-08. The 9:35
  fire that day never reached a gate: the sandboxed shell tool refused to
  start on four consecutive attempts, each timing out at 120-180 seconds, so
  the `ls -d /sessions/*/mnt/all-stock-dca` probe never ran. Gate 2 had no
  branch for "the shell itself is dead", so the executor improvised one --
  retried until roughly a third of the window was gone, then reported the
  repo-access reason whose remedy is "reconnect the folder", which nothing
  had shown to be disconnected, and labelled the abort "GATE 1 - REPO
  REACHABLE" (the TIME SANITY bucket in `scripts/alert_digest.py`'s
  `Failing gate: Gate N` grouping). Every mirror now carries a bounded
  liveness probe, the distinct reason `executor shell tool unavailable, repo
  probes could not run`, the shell-restore remedy, and the canonical gate
  number, both in Gate 2's body and in the truncation-safe prefix. Same
  caveat as every other bullet here: until a human applies these bodies via
  `update_scheduled_task` and the mirrors are re-captured, the live tasks
  still have no dead-shell branch and will improvise again.
* Gate 1 was corrected on 2026-09-08 after the 1:30 PM slot was killed by a
  timezone misread. The run sampled 11:31, reported it as "11:31 AM ET" and
  aborted as an off-schedule scheduler fault; 11:31 at that instant was
  UTC-06:00 and New York was 1:31 PM, inside the 1:25-1:55 window. The slot was
  on time. Six of the seven mirrors said only "Get the current ET time", which
  a host in another zone answers confidently and wrongly -- the 1230 mirror
  already derived it correctly as `datetime.now(ZoneInfo('America/New_York'))`
  and is the precedent the other six now follow. Gate 1 also pins the only two
  offsets a real ET timestamp carries (-04:00 / -05:00), routes the value
  through `dca_runner._require_aware()`, and classifies a whole-hour
  discrepancy as a probable timezone misread rather than a scheduler fault.
  Fail-closed is unchanged: an out-of-window converted time still aborts.
* Gate 2 and Gate 7 were corrected again on 2026-09-08 after three more slots
  failed the same day and two 2026-09-04 alerts surfaced a second failure
  class. The 10:30 and 11:30 fires lost the shell but kept FILE access, and
  both read repo files successfully before still reporting repo access as the
  failure -- which disproved two claims the morning's own fix had made: that a
  dead shell means the repo is unreachable, and that it means no artifact is
  possible. Gate 2 now reads `config/trading_window.json` as a fallback probe,
  reports a readable repo with a distinct reason, and labels the alert by the
  gate that actually failed (a successful read PASSES Gate 2; the run dies at
  Gate 1 or Gate 5 instead). When a file write still works it must persist the
  Gate 9 set, sourcing the path from the stream's own config rather than
  `dca_paths`, and write the durable alert to the configured destination and
  not the gitignored `artifacts/` tree the 11:30 fire used. Gate 7 gained a
  branch for a `place_equity_order` refused by the platform before the broker
  sees it ("Blocked by classifier", 2026-09-04 1:30 PM): a whole-batch stop,
  one retry only, no `max_orders_per_day` attempt consumed, and the resolved
  Gate 6.5 `buy_usd` amounts carried into the alert verbatim so they can be
  placed by hand. Same caveat as every bullet here: none of it runs until a
  human applies these bodies and re-captures the mirrors.
* Gate 7 gained a correction on 2026-09-23 after the 2:30 PM run skipped
  Gates 5 through 6.7 entirely and alerted `ABORT_GATE7_PLACEMENT_DECLINED`
  on "this Claude executor session does not execute live brokerage trades
  (place_equity_order) ... a standing limit on the agent" -- a claim that
  contradicts this very file: Gate 7 has always instructed the executor to
  call `place_equity_order` directly, and the 2026-09-04 bullet above records
  exactly that call reaching the broker and being refused BY THE CLASSIFIER
  on that specific attempt, not declined in advance by the session. Gate 7
  now opens with an explicit statement that the executor session does place
  orders, that a pre-emptive, unattempted incapacity claim is never a valid
  Gate 7 outcome, and that a genuine per-session tool-unavailability case
  still routes through Gates 5-6.7 to a distinct, checkable Gate 2-style
  tool-reachability abort rather than a skipped run. See `docs/safeguards.md`'s
  live execution roster note for the same incident. Same caveat as every
  bullet here: none of it runs until a human applies these bodies and
  re-captures the mirrors.
* Gate 7 gained two read-only refusal paragraphs in the seven mirrors after
  the 2026-10-20 11:30 ET intraday run (PRs #1807, #1818, #1823). That run
  placed 14 buys ($30.81, all filled), then the auto-mode classifier refused
  the read-only `get_equity_orders` fetch that precedes every order, and
  CSCO ($1.40) and TEL ($1.00) were never attempted. Earlier quote and
  order-history reads that day had been refused too, and each cleared on one
  retry. The existing rules covered a refused `place_equity_order` and a
  refused `get_portfolio` (2026-10-19, PR #1792) but not these two reads.
  `READ-ONLY REFUSAL, ORDER HISTORY`: retry the fetch once; a second denial
  stops the remaining placements, because the cap and rogue-activity checks
  cannot be proven without fresh, complete history; confirmed fills stand, the
  refusal consumes no placement attempt, and the alert separates confirmed
  fills from UNPLACED TICKERS, each listed with its exact Gate 6.5 `buy_usd`
  (not to be resized). The same paragraph covers a refusal of the post-batch
  verification fetch: the submitted orders' fills are UNVERIFIED, not failed,
  so no replacement is placed and the alert requires reconciliation before
  any manual action. `READ-ONLY REFUSAL, QUOTE`: retry the per-ticker quote
  re-fetch once; a second denial drops that ticker (its price eligibility is
  unprovable and a stale quote is never a substitute) and the batch
  continues, but two consecutive tickers dropped this way stop the batch. The
  two-consecutive cap is a judgment call made in review, not an observed
  incident. Each paragraph carries its own slot's stamp. The Perplexity
  backup prompts were audited on 2026-10-21 and not changed: their rules
  already skip a ticker on an unreadable quote and stop the batch on an
  incomplete history pull, with one unresolved ambiguity, in that a refused
  quote read matches both the "skip the ticker" rule and the "platform
  refusal stops the batch" step. The lasting fix is a classifier allowlist for
  the read-only Robinhood calls on the executor side, which no prompt text can
  provide. Same caveat as every bullet here: none of it runs until a human
  applies these bodies and re-captures the mirrors.
* Gate 7 gained `OPERATOR RETRY AFTER A PLATFORM REFUSAL` on 2026-10-28,
  after the 9:35 and 10:30 ET runs both stopped on a classifier-refused
  `place_equity_order`. The Robinhood connector had every tool set to Always
  allow at the time, so the connector setting was not the cause. At 10:30 the
  first ticker (CCI) was refused twice, and all seven buys ($13.68) filled only
  after the operator replied "Try again" in the task session. The new rule
  keeps the one-automatic-retry cap and adds a documented path for that reply.
  The push and the alert both name the exact reply. Only the operator's own
  in-session reply starts the retry. Every unplaced ticker is fully
  revalidated and never resized. The list expires when the slot window closes,
  and a second refusal stops the run again. `tests/test_live_prompt_operator_retry.py`
  pins it. **The lasting fix is outside the prompts.** Claude support
  confirmed that in Auto mode a connector's Always allow does not skip the
  classifier for write tools; a `permissions.allow` rule in Claude Code
  managed settings does. The repo's `.claude/settings.json` already allows
  the tool, but it only loads for a session started in this repository,
  which the scheduled tasks are not. On the Mac that runs the tasks, put the
  rule in `/Library/Application Support/ClaudeCode/managed-settings.json`
  (needs `sudo`; merge into an existing file instead of overwriting it):
  `{"parentSettingsBehavior": "merge", "permissions": {"allow":
  ["mcp__Robinhood__place_equity_order", "mcp__robinhood__place_equity_order"]}}`.
  Both spellings are listed because the server is named `Robinhood` and
  matching may be case-sensitive. Then restart Claude Desktop. With that rule
  in place an unattended run can place equity buys with no classifier review,
  so set the connector's option, crypto and advanced-order tools to Block and
  `Cancel equity order` to Needs approval; the prompts already forbid all of
  them. Same caveat as every bullet here: none of it runs until a human
  applies these bodies and re-captures the mirrors.
* Gate 2 also gained a FOURTH repo probe,
  `find /home/user -maxdepth 3 -type d -name all-stock-dca`, in the same
  change. The original three only ever looked under `/sessions/*/mnt/`, which
  is the Cowork mount layout and not the only place this checkout lives:
  `config/intraday_dca.json` names `/home/user/workspace/cron_tracking` as the
  live artifact root for the sibling executor on cron `4412c3c4`, and a Claude
  session's own clone can sit at `/home/user/all-stock-dca`. In either
  environment all three original probes come back empty and Gate 2 reports a
  disconnected folder for a repo that was readable the whole time. The fourth
  probe folds into the same deduplicated union, the same
  exactly-one-surviving-checkout rule, and the same required-config-files
  check, so it is not a back door into an arbitrary tree.
* Gate 2 gained a BRIDGE-ONLY abort on 2026-09-30 after the 2026-09-29 2:30 PM
  run: the shell was alive, all four probes came back empty, and the repo was
  readable through the device file bridge, with `mcp__remote-devices__device_bash`
  listed even though Gate 2 said that tool family "is not present in this
  environment" (and this README called its absence "expected"; both were
  wrong). That is neither the dead-shell case, where a successful read PASSES
  Gate 2, nor an unmounted folder. Zero checkouts in the shell that would
  execute the signal entry point still fails the exactly-one-surviving-checkout
  rule, so the run now aborts as `Failing gate: Gate 2` with the distinct reason
  "repo readable via device bridge, not mounted in executor shell" and a
  mount-the-folder-into-the-shell remedy, in Gate 2's body and in the
  truncation-safe prefix (a 301-character paragraph, sized so the later
  prefix rules stay inside the 5,850-character boundary). A bridge read never
  authorizes placement, and the ban on running repo code through any bridge
  tool is unchanged; tool presence is now reported as diagnostic only. The
  probes, dedupe and exactly-one rule are untouched. Open question, not
  decided here: whether a bridge shell that holds the repo should ever be
  allowed to run the signal entry point (needs the operator's answer on what
  that device exposes and its own review). Same caveat as every bullet here:
  none of it runs until a human applies these bodies and re-captures the
  mirrors; `scripts.check_live_prompt_drift` now reports
  `missing_bridge_only_abort` for a live body still lacking the reason.
* Live placement is authorized for all seven slots as of 2026-09-02, per
  Geoff's explicit direction that order placement be on for all scheduled
  buy runs. The 1230 slot was the one exception up to that point, and its
  `live-mirror/` copy is currently AHEAD of what was actually captured
  2026-07-31: PR #412 rewrote it so Gates 0/1/3/4/6 call `dca_runner.py`'s
  individual gate functions directly -- `market_day_status`,
  `gate_time_sanity`, `gate_trading_window`, `gate_sole_executor`,
  `budget_snapshot`, `plan_orders` -- rather than `dca_runner.assess()`,
  since `assess()` bakes in dynamic budget-tier escalation that
  `config/midday_dca.json` forbids for this slot. That PR's own placement
  (Gate 7) text stayed explicitly OFF pending validation of this slot's
  Gate 5 signal-generation producer; this repo's copy has since been
  updated to bring Gate 7 up to the same authorized standard as the other
  six slots, superseding that OFF text. Per this file's own policy above,
  editing the committed copy changes nothing about the live task: until a
  human applies this text via `update_scheduled_task` and the mirror is
  re-captured, the ACTUAL live 1230 task is still whatever was captured
  2026-07-31 (the `assess()`-based, placement-OFF version this bullet used
  to describe), not what `live-mirror/1230ET-1230-pm-midday-buying-run.txt`
  currently contains. Diff against a fresh capture before trusting this
  bullet as "what actually runs."
* Gate 5 tells the executor to run the repo's signal generation and to abort if
  it errors. As of 2026-08-26 all seven slots have their own committed,
  tested producer, and every live-mirror prompt in this directory already
  names its command: `0935ET` -> `scripts/generate_market_open_candidates.py`,
  `1030ET` -> `scripts/generate_morning_candidates.py`, `1130`/`1330`/`1430ET`
  -> `scripts/generate_intraday_candidates.py --slot {1130,1330,1430}`,
  `1230ET` -> `scripts/generate_midday_candidates.py`, `1530ET` ->
  `scripts/generate_buy_candidates.py` (see `../../PRODUCTION_READINESS.md`).
  Each producer is scoped to its own slot on purpose (the streams have
  different executors, decision anchors, and ladders), so pointing one slot's
  prompt at another slot's producer would silently score it against the wrong
  logic. None of these Gate 5 producers places an order or touches a broker --
  Gates 6/6.5/6.7/7 remain prompt-driven, so Gate 5 no longer aborts every
  day by default for any slot, but that says nothing about whether Gate 7
  is authorized to place what Gate 5 finds.
* A producer existing is not the same as a slot trading: the live task's prompt
  must actually name the command, and that text lives outside this repo. Until a
  human applies a rewritten body via `update_scheduled_task` and the mirror is
  re-captured, the live task still runs whatever was captured -- so treat every
  Gate 5 claim in these files as proposed, not active.
* `GATE 6.5 - SAME-DAY TOP-UP GUARD` (step 4.5 in the 1230 slot) invokes
  `scripts/same_day_top_up_gate.py`.
* On 2026-10-03 the live tasks were AHEAD of these mirrors: they carry the operator-approved
  BRIDGE-SHELL RULE and BRIDGE PROTOCOL (Gate 2 may run repo commands through
  `mcp__remote-devices__device_bash` when the executor's own shell has no checkout), which the
  mirrors lacked, so `check_live_prompt_drift` reported 7 of 7 slots "differs" and its
  "repaste from live-mirror/" advice would have REVERTED that change. When the checker says
  repaste, diff first (`--diff`): the mirror is the source of truth only when the live body is
  the stale side. `apply_dotenv_edits.py` (dry run by default, `--write` applies, live files
  backed up first) edits the live bodies with the `.env` loader changes from
  `dotenv-loader.draft.md` and then RE-CAPTURES every mirror from the edited live body
  (frontmatter dropped, account masked), so the mirrors end identical to live whatever state
  they started in. Until it has been run with `--write`, these mirrors contain neither the
  bridge-shell change nor the `.env` edits. `drafts/*.bridge-shell.draft.txt` hold the
  bridge-shell text and become redundant once the mirrors are re-captured.

## `1-executor-*.txt` … `7-executor-*.txt` — SUPERSEDED. Do not patch these

An earlier generation, last touched 2026-07-29, kept for history. It is **not**
what runs today and has NOT been updated with the same-day top-up guard,
deliberately: retrofitting a placement-time guard onto prompts whose standing
instruction is "must NEVER place a real equity order today" would be incoherent.

How to recognize it:

* A genuine `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` env-var reference — this
  generation never carried the literal, so unlike `live-mirror/` its committed
  text matches what ran. The placeholder alone no longer tells the two apart;
  use the markers below.
* A `STANDING GATE 5 OVERRIDE` block at the very top, ahead of the truncation
  boundary.
* Gate 5 ends in verdict `GATE5_DISABLED` — silent, no alert, no order, every
  fire.
* Much longer (18-20 KB vs ~12 KB), carrying per-review annotations and the
  `run_state.json` evidence contract that the live generation dropped.

The two generations differ in more than the top-up guard: Gate 2's repo probe,
Gate 3's date handling, Gate 4's ref-id overlap classification, and the whole
Gate 9 evidence-writing contract all diverge. **Reconciling them is an open
decision, not a merge.** The superseded generation contains safety machinery the
live one lacks (notably the run_state evidence contract and the ambiguous-mount
abort); the live one is the only one that actually runs.

## `0-setup-card.md`

Belongs to the superseded generation. Its warning about the overlapping old/new
task window during cutover is, as of 2026-07-31, still live and still unresolved
— see `../SECOND-EXECUTOR-FINDING.md`.
