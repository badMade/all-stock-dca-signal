# The PR review workflow: trust boundary and migration note

Two workflow files implement the automated Antigravity PR review:

* **`.github/workflows/antigravity-pr-review.yml`** ("Antigravity PR Review
  (Analyze)") -- unprivileged, triggered on `pull_request`. Checks out the PR
  head, computes the diff against the base branch, and uploads it as an
  artifact along with the PR number and head SHA. Holds no secret, no
  `pull-requests` permission, nothing beyond `contents: read`.
* **`.github/workflows/antigravity-pr-review-post.yml`** ("Antigravity PR
  Review (Post)") -- privileged, triggered on `workflow_run` when the analyze
  workflow completes. Holds `GEMINI_API_KEY` and `pull-requests: write`.
  Validates that the triggering run is genuinely what it claims to be, then
  downloads the diff artifact, generates the review, and posts the comment.

## Why two workflows

A `pull_request`-triggered workflow's own YAML is taken from the **PR's head
branch**. A contributor who can push a branch can edit that workflow file in
their PR, and the edit takes effect in their own run -- including deleting a
safety step, or replacing the command a later step runs. This repo's earlier
single-workflow design tried to compensate for that by restoring
`.github/scripts/` from the base branch immediately before running the
reviewer script, and hardened that restore step across several review rounds
(removing added files, not just overlaying changed ones; failing closed when
the base branch has nothing to restore; `set -euo pipefail`). Each of those
fixes was correct and necessary, but all of them were mitigating symptoms of
the same root cause: the workflow doing the mitigating was itself editable by
the thing it was defending against.

`workflow_run` does not have that problem. GitHub always runs the version of
a `workflow_run`-triggered workflow's YAML that lives on the repository's
**default branch**, regardless of what the triggering run's branch contains.
A PR cannot edit `antigravity-pr-review-post.yml` and have that edit apply to
its own review -- an edit there only takes effect once merged. That is a
structural property of the trigger, not a runtime check that could be
bypassed by removing the check.

## What the privileged workflow still has to defend against

Moving the secret to a `workflow_run` job does not, by itself, make it safe --
it just changes what has to be verified:

1. **The job must never check out PR-head code.** `workflow_run` fixes *which
   version of the workflow file runs*, but a checkout step inside that file
   could still be pointed at the PR's branch via `ref:`, which would check out
   attacker-influenced code into a job holding the API key. The `review`
   job's checkout step has no `ref:` at all -- omitting it checks out
   `github.sha`, which for a `workflow_run` event is the default branch's
   current commit. The PR still reaches this job, but only as the diff
   **text**, downloaded as an artifact, never as code that gets executed.
2. **The triggering run's identity must be validated, not assumed.** The
   `workflows:` filter on the trigger restricts by name at GitHub's level, but
   the job re-validates explicitly: the triggering workflow's name, that its
   event was `pull_request`, that it succeeded, that its head repository is
   this repository (not a fork), and that it has an associated PR. Any
   failure here is logged per-check and fails the job (`validate-provenance`
   in the workflow file).
3. **A downloaded artifact must be checked against what was validated.**
   `actions/download-artifact` fetches by run ID; the job cross-checks the
   artifact's own recorded PR number and head SHA against the validated
   values before trusting its contents, so an artifact from the wrong run
   (or a malformed one) is caught rather than silently used.

`scripts/tests/test_pr_review_workflow_trust_boundary.py` encodes all three of
these as structural assertions against the workflow YAML -- not just that the
right steps exist, but that they are wired in the order and with the
dependencies that make the properties above actually hold. It fails loudly,
naming the specific thing that drifted, if any of them regress.

## Bootstrapping note

`workflow_run` only fires for a consumer workflow that already exists on the
default branch. Until the two-workflow split itself is merged, the privileged
post-workflow will not run for PRs against this branch -- the analyze
workflow still runs and uploads artifacts, but nothing consumes them yet. This
is expected, not a regression: the same PR that introduces this split cannot
receive a privileged review of itself.

## Visible changes

* The GitHub Actions check name changes from "Antigravity PR Reviewer /
  review" to "Antigravity PR Review (Analyze) / analyze" (plus, once merged,
  "Antigravity PR Review (Post) / validate-provenance" and "/ review" on a
  short delay after the analyze check completes).
* The posted review comment's format, the `<!-- antigravity-review:complete:
  <sha> -->` dedup marker, and the skip-note behavior for rate limits/errors
  are all unchanged.

## Workflow policy checks

`scripts/workflow_policy.py` statically enforces three org-wide policies
against every file in `.github/workflows/*.yml`, run as ordinary pytest tests
(`scripts/tests/test_workflow_policy.py`) rather than a separate CI job:

* every job sets `timeout-minutes`;
* every workflow (or job) declares a `concurrency` block with a `group` and
  an explicit `cancel-in-progress`;
* every `actions/checkout` step sets `persist-credentials: false`.

A workflow file can declare an explicit, reasoned exception via a top-level
`x-policy-exceptions` key (GitHub Actions ignores unrecognized top-level keys,
the same mechanism already used for YAML anchor blocks in shared workflows):

```yaml
x-policy-exceptions:
  timeout-minutes:
    - job: some-job
      reason: "why this job is exempt"
  persist-credentials:
    - job: some-job
      step: "Some step name"   # optional: omit to exempt the whole job
      reason: "why this checkout is exempt"
```

An exception with no `reason` is rejected at parse time rather than silently
accepted. No workflow in this repository currently declares one; if that
changes, `test_no_real_workflow_declares_a_policy_exception` in the test file
above will fail until the test is updated to name and justify it explicitly,
so an exception cannot accumulate unnoticed.
