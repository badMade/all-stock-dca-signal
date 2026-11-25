# VTO: triage prep, cycle 2026-11-19 (rows), issues 8-10

**What:** make "a fix is marked done with nothing behind it" visible in the daily digest, and give the operator exact text for the
rows only they can edit.
**Why now:** the 2026-11-02 cycle shipped `tr.audit_unsupported_fixes` but never connected it, so five Approved rows still read
Repaired / In review with no PR or commit, and the digest reports them only as "No work".
**Who:** the operator reading the 18:15 ET digest and editing Notion.
**Success:** (a) `triage_reconcile_batch` output carries an `unsupported_fixes` block and counts, and the digest prompt reports it;
(b) one table classifies every derived "No work" Approved row as has-evidence, no-repo-fix-possible, or truly unsupported;
(c) paste-ready Notion text exists for `output_cap_exceeded`, and the Progress-vs-derived-label design question is written up as an
operator decision with a recommendation.
**Non-negotiables:** stdlib only; no live-path changes (no edit to `near_close_dca.py`, executors, `dca_runner.py`, live-mirror
prompts); no Notion writes from code or from me; no secrets or account-number literals; every code change has a regression test;
draft PRs only; prompts stay whole.
