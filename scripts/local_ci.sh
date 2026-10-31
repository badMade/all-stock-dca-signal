#!/usr/bin/env bash
# Run the CI checks locally so a push does not spend GitHub Actions minutes on a
# failure that a laptop would have caught. Mirrors .github/workflows/ci.yml job
# for job: core lint -> coverage-gated tests -> dry-run demo (+ the scripts/ suite,
# which CI's root sweep does not collect), the dev-dependency audit, the research
# dependency audit, and the seven sibling projects.
#
# Usage:
#   scripts/local_ci.sh                       # core checks on the shared .venv (3.12)
#   scripts/local_ci.sh --quick               # skip the full coverage run; lint + demo + scripts/tests
#   scripts/local_ci.sh --changed             # also the siblings this branch touches vs origin/main (CI's rule)
#   scripts/local_ci.sh ticker-advisor ...    # also lint+test the named sibling projects
#   scripts/local_ci.sh --py311               # also run the core tests on 3.11 (main's second matrix leg)
#   scripts/local_ci.sh --audit               # also run both pip-audit jobs (needs network)
#   scripts/local_ci.sh --all                 # everything a push to main runs: --py311 --audit + every sibling
#
# Interpreter: $PYTHON, else ./.venv, else the MAIN checkout's .venv. A git worktree
# (.claude/worktrees/*) has no .venv of its own, and the old fallback to a bare
# `python3` picked up Homebrew's 3.14 with no pytest/ruff, so every worktree run died
# with "No module named ruff" (2026-10-31). It now stops with the fix instead.
# The 3.11 leg and the siblings run in uv-managed throwaway envs (cached under
# ~/.cache/uv), so they are isolated the way CI's fresh runners are and never
# install the siblings' heavy deps into the shared .venv.
set -euo pipefail

cd "$(dirname "$0")/.."

main_checkout="$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in .venv/bin/python "$main_checkout/.venv/bin/python"; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
fi
if [ -z "$PY" ] || ! "$PY" -c 'import pytest, ruff, pytest_cov' 2>/dev/null; then
  echo "local_ci: no interpreter with the dev tooling (tried \$PYTHON, ./.venv, $main_checkout/.venv)." >&2
  echo "  Fix: python3.12 -m venv $main_checkout/.venv && $main_checkout/.venv/bin/pip install -r requirements-dev.txt" >&2
  exit 2
fi

SIBLINGS=(production-safe-agent-loop ticker-advisor ml-forecaster wondrous-octopus ticker-market-breadth llm-providers yfinance-prefetch)

quick=0
audit=0
py311=0
changed=0
siblings=()
for arg in "$@"; do
  case "$arg" in
    --quick) quick=1 ;;
    --audit) audit=1 ;;
    --py311) py311=1 ;;
    --changed) changed=1 ;;
    --all) audit=1; py311=1; siblings=("${SIBLINGS[@]}") ;;
    -*) echo "local_ci: unknown option $arg" >&2; exit 2 ;;
    *) siblings+=("$arg") ;;
  esac
done

# CI's `changes` job: a sibling runs when the PR touches its tree, and every sibling
# runs when requirements-dev.txt or ci.yml changes (the inputs they all install from).
if [ "$changed" -eq 1 ]; then
  files="$(git diff --name-only "$(git merge-base origin/main HEAD)" HEAD; git diff --name-only HEAD)"
  if echo "$files" | grep -Eq '^(requirements-dev\.txt|\.github/workflows/ci\.yml)$'; then
    siblings=("${SIBLINGS[@]}")
  else
    for p in "${SIBLINGS[@]}"; do
      echo "$files" | grep -q "^$p/" && siblings+=("$p")
    done
  fi
fi

step() { printf '\n==> %s\n' "$*"; }
UV=("$PY" -m uv)
# One throwaway env per (python, requirement set), reused from uv's cache on later runs.
uvrun() { local pyver="$1"; shift; "${UV[@]}" run --no-project --quiet --python "$pyver" "$@"; }

step "ruff check .  ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
"$PY" -m ruff check .

if [ "$quick" -eq 0 ]; then
  step "pytest (coverage gate, same as CI)"
  "$PY" -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90
fi

if [ "$py311" -eq 1 ]; then
  step "pytest on Python 3.11 (main's second matrix leg)"
  uvrun 3.11 --with-requirements requirements-dev.txt python -m pytest
fi

step "scripts/tests"
"$PY" -m pytest scripts/tests -q

step "dry-run demo smoke test"
demo_dir="$(mktemp -d)"
trap 'rm -rf "$demo_dir"' EXIT
"$PY" run_dca.py --demo --report --now 2026-07-06T15:45 --artifacts-dir "$demo_dir"

if [ "$audit" -eq 1 ]; then
  step "pip-audit (pinned dev deps)"
  "$PY" -m pip_audit --requirement requirements-dev.txt --strict

  # Same manifests and lock export as ci.yml's research-audit job, both matrix versions.
  for pyver in 3.11 3.12; do
    step "pip-audit (research dependency trees, $pyver)"
    audit_dir="$demo_dir/audit-$pyver"
    mkdir -p "$audit_dir"
    "${UV[@]}" export --quiet --locked --all-extras --no-emit-project --format pylock.toml \
      --output-file "$audit_dir/pylock.root.toml"
    for manifest in "${SIBLINGS[@]/%//requirements.txt}" trading-strategies/requirements.txt \
      analysis/market-graph-3d/requirements.txt; do
      scope="${manifest%/requirements.txt}"
      "${UV[@]}" pip compile --quiet "$manifest" --python-version "$pyver" --no-build --format pylock.toml \
        --output-file "$audit_dir/pylock.${scope//\//-}.toml"
    done
    "$PY" -m pip_audit --locked "$audit_dir" --strict
  done
fi

# Each sibling gets the interpreter and install set its ci.yml job uses, in its own
# throwaway env. Linting uses the same isolated rule set as CI.
for proj in "${siblings[@]+"${siblings[@]}"}"; do
  case "$proj" in
    production-safe-agent-loop | ml-forecaster) pyver=3.11; deps=(--with-requirements "$PWD/$proj/requirements.txt") ;;
    # CI deliberately skips vectorbt/torch here; the heavy tests importorskip away.
    ticker-advisor) pyver=3.11; deps=(--with pandas --with numpy --with scipy --with requests --with pandas_market_calendars) ;;
    wondrous-octopus) pyver=3.11; deps=(--with-requirements "$PWD/$proj/requirements.txt") ;;
    ticker-market-breadth | llm-providers | yfinance-prefetch) pyver=3.12; deps=(--with-requirements "$PWD/$proj/requirements.txt") ;;
    *) echo "local_ci: unknown sibling project $proj" >&2; exit 2 ;;
  esac
  step "ruff + pytest: $proj ($pyver)"
  "$PY" -m ruff check --isolated --select=E,F,B --line-length=120 "$proj"
  (cd "$proj" && uvrun "$pyver" --with-requirements "$OLDPWD/requirements-dev.txt" "${deps[@]}" python -m pytest tests/)
done

printf '\nAll local CI checks passed.\n'
