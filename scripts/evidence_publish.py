#!/usr/bin/env python3
"""Publish a cloud run's evidence to the private evidence store, and claim a slot before trading.

A cloud container is thrown away after every fire, so Gate 9 writes the
evidence locally exactly as it always has and this command publishes it
afterwards. The rules it enforces:

* The local artifacts are never moved or deleted, whatever the push does. A
  failed push commits a marker locally, exits 3, and returns an alert text that
  carries the key run numbers (SHA, slot, orders, tickers, dollars, order IDs),
  because the store copy is the one that failed. It never re-runs Gate 7.
* Nothing is pushed that contains the account number: an exact match against
  ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` or an account-shaped literal refuses the
  whole publish (exit 4) and names only the offending relative paths.
* Slots publish into their own directories, so a competing publish from another
  container is merged, not lost. ``MANIFEST.json`` and ``latest_*_run.json``
  pointers are the only shared files and are merged by rule.
* ``claim`` is a compare-and-swap on the remote: the first executor for a slot
  and day wins, a second is refused (exit 5), and an unreachable store refuses
  too (exit 6). It replaces the per-container leases and file locks that a fresh
  container cannot share.

Usage::

    python3 -m scripts.evidence_publish publish --evidence-root ROOT --remote URL \\
        --trading-day 2026-09-29 --slot 1330 --code-sha <40 hex>
    python3 -m scripts.evidence_publish claim --evidence-root ROOT --remote URL \\
        --trading-day 2026-09-29 --slot 1330 --claimant <run id>

Exit codes: 0 ok, 3 push failed, 4 refused (secret), 5 already claimed, 6 store
unreachable, 7 refused (release provenance does not match the pinned release). Stdout ends with one JSON object. Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from dca_config import _reject_duplicate_keys
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative

__all__ = ["main"]

EXIT_OK = 0
EXIT_PUSH_FAILED = 3
EXIT_REFUSED_SECRET = 4
EXIT_ALREADY_CLAIMED = 5
EXIT_STORE_UNREACHABLE = 6
EXIT_REFUSED_PROVENANCE = 7

ACCOUNT_ENV = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
PINNED_SHA_ENV = "DCA_PINNED_SHA"
BRANCH = "main"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_VERSION = 1
MARKER_DIR = "evidence_push_failed"
CLAIMS_DIR = "claims"
NETWORK_TIMEOUT_SECONDS = 60
CLAIM_ATTEMPTS = 3
# Per-run lock sidecars (scripts/finalize_run_evidence.py) are not evidence.
GITIGNORE_LINES = ("*.lock",)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SLOT_RE = re.compile(r"(\d{4})(?:ET)?")
# An account-shaped literal: the word account, an optional number/id suffix, then a
# separator and six or more digits. "account_number_redacted": true (what
# finalize_run_evidence writes) does not match.
_SHAPED_ACCOUNT_RE = re.compile(r"account[\s_-]*(?:number|num|no|id)?[\"']?\s*[:=]\s*[\"']?\d{6,}", re.IGNORECASE)
# A run id: what --claimant may hold. Anything else could smuggle free text (or the account
# number, once split by punctuation) into a file and a commit message that get pushed.
_CLAIMANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
_POINTER_RE = re.compile(r"latest_.*_run\.json")
_NON_FAST_FORWARD_HINTS = ("rejected", "non-fast-forward", "fetch first")


class Failure(Exception):
    """A step failed in a way that must surface as a failed publish or an unreachable store."""


def _scrub(text: str, env: Mapping[str, str]) -> str:
    account = env.get(ACCOUNT_ENV) or ""
    return text.replace(account, "[REDACTED]") if len(account) >= 6 else text


def _git(
    root: Path | str, env: Mapping[str, str], *args: str, network: bool = False
) -> subprocess.CompletedProcess:
    # An identity is always supplied so a container with no global git config can commit.
    command = ["git", "-c", "user.name=dca-evidence", "-c", "user.email=dca-evidence@localhost", *args]
    try:
        return subprocess.run(
            command, cwd=str(root), env={**env, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True,
            timeout=NETWORK_TIMEOUT_SECONDS if network else None,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"git {args[0]} timed out after {NETWORK_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"git is not runnable: {exc.__class__.__name__}")


def _last_line(result: subprocess.CompletedProcess) -> str:
    return f"git exited with status {result.returncode}"


def _require(result: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if result.returncode != 0:
        raise Failure(f"{what} failed: {_last_line(result)}")
    return result


def _json_out(payload: Dict[str, Any], env: Mapping[str, str]) -> None:
    print(_scrub(json.dumps(payload, sort_keys=True), env))


# -- manifest ---------------------------------------------------------------------------------------------------


def _load_manifest(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"version": MANIFEST_VERSION, "days": {}}
    try:
        # A duplicate key is ambiguous completeness data: refuse it rather than let file
        # order pick a winner (Copilot review, PR #1571).
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise Failure("evidence manifest is unusable: not valid JSON or has a duplicate key") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION or not isinstance(
        manifest.get("days"), dict
    ):
        raise Failure("evidence manifest is unusable: expected version 1 with a days object")
    return manifest


def _slots_of(manifest: Mapping[str, Any], day: str) -> List[str]:
    entry = manifest.get("days", {}).get(day, {})
    slots = entry.get("slots", []) if isinstance(entry, dict) else []
    return [slot for slot in slots if isinstance(slot, str)]


def _with_slot(manifest: Mapping[str, Any], day: str, slot: str, sha: str) -> Dict[str, Any]:
    updated = json.loads(json.dumps(manifest))
    updated["days"].setdefault(day, {})["slots"] = sorted(set(_slots_of(manifest, day)) | {slot})
    updated["last_publish"] = {"day": day, "slot": slot, "code_sha": sha}
    return updated


def _merge_manifests(theirs: Mapping[str, Any], mine: Mapping[str, Any]) -> Dict[str, Any]:
    """Union of both sides' published slots; this publisher's ``last_publish`` wins."""
    merged = json.loads(json.dumps(theirs))
    for day in mine.get("days", {}):
        merged.setdefault("days", {}).setdefault(day, {})["slots"] = sorted(
            set(_slots_of(theirs, day)) | set(_slots_of(mine, day))
        )
    if "last_publish" in mine:
        merged["last_publish"] = mine["last_publish"]
    return merged


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# -- secret scan ------------------------------------------------------------------------------------------------


def _pending_files(root: Path, env: Mapping[str, str]) -> List[str]:
    """Modified or untracked, non-ignored files: exactly what a publish would newly commit."""
    listed = _require(_git(root, env, "ls-files", "-m", "-o", "--exclude-standard", "-z"), "git ls-files")
    return sorted(path for path in listed.stdout.split("\0") if path and (root / path).is_file())


def _holds_account(data: bytes, env: Mapping[str, str]) -> bool:
    """True when ``data`` holds the account number, exact or account-shaped."""
    account = env.get(ACCOUNT_ENV) or ""
    exact = account.encode("utf-8") if len(account) >= 6 else b""
    return bool((exact and exact in data) or _SHAPED_ACCOUNT_RE.search(data.decode("utf-8", errors="ignore")))


def scan_for_secrets(root: Path, env: Mapping[str, str]) -> List[str]:
    """Relative paths whose contents hold the account number, exact or account-shaped."""
    return [relative for relative in _pending_files(root, env) if _holds_account((root / relative).read_bytes(), env)]


def _provenance_mismatch(root: Path, env: Mapping[str, str], day: str, slot: str, sha: str) -> Optional[str]:
    """Why ``sha`` is not provably the release that produced this slot's evidence, else None.

    ``--code-sha`` is caller-supplied, so it is bound to the pin the run environment
    holds outside the repo, and to the ``code_version`` Gate 9 recorded in the artifacts
    (Copilot review, PR #1571). Artifacts that carry no ``code_version`` (an executor
    that never passed one) have nothing to contradict and are not refused for that.
    """
    pin = env.get(PINNED_SHA_ENV) or ""
    if pin and sha != pin:
        return f"--code-sha is not the pinned release named by {PINNED_SHA_ENV}"
    try:
        run_dir = root / slot_run_dir_relative(slot, date.fromisoformat(day))
    except (KeyError, ValueError):
        return None
    for name in ("run_state.json", "run_summary.json"):
        try:
            document = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        code_version = document.get("code_version") if isinstance(document, dict) else None
        if isinstance(code_version, dict) and code_version.get("git_sha") != sha:
            return f"{name} records a code_version.git_sha that differs from --code-sha"
    return None


# -- alert text -------------------------------------------------------------------------------------------------


def _run_numbers(root: Path, day: str, slot: str) -> List[str]:
    """The key run numbers from the local artifacts; empty when they are not readable."""
    try:
        run_dir = root / slot_run_dir_relative(slot, date.fromisoformat(day))
    except (KeyError, ValueError):
        return ["run artifacts: unknown slot or day"]
    lines: List[str] = []
    try:
        summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        lines.append(
            f"orders submitted: {summary.get('orders_submitted_count')} | "
            f"order_submit_status: {summary.get('order_submit_status')} | status: {summary.get('status')}"
        )
    except (OSError, ValueError):
        lines.append("run_summary.json: unavailable")
    try:
        with (run_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        rows = []
    total = 0.0
    for row in rows:
        amount = row.get("submitted_amount_usd", "")
        lines.append(f"order: {row.get('ticker')} {row.get('order_id')} {amount} USD {row.get('status')}")
        try:
            total += float(amount)
        except ValueError:
            pass
    if rows:
        lines.append(f"total submitted: {total:g} USD across {len(rows)} order(s)")
    return lines


def _alert_text(headline: str, root: Path, day: str, slot: str, sha: str, detail: str) -> str:
    lines = [f"{headline} | slot {slot} | trading day {day} | code sha {sha}", *_run_numbers(root, day, slot)]
    lines.append(f"detail: {detail}")
    lines.append(f"local evidence is kept under {root}; nothing was re-run and no order was changed")
    return "\n".join(lines)


# -- publish ----------------------------------------------------------------------------------------------------


def _ensure_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    present = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [line for line in GITIGNORE_LINES if line not in present]
    if missing:
        path.write_text("\n".join([*present, *missing]) + "\n", encoding="utf-8")


def _has_changes(root: Path, env: Mapping[str, str]) -> bool:
    return bool(_require(_git(root, env, "status", "--porcelain"), "git status").stdout.strip())


def _commit_all(root: Path, env: Mapping[str, str], message: str) -> None:
    _require(_git(root, env, "add", "-A"), "git add")
    if _has_changes(root, env):
        _require(_git(root, env, "commit", "-q", "-m", message), "git commit")


def _resolve_pointer(root: Path, env: Mapping[str, str], relative: str) -> None:
    """A conflicted ``latest_*_run.json``: the more recently written pointer wins, ties keep ours."""
    ours = _git(root, env, "show", f":2:{relative}").stdout
    theirs = _git(root, env, "show", f":3:{relative}").stdout

    def written_at(text: str) -> str:
        try:
            parsed = json.loads(text)
            return str(parsed.get("updated_at_utc", "")) if isinstance(parsed, dict) else ""
        except ValueError:
            return ""

    chosen = theirs if written_at(theirs) > written_at(ours) else ours
    (root / relative).write_text(chosen, encoding="utf-8")
    _require(_git(root, env, "add", relative), "git add")


def _integrate(root: Path, remote: str, env: Mapping[str, str], mine: Mapping[str, Any]) -> None:
    """Merge the store's newer main into local main after a non-fast-forward rejection."""
    _require(_git(root, env, "fetch", remote, BRANCH, network=True), "git fetch")
    merged = _git(root, env, "merge", "--no-edit", "FETCH_HEAD")
    if merged.returncode != 0:
        conflicted = _git(root, env, "diff", "--name-only", "--diff-filter=U").stdout.split()
        for relative in conflicted:
            if relative == MANIFEST_NAME:
                continue  # rewritten below from both sides
            if _POINTER_RE.fullmatch(Path(relative).name):
                _resolve_pointer(root, env, relative)
                continue
            _git(root, env, "merge", "--abort")
            raise Failure(f"merge conflict in {relative}")
        theirs_text = _git(root, env, "show", f"FETCH_HEAD:{MANIFEST_NAME}").stdout
        _write_manifest(root / MANIFEST_NAME, _merge_manifests(_manifest_from_text(theirs_text), mine))
        _require(_git(root, env, "add", MANIFEST_NAME), "git add")
        _require(_git(root, env, "commit", "-q", "--no-edit"), "git commit")
    # Even a clean textual merge of the manifest is re-derived, so it can never end up
    # as a JSON document nobody wrote.
    theirs_text = _git(root, env, "show", f"FETCH_HEAD:{MANIFEST_NAME}").stdout
    normalized = _merge_manifests(_manifest_from_text(theirs_text), mine)
    if _load_manifest(root / MANIFEST_NAME) != normalized:
        _write_manifest(root / MANIFEST_NAME, normalized)
        _commit_all(root, env, "evidence manifest merge")


