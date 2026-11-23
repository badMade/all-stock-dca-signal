# PLAN: triage prep rows (issues 8-10, ISSUES.md beside this file)

Revision 3 (after Same Page rounds 1-2, plus Rock 1b found by the real-data run). Vision: `docs/triage-cycle/2026-11-19-rows/VTO.md` (the one beside this file; the repo-root VTO/PLAN/ISSUES belong to an
unrelated cycle, and `docs/triage-cycle/2026-11-19/` is the merged first cycle, read-only history).

## Starting point (verified 2026-11-19 night)
- `selfheal/triage_reconcile.py:548` `audit_unsupported_fixes(rows, evidence: Mapping[str, GitEvidence])` returns three sorted Key
  lists: `unsupported_approved_fix`, `brief_acceptance_not_demonstrated`, `recorded_pr_not_found`. Tested in
  `tests/selfheal/test_triage_reconcile.py`. No caller outside tests.
- `scripts/triage_reconcile_batch.py` `_decide()` builds a `tr.GitEvidence` per Key but stores only a flattened `evidence` dict on
  the decision; `reconcile()` returns `counts`, `decisions`, `approved`, `approved_groups`. Tests: `scripts/tests/test_triage_reconcile_batch.py`.
- The prompt (`claude/triage-routine/prompt.txt`, repo copy; the scheduled task copy lives outside the repo) STEP 4 consumes the batch
  output; STEP 7 lists the digest sections.
- Notion DCA Issues SQL exposes `createdTime` only, no last-edited column.

## Rock 1 (#8): wire the audit into the batch
In `scripts/triage_reconcile_batch.py` record each Key's `tr.GitEvidence` in a side mapping whenever `tr.match_git_evidence` succeeds,
including rows that are later `skip`ped for `recorded_pr_unconfirmed` or an unverifiable shallow commit (the evidence exists; only the write is
skipped), and call `tr.audit_unsupported_fixes` over every row that has evidence. A row whose `match_git_evidence` raised `EvidenceError` has no
evidence to audit: list it under a separate `unsupported_fixes.audit_not_run` entry (`key` + `reason`), never under `unsupported_approved_fix`,
so a malformed row is surfaced without being called unsupported. The audit requires `PR number` to be a positive int while the batch accepts
Notion forms such as `"#10"` or a PR URL (`_recorded_pr()`), so build the audit's row copies with `PR number` set from `_recorded_pr(row)` (int or
None), leaving the caller's rows untouched. Result additions are strictly additive: a top-level `unsupported_fixes` object (the three lists plus
`audit_not_run`) and new `counts` keys (`unsupported_approved_fix`, `brief_acceptance_not_demonstrated`, `recorded_pr_not_found`, `audit_not_run`);
every existing key, count value and decision field keeps its name, type and value.
Update `claude/triage-routine/prompt.txt` STEP 6/7 and `docs/triage_routine.md`: the digest prints a short "Unsupported fix claims" section
from `unsupported_fixes` (Key, Status, Fix state; `audit_not_run` listed as "audit not run: <reason>"), never changes a row because of it, and counts
come from the batch lists.
Non-goals: editing `audit_unsupported_fixes`; writing to Notion; changing Progress derivation.
Proof: `.venv/bin/python -m pytest scripts/tests/test_triage_reconcile_batch.py tests/selfheal/test_triage_reconcile.py -q && ruff check scripts/triage_reconcile_batch.py scripts/tests/test_triage_reconcile_batch.py`
(new tests: an Approved/Repaired row with no PR, commit or issue lands in `unsupported_approved_fix`; a row with a matching PR does not;
a row with `PR number` given as "#10" or a URL does not raise; an EvidenceError row lands in `audit_not_run`, not in `unsupported_approved_fix`;
a `recorded_pr_unconfirmed` skip row is still audited; a regression test pins every pre-existing result key and count value on a fixed fixture).

