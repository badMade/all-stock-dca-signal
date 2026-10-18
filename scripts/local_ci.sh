#!/usr/bin/env bash
# Run the CI checks locally so a push does not spend GitHub Actions minutes on a
# failure that a laptop would have caught. Mirrors .github/workflows/ci.yml's
# core job (lint -> coverage-gated tests -> dry-run demo) plus the scripts/ suite.
#
# Usage:
#   scripts/local_ci.sh                       # core checks
#   scripts/local_ci.sh --quick               # skip the full coverage run; lint + demo + scripts/tests
#   scripts/local_ci.sh ticker-advisor ...    # also lint+test the named sibling projects
#   scripts/local_ci.sh --audit               # also run pip-audit (needs network)
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

quick=0
audit=0
siblings=()
for arg in "$@"; do
  case "$arg" in
    --quick) quick=1 ;;
    --audit) audit=1 ;;
    *) siblings+=("$arg") ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }

step "ruff check ."
"$PY" -m ruff check .

if [ "$quick" -eq 0 ]; then
  step "pytest (coverage gate, same as CI)"
  "$PY" -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90
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
fi

# Sibling projects carry their own requirements.txt; install them yourself first
# (see CLAUDE.md). Linting uses the same isolated rule set as CI.
for proj in "${siblings[@]+"${siblings[@]}"}"; do
  step "ruff + pytest: $proj"
  "$PY" -m ruff check --isolated --select=E,F,B --line-length=120 "$proj"
  (cd "$proj" && "$PY" -m pytest tests/)
done

printf '\nAll local CI checks passed.\n'
