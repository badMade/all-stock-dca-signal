# Gate 5 alert review — 2026-07-29 10:30 AM ET slot

Status: **Investigation finding, revised twice on 2026-07-30 after PR review
caught issues in each prior draft — see "Revision history" at the bottom. No
code change made, though the second revision surfaced one standing
code/config inconsistency left for a human decision (§3). The original
draft's "confirmed deviation" conclusion did not survive review; this
version is a materially weaker, more hedged claim.**

See `claude/GATE5-PROPOSAL.md` for the underlying (and still unapplied)
design proposal this incident references.

## 1. What happened

On 2026-07-29, an ABORT alert arrived at 10:30 AM ET — sent to Slack, a
claimed repo alert file, and a Gmail draft — that **appears**, based on its
own wording, to come from the 10:30 slot of the seven-slot Claude executor
roster (see §5); which task actually fired is not confirmed and is one of
this document's open questions, not a settled premise. The alert reported a
Gate 5 failure: it described invoking
`morning_dca.MorningDCAExecutor` in live mode, hitting its internal
`_apply_preflight_gate()`, and getting blocked because no same-day 9:45
pre-flight artifact existed. No order was placed **per the alert's own
self-report** — this document has not independently verified that against
Robinhood order history, and §5/§6 below explain why that matters here more
than usual. The alert predicted five more identical alerts later the same
day (11:30, 12:30, 1:30, 2:30, 3:30) — all of which have, by the time of
this revision, already elapsed.

## 2. The central problem: the prompt this document compares against did not exist in the repo at incident time

`git log --diff-filter=A -- claude/dca-executor-prompts/2-executor-1030-cron_30_14.txt`
shows it was first committed by the PR #232 merge at
**2026-07-29T20:55:32-04:00** — over ten hours *after* the 10:30 AM ET fire
this document is about. The same
is true of `0-setup-card.md`. `0-setup-card.md` itself documents that it
supersedes an earlier pasted version with different behavior (its "What
changed from the version you pasted earlier" section: different budget caps,
a different ladder, and — directly relevant here — "**Notifications: in-app
message center + email to you, not Slack**," language that only makes sense
if an earlier version used Slack).

That means this document's original comparison — "here is the committed
prompt text, and the alert deviated from it" — compares the alert against
text whose deployment status at 10:30 AM is unconfirmed, not text
established to have been undeployed then. "Not yet committed to this repo"
and "not yet pasted into Cowork" are different claims: `0-setup-card.md`'s
workflow is a manual paste from a local working copy, so the current text
could in principle have already been live in Cowork from an uncommitted or
differently-branched local file before it was ever pushed here — the commit
timestamp bounds when the repo learned about this text, not when a human
put it into the scheduled task. It is entirely possible an earlier prompt
revision was live in Cowork at 10:30 AM and that revision genuinely
specified Slack, or handled the missing pre-flight artifact as a real ABORT
rather than `GATE5_DISABLED` — but it's equally possible the current text
was already deployed. Nothing in this repository can establish which
revision was actually pasted into the scheduled task at the time; only
whoever manages the Cowork task configuration can answer that, by checking
the task's edit history or asking whether it was updated between 10:30 AM
and the PR #232 push that evening.
**Everything below compares the alert against the current, committed prompt
only — it is a check of "does today's version explain this," not proof of
what actually ran.**

## 3. Against the current prompt: partial match, not a clean contradiction

Two claims in the original draft of this document were wrong and are
corrected here.

**Gate 9 does mandate multi-channel alerting for a real ABORT.** The
original draft treated any alert on this slot as automatically wrong,
citing only Gate 5's silent `GATE5_DISABLED` path. That was incomplete:
Gate 9 in `2-executor-1030-cron_30_14.txt` separately says, for any
*genuine* abort (as opposed to the known-disabled Gate 5 case): *"alert on
ALL THREE channels below, in order... 1. IN-APP MESSAGE CENTER, primary...
2. REPO ALERT FILE, durable fallback... 3. EMAIL DRAFT, belt-and-braces."*
Three channels for a real abort is exactly what the received alert did.
The one concrete mismatch against the *current* prompt is channel 1: Gate 9
specifies the in-app message center as primary, and the alert used Slack as
primary instead. Whether that is a real discrepancy depends entirely on §2 —
if an earlier prompt revision specified Slack, there is no mismatch at all.

**`dca_alerts.py` has a second, real, non-drift Slack path.** The original
draft claimed the only Slack path in this repo is
`scripts/drift_detection.py`'s opt-in webhook. That is false.
`morning_dca.py:3719-3723` documents that scheduled live invocations of
`MorningDCAExecutor.run()` execute under `dca_alerts.guarded_run`, which — on
an escaping exception — calls `alert_failure()`, posting to
`DCA_ALERT_SLACK_WEBHOOK` (falling back to `DCA_DRIFT_SLACK_WEBHOOK` if
unset; see `dca_alerts.py:21-23,50-54`) before re-raising. So a live
`MorningDCAExecutor` run genuinely can produce a Slack alert through
already-committed code, independent of any executor-prompt text and
independent of `config/notifications.json`'s routing table, which
`dca_alerts.py` does not consult at all.

