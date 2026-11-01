#!/usr/bin/env bash
# Run the CI checks locally so a push does not spend GitHub Actions minutes on a
# failure that a laptop would have caught -- and, since 2026-10-31, so a PR can still
# be checked when Actions will not start at all (that day every job was refused before
# it ran: "recent account payments have failed or your spending limit needs to be
# increased"). Mirrors .github/workflows/ci.yml job for job:
#
#   core (default)      Lint & test (core DCA): ruff -> coverage-gated pytest -> dry-run demo,
#                       plus scripts/tests (no CI job of its own runs that suite; CLAUDE.md lists it)
#   --audit             Dependency & secret audit: pip-audit on the pinned dev tooling
#   --research-audit    Dependency audit (research): uv export of uv.lock + every research
#                       manifest resolved without building, then pip-audit --locked
#   <project> ...       Lint & test (<project>), in its own venv built the way its CI job
#                       installs it (CI gives each sibling a fresh runner; sharing .venv would
#                       test against whatever the core venv happens to hold)
#
# Usage:
#   scripts/local_ci.sh                       # core checks
#   scripts/local_ci.sh --quick               # skip the full coverage run; lint + demo + scripts/tests
#   scripts/local_ci.sh ticker-advisor ...    # also lint+test the named sibling projects
#   scripts/local_ci.sh --changed             # also the siblings CI would run for this branch vs origin/main
#   scripts/local_ci.sh --changed=REF         # ...diffed against REF instead
#   scripts/local_ci.sh --py311               # also run the core tests on 3.11 (main's second matrix leg)
#   scripts/local_ci.sh --audit               # also run pip-audit (needs network)
#   scripts/local_ci.sh --research-audit      # also the research dependency audit (needs network)
#   scripts/local_ci.sh --all                 # everything CI can run: core, both audits, every sibling
#   scripts/local_ci.sh --keep-going ...      # run every step and report all failures (default: stop at the first)
#   scripts/local_ci.sh --dry-run ...         # print the plan, run nothing
#   scripts/local_ci.sh --pr 2071 ...         # also post the pass/fail summary as a comment on that PR
#
# What this cannot reproduce: CI's Python matrix (core runs 3.11 and 3.12 on main) and its
# Ubuntu runner. This runs core on the local .venv interpreter only, and each sibling on the
# version its CI job pins (uv fetches it if it is missing). A local pass is evidence, not the
# required check; the summary says which interpreter and OS it ran on.
set -euo pipefail

cd "$(dirname "$0")/.."
main_checkout="$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in .venv/bin/python "$main_checkout/.venv/bin/python"; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
fi
# A git worktree (.claude/worktrees/*) has no .venv of its own, and a bare `python3` fallback
# picked up Homebrew's 3.14 with no pytest/ruff, so every worktree run died with "No module
# named ruff" (2026-10-31). Stop with the fix instead. (--dry-run and --help only print, so they skip this.)
if [[ " $* " != *" --dry-run "* && " $* " != *" --help "* && " $* " != *" -h "* ]] && { [ -z "$PY" ] || ! "$PY" -c 'import pytest, ruff, pytest_cov' 2>/dev/null; }; then
  echo "local_ci: no interpreter with the dev tooling (tried \$PYTHON, ./.venv, $main_checkout/.venv)." >&2
  echo "  Fix: python3.12 -m venv $main_checkout/.venv && $main_checkout/.venv/bin/pip install -r requirements-dev.txt" >&2
  exit 2
fi
[ -n "$PY" ] || PY="$(command -v python3)"

# Every sibling job in ci.yml, in its order there. tests/test_local_ci.py fails if this list
# and ci.yml's `changes` job drift apart, so a new sibling job cannot silently go unrun here.
SIBLINGS="production-safe-agent-loop ticker-advisor ml-forecaster wondrous-octopus ticker-market-breadth llm-providers yfinance-prefetch"
# Sibling venvs live OUTSIDE the checkout: pytest's testpaths is "." (it would collect every
# test_*.py in their site-packages), ruff would lint them, and Google Drive mirrors ~/Documents,
# so a CPU-torch venv in the tree would sync gigabytes. Override with DCA_LOCAL_CI_VENVS.
VENVS="${DCA_LOCAL_CI_VENVS:-$HOME/.cache/all-stock-dca/ci-venvs}"

