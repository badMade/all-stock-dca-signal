"""Tests for scripts/triage_reconcile_batch.py and the gh converters it relies on.

Regression source: the 2026-11-04 triage digest came out PARTIAL. ``gh`` reported PR state ``MERGED`` and the
reconciler refused it ("State must be open/closed"), Progress cells holding operator notes were judged by hand,
and STEP 4 writes were never made.
"""

from __future__ import annotations

import json

import pytest

from scripts import triage_reconcile_batch as batch
from selfheal import triage_reconcile as tr

URL = "https://github.com/badMade/all-stock-dca"


def _gh_pr(number=10, state="MERGED", head="selfheal/cap_breach", title="Fix cap_breach", rollup=None, **extra):
    record = {
        "number": number,
        "url": f"{URL}/pull/{number}",
        "state": state,
        "mergedAt": "2026-11-01T00:00:00Z" if state == "MERGED" else None,
        "headRefName": head,
        "title": title,
        "body": "",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}] if rollup is None else rollup,
    }
    record.update(extra)
    return record


# --- gh converters ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, merged, want_state",
    [("MERGED", True, "closed"), ("CLOSED", False, "closed"), ("OPEN", False, "open")],
)
def test_gh_pr_state_maps_to_open_closed_plus_merged_flag(state, merged, want_state):
    record = tr.gh_pr_record(_gh_pr(state=state))
    assert record["state"] == want_state
    assert record["merged"] is merged
    # The converted record is accepted by the reconciler (the 2026-11-04 refusal).
    tr.match_git_evidence("cap_breach", repo_url=URL, branches=[], prs=[record], commits=[], issues=[])


def test_gh_pr_state_disagreeing_with_merged_at_is_refused():
    raw = _gh_pr(state="CLOSED")
    raw["mergedAt"] = "2026-11-01T00:00:00Z"
    with pytest.raises(tr.EvidenceError):
        tr.gh_pr_record(raw)


def test_gh_pr_unknown_state_is_refused():
    with pytest.raises(tr.EvidenceError):
        tr.gh_pr_record(_gh_pr(state="merged"))


@pytest.mark.parametrize(
    "rollup, want",
    [
        ([], "pending"),
        ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}], "green"),
        ([{"status": "COMPLETED", "conclusion": "FAILURE"}], "red"),
        ([{"state": "ERROR"}], "red"),
        ([{"status": "IN_PROGRESS", "conclusion": ""}], "pending"),
        ([{"status": "COMPLETED", "conclusion": "SKIPPED"}], "green"),
    ],
)
def test_gh_check_rollup_mapping(rollup, want):
    assert tr.gh_pr_record(_gh_pr(rollup=rollup))["checks"] == want


def test_docs_only_needs_a_file_list():
    assert "docs_only" not in tr.gh_pr_record(_gh_pr())
    docs = tr.gh_pr_record(_gh_pr(files=[{"path": "claude/triage-routine/briefs/cap_breach.md"}]))
    assert docs["docs_only"] is True
    code = tr.gh_pr_record(_gh_pr(files=[{"path": "docs/x.md"}, {"path": "dca_risk.py"}]))
    assert code["docs_only"] is False


def test_gh_issue_state_is_lowercased_and_checked():
    raw = {"number": 3, "url": f"{URL}/issues/3", "state": "CLOSED", "title": "t"}
    assert tr.gh_issue_record(raw)["state"] == "closed"
    with pytest.raises(tr.EvidenceError):
        tr.gh_issue_record({**raw, "state": "closed"})


def test_is_progress_label():
    assert tr.is_progress_label(None)
    assert tr.is_progress_label("  ")
    assert tr.is_progress_label(tr.PROGRESS_MERGED_GREEN)
    assert not tr.is_progress_label("Applied to digest routine 2026-11-01")


# --- reconcile -------------------------------------------------------------------------------------------------


def _reconcile(rows, prs=(), **kw):
    return batch.reconcile(
        rows, branches=kw.pop("branches", []), prs=[tr.gh_pr_record(p) for p in prs], commits=[], issues=[], **kw
    )


def _decision(report, key):
    return next(d for d in report["decisions"] if d["key"] == key)


def test_label_cell_is_updated_from_evidence():
    report = _reconcile([{"Key": "cap_breach", "Progress": "PR open", "PR number": 10}], prs=[_gh_pr()])
    d = _decision(report, "cap_breach")
    assert d["updates"] == {"Progress": tr.PROGRESS_MERGED_GREEN}
    assert report["counts"]["update"] == 1