That said, this specific path fires on an **escaping exception** from
`_run_impl`, not on the ordinary handled return `_apply_preflight_gate()`
produces when it blocks a scheduled run (`preflight_missing_block: true` is
a normal, caught result, not a raised exception) — so it's not established
that this is literally the mechanism that produced today's Slack message.
It does establish that "Slack shouldn't have been possible here" was wrong
as stated in the original draft.

**This is itself a real, separate code/config inconsistency, independent of
today's incident.** `config/notifications.json` documents
`DCA_DRIFT_SLACK_WEBHOOK` as scoped to `scripts/drift_detection.py` only
("Opt-in path for immediate drift alerts from scripts/drift_detection.py
only"). But `alert_failure()` (`dca_alerts.py:256-257`) reads
`DCA_ALERT_SLACK_WEBHOOK` **or** `DCA_DRIFT_SLACK_WEBHOOK` as a fallback for
*any* escaping exception from a scheduled live run, not just drift alerts.
An operator who sets only `DCA_DRIFT_SLACK_WEBHOOK`, believing it's scoped
to drift per the documented config, gets scheduled-live-failure Slack
alerts too, whether they want them or not. This document doesn't resolve
which behavior is correct — that's a product decision (should the two stay
coupled as a deliberate belt-and-braces default, or split into distinct env
vars) — but §6 below no longer claims the code and documented config are
fully consistent; they aren't, on this one point.

## 4. The "repo durable fallback" channel: path corrected, but durability still unconfirmed

The alert described writing `artifacts/alerts/2026-07-29-1030ET-gate5.txt`
(and a same-morning 9:35 counterpart). Checked against this repository's
full history across every branch: no path under `artifacts/alerts/` has ever
been committed anywhere, and `docs/scheduled_session_repo_access.md`'s
2026-07-29 probe already established that an uncommitted local write from a
scheduled session does not survive that session's container being reclaimed.
That part still holds: if the alert file was written but never committed and
pushed, it was not durable regardless of prompt version.

One correction: the current prompt's Gate 9 does not actually specify a bare
`artifacts/alerts/...` path — it specifies
`dca_paths.stream_root('scheduled_runs', 'alerts', '<YYYY-MM-DD>-<slot>-<gate>.txt')`,
explicitly calling out that this must be resolved through the shared
`dca_paths` helper "never a bare `artifacts/alerts/...` path relative to the
session's working directory." The alert's stated path
(`artifacts/alerts/2026-07-29-0935ET-gate5.txt`) is exactly the bare-relative
form the current prompt calls out as wrong — another data point consistent
with an earlier prompt revision being the one actually in effect (see §2),
since this specific correction is the kind of thing a prompt revision would
exist to fix.