quick=0
audit=0
py311=0
research_audit=0
keep_going=0
dry_run=0
changed_ref=""
pr=""
siblings=()
expect_pr=0
for arg in "$@"; do
  if [ "$expect_pr" -eq 1 ]; then pr="$arg"; expect_pr=0; continue; fi
  case "$arg" in
    --quick) quick=1 ;;
    --audit) audit=1 ;;
    --py311) py311=1 ;;
    --research-audit) research_audit=1 ;;
    --keep-going) keep_going=1 ;;
    --dry-run) dry_run=1 ;;
    --changed) changed_ref="origin/main" ;;
    --changed=*) changed_ref="${arg#--changed=}" ;;
    --pr) expect_pr=1 ;;
    --pr=*) pr="${arg#--pr=}" ;;
    --all)
      audit=1
      py311=1
      research_audit=1
      # word-splitting SIBLINGS is the point here
      # shellcheck disable=SC2206
      siblings+=($SIBLINGS)
      ;;
    -h|--help) sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "local_ci: unknown option $arg" >&2; exit 2 ;;
    *)
      case " $SIBLINGS " in
        *" $arg "*) siblings+=("$arg") ;;
        *) echo "local_ci: $arg is not a CI sibling project ($SIBLINGS)" >&2; exit 2 ;;
      esac
      ;;
  esac
done
[ "$expect_pr" -eq 0 ] || { echo "local_ci: --pr needs a PR number" >&2; exit 2; }
if [ -n "$pr" ] && ! [[ "$pr" =~ ^[0-9]+$ ]]; then echo "local_ci: --pr must be a number" >&2; exit 2; fi

# --changed: the same rule as ci.yml's `changes` job -- a sibling runs when a file under it
# changed, and every sibling runs when requirements-dev.txt or ci.yml itself changed (the
# shared inputs each sibling job installs from). Diffed from the merge base, like a PR's file
# list, plus uncommitted and untracked files so a pre-push run sees what is about to land.
if [ -n "$changed_ref" ]; then
  base="$(git merge-base "$changed_ref" HEAD)"
  files="$(git diff --name-only "$base" HEAD; git diff --name-only HEAD; git ls-files --others --exclude-standard)"
  for p in $SIBLINGS; do
    if printf '%s\n' "$files" | grep -Eq '^(requirements-dev\.txt|\.github/workflows/ci\.yml)$' \
      || printf '%s\n' "$files" | grep -q "^$p/"; then
      siblings+=("$p")
    fi
  done
fi

# De-duplicate siblings named more than once (e.g. `--changed ticker-advisor`), keeping CI order.
ordered=()
for p in $SIBLINGS; do
  for s in "${siblings[@]+"${siblings[@]}"}"; do
    if [ "$s" = "$p" ]; then ordered+=("$p"); break; fi
  done
done

results=()
failed=0
step() { printf '\n==> %s\n' "$*"; }

finish() {
  local sha env line status name body
  sha="$(git rev-parse --short HEAD)"
  env="$("$PY" -V 2>&1) on $(uname -s) $(uname -m)"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || env="$env, uncommitted changes present"
  printf '\n==> summary (%s, %s)\n' "$sha" "$env"
  body="Local CI (\`scripts/local_ci.sh\`) at \`$sha\`, $env:"$'\n\n'
  for line in "${results[@]+"${results[@]}"}"; do
    status="${line%%|*}"; name="${line#*|}"
    printf '  %-4s %s\n' "$status" "$name"
    body+="- **$status** $name"$'\n'
  done
  if [ -n "$pr" ] && [ "$dry_run" -eq 0 ]; then
    body+=$'\n'"Run locally instead of GitHub Actions; a local pass is evidence, not the required check."
    gh pr comment "$pr" --body "$body" >/dev/null && printf '\nPosted the summary on PR #%s.\n' "$pr"
  fi
  if [ "$failed" -ne 0 ]; then
    printf '\nLocal CI FAILED.\n'
    exit 1
  fi
  if [ "$dry_run" -eq 1 ]; then printf '\nDry run: nothing was executed.\n'; else printf '\nAll local CI checks passed.\n'; fi
  exit 0
}

# run_step NAME CMD...: runs CMD, records NAME's result, and stops the script on failure
# unless --keep-going. Under --dry-run it only prints what it would run. The `if` keeps a
# failing CMD from tripping `set -e` before the result is recorded.
run_step() {
  local name="$1"; shift
  step "$name"
  if [ "$dry_run" -eq 1 ]; then
    printf '    %s\n' "$*"
    results+=("SKIP|$name")
    return 0
  fi
  local started=$SECONDS
  if "$@"; then
    results+=("PASS|$name ($((SECONDS - started))s)")
  else
    results+=("FAIL|$name ($((SECONDS - started))s)")
    failed=1
    if [ "$keep_going" -eq 0 ]; then finish; fi
  fi
}

# Python version each sibling's CI job pins.
sibling_python() {
  case "$1" in
    ticker-market-breadth|llm-providers|yfinance-prefetch) echo 3.12 ;;
    *) echo 3.11 ;;
  esac
}

