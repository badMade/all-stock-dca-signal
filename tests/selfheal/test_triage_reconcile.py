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


def test_pr_matches_by_key_in_title_when_head_ref_differs() -> None:
    ev = _match(prs=[_pr(102, head_ref="claude/some-branch", title=f"Fix {KEY}")])
    assert ev.pr is not None and ev.pr.number == 102


def test_body_only_mention_is_not_a_fix_but_is_recorded() -> None:
    pr = _pr(102, head_ref="claude/some-branch", state="closed", merged=True, checks="green", body=f"Fixes {KEY}")
    ev = _match(prs=[pr])
    assert ev.pr is None
    assert ev.mentioned_in is not None and ev.mentioned_in.number == 102
    assert tr.derive_progress(ev) == "No work"


def test_pr_body_that_lists_key_as_not_changed_does_not_match() -> None:
    # Regression: PR #1642 named eight keys under "Not changed, on purpose" and the
    # whole-token mention match stamped them "Merged, checks green", so the Notion
    # rows claimed a fix that the PR explicitly did not make.
    body = f"Docs only.\n\nNot changed, on purpose: classifier_block, {KEY}, cap_breach.\n"
    ev = _match(prs=[_pr(103, head_ref="claude/some-branch", state="closed", merged=True, checks="green", body=body)])
    assert ev.pr is None and ev.mentioned_in is None


def test_newest_pr_wins_when_several_match() -> None:
    ev = _match(prs=[_pr(101, state="closed"), _pr(105)])
    assert ev.pr is not None and ev.pr.number == 105


def test_body_only_mention_does_not_displace_an_older_pr_about_the_key() -> None:
    # Regression (2026-10-21 digest): PR #1873 cited the Key in its body while calling the root cause
    # unconfirmed, and as the newest match it regressed a merged fix to "PR open".
    fix = _pr(1704, head_ref="claude/fix", title=f"Fix {KEY}", body="", state="closed", merged=True)
    cite = _pr(1873, head_ref="claude/other", title="Investigate feed", body=f"Related to {KEY}, cause unconfirmed")
    ev = _match(prs=[fix, cite])
    assert ev.pr is not None and ev.pr.number == 1704
    assert ev.mentioned_in is not None and ev.mentioned_in.number == 1873


def test_recorded_pr_cited_only_in_body_keeps_counting() -> None:
    # Regression (2026-10-22 digest): five merged rows fell to "No work" because their recorded PR cites the Key
    # only in its body. A PR already on the row survives; any other body-only citation still does not count.
    fix = _pr(1704, head_ref="feat/x", title="Unrelated title", body=f"Closes {KEY}", state="closed", merged=True,
              checks="green")
    other = _pr(1873, head_ref="claude/other", title="Investigate", body=f"Related to {KEY}")
    ev = _match(prs=[fix, other], recorded_pr_number=1704)
    assert ev.pr is not None and ev.pr.number == 1704
    assert ev.mentioned_in is not None and ev.mentioned_in.number == 1873
    assert tr.derive_progress(ev) == "Merged, checks green"
    assert _match(prs=[fix, other]).pr is None


def test_newer_title_match_beats_recorded_body_only_pr() -> None:
    old = _pr(1704, head_ref="feat/x", title="Unrelated", body=f"Closes {KEY}", state="closed", merged=True)
    new = _pr(1900, head_ref="claude/y", title=f"Fix {KEY}")
    ev = _match(prs=[old, new], recorded_pr_number=1704)
    assert ev.pr is not None and ev.pr.number == 1900


def test_commit_url_comes_from_the_matching_commit() -> None:
    commits = [{"sha": "a" * 40, "url": f"{REPO_URL}/commit/{'a' * 40}", "message": f"fix {KEY}"}]
    ev = _match(commits=commits)
    assert ev.commit_url == f"{REPO_URL}/commit/{'a' * 40}"


def test_non_shallow_result_matches_pre_change_golden() -> None:
    """The new default field leaves the existing non-shallow result unchanged."""
    expected = tr.GitEvidence(
        branch=f"selfheal/{KEY}",
        pr=tr.PullRequestRef(101, f"{REPO_URL}/pull/101", "open", False, "pending"),
        commit_url=f"{REPO_URL}/commit/{'a' * 40}",
        issue=tr.IssueRef(7, f"{REPO_URL}/issues/7", "open"),
    )
    assert _match(
        branches=[f"selfheal/{KEY}"], prs=[_pr()], commits=[_commit("a", f"Fix {KEY}")], issues=[_issue()]
    ) == expected


def test_recorded_commit_unavailable_in_shallow_clone_is_unverifiable() -> None:
    sha = "a" * 40
    ev = _match(recorded_commit_url=f"{REPO_URL}/commit/{sha}", shallow=True, commit_present={sha: False})
    assert ev.commit_url is None
    assert ev.commit_status == "unverifiable_shallow"


def test_absent_commit_in_non_shallow_clone_has_no_status_or_error() -> None:
    sha = "a" * 40
    ev = _match(recorded_commit_url=f"{REPO_URL}/commit/{sha}", shallow=False, commit_present={sha: False})
    assert ev.commit_url is None
    assert ev.commit_status is None


@pytest.mark.parametrize("kwargs", [{"shallow": 1}, {"commit_present": {"bad": False}},
                                     {"commit_present": {"a" * 40: 0}}])
def test_shallow_metadata_is_validated(kwargs: dict[str, object]) -> None:
    with pytest.raises(tr.EvidenceError):
        _match(**kwargs)


def _commit(char: str, message: str) -> dict[str, object]:
    return {"sha": char * 40, "url": f"{REPO_URL}/commit/{char * 40}", "message": message}


