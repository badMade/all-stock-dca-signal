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
    "derive_progress",
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

_NOT_CHANGED = re.compile(r"\bnot\s+changed\b", re.IGNORECASE)
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_REPO_URL_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class EvidenceError(ValueError):
    """An input record is malformed, or points outside the repository."""


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
    body = "\n".join(
        line for line in _text(record, "body", "pull request").splitlines() if not _NOT_CHANGED.search(line)
    )
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


def _parse_commit(raw: object, repo_url: str) -> tuple[str, str]:
    record = _mapping(raw, "commit")
    sha = _text(record, "sha", "commit", required=True)
    if not _SHA_PATTERN.fullmatch(sha):
        raise EvidenceError(f"commit sha {sha!r} is not a full lower-case 40-character SHA")
    url = _own_url(record, f"{repo_url}/commit/{sha}", "commit")
    return url, _text(record, "message", "commit")


def match_git_evidence(
    key: str,
    *,
    repo_url: str,
    branches: Iterable[str],
    prs: Iterable[Mapping[str, object]],
    commits: Iterable[Mapping[str, object]],
    issues: Iterable[Mapping[str, object]],
) -> GitEvidence:
    """Match already-fetched git records to one DCA Issues Key.

    A branch matches only as exactly ``selfheal/<key>``. A pull request matches
    by that head ref, or by the key appearing as a whole token in its title; the
    newest (highest number) match wins. A pull request that cites the key only
    in its body is not a fix: it is returned as ``mentioned_in`` and never
    reaches :func:`derive_progress`. The first matching commit in
    the order given wins, so the caller passes them newest first. An issue
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

    Returns:
        The frozen :class:`GitEvidence`; unmatched fields are ``None``.

    Raises:
        EvidenceError: The key, the repository URL or any record is malformed,
            or a link points outside the repository.
    """
    _check_key(key)
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
    mentioned_in = max(cited, key=lambda ref: ref.number, default=None)

    commit_url = next(
        (url for url, message in (_parse_commit(raw, repo_url) for raw in commits) if _mentions(key, message)),
        None,
    )

    matching_issues = [ref for ref, title in (_parse_issue(raw, repo_url) for raw in issues) if _mentions(key, title)]
    issue = max(matching_issues, key=lambda ref: ref.number, default=None)

    return GitEvidence(branch=branch, pr=pr, commit_url=commit_url, issue=issue, mentioned_in=mentioned_in)


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
