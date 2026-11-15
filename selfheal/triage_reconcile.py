"""Propose-only triage reconciler: match DCA Issues rows to git evidence.

The scheduled routine, which alone holds the GitHub and Notion connector
access, hands this module records it has already fetched, as flat mappings.
For each DCA Issues ``Key`` the module finds the matching branch, pull request,
commit and GitHub issue, derives a ``Progress`` label from that evidence, and
reports which rows the operator has approved for action.

It never decides to write code. Approval is a Notion column the operator flips,
and anything that is not exactly :data:`APPROVED` is treated as not approved.
Every input is validated and a bad one raises :class:`EvidenceError`, so a
malformed record can neither release a row nor put a foreign link into Notion.

Standard library only. No I/O, no network, no subprocesses, no secrets, and no
Notion identifiers: the caller owns all of that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# One definition of what a Key is. It doubles as the branch suffix and the
# pull-request dedupe token, so the reconciler must never accept a value the
# issue loop would refuse (or the reverse).
from selfheal.notion_issue_loop import _KEY_PATTERN

__all__ = [
    "APPROVED",
    "BRANCH_PREFIX",
    "CHECKS_GREEN",
    "CHECKS_PENDING",
    "CHECKS_RED",
    "PROGRESS_BRANCH_ONLY",
    "PROGRESS_CLOSED_UNMERGED",
    "PROGRESS_LABELS",
    "PROGRESS_MERGED_DOCS_ONLY",
    "PROGRESS_MERGED_GREEN",
    "PROGRESS_MERGED_PENDING",
    "PROGRESS_MERGED_RED",
    "PROGRESS_NO_WORK",
    "PROGRESS_PR_CHECKS_RED",
    "PROGRESS_PR_OPEN",
    "EvidenceError",
    "GitEvidence",
    "IssueRef",
    "PullRequestRef",
    "approved_keys",
    "audit_unsupported_fixes",
    "blind_run_reason",
    "derive_progress",
    "gh_issue_record",
    "gh_pr_record",
    "is_progress_label",
    "match_git_evidence",
]

#: The only Approval value that releases a row. Compared exactly: a different
#: case, stray whitespace or a checkbox-shaped value never counts.
APPROVED = "Approved"

#: The branch a repair for ``Key`` is pushed to.
BRANCH_PREFIX = "selfheal/"

#: Check status as the caller maps it. Anything else is refused, not guessed.
CHECKS_GREEN = "green"
CHECKS_RED = "red"
CHECKS_PENDING = "pending"
_CHECKS = frozenset({CHECKS_GREEN, CHECKS_RED, CHECKS_PENDING})
_STATES = frozenset({"open", "closed"})

PROGRESS_NO_WORK = "No work"
PROGRESS_BRANCH_ONLY = "Branch only"
PROGRESS_PR_OPEN = "PR open"
PROGRESS_PR_CHECKS_RED = "PR checks red"
PROGRESS_MERGED_GREEN = "Merged, checks green"
PROGRESS_MERGED_PENDING = "Merged, checks pending"
PROGRESS_MERGED_RED = "Merged, checks red"
# A merged PR whose diff touched only documentation drafted a fix; it did not apply one.
PROGRESS_MERGED_DOCS_ONLY = "Merged, docs only"
PROGRESS_CLOSED_UNMERGED = "Closed, not merged"

#: Every label :func:`derive_progress` can return. A Progress cell holding anything else carries an
#: operator-written note (the 2026-11-04 digest found "Applied to digest routine..." style notes on most
#: Approved rows); the routine must not overwrite it, see :func:`is_progress_label`.
PROGRESS_LABELS = frozenset(
    {
        PROGRESS_NO_WORK,
        PROGRESS_BRANCH_ONLY,
        PROGRESS_PR_OPEN,
        PROGRESS_PR_CHECKS_RED,
        PROGRESS_MERGED_GREEN,
        PROGRESS_MERGED_PENDING,
        PROGRESS_MERGED_RED,
        PROGRESS_MERGED_DOCS_ONLY,
        PROGRESS_CLOSED_UNMERGED,
    }
)

# Documentation-only paths, the routine's STEP 2 rule (docs/, claude/, *.md, *.txt).
_DOC_PREFIXES = ("docs/", "claude/")
_DOC_SUFFIXES = (".md", ".txt")
# statusCheckRollup conclusions/states that make a PR red. Anything not yet completed is pending.
_RED_CONCLUSIONS = frozenset({"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"})
_DONE_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

_NOT_CHANGED = re.compile(r"\bnot\s+changed\b", re.IGNORECASE)
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
# GitHub's merge-commit subject. Anchored and requiring " from ", so a squash subject that merely ends "(#N)" or
# quotes another merge is never read as one.
_MERGE_SUBJECT = re.compile(r"Merge pull request #(\d+) from \S")
_REPO_URL_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class EvidenceError(ValueError):
    """An input record is malformed, or points outside the repository."""


def blind_run_reason(resolved_root: str, root_exists: bool, stream_dirs: Iterable[str]) -> str | None:
    """Explain why the cron tracking root cannot support a trustworthy triage run.

    The caller supplies facts gathered from the resolved root; this helper does
    no filesystem access. ``root_exists`` means the root exists as a directory.
    """
    if not root_exists:
        return f"cron tracking root is not an existing directory: {resolved_root}"
    if not any(stream_dirs):
        return f"cron tracking root has no stream subdirectory: {resolved_root}"
    return None


@dataclass(frozen=True)
class PullRequestRef:
    """A pull request as the reconciler records it."""

    number: int
    url: str
    state: str
    merged: bool
    checks: str
    docs_only: bool = False


@dataclass(frozen=True)
class IssueRef:
    """A GitHub issue as the reconciler records it."""

    number: int
    url: str
    state: str


@dataclass(frozen=True)
class GitEvidence:
    """Everything found for one Key; a field is ``None`` when nothing matched."""

    branch: str | None = None
    pr: PullRequestRef | None = None
    commit_url: str | None = None
    issue: IssueRef | None = None
    #: The newest PR that cites the Key only in its body. Recorded for the operator, never counted as a fix.
    mentioned_in: PullRequestRef | None = None
    #: True when the row already records a PR that this evidence neither matched nor found citing the Key. The
    #: caller must leave the row alone rather than lower it: the 2026-10-26 run could not confirm
    #: ``run_states_unclassifiable``'s recorded PR #1514 and would otherwise have overwritten it with "No work".
    recorded_pr_unconfirmed: bool = False
    #: A recorded commit could not be inspected because the caller's clone is shallow.
    commit_status: str | None = None


def _check_key(key: object) -> str:
    if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise EvidenceError(f"Key {key!r} is not a snake_case identifier")
    return key


def _mentions(key: str, text: str) -> bool:
    """True when ``key`` appears as a whole token, so ``market_data`` never claims ``market_data_unavailable``."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text) is not None


