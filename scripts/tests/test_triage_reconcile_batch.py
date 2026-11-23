"""Tests for scripts/triage_reconcile_batch.py and the gh converters it relies on.

Regression source: the 2026-11-04 triage digest came out PARTIAL. ``gh`` reported PR state ``MERGED`` and the
reconciler refused it ("State must be open/closed"), Progress cells holding operator notes were judged by hand,
and STEP 4 writes were never made.
"""

from __future__ import annotations

import importlib
import json

import pytest

# Imported lazily, never at collection time: the root sweep collects scripts/tests before tests/selfheal, and an
# early import of the real ``selfheal`` package beats tests/selfheal/__init__.py's __path__ shim, so all four
# tests/selfheal modules then fail with "No module named 'selfheal.test_config'" (PR #2160's first CI run).


def _tr():
    return importlib.import_module("selfheal.triage_reconcile")


def _batch():
    return importlib.import_module("scripts.triage_reconcile_batch")

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
    record = _tr().gh_pr_record(_gh_pr(state=state))
    assert record["state"] == want_state
    assert record["merged"] is merged
    # The converted record is accepted by the reconciler (the 2026-11-04 refusal).
    _tr().match_git_evidence("cap_breach", repo_url=URL, branches=[], prs=[record], commits=[], issues=[])


def test_gh_pr_state_disagreeing_with_merged_at_is_refused():
    raw = _gh_pr(state="CLOSED")
    raw["mergedAt"] = "2026-11-01T00:00:00Z"
    with pytest.raises(_tr().EvidenceError):
        _tr().gh_pr_record(raw)


def test_gh_pr_unknown_state_is_refused():
    with pytest.raises(_tr().EvidenceError):
        _tr().gh_pr_record(_gh_pr(state="merged"))


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
    assert _tr().gh_pr_record(_gh_pr(rollup=rollup))["checks"] == want


def test_docs_only_needs_a_file_list():
    assert "docs_only" not in _tr().gh_pr_record(_gh_pr())
    docs = _tr().gh_pr_record(_gh_pr(files=[{"path": "claude/triage-routine/briefs/cap_breach.md"}]))
    assert docs["docs_only"] is True
    code = _tr().gh_pr_record(_gh_pr(files=[{"path": "docs/x.md"}, {"path": "dca_risk.py"}]))
    assert code["docs_only"] is False


def test_gh_issue_state_is_lowercased_and_checked():
    raw = {"number": 3, "url": f"{URL}/issues/3", "state": "CLOSED", "title": "t"}
    assert _tr().gh_issue_record(raw)["state"] == "closed"
    with pytest.raises(_tr().EvidenceError):
        _tr().gh_issue_record({**raw, "state": "closed"})


def test_is_progress_label():
    assert _tr().is_progress_label(None)
    assert _tr().is_progress_label("  ")
    assert _tr().is_progress_label(_tr().PROGRESS_MERGED_GREEN)
    assert not _tr().is_progress_label("Applied to digest routine 2026-11-01")


# --- reconcile -------------------------------------------------------------------------------------------------


def _reconcile(rows, prs=(), **kw):
    return _batch().reconcile(
        rows, branches=kw.pop("branches", []), prs=[_tr().gh_pr_record(p) for p in prs], commits=[], issues=[], **kw
    )


def _decision(report, key):
    return next(d for d in report["decisions"] if d["key"] == key)


def test_label_cell_is_updated_from_evidence():
    report = _reconcile([{"Key": "cap_breach", "Progress": "PR open", "PR number": 10}], prs=[_gh_pr()])
    d = _decision(report, "cap_breach")
    assert d["updates"] == {"Progress": _tr().PROGRESS_MERGED_GREEN}
    assert report["counts"]["update"] == 1


def test_operator_note_in_progress_is_kept_not_overwritten():
    rows = [{"Key": "cap_breach", "Progress": "Applied to digest routine 2026-11-01", "PR number": "#10"}]
    d = _decision(_reconcile(rows, prs=[_gh_pr()]), "cap_breach")
    assert d["progress_note_kept"] is True
    assert "Progress" not in d["updates"]
    assert d["progress"] == _tr().PROGRESS_MERGED_GREEN


