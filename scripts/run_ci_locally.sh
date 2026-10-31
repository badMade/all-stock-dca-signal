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
#
# Any other argument is passed straight to act (e.g. --matrix python-version:3.11).
# Results print here; nothing is reported back to the PR's checks.
set -eu

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

repo_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
image=all-stock-dca/act-runner:24.04
event=pull_request
pr=
while test "$#" -gt 0; do
    case $1 in
        --pr) test "$#" -ge 2 || { usage >&2; exit 2; }; pr=$2; shift 2 ;;
        --push) event=push; shift ;;
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

# The runner image is act's medium Ubuntu 24.04 image plus the GitHub CLI
# (deploy/act/Dockerfile). Rebuilt only when missing; `docker rmi` it to refresh.
if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "building $image..." >&2
    docker build -q -t "$image" "$repo_root/deploy/act" >&2
fi

event_file=$(mktemp "${TMPDIR:-/tmp}/act-event.XXXXXX")
trap 'rm -f "$event_file"' EXIT
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
cd "$repo_root"
GITHUB_TOKEN=$(gh auth token)
export GITHUB_TOKEN
# Not exec: the EXIT trap above must still remove the event file.
status=0
act "$event" \
    -W .github/workflows/ci.yml \
    -e "$event_file" \
    -P ubuntu-26.04="$image" \
    --container-architecture linux/arm64 \
    --container-daemon-socket - \
    --pull=false \
    --concurrent-jobs 1 \
    -s GITHUB_TOKEN \
    "$@" || status=$?
exit "$status"