def test_operator_note_in_progress_is_kept_not_overwritten():
    rows = [{"Key": "cap_breach", "Progress": "Applied to digest routine 2026-11-01", "PR number": "#10"}]
    d = _decision(_reconcile(rows, prs=[_gh_pr()]), "cap_breach")
    assert d["progress_note_kept"] is True
    assert "Progress" not in d["updates"]
    assert d["progress"] == tr.PROGRESS_MERGED_GREEN


def test_recorded_values_are_never_cleared():
    rows = [{"Key": "cap_breach", "Progress": "", "Branch": "selfheal/cap_breach", "Commit URL": ""}]
    d = _decision(_reconcile(rows), "cap_breach")
    assert d["updates"] == {"Progress": tr.PROGRESS_NO_WORK}


def test_merged_label_is_not_lowered_to_no_work():
    d = _decision(_reconcile([{"Key": "cap_breach", "Progress": tr.PROGRESS_MERGED_DOCS_ONLY}]), "cap_breach")
    assert d["progress_regression_blocked"] is True
    assert d["updates"] == {}


def test_recorded_pr_that_does_not_cite_the_key_is_skipped():
    # run_states_unclassifiable / #1514 on 2026-11-04: real fix, but neither title nor body names the Key.
    rows = [{"Key": "run_states_unclassifiable", "PR number": 1514}]
    pr = _gh_pr(number=1514, head="fix/daily-schedule-verification", title="Verify every live slot daily")
    d = _decision(_reconcile(rows, prs=[pr]), "run_states_unclassifiable")
    assert d["action"] == "skip"
    assert "1514" in d["skip_reason"]


def test_bad_pr_number_skips_only_that_row():
    report = _reconcile([{"Key": "a_key", "PR number": "abc"}, {"Key": "b_key"}])
    assert _decision(report, "a_key")["action"] == "skip"
    assert _decision(report, "b_key")["action"] == "update"


def test_candidates_without_rows_are_created_and_duplicates_ignored():
    report = _reconcile([{"Key": "cap_breach"}], candidates=["bridge_disk_low", "cap_breach", "bridge_disk_low"])
    assert [d["key"] for d in report["decisions"]] == ["cap_breach", "bridge_disk_low"]
    assert _decision(report, "bridge_disk_low")["action"] == "create"


def test_duplicate_row_key_refuses_the_batch():
    with pytest.raises(tr.EvidenceError):
        _reconcile([{"Key": "cap_breach"}, {"Key": "cap_breach"}])


def test_approved_rows_are_listed_with_cell_and_derived_label():
    rows = [{"Key": "cap_breach", "Approval": "Approved", "Progress": "note", "PR number": 10}, {"Key": "other_key"}]
    report = _reconcile(rows, prs=[_gh_pr()])
    assert report["approved"] == [{"key": "cap_breach", "progress_cell": "note", "derived": tr.PROGRESS_MERGED_GREEN}]


# --- CLI over a collected work dir ---------------------------------------------------------------------------


def _work_dir(tmp_path, prs, pr_files=None):
    for name, payload in (
        ("prs.json", prs),
        ("issues.json", []),
        ("branches.json", []),
        ("commits.json", []),
        ("shallow.json", False),
        ("commit_present.json", {}),
    ):
        (tmp_path / name).write_text(json.dumps(payload))
    if pr_files is not None:
        (tmp_path / "pr_files.json").write_text(json.dumps(pr_files))
    return tmp_path


def test_cli_reads_raw_gh_json_and_applies_pr_files(tmp_path, capsys):
    work = _work_dir(tmp_path, [_gh_pr()], {"10": ["claude/triage-routine/briefs/cap_breach.md"]})
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"Key": "cap_breach", "Progress": "", "PR number": 10}]))
    assert batch.main(["--rows", str(rows), "--work-dir", str(work)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["decisions"][0]["updates"]["Progress"] == tr.PROGRESS_MERGED_DOCS_ONLY


def test_cli_refuses_a_bad_gh_record(tmp_path, capsys):
    work = _work_dir(tmp_path, [_gh_pr(state="weird")])
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    assert batch.main(["--rows", str(rows), "--work-dir", str(work)]) == 1
    assert "refused" in json.loads(capsys.readouterr().out)["error"]


def test_cli_missing_input_file_fails_closed(tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    assert batch.main(["--rows", str(rows), "--work-dir", str(tmp_path)]) == 1
    assert "prs.json" in json.loads(capsys.readouterr().out)["error"]


def test_recorded_helpers():
    rows = [
        {"Commit URL": f"{URL}/commit/{'a' * 40}", "PR number": 5},
        {"Commit URL": "not a url", "PR number": "#7"},
        {"PR number": "junk"},
    ]
    assert batch.recorded_shas(rows) == ["a" * 40]
    assert batch.recorded_pr_numbers(rows) == [5, 7]