def _mapping(item: object, what: str) -> Mapping[str, object]:
    if not isinstance(item, Mapping):
        raise EvidenceError(f"{what} must be a mapping, got {type(item).__name__}")
    return item


def _text(record: Mapping[str, object], field: str, what: str, *, required: bool = False) -> str:
    value = record.get(field, None if required else "")
    if not isinstance(value, str) or (required and not value):
        raise EvidenceError(f"{what} {field!r} must be a {'non-empty ' if required else ''}string")
    return value


def _positive_int(record: Mapping[str, object], what: str) -> int:
    number = record.get("number")
    # bool is an int subclass, so it has to be refused by name.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise EvidenceError(f"{what} number {number!r} must be a positive integer")
    return number


def _own_url(record: Mapping[str, object], expected: str, what: str) -> str:
    """Only a link into this repository, built from the record's own identity, may be recorded."""
    url = record.get("url")
    if url != expected:
        raise EvidenceError(f"{what} url {url!r} is not {expected!r}")
    return expected


def _parse_pr(raw: object, repo_url: str) -> tuple[PullRequestRef, str, str, str]:
    record = _mapping(raw, "pull request")
    number = _positive_int(record, "pull request")
    state = _text(record, "state", "pull request", required=True)
    checks = _text(record, "checks", "pull request", required=True)
    merged = record.get("merged")
    if state not in _STATES:
        raise EvidenceError(f"pull request state {state!r} is not one of {sorted(_STATES)}")
    if checks not in _CHECKS:
        raise EvidenceError(f"pull request checks {checks!r} is not one of {sorted(_CHECKS)}")
    if not isinstance(merged, bool):
        raise EvidenceError("pull request 'merged' must be a boolean")
    docs_only = record.get("docs_only", False)
    if not isinstance(docs_only, bool):
        raise EvidenceError("pull request 'docs_only' must be a boolean")
    url = _own_url(record, f"{repo_url}/pull/{number}", "pull request")
    # A line saying "Not changed" names keys the PR deliberately left alone (PR #1642 listed eight), so
    # a mention there is evidence against a fix, not for one.
    body = _without_not_changed(_text(record, "body", "pull request"))
    title = _text(record, "title", "pull request")
    searchable = f"{title}\n{body}"
    ref = PullRequestRef(number=number, url=url, state=state, merged=merged, checks=checks, docs_only=docs_only)
    return ref, _text(record, "head_ref", "pull request"), title, searchable