## Rock 1b (#8): split the audit result by whether the row records its own evidence
Found by running Rock 1 on the real Notion rows (2026-11-19 night): `tr.match_git_evidence` keeps a row's recorded Commit URL only when the
commit subject names the Key or merges a matched PR (`selfheal/triage_reconcile.py` around line 380), and a recorded PR that does not cite the Key is
`recorded_pr_unconfirmed`. So rows the operator filled in by hand (for example `claude_app_not_running` with commit dd5ceed, `fabricated_empty_candidates`
and `logged_as_quiet_tape` with 9cf51fff, `robinhood_payload_rate_limited` with PR 2221) appear in `unsupported_approved_fix` beside rows with no PR or commit
at all, and the digest would present both as the same problem. In `reconcile()` add two additive lists inside `unsupported_fixes`:
`unsupported_with_recorded_evidence` (members of `unsupported_approved_fix` whose row has a non-empty PR number, after `_recorded_pr` normalisation, or a non-empty
Commit URL) and `unsupported_no_recorded_evidence` (the remaining members), plus matching `counts` keys. `unsupported_approved_fix` itself is unchanged (the union).
Update the prompt/doc sentence so the digest prints the two groups separately ("operator-recorded, not matched by text: confirm" vs "no PR, commit or issue recorded").
Non-goals: changing `match_git_evidence` or `audit_unsupported_fixes`; treating a recorded commit as proof of a fix (it stays an operator claim the audit does not verify).
Proof: same command as Rock 1, plus tests: a row with a recorded PR number and no match lands in `unsupported_with_recorded_evidence`; a row with a recorded Commit URL
only lands there; a row with neither lands in `unsupported_no_recorded_evidence`; the two lists partition `unsupported_approved_fix` exactly.

## Rock 2 (#8): classify the derived "No work" rows (note, no code)
Run the new batch on the real rows (Notion rows exported to a temp JSON, outside the checkout; `--collect` is read-only `gh`/`git`) and write
`docs/triage-cycle/2026-11-19-rows/NO-WORK-ROWS.md`: one table of every Approved row whose derived label is "No work" with, per row, the
Notion Status / Fix state, the repo evidence found by search (code, test or config that does what the row asks, ignoring proposal/incident
text per the prompt's own rule), and a verdict: `has repo evidence` (cite file:line), `no repo fix possible` (operational or external, say why),
or `unsupported` (operator to reopen or document). Also record the batch's real `unsupported_fixes` output as the Rock 1 acceptance check.
Non-goals: any Notion write; judging a row Resolved.
Proof: every verdict cites a file path or states why none can exist; the table's row count equals `approved_groups` "No work" `count`.

## Rock 3 (#9): Progress-vs-label design note (note, no code)
Write `docs/triage-cycle/2026-11-19-rows/OPERATOR-NOTES.md` section "Progress column": state that both halves of issue 9 are already
handled by existing rules (the prompt's STEP 3/4 and `progress_note_kept`; the 10-minute recent-edit skip is enforced by the routine following the
prompt, HARD RULE 6, not by the batch, which has no timestamp input), and recommend one schema change for the operator to accept
or reject: a separate "Derived progress" text column the digest may write, leaving "Progress" for operator notes. Say what it would change
in HARD RULE 1 and the batch's `WRITABLE` list, and that it is deferred until the operator decides.
Non-goals: editing the Notion schema; changing `WRITABLE`.
Proof: the section names the files and rules it cites; `DEFERRED.md` records the column as deferred.

## Rock 4 (#10): paste-ready `output_cap_exceeded` update (note, no code)
In `OPERATOR-NOTES.md` section "output_cap_exceeded row": a drafted replacement for the row's Progress and a one-line Status recommendation
(keep Monitoring), written from verified facts only: 2026-11-05 15:30 recurrence; 2026-11-12 14:30 abort and the same-day baseline chain
(#2246, `docs/adr-incremental-order-history.md`, live-mirror 13:30/14:30/15:30); 2026-11-19 14:30 and 15:30 used `baseline_merge`,
13:30 aborted with no `day_history_mode` although an earlier slot's baseline was usable (PR #2378 `baseline_unused`); cause undetermined
(stale live routine prompt vs skipped rule). Same file also records that `robinhood_payload_rate_limited` and `bridge_python_alert_channels`
carry hand-set PR/Commit cells so their reconciler skip is noise, and the scheduled-task prompt sync still owed from cycle 1.
Non-goals: writing the text to Notion.
Proof: each factual sentence cites a repo path (e.g. `docs/adr-incremental-order-history.md`,
`claude/dca-executor-prompts/live-mirror/1330ET-130-pm-afternoon-buyiing-run.txt`, `docs/triage-cycle/2026-11-19/SAME-PAGE-LOG.md`), a PR as
`https://github.com/badMade/all-stock-dca/pull/<n>`, a run artifact as `<stream>/<YYYYMMDD_HHMMET>/run_state.json` field, or a Notion row Key; anything
that cannot be independently checked from those (for example what the live routine prompt currently says) is written as "unverified".

## Order
Rock 1 and 1b (Codex, workspace-write; Visionary reviews diff and reruns proof), then Rock 2 using Rock 1's output, then 3 and 4.