# Install a sibling's venv the way its CI job does. ticker-advisor installs a hand-picked
# lightweight set, not its requirements.txt (its heavy vectorbt/FinBERT tests importorskip
# away in CI too); wondrous-octopus takes the CPU torch wheel first so the unpinned `torch`
# in its requirements.txt never pulls the CUDA bundle.
install_sibling() {
  local proj="$1" venv="$VENVS/$1"
  local vpy="$venv/bin/python"
  uv venv --quiet --python "$(sibling_python "$proj")" "$venv" || return 1
  uv pip install --quiet --python "$vpy" -r requirements-dev.txt || return 1
  case "$proj" in
    ticker-advisor) uv pip install --quiet --python "$vpy" pandas numpy scipy requests pandas_market_calendars ;;
    wondrous-octopus)
      uv pip install --quiet --python "$vpy" torch --index-url https://download.pytorch.org/whl/cpu \
        && uv pip install --quiet --python "$vpy" -r "$proj/requirements.txt"
      ;;
    *) uv pip install --quiet --python "$vpy" -r "$proj/requirements.txt" ;;
  esac
}

# Rebuild a sibling's venv only when its install inputs change, so a second run costs seconds.
ensure_sibling_venv() {
  local proj="$1" venv="$VENVS/$1"
  local stamp
  stamp="$(cat requirements-dev.txt "$proj/requirements.txt" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)-$(sibling_python "$proj")"
  if [ -x "$venv/bin/python" ] && [ "$(cat "$venv/.local_ci_stamp" 2>/dev/null)" = "$stamp" ]; then
    echo "    reusing $venv"
    return 0
  fi
  rm -rf "$venv"
  install_sibling "$proj" && printf '%s' "$stamp" > "$venv/.local_ci_stamp"
}

test_sibling() {
  local proj="$1"
  local vpy
  vpy="$VENVS/$proj/bin/python"
  "$vpy" -m ruff check --isolated --select=E,F,B --line-length=120 "$proj" \
    && (cd "$proj" && "$vpy" -m pytest tests/)
}

demo_smoke() {
  local demo_dir status=0
  demo_dir="$(mktemp -d)"
  # The demo scans the parent of the scheduled_runs/ segment for prior same-day run_state files, recursively.
  # With a bare mktemp dir that parent was the shared $TMPDIR, so it read the pytest folders left by the run
  # just before (UnicodeDecodeError tracebacks, 2026-10-31). Nesting under scheduled_runs/ keeps the scan private.
  "$PY" run_dca.py --demo --report --now 2026-07-06T15:45 --artifacts-dir "$demo_dir/scheduled_runs/demo" || status=$?
  rm -rf "$demo_dir"
  return $status
}

# ci.yml's research-audit step, minus $RUNNER_TEMP and the matrix: the root graph exported
# from uv.lock plus every independent manifest resolved for the local interpreter's version.
research_audit_step() {
  local audit_dir pyver manifest scope status=0
  audit_dir="$(mktemp -d)"
  pyver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  uv export --locked --all-extras --no-emit-project --format pylock.toml \
    --output-file "$audit_dir/pylock.root.toml" || status=$?
  for manifest in \
    production-safe-agent-loop/requirements.txt \
    ticker-advisor/requirements.txt \
    ml-forecaster/requirements.txt \
    wondrous-octopus/requirements.txt \
    ticker-market-breadth/requirements.txt \
    llm-providers/requirements.txt \
    yfinance-prefetch/requirements.txt \
    trading-strategies/requirements.txt \
    analysis/market-graph-3d/requirements.txt; do
    [ "$status" -eq 0 ] || break
    scope="${manifest%/requirements.txt}"
    uv pip compile "$manifest" --quiet --python-version "$pyver" --no-build --format pylock.toml \
      --output-file "$audit_dir/pylock.${scope//\//-}.toml" || status=$?
  done
  [ "$status" -ne 0 ] || "$PY" -m pip_audit --locked "$audit_dir" --strict || status=$?
  rm -rf "$audit_dir"
  return $status
}

run_step "ruff check ." "$PY" -m ruff check .

if [ "$quick" -eq 0 ]; then
  run_step "pytest (coverage gate, same as CI)" \
    "$PY" -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90
fi

if [ "$py311" -eq 1 ]; then
  # main's second matrix leg; a throwaway uv env so it never touches the shared .venv.
  run_step "pytest on Python 3.11 (main's second matrix leg)" \
    uv run --no-project --quiet --python 3.11 --with-requirements requirements-dev.txt python -m pytest
fi

run_step "scripts/tests" "$PY" -m pytest scripts/tests -q

run_step "dry-run demo smoke test" demo_smoke

if [ "$audit" -eq 1 ]; then
  run_step "pip-audit (pinned dev deps)" "$PY" -m pip_audit --requirement requirements-dev.txt --strict
fi

if [ "$research_audit" -eq 1 ]; then
  run_step "pip-audit (research dependency trees)" research_audit_step
fi

for proj in "${ordered[@]+"${ordered[@]}"}"; do
  run_step "install: $proj (Python $(sibling_python "$proj"))" ensure_sibling_venv "$proj"
  run_step "ruff + pytest: $proj" test_sibling "$proj"
done

finish