def _parse_issue(raw: object, repo_url: str) -> tuple[IssueRef, str]:
    record = _mapping(raw, "issue")
    number = _positive_int(record, "issue")
    state = _text(record, "state", "issue", required=True)
    if state not in _STATES:
        raise EvidenceError(f"issue state {state!r} is not one of {sorted(_STATES)}")
    url = _own_url(record, f"{repo_url}/issues/{number}", "issue")
    return IssueRef(number=number, url=url, state=state), _text(record, "title", "issue")


def _without_not_changed(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _NOT_CHANGED.search(line))


def _parse_commit(raw: object, repo_url: str) -> tuple[str, str, str]:
    """Return the commit URL, its subject line and its whole message minus "Not changed" lines."""
    record = _mapping(raw, "commit")
    sha = _text(record, "sha", "commit", required=True)
    if not _SHA_PATTERN.fullmatch(sha):
        raise EvidenceError(f"commit sha {sha!r} is not a full lower-case 40-character SHA")
    url = _own_url(record, f"{repo_url}/commit/{sha}", "commit")
    message = _text(record, "message", "commit")
    subject = message.splitlines()[0] if message else ""
    return url, subject, _without_not_changed(message)


def match_git_evidence(
    key: str,
    *,
    repo_url: str,
    branches: Iterable[str],
    prs: Iterable[Mapping[str, object]],
    commits: Iterable[Mapping[str, object]],
    issues: Iterable[Mapping[str, object]],
    recorded_pr_number: int | None = None,
    recorded_commit_url: str | None = None,
    shallow: bool = False,
    commit_present: Mapping[str, bool] | None = None,
) -> GitEvidence:
    """Match already-fetched git records to one DCA Issues Key.

    A branch matches only as exactly ``selfheal/<key>``. A pull request matches
    by that head ref, or by the key appearing as a whole token in its title; the
    newest (highest number) match wins. A pull request that cites the key only
    in its body is not a fix: it is returned as ``mentioned_in`` and never
    reaches :func:`derive_progress`. A commit matches the same way, by the key
    in its subject line (a squash merge's subject is the PR title); the first
    match in the order given wins, so the caller passes them newest first. An issue
    matches by the key in its title, newest first. Every record is validated,
    matching or not, so one bad record refuses the batch.

    Args:
        key: The DCA Issues Key, a snake_case identifier.
        repo_url: The repository URL, for example ``https://github.com/owner/repo``.
            Every recorded link must point into it.
        branches: Branch names.
        prs: Pull requests as mappings with ``number``, ``url``, ``state``,
            ``merged``, ``head_ref``, ``title``, ``body`` and ``checks``
            (``green``, ``red`` or ``pending``); optional ``docs_only`` (bool,
            true when the diff touched only documentation).
        commits: Commits as mappings with ``sha``, ``url`` and ``message``.
        issues: GitHub issues as mappings with ``number``, ``url``, ``state``
            and ``title``.
        recorded_pr_number: The PR number already on the Notion row, if any. A
            body-only citation of that exact PR keeps counting, so tightening
            the matcher never regresses a link the operator already holds
            (the 2026-10-22 run sent five merged rows to "No work"). When no PR
            is found at all, ``recorded_pr_unconfirmed`` is set instead of
            silently lowering the row.
        recorded_commit_url: The Commit URL already on the Notion row, if any.
            When it is among ``commits`` and its message still cites the key,
            it wins over any other match, so a newer commit that merely
            discusses the key never displaces the real fix commit.
        shallow: Whether the caller's repository is shallow.
        commit_present: Per-SHA results of ``git cat-file -e <sha>^{commit}``,
            queried by the caller for recorded commits. Never infer absence from
            the bounded recent-commit listing.

    Returns:
        The frozen :class:`GitEvidence`; unmatched fields are ``None``.

    Raises:
        EvidenceError: The key, the repository URL or any record is malformed,
            or a link points outside the repository.
    """
    _check_key(key)
    if not isinstance(shallow, bool):
        raise EvidenceError("shallow must be a boolean")
    if commit_present is not None:
        _mapping(commit_present, "commit_present")
        for sha, present in commit_present.items():
            if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
                raise EvidenceError(f"commit_present key {sha!r} is not a full lower-case 40-character SHA")
            if not isinstance(present, bool):
                raise EvidenceError(f"commit_present value for {sha!r} must be a boolean")
    if not isinstance(repo_url, str) or not _REPO_URL_PATTERN.fullmatch(repo_url):
        raise EvidenceError(f"repo_url {repo_url!r} is not an https://github.com/<owner>/<repo> URL")

    branch_names = list(branches)
    if not all(isinstance(name, str) for name in branch_names):
        raise EvidenceError("branches must be strings")
    expected_branch = f"{BRANCH_PREFIX}{key}"
    branch = expected_branch if expected_branch in branch_names else None

    # Only a PR that is *about* the Key (head ref or title) counts as work on it. A body-only citation does not:
    # PR #1873 cited market_data_unavailable while calling its root cause unconfirmed, and as the newest match it
    # regressed a merged fix to "PR open"; batch PRs (#1750, #1751) list many Keys in prose without fixing them.
    # Those are kept in ``mentioned_in`` so the operator can still see them. Newest wins within each group.
    about: list[PullRequestRef] = []
    cited: list[PullRequestRef] = []
    for ref, head_ref, title, searchable in (_parse_pr(raw, repo_url) for raw in prs):
        if head_ref == expected_branch or _mentions(key, title):
            about.append(ref)
        elif _mentions(key, searchable):
            cited.append(ref)
    pr = max(about, key=lambda ref: ref.number, default=None)
    # Prior evidence survives: #1704/#1745/#1533/#1514 are real merged fixes that cite their Key only in the body.
    # A title/head-ref match still wins; only the "nothing found" case falls back to the recorded PR.
    if pr is None and recorded_pr_number is not None:
        pr = next((ref for ref in cited if ref.number == recorded_pr_number), None)
        if pr is not None:
            cited = [ref for ref in cited if ref.number != recorded_pr_number]
    mentioned_in = max(cited, key=lambda ref: ref.number, default=None)

    if recorded_commit_url is not None and not isinstance(recorded_commit_url, str):
        raise EvidenceError("recorded_commit_url must be a string")
    # Same rule as PRs. Matching anywhere in the message let the squash commit of #1930 (205ae99e), a triage-tool
    # change whose body only discussed five Keys, claim all five rows, four of which already held their real fix
    # commit (2026-10-26 digest). The subject line is the squash's PR title, so this mirrors the title rule.
    parsed_commits = [_parse_commit(raw, repo_url) for raw in commits]

    def _merge_of_matched_pr(subject: str) -> bool:
        # 2026-11-13/14 digests: the operator moved two rows' Commit URL to the merge commit of their matched fix PR
        # (632fd257 for #2224, b90e6025 for #2221). A "Merge pull request #N from ..." subject never names the Key,
        # so the recorded value lost to the branch commit every day and the digest had to hold both rows by hand.
        # The merge commit of the very PR this function matched to the Key is the same fix, so it pins the row too.
        found = _MERGE_SUBJECT.match(subject)
        return pr is not None and found is not None and int(found.group(1)) == pr.number

    commit_url = next(
        (
            url
            for url, subject, message in parsed_commits
            if url == recorded_commit_url and (_mentions(key, message) or _merge_of_matched_pr(subject))
        ),
        None,
    ) or next((url for url, subject, _message in parsed_commits if _mentions(key, subject)), None)
    commit_status = None
    if recorded_commit_url is not None and shallow and commit_present is not None:
        recorded_sha = recorded_commit_url.rsplit("/", 1)[-1]
        if commit_present.get(recorded_sha) is False:
            commit_url = None
            commit_status = "unverifiable_shallow"

    matching_issues = [ref for ref, title in (_parse_issue(raw, repo_url) for raw in issues) if _mentions(key, title)]
    issue = max(matching_issues, key=lambda ref: ref.number, default=None)

    return GitEvidence(
        branch=branch,
        pr=pr,
        commit_url=commit_url,
        issue=issue,
        mentioned_in=mentioned_in,
        recorded_pr_unconfirmed=pr is None and recorded_pr_number is not None,
        commit_status=commit_status,
    )