def test_commit_citing_the_key_only_in_its_body_does_not_match() -> None:
    # Regression (2026-10-26 digest): the squash commit of #1930, a triage-tool change whose body discussed five
    # Keys, was matched as the Commit URL for all five rows. Commits now follow the PR title rule via the subject.
    tool_change = _commit("b", f"triage_reconcile: restore handling (#1930)\n\nRows like {KEY} regressed.")
    fix = _commit("a", f"Fix {KEY} fallback (#1704)")
    assert _match(commits=[tool_change, fix]).commit_url == f"{REPO_URL}/commit/{'a' * 40}"
    assert _match(commits=[tool_change]).commit_url is None


def test_commit_listing_key_as_not_changed_does_not_match() -> None:
    commit = _commit("c", f"Docs (#1642)\n\nNot changed, on purpose: {KEY}.")
    assert _match(commits=[commit]).commit_url is None


def test_recorded_commit_cited_only_in_body_keeps_counting_and_beats_newer_match() -> None:
    recorded = _commit("a", f"Finalizer fix (#1519)\n\nCovers {KEY}.")
    newer = _commit("d", f"Mention {KEY} in docs (#1950)")
    url = f"{REPO_URL}/commit/{'a' * 40}"
    assert _match(commits=[newer, recorded], recorded_commit_url=url).commit_url == url
    # A recorded commit that no longer cites the Key does not pin the row.
    unrelated = _commit("a", "Unrelated change")
    assert _match(commits=[newer, unrelated], recorded_commit_url=url).commit_url == f"{REPO_URL}/commit/{'d' * 40}"


def test_recorded_commit_url_must_be_a_string() -> None:
    with pytest.raises(tr.EvidenceError):
        _match(recorded_commit_url=123)


def test_recorded_pr_that_never_cites_the_key_is_flagged_not_lowered() -> None:
    # Regression (2026-10-26 digest): run_states_unclassifiable records PR #1514, whose body never cites the Key.
    # The reconciler returned "No work" with no signal; the caller must skip the row instead of overwriting it.
    unrelated = _pr(1514, head_ref="feat/x", title="Unrelated", body="", state="closed", merged=True)
    ev = _match(prs=[unrelated], recorded_pr_number=1514)
    assert ev.pr is None and ev.recorded_pr_unconfirmed is True


def test_recorded_pr_flag_is_clear_when_evidence_is_found_or_nothing_was_recorded() -> None:
    assert _match(prs=[_pr(101)], recorded_pr_number=99).recorded_pr_unconfirmed is False
    assert _match().recorded_pr_unconfirmed is False


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


def test_merged_docs_only_pr_is_not_reported_as_a_green_fix() -> None:
    # PR #1642 only drafted fixes (documentation), yet 12 rows read "Merged, checks green".
    pr = _pr(state="closed", merged=True, checks="green", docs_only=True)
    assert tr.derive_progress(_match(prs=[pr])) == tr.PROGRESS_MERGED_DOCS_ONLY


def test_docs_only_flag_defaults_to_false_and_must_be_a_boolean() -> None:
    assert _match(prs=[_pr()]).pr.docs_only is False  # type: ignore[union-attr]
    with pytest.raises(tr.EvidenceError):
        _match(prs=[_pr(docs_only="yes")])


def test_unmerged_docs_only_pr_keeps_its_open_label() -> None:
    assert tr.derive_progress(_match(prs=[_pr(docs_only=True)])) == tr.PROGRESS_PR_OPEN


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


def test_blind_run_reason_accepts_readable_fallback_root_when_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DCA_CRON_TRACKING_ROOT", raising=False)
    assert tr.blind_run_reason("/checkout/cron_tracking", True, ["scheduled_runs"]) is None


def test_blind_run_reason_flags_missing_root() -> None:
    reason = tr.blind_run_reason("/missing/cron_tracking", False, [])
    assert reason is not None and "not an existing directory" in reason


def test_blind_run_reason_flags_empty_root() -> None:
    reason = tr.blind_run_reason("/checkout/cron_tracking", True, [])
    assert reason is not None and "no stream subdirectory" in reason


def test_module_does_no_io_and_holds_no_notion_identifiers() -> None:
    import inspect

    source = inspect.getsource(tr)
    for banned in ("import requests", "import urllib", "import http", "import socket", "collection://", "app.notion.com"):
        assert banned not in source, banned


def test_audit_flags_approved_repaired_row_without_fix_evidence() -> None:
    row = {"Key": KEY, "Approval": "Approved", "Fix state": "Repaired"}
    assert tr.audit_unsupported_fixes([row], {KEY: _match()}) == {
        "unsupported_approved_fix": [KEY],
        "brief_acceptance_not_demonstrated": [],
        "recorded_pr_not_found": [],
    }


def test_audit_flags_monitoring_row_with_brief_acceptance_note() -> None:
    key = "false_all_clear"
    row = {
        "Key": key,
        "Status": "Monitoring",
        "Progress": "brief acceptance not demonstrated",
        "Proposed plan": "",
    }
    assert tr.audit_unsupported_fixes([row], {key: _match()})["brief_acceptance_not_demonstrated"] == [key]


def test_audit_flags_recorded_pr_missing_from_matching_evidence() -> None:
    row = {"Key": KEY, "PR number": 101}
    assert tr.audit_unsupported_fixes([row], {KEY: _match()})["recorded_pr_not_found"] == [KEY]


def test_audit_empty_inputs_return_empty_sorted_categories() -> None:
    assert tr.audit_unsupported_fixes([], {}) == {
        "unsupported_approved_fix": [],
        "brief_acceptance_not_demonstrated": [],
        "recorded_pr_not_found": [],
    }