def test_recorded_values_are_never_cleared():
    rows = [{"Key": "cap_breach", "Progress": "", "Branch": "selfheal/cap_breach", "Commit URL": ""}]
    d = _decision(_reconcile(rows), "cap_breach")
    assert d["updates"] == {"Progress": _tr().PROGRESS_NO_WORK}


def test_merged_label_is_not_lowered_to_no_work():
    d = _decision(_reconcile([{"Key": "cap_breach", "Progress": _tr().PROGRESS_MERGED_DOCS_ONLY}]), "cap_breach")
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
    with pytest.raises(_tr().EvidenceError):
        _reconcile([{"Key": "cap_breach"}, {"Key": "cap_breach"}])


def test_approved_rows_are_listed_with_cell_and_derived_label():
    rows = [{"Key": "cap_breach", "Approval": "Approved", "Progress": "note", "PR number": 10}, {"Key": "other_key"}]
    report = _reconcile(rows, prs=[_gh_pr()])
    assert report["approved"] == [{"key": "cap_breach", "progress_cell": "note", "derived": _tr().PROGRESS_MERGED_GREEN}]


def test_approved_groups_counts_come_from_the_lists_and_partition_the_total():
    rows = [
        {"Key": "cap_breach", "Approval": "Approved", "PR number": 10},
        {"Key": "cap_consumed", "Approval": "Approved"},
        {"Key": "no_pr_key", "Approval": "Approved"},
        {"Key": "not_approved"},
    ]
    report = _reconcile(rows, prs=[_gh_pr()])
    groups = report["approved_groups"]
    assert groups["total"] == 3 == len(report["approved"])
    assert all(g["count"] == len(g["keys"]) for g in groups["groups"])
    assert sum(g["count"] for g in groups["groups"]) == groups["total"]
    by_label = {g["label"]: g["keys"] for g in groups["groups"]}
    assert by_label[_tr().PROGRESS_MERGED_GREEN] == ["cap_breach"]
    assert sorted(k for label, keys in by_label.items() if label != _tr().PROGRESS_MERGED_GREEN for k in keys) == [
        "cap_consumed",
        "no_pr_key",
    ]
    assert "not_approved" not in sum(by_label.values(), [])


def test_approved_groups_with_no_approved_rows_is_empty():
    assert _reconcile([{"Key": "cap_breach"}])["approved_groups"] == {"total": 0, "groups": []}