def _manifest_from_text(text: str) -> Dict[str, Any]:
    try:
        manifest = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise Failure("fetched evidence manifest is unusable: not valid JSON or has a duplicate key") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != MANIFEST_VERSION
        or not isinstance(manifest.get("days"), dict)
    ):
        raise Failure("fetched evidence manifest is unusable: expected version 1 with a days object")
    return manifest


def _push(root: Path, remote: str, env: Mapping[str, str], mine: Mapping[str, Any],
          max_attempts: int, backoff: float) -> Optional[str]:
    """Push HEAD to the store; None on success, else the last failure detail."""
    detail = "push not attempted"
    for attempt in range(max_attempts):
        pushed = _git(root, env, "push", remote, f"HEAD:{BRANCH}", network=True)
        if pushed.returncode == 0:
            return None
        detail = _last_line(pushed)
        if any(hint in (pushed.stderr or "") for hint in _NON_FAST_FORWARD_HINTS):
            try:
                _integrate(root, remote, env, mine)
            except Failure as exc:
                detail = str(exc)
            else:
                retried = _git(root, env, "push", remote, f"HEAD:{BRANCH}", network=True)
                if retried.returncode == 0:
                    return None
                detail = _last_line(retried)
        if attempt + 1 < max_attempts:
            time.sleep(backoff * (2 ** attempt))
    return detail


