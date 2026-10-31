#!/bin/sh
# Run .github/workflows/ci.yml on this Mac with act + Colima (docs/local_ci.md).
# Added 2026-10-31 when a GitHub billing block stopped every hosted Actions job
# from starting: act runs the real workflow file in local Linux containers, so
# there is no second copy of the CI steps to drift, and nothing is billed.
#
#   scripts/run_ci_locally.sh                 # every ci.yml job, as a PR run of this branch
#   scripts/run_ci_locally.sh -j test         # one job (job ids: act -W .github/workflows/ci.yml -l)
#   scripts/run_ci_locally.sh --pr 2066 -j audit
#   scripts/run_ci_locally.sh --push          # as a push to main (both Python versions, every sibling job)
#   scripts/run_ci_locally.sh --report        # also post each job's result to GitHub as a commit status
#
# Any other argument is passed straight to act (e.g. --matrix python-version:3.11).
# Without --report, results print here only; with it, HEAD (which must be pushed and
# clean) gets one "local-ci (act) / <job>" status per job plus an overall one.
set -eu

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

repo_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
image=all-stock-dca/act-runner:24.04
event=pull_request
pr=
report=0
while test "$#" -gt 0; do
    case $1 in
        --pr) test "$#" -ge 2 || { usage >&2; exit 2; }; pr=$2; shift 2 ;;
        --push) event=push; shift ;;
        --report) report=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) break ;;
    esac
done

for tool in act colima docker gh; do
    command -v "$tool" >/dev/null 2>&1 || { echo "missing $tool: brew install act colima docker gh" >&2; exit 1; }
done

# Colima is the container engine act talks to. Start it on demand rather than
# as a login service, so the VM only holds its 16 GB while CI is running.
if ! colima status >/dev/null 2>&1; then
    echo "starting colima..." >&2
    colima start --cpu 8 --memory 16 --disk 100 --vm-type vz --vz-rosetta >&2
fi

# The runner image is act's medium Ubuntu 24.04 image plus the GitHub CLI and a
# `runner` user (deploy/act/Dockerfile). It is labelled with the Dockerfile's
# hash and rebuilt whenever that file changes, so an edit there takes effect.
dockerfile_sha=$(shasum -a 256 "$repo_root/deploy/act/Dockerfile" | cut -d' ' -f1)
built_sha=$(docker image inspect -f '{{ index .Config.Labels "dockerfile-sha" }}' "$image" 2>/dev/null || true)
if test "$built_sha" != "$dockerfile_sha"; then
    echo "building $image..." >&2
    docker build -q --label "dockerfile-sha=$dockerfile_sha" -t "$image" "$repo_root/deploy/act" >&2
fi

# One act run per machine. act names a job's container and volumes after the
# workflow and job only (act-CI-Lint--test-core-DCA-<hash>), not the checkout, so
# two concurrent runs share them and each one's cleanup deletes the other's
# container mid-test: on 2026-10-31 a second session's run killed this one's core
# job with exit 137 ("removal of container ... already in progress") and, before
# that, with "...-env: no such volume". mkdir is atomic; a lock whose pid is gone
# is stale and taken over.
lock=${XDG_CACHE_HOME:-$HOME/.cache}/all-stock-dca/act.lock
mkdir -p "$(dirname "$lock")"
while ! mkdir "$lock" 2>/dev/null; do
    holder=$(cat "$lock/pid" 2>/dev/null || true)
    if test -n "$holder" && ! kill -0 "$holder" 2>/dev/null; then
        echo "removing stale act lock left by pid $holder" >&2
        rm -rf "$lock"
        continue
    fi
    echo "another act run holds $lock (pid ${holder:-?}); waiting..." >&2
    sleep 30
done
echo "$$" > "$lock/pid"

work=$(mktemp -d "${TMPDIR:-/tmp}/act-ci.XXXXXX")
trap 'rm -rf "$work" "$lock"' EXIT
event_file=$work/event.json
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
if test "$event" = pull_request; then
    # The "Detect changed projects" job lists the PR's files through the API, so
    # a PR run needs a real PR number: the one given, else this branch's open PR.
    if test -z "$pr"; then
        pr=$(cd "$repo_root" && gh pr view --json number --jq .number 2>/dev/null) || {
            echo "no open PR for this branch; pass --pr N or --push" >&2
            exit 2
        }
    fi
    printf '{"pull_request":{"number":%s,"base":{"ref":"main"}},"repository":{"full_name":"%s"}}\n' \
        "$pr" "$repo" > "$event_file"
    echo "act: pull_request run for $repo#$pr" >&2
else
    printf '{"ref":"refs/heads/main","repository":{"full_name":"%s"}}\n' "$repo" > "$event_file"
    echo "act: push run" >&2
fi

