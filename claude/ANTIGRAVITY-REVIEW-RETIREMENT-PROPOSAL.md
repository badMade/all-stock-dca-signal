# Proposal: retire the Antigravity (Gemini) PR review

**Status:** proposal for the operator. It changes `.github/workflows/`, which agent-driven pull requests must not
edit (`AGENTS.md`, "Pull request expectations"), so the change is delivered as a proposal for the operator to apply,
not as a PR diff. The change itself is complete: as a PR it was green in CI (both core DCA jobs, the audits and the
sibling suites) before this PR was converted to a proposal. It stays in this branch's history at commit `000eb68`.

## Why

The review runs `antigravity-pr-review.yml` (analyze) and `antigravity-pr-review-post.yml` (post), which call the
Gemini API with the `GEMINI_API_KEY` secret. On 2026-10-11 it posted no real review on any PR:

| Observed | Cause |
| --- | --- |
| `Error 402 ... prepayment credits are depleted` | The key's project was on prepaid billing with no credit. |
| After the key was replaced: `RESOURCE_EXHAUSTED ... GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit 20 (model `gemini-3.8-flash`, retry in about 6.5 hours) | The free tier allows 20 requests per project per model per day. One review spends several (retries and agent turns), and this repo opens many PRs, so reviews become silent skips. |
| `No diff to review; nothing to post.` | Not a fault: a PR whose diff against its base is empty. |

Both failures leave the run green: `pr_review.py` fails open by design and the post step writes one "skipped" note per
PR, which says "rate-limited (HTTP 429)" even for a billing error. Copilot and `claude-code-review.yml` still review
PRs. The alternative to retiring it is to enable billing on the key's project.

## What the patch changes (14 files)

* Deletes `.github/workflows/antigravity-pr-review.yml`, `.github/workflows/antigravity-pr-review-post.yml`,
  `.github/scripts/pr_review.py`, `requirements-pr-review.txt`, `scripts/tests/test_pr_review_script.py` and
  `scripts/tests/test_pr_review_workflow_trust_boundary.py`.
* `ci.yml`: the dependency audit no longer resolves `requirements-pr-review.txt`; the step is renamed.
* `scripts/tests/test_workflow_policy.py`: drops the one test that pins the reviewer SDK. The three policy checks stay.
* Docs: the still-relevant workflow-policy section moves to `docs/workflow_policy.md`; `docs/pr_review_workflow.md`
  is deleted; `CLAUDE.md` and `docs/README.md` point at the new page. Comments in `claude-code-review.yml` and
  `drift-check.yml` stop citing the removed workflows.

Left alone on purpose: historical code comments that cite past "Antigravity review" findings, and `rocket-fuel/`.

## How to apply

Apply it as **one change**. Partial application breaks CI: `ci.yml` compiles `requirements-pr-review.txt`, the post
workflow needs `pr_review.py`, and two tests assert on the workflow files.

The complete change is the diff of 14 paths between `main` and commit `000eb68` (the tip of PR #1638 before it became
this proposal; GitHub keeps it under `refs/pull/1638/head` even if the branch is deleted). `*.patch` files are
gitignored here, so generate it instead of committing it:

```bash
git fetch origin pull/1638/head          # makes 000eb68 available locally
git switch -c retire-antigravity-review origin/main
git diff origin/main 000eb68 -- \
  .github/scripts/pr_review.py .github/workflows/antigravity-pr-review-post.yml \
  .github/workflows/antigravity-pr-review.yml .github/workflows/ci.yml \
  .github/workflows/claude-code-review.yml .github/workflows/drift-check.yml \
  CLAUDE.md docs/README.md docs/pr_review_workflow.md docs/workflow_policy.md \
  requirements-pr-review.txt scripts/tests/test_pr_review_script.py \
  scripts/tests/test_pr_review_workflow_trust_boundary.py scripts/tests/test_workflow_policy.py \
  | git apply --3way
ruff check . && python -m pytest scripts/tests/test_workflow_policy.py -q
```

Tested on `main` at `7bad56b` (applies cleanly) and on the #1641 branch `revert-1635-restore-lost-prs` (clean with
`--3way`; its `docs/README.md` differs). If `main` has moved, expect at most a one-row conflict in `CLAUDE.md` or
`docs/README.md`. Do not use `origin/main..000eb68` for the whole tree: `000eb68` also contains an older merge of `main`,
which is why the diff is limited to the 14 paths above.

After merging, delete the `GEMINI_API_KEY` repo secret if nothing else uses it (`llm-providers/` reads that name from
the environment only, not from repo secrets).

## Rollback

Revert the merge commit. Nothing outside CI changes: no runtime code is touched.
