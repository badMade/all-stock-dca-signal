# PR #1348's Antigravity review comment was already fixed and merged; a stale-branch checkout briefly reverted a security fix along the way

Written 2026-09-07. Findings only; the fix this note describes was made by an
earlier session directly on PR #1348's own branch and is already on `main`. No
code changes ship with this note.

## What PR #1348 was and what the review asked for

PR #1348 ("Refactor redirect_request to reduce parameters", branch
`jules-4934671427453366726-ad526d00`) collapsed
`SafeRedirectHandler.redirect_request` in `dca_net_utils.py` from an explicit
`(self, req, fp, code, msg, headers, newurl)` signature to `(self, *args)` with
manual tuple unpacking, to dodge a PLR0913 "too many arguments" lint warning.
It also committed several scratch files (`patch.py`, `test_args.py`,
`test_redirect.py`, `test_redirect_args.py`).

The Antigravity AI Code Review comment on the
[PR](https://github.com/badMade/all-stock-dca/pull/1348) flagged three
problems:

1. The `*args` signature breaks keyword callers with a confusing `TypeError`,
   turns an arity mistake into a `ValueError` from the internal unpack instead
   of Python's own `TypeError`, and drifts from the base class's explicit
   `urllib.request.HTTPRedirectHandler.redirect_request` signature the
   override exists to match.
2. The scratch files (`patch.py`, `test_args.py`, `test_redirect.py`,
   `test_redirect_args.py`) didn't belong in the PR.
3. No regression tests were added to `tests/test_dca_net_utils.py`.

## It was already fixed, on the PR's own branch

By the time this session looked at it, all three items were already resolved
in commit
[964d1da8](https://github.com/badMade/all-stock-dca/commit/964d1da8c421f7be07ed44c61afce1513ddb76d6),
pushed directly to PR #1348's branch by an earlier Claude session: the
explicit signature was restored, the scratch files were confirmed absent, and
five regression tests were added to `tests/test_dca_net_utils.py` (signature
parity with the base class, keyword-call and positional-call safety
enforcement, and a missing-argument `TypeError` check). PR #1348 merged into
`main` on 2026-09-07T05:14:12Z with that fix included, so `main` already
carries the corrected, explicit signature.

## What this session found while checking that

Confirming the fix meant fetching PR #1348's branch and comparing it against
`main`. The PR's branch is based on a **much older** `main` -- old enough that
`main`'s diff against it shows over 10,000 lines "removed", including entire
modules (`dca_console.py`, `dca_console_data.py`, ...) that were deleted from
the repo long before this PR was opened. That is exactly the shape of failure
recorded in this session's memory as "stale squash-merges clobber main":
checking out files from a stale branch onto a current one silently reverts
everything `main` gained since the branch point.

That is what happened here, briefly: a working-tree checkout of
`dca_net_utils.py` and `tests/test_dca_net_utils.py` from PR #1348's stale
branch got auto-committed on this session's own scratch branch
(`badMade/pr-1348-issue-43a510`, never pushed to `origin`). Diffed against the
*old* branch tip it looked like a two-file change; diffed against current
`main` it actually deleted `BlockedFileHandler` and `BlockedFTPHandler` --
the handlers added by
[the SSRF fix](https://github.com/badMade/all-stock-dca/commit/c306dd12c632548c6baea4b6467801768acab27) that
block `file://` and `ftp://` URLs at the opener level -- along with six
tests covering them and `resolve_ips` edge cases. Caught before anything was
pushed; the bad commit was reverted with `git revert` (a plain `git reset
--hard` was blocked by this environment's safety classifier for being
destructive, which is the correct call here) and current `main` was merged
back in. The scratch branch was never pushed, so `origin` was never at risk.

## Takeaway for future sessions

When a PR under review turns out to already be fixed and merged, resist the
urge to reconcile a local branch by checking out files from the PR's own
branch -- if that branch is stale relative to `main` (which a `github-actions`-
or `jules`-authored branch left open for days often is), the checkout brings
the staleness with it. Diff a candidate source branch against **current**
`main` before trusting any of its file contents, not just against whatever a
local branch's own tip happens to be.
