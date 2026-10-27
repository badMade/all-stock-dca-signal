#!/bin/bash
# Install (or reinstall, or remove) the launchd agent that publishes the `recommendations` branch from this Mac.
#
#   deploy/launchd/install_recommendations_publish.sh                       # install, cron root from the environment
#   deploy/launchd/install_recommendations_publish.sh --cron-root DIR       # install against an explicit cron root
#   deploy/launchd/install_recommendations_publish.sh --uninstall           # unload and remove the agent
#   deploy/launchd/install_recommendations_publish.sh --dry-run [...]       # render and check only; load nothing
#
# Why (2026-10-26): the branch was a 404 because its only writer ran in the courier's scheduled Claude session, which
# holds no GitHub credentials. The push now runs here, under your own git login, from
# com.all-stock-dca.recommendations-publish.plist.template (docs/plan_first_recommendations.md). This script is the
# install steps from that doc, plus the checks that would otherwise only show up as a silent NONE or ERROR later:
#
#   1. the venv Python and git exist, and the cron root holds the courier's recommendation store;
#   2. git can push to origin from this login (`git push --dry-run`, which changes nothing on the remote);
#   3. any loaded copy is unloaded, then `snapshot_cycle publish` runs once by hand and does not fail (NONE before the
#      day's first set is fine); a failure from here on reloads the old agent;
#   4. the rendered plist passes `plutil -lint` and the new one is bootstrapped.
#
# Re-running it replaces the agent, so it is also how you change the cron root. Standard tools only (bash, sed, git,
# launchctl, plutil). Overrides: DCA_PUBLISH_PYTHON (default <repo>/.venv/bin/python; the agent runs the same one),
# DCA_LAUNCH_AGENTS_DIR (default ~/Library/LaunchAgents; tests only, and ignored by --dry-run, which always sandboxes).
set -euo pipefail

LABEL="com.all-stock-dca.recommendations-publish"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$REPO/deploy/launchd/$LABEL.plist.template"
PY="${DCA_PUBLISH_PYTHON:-$REPO/.venv/bin/python}"
AGENTS_DIR="${DCA_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
TARGET="$AGENTS_DIR/$LABEL.plist"
LOG="/tmp/dca-recommendations-publish.log"

DRY_RUN=0
UNINSTALL=0
SKIP_PUSH_CHECK=0
CRON_ROOT=""

