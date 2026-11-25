# VTO: triage prep, cycle 2026-11-19

**What:** make the 2026-11-19 triage findings actionable: stop a repeat of the 13:30 history abort going unnoticed, make
the watchdog and the digest say the same true thing, and hand the operator exact, evidence-backed confirmations for the
rows only they can change.
**Why now:** on 2026-11-19 the 13:30 intraday slot placed 0 of 66 buys (`output_cap_exceeded`, Gate 6) although the
same-day baseline chain (#2246) was supposed to prevent exactly that; the 14:30 and 15:30 slots then hit caps. The digest
read the day as `ok` and finished PARTIAL.
**Who:** the operator who reads the 18:15 ET digest and approves Notion rows.
**Success this cycle:**
(a) the watchdog's JSON `ok` is false whenever a slot ended in an error outcome, and the digest prompt reads that;
(b) a slot that aborts for unpersistable history while an earlier same-day orders_pages file existed is flagged by name in
`triage_log_summary`, so the operator knows to check the live routine prompt against `claude/dca-executor-prompts/live-mirror/`;
(c) the digest can truthfully say COMPLETE when the batch path ran, and always attributes orders to slots;
(d) the executor-task UNKNOWN status is replaced by an evidence-derived status with a stated source;
(e) written, evidence-backed notes exist for the cap question (#3), the PR 2373 post-merge check (#6) and the three
unconfirmed recorded PRs (#7).
**Non-negotiables:** stdlib only; no live-path changes (no edit to `near_close_dca.py`, the stream executors,
`dca_runner.py` or any file under `claude/dca-executor-prompts/live-mirror/`); no Notion writes from code; no secrets or
account-number literals; every code change has a regression test; draft PRs only; prompts stay whole (one complete prompt
per task, no fragments).