def _publish(args: argparse.Namespace, env: Mapping[str, str]) -> Tuple[int, Dict[str, Any]]:
    root = Path(args.evidence_root)
    day, slot, sha = args.trading_day, args.slot, args.code_sha
    marker = root / MARKER_DIR / f"{day}_{slot}.json"
    try:
        if not (root / ".git").is_dir():
            raise Failure("evidence root is not an evidence clone")
        refused = scan_for_secrets(root, env)
        if refused:
            return EXIT_REFUSED_SECRET, {
                "status": "refused_secret",
                "refused_paths": refused,
                "alert_text": _alert_text(
                    "EVIDENCE PUBLISH REFUSED (account number in evidence; nothing was pushed)",
                    root, day, slot, sha, "contents were left uncommitted for a human to inspect",
                ),
            }
        mismatch = _provenance_mismatch(root, env, day, slot, sha)
        if mismatch:
            return EXIT_REFUSED_PROVENANCE, {
                "status": "refused_provenance",
                "detail": mismatch,
                "alert_text": _alert_text(
                    "EVIDENCE PUBLISH REFUSED (release provenance mismatch; nothing was pushed)",
                    root, day, slot, sha, mismatch,
                ),
            }
        _ensure_gitignore(root)
        manifest = _with_slot(_load_manifest(root / MANIFEST_NAME), day, slot, sha)
        _write_manifest(root / MANIFEST_NAME, manifest)
        # A later success supersedes an earlier failure marker for the same slot.
        if marker.is_file():
            marker.unlink()
        _commit_all(root, env, f"evidence {day} {slot} {sha[:12]}")
        detail = _push(root, args.remote, env, manifest, args.max_attempts, args.backoff_seconds)
    except Failure as exc:
        detail = str(exc)
    if detail is None:
        return EXIT_OK, {"status": "published"}
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"day": day, "slot": slot, "code_sha": sha, "detail": detail,
                        "failed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _commit_all(root, env, f"evidence push failed {day} {slot}")
    except (OSError, Failure):
        pass  # the alert below is the record; the local artifacts are untouched either way
    return EXIT_PUSH_FAILED, {
        "status": "push_failed",
        "alert_text": _alert_text("EVIDENCE PUSH FAILED", root, day, slot, sha, detail),
    }


