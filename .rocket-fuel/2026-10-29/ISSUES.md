# ISSUES (2026-10-29 Clarity Break)

Scope: the four DCA Issues rows reviewed in this session (classifier_block, transaction_block,
bridge_python_alert_channels, slot_did_not_fire), checked against the Oct 28 run artifacts.
Root repo `PLAN.md`/`VTO.md`/`SAME-PAGE-LOG.md` predate this run and are left alone.

- [impact: high] classifier_block + transaction_block: the allow rules went into Claude Code's `~/.claude/settings.json`, but the executors run as Cowork VM sessions (`/sessions/rcw-...` mounts in every Oct 28 artifact), which that file most likely does not govern — why it matters: 3 of 7 Oct 28 slots were refused twice by the classifier after the change; fills only happened when a human said "retry". Fix lives in the app's connector/tool permission for the Cowork tasks, not in the repo.
- [impact: high] Oct 28 3:30 aborted `GATE2_ABORT_NO_PYTHON311`: its shell had no repo mount, so it fell back to the device bridge, where `uv python install 3.11` failed ("cache at /tmp/uv-cache owned by nobody, permission denied") — why it matters: the final buying slot placed nothing. The bootstrap is ~600 chars of inline shell copied into all 7 live prompts with no fallback when the pinned /tmp dir is owned by another user, and nothing in the repo tests it.
- [impact: med] slot_did_not_fire: 11:30 and 12:30 left no run directory on Oct 28 (watchdog caught both) — why it matters: two buying slots lost; cause unknown, needs the Cowork tasks' run history. Not repo-fixable.
- [impact: low] Off-window abort reaches only the push channel — why it matters: only seen on a manual night-time Run now; the durable alert file is a heredoc that needs no Python, so the prompt could still write it.
- [impact: low] Notion rows are stale or wrong — why it matters: classifier/transaction rows say "untested" (it was tested Oct 28 and failed); the bridge row predates the 3:30 Gate 2 abort.