# --container-daemon-socket -: act otherwise bind-mounts the Mac's Docker socket
# into every job, which fails under Colima ("mkdir .../docker.sock: operation not
# supported") and none of these jobs runs containers. The token comes from gh's
# own login and is handed to act as a secret, never printed.
# --concurrent-jobs 1: act shares one /opt/hostedtoolcache volume across jobs, so
# on the first run (2026-10-31) the audit job's `pip install --upgrade pip` swapped
# out Python 3.12's pip while the 3.12 research-audit leg was importing it
# (FileNotFoundError .../pip/_vendor/rich/style.py). GitHub gives each job its own
# VM; one job at a time is the local equivalent.
#
# act copies the directory it runs in into the job. A linked worktree's `.git` is
# a file naming a host path the container cannot see, so every git call there
# exited 128 and from.py looked for the main checkout's .venv: 14 of 17 local
# failures on 2026-10-31. GitHub checks out a plain clone, so do the same: clone
# this checkout's HEAD into $work/repo, then lay its uncommitted and untracked
# (not ignored) files on top, so "runs your working tree" still holds.
snapshot=$work/repo
head=$(git -C "$repo_root" rev-parse HEAD)
if test "$report" -eq 1; then
    # A commit status is a claim about one commit, so it may only describe that
    # commit exactly: refuse uncommitted or untracked files, and a HEAD GitHub has
    # never seen (the statuses API rejects it with 422 after a 16-minute run).
    if test -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)"; then
        echo "--report needs a clean tree: commit or stash first, so the status describes $head" >&2
        exit 2
    fi
    gh api "repos/$repo/commits/$head" --silent 2>/dev/null || {
        echo "--report needs HEAD on GitHub: push $head first" >&2
        exit 2
    }
fi
git clone -q "$repo_root" "$snapshot"
git -C "$snapshot" fetch -q "$repo_root" "$head"
git -C "$snapshot" checkout -q --detach "$head"
git -C "$snapshot" remote set-url origin "$(git -C "$repo_root" remote get-url origin)"
git -C "$repo_root" diff --binary HEAD > "$work/uncommitted.patch"
if test -s "$work/uncommitted.patch"; then
    git -C "$snapshot" apply --whitespace=nowarn "$work/uncommitted.patch"
fi
(cd "$repo_root" && git ls-files -z --others --exclude-standard | tar --null -T - -cf -) | tar -C "$snapshot" -xf -
#
# --user 1001:1001 runs steps as the image's `runner` user, as GitHub does; as
# root, read-only-directory tests could not fail the way they are meant to.
# --rm removes a failed job's container instead of leaving it running.
cd "$snapshot"
GITHUB_TOKEN=$(gh auth token)
export GITHUB_TOKEN
# Not exec: the EXIT trap above must still remove the snapshot.
host=$(hostname -s)
post_status() {
    # $1 state, $2 context suffix ("" for the overall status), $3 description.
    context="local-ci (act)${2:+ / $2}"
    gh api -X POST "repos/$repo/statuses/$head" --silent \
        -f state="$1" -f context="$context" -f description="$(printf '%s' "$3" | cut -c1-140)" ||
        echo "could not post status '$context' (run results are unaffected)" >&2
}
if test "$report" -eq 1; then
    post_status pending "" "running ci.yml with act on $host (linux/arm64), started $(date '+%Y-%m-%d %H:%M %Z')"
fi
status=0
{
act "$event" \
    -W .github/workflows/ci.yml \
    -e "$event_file" \
    -P ubuntu-26.04="$image" \
    --container-architecture linux/arm64 \
    --container-daemon-socket - \
    --pull=false \
    --concurrent-jobs 1 \
    --container-options "--user 1001:1001" \
    --rm \
    -s GITHUB_TOKEN \
    "$@" || echo "$?" > "$work/act.status"
} 2>&1 | tee "$work/act.log"
test ! -s "$work/act.status" || status=$(cat "$work/act.status")

if test "$report" -eq 1; then
    # act ends every job with "[CI/<job name>] 🏁  Job succeeded|failed", the name
    # padded with spaces and a matrix leg suffixed -1, -2 in matrix order.
    passed=0
    failed=0
    sed -n -E 's/^\[CI\/(.*)\] 🏁  Job (succeeded|failed)$/\2|\1/p' "$work/act.log" > "$work/jobs.txt"
    while IFS='|' read -r outcome name; do
        name=$(printf '%s' "$name" | sed 's/[[:space:]]*$//')
        if test "$outcome" = succeeded; then
            passed=$((passed + 1))
            post_status success "$name" "passed locally with act on $host (linux/arm64)"
        else
            failed=$((failed + 1))
            post_status failure "$name" "failed locally with act on $host (linux/arm64); see the operator's run log"
        fi
    done < "$work/jobs.txt"
    if test "$status" -eq 0 && test "$failed" -eq 0 && test "$passed" -gt 0; then
        post_status success "" "$passed job(s) passed locally with act on $host (linux/arm64)"
    else
        post_status failure "" "$failed of $((passed + failed)) job(s) failed locally (act exit $status) on $host"
    fi
    echo "reported $passed passed, $failed failed to $repo@$head" >&2
fi
exit "$status"