def is_progress_label(value: object) -> bool:
    """Whether a Progress cell may be overwritten: empty, or exactly one of :data:`PROGRESS_LABELS`.

    Free text is an operator note. Overwriting it loses the note, so the routine leaves the cell alone and
    reports the derived label instead (the 2026-11-04 run handled this by hand and then called STEP 4 undone).
    """
    if value is None:
        return True
    return isinstance(value, str) and (value.strip() == "" or value in PROGRESS_LABELS)


def _gh_checks(rollup: object) -> str:
    if rollup is None:
        return CHECKS_PENDING
    if not isinstance(rollup, list):
        raise EvidenceError("statusCheckRollup must be a list")
    if not rollup:
        return CHECKS_PENDING
    pending = False
    for item in rollup:
        check = _mapping(item, "status check")
        # CheckRun rows carry status/conclusion; StatusContext rows carry state.
        conclusion = check.get("conclusion") or check.get("state") or ""
        conclusion = conclusion.upper() if isinstance(conclusion, str) else ""
        if conclusion in _RED_CONCLUSIONS:
            return CHECKS_RED
        status = check.get("status")
        completed = status is None or (isinstance(status, str) and status.upper() == "COMPLETED")
        if not completed or conclusion not in _DONE_OK:
            pending = True
    return CHECKS_PENDING if pending else CHECKS_GREEN


