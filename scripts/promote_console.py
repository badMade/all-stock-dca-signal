#!/usr/bin/env python3
"""Promote a reviewed console release to the stable checkout, only after explicit approval.

The sandbox is where GUI changes are tried; the stable console (launchd, pinned
tag) must not move until the operator approves.  This script is the only path
that moves it, and it is deliberately outside the console itself (the console
never writes state or config).  Gates, all of which must pass:

1. ``--ref`` (default ``HEAD``) is already merged into ``--main-ref`` (default
   ``origin/main``) -- i.e. the PR was reviewed and merged.
2. At that ref, ``CONSOLE_VERSION`` is newer than the stable checkout's, has the
   top ``docs/console_changelog.md`` entry, and ``ASSET_DIGEST`` matches ``console/``.
3. A human on a TTY types ``promote <version>``.  Piped/agent runs are refused.

Then it tags ``console-<version>``, checks the stable checkout out at the tag
and (with ``--kickstart``) restarts the launchd agent.  ``--dry-run`` runs the
gates and stops.  Stdlib only; never places orders.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dca_console_release import digest_files  # noqa: E402

LABEL = "com.all-stock-dca.console"
TAG_PREFIX = "console-"


class PromotionError(Exception):
    """A gate failed; nothing was changed."""


def git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)  # nosec B603 B607
    if result.returncode != 0:
        raise PromotionError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else result.stdout.decode().strip()


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as error:
        raise PromotionError(f"Not a numeric dotted version: {version!r}") from error


def release_at(repo: Path, ref: str) -> dict:
    """Version, pinned digest, computed digest and top changelog version at ``ref``."""
    source = git(repo, "show", f"{ref}:dca_console_release.py")
    version = re.search(r'^CONSOLE_VERSION = "([^"]+)"', source, re.M)
    pinned = re.search(r'^ASSET_DIGEST = "([^"]+)"', source, re.M)
    if not (version and pinned):
        raise PromotionError(f"{ref} has no CONSOLE_VERSION/ASSET_DIGEST in dca_console_release.py")
    files = {}
    for name in git(repo, "ls-tree", "--name-only", f"{ref}:console/").splitlines():
        files[name] = git(repo, "show", f"{ref}:console/{name}", binary=True)
    changelog = git(repo, "show", f"{ref}:docs/console_changelog.md")
    headings = [ln[3:].split()[0] for ln in changelog.splitlines() if ln.startswith("## ") and ln[3:].strip()]
    return {"version": version.group(1), "pinned": pinned.group(1), "computed": digest_files(files),
            "changelog_top": headings[0] if headings else None}


def check_gates(repo: Path, ref: str, main_ref: str, stable_version: str | None) -> dict:
    """Raise PromotionError unless ``ref`` is merged, versioned, logged and digest-clean."""
    merged = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ref, main_ref],
                            capture_output=True, check=False)  # nosec B603 B607
    if merged.returncode != 0:
        raise PromotionError(f"{ref} is not merged into {main_ref}; merge the reviewed PR first.")
    info = release_at(repo, ref)
    if info["pinned"] != info["computed"]:
        raise PromotionError("console/ does not match the pinned ASSET_DIGEST (UI changed without a re-pin).")
    if info["changelog_top"] != info["version"]:
        raise PromotionError(f"docs/console_changelog.md top entry is {info['changelog_top']}, expected {info['version']}.")
    if stable_version is not None and version_key(info["version"]) <= version_key(stable_version):
        raise PromotionError(f"Version {info['version']} is not newer than the stable version {stable_version}.")
    return info


def stable_version_of(stable: Path) -> str | None:
    try:
        text = (stable / "dca_console_release.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^CONSOLE_VERSION = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def confirm(version: str, *, stdin=sys.stdin, prompt=input) -> bool:
    """Approval must be typed by a human at a terminal; scripts and agents cannot answer."""
    if not stdin.isatty():
        raise PromotionError("Approval needs an interactive terminal; refusing to promote non-interactively.")
    return prompt(f"Type 'promote {version}' to move the stable console: ").strip() == f"promote {version}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stable-checkout", type=Path, required=True, help="Pinned checkout the launchd agent runs")
    parser.add_argument("--ref", default="HEAD", help="Reviewed release commit/branch (default: HEAD)")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--kickstart", action="store_true", help="Restart the launchd agent afterwards")
    parser.add_argument("--dry-run", action="store_true", help="Run the gates and stop")
    args = parser.parse_args(argv)
    stable = args.stable_checkout.expanduser().resolve()
    try:
        current = stable_version_of(stable)
        info = check_gates(REPO, args.ref, args.main_ref, current)
        version, tag = info["version"], TAG_PREFIX + info["version"]
        print(f"Stable is {current or 'unknown'}; candidate {version} at {args.ref} passes all gates.")
        print(git(REPO, "diff", "--stat", f"{stable_ref(stable)}..{args.ref}", "--", "console", "dca_console*.py"))
        if args.dry_run:
            return 0
        if not confirm(version):
            print("Not approved; nothing changed.")
            return 1
        sha = git(REPO, "rev-parse", f"{args.ref}^{{commit}}")
        if subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                          capture_output=True, check=False).returncode != 0:  # nosec B603 B607
            git(REPO, "tag", tag, sha)
        git(stable, "fetch", "--tags", str(REPO))
        git(stable, "checkout", "--detach", tag)
        print(f"Stable checkout is now at {tag}.")
        if args.kickstart:
            uid = subprocess.run(["id", "-u"], capture_output=True, text=True, check=True).stdout.strip()  # nosec B603 B607
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"], check=True)  # nosec B603 B607
        return 0
    except PromotionError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1


def stable_ref(stable: Path) -> str:
    try:
        return git(stable, "rev-parse", "HEAD")
    except PromotionError:
        return "HEAD"


if __name__ == "__main__":
    raise SystemExit(main())