usage() {
  echo "usage: install_recommendations_publish.sh [--cron-root DIR] [--dry-run] [--skip-push-check] [--uninstall]" >&2
  exit 2
}
die() { echo "install_recommendations_publish: $*" >&2; exit 1; }
# In a dry run every command that would change this Mac is printed instead of run.
act() { if [ "$DRY_RUN" = 1 ]; then echo "would run: $*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --cron-root) [ $# -ge 2 ] || usage; CRON_ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-push-check) SKIP_PUSH_CHECK=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

# A dry run must never leave a plist in the real ~/Library/LaunchAgents (launchd would load it at the next login), nor
# delete one there. It always works in a sandbox, whatever DCA_LAUNCH_AGENTS_DIR says: that override is for exercising
# the real path against fakes, and honouring it here let `--dry-run [--uninstall]` write or remove a live agent
# (PR #1972 review).
if [ "$DRY_RUN" = 1 ]; then
  AGENTS_DIR="${TMPDIR:-/tmp}/dca-launch-agents-dry-run"
  TARGET="$AGENTS_DIR/$LABEL.plist"
fi

if [ "$DRY_RUN" = 0 ] && [ "$(uname -s)" != "Darwin" ]; then
  die "launchd agents exist only on macOS; run this on the Mac (or pass --dry-run to check the rendering)"
fi
DOMAIN="gui/$(id -u)"

WAS_LOADED=0
unload_if_loaded() {
  # A dry run cannot ask launchctl, so an existing plist stands in for "loaded".
  if [ "$DRY_RUN" = 1 ]; then
    [ -e "$TARGET" ] && echo "would run: launchctl bootout $DOMAIN/$LABEL"
    return 0
  fi
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
    WAS_LOADED=1
  fi
}

# Put back the agent this run stopped, untouched, when the install gives up after unloading it.
restore_old_agent() {
  if [ "$DRY_RUN" = 0 ] && [ "$WAS_LOADED" = 1 ] && [ -e "$TARGET" ]; then
    launchctl bootstrap "$DOMAIN" "$TARGET" || echo "install_recommendations_publish: could not reload the old agent" >&2
  fi
}

if [ "$UNINSTALL" = 1 ]; then
  unload_if_loaded
  rm -f "$TARGET"
  echo "install_recommendations_publish: removed $LABEL"
  exit 0
fi

# --- 1. inputs -------------------------------------------------------------------------------------------------------
[ -f "$TEMPLATE" ] || die "template missing: $TEMPLATE"
if [ "$DRY_RUN" = 0 ] && [ "$PY" != "$REPO/.venv/bin/python" ]; then
  die "DCA_PUBLISH_PYTHON is supported only with --dry-run; the agent requires $REPO/.venv/bin/python"
fi
[ -x "$PY" ] || die "python not found at $PY (create the venv: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')"
# The agent runs this SAME interpreter (rendered into the plist below), so what the checks prove is what launchd
# starts. The template's fixed .venv path used to ignore DCA_PUBLISH_PYTHON: an install could pass every check and
# then never start (PR #1972 review). Absolute, because launchd resolves nothing relative.
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
case "$PY" in
  *'&'*|*'<'*|*'>'*|*'|'*|*'\'*) die "python path contains a character that cannot go into the plist: $PY" ;;
esac
command -v git >/dev/null || die "git is not on PATH"
cd "$REPO"
if [ -z "$CRON_ROOT" ]; then
  # dca_paths is the one resolver every stream uses: $DCA_CRON_TRACKING_ROOT, else the gitignored .env, else
  # <checkout>/cron_tracking. Asking it keeps this script from guessing a different tree than the courier writes.
  CRON_ROOT="$("$PY" -c 'import dca_paths; print(dca_paths.cron_tracking_root())')"
fi
case "$CRON_ROOT" in
  # sed's replacement and the plist's XML both need these escaped; refusing them is simpler and safer than escaping.
  *'&'*|*'<'*|*'>'*|*'|'*|*'\'*) die "cron root contains a character that cannot go into the plist (& < > | \\): $CRON_ROOT" ;;
esac
case "$REPO" in
  *'&'*|*'<'*|*'>'*|*'|'*|*'\'*) die "checkout path contains a character that cannot go into the plist: $REPO" ;;
esac
[ -d "$CRON_ROOT/recommendations" ] || die "no recommendation store at $CRON_ROOT/recommendations: is this the root the courier writes to? Pass --cron-root DIR"
echo "install_recommendations_publish: checkout $REPO"
echo "install_recommendations_publish: cron root $CRON_ROOT"

# --- 2. can this login push? -----------------------------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ] || [ "$SKIP_PUSH_CHECK" = 1 ]; then
  echo "install_recommendations_publish: skipping the push check"
else
  # --dry-run authenticates against the remote and sends nothing; --force keeps an existing branch's history from
  # turning a working login into a false "rejected".
  # Its own output is discarded, not shown: --quiet does not silence errors, and git's stderr can name the remote URL
  # with a token in it (the reason dca_rec_publish's GitHubBranchPublisher._git captures it; PR #1972 review). Run
  # `git push --dry-run origin HEAD:refs/heads/recommendations` by hand to see the reason.
  git push --dry-run --force --quiet origin HEAD:refs/heads/recommendations >/dev/null 2>&1 \
    || die "git cannot push to origin from this login; fix the credential helper, then re-run"
  echo "install_recommendations_publish: git push credentials OK"
fi

# --- 3. one publish by hand ------------------------------------------------------------------------------------------
# Stop any loaded agent FIRST: left running, it could force-push from the old cron root right after this publish saved
# the new root's digest, leaving the branch on old data while the new agent reports UNCHANGED (PR #1972 review). A
# failure from here on puts the old agent back, so a refused reinstall never leaves the branch unpublished.
unload_if_loaded
if [ "$DRY_RUN" = 1 ]; then
  echo "would run: DCA_CRON_TRACKING_ROOT=$CRON_ROOT $PY -m scripts.snapshot_cycle publish"
else
  set +e
  OUT="$(DCA_CRON_TRACKING_ROOT="$CRON_ROOT" "$PY" -m scripts.snapshot_cycle publish)"
  STATUS=$?
  set -e
  echo "install_recommendations_publish: publish -> $OUT"
  case "$STATUS" in
    0|3) ;;  # OK / UNCHANGED, or NONE before the day's first set
    *) restore_old_agent; die "publish failed (exit $STATUS); the agent would fail the same way, so it was not installed" ;;
  esac
fi

# --- 4. render, check, load ------------------------------------------------------------------------------------------
mkdir -p "$AGENTS_DIR"
RENDERED="$(mktemp "${TMPDIR:-/tmp}/dca-publish-plist.XXXXXX")"
trap 'rm -f "$RENDERED"' EXIT
# The interpreter line first: it is the one REPLACE_WITH_REPO_PATH use that must become $PY, not the checkout's .venv.
sed -e "s|REPLACE_WITH_REPO_PATH/.venv/bin/python|$PY|g" -e "s|REPLACE_WITH_REPO_PATH|$REPO|g" \
  -e "s|REPLACE_WITH_CRON_TRACKING_ROOT|$CRON_ROOT|g" "$TEMPLATE" >"$RENDERED"
if grep -q "REPLACE_WITH_" "$RENDERED"; then
  restore_old_agent; die "the template has a placeholder this script does not fill"
fi
if command -v plutil >/dev/null; then
  plutil -lint "$RENDERED" >/dev/null || { restore_old_agent; die "the rendered plist is not valid"; }
fi
cp "$RENDERED" "$TARGET"
chmod 644 "$TARGET"
act launchctl bootstrap "$DOMAIN" "$TARGET"

if [ "$DRY_RUN" = 1 ]; then
  echo "install_recommendations_publish: dry run; rendered $TARGET, nothing loaded"
  exit 0
fi
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || die "launchd did not load $LABEL"
echo "install_recommendations_publish: installed $LABEL (every 5 minutes; log: $LOG)"
echo "install_recommendations_publish: after the first trading-day set, check: git ls-remote origin refs/heads/recommendations"