def gh_pr_record(raw: object) -> dict[str, object]:
    """Convert one ``gh pr list/view --json`` object into the PR mapping :func:`match_git_evidence` takes.

    ``gh`` reports ``state`` as ``OPEN``, ``CLOSED`` or ``MERGED``; the reconciler takes ``open``/``closed``
    plus a separate ``merged`` flag, and refused the raw value on 2026-11-04 ("State must be open/closed").
    ``merged`` comes from ``mergedAt``, which ``gh`` reports reliably. ``docs_only`` is set only when the
    record carries ``files`` (``--json ...,files``), so a missing file list never claims docs-only.

    Requires ``number``, ``url``, ``state``, ``headRefName``, ``title``, ``body``, ``mergedAt`` and
    ``statusCheckRollup``.

    Raises:
        EvidenceError: A field is missing or has an unexpected value.
    """
    record = _mapping(raw, "gh pull request")
    for field in ("number", "url", "state", "headRefName", "title", "mergedAt", "statusCheckRollup"):
        if field not in record:
            raise EvidenceError(f"gh pull request lacks {field!r}")
    raw_state = record["state"]
    if raw_state not in ("OPEN", "CLOSED", "MERGED"):
        raise EvidenceError(f"gh pull request state {raw_state!r} is not OPEN, CLOSED or MERGED")
    merged_at = record["mergedAt"]
    merged = isinstance(merged_at, str) and merged_at != ""
    if merged != (raw_state == "MERGED"):
        raise EvidenceError(f"gh pull request state {raw_state!r} disagrees with mergedAt {merged_at!r}")
    out: dict[str, object] = {
        "number": record["number"],
        "url": record["url"],
        "state": "open" if raw_state == "OPEN" else "closed",
        "merged": merged,
        "checks": _gh_checks(record["statusCheckRollup"]),
        "head_ref": record["headRefName"],
        "title": record["title"],
        "body": record.get("body") or "",
    }
    files = record.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise EvidenceError("gh pull request files must be a list")
        paths = [_text(_mapping(item, "gh file"), "path", "gh file", required=True) for item in files]
        out["docs_only"] = bool(paths) and all(
            path.startswith(_DOC_PREFIXES) or path.endswith(_DOC_SUFFIXES) for path in paths
        )
    return out


def gh_issue_record(raw: object) -> dict[str, object]:
    """Convert one ``gh issue list --json number,url,state,title,body`` object (state ``OPEN``/``CLOSED``).

    Raises:
        EvidenceError: A field is missing or the state is not ``OPEN`` or ``CLOSED``.
    """
    record = _mapping(raw, "gh issue")
    for field in ("number", "url", "state", "title"):
        if field not in record:
            raise EvidenceError(f"gh issue lacks {field!r}")
    if record["state"] not in ("OPEN", "CLOSED"):
        raise EvidenceError(f"gh issue state {record['state']!r} is not OPEN or CLOSED")
    return {
        "number": record["number"],
        "url": record["url"],
        "state": record["state"].lower(),
        "title": record["title"],
        "body": record.get("body") or "",
    }