This fixes *which path is correct*, not *whether that path is actually
durable*. `dca_paths.stream_root()` just resolves `$DCA_CRON_TRACKING_ROOT`
(or the bundled `/home/user/workspace/cron_tracking` default) — an ordinary
local filesystem path with no persistence guarantee of its own. Whether
writes there survive a reclaimed Cowork/CCR container the way an
uncommitted git edit does not (per
`docs/scheduled_session_repo_access.md`'s framing of "pushed to the repo or
persisted outside it") depends entirely on whether that root sits on
storage that outlives the session — something neither this document nor the
prompt text establishes. So the durability gap in the original draft's
finding is only partly closed: the prompt now points at the *right* path
resolution mechanism, but nothing here confirms that resolved location is
actually durable. Gate 9 needs either a `$DCA_CRON_TRACKING_ROOT` verified to
point at storage that outlives the session, or an explicit commit-and-push
step for the alert file, before this channel's "durable fallback" label can
be treated as confirmed rather than aspirational.

## 5. The legacy 10:30 task (42173154) is real, separate, and not yet ruled out of this incident

`docs/morning_dca.md` and `config/scheduled_tasks.json` describe a genuinely
live, currently-functioning task — `42173154`, using this same
`morning_dca.MorningDCAExecutor` class in live mode — that legitimately
depends on the same-day 9:45 pre-flight artifact. **Correction: the code
does not establish that a missing artifact makes this task alert, only that
it fails closed.** `_run_impl()`'s `preflight_missing_block` branch
(`morning_dca.py:4170-4196`) builds a normal `status="blocked"` result
through the ordinary artifact pipeline and returns it — no exception is
raised. `dca_alerts.guarded_run()`, which wraps scheduled
`MorningDCAExecutor.run()` invocations, sends its Slack failure alert only
when an exception escapes `fn`; its own docstring is explicit that a
result-based failure (which a `status="blocked"` return is) pings a failed
heartbeat at most and deliberately sends **no** Slack alert, to avoid
double-paging over a structured failure path. So this repo establishes
fail-closed *artifact writing* for `42173154`, not an alert contract for it.
Whatever external scheduler prompt fires `42173154` might add its own
alerting on top of that blocked result — the same way the seven-slot
roster's prompts add alerting logic the repo's Python code doesn't provide
— but that prompt isn't in this repository, so `42173154`'s actual
notification behavior on a missing preflight artifact is unknown here, not
established. That task is produced by `bb4f6ef1`
(the 9:45 AM "morning DCA watchlist early-warning scan," active per
`config/scheduled_tasks.json`, which `also_runs_required_preflight_writer`
requires to invoke `MorningDCAExecutor` in both preflight and watchlist mode
at 9:45). `bb4f6ef1` has `no_python_entry_point=true`: there is no in-repo
script for it, so the artifact only exists if whatever session/scheduler
fires at 9:45 actually performs both of those calls. This repo-side session
cannot see the Mac's `cron_tracking` tree or its 9:45 execution history, so
it cannot confirm whether `42173154` (the real task) failed for a genuine
reason today, or whether the alert that arrived was entirely a seven-slot
roster artifact, mis-describing itself as this legacy task's executor. The
report's own text — "Slot 2 of 7," an explicit reference to
`docs/safeguards.md#live-execution-roster`, and side-by-side comparison with
the 9:35 slot's "no entry point at all" — is seven-slot-roster framing, not
language used anywhere for `42173154`, which points to this being (some
revision of) the 10:30 seven-slot prompt's fire, not the legacy task. This is
inferred from the alert's own wording, not confirmed against Mac-side state.

## 6. Conclusion and next steps

**No code change is made here**, but not because everything checked out:
§3 found one real, standing code/config inconsistency (the drift Slack
webhook doubling as a scheduled-live-failure fallback, contrary to
`config/notifications.json`'s documented scope for it), and §2 means this
document still can't establish what actually ran at 10:30 AM. Fixing the
§3 inconsistency is a product decision (couple the two alert types
deliberately, or split their env vars) left to a human rather than made
unilaterally in a same-day incident write-up.

Human-actionable next steps, revised for the fact that all seven of
2026-07-29's scheduled windows have elapsed by the time of this revision
(the original draft's "pause the remaining slots" recommendation is no
longer executable and is retracted). Note the distinction below: "the
window elapsed" is a fact this document can establish from the clock alone;
"the task fired" is not — this repo cannot confirm any slot actually ran
without the tool-call/alert history in step 2, and a slot whose window
merely passed could equally be a missed fire, not a quiet expected one.

1. **Verify "no order was placed" against actual broker history, reconciled
   by ref ID — not a bare zero-orders check.** Query Robinhood order history
   (including client/ref IDs) for the 10:25-11:00 AM ET window on
   2026-07-29. Zero orders is *not* the expected-success bar: `42173154` is
   a real, intentionally-still-running legacy executor sharing this exact
   window, and per `2-executor-1030-cron_30_14.txt`'s Gate 4, any order
   carrying its `morning-dca-<trading_day>-<ticker>` ref-id convention is
   *expected* legacy activity, not incident evidence — that same Gate 4
   text is why the roster's own prompt already treats an all-`morning-dca-`
   window as silent and unremarkable. Classify what's found instead:
   `morning-dca-*` = the legacy task's own expected fill, informational
   only for this incident; `dca-runner-<slot_id>-*` = the seven-slot roster
   genuinely placed an order, which would directly contradict Gate 5's
   unconditional "never place a real order today" override and *would* be
   the incident's real finding; anything else unrecognized needs its own
   investigation. Do this before treating the incident as closed with no
   placement risk.
2. **Resolve §2 first, using each task's own fire time.** Whoever manages
   the Cowork task configuration should check whether the 9:35 task prompt
   was edited between 9:35 AM ET and the PR #232 push that evening
   (20:55 ET), and separately whether the 10:30 task prompt was edited
   between 10:30 AM ET and that same push — each from its own fire time, not
   both from 10:30, since an edit made in the 9:35-10:30 window would matter
   for the 9:35 task and be invisible to a check that only starts at 10:30.
   If either was still on an earlier revision at its fire time, that
   revision — not the current committed file — is what should be compared
   against the alert, and may fully explain the Slack channel and the
   alert-file path without any deviation at all.
3. **Retrospective audit, not a forward-looking pause — and confirm fire,
   not just elapsed window.** Pull the actual tool-call/alert history for
   all seven of 2026-07-29's slots (9:35 through 3:30), not just 9:35 and
   10:30, and check each against whichever prompt revision was live for it
   at its own fire time. Establish for each slot whether it actually fired
   at all before assuming a quiet slot was an expected `GATE5_DISABLED`
   silence rather than a missed fire — the two are indistinguishable from
   the elapsed-window fact alone.
4. Separately, and only if `42173154` is suspected of having genuinely
   failed today: that needs Mac-side confirmation of whether `bb4f6ef1`
   fired at 9:45 and completed its preflight-mode call, and of whether
   `42173154`'s own deployed scheduler prompt (not the code alone, per §5)
   actually alerts on a blocked result — this repo session cannot check
   either.

## Revision history

- **2026-07-30 (fourth revision):** a further PR review round
  (`chatgpt-codex-connector`) found P2 — §1's opening sentence stated as
  fact that "the seven-slot Claude executor roster fired," even though §5
  and §6 both treat which task actually fired as an open, unconfirmed
  question (the legacy `42173154` task is a live alternative). Reworded to
  say the alert only *appears*, based on its own wording, to come from that
  slot, matching the hedging already present elsewhere in the document.
- **2026-07-30 (fifth revision):** a further PR review round
  (`chatgpt-codex-connector`) found P2 — §6's broker-verification step told
  the operator to confirm *zero* orders in the window, but `42173154` is a
  real, intentionally-still-running legacy executor sharing that exact
  window, and its own `morning-dca-*` ref-id fills are expected per Gate 4
  of the seven-slot prompt, not incident evidence. Rewrote the step to
  reconcile by ref-id prefix instead of demanding zero: `morning-dca-*` is
  expected and informational, `dca-runner-<slot_id>-*` would be the actual
  incident finding, anything else needs its own look.
- **2026-07-30 (third revision):** another PR review round
  (`chatgpt-codex-connector`) found: (1) P2 — §5 claimed `42173154` "is
  supposed to abort and alert" on a missing preflight artifact, but the code
  only shows a normal `status="blocked"` return with no raised exception,
  and `guarded_run()`'s own docstring says a result-based failure gets no
  Slack alert — corrected to say the alert *contract* for that task is
  unknown, only its fail-closed artifact-writing is established; (2) P2 —
  §2 stated as fact that the current prompt text "was not the deployed
  contract" at fire time, when only "not yet committed to this repo" is
  established — a manual Cowork paste could have deployed the same text
  earlier from an uncommitted copy; softened to "deployment status
  unconfirmed"; (3) P2 — §1's "no order was placed" repeated the alert's
  self-report without independent verification, which matters more than
  usual here given §5's now-open question about a second candidate task in
  the same window — added a broker-history verification step as the new
  top recommendation. All three addressed above.
- **2026-07-30 (second revision):** a further PR review round
  (`chatgpt-codex-connector`) on the first revision found: (1) P2 — the drift
  Slack webhook's use as a scheduled-live-failure fallback in `dca_alerts.py`
  contradicts `config/notifications.json`'s documented scope for it, a real
  standing inconsistency the first revision's §3 had the evidence for but
  didn't call out as unresolved; (2) P2 — pointing at the correct
  `dca_paths`-resolved path (§4) doesn't by itself establish that path is
  durable, only that it's the right one to resolve; (3) P2 — "all seven
  slots have already fired" asserted something this document can't confirm;
  corrected to "elapsed" with fire-vs-missed left as an open audit question;
  (4) P2 — the 9:35 task's audit window should start at its own 9:35 fire
  time, not be folded into the 10:30 task's window. All four addressed
  above.
- **2026-07-30 (first revision):** PR review (`chatgpt-codex-connector`)
  identified four issues in the original same-day draft: (1) P1 — the
  compared-against prompt file postdates the incident by 10+ hours, so it
  cannot be treated as the deployed contract at fire time; (2) P2 —
  `dca_alerts.py` has a genuine, non-drift Slack path wired into scheduled
  `MorningDCAExecutor` runs, contradicting the claim that drift detection is
  the only Slack path in the repo; (3) P2 — the current prompt's Gate 9
  actually mandates three-channel alerting for a real abort, which the
  original draft did not check before declaring the alert's channel choice
  wrong; (4) P2 — the "pause the remaining slots today" recommendation was
  already non-actionable by the stated write date, since those slots'
  windows had already elapsed. All four are addressed above; the document's central claim
  is downgraded from "confirmed deviation" to "inconclusive, pending
  confirmation of which prompt revision was actually deployed at incident
  time."
- **2026-07-29 (original draft, superseded):** claimed the alert was a clear
  deviation from the current prompt's silent `GATE5_DISABLED` contract and
  from `config/notifications.json`'s routing, and that no Slack path other
  than the drift detector's existed in this repo. Both claims were
  incomplete or wrong; see above.
