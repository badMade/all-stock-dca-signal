#!/usr/bin/env python3
"""Render or install the checkout-only launchd agent for the local console."""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
# Index 1, never 0: index 0 belongs to deploy/launchd when this file is run as
# a script.  Keeping that directory first prevents the repository root from
# shadowing sibling modules in script-style layouts, matching the
# ticker-advisor import-precedence fix cited in the Rock 4 contract.
sys.path.insert(1, str(CHECKOUT))

from dca_bootstrap import get_venv_python, resolve_venv_dir  # noqa: E402
from dca_console import load_auth_file  # noqa: E402
from dca_paths import cron_tracking_root  # noqa: E402

LABEL = "com.all-stock-dca.console"
# Stable-channel rule: always the main checkout's .venv, even from a pinned worktree checkout.
DEFAULT_PYTHON = get_venv_python(resolve_venv_dir(CHECKOUT))
DEFAULT_PORT = 8765
MAX_PORT = 65535
AGENT_RELATIVE_PATH = Path("Library") / "LaunchAgents" / f"{LABEL}.plist"


def _absolute_without_following(path: str | os.PathLike[str]) -> Path:
    """Return an absolute spelling without dereferencing its final component."""
    # Path.resolve() would follow an auth-file symlink before load_auth_file()
    # gets the chance to reject it with O_NOFOLLOW.  A lexical absolute path
    # gives launchd the required full path while preserving that security check
    # (Same Page round 2).
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def render_plist(
    *,
    python_path: Path,
    auth_file: Path,
    port: int,
    trusted_hosts: list[str],
    behind_proxy: bool,
    octopus_run: Path | None,
    root: Path,
    checkout: Path | None = None,
    research_python: Path | None = None,
) -> bytes:
    """Return the complete launchd plist without reading or embedding a secret."""
    # ``checkout`` is the pinned stable-release checkout (docs/local_console.md);
    # it defaults to the checkout holding this script.
    launch_checkout = checkout or CHECKOUT
    arguments = [
        str(python_path),
        str(launch_checkout / "from.py"),
        "--local",
        "--no-browser",
        "--port",
        str(port),
        "--auth-file",
        str(auth_file),
    ]
    for host in trusted_hosts:
        arguments.extend(("--trusted-host", host))
    if behind_proxy:
        arguments.append("--behind-proxy")
    if research_python is not None:
        arguments.extend(("--research-python", str(research_python)))
    if octopus_run is not None:
        arguments.extend(("--octopus-run", str(octopus_run)))

    # Resolve the artifact root once and fan every consumer out from this one
    # value.  In particular, inheriting one root for the console while launchd
    # opens logs under another would split the evidence operators need during
    # an incident (Rock 4 review finding).
    log_directory = root / "console"
    definition = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(launch_checkout),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15,
        "ExitTimeOut": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(log_directory / "launchd.out.log"),
        "StandardErrorPath": str(log_directory / "launchd.err.log"),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "DCA_CRON_TRACKING_ROOT": str(root),
        },
    }
    # plistlib owns XML escaping and type encoding; a hand-maintained template
    # could turn a path containing XML punctuation into an invalid agent.
    return plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the local DCA console launchd agent.")
    parser.add_argument("--auth-file", required=True, type=Path)
    # None is intentional: main() reads DEFAULT_PYTHON at call time, which lets
    # callers and tests replace the checkout default without rebuilding a
    # parser that captured the old value (Rock 4 contract).
    parser.add_argument("--python", type=Path, default=None)
    parser.add_argument("--checkout", type=Path, default=None, metavar="DIR",
                        help="Pinned stable-release checkout whose from.py launchd runs (default: this checkout)")
    parser.add_argument("--research-python", type=Path, default=None, metavar="PYTHON",
                        help="Explicit research interpreter (default: the --python venv)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--trusted-host", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--behind-proxy", action="store_true")
    parser.add_argument("--octopus-run", type=Path, metavar="DIR")
    # Keep the raw string until main() applies the same empty-means-unset rule
    # as dca_paths; Path("") becomes truthy Path(".") and would otherwise
    # override an explicitly configured environment root (Rock 4 review).
    parser.add_argument("--cron-tracking-root", type=str, default=None, metavar="DIR")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--print", dest="print_plist", action="store_true")
    action.add_argument("--install", action="store_true")
    return parser


def _print_launchctl_instructions() -> None:
    agent = f"gui/$(id -u)/{LABEL}"
    plist = f"~/Library/LaunchAgents/{LABEL}.plist"
    print(f"launchctl bootstrap gui/$(id -u) {plist}   # load + start")
    print(f"launchctl print {agent} | grep -E 'state|pid'             # status")
    print(f"launchctl kickstart -k {agent}                            # restart an UNCHANGED definition (e.g. after rotating the auth file)")
    print(f"launchctl bootout {agent}                                 # stop + unload; REQUIRED before bootstrap whenever the plist changed")
    print("# apply pair, printed last, for a changed plist:")
    # bootout must precede bootstrap when the definition changed.  kickstart
    # only restarts the already-loaded definition and would leave stale argv or
    # environment in force (Same Page launchd review).
    print(f"launchctl bootout {agent} 2>/dev/null || true; launchctl bootstrap gui/$(id -u) {plist}")


def main(argv: list[str] | None = None) -> int:
    """Validate inputs, then print or install one self-consistent agent."""
    parser = _parser()
    args = parser.parse_args(argv)

    if not 0 <= args.port <= MAX_PORT:
        parser.error(f"--port must be between 0 and {MAX_PORT}")
    if args.behind_proxy and not args.trusted_host:
        # Rendering a service that ConsoleAccess must reject would create a
        # permanent launchd retry loop instead of a usable fail-closed service.
        parser.error("--behind-proxy requires at least one --trusted-host")

    auth_file = _absolute_without_following(args.auth_file)
    try:
        # Validation is the only use of the returned credential.  The secret is
        # deliberately discarded and can therefore never enter argv, the
        # environment, or plistlib's input (Same Page rounds 1 and 2).
        load_auth_file(auth_file)
    except ValueError as error:
        parser.error(str(error))

    selected_python = args.python if args.python is not None else DEFAULT_PYTHON
    python_path = _absolute_without_following(selected_python)
    if not python_path.is_file():
        parser.error("--python must point to an existing file")

    checkout = None
    if args.checkout is not None:
        checkout = _absolute_without_following(args.checkout)
        if not (checkout / "from.py").is_file():
            parser.error("--checkout must be a directory containing from.py")
    research_python = None
    if args.research_python is not None:
        research_python = _absolute_without_following(args.research_python)
        if not research_python.is_file():
            parser.error("--research-python must point to an existing file")

    octopus_run = None
    if args.octopus_run is not None:
        candidate = _absolute_without_following(args.octopus_run)
        if not candidate.is_dir():
            parser.error("--octopus-run must point to an existing directory")
        octopus_run = candidate

    # cron_tracking_root() reads DCA_CRON_TRACKING_ROOT at call time, so this
    # gives the approved non-empty flag > environment > repository-default
    # precedence.  launchd opens relative stdio paths from /, while the console
    # resolves its environment root against WorkingDirectory; only one lexical
    # absolute value keeps the logs, environment, and pre-created directory in
    # the same tree without dereferencing a symlinked archive (Rock 4 review).
    root = _absolute_without_following(args.cron_tracking_root or cron_tracking_root())
    contents = render_plist(
        python_path=python_path,
        auth_file=auth_file,
        port=args.port,
        trusted_hosts=args.trusted_host,
        behind_proxy=args.behind_proxy,
        octopus_run=octopus_run,
        root=root,
        checkout=checkout,
        research_python=research_python,
    )

    if args.print_plist:
        sys.stdout.write(contents.decode("utf-8"))
        return 0

    # launchd creates the two log files but not their parent.  Create the
    # directory before publishing an agent that can run immediately at load
    # (Rock 4 review finding).
    (root / "console").mkdir(parents=True, exist_ok=True)
    destination = Path.home() / AGENT_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(contents)
    destination.chmod(0o644)
    _print_launchctl_instructions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