def derive_progress(evidence: GitEvidence) -> str:
    """The ``Progress`` label for the evidence; a pull request outranks a bare branch."""
    pr = evidence.pr
    if pr is not None:
        if pr.merged:
            if pr.docs_only:
                return PROGRESS_MERGED_DOCS_ONLY
            return {
                CHECKS_GREEN: PROGRESS_MERGED_GREEN,
                CHECKS_RED: PROGRESS_MERGED_RED,
                CHECKS_PENDING: PROGRESS_MERGED_PENDING,
            }[pr.checks]
        if pr.state == "closed":
            return PROGRESS_CLOSED_UNMERGED
        return PROGRESS_PR_CHECKS_RED if pr.checks == CHECKS_RED else PROGRESS_PR_OPEN
    if evidence.branch is not None:
        return PROGRESS_BRANCH_ONLY
    return PROGRESS_NO_WORK


def approved_keys(rows: Iterable[Mapping[str, object]]) -> list[str]:
    """Keys of the rows whose ``Approval`` is exactly ``Approved``, in row order.

    Fails closed: a malformed Key, a non-mapping row or a duplicated Key refuses
    the whole batch, because a released row must be unambiguous.

    Raises:
        EvidenceError: A row is not a mapping, a Key is invalid, or a Key repeats.
    """
    seen: set[str] = set()
    released: list[str] = []
    for row in rows:
        record = _mapping(row, "row")
        key = _check_key(record.get("Key"))
        if key in seen:
            raise EvidenceError(f"Key {key!r} appears more than once")
        seen.add(key)
        approval = record.get("Approval")
        if isinstance(approval, str) and approval == APPROVED:
            released.append(key)
    return released


def audit_unsupported_fixes(
    rows: Iterable[Mapping[str, object]], evidence: Mapping[str, GitEvidence]
) -> dict[str, list[str]]:
    """Return sorted Keys whose recorded fix claims are not supported by evidence.

    The result has three sorted Key lists: ``unsupported_approved_fix`` for
    approved repaired or in-review rows without matching PR, commit or issue
    evidence; ``brief_acceptance_not_demonstrated`` for monitoring rows whose
    Progress or Proposed plan says that acceptance was not demonstrated; and
    ``recorded_pr_not_found`` when a recorded PR number is absent from the
    matching evidence.

    Raises:
        EvidenceError: A row, key, PR number or evidence record is malformed,
            or a Key appears more than once.
    """
    checked_evidence: dict[str, GitEvidence] = {}
    for raw_key, item in evidence.items():
        key = _check_key(raw_key)
        if key in checked_evidence:
            raise EvidenceError(f"Key {key!r} appears more than once in evidence")
        if not isinstance(item, GitEvidence):
            raise EvidenceError(f"evidence for Key {key!r} must be GitEvidence")
        checked_evidence[key] = item

    categories: dict[str, list[str]] = {
        "unsupported_approved_fix": [],
        "brief_acceptance_not_demonstrated": [],
        "recorded_pr_not_found": [],
    }
    seen: set[str] = set()
    for raw_row in rows:
        row = _mapping(raw_row, "row")
        key = _check_key(row.get("Key"))
        if key in seen:
            raise EvidenceError(f"Key {key!r} appears more than once")
        seen.add(key)

        item = checked_evidence.get(key)
        approval = row.get("Approval")
        fix_state = row.get("Fix state")
        if approval == APPROVED and fix_state in ("Repaired", "In review") and (
            item is None or not any((item.pr, item.commit_url, item.issue))
        ):
            categories["unsupported_approved_fix"].append(key)

        progress = _text(row, "Progress", "row")
        proposed_plan = _text(row, "Proposed plan", "row")
        if row.get("Status") == "Monitoring" and "brief acceptance not demonstrated" in (
            f"{progress}\n{proposed_plan}".lower()
        ):
            categories["brief_acceptance_not_demonstrated"].append(key)

        recorded_pr = row.get("PR number")
        if recorded_pr is not None and recorded_pr != "":
            if isinstance(recorded_pr, bool) or not isinstance(recorded_pr, int) or recorded_pr <= 0:
                raise EvidenceError(f"row PR number {recorded_pr!r} must be a positive integer")
            if item is None or item.pr is None or item.pr.number != recorded_pr:
                categories["recorded_pr_not_found"].append(key)

    for keys in categories.values():
        keys.sort()
    return categories