# -- claim ------------------------------------------------------------------------------------------------------


def _drop_claim_commit(root: Path, env: Mapping[str, str], relative: str) -> None:
    """Undo this call's own claim commit (never anything else) after a lost race."""
    _git(root, env, "reset", "-q", "--mixed", "HEAD~1")
    claim_file = root / relative
    if claim_file.is_file():
        claim_file.unlink()


def _claimed_by(text: str) -> str:
    try:
        parsed = json.loads(text)
        return str(parsed.get("claimed_by", "unknown")) if isinstance(parsed, dict) else "unknown"
    except ValueError:
        return "unknown"


def _claim(args: argparse.Namespace, env: Mapping[str, str]) -> Tuple[int, Dict[str, Any]]:
    root = Path(args.evidence_root)
    relative = f"{CLAIMS_DIR}/{args.trading_day}_{args.slot}.json"
    unreachable = {"status": "store_unreachable", "claimed_by": ""}
    # The claimant is written to a pushed file and a commit message, so it gets the same
    # account-number refusal publish applies to its files, before anything is committed.
    if _holds_account(args.claimant.encode("utf-8"), env):
        return EXIT_REFUSED_SECRET, {"status": "refused_secret", "claimed_by": "",
                                     "detail": "the claimant holds the account number; nothing was committed"}
    if not (root / ".git").is_dir():
        return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": "evidence root is not an evidence clone"}
    for _ in range(CLAIM_ATTEMPTS):
        fetched = _git(root, env, "fetch", args.remote, BRANCH, network=True)
        if fetched.returncode != 0:
            return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": _last_line(fetched)}
        existing = _git(root, env, "show", f"FETCH_HEAD:{relative}")
        if existing.returncode == 0:
            return EXIT_ALREADY_CLAIMED, {"status": "already_claimed", "claimed_by": _claimed_by(existing.stdout)}
        if _git(root, env, "merge", "--ff-only", "FETCH_HEAD").returncode != 0:
            rebased = _git(root, env, "rebase", "FETCH_HEAD")
            if rebased.returncode != 0:
                _git(root, env, "rebase", "--abort")
                return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": _last_line(rebased)}
        claim_file = root / relative
        claim_file.parent.mkdir(parents=True, exist_ok=True)
        claim_file.write_text(
            json.dumps({"claimed_by": args.claimant, "day": args.trading_day, "slot": args.slot,
                        "claimed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        committed = _git(root, env, "add", relative)
        if committed.returncode == 0:
            committed = _git(root, env, "commit", "-q", "-m", f"claim {args.trading_day} {args.slot} {args.claimant}")
        if committed.returncode != 0:
            return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": _last_line(committed)}
        pushed = _git(root, env, "push", args.remote, f"HEAD:{BRANCH}", network=True)
        if pushed.returncode == 0:
            return EXIT_OK, {"status": "claimed", "claimed_by": args.claimant}
        # Lost a race, or the push failed. Drop only our own claim commit and look again: if
        # the remote now holds a claim it is theirs; if not, the remote just moved and we retry.
        _drop_claim_commit(root, env, relative)
        if not any(hint in (pushed.stderr or "") for hint in _NON_FAST_FORWARD_HINTS):
            return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": _last_line(pushed)}
    return EXIT_STORE_UNREACHABLE, {**unreachable, "detail": "store kept changing; claim not confirmed"}


# -- CLI --------------------------------------------------------------------------------------------------------


def _slot(value: str) -> str:
    match = _SLOT_RE.fullmatch(value)
    # A well-formed but off-roster id (a typo such as 1340) must not be claimable or
    # publishable, or the real slot would stay unclaimed and defeat the mutex.
    if not match or match.group(1) not in SLOT_EVIDENCE_LAYOUT:
        raise argparse.ArgumentTypeError(f"slot {value!r} is not a roster slot id such as 1330")
    return match.group(1)


def _claimant(value: str) -> str:
    if not _CLAIMANT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("claimant must be a run id: 1-80 letters, digits, '.', '_', ':' or '-'")
    return value


def _sha(value: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("code sha must be a full lower-case 40-character commit SHA")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("publish", "claim"):
        sub = commands.add_parser(name)
        sub.add_argument("--evidence-root", required=True)
        sub.add_argument("--remote", required=True)
        sub.add_argument("--trading-day", required=True, help="YYYY-MM-DD, America/New_York")
        sub.add_argument("--slot", required=True, type=_slot)
        if name == "publish":
            sub.add_argument("--code-sha", required=True, type=_sha)
            sub.add_argument("--max-attempts", type=int, default=3)
            sub.add_argument("--backoff-seconds", type=float, default=2.0)
        else:
            sub.add_argument("--claimant", required=True, type=_claimant)
    return parser


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    """Run publish or claim; ``env`` defaults to the process environment (tests inject their own)."""
    active_env: Mapping[str, str] = os.environ if env is None else env
    args = _parser().parse_args(argv)
    try:
        date.fromisoformat(args.trading_day)
    except ValueError:
        _parser().error(f"--trading-day {args.trading_day!r} is not YYYY-MM-DD")
    runner = _publish if args.command == "publish" else _claim
    try:
        code, payload = runner(args, active_env)
    except Exception as exc:  # noqa: BLE001 -- fail closed: an unexpected error is a failed publish or an unreachable store
        code = EXIT_PUSH_FAILED if args.command == "publish" else EXIT_STORE_UNREACHABLE
        payload = {"status": "push_failed" if args.command == "publish" else "store_unreachable",
                   "detail": f"unexpected {exc.__class__.__name__}"}
    _json_out(payload, active_env)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
