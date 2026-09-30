"""Tests for the propose-only triage reconciler.

Written before ``selfheal/triage_reconcile.py`` exists, per this repository's
test-first workflow: the behaviour comes from the routine's contract, never from
an implementation.

The contract, in one paragraph. The scheduled routine (which alone holds
GitHub and Notion connector access) hands this module already-fetched records
as flat mappings. The module matches each DCA Issues ``Key`` to git evidence
(branches, pull requests, commits, GitHub issues), derives a ``Progress`` label
from that evidence, and reports which rows the operator has approved for
action. It performs no I/O, holds no Notion identifiers, and never decides to
write code: approval is a Notion column the operator flips, and anything that is
not exactly ``Approved`` is treated as not approved.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from selfheal import triage_reconcile as tr

KEY = "market_data_unavailable"
REPO_URL = "https://github.com/badMade/all-stock-dca"


def _pr(number: int = 101, **overrides: object) -> dict[str, object]:
    pr: dict[str, object] = {
        "number": number,
        "url": f"{REPO_URL}/pull/{number}",
        "state": "open",
        "merged": False,
        "head_ref": f"selfheal/{KEY}",
        "title": "Fix market data",
        "body": "",
        "checks": "pending",
    }
    pr.update(overrides)
    return pr


def _issue(number: int = 7, **overrides: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "number": number,
        "url": f"{REPO_URL}/issues/{number}",
        "state": "open",
        "title": f"{KEY} keeps firing",
        "body": "",
    }
    issue.update(overrides)
    return issue


def _match(**evidence: object) -> tr.GitEvidence:
    base: dict[str, object] = {"branches": [], "prs": [], "commits": [], "issues": []}
    base.update(evidence)
    return tr.match_git_evidence(KEY, repo_url=REPO_URL, **base)  # type: ignore[arg-type]


# --- Matching evidence to a Key ---------------------------------------------


def test_no_evidence_matches_nothing() -> None:
    ev = _match()
    assert ev.branch is None
    assert ev.pr is None
    assert ev.commit_url is None
    assert ev.issue is None


def test_branch_matches_by_selfheal_prefix() -> None:
    ev = _match(branches=[f"selfheal/{KEY}", "selfheal/other_key"])
    assert ev.branch == f"selfheal/{KEY}"


def test_key_prefix_of_a_longer_key_does_not_match() -> None:
    # market_data must not claim the branch of market_data_unavailable.
    ev = tr.match_git_evidence(
        "market_data", repo_url=REPO_URL, branches=[f"selfheal/{KEY}"], prs=[], commits=[], issues=[]
    )
    assert ev.branch is None


def test_pr_matches_by_head_ref_and_records_number_and_url() -> None:
    ev = _match(prs=[_pr(101)])
    assert ev.pr is not None
    assert ev.pr.number == 101
    assert ev.pr.url == f"{REPO_URL}/pull/101"


def test_pr_matches_by_key_mention_in_body_when_head_ref_differs() -> None:
    ev = _match(prs=[_pr(102, head_ref="claude/some-branch", body=f"Fixes {KEY}")])
    assert ev.pr is not None and ev.pr.number == 102


def test_newest_pr_wins_when_several_match() -> None:
    ev = _match(prs=[_pr(101, state="closed"), _pr(105)])
    assert ev.pr is not None and ev.pr.number == 105


def test_commit_url_comes_from_the_matching_commit() -> None:
    commits = [{"sha": "a" * 40, "url": f"{REPO_URL}/commit/{'a' * 40}", "message": f"fix {KEY}"}]
    ev = _match(commits=commits)
    assert ev.commit_url == f"{REPO_URL}/commit/{'a' * 40}"


def test_github_issue_matches_by_key_in_title_and_records_link() -> None:
    ev = _match(issues=[_issue(7)])
    assert ev.issue is not None
    assert ev.issue.url == f"{REPO_URL}/issues/7"


# --- Deriving Progress -------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ({}, tr.PROGRESS_NO_WORK),
        ({"branches": [f"selfheal/{KEY}"]}, tr.PROGRESS_BRANCH_ONLY),
        ({"prs": [_pr(checks="pending")]}, tr.PROGRESS_PR_OPEN),
        ({"prs": [_pr(checks="green")]}, tr.PROGRESS_PR_OPEN),
        ({"prs": [_pr(checks="red")]}, tr.PROGRESS_PR_CHECKS_RED),
        ({"prs": [_pr(state="closed", merged=True, checks="green")]}, tr.PROGRESS_MERGED_GREEN),
        ({"prs": [_pr(state="closed", merged=True, checks="red")]}, tr.PROGRESS_MERGED_RED),
        ({"prs": [_pr(state="closed", merged=False)]}, tr.PROGRESS_CLOSED_UNMERGED),
    ],
)
def test_progress_from_evidence(evidence: dict[str, object], expected: str) -> None:
    assert tr.derive_progress(_match(**evidence)) == expected


def test_pr_beats_branch_when_both_exist() -> None:
    ev = _match(branches=[f"selfheal/{KEY}"], prs=[_pr(checks="red")])
    assert tr.derive_progress(ev) == tr.PROGRESS_PR_CHECKS_RED


# --- The approval gate -------------------------------------------------------


def _row(key: str, approval: object) -> dict[str, object]:
    return {"Key": key, "Approval": approval}


def test_only_exactly_approved_rows_are_released() -> None:
    rows = [
        _row("a_key", "Approved"),
        _row("b_key", "Proposed"),
        _row("c_key", "Rejected"),
    ]
    assert tr.approved_keys(rows) == ["a_key"]


@pytest.mark.parametrize("approval", [None, "", "approved", "APPROVED", " Approved", "Approved ", True, 1, "Yes"])
def test_near_misses_and_bad_types_are_not_approval(approval: object) -> None:
    # Fail closed: a typo, a wrong case or a checkbox-shaped value never releases a row.
    assert tr.approved_keys([_row("a_key", approval)]) == []


def test_missing_approval_column_is_not_approval() -> None:
    assert tr.approved_keys([{"Key": "a_key"}]) == []


def test_row_with_invalid_key_is_rejected_not_released() -> None:
    with pytest.raises(tr.EvidenceError):
        tr.approved_keys([_row("Bad Key!", "Approved")])


def test_duplicate_keys_refuse_the_whole_batch() -> None:
    with pytest.raises(tr.EvidenceError):
        tr.approved_keys([_row("a_key", "Approved"), _row("a_key", "Proposed")])


# --- Fail-closed input handling ---------------------------------------------


def test_non_mapping_pr_is_refused() -> None:
    with pytest.raises(tr.EvidenceError):
        _match(prs=["not a mapping"])


@pytest.mark.parametrize("number", [True, "101", 1.5, -3, 0, None])
def test_pr_number_must_be_a_positive_int(number: object) -> None:
    with pytest.raises(tr.EvidenceError):
        _match(prs=[_pr(number=number)])  # type: ignore[arg-type]


def test_pr_with_unknown_checks_value_is_refused_not_guessed() -> None:
    with pytest.raises(tr.EvidenceError):
        _match(prs=[_pr(checks="mostly ok")])


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/badMade/all-stock-dca/pull/101",
        "https://evil.example/badMade/all-stock-dca/pull/101",
        "https://github.com/someone-else/repo/pull/101",
        "javascript:alert(1)",
        "",
    ],
)
def test_pr_url_outside_the_repo_is_refused(url: str) -> None:
    # Only links into this repo may be written to Notion.
    with pytest.raises(tr.EvidenceError):
        _match(prs=[_pr(url=url)])


def test_invalid_key_is_refused() -> None:
    with pytest.raises(tr.EvidenceError):
        tr.match_git_evidence("Bad Key!", repo_url=REPO_URL, branches=[], prs=[], commits=[], issues=[])


def test_result_is_immutable() -> None:
    ev = _match(prs=[_pr(101)])
    with pytest.raises(FrozenInstanceError):
        ev.branch = "changed"  # type: ignore[misc]


# --- Purity guard ------------------------------------------------------------


def test_module_does_no_io_and_holds_no_notion_identifiers() -> None:
    import inspect

    source = inspect.getsource(tr)
    for banned in ("import requests", "import urllib", "import http", "import socket", "collection://", "app.notion.com"):
        assert banned not in source, banned
