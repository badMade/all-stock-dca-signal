#!/usr/bin/env bash
# Run scripts/local_ci.sh and publish its real result to GitHub as a commit status
# on the exact commit it tested, so a PR shows a green/red `local-ci` check while
# GitHub-hosted Actions are unavailable or deliberately skipped (draft PRs since
# #2067; the 2026-10-31 billing block). See docs/actions_minutes.md.
#
# Usage:
#   scripts/local_ci_status.sh [local_ci.sh flags...]     # e.g. --all, --quick, --changed
#
# Every argument is passed straight through to scripts/local_ci.sh, so this works
# with whatever flags that script currently takes. The status is posted as:
#   pending  when the run starts,
#   success  only when local_ci.sh exits 0,
#   failure  when it exits non-zero,
#   error    when the run is interrupted.
# The context is `local-ci` ($LOCAL_CI_CONTEXT overrides). It is an ordinary commit
# status, not an Actions check run, and no ruleset requires it today.
#
# Refuses to run (exit 2) on a dirty tree, because the status would then describe
# code that is not the commit it is attached to, or on a commit GitHub does not
# have yet (push first). Test hooks: $LOCAL_CI_SCRIPT replaces the local_ci.sh
# path and $GH replaces the gh binary.
set -uo pipefail

cd "$(dirname "$0")/.."
GH="${GH:-gh}"
CONTEXT="${LOCAL_CI_CONTEXT:-local-ci}"
LOCAL_CI="${LOCAL_CI_SCRIPT:-scripts/local_ci.sh}"

if [ -n "$(git status --porcelain)" ]; then
  echo "local_ci_status: the working tree is dirty; commit or stash first so the status matches the commit." >&2
  git status --short >&2
  exit 2
fi
sha="$(git rev-parse HEAD)"
repo="${LOCAL_CI_REPO:-$("$GH" repo view --json nameWithOwner -q .nameWithOwner)}" || {
  echo "local_ci_status: could not resolve the GitHub repository (gh auth?)" >&2; exit 2; }
if ! "$GH" api "repos/$repo/commits/$sha" --silent >/dev/null 2>&1; then
  echo "local_ci_status: $repo does not have commit $sha yet; push it first." >&2
  exit 2
fi

mode="${*:-default}"
host="$(hostname -s 2>/dev/null || echo local)"

post() {  # $1 = state, $2 = description (GitHub truncates at 140 chars)
  "$GH" api "repos/$repo/statuses/$sha" --silent \
    -f state="$1" -f context="$CONTEXT" -f description="${2:0:140}" \
    || echo "local_ci_status: failed to post the '$1' status" >&2
}

post pending "local_ci.sh $mode: running on $host"
interrupted() { post error "local_ci.sh $mode: interrupted on $host"; exit 130; }
trap interrupted INT TERM

start=$(date +%s)
"$LOCAL_CI" "$@"
rc=$?
trap - INT TERM
mins=$(( ($(date +%s) - start + 59) / 60 ))

if [ "$rc" -eq 0 ]; then
  post success "local_ci.sh $mode passed in ${mins}m on $host ($(uname -s))"
else
  post failure "local_ci.sh $mode failed (exit $rc) after ${mins}m on $host ($(uname -s))"
fi
echo "local_ci_status: posted $([ "$rc" -eq 0 ] && echo success || echo failure) for $CONTEXT on ${sha:0:8} ($repo)"
exit "$rc"
