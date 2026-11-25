#!/usr/bin/env python3
"""Reconcile EVERY DCA Issues row against GitHub in one deterministic pass (triage routine STEPs 2, 4 and 6).

Why this exists (2026-11-04). The local ``dca-triage-digest`` task read 42 rows, then spent its turns
hand-converting ``gh`` JSON for ``selfheal.triage_reconcile``: ``gh`` reports PR ``state`` as
``OPEN``/``CLOSED``/``MERGED`` and the reconciler refused it ("State must be open/closed (merged is a separate
flag)"), Progress cells holding operator notes had to be judged one by one, and the digest came out PARTIAL
with STEP 4 Progress writes, STEP 5b briefs and the Approved-row pass all undone. Nothing in that work needs
judgement, so it lives here: the routine exports the Notion rows to a JSON file, runs this once, and writes
exactly the updates the report lists.

Two halves:

* :func:`reconcile` is pure. It takes the rows and already-converted git records and returns the per-Key
  decisions (write / leave / skip, with reasons). Tests drive it directly.
* ``--collect`` gathers the inputs with read-only ``gh`` and ``git`` calls (``gh pr list``/``view``,
  ``gh issue list``, ``git branch -r``, ``git log``, ``git cat-file -e``, ``git rev-parse
  --is-shallow-repository``) into ``--work-dir``. It never writes to GitHub, Notion or the checkout.

What a decision means (the prompt's STEP 4 rules, applied mechanically):

* ``skip`` (with ``skip_reason``): an ``EvidenceError``, a recorded PR neither matched nor citing the Key
  (``recorded_pr_unconfirmed``), or a recorded commit unverifiable in a shallow clone. Write nothing on that row.
* ``updates``: only Progress, GitHub issue, Branch, Commit URL and PR number, only when the new value is
  non-empty and differs. Never a clear.
* ``progress_note_kept``: the Progress cell holds free text (an operator note), so the derived label is reported
  in ``progress`` but NOT written. That is a completed STEP 4 decision, not an undone one.
* ``progress_regression_blocked``: the cell says "Merged, ..." but no evidence was found, so "No work" is
  NOT written; list the Key for the operator.
* ``create``: a ``--candidate`` Key with no row; the routine creates it (Fix state New, Approval Proposed).

CLI::

    python -m scripts.triage_reconcile_batch --rows rows.json --collect --work-dir "$TMP" [--candidate KEY ...]
    python -m scripts.triage_reconcile_batch --rows rows.json --work-dir "$TMP"   # reuse collected inputs

``rows.json`` is a JSON array of objects keyed by the Notion property names (Key, Approval, Status, Progress,
Proposed plan, GitHub issue, Branch, Commit URL, PR number). Feed it the Notion rows as fetched: a Key's
``\\_`` escapes are undone and a PR number given as this repository's PR URL is read as its number, so a session
never hand-edits the rows (2026-11-14). After changing rows.json in any way, rerun with ``--collect``: a reuse whose
``prs.json`` lacks a row's recorded PR exits 1 instead of reporting that row ``recorded_pr_unconfirmed``.
Output is one JSON report on stdout. Exit 0 when every Key was decided (skips included), 1 when an input file is
unusable, the cached inputs do not cover the rows, or ``gh``/``git`` failed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from selfheal import triage_reconcile as tr

REPO = "badMade/all-stock-dca"
REPO_URL = f"https://github.com/{REPO}"
# No ``files`` here: with it, ``gh pr list --limit 150`` hit a GraphQL 504 on 2026-11-04. File lists are fetched
# per PR, only for the merged PRs that actually match a Key (see collect_files).
_PR_FIELDS = "number,url,state,mergedAt,headRefName,title,body,statusCheckRollup"
_SHA_IN_URL = re.compile(r"/commit/([0-9a-f]{40})$")
# 2026-11-14 digest: the Notion fetch handed the PR number column over as PR URLs, and the session's first
# --collect then silently backfilled none of the recorded PRs (recorded_pr_numbers skips what it cannot parse).
# A URL into THIS repository is the same fact as its number; a URL anywhere else stays an error.
_PR_IN_URL = re.compile(rf"^{re.escape(REPO_URL)}/pull/(\d+)/?$")
# Columns the routine may write, in report order. Status/Class/Severity/Fix state/Approval are never written here.
WRITABLE = ("Progress", "GitHub issue", "Branch", "Commit URL", "PR number")


class InputError(RuntimeError):
    """An input file or a gh/git call is unusable; the batch refuses rather than guessing."""


def _recorded_pr(row: Mapping[str, Any]) -> int | None:
    value = row.get("PR number")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise tr.EvidenceError(f"row PR number {value!r} is not a number")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and (url := _PR_IN_URL.fullmatch(value.strip())):
        value = url.group(1)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if not text.isdigit():
            raise tr.EvidenceError(f"row PR number {value!r} is not a number")
        value = int(text)
    if not isinstance(value, int) or value <= 0:
        raise tr.EvidenceError(f"row PR number {value!r} is not a positive integer")
    return value


def normalize_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copies of ``rows`` with Notion's markdown escaping undone in each Key.

    The Notion fetch renders ``max_orders_per_day_unenforced`` as ``max\\_orders\\_per\\_day\\_unenforced``
    (2026-11-14 digest), which the Key pattern rightly refuses. Only ``\\_`` is undone, and only in the Key:
    every other cell is compared or written back verbatim.
    """
    out = []
    for row in rows:
        copy = dict(row)
        if isinstance(copy.get("Key"), str):
            copy["Key"] = copy["Key"].replace("\\_", "_")
        out.append(copy)
    return out


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def recorded_shas(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Full SHAs of every row's recorded Commit URL, for ``git cat-file -e``."""
    shas = []
    for row in rows:
        url = row.get("Commit URL")
        match = _SHA_IN_URL.search(url) if isinstance(url, str) else None
        if match:
            shas.append(match.group(1))
    return sorted(set(shas))


def recorded_pr_numbers(rows: Iterable[Mapping[str, Any]]) -> list[int]:
    numbers = set()
    for row in rows:
        try:
            number = _recorded_pr(row)
        except tr.EvidenceError:
            continue  # reconcile() reports it against the row
        if number is not None:
            numbers.add(number)
    return sorted(numbers)


def _decide(
    row: Mapping[str, Any] | None,
    key: str,
    *,
    branches: Sequence[str],
    prs: Sequence[Mapping[str, Any]],
    commits: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    shallow: bool,
    commit_present: Mapping[str, bool],
    evidence_by_key: dict[str, tr.GitEvidence] | None = None,
    audit_not_run: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    decision: dict[str, Any] = {"key": key, "action": "update" if row is not None else "create"}
    row = row or {}
    try:
        recorded_pr = _recorded_pr(row)
        recorded_commit = row.get("Commit URL")
        recorded_commit = recorded_commit if isinstance(recorded_commit, str) and recorded_commit else None
        evidence = tr.match_git_evidence(
            key,
            repo_url=REPO_URL,
            branches=branches,
            prs=prs,
            commits=commits,
            issues=issues,
            recorded_pr_number=recorded_pr,
            recorded_commit_url=recorded_commit,
            confirmed_pr_number=tr.OPERATOR_CONFIRMED_PRS.get(key),
            shallow=shallow,
            commit_present=commit_present,
        )
    except tr.EvidenceError as exc:
        if audit_not_run is not None:
            audit_not_run.append({"key": key, "reason": str(exc)})
        return {**decision, "action": "skip", "skip_reason": f"EvidenceError: {exc}"}

    # Keep the typed evidence alongside the legacy flattened decision. The 2026-11-02 cycle built
    # tr.audit_unsupported_fixes but never wired it into a caller; on 2026-11-19 this side map lets the
    # batch audit the exact evidence even when a later guard skips writes (recorded PR or shallow commit).
    if evidence_by_key is not None:
        evidence_by_key[key] = evidence

    progress = tr.derive_progress(evidence)
    decision["progress"] = progress
    decision["evidence"] = {
        "branch": evidence.branch,
        "pr": evidence.pr.url if evidence.pr else None,
        "pr_merged": evidence.pr.merged if evidence.pr else None,
        "pr_checks": evidence.pr.checks if evidence.pr else None,
        "commit_url": evidence.commit_url,
        "issue": evidence.issue.url if evidence.issue else None,
        "mentioned_in": evidence.mentioned_in.url if evidence.mentioned_in else None,
        "commit_status": evidence.commit_status,
    }
    if evidence.recorded_pr_unconfirmed:
        return {
            **decision,
            "action": "skip",
            "skip_reason": f"recorded PR #{recorded_pr} neither matched nor cites the Key; operator to confirm",
        }
    if evidence.commit_status == "unverifiable_shallow":
        return {**decision, "action": "skip", "skip_reason": "recorded commit unverifiable in a shallow clone"}

    wanted: dict[str, Any] = {
        "Progress": progress,
        "GitHub issue": evidence.issue.url if evidence.issue else None,
        "Branch": evidence.branch,
        "Commit URL": evidence.commit_url,
        "PR number": evidence.pr.number if evidence.pr else None,
    }
    updates: dict[str, Any] = {}
    for column in WRITABLE:
        new = wanted[column]
        if _blank(new):
            continue  # never clear a recorded value because nothing was found
        current = row.get(column)
        if column == "PR number":
            same = recorded_pr == new
        else:
            same = isinstance(current, str) and current.strip() == new
        if same:
            continue
        if column == "Progress" and not tr.is_progress_label(current):
            decision["progress_note_kept"] = True
            continue
        if column == "Progress" and new == tr.PROGRESS_NO_WORK and isinstance(current, str) and current.startswith("Merged"):
            # Evidence that vanished (a PR beyond the list window, a row with no PR number recorded) must not
            # lower a merged fix to "No work"; the operator confirms instead, as for recorded_pr_unconfirmed.
            decision["progress_regression_blocked"] = True
            continue
        updates[column] = new
    decision["updates"] = updates
    return decision


def reconcile(
    rows: Sequence[Mapping[str, Any]],
    *,
    branches: Sequence[str],
    prs: Sequence[Mapping[str, Any]],
    commits: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    shallow: bool = False,
    commit_present: Mapping[str, bool] | None = None,
    candidates: Iterable[str] = (),
) -> dict[str, Any]:
    """Decide every row and candidate Key. Pure: no I/O.

    ``prs``/``issues`` are already in :func:`tr.match_git_evidence` form (see :func:`tr.gh_pr_record`).
    A malformed row Key or a duplicated Key refuses the batch (``EvidenceError``), as ``tr.approved_keys`` does.
    """
    approved = set(tr.approved_keys(rows))  # validates every Key and refuses duplicates
    by_key = {row["Key"]: row for row in rows}
    kwargs = dict(
        branches=branches, prs=prs, commits=commits, issues=issues, shallow=shallow, commit_present=commit_present or {}
    )
    evidence_by_key: dict[str, tr.GitEvidence] = {}
    audit_not_run: list[dict[str, str]] = []
    decisions = [
        _decide(row, key, **kwargs, evidence_by_key=evidence_by_key, audit_not_run=audit_not_run)
        for key, row in by_key.items()
    ]
    for key in dict.fromkeys(candidates):
        if key not in by_key:
            decisions.append(
                _decide(None, key, **kwargs, evidence_by_key=evidence_by_key, audit_not_run=audit_not_run)
            )
    for decision in decisions:
        decision["approved"] = decision["key"] in approved
    counts = {
        "rows_read": len(rows),
        "create": sum(d["action"] == "create" for d in decisions),
        "update": sum(d["action"] == "update" and bool(d.get("updates")) for d in decisions),
        "unchanged": sum(d["action"] == "update" and not d.get("updates") for d in decisions),
        "skip": sum(d["action"] == "skip" for d in decisions),
        "progress_notes_kept": sum(bool(d.get("progress_note_kept")) for d in decisions),
        "progress_regressions_blocked": sum(bool(d.get("progress_regression_blocked")) for d in decisions),
    }
    # audit_unsupported_fixes predates this batch (2026-11-02) and intentionally remains pure. Give it copies:
    # Notion's PR cell may be '#10' or a repository PR URL, and the audit contract accepts only int/None.
    # Empty defaults satisfy its text-cell validation for sparse batch callers without changing their rows.
    audit_rows: list[dict[str, Any]] = []
    audit_not_run_keys = {item["key"] for item in audit_not_run}
    for row in rows:
        # A Key whose evidence match raised has no GitEvidence: auditing it would read as "no PR, commit or issue" and
        # call a malformed row unsupported. It stays in audit_not_run only (Same Page round 1, finding 1).
        if row.get("Key") in audit_not_run_keys:
            continue
        copy = dict(row)
        try:
            copy["PR number"] = _recorded_pr(row)
        except tr.EvidenceError as exc:
            key = str(row.get("Key", ""))
            if key not in audit_not_run_keys:
                audit_not_run.append({"key": key, "reason": str(exc)})
                audit_not_run_keys.add(key)
            continue
        for column in ("Progress", "Proposed plan"):
            if not isinstance(copy.get(column), str):
                copy[column] = ""
        audit_rows.append(copy)
    unsupported_fixes = tr.audit_unsupported_fixes(audit_rows, evidence_by_key)
    unsupported_fixes["audit_not_run"] = audit_not_run
    rows_by_key = {row["Key"]: row for row in rows}
    unsupported_with_recorded_evidence = []
    unsupported_no_recorded_evidence = []
    for key in unsupported_fixes["unsupported_approved_fix"]:
        row = rows_by_key[key]
        recorded_pr = _recorded_pr(row)
        commit_url = row.get("Commit URL")
        if recorded_pr is not None or (isinstance(commit_url, str) and bool(commit_url.strip())):
            unsupported_with_recorded_evidence.append(key)
        else:
            unsupported_no_recorded_evidence.append(key)
    unsupported_fixes["unsupported_with_recorded_evidence"] = unsupported_with_recorded_evidence
    unsupported_fixes["unsupported_no_recorded_evidence"] = unsupported_no_recorded_evidence
    for key in (
        "unsupported_approved_fix",
        "unsupported_with_recorded_evidence",
        "unsupported_no_recorded_evidence",
        "brief_acceptance_not_demonstrated",
        "recorded_pr_not_found",
        "audit_not_run",
    ):
        counts[key] = len(unsupported_fixes[key])
    approved_rows = [
        {"key": key, "progress_cell": by_key[key].get("Progress"), "derived": d.get("progress")}
        for d in decisions
        if (key := d["key"]) in approved
    ]
    return {
        "counts": counts,
        "decisions": decisions,
        "approved": approved_rows,
        "approved_groups": approved_groups(approved_rows),
        "unsupported_fixes": unsupported_fixes,
    }


def approved_groups(approved_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Group approved rows by derived Progress label, with counts computed here and never by the model.

    2026-11-16: the digest stated 34 approved rows (right) but split them "No work (10)" over a list of 9 and
    "docs only (15)" over a list of 16, because the model counted by hand. Every count the digest prints for
    this section now comes from ``total`` and each group's ``count``, which are ``len()`` of the same lists the
    keys come from, so a stated count can never disagree with its list. A row whose label could not be derived
    is grouped under ``"No work"`` only when the batch said so; otherwise under ``"(no derived label)"``.
    """
    groups: dict[str, list[str]] = {}
    for row in approved_rows:
        groups.setdefault(row.get("derived") or "(no derived label)", []).append(row["key"])
    ordered = [{"label": label, "count": len(keys), "keys": sorted(keys)} for label, keys in sorted(groups.items())]
    total = len(approved_rows)
    assert sum(g["count"] for g in ordered) == total  # grouping must partition the approved rows exactly
    return {"total": total, "groups": ordered}


# ---------------------------------------------------------------------------------------------------------------
# --collect: read-only gh/git, results cached in --work-dir so a rerun needs no network.


def _run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(list(cmd), capture_output=True, text=True, shell=False)  # nosec B603 - fixed argv
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        raise InputError(f"{' '.join(cmd[:3])} failed (exit {result.returncode}): {tail[0]}")
    return result


def collect(work_dir: Path, rows: Sequence[Mapping[str, Any]], *, pr_limit: int = 150) -> None:
    """Write prs.json, issues.json, branches.json, commits.json, shallow.json, commit_present.json.

    Then :func:`collect_files` adds pr_files.json for the docs_only rule.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    prs = json.loads(_run(["gh", "pr", "list", "-R", REPO, "--state", "all", "--limit", str(pr_limit), "--json", _PR_FIELDS]).stdout)
    have = {pr["number"] for pr in prs}
    for number in recorded_pr_numbers(rows):
        if number not in have:  # an old fix beyond the list window must not read as recorded_pr_unconfirmed
            prs.append(json.loads(_run(["gh", "pr", "view", str(number), "-R", REPO, "--json", _PR_FIELDS]).stdout))
    issues = json.loads(
        _run(["gh", "issue", "list", "-R", REPO, "--state", "all", "--limit", "100", "--json", "number,url,state,title,body"]).stdout
    )
    branches = [
        line.strip()
        for line in _run(["git", "branch", "-r", "--format=%(refname:lstrip=3)"]).stdout.splitlines()
        if line.strip()
    ]
    commits = []
    for line in _run(["git", "log", "origin/main", "-n", "300", "--format=%H%x09%s"]).stdout.splitlines():
        sha, _, subject = line.partition("\t")
        commits.append({"sha": sha, "url": f"{REPO_URL}/commit/{sha}", "message": subject})
    shallow = _run(["git", "rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"
    present = {sha: _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0 for sha in recorded_shas(rows)}
    for name, payload in (
        ("prs.json", prs),
        ("issues.json", issues),
        ("branches.json", branches),
        ("commits.json", commits),
        ("shallow.json", shallow),
        ("commit_present.json", present),
    ):
        (work_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def collect_files(work_dir: Path, rows: Sequence[Mapping[str, Any]], candidates: Iterable[str]) -> None:
    """Write pr_files.json: changed paths of every merged PR the first pass matched to a Key."""
    first = run(rows, work_dir, candidates)
    numbers = sorted(
        {
            int(d["evidence"]["pr"].rsplit("/", 1)[-1])
            for d in first["decisions"]
            if d.get("evidence") and d["evidence"]["pr"] and d["evidence"]["pr_merged"]
        }
    )
    files = {}
    for number in numbers:
        view = json.loads(_run(["gh", "pr", "view", str(number), "-R", REPO, "--json", "files"]).stdout)
        files[str(number)] = [item["path"] for item in view.get("files") or []]
    (work_dir / "pr_files.json").write_text(json.dumps(files), encoding="utf-8")


def _load(work_dir: Path, name: str) -> Any:
    path = work_dir / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read {name}: {exc}") from exc


def run(rows: Sequence[Mapping[str, Any]], work_dir: Path, candidates: Iterable[str]) -> dict[str, Any]:
    raw_prs = _load(work_dir, "prs.json")
    # Fail closed on a work dir collected for DIFFERENT rows. 2026-11-14: the first --collect saw PR URLs it could
    # not parse, so it backfilled none of the recorded PRs beyond the 150-PR list window (#1745, #1752, #2008, ...);
    # the session then fixed rows.json and reused that cache, and 21 rows whose recorded PR plainly cites the Key
    # came out recorded_pr_unconfirmed. A recorded PR absent from prs.json is a stale cache, never evidence.
    have = {raw.get("number") for raw in raw_prs if isinstance(raw, Mapping)}
    missing = [number for number in recorded_pr_numbers(rows) if number not in have]
    if missing:
        listed = ", ".join(f"#{number}" for number in missing)
        raise InputError(f"prs.json lacks recorded PR(s) {listed}: it was collected for other rows; rerun with --collect")
    pr_files = _load(work_dir, "pr_files.json") if (work_dir / "pr_files.json").exists() else {}
    for raw in raw_prs:
        paths = pr_files.get(str(raw.get("number")))
        if paths is not None:
            raw["files"] = [{"path": path} for path in paths]
    try:
        prs = [tr.gh_pr_record(raw) for raw in raw_prs]
        issues = [tr.gh_issue_record(raw) for raw in _load(work_dir, "issues.json")]
    except tr.EvidenceError as exc:
        raise InputError(f"gh record refused: {exc}") from exc
    # Newest first: tr takes the first matching commit, and gh pr list is already newest first.
    return reconcile(
        rows,
        branches=_load(work_dir, "branches.json"),
        prs=prs,
        commits=_load(work_dir, "commits.json"),
        issues=issues,
        shallow=bool(_load(work_dir, "shallow.json")),
        commit_present=_load(work_dir, "commit_present.json"),
        candidates=candidates,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", required=True, type=Path, help="JSON array of DCA Issues rows")
    parser.add_argument("--work-dir", required=True, type=Path, help="where collected gh/git inputs live")
    parser.add_argument("--collect", action="store_true", help="fetch inputs with read-only gh/git first")
    parser.add_argument("--candidate", action="append", default=[], help="new failure Key from STEP 1")
    args = parser.parse_args(argv)
    try:
        rows = json.loads(args.rows.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise InputError("--rows must hold a JSON array")
        rows = normalize_rows(rows)
        if args.collect:
            collect(args.work_dir, rows)
            collect_files(args.work_dir, rows, args.candidate)
        report = run(rows, args.work_dir, args.candidate)
    except (InputError, OSError, json.JSONDecodeError, tr.EvidenceError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stdout)
        return 1
    json.dump(report, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