def test_unsupported_approved_repaired_without_git_claim_is_reported():
    report = _reconcile([{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired"}])
    assert report["unsupported_fixes"]["unsupported_approved_fix"] == ["cap_breach"]


def test_unsupported_fix_with_unmatched_recorded_pr_is_separated():
    report = _reconcile([{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired", "PR number": 99}])
    assert report["unsupported_fixes"]["unsupported_with_recorded_evidence"] == ["cap_breach"]
    assert report["counts"]["unsupported_with_recorded_evidence"] == 1


def test_unsupported_fix_with_recorded_commit_url_is_separated():
    row = {
        "Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired",
        "Commit URL": f"{URL}/commit/{'a' * 40}",
    }
    report = _reconcile([row])
    assert report["unsupported_fixes"]["unsupported_with_recorded_evidence"] == ["cap_breach"]
    assert report["counts"]["unsupported_with_recorded_evidence"] == 1


def test_unsupported_fix_without_recorded_pr_or_commit_is_separated():
    report = _reconcile([{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired"}])
    assert report["unsupported_fixes"]["unsupported_no_recorded_evidence"] == ["cap_breach"]
    assert report["counts"]["unsupported_no_recorded_evidence"] == 1


def test_unsupported_fix_recorded_evidence_lists_partition_union():
    rows = [
        {"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired", "PR number": "#99"},
        {"Key": "low_confidence_tickers", "Approval": "Approved", "Fix state": "Repaired"},
    ]
    report = _reconcile(rows)
    fixes = report["unsupported_fixes"]
    split = fixes["unsupported_with_recorded_evidence"] + fixes["unsupported_no_recorded_evidence"]
    assert sorted(split) == sorted(fixes["unsupported_approved_fix"])
    assert len(split) == len(set(split))


def test_matching_pr_supports_approved_repaired_row():
    report = _reconcile(
        [{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired", "PR number": 10}],
        prs=[_gh_pr()],
    )
    assert report["unsupported_fixes"]["unsupported_approved_fix"] == []


@pytest.mark.parametrize("pr_number", ["#10", f"{URL}/pull/10"])
def test_audit_normalizes_notion_pr_number_forms(pr_number):
    report = _reconcile([{"Key": "cap_breach", "PR number": pr_number}], prs=[_gh_pr()])
    assert report["unsupported_fixes"]["recorded_pr_not_found"] == []


def test_evidence_error_is_audit_not_run_not_unsupported():
    report = _reconcile([{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired", "PR number": "bad"}])
    assert report["unsupported_fixes"]["unsupported_approved_fix"] == []
    assert report["unsupported_fixes"]["audit_not_run"] == [
        {"key": "cap_breach", "reason": "row PR number 'bad' is not a number"}
    ]


def test_row_whose_evidence_match_raises_is_not_audited_as_unsupported(monkeypatch):
    # An EvidenceError raised by match_git_evidence itself (not the PR-number parse) leaves no GitEvidence for the Key.
    tr = _tr()
    real = tr.match_git_evidence

    def boom(key, **kwargs):
        if key == "cap_breach":
            raise tr.EvidenceError("simulated evidence refusal")
        return real(key, **kwargs)

    monkeypatch.setattr(tr, "match_git_evidence", boom)
    rows = [{"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired"}]
    report = _reconcile(rows)
    assert report["unsupported_fixes"]["unsupported_approved_fix"] == []
    assert report["unsupported_fixes"]["audit_not_run"] == [
        {"key": "cap_breach", "reason": "simulated evidence refusal"}
    ]


def test_recorded_pr_unconfirmed_row_is_still_audited():
    row = {"Key": "run_states_unclassifiable", "Approval": "Approved", "Fix state": "Repaired", "PR number": 1514}
    pr = _gh_pr(number=1514, head="fix/daily-schedule-verification", title="Verify every live slot daily")
    report = _reconcile([row], prs=[pr])
    assert _decision(report, row["Key"])["action"] == "skip"
    assert report["unsupported_fixes"]["unsupported_approved_fix"] == [row["Key"]]
    assert report["unsupported_fixes"]["recorded_pr_not_found"] == [row["Key"]]


def test_audit_does_not_mutate_caller_rows():
    row = {"Key": "cap_breach", "Approval": "Approved", "Fix state": "Repaired", "PR number": "#10"}
    before = dict(row)
    _reconcile([row], prs=[_gh_pr()])
    assert row == before


def test_preexisting_report_contract_is_pinned():
    # Captured from pre-audit reconcile() behavior for this fixed fixture. New audit keys are additive only.
    report = _reconcile([{"Key": "cap_breach", "Progress": "PR open", "PR number": 10}], prs=[_gh_pr()])
    legacy_counts = {
        "rows_read": 1, "create": 0, "update": 1, "unchanged": 0, "skip": 0,
        "progress_notes_kept": 0, "progress_regressions_blocked": 0,
    }
    assert {key: report["counts"][key] for key in legacy_counts} == legacy_counts
    assert set(("counts", "decisions", "approved", "approved_groups")) <= report.keys()
    assert report["decisions"] == [{
            "key": "cap_breach", "action": "update", "progress": "Merged, checks green",
            "evidence": {"branch": None, "pr": f"{URL}/pull/10", "pr_merged": True,
                         "pr_checks": "green", "commit_url": None, "issue": None, "mentioned_in": None,
                         "commit_status": None},
            "updates": {"Progress": "Merged, checks green"}, "approved": False,
        }]
    assert report["approved"] == []
    assert report["approved_groups"] == {"total": 0, "groups": []}


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
    assert _batch().main(["--rows", str(rows), "--work-dir", str(work)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["decisions"][0]["updates"]["Progress"] == _tr().PROGRESS_MERGED_DOCS_ONLY


def test_cli_refuses_a_bad_gh_record(tmp_path, capsys):
    work = _work_dir(tmp_path, [_gh_pr(state="weird")])
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    assert _batch().main(["--rows", str(rows), "--work-dir", str(work)]) == 1
    assert "refused" in json.loads(capsys.readouterr().out)["error"]


def test_cli_missing_input_file_fails_closed(tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    assert _batch().main(["--rows", str(rows), "--work-dir", str(tmp_path)]) == 1
    assert "prs.json" in json.loads(capsys.readouterr().out)["error"]


def test_pr_number_given_as_this_repos_pr_url_is_read_as_the_number():
    # 2026-11-14: the Notion fetch handed the PR number column over as URLs.
    rows = [{"Key": "cap_breach", "Progress": "PR open", "PR number": f"{URL}/pull/10"}]
    assert _decision(_reconcile(rows, prs=[_gh_pr()]), "cap_breach")["updates"] == {"Progress": _tr().PROGRESS_MERGED_GREEN}
    assert _batch().recorded_pr_numbers(rows + [{"PR number": f"{URL}/pull/7/"}]) == [7, 10]


@pytest.mark.parametrize(
    "value",
    ["https://github.com/someone-else/all-stock-dca/pull/10", f"{URL}/issues/10", f"{URL}/pull/10/files"],
)
def test_a_pr_url_outside_this_repo_or_not_a_pr_is_still_refused(value):
    d = _decision(_reconcile([{"Key": "cap_breach", "PR number": value}], prs=[_gh_pr()]), "cap_breach")
    assert d["action"] == "skip" and "EvidenceError" in d["skip_reason"]


def test_notion_escaped_key_is_unescaped_and_nothing_else_changes():
    rows = [{"Key": "max\\_orders\\_per\\_day\\_unenforced", "Proposed plan": "keep \\_ here", "PR number": 1745}]
    normalized = _batch().normalize_rows(rows)
    assert normalized == [{"Key": "max_orders_per_day_unenforced", "Proposed plan": "keep \\_ here", "PR number": 1745}]
    assert rows[0]["Key"] == "max\\_orders\\_per\\_day\\_unenforced"  # the caller's rows are not mutated


def test_cli_accepts_escaped_keys_and_pr_urls_from_the_notion_fetch(tmp_path, capsys):
    work = _work_dir(tmp_path, [_gh_pr()])
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"Key": "cap\\_breach", "Progress": "PR open", "PR number": f"{URL}/pull/10"}]))
    assert _batch().main(["--rows", str(rows), "--work-dir", str(work)]) == 0
    decision = json.loads(capsys.readouterr().out)["decisions"][0]
    assert decision["key"] == "cap_breach" and decision["action"] == "update"


def test_cli_refuses_a_cache_collected_for_other_rows(tmp_path, capsys):
    # 2026-11-14: prs.json from a --collect that could not parse the PR column lacked #1745, and the rerun on fixed
    # rows reported a merged fix that cites its Key as recorded_pr_unconfirmed. It must refuse instead.
    work = _work_dir(tmp_path, [_gh_pr()])
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"Key": "cap_breach", "PR number": 10}, {"Key": "max_orders_per_day_unenforced", "PR number": 1745}]))
    assert _batch().main(["--rows", str(rows), "--work-dir", str(work)]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "#1745" in error and "--collect" in error and "#10" not in error


def test_recorded_helpers():
    rows = [
        {"Commit URL": f"{URL}/commit/{'a' * 40}", "PR number": 5},
        {"Commit URL": "not a url", "PR number": "#7"},
        {"PR number": "junk"},
    ]
    assert _batch().recorded_shas(rows) == ["a" * 40]
    assert _batch().recorded_pr_numbers(rows) == [5, 7]
