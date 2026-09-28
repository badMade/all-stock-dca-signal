"""Tests for scripts/alert_digest.py.

The corpus these exercise is deliberately shaped like the real one: executors
spell the same slot half a dozen ways, narrate several gates in one body before
naming the one that actually failed, and report delivery on channels that are
switched off. Each of those is a way the digest could quietly mis-attribute an
alert, so each gets a test.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PureWindowsPath

import pytest

from scripts.alert_digest import (
    _DECLARED_ET_DATE,
    _DRAFT_WARNING,
    _REPO_ROOT,
    _RETRY_EVIDENCE,
    AlertRecord,
    _calendar_window_end,
    _classify_coverage_day,
    _compute_streaks,
    _corroborates,
    _file_event_utc,
    _incomplete_roster_days,
    _last_roster_slot_time,
    _matched_path_is_external,
    _message_failure_is_first_attempt_qualified,
    _reason_signature,
    _reconcile_drafts_and_messages,
    _roster_completion_time,
    _slot_from_filename_token,
    _slot_scheduled_utc,
    approved_trading_window,
    build_digest,
    collect_records,
    detect_channel_warnings,
    detect_gate,
    detect_gate_evidence,
    detect_slot,
    detect_slot_evidence,
    detect_verdict,
    early_close_days,
    expected_roster_days,
    filter_records,
    is_trading_day,
    ledger_path,
    load_alert_files,
    load_gmail_drafts,
    load_gmail_messages,
    load_ledger,
    looks_like_executor_alert,
    main,
    merge_cross_source,
    parse_timestamp,
    render_json,
    render_text,
    save_ledger,
    states_email_failure,
    trading_days_between,
)


def _draft(draft_id, when, subject, body=""):
    return {"id": draft_id, "date": when, "subject": subject, "plaintextBody": body}


def _record(alert_id, when, slot, gate, verdict="ABORT", warnings=()):
    return AlertRecord(
        alert_id=alert_id,
        source="gmail_draft",
        timestamp_utc=parse_timestamp(when),
        subject=alert_id,
        body="Reason: executor gate failure",
        slot=slot,
        gate=gate,
        verdict=verdict,
        warnings=tuple(warnings),
    )


def _approved_window_last_trading_day_and_next():
    window = approved_trading_window()
    assert window is not None
    start, end = window
    window_days = expected_roster_days(start, end)
    assert window_days
    last_window_day = window_days[-1]
    next_trading_day = last_window_day + timedelta(days=1)
    while not is_trading_day(next_trading_day):
        next_trading_day += timedelta(days=1)
    return last_window_day, next_trading_day


# --- timestamps --------------------------------------------------------------


def test_parse_timestamp_handles_trailing_z():
    parsed = parse_timestamp("2026-08-26T18:36:54Z")
    assert parsed == datetime(2026, 8, 26, 18, 36, 54, tzinfo=timezone.utc)


def test_parse_timestamp_assumes_utc_when_offset_absent():
    # Guessing the runner's local zone here would shift every record silently.
    assert parse_timestamp("2026-08-26T18:36:54").tzinfo == timezone.utc


def test_parse_timestamp_normalizes_a_non_utc_offset():
    assert parse_timestamp("2026-08-26T14:36:54-04:00") == datetime(
        2026, 8, 26, 18, 36, 54, tzinfo=timezone.utc
    )


def test_et_date_uses_eastern_not_utc():
    # 2026-08-14T21:07 ET is 2026-08-15T01:07Z. A naive UTC read would file the
    # off-schedule evening batch under the 15th and split the incident in two.
    record = _record("a", "2026-08-15T01:07:57Z", "0935", "Gate 3")
    assert record.et_date == date(2026, 8, 14)


# --- slot detection ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DCA 1330ET intraday slot", "1330"),
        ("[1030ET DCA] ABORT", "1030"),
        ("DCA midday (1230ET) run", "1230"),
        ("ABORT - 2:30 PM ET slot", "1430"),
        ("DCA ABORT - 9:35 AM Market-Open Slot", "0935"),
        ("330-final-buying-run aborted", "1530"),
        ("1130-mornig-buying-run - Gate 4", "1130"),
        ("DCA market-open slot run report", "0935"),
    ],
)
def test_detect_slot_across_the_spellings_executors_actually_use(text, expected):
    assert detect_slot(text) == expected


def test_detect_slot_prefers_the_explicit_et_label_over_a_clock_time():
    # Bodies quote other slots' fills while naming their own slot in the label.
    text = "1530ET final run; earlier fills at 12:35 PM ET from another slot"
    assert detect_slot(text) == "1530"


def test_detect_slot_ignores_a_bare_time_without_am_pm():
    assert detect_slot("order filled at 1:30 into the window") is None


def test_detect_slot_returns_none_when_nothing_identifies_it():
    assert detect_slot("(no subject)") is None


def test_bare_morning_is_not_treated_as_a_slot():
    # "morning" belongs to both 0935 (dca-morning-buy) and 1130 (morning-buying-run).
    assert detect_slot("the morning run had a problem") is None


# --- gate detection ----------------------------------------------------------


def test_detect_gate_prefers_the_explicit_failing_gate_line():
    body = "Gates 0-4 passed.\nFailing gate: Gate 6 - Budget\nAction: placed nothing."
    assert detect_gate("DCA ALERT", body) == "Gate 6"


def test_detect_gate_does_not_blame_the_first_gate_narrated():
    # The real 2026-08-26 1:30 PM body opens "Gates 0-4 passed ... stopped
    # before Gate 5". Naive first-match scanning blames Gate 0.
    body = "Gates 0-4 passed (market day, time sanity).\nRun stopped before Gate 5 (buy list)."
    assert detect_gate("DCA 1:30 PM ET intraday slot -- ABORTED", body) == "Gate 5"


def test_detect_gate_prefers_the_subject_over_body_narration():
    assert detect_gate("ABORT at Gate 4", "Gates 0-3 passed, then trouble") == "Gate 4"


def test_detect_gate_recognizes_named_reason_codes():
    assert detect_gate("slot alert", "verdict GATE5_DISABLED, nothing placed") == "GATE5_DISABLED"
    assert detect_gate("GATE4_EXPECTED_OVERLAP", "") == "GATE4_EXPECTED_OVERLAP"


def test_named_reason_code_outranks_a_bare_gate_number():
    # Both appear together constantly; the code says why as well as where.
    assert detect_gate("Gate 4 note", "classified GATE4_EXPECTED_OVERLAP") == "GATE4_EXPECTED_OVERLAP"


def test_detect_gate_handles_the_lowercase_filename_spelling():
    assert detect_gate("gate7", "") == "Gate 7"


def test_detect_gate_returns_none_when_absent():
    assert detect_gate("weekly summary", "nothing to report") is None


# --- verdicts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("DCA ABORT - 3:30 PM slot", "ABORT"),
        ("Gate 7 order rejected (RVII)", "REJECTED"),
        ("DCA 9:35 AM slot - run report", "REPORT"),
        ("cap breach + top-up resizing", "CAP_BREACH"),
    ],
)
def test_detect_verdict(subject, expected):
    assert detect_verdict(subject, "") == expected


def test_detect_verdict_prefers_subject_over_body():
    assert detect_verdict("ABORTED at Gate 4", "one order was rejected earlier") == "ABORT"


# --- draft loading -----------------------------------------------------------


def test_load_gmail_drafts_normalizes_a_realistic_draft():
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "DCA Alert: 2:30 PM ET intraday run ABORTED - Gate 4 sole-executor interlock",
                "Failing gate: Gate 4 - Sole-Executor Interlock\nAction taken: placed NOTHING.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert (record.slot, record.gate, record.verdict) == ("1430", "Gate 4", "ABORT")
    assert record.source == "gmail_draft"
    assert record.key.startswith("gmail_draft:r1:")


def test_every_draft_is_flagged_as_undelivered():
    payload = {"drafts": [_draft("r1", "2026-08-26T18:36:54Z", "ABORT Gate 4", "body")]}
    (record,) = load_gmail_drafts(payload)
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {code for code, _ in record.warnings}


def test_draft_without_a_timestamp_is_dropped_not_misfiled():
    payload = {"drafts": [{"id": "r1", "subject": "ABORT Gate 4"}]}
    assert load_gmail_drafts(payload) == []


def test_load_gmail_drafts_tolerates_missing_subject_and_body():
    payload = {"drafts": [{"id": "r1", "date": "2026-08-06T21:43:52Z"}]}
    (record,) = load_gmail_drafts(payload)
    assert (record.slot, record.gate, record.verdict) == (None, None, None)


def test_load_gmail_drafts_skips_non_mapping_entries():
    payload = {"drafts": ["junk", {"id": "r1", "date": "2026-08-26T18:36:54Z", "subject": "Gate 4"}]}
    assert len(load_gmail_drafts(payload)) == 1


def test_load_gmail_drafts_handles_an_empty_export():
    # The connector returns {} for zero matches, which is not an error.
    assert load_gmail_drafts({}) == []


# --- channel warnings --------------------------------------------------------


def test_gitignored_alert_path_is_flagged():
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "A copy was written to artifacts/alerts/2026-08-26-1430ET-gate4.txt in the repo.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "GITIGNORED_ALERT_PATH" in {code for code, _ in record.warnings}


def test_correct_stream_root_path_is_not_flagged():
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "Written via dca_paths.stream_root('intraday_dca', 'alerts', ...).",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "GITIGNORED_ALERT_PATH" not in {code for code, _ in record.warnings}


def test_slack_delivery_claim_is_flagged():
    payload = {
        "drafts": [_draft("r1", "2026-08-26T18:36:54Z", "ABORT Gate 4", "It was also posted to Slack.")]
    }
    (record,) = load_gmail_drafts(payload)
    assert "SLACK_DISABLED" in {code for code, _ in record.warnings}


# --- alert files -------------------------------------------------------------


def test_load_alert_files_reads_slot_and_gate_from_the_filename(tmp_path):
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text("nothing placed", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert (record.slot, record.gate, record.et_date) == ("1430", "Gate 4", date(2026, 8, 26))
    assert record.source == "alert_file"


def test_alert_files_are_not_flagged_as_undelivered(tmp_path):
    # Only drafts carry that warning: a written file is a real durable record.
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text("nothing placed", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in {code for code, _ in record.warnings}


def test_load_alert_files_falls_back_to_mtime_for_an_off_contract_name(tmp_path):
    (tmp_path / "notes.txt").write_text("ABORT at Gate 4 for the 2:30 PM ET slot", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert (record.slot, record.gate) == ("1430", "Gate 4")


def test_load_alert_files_recurses_into_per_stream_subdirectories(tmp_path):
    nested = tmp_path / "intraday_dca" / "alerts"
    nested.mkdir(parents=True)
    (nested / "2026-08-26-1130ET-gate7.txt").write_text("rejected", encoding="utf-8")
    assert len(load_alert_files(tmp_path)) == 1


def test_load_alert_files_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert load_alert_files(tmp_path / "nope") == []


# --- aggregation -------------------------------------------------------------


def test_build_digest_counts_by_gate_slot_and_verdict():
    digest = build_digest(
        [
            _record("a", "2026-08-24T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-25T15:38:00Z", "1130", "Gate 4"),
            _record("c", "2026-08-25T19:34:00Z", "1530", "Gate 7", verdict="REJECTED"),
        ]
    )
    assert digest.by_gate["Gate 4"] == 2
    assert digest.by_slot["1130"] == 2
    assert digest.by_verdict["REJECTED"] == 1
    assert digest.by_slot_gate[("1130", "Gate 4")] == 2


def test_streak_counts_consecutive_alerting_days_across_a_weekend():
    # Fri 2026-08-21 then Mon 2026-08-24. Calendar-day counting would call this
    # two separate one-day blips; it is one four-day run of the same failure.
    days = ["2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25"]
    digest = build_digest(
        [_record(f"r{i}", f"{d}T15:38:00Z", "1130", "Gate 4") for i, d in enumerate(days)]
    )
    top = digest.streaks[0]
    assert (top.slot, top.gate, top.days) == ("1130", "Gate 4", 4)
    assert (top.start, top.end) == (date(2026, 8, 20), date(2026, 8, 25))


def test_streak_breaks_when_the_slot_skips_a_day_the_corpus_covers():
    digest = build_digest(
        [
            _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
            # Another slot alerts on the 21st, so the 21st IS in the corpus
            # calendar and 1130's absence is a real break, not a market holiday.
            _record("b", "2026-08-21T19:38:00Z", "1530", "Gate 6"),
            _record("c", "2026-08-24T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    gate4 = next(s for s in digest.streaks if s.slot == "1130")
    assert gate4.days == 1


def test_streaks_are_ordered_longest_first():
    digest = build_digest(
        [
            _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-21T15:38:00Z", "1130", "Gate 4"),
            _record("c", "2026-08-20T19:38:00Z", "1530", "Gate 6"),
        ]
    )
    assert digest.streaks[0].days >= digest.streaks[-1].days
    assert digest.streaks[0].slot == "1130"


def test_records_missing_a_slot_or_gate_are_counted_not_dropped():
    digest = build_digest([_record("a", "2026-08-20T15:38:00Z", None, None, verdict=None)])
    assert digest.unknown_slot == 1 and digest.unknown_gate == 1
    assert digest.by_gate["unclassified"] == 1
    assert len(digest.records) == 1


def test_coverage_matrix_records_alerts_per_slot_per_day():
    digest = build_digest(
        [
            _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-20T19:38:00Z", "1530", "Gate 6"),
        ]
    )
    assert digest.coverage[date(2026, 8, 20)] == {"1130": 1, "1530": 1}
    assert digest.days == [date(2026, 8, 20)]


def test_digest_records_are_sorted_chronologically():
    digest = build_digest(
        [
            _record("late", "2026-08-25T15:38:00Z", "1130", "Gate 4"),
            _record("early", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    assert [r.alert_id for r in digest.records] == ["early", "late"]


# --- filtering ---------------------------------------------------------------


def test_filter_records_by_since_until_and_slot():
    records = [
        _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
        _record("b", "2026-08-25T15:38:00Z", "1130", "Gate 4"),
        _record("c", "2026-08-25T19:38:00Z", "1530", "Gate 6"),
    ]
    assert len(filter_records(records, since=date(2026, 8, 25), until=None, slot=None)) == 2
    assert len(filter_records(records, since=None, until=date(2026, 8, 20), slot=None)) == 1
    assert len(filter_records(records, since=None, until=None, slot="1530")) == 1


def test_filter_boundaries_are_inclusive():
    records = [_record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4")]
    kept = filter_records(records, since=date(2026, 8, 20), until=date(2026, 8, 20), slot=None)
    assert len(kept) == 1


# --- ledger ------------------------------------------------------------------


def test_ledger_round_trips(tmp_path):
    path = tmp_path / "seen_alerts.json"
    save_ledger(path, ["gmail_draft:a", "gmail_draft:b"])
    assert load_ledger(path)["seen"] == ["gmail_draft:a", "gmail_draft:b"]


def test_missing_ledger_reports_everything_as_new(tmp_path):
    assert load_ledger(tmp_path / "absent.json")["seen"] == []


def test_corrupt_ledger_degrades_to_empty_rather_than_failing(tmp_path):
    # A ledger is a convenience, not evidence -- losing it must never cost the
    # operator the digest itself.
    path = tmp_path / "seen_alerts.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_ledger(path)["seen"] == []


def test_ledger_holding_a_json_array_is_treated_as_empty(tmp_path):
    path = tmp_path / "seen_alerts.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_ledger(path)["seen"] == []


def test_save_ledger_deduplicates_and_sorts(tmp_path):
    path = tmp_path / "seen_alerts.json"
    save_ledger(path, ["b", "a", "a"])
    assert load_ledger(path)["seen"] == ["a", "b"]


# --- rendering ---------------------------------------------------------------


def test_render_text_reports_the_headline_numbers():
    digest = build_digest([_record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4")])
    text = render_text(digest)
    assert "alerts        : 1" in text
    assert "Gate 4" in text and "COVERAGE" in text


def test_render_text_on_an_empty_digest_says_silence_is_ambiguous():
    # The single most dangerous misreading: a clean slot and a slot that never
    # fired both produce zero alerts.
    text = render_text(build_digest([]))
    assert "silence is ambiguous" in text


def test_render_text_reports_delivery_warnings():
    digest = build_digest(
        [_record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4", warnings=[("SLACK_DISABLED", "off")])]
    )
    assert "SLACK_DISABLED x1" in render_text(digest)


def test_render_json_is_valid_and_carries_the_alert_list():
    digest = build_digest([_record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4")])
    payload = json.loads(render_json(digest, new_keys=["gmail_draft:a"]))
    assert payload["totals"]["alerts"] == 1
    assert payload["by_slot_gate"] == {"1130/Gate 4": 1}
    assert payload["new_since_last_digest"] == ["gmail_draft:a"]
    assert payload["alerts"][0]["et_date"] == "2026-08-20"


# --- CLI ---------------------------------------------------------------------


def _write_export(tmp_path: Path) -> Path:
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-25T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_requires_at_least_one_source(capsys):
    assert main([]) == 2
    assert "at least one source" in capsys.readouterr().err


def test_cli_renders_a_digest_from_a_draft_export(tmp_path, capsys):
    assert main(["--drafts", str(_write_export(tmp_path))]) == 0
    assert "alerts        : 2" in capsys.readouterr().out


def test_cli_json_mode_emits_parsable_json(tmp_path, capsys):
    assert main(["--drafts", str(_write_export(tmp_path)), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 2


def test_cli_since_filter_narrows_the_window(tmp_path, capsys):
    main(["--drafts", str(_write_export(tmp_path)), "--since", "2026-08-25", "--json"])
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 1


def test_cli_slot_filter_narrows_to_one_slot(tmp_path, capsys):
    main(["--drafts", str(_write_export(tmp_path)), "--slot", "1530", "--json"])
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 1


def test_cli_ledger_reports_new_alerts_only_on_the_second_run(tmp_path, capsys):
    export = _write_export(tmp_path)
    root = str(tmp_path / "ledger")
    main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--json"])
    assert len(json.loads(capsys.readouterr().out)["new_since_last_digest"]) == 2
    main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--json"])
    assert json.loads(capsys.readouterr().out)["new_since_last_digest"] == []


def test_cli_fail_on_warnings_exits_nonzero(tmp_path, capsys):
    # Every draft carries EMAIL_DRAFT_NOT_DELIVERED, so this always trips while
    # the email channel is still a draft-only channel -- which is the point.
    assert main(["--drafts", str(_write_export(tmp_path)), "--fail-on-warnings"]) == 1


def test_cli_reads_stdin(tmp_path, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(_write_export(tmp_path).read_text()))
    assert main(["--stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 2


def test_cli_reports_unreadable_input_instead_of_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["--drafts", str(bad)]) == 2
    assert "could not read alerts" in capsys.readouterr().err


def test_cli_rejects_a_json_export_that_is_not_an_object(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert main(["--drafts", str(bad)]) == 2


def test_collect_records_counts_drafts_it_had_to_skip(tmp_path):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [{"id": "r1", "subject": "no date here"}]}), encoding="utf-8"
    )
    records, skipped, _ = collect_records(drafts_path=path, alerts_dir=None, read_stdin=False)
    assert records == [] and skipped == 1


def test_collect_records_merges_both_sources(tmp_path):
    export = _write_export(tmp_path)
    alerts = tmp_path / "alerts"
    alerts.mkdir()
    (alerts / "2026-08-26-1130ET-gate7.txt").write_text("rejected", encoding="utf-8")
    records, _, _ = collect_records(drafts_path=export, alerts_dir=alerts, read_stdin=False)
    assert {r.source for r in records} == {"gmail_draft", "alert_file"}


# --- regressions from the 2026-08-26 Codex review ----------------------------


def test_am_clock_time_is_not_read_as_the_pm_slot():
    # "2:30 AM" is not the 2:30 PM slot. Off-schedule firings are part of this
    # corpus, so a meridiem-blind lookup files an overnight alert on a real slot.
    assert detect_slot("ABORT at 2:30 AM ET, scheduler fired overnight") is None
    assert detect_slot("ABORT at 9:35 PM ET") is None


def test_meridiem_aware_lookup_still_matches_the_real_slots():
    assert detect_slot("ABORT - 2:30 PM ET slot") == "1430"
    assert detect_slot("DCA 9:35 AM market-open slot") == "0935"


def test_slot_detection_skips_a_wrong_meridiem_time_and_keeps_scanning():
    # The 2026-08-14 batch led with the off-schedule firing time, then named the
    # slot. The first clock match must not end the search.
    text = "ABORTED - off-schedule firing at 9:07 PM ET - this is the 11:30 AM ET slot"
    assert detect_slot(text) == "1130"


def test_negated_channel_mention_is_not_flagged_as_a_delivery_claim():
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "Slack is disabled in config, so no Slack alert was posted.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "SLACK_DISABLED" not in {code for code, _ in record.warnings}


def test_negated_artifacts_path_mention_is_not_flagged():
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "No file was written to artifacts/alerts; used stream_root instead.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "GITIGNORED_ALERT_PATH" not in {code for code, _ in record.warnings}


def test_affirmative_claim_in_a_later_sentence_still_warns():
    # Negation is scoped per sentence, so an unrelated "no" elsewhere in the body
    # must not suppress a real claim.
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "No orders were placed. A copy was also posted to Slack.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "SLACK_DISABLED" in {code for code, _ in record.warnings}


def test_alert_file_slot_token_without_et_suffix_is_honored():
    # Gate 9 does not pin the slot spelling; a bare "1230" is an exact roster
    # slot the filename already gave us, and must not need detect_slot's ET rule.
    assert _slot_from_filename_token("1230") == "1230"
    assert _slot_from_filename_token("1430ET") == "1430"
    assert _slot_from_filename_token("nonsense") is None


def test_contract_filename_with_bare_slot_classifies(tmp_path):
    (tmp_path / "2026-08-26-1230-gate5.txt").write_text("nothing placed", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert record.slot == "1230"


def test_alert_file_identity_includes_its_relative_path(tmp_path):
    # Two streams can hold the same basename for the same day; a bare-basename
    # key would make the ledger treat the second as already seen.
    for stream in ("morning_dca", "intraday_dca"):
        nested = tmp_path / stream / "alerts"
        nested.mkdir(parents=True)
        (nested / "2026-08-26-1230-gate5.txt").write_text("x", encoding="utf-8")
    records = load_alert_files(tmp_path)
    assert len({r.key for r in records}) == 2


def test_digest_window_includes_unclassified_records():
    # An alert with a timestamp but no slot still happened on a day.
    digest = build_digest([_record("a", "2026-08-20T15:38:00Z", None, None, verdict=None)])
    assert digest.first_day == date(2026, 8, 20)
    assert digest.days == [date(2026, 8, 20)]


def test_unclassified_record_outside_the_classified_range_widens_the_window():
    digest = build_digest(
        [
            _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-25T15:38:00Z", None, None, verdict=None),
        ]
    )
    assert digest.last_day == date(2026, 8, 25)


def test_render_text_on_an_all_unclassified_digest_still_prints_a_window():
    text = render_text(build_digest([_record("a", "2026-08-20T15:38:00Z", None, None)]))
    assert "2026-08-20" in text


# --- cross-source merge ------------------------------------------------------


def _file_record(alert_id, when, slot, gate, warnings=()):
    return AlertRecord(
        alert_id=alert_id,
        source="alert_file",
        timestamp_utc=parse_timestamp(when),
        subject=alert_id,
        body="Reason: executor gate failure",
        slot=slot,
        gate=gate,
        verdict="ABORT",
        warnings=tuple(warnings),
    )


def test_same_event_in_both_sources_is_counted_once():
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [_file_record("2026-08-26-1430ET-gate4.txt", "2026-08-26T16:00:00Z", "1430", "Gate 4")],
    )
    assert len(merged) == 1
    # The durable copy wins: its slot/gate came from the executor's own filename.
    assert merged[0].source == "alert_file"


def test_merge_unions_warnings_from_both_copies():
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
        [
            _file_record(
                "f1", "2026-08-26T16:00:00Z", "1430", "Gate 4", warnings=[("SLACK_DISABLED", "x")]
            )
        ],
    )
    codes = {code for code, _ in merged[0].warnings}
    assert codes == {"EMAIL_DRAFT_NOT_DELIVERED", "SLACK_DISABLED"}


def test_merge_keeps_events_that_appear_in_only_one_source():
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [_file_record("f1", "2026-08-25T16:00:00Z", "1130", "Gate 7")],
    )
    assert len(merged) == 2


def test_merge_does_not_collapse_duplicates_within_one_source():
    # A slot that alerted twice on the same gate really did alert twice.
    merged = merge_cross_source(
        [
            _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
            _record("d2", "2026-08-26T18:50:00Z", "1430", "Gate 4"),
        ],
        [],
    )
    assert len(merged) == 2


def test_merge_keeps_unclassifiable_records_on_both_sides():
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", None, None, verdict=None)],
        [_file_record("f1", "2026-08-26T16:00:00Z", None, None)],
    )
    assert len(merged) == 2


def test_collect_records_deduplicates_across_the_two_sources(tmp_path):
    export = tmp_path / "drafts.json"
    export.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft(
                        "r1",
                        "2026-08-26T18:36:00Z",
                        "ABORT 2:30 PM ET slot Gate 4",
                        "Reason: executor gate failure",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    alerts = tmp_path / "alerts"
    alerts.mkdir()
    (alerts / "2026-08-26-1430ET-gate4.txt").write_text("Reason: executor gate failure", encoding="utf-8")
    records, _, _ = collect_records(drafts_path=export, alerts_dir=alerts, read_stdin=False)
    assert len(records) == 1


# --- empty stdin -------------------------------------------------------------


def test_empty_stdin_is_rejected_not_read_as_zero_alerts(capsys, monkeypatch):
    # The connector's zero-results form is `{}`; no bytes at all means the export
    # command failed, and a clean digest would mask a broken ingestion path.
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    assert main(["--stdin"]) == 2
    assert "could not read alerts" in capsys.readouterr().err


def test_explicit_empty_json_object_on_stdin_is_accepted(capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert main(["--stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 0


# --- fail-on-warnings scoping ------------------------------------------------


def test_fail_on_warnings_goes_green_once_the_backlog_is_already_seen(tmp_path, capsys):
    export = _write_export(tmp_path)
    root = str(tmp_path / "ledger")
    # First run: the backlog is new, so the gate trips.
    assert main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--fail-on-warnings"]) == 1
    capsys.readouterr()
    # Second run over the same export: nothing new, so a scheduled check clears.
    assert main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--fail-on-warnings"]) == 0


def test_fail_on_warnings_trips_again_when_a_new_alert_arrives(tmp_path, capsys):
    export = _write_export(tmp_path)
    root = str(tmp_path / "ledger")
    main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--fail-on-warnings"])
    capsys.readouterr()
    export.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-25T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                    _draft("r3", "2026-08-26T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--fail-on-warnings"]) == 1


def test_fail_on_warnings_without_a_ledger_still_trips_on_the_whole_window(tmp_path):
    # No ledger means no notion of "new", so every warning counts.
    assert main(["--drafts", str(_write_export(tmp_path)), "--fail-on-warnings"]) == 1


def test_ledger_run_still_reports_the_full_corpus_not_just_new_alerts(tmp_path, capsys):
    # Streaks and coverage need the history; only the warning gate is scoped.
    export = _write_export(tmp_path)
    root = str(tmp_path / "ledger")
    main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--json"])
    capsys.readouterr()
    main(["--drafts", str(export), "--ledger", "--ledger-root", root, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 2
    assert payload["new_since_last_digest"] == []


# --- regressions from the second (c455edc) Codex review ----------------------


def test_reason_code_outranks_the_explicit_failing_gate_line():
    # A compliant GATE5_DISABLED alert carries BOTH -- Gate 9 requires the
    # labelled line, and the verdict is still the named code. Checking the label
    # first filed every expected condition as an ordinary gate failure.
    body = "Failing gate: Gate 5\nVerdict: GATE5_DISABLED, no entry point exists."
    assert detect_gate("DCA ALERT 1130ET", body) == "GATE5_DISABLED"


def test_expected_overlap_code_outranks_its_own_failing_gate_line():
    body = "Failing gate: Gate 4\nClassified GATE4_EXPECTED_OVERLAP (legacy stream)."
    assert detect_gate("DCA ALERT", body) == "GATE4_EXPECTED_OVERLAP"


def test_declared_task_name_beats_an_incidental_clock_time():
    # A 3:30 slot quoting a 12:30 PM fill from another slot must stay 1530.
    text = "330-final-buying-run aborted; earlier fills at 12:30 PM ET from another slot"
    assert detect_slot(text) == "1530"


def test_declared_task_name_beats_an_off_schedule_firing_time():
    text = "1130-mornig-buying-run reported an off-schedule firing at 10:30 AM ET"
    assert detect_slot(text) == "1130"


def test_two_alert_files_matching_one_draft_do_not_crash_the_digest(tmp_path):
    # The recursive scan yields two files for one event; the draft is consumed by
    # the first, and the second must append rather than raise from list.index.
    draft = _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4")
    files = [
        _file_record("morning_dca/2026-08-26-1430ET-gate4.txt", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        _file_record("intraday_dca/2026-08-26-1430ET-gate4.txt", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
    ]
    merged = merge_cross_source([draft], files)
    assert len(merged) == 2
    assert all(r.source == "alert_file" for r in merged)


def test_collect_records_survives_two_files_matching_one_draft(tmp_path):
    export = tmp_path / "drafts.json"
    export.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft(
                        "r1",
                        "2026-08-26T18:36:00Z",
                        "ABORT 2:30 PM ET slot Gate 4",
                        "Reason: executor gate failure",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    # Real layout: <cron_root>/<stream>/alerts/<file>.txt
    root = tmp_path / "cron_tracking"
    for stream in ("morning_dca", "intraday_dca"):
        nested = root / stream / "alerts"
        nested.mkdir(parents=True)
        (nested / "2026-08-26-1430ET-gate4.txt").write_text("Reason: executor gate failure", encoding="utf-8")
    records, _, _ = collect_records(drafts_path=export, alerts_dir=root, read_stdin=False)
    assert len(records) == 2


def test_non_executor_draft_is_set_aside_and_counted(tmp_path):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-06T21:43:52Z", "", ""),
                ]
            }
        ),
        encoding="utf-8",
    )
    records, _, non_alerts = collect_records(drafts_path=path, alerts_dir=None, read_stdin=False)
    assert len(records) == 1 and non_alerts == 1


def test_keep_unmatched_restores_non_executor_drafts(tmp_path):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r2", "2026-08-06T21:43:52Z", "", "")]}), encoding="utf-8"
    )
    records, _, non_alerts = collect_records(
        drafts_path=path, alerts_dir=None, read_stdin=False, keep_unmatched=True
    )
    assert len(records) == 1 and non_alerts == 0


def test_an_unrelated_draft_alone_does_not_pin_fail_on_warnings_red(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r2", "2026-08-06T21:43:52Z", "", "")]}), encoding="utf-8"
    )
    assert main(["--drafts", str(path), "--fail-on-warnings"]) == 0


def test_a_partially_parsed_alert_is_kept_not_filtered():
    # Only a draft that yielded nothing at all is set aside; a half-written
    # alert is exactly what an operator most wants surfaced.
    assert looks_like_executor_alert(_record("a", "2026-08-20T15:38:00Z", None, "Gate 4"))
    assert looks_like_executor_alert(_record("a", "2026-08-20T15:38:00Z", "1130", None))
    assert not looks_like_executor_alert(
        _record("a", "2026-08-20T15:38:00Z", None, None, verdict=None)
    )


@pytest.mark.parametrize("bad", [{}, "drafts", 1, 0])
def test_non_array_drafts_field_is_rejected(bad):
    with pytest.raises(ValueError, match="must be a JSON array"):
        load_gmail_drafts({"drafts": bad})


def test_absent_drafts_field_is_an_empty_corpus_not_an_error():
    assert load_gmail_drafts({}) == []
    assert load_gmail_drafts({"drafts": []}) == []


def test_cli_reports_a_non_array_drafts_field_instead_of_a_traceback(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": 1}), encoding="utf-8")
    assert main(["--drafts", str(path)]) == 2
    assert "could not read alerts" in capsys.readouterr().err


def test_ledger_with_invalid_utf8_degrades_instead_of_crashing(tmp_path):
    path = tmp_path / "seen_alerts.json"
    path.write_bytes(b'{"seen": ["\xff\xfe invalid utf-8"]}')
    assert load_ledger(path)["seen"] == []


def test_ledger_key_changes_when_an_alert_is_rewritten_in_place():
    # Gate 9's filename is fixed per (day, slot, gate), so a second firing
    # overwrites the first; a location-only key would call it already seen.
    first = AlertRecord(
        alert_id="2026-08-26-1430ET-gate4.txt",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T16:00:00Z"),
        subject="2026-08-26-1430ET-gate4.txt",
        body="first firing",
        slot="1430",
        gate="Gate 4",
        verdict="ABORT",
    )
    second = replace(first, body="second firing, different reason")
    assert first.key != second.key


# --- trading calendar / roster-silent days -----------------------------------


def test_trading_days_between_excludes_weekends():
    days = trading_days_between(date(2026, 8, 21), date(2026, 8, 24))
    assert days == [date(2026, 8, 21), date(2026, 8, 24)]


def test_trading_days_between_excludes_an_nyse_holiday():
    # 2026-07-03 is the observed Independence Day closure.
    days = trading_days_between(date(2026, 7, 2), date(2026, 7, 6))
    assert date(2026, 7, 3) not in days
    assert date(2026, 7, 2) in days and date(2026, 7, 6) in days


def test_trading_days_between_handles_an_inverted_range():
    assert trading_days_between(date(2026, 8, 26), date(2026, 8, 20)) == []


def test_roster_silent_trading_day_gets_a_coverage_row():
    # Wed 2026-08-12 sits between two alerting days and nothing fired on it.
    digest = build_digest(
        [
            _record("a", "2026-08-11T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-13T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    assert date(2026, 8, 12) in digest.days
    assert digest.silent_days == [date(2026, 8, 12)]


def test_a_streak_does_not_bridge_a_roster_silent_trading_day():
    # This is the failure the calendar fix exists for: deriving the calendar
    # from observed alerts made these two days look consecutive.
    digest = build_digest(
        [
            _record("a", "2026-08-11T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-13T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    assert digest.streaks[0].days == 1


def test_a_streak_still_bridges_a_weekend():
    # Fri 2026-08-21 then Mon 2026-08-24: no trading day in between.
    digest = build_digest(
        [
            _record("a", "2026-08-21T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-24T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    assert digest.streaks[0].days == 2


def test_render_text_marks_roster_silent_days():
    digest = build_digest(
        [
            _record("a", "2026-08-11T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-13T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    text = render_text(digest)
    assert "roster silent" in text
    assert "ROSTER-SILENT TRADING DAYS: 1" in text


def test_since_widens_the_calendar_to_the_requested_window():
    digest = build_digest(
        [_record("a", "2026-08-26T15:38:00Z", "1130", "Gate 4")],
        window_start=date(2026, 8, 24),
        window_end=date(2026, 8, 26),
    )
    assert digest.days == [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    assert digest.silent_days == [date(2026, 8, 24), date(2026, 8, 25)]


def test_an_off_calendar_firing_still_gets_a_row():
    # A Saturday alert is itself a Gate 0/1 fault; it must not vanish from its
    # own report just because it falls outside the trading calendar.
    digest = build_digest([_record("a", "2026-08-22T15:38:00Z", "1130", "Gate 1")])
    assert date(2026, 8, 22) in digest.days
    assert digest.calendar == []
    assert "NOT a trading day" in render_text(digest)


def test_off_calendar_days_do_not_break_streak_computation():
    digest = build_digest(
        [
            _record("a", "2026-08-22T15:38:00Z", "1130", "Gate 1"),
            _record("b", "2026-08-23T15:38:00Z", "1130", "Gate 1"),
        ]
    )
    # Both days are weekend; neither has a calendar position, so no streak.
    assert digest.streaks == []


def test_render_json_reports_the_silent_day_totals():
    digest = build_digest(
        [
            _record("a", "2026-08-11T15:38:00Z", "1130", "Gate 4"),
            _record("b", "2026-08-13T15:38:00Z", "1130", "Gate 4"),
        ]
    )
    payload = json.loads(render_json(digest))
    assert payload["totals"]["roster_silent_trading_days"] == 1
    assert payload["roster_silent_days"] == ["2026-08-12"]


# --- regressions from the third (b84df67) Codex review -----------------------


def test_fractional_gates_are_not_folded_into_the_whole_number():
    # The repo runs a Gate 6.5 and a Gate 6.7 and names them constantly; a
    # single-digit capture merged all three into one frequency and one streak.
    assert detect_gate("ABORT at Gate 6.5", "") == "Gate 6.5"
    assert detect_gate("ABORT at Gate 6.7", "") == "Gate 6.7"
    assert detect_gate("ABORT at Gate 6", "") == "Gate 6"


def test_fractional_gate_in_the_failing_gate_line():
    body = "Failing gate: Gate 6.5 - per-ticker resize\nplaced nothing."
    assert detect_gate("DCA ALERT", body) == "Gate 6.5"


def test_fractional_gates_count_separately_in_the_digest():
    digest = build_digest(
        [
            _record("a", "2026-08-20T15:38:00Z", "1130", "Gate 6"),
            _record("b", "2026-08-20T15:39:00Z", "1130", "Gate 6.5"),
        ]
    )
    assert digest.by_gate["Gate 6"] == 1 and digest.by_gate["Gate 6.5"] == 1


def test_declared_task_name_beats_an_explicit_et_label_for_another_slot():
    # A 3:30 alert citing a prior 1230 ET fill must stay 1530.
    text = "330-final-buying-run: prior fill recorded at 1230 ET from another slot"
    assert detect_slot(text) == "1530"


def test_declared_task_name_beats_an_explicit_et_label_of_the_firing_time():
    text = "1130-mornig-buying-run fired off-schedule at 1030 ET"
    assert detect_slot(text) == "1130"


def test_explicit_et_label_still_wins_when_no_task_name_is_present():
    assert detect_slot("DCA midday (1230ET) run aborted") == "1230"


def test_a_window_of_trading_days_with_zero_alerts_renders_coverage():
    # The worst case: a total scheduler outage. It has no counts to print, so an
    # early return left it visible only in --json.
    digest = build_digest(
        [], window_start=date(2026, 8, 24), window_end=date(2026, 8, 26)
    )
    text = render_text(digest)
    assert "COVERAGE" in text
    assert "ROSTER-SILENT TRADING DAYS: 3" in text
    assert "Every one of the 3 trading days below is roster-silent." in text


def test_a_truly_empty_digest_still_returns_early():
    text = render_text(build_digest([]))
    assert "silence is ambiguous" in text
    assert "COVERAGE" not in text


def test_cli_reports_the_total_outage_window_in_text_mode(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    main(["--drafts", str(path), "--since", "2026-08-24", "--until", "2026-08-26"])
    assert "ROSTER-SILENT TRADING DAYS: 3" in capsys.readouterr().out


def test_missing_alerts_dir_is_an_error_not_an_empty_corpus(tmp_path, capsys):
    # Indistinguishable from a valid empty directory otherwise -- the same
    # masked-ingestion-failure that empty stdin is rejected for.
    assert main(["--alerts-dir", str(tmp_path / "not-mounted")]) == 2
    assert "could not read alerts" in capsys.readouterr().err


def test_existing_but_empty_alerts_dir_is_accepted(tmp_path, capsys):
    empty = tmp_path / "alerts"
    empty.mkdir()
    assert main(["--alerts-dir", str(empty), "--json"]) == 0


def test_merged_identity_changes_when_draft_evidence_is_added():
    # File ledgered first, matching draft arrives later: the merge keeps the
    # file's subject/body, so only the warnings distinguish the new record.
    file_only = _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4")
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
        [file_only],
    )
    assert merged[0].key != file_only.key


def test_slot_filter_does_not_invent_a_roster_silent_day(tmp_path, capsys):
    # 2026-08-25: 1130 quiet, 1530 alerted. Under --slot 1130 that is a gap in
    # one slot, not a scheduler-wide outage.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-25T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--slot", "1130", "--as-of", "2026-08-25", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1
    assert payload["roster_silent_days"] == []


def test_roster_silence_still_reported_when_no_slot_filter_is_used(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-11T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-13T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", "2026-08-13", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["roster_silent_days"] == ["2026-08-12"]


def test_build_digest_roster_observed_overrides_the_filtered_view():
    digest = build_digest(
        [_record("a", "2026-08-24T15:38:00Z", "1130", "Gate 4")],
        window_start=date(2026, 8, 24),
        window_end=date(2026, 8, 25),
        roster_observed={date(2026, 8, 24), date(2026, 8, 25)},
    )
    assert digest.silent_days == []


# --- regressions from the fourth (6d07368) Codex review ----------------------


def test_root_scan_ignores_txt_files_outside_an_alerts_directory(tmp_path):
    # The documented root scan must not ingest the rest of the cron tree.
    (tmp_path / "morning_dca").mkdir()
    (tmp_path / "morning_dca" / "run_notes.txt").write_text(
        "ABORT at Gate 4 for the 2:30 PM ET slot", encoding="utf-8"
    )
    alerts = tmp_path / "morning_dca" / "alerts"
    alerts.mkdir()
    (alerts / "2026-08-26-1430ET-gate4.txt").write_text("real alert", encoding="utf-8")
    records = load_alert_files(tmp_path)
    assert [r.alert_id for r in records] == ["morning_dca/alerts/2026-08-26-1430ET-gate4.txt"]


def test_files_directly_in_the_named_directory_are_still_read(tmp_path):
    # Pointing straight at an alerts dir asserts that is what it is.
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text("x", encoding="utf-8")
    assert len(load_alert_files(tmp_path)) == 1


def test_fractional_gate_in_a_contract_filename_is_parsed(tmp_path):
    (tmp_path / "2026-08-20-1130-gate6.5.txt").write_text("resized", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert record.et_date == date(2026, 8, 20)
    assert record.slot == "1130"
    assert record.gate == "Gate 6.5"


def test_explicit_null_drafts_field_is_rejected():
    with pytest.raises(ValueError, match="must be a JSON array"):
        load_gmail_drafts({"drafts": None})


def test_two_drafts_and_two_files_for_one_event_collapse_to_two():
    # Two real firings must stay two records, not become three.
    drafts = [
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        _record("d2", "2026-08-26T18:50:00Z", "1430", "Gate 4"),
    ]
    files = [
        _file_record("a/alerts/f1.txt", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        _file_record("b/alerts/f2.txt", "2026-08-26T16:10:00Z", "1430", "Gate 4"),
    ]
    merged = merge_cross_source(drafts, files)
    assert len(merged) == 2


def test_more_files_than_drafts_still_retains_the_extra_files():
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [
            _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
            _file_record("f2", "2026-08-26T16:10:00Z", "1430", "Gate 4"),
        ],
    )
    assert len(merged) == 2


def test_negated_reason_code_does_not_override_the_labelled_gate():
    body = "Failing gate: Gate 2; this is not GATE5_DISABLED, the repo was unreachable."
    assert detect_gate("DCA ALERT", body) == "Gate 2"


def test_reason_code_still_wins_when_its_sentence_says_no_entry_point_exists():
    # A real GATE5_DISABLED body says "no signal-generation entry point exists";
    # the narrow negation set must not suppress the code over that "no".
    body = "Failing gate: Gate 5\nGATE5_DISABLED: no signal-generation entry point exists."
    assert detect_gate("DCA ALERT", body) == "GATE5_DISABLED"


def test_skipped_is_a_recognized_verdict():
    assert detect_verdict("SKIPPED at Gate 6 after the cap boundary", "") == "SKIPPED"


def test_alert_file_stating_its_own_email_failure_is_flagged(tmp_path):
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text(
        "Aborted at Gate 4. Email could not be sent; a draft was created instead.",
        encoding="utf-8",
    )
    (record,) = load_alert_files(tmp_path)
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {code for code, _ in record.warnings}


def test_alert_file_reporting_successful_delivery_is_not_flagged(tmp_path):
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text(
        "Aborted at Gate 4. Email sent to the operator address.", encoding="utf-8"
    )
    (record,) = load_alert_files(tmp_path)
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in {code for code, _ in record.warnings}


def test_slot_filter_keeps_the_full_roster_window(tmp_path, capsys):
    # 1130 alerted only on 08-24; the roster spans 08-24..08-26. Bounding the
    # report by the selected slot would hide the days it went quiet.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-26T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    # --as-of pins the reporting day. Without it `--until` defaults to *today*,
    # so this assertion silently became a time bomb: it passed until the ET date
    # rolled past 2026-08-26 and then failed on the clock, not on the code.
    main(["--drafts", str(path), "--slot", "1130", "--as-of", "2026-08-26", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["first_et_day"] == "2026-08-24"
    assert payload["window"]["last_et_day"] == "2026-08-26"


def test_inverted_date_window_is_rejected(capsys):
    assert main(["--drafts", "unused.json", "--since", "2026-08-26", "--until", "2026-08-20"]) == 2
    assert "is later than" in capsys.readouterr().err


def test_ledger_is_not_committed_when_the_report_cannot_be_written(tmp_path, monkeypatch):
    export = _write_export(tmp_path)
    root = str(tmp_path / "ledger")

    class _BrokenStdout:
        def write(self, _text):
            raise BrokenPipeError("downstream closed")

        def flush(self):
            pass

    monkeypatch.setattr("sys.stdout", _BrokenStdout())
    with pytest.raises(BrokenPipeError):
        main(["--drafts", str(export), "--ledger", "--ledger-root", root])
    # Nothing reached the operator, so nothing may be marked as reported.
    assert load_ledger(ledger_path(root))["seen"] == []


# --- regressions from the fifth (ad0e792) Codex review -----------------------


def _message(msg_id, when, subject, body=""):
    return {"id": msg_id, "date": when, "subject": subject, "plaintextBody": body}


def test_sent_message_export_is_ingested():
    # After the prompt fix, alerts are sent -- they never appear in list_drafts.
    payload = {"messages": [_message("m1", "2026-08-26T18:36:00Z", "ABORT 2:30 PM ET Gate 4", "x")]}
    (record,) = load_gmail_messages(payload)
    assert (record.slot, record.gate, record.source) == ("1430", "Gate 4", "gmail_message")


def test_sent_message_is_not_flagged_as_undelivered():
    # A sent email IS delivery; only its own body can say otherwise.
    payload = {"messages": [_message("m1", "2026-08-26T18:36:00Z", "ABORT Gate 4", "x")]}
    (record,) = load_gmail_messages(payload)
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in {code for code, _ in record.warnings}


def test_sent_message_stating_its_own_failure_is_flagged():
    payload = {
        "messages": [
            _message("m1", "2026-08-26T18:36:00Z", "ABORT Gate 4", "Email could not be sent.")
        ]
    }
    (record,) = load_gmail_messages(payload)
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {code for code, _ in record.warnings}


def test_non_array_messages_field_is_rejected():
    with pytest.raises(ValueError, match="must be a JSON array"):
        load_gmail_messages({"messages": {}})


def test_cli_accepts_a_messages_export_as_the_only_source(tmp_path, capsys):
    path = tmp_path / "sent.json"
    path.write_text(
        json.dumps(
            {"messages": [_message("m1", "2026-08-26T18:36:00Z", "ABORT 2:30 PM ET Gate 4", "x")]}
        ),
        encoding="utf-8",
    )
    assert main(["--messages", str(path), "--as-of", "2026-08-26", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 1


def test_sent_alerts_do_not_read_as_roster_silence(tmp_path, capsys):
    # The failure mode this source exists to prevent: a working alert path that
    # the digest cannot see, reported as an outage.
    path = tmp_path / "sent.json"
    path.write_text(
        json.dumps(
            {"messages": [_message("m1", "2026-08-26T18:36:00Z", "ABORT 2:30 PM ET Gate 4", "x")]}
        ),
        encoding="utf-8",
    )
    main(["--messages", str(path), "--as-of", "2026-08-26", "--json"])
    assert json.loads(capsys.readouterr().out)["roster_silent_days"] == []


def test_open_ended_window_runs_to_the_reporting_day(tmp_path, capsys):
    # The roster stops after 08-20; an explicitly started, open-ended digest run
    # on 08-26 must show the trailing silence instead of ending at that alert.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {"drafts": [_draft("r1", "2026-08-20T15:38:00Z", "ABORT 11:30 AM ET Gate 4", "x")]}
        ),
        encoding="utf-8",
    )
    main(
        [
            "--drafts",
            str(path),
            "--since",
            "2026-08-20",
            "--as-of",
            "2026-08-26",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["first_et_day"] == "2026-08-20"
    assert payload["window"]["last_et_day"] == "2026-08-26"
    assert payload["roster_silent_days"] == [
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
    ]


def test_explicit_until_still_bounds_the_window(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {"drafts": [_draft("r1", "2026-08-20T15:38:00Z", "ABORT 11:30 AM ET Gate 4", "x")]}
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--until", "2026-08-20", "--as-of", "2026-08-26", "--json"])
    assert json.loads(capsys.readouterr().out)["window"]["last_et_day"] == "2026-08-20"


def test_drafts_and_stdin_together_are_rejected(tmp_path, capsys):
    # Both name the same kind of export; combining them doubles every count.
    assert main(["--drafts", str(_write_export(tmp_path)), "--stdin"]) == 2
    assert "only one" in capsys.readouterr().err


def test_a_verdict_alone_no_longer_admits_ordinary_mail():
    # "rejected", "skipped", "run report" are ordinary English.
    personal = _record("p1", "2026-08-20T15:38:00Z", None, None, verdict="REJECTED")
    assert not looks_like_executor_alert(personal)


def test_personal_draft_about_a_rejection_does_not_trip_the_warning_gate(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-20T15:38:00Z", "My application was rejected", "sadly")
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--drafts", str(path), "--as-of", "2026-08-20", "--fail-on-warnings"]) == 0


def test_merged_warnings_are_deduplicated_by_code():
    # Draft and file can both report undelivered email with different wording.
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
        [
            _file_record(
                "f1",
                "2026-08-26T16:00:00Z",
                "1430",
                "Gate 4",
                warnings=[("EMAIL_DRAFT_NOT_DELIVERED", "the alert itself states it")],
            )
        ],
    )
    codes = [code for code, _ in merged[0].warnings]
    assert codes.count("EMAIL_DRAFT_NOT_DELIVERED") == 1


def test_slot_filter_keeps_the_other_columns_truthful(tmp_path, capsys):
    # Filtering to 1130 must not render a real 1530 alert as a silent dot.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-24T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--slot", "1130", "--as-of", "2026-08-24", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1
    assert payload["coverage"]["2026-08-24"] == {"1130": 1, "1530": 1}


def test_alert_file_key_changes_when_rewritten_with_identical_text(tmp_path):
    # Same filename, same body, second firing: the mtime is the revision signal.
    path = tmp_path / "2026-08-26-1430ET-gate4.txt"
    path.write_text("identical text", encoding="utf-8")
    (first,) = load_alert_files(tmp_path)
    os.utime(path, (1_800_000_000, 1_800_000_000))
    (second,) = load_alert_files(tmp_path)
    assert first.key != second.key


# --- round 6: verdict precedence, reason-code casing, calendar bounds ---------


@pytest.mark.parametrize(
    "subject,expected",
    [
        # Each of these says it aborted AND names the specific outcome. The
        # specific one is what BY VERDICT exists to separate out.
        ("ABORTED -- cap breach at Gate 6", "CAP_BREACH"),
        ("Order rejected; aborting the batch", "REJECTED"),
        ("aborting after a partial batch", "PARTIAL"),
    ],
)
def test_specific_verdicts_outrank_the_generic_abort(subject, expected):
    assert detect_verdict(subject, "") == expected


def test_bare_abort_still_classifies_as_abort():
    # Nothing more specific present: ABORT is the fallback, not a casualty.
    assert detect_verdict("DCA ABORT -- 3:30 PM slot", "") == "ABORT"


def test_abort_outranks_an_incidentally_narrated_skip():
    assert detect_verdict("ABORTED at Gate 4, skipping the rest", "") == "ABORT"


def test_reason_codes_match_case_insensitively():
    # Every gate and verdict pattern is case-insensitive; these are too, and the
    # canonical uppercase label is what comes back either way.
    assert detect_gate("slot alert", "Failing gate: Gate 5; verdict gate5_disabled") == "GATE5_DISABLED"
    assert detect_gate("slot alert", "classified gate4_expected_overlap") == "GATE4_EXPECTED_OVERLAP"


def test_lowercase_reason_code_negation_is_still_honoured():
    assert detect_gate("slot alert", "Failing gate: Gate 2; this is not gate5_disabled") == "Gate 2"


def test_expected_days_stop_at_the_approved_trading_window():
    # The weekdays after the last approved in-window trading day are days the
    # scheduler's trigger window had already closed on, not days it died.
    last_window_day, next_trading_day = _approved_window_last_trading_day_and_next()
    days = expected_roster_days(last_window_day - timedelta(days=3), next_trading_day + timedelta(days=4))
    assert max(days) == last_window_day
    assert next_trading_day not in days
    # The raw NYSE calendar still has them; only the *expected* view is clamped.
    assert next_trading_day in trading_days_between(last_window_day - timedelta(days=3), next_trading_day + timedelta(days=4))


def test_expected_days_stop_before_the_approved_window_start():
    days = expected_roster_days(date(2026, 6, 25), date(2026, 7, 6))
    assert min(days) == date(2026, 7, 2)


def test_days_after_the_window_are_not_reported_as_roster_silent(tmp_path, capsys):
    last_window_day, next_trading_day = _approved_window_last_trading_day_and_next()
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", f"{last_window_day.isoformat()}T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    window_start = last_window_day - timedelta(days=3)
    as_of = next_trading_day + timedelta(days=4)
    main(["--drafts", str(path), "--since", window_start.isoformat(), "--as-of", as_of.isoformat(), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert next_trading_day.isoformat() not in payload["roster_silent_days"]
    assert payload["roster_silent_days"] == [
        day.isoformat() for day in expected_roster_days(window_start, last_window_day) if day < last_window_day
    ]


def test_unclamped_when_the_window_config_is_unreadable(tmp_path, monkeypatch):
    # Falls back to the raw NYSE calendar: over-reporting silence is the safe
    # direction for an alerting tool.
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert date(2026, 11, 2) in expected_roster_days(date(2026, 10, 28), date(2026, 11, 6))


# The early-close calendar is exercised against _compute_streaks directly rather
# than through build_digest: the two guards are independent and this is the one
# under test. (Until the 2026-09-18 window extension the stronger claim held --
# every configured early close fell after the then-2026-10-30 end, so the clamp
# left build_digest with an empty calendar. The 2026-11-27 early close is inside
# the window now, so only the independence argument still applies.)
def test_late_slot_streak_bridges_an_early_close_day():
    # 2026-11-27 is an early close at 1:00 PM ET, so the 1530 slot was not
    # scheduled that day. Failures either side of it are one repeat, not two.
    calendar = trading_days_between(date(2026, 11, 25), date(2026, 11, 30))
    assert date(2026, 11, 27) in calendar
    records = [
        _record("a", "2026-11-25T20:34:00Z", "1530", "Gate 4"),
        _record("b", "2026-11-30T20:34:00Z", "1530", "Gate 4"),
    ]
    streaks = _compute_streaks(records, calendar)
    assert [(s.slot, s.gate, s.days) for s in streaks] == [("1530", "Gate 4", 2)]


def test_early_slot_streak_still_breaks_across_an_early_close_day():
    # The 0935 slot trades normally on an early close, so its silence there is a
    # real gap and must still break the streak.
    calendar = trading_days_between(date(2026, 11, 25), date(2026, 11, 30))
    records = [
        _record("a", "2026-11-25T14:34:00Z", "0935", "Gate 4"),
        _record("b", "2026-11-30T14:34:00Z", "0935", "Gate 4"),
    ]
    assert [s.days for s in _compute_streaks(records, calendar)] == [1]


def test_since_later_than_the_reporting_day_is_rejected(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    # --until omitted, so the window end is --as-of, and this window is inverted
    # even though the earlier explicit --since/--until check never sees it.
    assert main(["--drafts", str(path), "--since", "2026-08-26", "--as-of", "2026-08-20"]) == 2
    assert "later than the reporting day (2026-08-20)" in capsys.readouterr().err


def test_since_later_than_an_explicit_until_names_that_bound(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    assert main(["--drafts", str(path), "--since", "2026-08-26", "--until", "2026-08-20"]) == 2
    assert "later than --until (2026-08-20)" in capsys.readouterr().err


def test_cross_source_merge_keeps_two_firings_with_different_reasons():
    # One retry loop, two incidents: the file is the LATER firing (Gate 9's
    # filename is fixed per day/slot/gate, so it overwrote the first), while both
    # emails survive. Pairing on (day, slot, gate) alone would collapse them.
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Reason: overlapping executor detected on the 1430 slot",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T18:50:00Z", "1430", "Gate 4"),
        body="Reason: broker snapshot could not be reconciled",
    )
    merged = merge_cross_source([draft], [alert_file])
    assert len(merged) == 2


def test_cross_source_merge_still_pairs_the_same_reason_retyped():
    # The two channels carry the reason retyped, not the same bytes.
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Reason: Overlapping executor detected.",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        body="reason:  overlapping executor detected",
    )
    merged = merge_cross_source([draft], [alert_file])
    assert len(merged) == 1
    assert merged[0].source == "alert_file"


def test_cross_source_merge_keeps_two_firings_with_different_verdicts():
    draft = replace(_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 6"), verdict="CAP_BREACH")
    alert_file = replace(_file_record("f1", "2026-08-26T18:50:00Z", "1430", "Gate 6"), verdict="REJECTED")
    assert len(merge_cross_source([draft], [alert_file])) == 2


def test_cross_source_merge_pairs_the_matching_draft_not_merely_the_first():
    # Two drafts queued for one event; the file corroborates the second. The
    # first must stay standing rather than be consumed in its place.
    first = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Reason: overlapping executor detected",
    )
    second = replace(
        _record("d2", "2026-08-26T18:50:00Z", "1430", "Gate 4"),
        body="Reason: broker snapshot unavailable",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T18:52:00Z", "1430", "Gate 4"),
        body="Reason: broker snapshot unavailable",
    )
    merged = merge_cross_source([first, second], [alert_file])
    assert len(merged) == 2
    assert {r.alert_id for r in merged} == {"d1", "f1"}


def test_a_record_declaring_no_reason_still_pairs():
    # Missing parsed content cannot prove that two same-day firings are one.
    draft = replace(_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"), body="")
    alert_file = replace(
        _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        body="Reason: overlapping executor detected",
    )
    assert len(merge_cross_source([draft], [alert_file])) == 2


# --- round 7 -----------------------------------------------------------------


def test_reason_code_survives_a_negation_that_modifies_its_explanation():
    # The standard phrasing of the condition itself: the "not" belongs to the
    # explanation, not to the code.
    body = "Verdict: GATE5_DISABLED because the entry point is not available"
    assert detect_gate("slot alert", body) == "GATE5_DISABLED"


def test_reason_code_is_still_disclaimed_by_a_preceding_negation():
    assert detect_gate("slot alert", "Failing gate: Gate 2; this is not GATE5_DISABLED") == "Gate 2"
    assert detect_gate("slot alert", "Failing gate: Gate 3; an earlier gate rather than GATE5_DISABLED") == "Gate 3"


def test_free_form_reasons_keep_two_firings_apart():
    # Gate 9 requires the reason to be stated, not labelled "Reason:".
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="aborted because quotes were stale",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T18:50:00Z", "1430", "Gate 4"),
        body="aborted because the budget was exhausted",
    )
    assert len(merge_cross_source([draft], [alert_file])) == 2


def test_the_same_free_form_reason_still_merges():
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Aborted because quotes were stale.",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        body="aborted because quotes were stale",
    )
    assert len(merge_cross_source([draft], [alert_file])) == 1


def test_labelled_reason_outranks_an_incidental_because_clause():
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Retried because the first attempt timed out.\nReason: overlapping executor",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        body="Reason: overlapping executor",
    )
    assert len(merge_cross_source([draft], [alert_file])) == 1


def test_slot_filter_still_renders_another_slots_off_calendar_firing(tmp_path, capsys):
    # A Saturday firing by 1530 while the report is filtered to 1130: the row
    # carries a real count and must not be dropped from the matrix.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-24T15:38:00Z", "ABORT 11:30 AM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-22T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--slot", "1130", "--as-of", "2026-08-24", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # coverage is rendered over digest.days, so its presence here is the check.
    assert payload["coverage"]["2026-08-22"] == {"1530": 1}


def test_an_out_of_window_weekday_is_not_called_a_non_trading_day(tmp_path, capsys):
    # The first ordinary trading day after the approved window is an off-schedule
    # firing, not an off-market one.
    _, next_trading_day = _approved_window_last_trading_day_and_next()
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", f"{next_trading_day.isoformat()}T20:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", next_trading_day.isoformat()])
    out = capsys.readouterr().out
    assert next_trading_day.isoformat() in out
    assert "outside the approved trading window" in out
    assert "NOT a trading day" not in out


def test_a_weekend_firing_is_still_called_a_non_trading_day(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-22T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", "2026-08-22"])
    assert "NOT a trading day" in capsys.readouterr().out


def test_is_trading_day_separates_holidays_from_the_approved_window():
    assert is_trading_day(date(2026, 11, 2))          # inside or outside the window, still open
    assert not is_trading_day(date(2026, 8, 22))      # Saturday
    assert not is_trading_day(date(2026, 11, 26))     # Thanksgiving


def test_drafts_without_an_id_keep_separate_ledger_keys():
    # Identical text, no id: without the timestamp fallback both collapse onto
    # one ledger key and the second undelivered alert reads as already seen.
    payload = {
        "drafts": [
            {"date": "2026-08-26T18:36:00Z", "subject": "ABORT 2:30 PM ET Gate 4", "plaintextBody": "x"},
            {"date": "2026-08-26T19:36:00Z", "subject": "ABORT 2:30 PM ET Gate 4", "plaintextBody": "x"},
        ]
    }
    first, second = load_gmail_drafts(payload)
    assert first.key != second.key


def test_root_level_diagnostics_are_not_ingested_from_a_cron_root(tmp_path):
    # The named directory is a cron root, not an alerts directory: a top-level
    # note is a note even when it mentions a gate in passing.
    (tmp_path / "cron_tracking").mkdir()
    root = tmp_path / "cron_tracking"
    (root / "drift_report.txt").write_text("weekly summary, nothing to report", encoding="utf-8")
    alerts = root / "morning_dca" / "alerts"
    alerts.mkdir(parents=True)
    (alerts / "2026-08-26-1430ET-gate4.txt").write_text("real alert", encoding="utf-8")
    records = load_alert_files(root)
    assert [r.alert_id for r in records] == ["morning_dca/alerts/2026-08-26-1430ET-gate4.txt"]


def test_a_root_level_file_naming_a_slot_and_gate_is_still_read(tmp_path):
    # The off-contract-name fallback stays: this one does look like an alert.
    (tmp_path / "notes.txt").write_text("ABORT at Gate 4 for the 2:30 PM ET slot", encoding="utf-8")
    assert len(load_alert_files(tmp_path)) == 1


def test_an_alerts_directory_named_outright_is_taken_at_face_value(tmp_path):
    alerts = tmp_path / "alerts"
    alerts.mkdir()
    (alerts / "freeform.txt").write_text("nothing parseable here", encoding="utf-8")
    assert len(load_alert_files(alerts)) == 1


# --- round 8 -----------------------------------------------------------------


def test_a_personal_draft_naming_only_a_clock_time_is_not_an_alert(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-26T14:00:00Z", "Lunch at 10:30 AM", "see you there")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(path), "--as-of", "2026-08-26", "--fail-on-warnings"]) == 0
    payload = capsys.readouterr().out
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in payload


def test_a_clock_time_plus_a_gate_is_still_an_alert(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-26T14:35:00Z", "ABORT at 10:30 AM", "Failing gate: Gate 4")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", "2026-08-26", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1
    assert payload["by_slot"] == {"1030": 1}


def test_detect_slot_evidence_names_the_signal_that_matched():
    assert detect_slot_evidence("1130-mornig-buying-run aborted") == ("1130", "task_name")
    assert detect_slot_evidence("DCA ALERT 1430 ET") == ("1430", "explicit_et")
    assert detect_slot_evidence("Lunch at 10:30 AM") == ("1030", "clock")
    assert detect_slot_evidence("the midday run") == ("1230", "stream_word")
    assert detect_slot_evidence("weekly summary") == (None, "")


@pytest.mark.parametrize(
    "subject,body",
    [
        # Evidence that DECLARES the slot still stands alone. A stream word does
        # not -- see test_a_stream_word_alone_is_not_an_alert (round 10).
        ("330-final-buying-run stopped", ""),
        ("DCA ALERT 1030 ET", "nothing placed"),
    ],
)
def test_a_slot_from_a_declared_signal_alone_is_still_an_alert(subject, body):
    (record,) = load_gmail_drafts({"drafts": [_draft("r1", "2026-08-26T14:00:00Z", subject, body)]})
    assert record.gate is None
    assert looks_like_executor_alert(record)


def test_a_root_level_file_naming_only_a_clock_time_is_not_ingested(tmp_path):
    (tmp_path / "cron_tracking").mkdir()
    root = tmp_path / "cron_tracking"
    (root / "standup.txt").write_text("sync moved to 10:30 AM", encoding="utf-8")
    assert load_alert_files(root) == []


# --- round 8, remainder ------------------------------------------------------


def test_a_drift_webhook_slack_mention_is_not_flagged():
    # config/notifications.json permits the env-gated drift webhook, and the live
    # prompts invoke `drift_detection.py --slack`. Warning on it would turn a
    # correct run red.
    body = "Posted the drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK."
    assert "SLACK_DISABLED" not in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_routine_slack_claim_is_still_flagged():
    body = "Alert posted to Slack for the 1430 slot."
    assert "SLACK_DISABLED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_routine_slack_claim_alongside_a_drift_mention_still_warns():
    # The exemption is scoped to the sentence carrying the Slack mention.
    body = "Drift check ran clean.\nAlert posted to Slack for the 1430 slot."
    assert "SLACK_DISABLED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_failed_send_is_flagged_from_an_alert_file():
    # The corrected prompts require a failed send to be recorded, not just an
    # absent send capability.
    for body in ("Email send failed (auth error); a draft was left instead.",
                 "Failed to send the alert email; throttled by the provider."):
        codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
        assert "EMAIL_DRAFT_NOT_DELIVERED" in codes, body


def test_an_empty_corpus_still_reports_its_window(tmp_path, capsys):
    # A scheduler that stopped entirely is the worst thing this tool reports, and
    # it was the one case that printed no calendar at all.
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    main(["--drafts", str(path), "--as-of", "2026-07-10", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 0
    # config/trading_window.json opens 2026-07-02.
    assert payload["roster_silent_days"][0] == "2026-07-02"
    assert payload["roster_silent_days"][-1] == "2026-07-10"


def test_an_empty_corpus_before_the_window_opens_reports_nothing(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    main(["--drafts", str(path), "--as-of", "2026-06-01", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["roster_silent_days"] == []


def test_an_explicit_since_still_wins_for_an_empty_corpus(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    main(["--drafts", str(path), "--since", "2026-08-24", "--as-of", "2026-08-26", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["roster_silent_days"] == ["2026-08-24", "2026-08-25", "2026-08-26"]


# --- round 9 -----------------------------------------------------------------


def test_a_bare_webhook_mention_does_not_exempt_a_routine_slack_claim():
    # The exemption has to NAME a permitted route; "webhook" alone does not.
    body = "Gate 5 alert posted to the Slack webhook."
    assert "SLACK_DISABLED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_the_named_permitted_routes_are_still_exempt():
    # "Drift finding posted to Slack." used to be in this list on the bare
    # "drift" word alone -- round 45 removed that alternative (see
    # test_permitted_slack_marker_requires_concrete_route_evidence), so a
    # claim naming no concrete route is no longer exempt even when its
    # subject is itself a drift finding.
    for body in (
        "Sent through DCA_ALERT_SLACK_WEBHOOK after an uncaught exception.",
        "Ran drift_detection.py --slack and it posted to Slack.",
    ):
        codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" not in codes, body


def test_a_windows_form_gitignored_path_is_flagged():
    body = r"Durable alert written to artifacts\alerts\2026-08-26-1430-gate4.txt"
    assert "GITIGNORED_ALERT_PATH" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_another_channels_send_failure_is_not_an_email_warning():
    body = "Slack webhook failed to send; email sent successfully."
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in {
        c for c, _ in detect_channel_warnings(body, source="alert_file")
    }


def test_an_email_send_failure_in_the_same_sentence_is_still_flagged():
    body = "The alert email failed to send (throttled); a draft was left instead."
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {
        c for c, _ in detect_channel_warnings(body, source="alert_file")
    }


def test_states_email_failure_is_sentence_scoped():
    assert states_email_failure("Email send failed after three attempts")
    assert not states_email_failure("Slack send failed.\nEmail was delivered.")


def test_a_specific_body_verdict_beats_a_generic_subject():
    # The common shape: a plain subject over a detailed body.
    assert detect_verdict("Run ABORTED", "cap breach at Gate 6") == "CAP_BREACH"
    assert detect_verdict("Run ABORTED", "the order was rejected by the broker") == "REJECTED"
    assert detect_verdict("Run ABORTED", "stopped mid-batch after two fills") == "PARTIAL"


def test_a_specific_subject_verdict_still_wins_over_the_body():
    assert detect_verdict("Gate 7 order rejected", "cap breach mentioned in passing") == "REJECTED"


def test_a_prior_event_in_the_body_does_not_override_the_subject():
    # The body is recounting history, not classifying this run.
    assert detect_verdict("ABORTED at Gate 4", "one order was rejected earlier") == "ABORT"
    assert detect_verdict("ABORTED at Gate 4", "a cap breach had already happened last week") == "ABORT"


def test_a_prior_event_specific_verdict_is_no_longer_resurrected():
    # Round 54 (Codex review: "Keep prior-only outcomes unclassified")
    # reversed this tier's old behavior: a specific verdict that appeared
    # ONLY in a prior-event sentence, with no generic wording anywhere,
    # used to be resurrected on the theory that reporting SOMETHING beats
    # leaving the alert unclassified -- but that unconditionally assigns a
    # HISTORICAL outcome to the CURRENT incident, corrupting verdict totals
    # and reconciliation. It now correctly stays unclassified.
    assert detect_verdict("DCA slot note", "one order was rejected earlier") is None


def test_a_prior_event_generic_verdict_in_the_body_does_not_beat_the_current_one():
    # "Previous run aborted" is history; "this run skipped" is the actual outcome.
    assert detect_verdict("", "Previous run aborted at Gate 2. This run skipped at Gate 5") == "SKIPPED"


def test_a_negated_generic_verdict_in_the_body_does_not_win():
    # The clause explicitly rules ABORT out in favor of SKIPPED.
    assert detect_verdict("", "the run did not abort; it skipped") == "SKIPPED"


def test_a_bare_generic_subject_verdict_still_matches():
    assert detect_verdict("DCA ABORT -- 3:30 PM slot", "") == "ABORT"


def test_a_negated_generic_subject_verdict_falls_through_to_the_body():
    assert detect_verdict("this is not an ABORT", "SKIPPED at Gate 6") == "SKIPPED"


def test_a_malformed_contract_date_does_not_suppress_the_whole_scan(tmp_path):
    (tmp_path / "2026-13-40-1030-gate5.txt").write_text("ABORT at Gate 5", encoding="utf-8")
    (tmp_path / "2026-08-26-1430ET-gate4.txt").write_text("ABORT at Gate 4", encoding="utf-8")
    records = load_alert_files(tmp_path)
    assert len(records) == 2
    # The malformed one falls back to its mtime and stays classified from its body.
    malformed = next(r for r in records if r.alert_id.startswith("2026-13-40"))
    assert malformed.gate == "Gate 5"


def test_a_malformed_contract_date_does_not_fail_the_cli(tmp_path, capsys):
    path = tmp_path / "2026-13-40-1030-gate5.txt"
    path.write_text("ABORT at Gate 5", encoding="utf-8")
    # A malformed contract date falls back to the file's mtime, which defaults to
    # "now" -- so with --as-of pinned in the past, the record fell outside the
    # window and the count went to zero the moment the real clock passed
    # 2026-08-26. Pin the mtime too, so both ends of the comparison are fixed.
    fixed = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc).timestamp()
    os.utime(path, (fixed, fixed))
    assert main(["--alerts-dir", str(tmp_path), "--as-of", "2026-08-26", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["alerts"] == 1


# --- round 10 ----------------------------------------------------------------


def test_a_stream_word_alone_is_not_an_alert():
    # "midday" is ordinary personal-mail vocabulary, like a bare clock time.
    (record,) = load_gmail_drafts({"drafts": [_draft("r1", "2026-08-26T14:00:00Z", "Midday lunch plan", "")]})
    assert (record.slot, record.slot_evidence) == ("1230", "stream_word")
    assert not looks_like_executor_alert(record)


def test_a_stream_word_with_a_gate_is_still_an_alert():
    (record,) = load_gmail_drafts(
        {"drafts": [_draft("r1", "2026-08-26T16:35:00Z", "midday slot aborted", "Failing gate: Gate 4")]}
    )
    assert looks_like_executor_alert(record)


def test_a_zero_padded_clock_resolves_to_its_slot():
    assert detect_slot_evidence("ABORT at 09:35 AM ET") == ("0935", "clock")
    assert detect_slot("the 9:35 AM slot") == "0935"
    # Unpadded hours are unaffected by the normalization.
    assert detect_slot("12:30 PM") == "1230"
    assert detect_slot("10:30 AM") == "1030"


def test_cap_already_blown_is_still_a_cap_breach():
    # "cap already blown" is one of _VERDICT_PATTERNS' own CAP_BREACH spellings
    # and describes THIS run -- the prior-event filter must not swallow it.
    assert detect_verdict("Run ABORTED", "Budget cap already blown at Gate 6") == "CAP_BREACH"


def test_a_genuine_prior_event_is_still_skipped():
    assert detect_verdict("ABORTED at Gate 4", "one order was rejected earlier") == "ABORT"
    assert detect_verdict("ABORTED at Gate 4", "a cap breach had already happened") == "ABORT"


@pytest.mark.parametrize(
    "body",
    [
        "Could not send email because authentication failed",
        "Couldn't send the alert email",
        "Sending email failed",
        "Was unable to send the alert email",
    ],
)
def test_active_voice_email_failures_are_flagged(body):
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_an_active_voice_failure_on_another_channel_is_not_an_email_warning():
    body = "Could not send the Slack post; the alert email went out fine."
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in {
        c for c, _ in detect_channel_warnings(body, source="alert_file")
    }


def test_appended_delivery_evidence_does_not_split_one_firing():
    # The corrected Gate 9 contract has the executor append non-delivery evidence
    # to the alert file, so the file copy runs on past the reason sentence.
    draft = replace(
        _record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4"),
        body="Aborted because quotes were stale.",
    )
    alert_file = replace(
        _file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4"),
        body="Aborted because quotes were stale. Email send attempt failed; draft created.",
    )
    merged = merge_cross_source([draft], [alert_file])
    assert len(merged) == 1


def test_an_unparseable_gmail_date_is_skipped_not_fatal(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "not-a-date", "ABORT 2:30 PM ET slot Gate 4", "x"),
                    _draft("r2", "2026-08-26T18:36:00Z", "ABORT 2:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--drafts", str(path), "--as-of", "2026-08-26", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1
    assert payload["totals"]["skipped_no_timestamp"] == 1


def test_an_unparseable_sent_message_date_is_skipped_not_fatal():
    payload = {
        "messages": [
            _message("m1", "nonsense", "ABORT Gate 4", "x"),
            _message("m2", "2026-08-26T18:36:00Z", "ABORT Gate 4", "x"),
        ]
    }
    assert len(load_gmail_messages(payload)) == 1


# --- round 12: JSON/text classification parity -------------------------------


def test_json_coverage_classifies_a_roster_silent_day(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-26T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--since", "2026-08-24", "--as-of", "2026-08-26", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["day_classification"]["2026-08-24"] == "roster_silent"
    assert payload["day_classification"]["2026-08-26"] == "expected"


def test_json_coverage_classifies_a_weekend_firing(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-22T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", "2026-08-22", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["day_classification"]["2026-08-22"] == "not_a_trading_day"


def test_json_coverage_classifies_a_day_outside_the_approved_window(tmp_path, capsys):
    _, next_trading_day = _approved_window_last_trading_day_and_next()
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", f"{next_trading_day.isoformat()}T20:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--as-of", next_trading_day.isoformat(), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["day_classification"][next_trading_day.isoformat()] == "outside_approved_trading_window"


def test_json_and_text_classification_agree_on_every_day(tmp_path, capsys):
    # The two renderers must never diverge again: walk every day JSON reports
    # and check its text-report marker matches.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("r1", "2026-08-22T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                    _draft("r2", "2026-11-02T20:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x"),
                ]
            }
        ),
        encoding="utf-8",
    )
    main(["--drafts", str(path), "--since", "2026-08-20", "--as-of", "2026-11-02", "--json"])
    json_payload = json.loads(capsys.readouterr().out)
    main(["--drafts", str(path), "--since", "2026-08-20", "--as-of", "2026-11-02"])
    text_output = capsys.readouterr().out
    marker_for = {
        "roster_silent": "roster silent",
        "outside_approved_trading_window": "outside the approved trading window",
        "not_a_trading_day": "NOT a trading day",
        "expected": None,
    }
    for day_str, classification in json_payload["day_classification"].items():
        expected_marker = marker_for[classification]
        line = next(line for line in text_output.splitlines() if line.strip().startswith(day_str))
        if expected_marker is None:
            for other in marker_for.values():
                if other:
                    assert other not in line, (day_str, classification, line)
        else:
            assert expected_marker in line, (day_str, classification, line)


# --- round 12, the rest ------------------------------------------------------


def test_the_no_capability_phrase_is_scoped_to_email():
    # Fresh evidence after the failed-send scoping fix: the separate "no send
    # capability" alternative was unqualified, so a routine claim about a
    # DIFFERENT connector's capability still tripped an email warning.
    body = "Slack connector has no send capability; email sent successfully"
    assert not states_email_failure(body)


def test_a_reason_code_negated_after_the_mention_is_rejected():
    # "GATE5_DISABLED was not the verdict" disclaims the code from the RIGHT,
    # which a before-only negation check could not see.
    body = "Failing gate: Gate 2; GATE5_DISABLED was not the verdict"
    assert detect_gate("", body) == "Gate 2"


def test_a_reason_code_survives_a_negation_in_a_later_explanation_clause():
    # Regression guard for the fix above: the after-side reach must stay short
    # enough not to reach into an explanation trailing the mention.
    body = "Verdict: GATE5_DISABLED because the entry point is not available"
    assert detect_gate("slot alert", body) == "GATE5_DISABLED"


def test_an_unpadded_935_filename_token_normalizes_to_the_roster_slot():
    assert _slot_from_filename_token("935") == "0935"


def test_a_bare_gate_mention_alone_is_not_an_alert():
    (record,) = load_gmail_drafts(
        {"drafts": [_draft("r1", "2026-08-26T14:00:00Z", "Meet at Gate 5 before boarding", "")]}
    )
    assert record.gate == "Gate 5"
    assert not looks_like_executor_alert(record)


def test_a_bare_gate_mention_corroborated_by_a_declared_slot_is_still_an_alert():
    (record,) = load_gmail_drafts(
        {
            "drafts": [
                _draft(
                    "r1",
                    "2026-08-26T14:00:00Z",
                    "1130-mornig-buying-run notes: meet at Gate 5",
                    "",
                )
            ]
        }
    )
    assert record.gate == "Gate 5"
    assert looks_like_executor_alert(record)


def test_failed_send_corroboration_is_scoped_to_the_clause_not_the_sentence():
    # Verifies that clause-level scoping prevents a failure in one channel (Slack)
    # from being misattributed to email within the same sentence.
    body = "Slack could not send, but the alert email went out fine"
    assert not states_email_failure(body)


def test_merge_preserves_the_counterpart_verdict_when_the_winner_has_none():
    draft = _record("d1", "2026-08-20T15:38:00Z", "1130", "Gate 4", verdict="REJECTED")
    alert_file = replace(
        _record("f1", "2026-08-20T15:38:00Z", "1130", "Gate 4", verdict=None),
        source="alert_file",
    )
    merged = merge_cross_source([draft], [alert_file])
    assert len(merged) == 1
    assert merged[0].verdict == "REJECTED"


def test_an_until_before_the_corpus_start_reports_no_fabricated_outage(tmp_path, capsys):
    # A corpus beginning 2026-08-26 read with --until 2026-08-20 has a real,
    # later implicit start. Deriving the start from an already-windowed
    # (and therefore empty) observed set instead fell back to the approved
    # trading window's start and reported weeks of fabricated roster-silent
    # days that never had a chance to alert in the first place.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {"drafts": [_draft("r1", "2026-08-26T18:36:00Z", "ABORT 2:30 PM ET slot Gate 4", "x")]}
        ),
        encoding="utf-8",
    )
    assert (
        main(["--drafts", str(path), "--until", "2026-08-20", "--as-of", "2026-08-26", "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["trading_days_in_window"] == 0
    assert payload["totals"]["roster_silent_trading_days"] == 0


def test_a_declared_slot_wins_over_an_earlier_firing_time():
    # The leading clock is the wrong-hour firing time; "the 11:30 AM slot" is
    # what the alert is actually reporting on. Returning the first mappable
    # clock files this under 1030 instead.
    assert detect_slot("Scheduler fired at 10:30 AM instead of the 11:30 AM slot") == "1130"


def test_a_negated_specific_verdict_does_not_beat_the_generic_subject():
    assert (
        detect_verdict("Run ABORTED", "The order was not rejected; quotes were unavailable at Gate 7")
        == "ABORT"
    )
    assert detect_verdict("Run ABORTED", "This was not a cap breach, just a routine stop") == "ABORT"
    assert detect_verdict("Run ABORTED", "The batch was not stopped mid-run") == "ABORT"


def test_a_reason_code_describing_an_earlier_run_is_ignored():
    body = "The previous run was GATE5_DISABLED; this run cannot reach the repo"
    assert detect_gate("ABORT at Gate 2", body) == "Gate 2"


def test_a_date_only_gmail_timestamp_is_rejected():
    with pytest.raises(ValueError):
        parse_timestamp("2026-08-26")


def test_a_date_only_export_item_is_skipped_not_misfiled(tmp_path, capsys):
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-26", "ABORT 2:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(path), "--as-of", "2026-08-26", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 0
    assert payload["totals"]["skipped_no_timestamp"] == 1


# --- round 14 ------------------------------------------------------------


def test_a_contrastive_prior_event_clause_does_not_disqualify_the_code():
    # The finding's own example: "unlike" opens a clause contrasting with the
    # PREVIOUS run, not disclaiming GATE5_DISABLED -- and "unlike" is also one
    # of _mention_is_negated's own disclaimer cues, close enough in raw
    # character distance to collide with the code if that check isn't also
    # clause-scoped.
    body = "Unlike the previous run, this run is GATE5_DISABLED because no entry point is available"
    assert detect_gate("", body) == "GATE5_DISABLED"


def test_a_genuine_historical_reason_code_is_still_ignored():
    body = "The previous run was GATE5_DISABLED; this run cannot reach the repo"
    assert detect_gate("ABORT at Gate 2", body) == "Gate 2"


def test_negation_clause_scoping_does_not_regress_existing_disclaimers():
    assert detect_gate(
        "DCA ALERT", "Failing gate: Gate 2; this is not GATE5_DISABLED, the repo was unreachable."
    ) == "Gate 2"
    assert (
        detect_gate("slot alert", "Failing gate: Gate 3; an earlier gate rather than GATE5_DISABLED")
        == "Gate 3"
    )
    assert detect_gate("", "Failing gate: Gate 2; GATE5_DISABLED was not the verdict") == "Gate 2"
    assert (
        detect_gate("slot alert", "Verdict: GATE5_DISABLED because the entry point is not available")
        == "GATE5_DISABLED"
    )


def test_a_prefixed_scheduled_slot_declaration_wins_over_the_firing_time():
    assert detect_slot("Scheduler fired at 10:30 AM instead of scheduled 11:30 AM") == "1130"
    assert detect_slot("Off-schedule at 9:35 AM; expected 10:30 AM run") == "1030"
    # The original suffix form ("... 11:30 AM slot") keeps working.
    assert detect_slot("Scheduler fired at 10:30 AM instead of the 11:30 AM slot") == "1130"


def test_and_splits_coordinated_channel_outcomes():
    body = "Slack could not send and email sent successfully"
    assert not states_email_failure(body)


def test_and_does_not_regress_a_real_failure_in_one_clause():
    body = "The alert email failed to send and a draft was left instead"
    assert states_email_failure(body)


def test_a_non_object_calendar_json_degrades_instead_of_crashing(tmp_path, monkeypatch):
    (tmp_path / "market_calendar.json").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert early_close_days() == frozenset()


def test_a_non_object_calendar_json_does_not_crash_the_cli(tmp_path, monkeypatch, capsys):
    (tmp_path / "market_calendar.json").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    export = tmp_path / "drafts.json"
    export.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-26T19:34:00Z", "ABORT 3:30 PM ET slot Gate 4", "x")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(export), "--as-of", "2026-08-26", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1


def test_a_configured_closure_the_formula_does_not_know_is_treated_as_a_holiday(tmp_path, monkeypatch):
    # An ad hoc closure config/market_calendar.json can carry that
    # near_close_dca.nyse_holidays()'s formula has no way to predict -- the
    # same field dca_runner.market_day_status treats as Gate 0's authority.
    (tmp_path / "market_calendar.json").write_text(
        json.dumps({"early_close_1300": [], "holidays_closed": ["2026-08-19"]}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert not is_trading_day(date(2026, 8, 19))
    assert date(2026, 8, 19) not in trading_days_between(date(2026, 8, 17), date(2026, 8, 21))


def test_the_computed_holiday_formula_still_applies_without_a_configured_closure(tmp_path, monkeypatch):
    (tmp_path / "market_calendar.json").write_text(
        json.dumps({"early_close_1300": [], "holidays_closed": []}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert not is_trading_day(date(2026, 1, 1))  # New Year's Day


def test_a_disclaimed_reason_code_mention_does_not_hide_the_affirmative_one():
    # find()-based single-occurrence scanning stopped at the disclaimed copy
    # and never reached the affirmative one later in the same sentence.
    body = "This was not GATE5_DISABLED, but this run is GATE5_DISABLED. Failing gate: Gate 5"
    assert detect_gate("", body) == "GATE5_DISABLED"


def test_a_negated_verdict_match_does_not_hide_the_real_one_after_it():
    # re.search()-based single-match scanning stopped at the negated
    # "rejected" and fell through to the generic ABORT subject instead of
    # reaching the affirmative "rejected" later in the same sentence.
    subject = "Run ABORTED"
    body = "The order was not rejected, but the retry was rejected by the broker"
    assert detect_verdict(subject, body) == "REJECTED"


def test_a_coordinated_predicate_still_inherits_its_shared_historical_subject():
    # _mention_has_prior_event reused _CLAUSE_SPLIT, which treats "and" as a
    # clause boundary -- correct for channel outcomes, but it stranded the
    # second half of a coordinated pair sharing one historical subject
    # without the "previous run" cue that marks both halves as history.
    subject = "Run ABORTED"
    body = "The previous run aborted and its order was rejected"
    assert detect_verdict(subject, body) == "ABORT"


def test_a_contrastive_and_clause_still_marks_only_its_own_predicate_as_history():
    # Regression guard for the narrower _PRIOR_EVENT_CLAUSE_SPLIT: a comma or
    # "but" boundary must still isolate an unrelated, non-historical clause
    # from a "previous run" cue elsewhere in the sentence -- only "and" is
    # exempted from acting as a boundary.
    #
    # "order" is in the body (round 54's REJECTED-order-context requirement,
    # see test_detect_verdict_scopes_rejected_to_order_outcomes) -- this
    # test's own point is the clause-scoping, not that requirement, so the
    # wording is adjusted to satisfy both rather than dropping either check.
    subject = "Run ABORTED"
    body = "The previous run aborted, but this run's order was rejected at Gate 6"
    assert detect_verdict(subject, body) == "REJECTED"


def test_a_disclaimed_bare_gate_mention_is_not_attributed():
    # The free-text gate fallback used to be a bare, unscoped search: an
    # explicitly ruled-out gate mention was still returned as the answer.
    subject = "1030-dca-recurring-buy ABORT"
    body = "This did not fail at Gate 5; repository access was unavailable"
    assert detect_gate(subject, body) is None


def test_an_ordinary_free_text_gate_mention_still_matches():
    assert detect_gate("", "Something happened near Gate 3") == "Gate 3"


def test_a_fractional_gate_free_text_mention_still_matches():
    # Regression guard: the free-text fallback's negation screening must not
    # be sentence-scoped, or splitting on the "." in "6.5" would corrupt it.
    assert detect_gate("ABORT at Gate 6.5", "") == "Gate 6.5"


def test_a_successful_retry_clears_an_earlier_email_failure_claim():
    body = "Email could not be sent on the first attempt, but the retry email sent successfully"
    assert not states_email_failure(body)


def test_an_email_failure_with_no_later_success_is_still_flagged():
    assert states_email_failure("The alert email failed to send and a draft was left instead")


def test_and_split_channel_outcomes_still_do_not_regress_after_the_retry_fix():
    assert not states_email_failure("Slack could not send and email sent successfully")


def test_a_declared_slot_wins_over_an_explicit_et_firing_time():
    # The generic "1030 ET" explicit-ET label used to be checked before the
    # declared-clock scan, so it grabbed the wrong-hour firing time before the
    # sentence's own "scheduled 11:30 AM" declaration was ever reached.
    text = "Scheduler fired at 1030 ET instead of scheduled 11:30 AM"
    assert detect_slot(text) == "1130"


def test_a_leftmost_task_name_wins_over_table_order():
    # The task-name loop used to return whichever name came first in
    # _TASK_NAME_TO_SLOT, not whichever appeared first in the alert -- so a
    # 3:30 alert whose body cited an unrelated 10:30 task name was
    # misclassified as 1030 purely because that entry sits earlier in the
    # table.
    text = "330-final-buying-run ABORT\nConflict with 1030-dca-recurring-buy"
    assert detect_slot(text) == "1530"


def test_explicit_et_still_wins_with_no_task_name_or_declared_clock():
    assert detect_slot("DCA midday (1230ET) run aborted") == "1230"


def test_a_declared_four_digit_et_slot_wins_over_the_firing_time():
    # The declared-clock check only recognized the AM/PM form of a
    # prefixed declaration ("scheduled 11:30 AM"), not the equally
    # canonical four-digit ET form ("scheduled 1130 ET").
    text = "Scheduler fired at 1030 ET instead of expected 1130 ET"
    assert detect_slot(text) == "1130"


def test_a_declared_am_pm_slot_still_wins_after_the_et_form_is_added():
    text = "Scheduler fired at 1030 ET instead of scheduled 11:30 AM"
    assert detect_slot(text) == "1130"


def test_a_retry_success_in_a_later_sentence_clears_an_earlier_failure():
    # states_email_failure used to only look for an override in the SAME
    # sentence as the failure; a retry's outcome narrated in its own
    # sentence was invisible to it.
    body = "Email could not be sent. Retried, and email sent successfully."
    assert not states_email_failure(body)


def test_a_later_unrelated_success_does_not_hide_a_real_failure():
    body = "The alert email failed to send and a draft was left instead. Slack sent successfully."
    assert states_email_failure(body)


def test_the_json_ledger_key_is_attached_to_its_own_alert_record(tmp_path):
    # --ledger --json used to emit new_since_last_digest as opaque keys with
    # no way to map any one of them back to the alert it identifies.
    (tmp_path / "2026-08-26-1030-gate5.txt").write_text("ABORT at Gate 5", encoding="utf-8")
    records = load_alert_files(tmp_path)
    digest = build_digest(records)
    payload = json.loads(render_json(digest, new_keys=[records[0].key]))
    assert payload["alerts"][0]["key"] == payload["new_since_last_digest"][0]


def test_a_gitignored_storage_path_is_flagged_even_when_the_body_omits_it():
    # The check used to inspect only the body; a file's own on-disk location
    # under a bare repo-relative artifacts/alerts is discarded by
    # .gitignore whether or not the alert's text mentions its own path.
    warnings = detect_channel_warnings(
        "nothing placed",
        source="alert_file",
        path_hint="workspace/cron_tracking/morning_dca/artifacts/alerts/2026-08-26-1030-gate5.txt",
    )
    assert any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_a_multi_segment_nested_cron_tracking_path_is_flagged():
    # This test previously asserted the OPPOSITE: that a bare-relative
    # multi-segment nesting under cron_tracking/ (a stream name followed by
    # a further subdirectory before "alerts") was NOT gitignored evidence.
    # That premise was wrong -- .gitignore's bare `cron_tracking/` entry
    # discards the ENTIRE subtree at any depth, and the live, canonical
    # 1530ET prompt's own REQUIRED call is exactly this shape:
    # stream_root('ce16ba6b', 'scheduled_runs', 'alerts', ...) (Codex
    # review: "Cover nested paths under the default cron root"). Corrected
    # here rather than left passing on a false assumption.
    warnings = detect_channel_warnings(
        "nothing to report",
        source="alert_file",
        path_hint="workspace/cron_tracking/ce16ba6b/scheduled_runs/alerts/2026-08-26-1030-gate5.txt",
    )
    assert any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_a_final_failure_after_an_intervening_success_is_still_flagged():
    # search() only ever inspects the FIRST _EMAIL_FAILURE occurrence, so a
    # later, ultimate failure after an intervening success was invisible.
    body = "Email could not be sent, then email sent successfully, but email wasn't delivered"
    assert states_email_failure(body)


def test_a_bare_number_after_the_failing_gate_label_is_recognized():
    assert detect_gate("", "Failing gate: 5") == "Gate 5"
    assert detect_gate("", "Failing gate: 6.5") == "Gate 6.5"


def test_the_gate_word_form_still_works_alongside_the_bare_number_form():
    assert detect_gate("", "Failing gate: Gate 5") == "Gate 5"


def test_a_drift_mention_in_an_unrelated_clause_does_not_exempt_a_routine_slack_claim():
    # The exemption used to search the whole sentence, so an unrelated drift
    # observation joined by "and" exempted a routine Slack claim it has
    # nothing to do with.
    body = "Posted the Gate 5 alert to Slack, and portfolio drift was unchanged"
    assert "SLACK_DISABLED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_an_and_joined_cause_and_effect_drift_claim_is_still_exempt():
    # Regression guard: "and" alone (no comma) still narrates one action, so
    # the "--slack" evidence in the first clause must still cover the second.
    body = "Ran drift_detection.py --slack and it posted to Slack."
    assert "SLACK_DISABLED" not in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_path_outside_the_checkout_is_not_flagged_as_gitignored(tmp_path):
    # A durable artifact root can live entirely outside this repository and
    # still happen to be named .../artifacts/alerts -- this repo's
    # .gitignore has no effect on a tree it isn't part of.
    external = tmp_path / "mnt" / "artifacts" / "alerts"
    external.mkdir(parents=True)
    (external / "2026-08-26-1030-gate5.txt").write_text("ABORT at Gate 5", encoding="utf-8")
    records = load_alert_files(external)
    assert "GITIGNORED_ALERT_PATH" not in {code for code, _ in records[0].warnings}


def test_every_slack_claim_in_a_sentence_is_inspected_for_exemption():
    # _is_affirmative_claim used to stop at the first Slack match in the
    # sentence; an exempt claim before an unexempt one cleared the whole
    # sentence without ever reaching the real one.
    body = "Posted drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK, but also posted the Gate 5 alert to Slack"
    assert "SLACK_DISABLED" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_labelled_gate_on_a_historical_mention_is_screened():
    # The labelled "Failing gate:" search had no prior-event screening, so a
    # comparison against a previous run's gate was attributed to this run.
    body = "Previous run's failing gate: Gate 5, but this run failed at Gate 2"
    assert detect_gate("", body) == "Gate 2"


def test_a_semicolon_joined_labelled_gate_is_not_confused_by_a_later_negation():
    # Regression guard: disabling negation screening on the labelled match
    # (to protect it from decimal-splitting) must not resurrect the
    # semicolon-joined false-negative this was meant to avoid.
    assert detect_gate("slot alert", "Failing gate: Gate 2; this is not GATE5_DISABLED") == "Gate 2"
    assert (
        detect_gate("slot alert", "Failing gate: Gate 3; an earlier gate rather than GATE5_DISABLED")
        == "Gate 3"
    )


def test_an_inverted_configured_trading_window_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "trading_window.json").write_text(
        json.dumps({"start_date": "2026-10-30", "end_date": "2026-01-01"}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert approved_trading_window() is None


def test_a_correctly_ordered_configured_trading_window_still_works(tmp_path, monkeypatch):
    (tmp_path / "trading_window.json").write_text(
        json.dumps({"start_date": "2026-01-01", "end_date": "2026-10-30"}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert approved_trading_window() == (date(2026, 1, 1), date(2026, 10, 30))


def test_keep_unmatched_audit_records_do_not_hide_roster_silence(tmp_path, capsys):
    # A --keep-unmatched personal draft ("Lunch at midday") parses to the
    # 1230 slot via the weak stream-word signal alone; it must not count as
    # roster evidence, or a genuinely roster-silent trading day would be
    # masked by an unrelated draft that only shares a word with the slot.
    export = tmp_path / "drafts.json"
    export.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-20T17:30:00Z", "Lunch at midday", "nothing to report")]}),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--drafts",
                str(export),
                "--keep-unmatched",
                "--json",
                "--since",
                "2026-08-20",
                "--until",
                "2026-08-20",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["day_classification"]["2026-08-20"] == "roster_silent"
    assert payload["totals"]["alerts"] == 1


def test_a_cross_sentence_success_check_respects_sentence_boundaries():
    # The success-override check used _CLAUSE_SPLIT alone, which doesn't
    # split on "." or ";", so a success reported in its own SENTENCE (not
    # joined to the failure by a comma/conjunction) had a span starting at
    # offset 0 -- before the failure it was supposed to override.
    body = "Email failed to send. Retry email sent successfully."
    assert not states_email_failure(body)


def test_a_later_negated_labelled_gate_is_screened():
    # Disabling negation entirely on the labelled match (the round-20 fix)
    # let an explicitly disclaimed FIRST labelled value win over the real,
    # affirmative second one.
    body = "Failing gate: Gate 5 was not the verdict; failing gate: Gate 2"
    assert detect_gate("", body) == "Gate 2"


def test_a_labelled_slot_field_is_recognized():
    assert detect_slot_evidence("Slot: 1030") == ("1030", "labelled")
    assert detect_slot_evidence("slot 1230") == ("1230", "labelled")


def test_a_declared_signal_still_beats_a_labelled_slot_field():
    # Task name and declared-clock evidence remain stronger than a bare
    # "Slot: N" field when both are present.
    text = "330-final-buying-run: prior fill recorded at 1230 ET from another slot"
    assert detect_slot_evidence(text) == ("1530", "task_name")


def test_keep_unmatched_audit_records_do_not_widen_the_resolved_calendar(tmp_path, capsys):
    # _resolved_window_start's UNFILTERED corpus bound must ignore an
    # audit-only --keep-unmatched draft too: with NO real alerts and only a
    # personal draft, the calendar must fall back to the configured trading
    # window's start rather than the draft's own date, or the days between
    # the two would silently vanish from roster-silent coverage.
    export = tmp_path / "drafts.json"
    export.write_text(
        json.dumps({"drafts": [_draft("r1", "2026-08-20T17:30:00Z", "Lunch at midday", "nothing to report")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(export), "--keep-unmatched", "--json", "--until", "2026-08-20"]) == 0
    payload = json.loads(capsys.readouterr().out)
    window = approved_trading_window()
    assert window is not None
    assert payload["window"]["first_et_day"] == window[0].isoformat()


def test_a_negated_email_success_claim_does_not_clear_a_failure():
    assert states_email_failure("Email could not be sent. Retry email was not successfully sent.")
    assert states_email_failure("Email could not be sent. Retry email never delivered successfully.")


def test_an_unnegated_cross_sentence_success_still_clears():
    assert not states_email_failure("Email could not be sent. Retry email sent successfully.")


def test_a_historical_labelled_slot_is_skipped_for_the_current_one():
    text = "Previous run's slot: 1030; this run's Slot: 1130"
    assert detect_slot_evidence(text) == ("1130", "labelled")


def test_a_semicolon_bounds_free_text_gate_negation_too():
    # The free-text gate fallback's negation used the default (non-semicolon)
    # splitter even though it operates on unsplit text, so a "not" that
    # correctly disclaimed a nearby reason code also reached back across the
    # semicolon and disclaimed an unrelated free-text gate mention.
    body = "This run failed at Gate 2; this is not GATE5_DISABLED"
    assert detect_gate("", body) == "Gate 2"


def test_a_declared_et_date_wins_over_a_late_transport_timestamp():
    # An email delivered after midnight ET for the PRIOR trading day's slot
    # would otherwise land under the wrong day.
    record = AlertRecord(
        alert_id="r1",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-27T02:15:00Z"),
        subject="ABORT Gate 4 -- Date: 2026-08-26",
        body="",
        slot="1530",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_no_declared_date_falls_back_to_the_transport_timestamp():
    record = _record("r1", "2026-08-26T19:34:00Z", "1530", "Gate 4")
    assert record.et_date == date(2026, 8, 26)


def test_a_prior_narrated_date_is_not_mistaken_for_the_declared_one():
    # The declared-date pattern requires the literal "date" label -- a bare
    # ISO-shaped date narrating an unrelated PRIOR event must not be read as
    # this alert's own date.
    record = AlertRecord(
        alert_id="r1",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T19:34:00Z"),
        subject="ABORT Gate 4",
        body="aborted on 2026-08-19 for the same reason",
        slot="1530",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_a_draft_and_its_successful_retry_message_are_reconciled(tmp_path, capsys):
    # An initial failed send leaves a draft; the retry that succeeds
    # survives as a sent message. Together they are ONE incident, not two,
    # and the message's proof of delivery clears the draft's stale
    # non-delivery warning.
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps(
            {
                "drafts": [
                    {
                        **_draft("d1", "2026-08-26T19:30:00Z", "ABORT 1030 ET Gate 4", "Reason: x"),
                        "threadId": "delivery-1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    messages = tmp_path / "messages.json"
    messages.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        **_message("m1", "2026-08-26T19:35:00Z", "ABORT 1030 ET Gate 4", "Reason: x"),
                        "threadId": "delivery-1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--drafts", str(drafts), "--messages", str(messages), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 1
    assert payload["warnings"] == []


def test_a_draft_with_no_matching_message_still_warns(tmp_path, capsys):
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps({"drafts": [_draft("d1", "2026-08-26T19:30:00Z", "ABORT 1030 ET Gate 4", "x")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(drafts), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    codes = {w["code"] for w in payload["warnings"]}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_a_draft_and_a_contradicting_message_stay_two_incidents(tmp_path, capsys):
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps({"drafts": [_draft("d1", "2026-08-26T19:30:00Z", "ABORT 1030 ET Gate 4", "x")]}),
        encoding="utf-8",
    )
    messages = tmp_path / "messages.json"
    messages.write_text(
        json.dumps({"messages": [_message("m1", "2026-08-26T19:35:00Z", "REJECTED 1030 ET Gate 4", "x")]}),
        encoding="utf-8",
    )
    assert main(["--drafts", str(drafts), "--messages", str(messages), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["alerts"] == 2


# --- round 23 ------------------------------------------------------------


def test_a_reason_code_preceding_rather_than_is_still_affirmed():
    # "rather than"/"instead of"/"unlike"/"as opposed to" always negate their
    # OBJECT -- the thing named AFTER them -- never the thing before it.
    # "GATE5_DISABLED rather than Gate 5" affirms GATE5_DISABLED and rejects
    # only "Gate 5"; checking the cue in the mention's AFTER-reach (the old,
    # undirected behavior) read this backwards and disclaimed the very
    # mention it affirms.
    assert detect_gate("slot alert", "GATE5_DISABLED rather than Gate 5") == "GATE5_DISABLED"


def test_a_reason_code_following_rather_than_is_still_disclaimed():
    # Regression guard: the cue still disclaims a mention that comes AFTER
    # it, which is its actual grammatical object.
    assert (
        detect_gate("slot alert", "Failing gate: Gate 3; an earlier gate rather than GATE5_DISABLED")
        == "Gate 3"
    )


def test_a_declared_prior_event_scheduled_clock_does_not_shadow_the_labelled_slot():
    # The finding's own example: an unscreened "het" (declared four-digit
    # "scheduled/expected N ET") match returned immediately on the FIRST
    # occurrence in the text, even when that occurrence narrates a PRIOR
    # run's slot and the current run's own labelled "Slot:" field appears
    # later in the same text.
    text = "Previous run was scheduled 1030 ET; this run's Slot: 1130"
    assert detect_slot_evidence(text) == ("1130", "labelled")


def test_a_negation_word_trailing_the_success_clause_does_not_block_the_override():
    # "Retry email sent successfully without the fallback connector" has
    # "without" modifying "the fallback connector", well after the success
    # phrase -- a clause-wide negation search read that as negating the send
    # itself and kept reporting the earlier, since-retried failure.
    body = "Email failed to send. Retry email sent successfully without the fallback connector."
    assert not states_email_failure(body)


def test_an_alert_file_keeps_its_filename_derived_date_over_a_body_narrated_one():
    # A gmail_draft/gmail_message record's declared "Date:" label wins over
    # its transport timestamp (see test_a_declared_et_date_wins_over_a_late_
    # transport_timestamp above) -- but an alert_file's day already comes
    # from the Gate 9 contract filename, which is authoritative by
    # construction, and its body can narrate an unrelated PRIOR date under
    # the same label ("Previous run Date: 2026-08-25 aborted for the same
    # reason") without that being this file's actual day.
    record = AlertRecord(
        alert_id="2026-08-26-1530-gate4.txt",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T16:00:00Z"),
        subject="2026-08-26-1530-gate4.txt",
        body="Previous run Date: 2026-08-25 aborted for the same reason",
        slot="1530",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def _message_record(alert_id, when, slot, gate, warnings=()):
    return AlertRecord(
        alert_id=alert_id,
        source="gmail_message",
        timestamp_utc=parse_timestamp(when),
        subject=alert_id,
        body="Reason: executor gate failure. Retry email sent.",
        slot=slot,
        gate=gate,
        verdict="ABORT",
        warnings=tuple(warnings),
    )


def test_merge_drops_the_files_stale_draft_warning_when_the_counterpart_is_a_sent_message():
    # A gmail_message counterpart is direct proof the email was sent --
    # potentially the very retry the file's own body narrates as a failure
    # (an initial attempt failed, was recorded in the file, and then
    # succeeded on retry). Folding the file's EMAIL_DRAFT_NOT_DELIVERED
    # warning in over that proof would restore a claim the message already
    # resolves.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [
            _file_record(
                "f1",
                "2026-08-26T16:00:00Z",
                "1430",
                "Gate 4",
                warnings=[_DRAFT_WARNING, ("SLACK_DISABLED", "x")],
            )
        ],
    )
    assert len(merged) == 1
    codes = {code for code, _ in merged[0].warnings}
    assert _DRAFT_WARNING[0] not in codes
    assert "SLACK_DISABLED" in codes


def test_merge_still_unions_the_draft_warning_when_the_counterpart_is_a_draft():
    # Regression guard: the drop is scoped to a `gmail_message` counterpart
    # only. An ordinary draft counterpart (no proof of a successful send)
    # must still carry EMAIL_DRAFT_NOT_DELIVERED into the merged record, per
    # test_merge_unions_warnings_from_both_copies above.
    merged = merge_cross_source(
        [_record("d1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [_file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] in {code for code, _ in merged[0].warnings}



def test_gitignored_path_drops_trailing_punctuation_from_sentence(tmp_path):
    # A path mentioned in prose might end with a period. The regex should strip
    # it so the file path checks work accurately without looking for a file
    # ending in `.txt.`
    external = tmp_path / "mnt" / "external" / "artifacts" / "alerts" / "2026-08-26-1430-gate4.txt"
    body = f"Error found in {external}."
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    # The external path should be successfully stripped and therefore exempted.
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_gitignored_path_drops_trailing_brackets_and_quotes(tmp_path):
    external = tmp_path / "mnt" / "external" / "artifacts" / "alerts" / "data.csv"

    # In brackets
    body_brackets = f"See attached [{external}]"
    codes_brackets = {code for code, _ in detect_channel_warnings(body_brackets, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes_brackets

    # In JSON string
    body_json = f'{{"path": "{external}", "status": "failed"}}'
    codes_json = {code for code, _ in detect_channel_warnings(body_json, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes_json

def test_relative_paths_bypass_external_exemption(tmp_path):
    # Purely relative paths have no anchor to resolve and thus are never
    # considered external; they should still trigger the gitignored path warning.

    # Missing leading slash with a write claim
    body_missing_slash = "wrote to var/log/artifacts/alerts"
    codes_slash = {code for code, _ in detect_channel_warnings(body_missing_slash, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes_slash

    # Purely relative to current directory with a write claim
    body_relative = "wrote to artifacts/run.json"
    codes_relative = {code for code, _ in detect_channel_warnings(body_relative, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes_relative

def test_a_body_stated_absolute_path_outside_the_checkout_is_not_flagged(tmp_path):
    # A durable artifact root can live entirely outside this repository and
    # still be named in the alert's OWN body text -- not just at its loaded
    # location (see test_a_path_outside_the_checkout_is_not_flagged_as_
    # gitignored above, which covers the loader's path_hint). Only an
    # absolute path can be checked this way.
    external = tmp_path / "mnt" / "external" / "artifacts" / "alerts" / "2026-08-26-1430-gate4.txt"
    body = f"Durable alert written to {external}"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_a_body_stated_absolute_path_inside_the_checkout_is_still_flagged():
    # Regression guard: the exemption is scoped to a path PROVEN outside the
    # checkout, not any absolute path. One that resolves inside this repo
    # (the actual tree .gitignore governs) must still warn.
    repo_root = Path(__file__).resolve().parents[2]
    internal = repo_root / "artifacts" / "alerts" / "2026-08-26-1430-gate4.txt"
    body = f"Durable alert written to {internal}"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_a_bare_relative_gitignored_mention_is_still_flagged():
    # Regression guard: a bare relative mention (no root to resolve against)
    # is unaffected by the new absolute-path check and still warns, per
    # test_a_windows_form_gitignored_path_is_flagged above.
    body = "Durable alert written to artifacts/alerts/2026-08-26-1430-gate4.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_last_roster_slot_time_matches_the_final_roster_entry():
    assert _last_roster_slot_time() == time(15, 30)


def test_calendar_window_end_clamps_an_unfinished_today_back_one_day():
    # The finding's own scenario: a normal morning invocation with no --until
    # or --as-of, run before the 15:30 ET roster completion time, must not
    # enter today into the roster-silence calendar.
    today = date(2026, 8, 27)
    now_et = datetime(2026, 8, 27, 10, 0)
    assert _calendar_window_end(today, until_given=False, as_of_given=False, now_et=now_et) == date(
        2026, 8, 26
    )


def test_calendar_window_end_does_not_clamp_once_the_roster_has_finished():
    # The roster's last slot (1530) is not provably done until its own Gate 1
    # window closes at 16:00 ET, not the bare 15:30 nominal trigger time.
    today = date(2026, 8, 27)
    now_et = datetime(2026, 8, 27, 16, 0)
    assert _calendar_window_end(today, until_given=False, as_of_given=False, now_et=now_et) == today


def test_calendar_window_end_does_not_clamp_an_explicit_until():
    # An explicit --until means the caller asked for that exact day, silence
    # included, and is never second-guessed here.
    today = date(2026, 8, 27)
    now_et = datetime(2026, 8, 27, 10, 0)
    assert _calendar_window_end(today, until_given=True, as_of_given=False, now_et=now_et) == today


def test_calendar_window_end_does_not_clamp_an_explicit_as_of():
    today = date(2026, 8, 27)
    now_et = datetime(2026, 8, 27, 10, 0)
    assert _calendar_window_end(today, until_given=False, as_of_given=True, now_et=now_et) == today


def test_calendar_window_end_does_not_clamp_a_window_end_already_in_the_past():
    # Only today's own unfinished roster is ever clamped -- an implicit
    # window ending on an earlier day (a corpus that has not alerted since)
    # is left untouched.
    past_end = date(2026, 8, 20)
    now_et = datetime(2026, 8, 27, 10, 0)
    assert (
        _calendar_window_end(past_end, until_given=False, as_of_given=False, now_et=now_et) == past_end
    )


def test_build_digest_excludes_a_clamped_calendar_end_from_roster_silence():
    # Verifies the mechanism main() relies on: window_end only shapes
    # build_digest's CALENDAR (and therefore ROSTER-SILENT labelling), never
    # which records are counted. A day excluded from the calendar by
    # clamping must not read as roster-silent, but a record that genuinely
    # fired on it still surfaces via the observed/coverage union.
    clamped_end = date(2026, 8, 25)
    today = date(2026, 8, 26)
    records = [_record("r1", "2026-08-26T14:35:00Z", "1130", "Gate 4")]
    digest = build_digest(
        records,
        window_start=date(2026, 8, 24),
        window_end=clamped_end,
        roster_observed={today},
        roster_coverage={today: {"1130": 1}},
    )
    assert today not in digest.calendar
    assert today not in digest.silent_days
    assert today in digest.days


# --- round 24 ------------------------------------------------------------


def test_a_declared_prior_event_am_pm_clock_does_not_shadow_the_labelled_slot():
    # The AM/PM twin of the round-23 "het" fix: the declared AM/PM branch
    # returned on its first match unconditionally, so a HISTORICAL AM/PM
    # declaration was attributed before the current run's own labelled slot
    # was ever reached.
    text = "Previous run was scheduled 10:30 AM; this run's Slot: 1130"
    assert detect_slot_evidence(text) == ("1130", "labelled")


def test_a_bare_iso_date_in_the_subject_is_recognized_as_declared():
    # The live-mirror Gate 9 contract (dca-executor-prompts/live-mirror/*.txt)
    # only requires the subject to NAME the slot, the ET date, and the
    # failing gate -- not the literal "Date:" label _DECLARED_ET_DATE alone
    # recognized. A compliant subject like "1030 ET | 2026-08-26 | Gate 5"
    # delivered after midnight ET must still resolve to 2026-08-26.
    record = AlertRecord(
        alert_id="r1",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-27T02:15:00Z"),
        subject="1030 ET | 2026-08-26 | Gate 5",
        body="",
        slot="1030",
        gate="Gate 5",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_a_bare_date_in_the_body_alone_is_still_not_declared():
    # Regression guard: the bare-ISO fallback is scoped to the SUBJECT only.
    # The body routinely narrates an unrelated prior date, and a subject
    # carrying no date at all must still fall back to the transport
    # timestamp, not a date the body is merely quoting.
    record = AlertRecord(
        alert_id="r1",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T19:34:00Z"),
        subject="ABORT Gate 4",
        body="aborted on 2026-08-19 for the same reason",
        slot="1530",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_a_standalone_negated_success_claim_is_treated_as_a_failure():
    # Neither _EMAIL_FAILURE nor _SEND_FAILED matches this wording, so
    # without also feeding a negated success clause into `last_failure_end`,
    # states_email_failure() returned early (last_failure_end stays None)
    # before ever reaching the negation screening added last round.
    assert states_email_failure("Retry email was not successfully sent")
    assert states_email_failure("Retry email never delivered successfully")


def test_a_standalone_affirmed_success_is_still_not_a_failure():
    # Regression guard: an unnegated, standalone success claim must not be
    # newly misread as a failure by the same code path.
    assert not states_email_failure("Email sent successfully.")


def test_a_zero_result_slot_filter_does_not_claim_full_roster_silence():
    # Under --slot, `digest.records` can be empty (this slot never alerted)
    # while roster_observed/roster_coverage -- and so digest.silent_days --
    # still reflect real alerts from OTHER slots. The old unconditional
    # "every trading day below is roster-silent" sentence contradicted the
    # nonzero cells the coverage matrix prints two lines below.
    digest = build_digest(
        [],
        window_start=date(2026, 8, 24),
        window_end=date(2026, 8, 24),
        roster_observed={date(2026, 8, 24)},
        roster_coverage={date(2026, 8, 24): {"1430": 1}},
    )
    text = render_text(digest)
    assert "Every one of the" not in text
    assert "No roster-wide outage in range" in text


def test_a_partial_roster_silence_names_the_count_not_every_day():
    digest = build_digest(
        [],
        window_start=date(2026, 8, 24),
        window_end=date(2026, 8, 25),
        roster_observed={date(2026, 8, 24)},
        roster_coverage={date(2026, 8, 24): {"1430": 1}},
    )
    text = render_text(digest)
    assert "1 of the 2 trading days below" in text
    assert "Every one of the" not in text


def test_a_genuine_total_outage_still_names_every_day():
    # Regression guard for the original round-6 behavior: with no
    # roster_observed override at all, a truly empty corpus still reports
    # every trading day as roster-silent.
    digest = build_digest([], window_start=date(2026, 8, 24), window_end=date(2026, 8, 26))
    text = render_text(digest)
    assert "Every one of the 3 trading days below is roster-silent." in text


def test_roster_completion_time_is_1300_on_a_configured_early_close(tmp_path, monkeypatch):
    # The last slot that can alert on an early close is 12:30 (13:30/14:30/
    # 15:30 must end silently), but ITS OWN Gate 1 window still extends 30
    # minutes past that nominal time, same as every other slot.
    (tmp_path / "market_calendar.json").write_text(
        json.dumps({"early_close_1300": ["2026-11-27"]}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert _roster_completion_time(date(2026, 11, 27)) == time(13, 0)


def test_roster_completion_time_is_the_last_slots_window_close_on_an_ordinary_day(tmp_path, monkeypatch):
    (tmp_path / "market_calendar.json").write_text(json.dumps({"early_close_1300": []}), encoding="utf-8")
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert _roster_completion_time(date(2026, 8, 26)) == time(16, 0)


def test_calendar_window_end_uses_the_early_close_cutoff(tmp_path, monkeypatch):
    # The finding's own scenario: on a configured early_close_1300 day, the
    # last slot that can alert is 12:30 (13:30/14:30/15:30 must end silently)
    # -- waiting for the normal 15:30/16:00 cutoff hid an already-finished,
    # genuinely roster-silent early close for hours. Its own Gate 1 window
    # (verified against dca-executor-prompts/*.txt) closes at 13:00, not
    # 12:30 itself.
    (tmp_path / "market_calendar.json").write_text(
        json.dumps({"early_close_1300": ["2026-11-27"]}), encoding="utf-8"
    )
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    today = date(2026, 11, 27)
    after_1300 = datetime(2026, 11, 27, 13, 0)
    assert _calendar_window_end(today, until_given=False, as_of_given=False, now_et=after_1300) == today
    before_1300 = datetime(2026, 11, 27, 12, 30)
    assert _calendar_window_end(
        today, until_given=False, as_of_given=False, now_et=before_1300
    ) == date(2026, 11, 26)


def test_an_observed_incomplete_day_is_classified_separately_from_out_of_window():
    # Fresh evidence after the unfinished-current-day clamp: an alert that
    # already fired today is still unioned into `digest.days` even though
    # today was excluded from `calendar`. Labelling that observed row
    # "outside_approved_trading_window" misreports it as past
    # config/trading_window.json's end_date, when it is simply incomplete.
    today = date(2026, 8, 26)
    digest = build_digest(
        [_record("r1", "2026-08-26T14:35:00Z", "1130", "Gate 4")],
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 25),
        incomplete_days={today},
    )
    assert today in digest.days
    assert today not in digest.calendar
    assert _classify_coverage_day(today, set(digest.silent_days), set(digest.calendar), digest.incomplete_days) == (
        "incomplete_roster_day"
    )
    payload = json.loads(render_json(digest))
    assert payload["day_classification"]["2026-08-26"] == "incomplete_roster_day"
    text = render_text(digest)
    assert "roster not finished yet today" in text


def test_an_incomplete_day_with_no_alert_is_simply_absent():
    # Regression guard: incomplete_days only relabels a day that is ALREADY
    # in digest.days (because something fired). A day with no alert at all
    # stays correctly excluded from the report entirely, not surfaced with
    # the new label.
    today = date(2026, 8, 26)
    digest = build_digest(
        [],
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 25),
        incomplete_days={today},
    )
    assert today not in digest.days


# --- round 25 ------------------------------------------------------------


def test_a_subjects_bare_date_beats_a_labelled_body_date():
    # The declared-date search order used to check a labelled BODY date
    # before the subject's own bare ISO date. A compliant subject like
    # "1030 ET | 2026-08-26 | Gate 5" whose body separately narrates an
    # unrelated PRIOR incident under the same "Date:" label was misdated to
    # that prior date instead of its own.
    record = AlertRecord(
        alert_id="r1",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-27T02:15:00Z"),
        subject="1030 ET | 2026-08-26 | Gate 5",
        body="Previous run Date: 2026-08-25",
        slot="1030",
        gate="Gate 5",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_each_artifacts_path_mention_is_evaluated_independently(tmp_path):
    # A body can first name a supported external path AND separately claim a
    # bare repo-relative copy for the SAME alert -- the second is exactly
    # the gitignored evidence this check exists to catch, and the first
    # match's external-path exemption must not cover the whole body.
    external = tmp_path / "mnt" / "durable" / "artifacts" / "alerts" / "x.txt"
    body = f"Saved to {external}. Also wrote artifacts/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_a_relative_path_hint_with_an_internal_slash_is_not_misread_as_absolute():
    # Regression guard for the _BODY_ARTIFACTS_PATH anchoring fix: a
    # genuinely relative path_hint like "workspace/cron_tracking/.../
    # artifacts/alerts/x.txt" (no leading "/" at all) contains an internal
    # "/" before "artifacts/alerts" (after "workspace") -- an unanchored
    # regex mistook the substring starting there for a real absolute path
    # rooted outside the checkout and wrongly exempted it.
    warnings = detect_channel_warnings(
        "nothing placed",
        source="alert_file",
        path_hint="workspace/cron_tracking/morning_dca/artifacts/alerts/2026-08-26-1030-gate5.txt",
    )
    assert any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_body_artifacts_path_anchoring_regression(caplog):
    # Regression guard for the _BODY_ARTIFACTS_PATH anchoring fix: a
    # malformed path missing the root anchor should be caught and rejected,
    # rather than silently misread as a relative path.
    import logging

    from scripts.alert_digest import _matched_path_is_external

    # Capture logs at the WARNING level or higher
    caplog.set_level(logging.WARNING)

    # Arrange: A malformed path missing a root anchor (/, C:\)
    malformed_path = "var/log/artifacts/alerts/x.txt"

    # Act: Bypass the regex gatekeeper and test the boundary function directly
    result = _matched_path_is_external(malformed_path)

    # Assert: The function must explicitly reject the input
    assert result is False
    # Assert: The specific rejection reason must be logged
    assert "Artifacts path anchored" in caplog.text


def test_then_is_recognized_as_a_retry_outcome_boundary():
    # "Email failed to send initially then retry email sent successfully"
    # narrates two sequential outcomes with no comma or coordinating
    # conjunction at all -- without "then" as a clause boundary, the failure
    # clause spanned the whole sentence and the success scan's
    # `start >= last_failure_end` check could never be satisfied.
    body = "Email failed to send initially then retry email sent successfully"
    assert not states_email_failure(body)


def test_reconcile_requires_the_message_to_follow_its_candidate_draft():
    # An EARLIER, successful message must not be treated as the later retry
    # that resolves a LATER, genuinely-still-failed draft for the same (day,
    # slot, gate) -- only a draft strictly earlier than the message can
    # plausibly be the attempt it retried.
    early_message = _message_record("m1", "2026-08-26T18:00:00Z", "1030", "Gate 5")
    late_draft = _record("d1", "2026-08-26T19:00:00Z", "1030", "Gate 5", warnings=[_DRAFT_WARNING])
    merged = _reconcile_drafts_and_messages([late_draft], [early_message])
    assert len(merged) == 2
    draft_copy = next(r for r in merged if r.alert_id == "d1")
    assert _DRAFT_WARNING in draft_copy.warnings


# --- round 26 ------------------------------------------------------------


def test_a_historical_task_name_does_not_shadow_the_labelled_slot():
    # The task-name branch -- the STRONGEST slot signal, tried first -- was
    # the only one of the four declared-slot branches with no prior-event
    # screening at all: a historical task-name mention was attributed before
    # the current run's own labelled slot was ever reached.
    text = "Previous run 1030-dca-recurring-buy failed; this run's Slot: 1130"
    assert detect_slot_evidence(text) == ("1130", "labelled")


def test_a_windows_drive_path_is_recognized_as_external(monkeypatch):
    from pathlib import Path, PureWindowsPath
    from unittest.mock import patch
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path("/app/dca"))
    with patch('scripts.alert_digest.Path', PureWindowsPath):
        assert _matched_path_is_external(r"C:\logs\artifacts\alerts\x.txt")



def test_connector_error_fallback_wording_is_recognized_as_a_failure():
    # Gate 9's fallback text explicitly permits auth, throttling, provider,
    # and network errors as the reason a send failed -- none of which
    # necessarily uses the words "send" and "failed" together.
    assert states_email_failure("Email authentication failed; draft created")
    assert states_email_failure("Email provider timed out; draft created")
    assert states_email_failure("Email send returned 401 Unauthorized; draft created")


def test_another_channels_connector_error_is_not_an_email_failure():
    # Regression guard: the new connector-error phrases are still scoped to
    # a clause that names email, same as every other _SEND_FAILED phrase.
    assert not states_email_failure("Slack authentication failed; email sent successfully.")


def test_incomplete_roster_days_excludes_a_non_scheduled_day():
    # _calendar_window_end() clamps purely on time of day, with no notion of
    # weekends -- an off-schedule alert observed on a Saturday must still
    # classify as not_a_trading_day, never incomplete_roster_day.
    saturday = date(2026, 8, 29)
    friday = date(2026, 8, 28)
    assert _incomplete_roster_days(saturday, friday) == set()


def test_incomplete_roster_days_includes_a_genuinely_scheduled_day():
    today = date(2026, 8, 27)
    yesterday = date(2026, 8, 26)
    assert _incomplete_roster_days(today, yesterday) == {today}


def test_an_off_schedule_weekend_alert_is_not_classified_as_incomplete():
    # End-to-end guard: even if incomplete_days were (incorrectly) given a
    # non-roster day, _classify_coverage_day still has no way to catch that
    # itself -- the real fix has to be in what main() feeds it, which
    # _incomplete_roster_days above now enforces. This confirms the correct,
    # empty result flows through to classification untouched.
    saturday = date(2026, 8, 29)
    records = [_record("r1", "2026-08-29T14:35:00Z", "1130", "Gate 4")]
    digest = build_digest(
        records,
        window_start=date(2026, 8, 24),
        window_end=date(2026, 8, 24),
        incomplete_days=_incomplete_roster_days(saturday, date(2026, 8, 28)),
    )
    assert saturday not in digest.calendar
    classification = _classify_coverage_day(
        saturday, set(digest.silent_days), set(digest.calendar), digest.incomplete_days
    )
    assert classification == "not_a_trading_day"


# --- round 27 ------------------------------------------------------------


def test_success_stated_before_after_a_connector_error_is_not_a_failure():
    # "Email sent successfully after provider timed out on the first
    # attempt" states the CURRENT outcome (success) before "after" and the
    # now-superseded earlier problem after it -- the opposite text order
    # from every other construct this scan handles. Splitting on "after"
    # moves the connector-error phrase into its own clause, which no longer
    # names email, so it stops counting as a NEW failure.
    body = "Email sent successfully after provider timed out on the first attempt"
    assert not states_email_failure(body)


def test_a_real_failure_using_the_word_after_is_still_a_failure():
    # Regression guard: the pre-existing "Email send failed after three
    # attempts" case has its failure evidence BEFORE "after", not after it,
    # so splitting on "after" must not affect it.
    assert states_email_failure("Email send failed after three attempts")


def test_every_occurrence_of_a_task_name_is_tried_not_just_the_first():
    # Fresh evidence after historical task-name screening was added: the
    # loop only checked the FIRST occurrence of each name via str.find(). A
    # name that appears twice -- once historical, once for the current run
    # -- had its only checked occurrence screened out, leaving the record
    # with no slot at all instead of falling through to the second.
    text = "Previous run 1030-dca-recurring-buy failed; this run 1030-dca-recurring-buy failed again"
    assert detect_slot_evidence(text) == ("1030", "task_name")


def test_a_same_drive_windows_path_matching_the_checkout_is_not_external(monkeypatch):
    # If this process itself ran on Windows, _REPO_ROOT would be
    # drive-qualified, and a same-drive, same-tree alert path must still be
    # recognized as internal rather than unconditionally external.
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path("C:/workspace/all-stock-dca"))
    assert not _matched_path_is_external("C:\\workspace\\all-stock-dca\\artifacts\\alerts\\x.txt")


def test_a_different_drive_windows_path_is_still_external(monkeypatch):
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path("C:/workspace/all-stock-dca"))
    assert _matched_path_is_external("D:\\durable\\artifacts\\alerts\\x.txt")


def test_a_windows_drive_path_is_still_external_on_a_posix_checkout(monkeypatch):
    from pathlib import Path, PureWindowsPath
    from unittest.mock import patch
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path("/app/dca"))
    with patch('scripts.alert_digest.Path', PureWindowsPath):
        assert _matched_path_is_external(r"C:\durable\artifacts\alerts\x.txt")



def test_a_streak_includes_todays_own_incomplete_but_observed_alert():
    # Fresh evidence after the unfinished-day clamp: a record that already
    # alerted today is still in `ordered`, but the clamped `calendar`
    # excludes today entirely, so _compute_streaks could not see it as
    # adjacent to yesterday's matching alert and a genuine 2-day repeat read
    # as a 1-day one.
    today = date(2026, 8, 26)
    yesterday = date(2026, 8, 25)
    records = [
        _record("r1", "2026-08-25T14:35:00Z", "1130", "Gate 4"),
        _record("r2", "2026-08-26T14:35:00Z", "1130", "Gate 4"),
    ]
    digest = build_digest(
        records,
        window_start=yesterday,
        window_end=yesterday,
        incomplete_days={today},
    )
    assert today not in digest.calendar
    assert any(
        s.slot == "1130" and s.gate == "Gate 4" and s.days == 2 and s.end == today
        for s in digest.streaks
    )


def test_incomplete_day_still_excluded_from_silence_despite_extending_streaks():
    # Regression guard: extending the STREAK calendar must not also make an
    # incomplete day eligible for roster-silence detection -- that's still
    # governed by `calendar` alone, unchanged.
    today = date(2026, 8, 26)
    yesterday = date(2026, 8, 25)
    digest = build_digest(
        [_record("r1", "2026-08-25T14:35:00Z", "1130", "Gate 4")],
        window_start=yesterday,
        window_end=yesterday,
        incomplete_days={today},
    )
    assert today not in digest.silent_days


def test_a_declared_clock_with_no_gate_is_still_an_alert():
    # detect_slot_evidence's declared-clock branches (step 2: "scheduled"/
    # "expected N ET" or "scheduled"/"expected H:MM AM/PM") returned the
    # same "clock" tag as the WEAK, purely incidental positional scan
    # (step 4) -- so a partially parsed alert identifying itself only by a
    # declaration like "Expected 1130 ET", with no gate, was discarded by
    # looks_like_executor_alert() even though "scheduled"/"expected" is
    # itself a declaration, not an incidental mention.
    assert detect_slot_evidence("Expected 1130 ET") == ("1130", "declared_clock")
    (record,) = load_gmail_drafts(
        {"drafts": [_draft("r1", "2026-08-26T14:00:00Z", "Expected 1130 ET", "nothing placed")]}
    )
    assert record.gate is None
    assert looks_like_executor_alert(record)


def test_an_incidental_bare_clock_with_no_gate_is_still_not_an_alert():
    # Regression guard: the truly incidental, purely positional wall-clock
    # scan (step 4 -- no "scheduled"/"expected" prefix) must still return
    # the WEAK "clock" tag and be discarded alone, same as before.
    assert detect_slot_evidence("Lunch at 10:30 AM") == ("1030", "clock")
    (record,) = load_gmail_drafts(
        {"drafts": [_draft("r1", "2026-08-26T14:00:00Z", "Lunch at 10:30 AM", "")]}
    )
    assert record.gate is None
    assert not looks_like_executor_alert(record)


# --- round 28 ------------------------------------------------------------


def test_a_referenced_task_name_does_not_shadow_the_subjects_declared_slot():
    # Gate 9 requires the SUBJECT to name the slot. A subject that already
    # does so ("1530 ET | 2026-08-26 | Gate 4") must not be outranked by a
    # task name the body only REFERENCES ("Conflict with
    # 1030-dca-recurring-buy") -- Gate 4's own alert wording for a legacy
    # collision always introduces the OTHER task's name, never this run's.
    text = "1530 ET | 2026-08-26 | Gate 4\nConflict with 1030-dca-recurring-buy"
    assert detect_slot_evidence(text) == ("1530", "explicit_et")


def test_a_self_identifying_task_name_still_wins_over_a_referenced_one():
    # Regression guard: the pre-existing "330-final-buying-run ABORT\nConflict
    # with 1030-dca-recurring-buy" case still resolves to the SELF-identifying
    # name, not the referenced one -- see
    # test_a_leftmost_task_name_wins_over_table_order above for the same
    # example via detect_slot().
    text = "330-final-buying-run ABORT\nConflict with 1030-dca-recurring-buy"
    assert detect_slot_evidence(text) == ("1530", "task_name")


def test_a_subject_verdict_negation_is_bounded_at_a_semicolon():
    # detect_verdict()'s subject-scoped calls operate on the whole UNSPLIT
    # subject, so "Run ABORTED; this was not rejected" correctly disclaimed
    # REJECTED but then let that same "not" reach backward across the
    # semicolon and disclaim ABORTED too, using the default negation
    # splitter (no ";" boundary) instead of the semicolon-aware one the
    # labelled gate/slot fixes already established.
    assert detect_verdict("Run ABORTED; this was not rejected", "") == "ABORT"


def test_success_after_a_problem_that_also_mentions_email_is_not_a_failure():
    # Fresh evidence after the "after" splitter was added: it only helped
    # when the trailing historical clause happened not to repeat "email".
    # "Email sent successfully after the first email send timed out" repeats
    # the word in its own trailing clause, so the failure scan is now
    # truncated right before "after" instead of relying on that clause-split
    # side effect.
    body = "Email sent successfully after the first email send timed out"
    assert not states_email_failure(body)


def test_a_negated_success_before_after_is_still_a_failure():
    # Regression guard: truncating the failure scan at "after" only applies
    # when the success before it is genuinely unnegated.
    body = "Email was not sent successfully after the first email send timed out"
    assert states_email_failure(body)


def test_reason_extraction_skips_a_shared_historical_reason():
    # Two same-day firings at one slot/gate can each state a genuinely
    # different CURRENT reason with "because" while both also narrate the
    # SAME historical labelled reason ("Previous run Reason: budget
    # exhausted") -- the labelled form is checked first for being the
    # stronger signal, and an unscreened search on it reported that shared
    # history as both records' own reason, corroborating two distinct
    # incidents into one.
    record_a = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-25T18:00:00Z"),
        subject="ABORT Gate 4",
        body="aborted because quotes were stale; previous run's Reason: budget exhausted",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    record_b = AlertRecord(
        alert_id="b",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-25T19:00:00Z"),
        subject="ABORT Gate 4",
        body="aborted because the budget was exhausted; previous run's Reason: budget exhausted",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record_a) == "quotes were stale"
    assert _reason_signature(record_b) == "the budget was exhausted"
    assert not _corroborates(record_a, record_b)


def test_zero_calendar_text_output_still_renders_an_observed_incomplete_row():
    # Under --slot, a default run before today's roster finishes can have an
    # EMPTY calendar (the unfinished-day clamp excludes today, and the
    # resolved window is only today) while digest.days/digest.coverage still
    # carry today's own incomplete-but-observed row from another slot that
    # already alerted. Returning early on `not digest.calendar` alone hid
    # that row from the text report entirely, even though --json included it.
    today = date(2026, 8, 26)
    yesterday = date(2026, 8, 25)
    digest = build_digest(
        [],
        window_start=today,
        window_end=yesterday,
        roster_observed={today},
        roster_coverage={today: {"1430": 1}},
        incomplete_days={today},
    )
    assert digest.calendar == []
    assert today in digest.days
    text = render_text(digest)
    assert "COVERAGE" in text


def test_a_current_labelled_reason_is_still_recognized():
    # Regression guard: a labelled "Reason:" with no historical framing at
    # all is still recognized as the record's own current reason.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-25T18:00:00Z"),
        subject="ABORT Gate 4",
        body="Reason: budget exhausted",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record) == "budget exhausted"


# --- round 29 ------------------------------------------------------------


def test_explicit_et_skips_a_historical_mention_to_reach_the_current_slot():
    # Codex review: "Skip historical explicit-ET slot mentions". Step 3's
    # unconditional re.search() took the FIRST "1430ET"-style match
    # unscreened, so a subject-less alert that narrates a prior slot before
    # the current one filed the whole record under the historical slot.
    text = "Previous run at 1030 ET. This run failed at 1130 ET"
    assert detect_slot_evidence(text) == ("1130", "explicit_et")


def test_explicit_et_still_returns_the_only_occurrence_when_it_is_historical():
    # Regression guard: when EVERY explicit-ET occurrence is screened out as
    # historical, the branch must fall through empty-handed (None), not
    # crash or return a stale, mis-attributed value.
    text = "Previous run at 1030 ET failed"
    assert detect_slot_evidence(text) == (None, "")


def test_explicit_et_still_wins_unscreened_with_no_prior_event_cue():
    # Regression guard: the common case (no historical narration at all)
    # still resolves on the first occurrence exactly as before.
    assert detect_slot_evidence("DCA ALERT 1230 ET aborted") == ("1230", "explicit_et")


def test_a_gitignored_default_cron_tracking_root_is_flagged():
    # Codex review: "Flag the default gitignored cron-tracking root".
    # dca_paths.stream_root(...) -- the REQUIRED call -- resolves inside
    # this checkout's own gitignored cron_tracking/ directory when
    # $DCA_CRON_TRACKING_ROOT is unset, not just under a bare
    # "artifacts/alerts" path. A file loaded from exactly that shape must
    # still be flagged, even though its path contains neither "artifacts"
    # nor $DCA_CRON_TRACKING_ROOT's bundled production prefix.
    #
    # Built from _REPO_ROOT rather than a hard-coded "/home/user/..."
    # literal (AGENTS.md: never a hard-coded absolute path) -- CI checks
    # this repo out to a different absolute path than a local sandbox, and
    # the whole point under test is whether the path sits INSIDE whatever
    # this checkout's actual root is.
    default_root_alert = _REPO_ROOT / "cron_tracking" / "morning_dca" / "alerts" / "2026-08-27-1030-gate6.txt"
    warnings = detect_channel_warnings(
        "nothing placed",
        source="alert_file",
        path_hint=str(default_root_alert),
    )
    assert any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_a_body_stated_default_cron_tracking_path_is_flagged():
    # Same gap, reached through the BODY text rather than the loader's own
    # path_hint: an executor that states its own (unset-env-var) write
    # target in the alert must be flagged identically.
    default_root_alert = _REPO_ROOT / "cron_tracking" / "morning_dca" / "alerts" / "x.txt"
    body = f"REPO ALERT FILE written to {default_root_alert}"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_the_bundled_production_cron_tracking_root_is_not_flagged():
    # Regression guard: the actually-configured production root
    # (BUNDLED_CRON_TRACKING_ROOT, exported via $DCA_CRON_TRACKING_ROOT)
    # shares the same "cron_tracking/<stream>/alerts" shape but sits
    # entirely OUTSIDE this checkout -- this repo's .gitignore has no
    # effect there, so stating it must not warn.
    body = "REPO ALERT FILE written to /home/user/workspace/cron_tracking/morning_dca/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_a_sibling_directory_merely_prefixed_with_cron_tracking_is_not_flagged():
    # Regression guard: nested-descendant matching must still anchor at a
    # real path boundary right after "cron_tracking" -- a sibling directory
    # that merely shares the prefix in its own name ("cron_tracking_backup")
    # is not actually under the gitignored tree at all.
    warnings = detect_channel_warnings(
        "nothing to report",
        source="alert_file",
        path_hint="workspace/cron_tracking_backup/morning_dca/alerts/2026-08-26-1030-gate5.txt",
    )
    assert not any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_reason_screening_is_bounded_at_a_sentence_break_not_just_a_semicolon():
    # Codex review: "Bound historical-reason screening at sentence breaks".
    # _mention_has_prior_event() received the whole unsplit subject/body
    # haystack, and _PRIOR_EVENT_CLAUSE_SPLIT does not split on periods or
    # newlines -- only round 28's fix added the ";" boundary. With
    # "Previous run Reason: budget exhausted. This run aborted because
    # quotes were stale" (a PERIOD, not a semicolon, between the historical
    # label and the current reason), the prior-run cue reached across the
    # period and disqualified the current "because" reason too, leaving
    # both records with signature None -- and _corroborates() merged two
    # genuinely different incidents.
    record_a = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-25T18:00:00Z"),
        subject="ABORT Gate 4",
        body="Previous run Reason: budget exhausted. This run aborted because quotes were stale",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    record_b = AlertRecord(
        alert_id="b",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-25T19:00:00Z"),
        subject="ABORT Gate 4",
        body="Previous run Reason: budget exhausted. This run aborted because the market was closed",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record_a) == "quotes were stale"
    assert _reason_signature(record_b) == "the market was closed"
    assert not _corroborates(record_a, record_b)


# --- round 30 ------------------------------------------------------------


def test_a_plain_delivered_retry_outcome_overrides_an_earlier_failure():
    # Codex review: "Recognize plain retry delivery outcomes". _EMAIL_SUCCESS
    # only recognized outcomes carrying the adverb "successfully"; a retry
    # reported with an ordinary passive ("was delivered") left the earlier
    # failure unoverridden, so a run whose email ultimately went through
    # still raised EMAIL_DRAFT_NOT_DELIVERED.
    body = "Email failed to send initially, but retry email was delivered"
    assert not states_email_failure(body)


def test_a_plain_succeeded_retry_outcome_overrides_an_earlier_failure():
    # Same finding, the other worked example: "succeeded" with neither
    # "sent"/"delivered" nor the adverb "successfully" at all.
    body = "Email failed to send initially, but retry email succeeded"
    assert not states_email_failure(body)


def test_a_negated_plain_delivery_outcome_is_still_a_failure():
    # Regression guard: broadening _EMAIL_SUCCESS to bare "was delivered"/
    # "succeeded" must not defeat the existing before-reach negation check
    # every caller of the pattern already applies.
    assert states_email_failure("Retry email was not delivered")
    assert states_email_failure("Email could not be sent. Retry email never succeeded.")


def test_a_denied_channel_does_not_suppress_an_unrelated_affirmed_claim():
    # Codex review: "Scope negation to each channel claim". The sentence-wide
    # _NEGATION_CUES.search(sentence) let a negation cue anywhere in the
    # sentence suppress EVERY match in it, even a genuinely affirmed claim
    # about a completely different channel in another clause: "Slack was not
    # used, but the durable alert was written to artifacts/alerts/x.txt"
    # denies Slack and affirms the gitignored path in the same breath, and
    # the Slack negation was silently clearing the GITIGNORED_ALERT_PATH
    # evidence too.
    body = "Slack was not used, but the durable alert was written to artifacts/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes
    assert "SLACK_DISABLED" not in codes


def test_a_same_clause_negation_still_suppresses_both_of_its_own_matches():
    # Regression guard: clause-scoping negation must not stop suppressing
    # the case this check exists for -- a channel named and denied in the
    # SAME clause, even when the clause is reached twice (once per match).
    payload = {
        "drafts": [
            _draft(
                "r1",
                "2026-08-26T18:36:54Z",
                "ABORT Gate 4",
                "Slack is disabled in config, so no Slack alert was posted.",
            )
        ]
    }
    (record,) = load_gmail_drafts(payload)
    assert "SLACK_DISABLED" not in {code for code, _ in record.warnings}


# --- round 31 ------------------------------------------------------------


def test_a_plain_success_naming_the_fallback_action_is_not_an_override():
    # Codex review: "Restrict plain success matches to actual email
    # delivery". Round 30's bare "succeeded"/"was delivered" alternatives
    # matched ANY success sharing a clause with the word "email", including
    # the FALLBACK action's own success: "saving the email draft succeeded"
    # is not delivery, but satisfied _EMAIL_MENTION via "email draft" and
    # _EMAIL_SUCCESS via the bare "succeeded", clearing a real
    # EMAIL_DRAFT_NOT_DELIVERED.
    body = "Email send failed, but saving the email draft succeeded"
    assert states_email_failure(body)


def test_the_round_30_plain_outcomes_still_override_a_failure():
    # Regression guard: restricting the plain forms to an email/send-shaped
    # subject must not defeat round 30's own fix.
    assert not states_email_failure("Email failed to send initially, but retry email was delivered")
    assert not states_email_failure("Email failed to send initially, but retry email succeeded")


def test_negation_across_and_still_scopes_to_the_matched_claim():
    # Codex review: "Bound channel negation across additive conjunctions".
    # The negation clause splitter deliberately did not split on "and", so
    # "Slack was not used and the durable alert was written to
    # artifacts/alerts/x.txt" still placed both claims in one clause and
    # the Slack negation cleared the unrelated GITIGNORED_ALERT_PATH claim.
    body = "Slack was not used and the durable alert was written to artifacts/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes
    assert "SLACK_DISABLED" not in codes


def test_the_and_joined_exempt_cause_and_effect_claim_is_still_exempt():
    # Regression guard: the `exempt` check (a DIFFERENT clause-scoping pass,
    # still on _PRIOR_EVENT_CLAUSE_SPLIT, which deliberately does NOT split
    # on "and") must be unaffected by switching the NEGATION check's own
    # splitter to _CLAUSE_SPLIT.
    body = "Ran drift_detection.py --slack and it posted to Slack."
    assert "SLACK_DISABLED" not in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_historical_fallback_wall_clock_mention_does_not_win():
    # Codex review: "Screen historical fallback wall-clock mentions". Step
    # 4's bare positional AM/PM scan (the fallback below the declared-clock
    # branches) still returned its first match unconditionally. A
    # partially-parsed alert (strong Gate evidence, no declared slot) that
    # narrates a prior firing's clock before its own filed under the
    # historical slot instead of the current one.
    text = "Gate 2\nPrevious run fired at 10:30 AM. This run failed at 11:30 AM"
    assert detect_slot_evidence(text) == ("1130", "clock")


def test_an_off_schedule_clock_then_the_real_slot_still_resolves():
    # Regression guard: the existing "wrong meridiem, then the real slot"
    # case (no historical framing at all) still keeps scanning past the
    # first, unmapped clock match to reach the real one.
    text = "ABORTED - off-schedule firing at 9:07 PM ET - this is the 11:30 AM ET slot"
    assert detect_slot_evidence(text) == ("1130", "clock")


def test_a_backtick_wrapped_external_path_is_not_flagged():
    # Codex review: "Accept punctuation before external absolute paths".
    # The anchor required whitespace or string-start immediately before the
    # leading "/", so a Markdown- or prose-quoted external path had no
    # exemption path to match against at all.
    body = "Saved to `/mnt/durable/artifacts/alerts/x.txt`"
    assert "GITIGNORED_ALERT_PATH" not in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_parenthesized_external_path_is_not_flagged():
    body = "Saved to (/mnt/durable/artifacts/alerts/x.txt)"
    assert "GITIGNORED_ALERT_PATH" not in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_a_bare_relative_mention_is_still_flagged_after_the_punctuation_fix():
    # Regression guard: the punctuation anchor must not make a genuinely
    # gitignored bare-relative mention exempt.
    body = "Durable alert written to artifacts/alerts/x.txt"
    assert "GITIGNORED_ALERT_PATH" in {c for c, _ in detect_channel_warnings(body, source="alert_file")}


def test_authentication_error_wording_is_recognized_as_a_send_failure():
    # Codex review: "Recognize authentication-error fallback wording". Only
    # the exact phrase "authentication failed" was recognized, though Gate 9
    # explicitly permits an auth error as a fallback reason too.
    assert states_email_failure("Email authentication error; draft created")
    assert states_email_failure("Email auth error; draft created")


def test_authentication_failed_wording_is_still_recognized():
    # Regression guard for the pre-existing phrasing.
    assert states_email_failure("Email authentication failed; draft created")


def test_retries_pair_with_their_nearest_preceding_draft_regardless_of_export_order():
    # Codex review: "Pair retries independently of export ordering". Two
    # retry pairs in one day -- drafts at t1/t3, their successful retries at
    # t2/t4 -- processed with the sent-message array newest-first (Gmail's
    # own list APIs commonly return results that way) had the naive
    # first-match-in-input-order pairing consume the OLDEST compatible
    # draft (t1) for the LATEST message (t4), stranding t2 and t3 with a
    # stale EMAIL_DRAFT_NOT_DELIVERED despite t4 being t3's own actual,
    # later, successful retry.
    d1 = _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    d3 = _record("d3", "2026-08-25T16:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    m4 = _message_record("m4", "2026-08-25T17:00:00Z", "1030", "Gate 4")
    m2 = _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4")
    # Newest-first, matching the reported export ordering.
    merged = _reconcile_drafts_and_messages([d1, d3], [m4, m2])
    assert len(merged) == 2
    for record in merged:
        assert _DRAFT_WARNING not in record.warnings


def test_a_single_retry_pair_still_reconciles_in_export_order():
    # Regression guard: the ordinary, already-tested single-pair case is
    # unaffected by switching from FIFO-first to nearest-preceding matching.
    d1 = _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    m2 = _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4")
    merged = _reconcile_drafts_and_messages([d1], [m2])
    assert len(merged) == 1
    assert _DRAFT_WARNING not in merged[0].warnings


def test_later_independent_message_does_not_erase_failed_send():
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale",
    )
    merged = _reconcile_drafts_and_messages([draft], [message])
    assert len(merged) == 2
    assert _DRAFT_WARNING in merged[0].warnings


@pytest.mark.parametrize(
    "wording",
    [
        "Reason: quotes were stale. No retry was attempted.",
        "Reason: quotes were stale. The email retry was not sent.",
        "Reason: quotes were stale. Never resend the email.",
        "Reason: quotes were stale. The order retry succeeded.",
    ],
)
def test_negated_or_non_email_retry_wording_does_not_erase_failed_send(wording):
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body=wording,
    )
    merged = _reconcile_drafts_and_messages([draft], [message])
    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


def test_terms_from_different_sentences_and_channels_do_not_prove_email_retry():
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. Message center notification sent. The order retry succeeded.",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


@pytest.mark.parametrize(
    "wording",
    [
        "Reason: quotes were stale. The previous run's email retry succeeded.",
        "Reason: quotes were stale. An earlier run's email resend was delivered.",
    ],
)
def test_historical_retry_success_does_not_erase_current_failed_send(wording):
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body=wording,
    )
    merged = _reconcile_drafts_and_messages([draft], [message])
    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


@pytest.mark.parametrize(
    "wording",
    [
        "Reason: quotes were stale. Email retry succeeded but the message was not delivered.",
        "Reason: quotes were stale. Email retry was successful, but email delivery failed.",
    ],
)
def test_contradicted_retry_success_does_not_erase_failed_send(wording):
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body=wording,
    )
    merged = _reconcile_drafts_and_messages([draft], [message])
    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


def test_shared_gmail_thread_proves_successful_retry():
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        thread_id="thread-1",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: executor gate failure",
        thread_id="thread-1",
    )
    merged = _reconcile_drafts_and_messages([draft], [message])
    assert len(merged) == 1
    assert _DRAFT_WARNING not in merged[0].warnings


def test_exact_gmail_thread_wins_over_nearer_prose_candidate():
    threaded_draft = replace(
        _record("threaded", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
        thread_id="thread-1",
    )
    unthreaded_draft = replace(
        _record("unthreaded", "2026-08-25T14:30:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("sent", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. Email retry succeeded.",
        thread_id="thread-1",
    )

    merged = _reconcile_drafts_and_messages([threaded_draft, unthreaded_draft], [message])

    assert len(merged) == 2
    assert merged[0].alert_id == "sent"
    assert _DRAFT_WARNING not in merged[0].warnings
    assert merged[1].alert_id == "unthreaded"
    assert _DRAFT_WARNING in merged[1].warnings


def test_mismatched_gmail_threads_cannot_pair_via_retry_prose():
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
        thread_id="thread-b",
    )
    message = replace(
        _message_record("m2", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. Email retry succeeded.",
        thread_id="thread-a",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


def test_exact_thread_match_bypasses_unparseable_reason_corroboration():
    # Codex review discussion_r3887554096 ("Let shared thread identity
    # bypass reason parsing"): both bodies use a free-form reason code the
    # parser cannot read (_reason_signature returns None for both), so
    # _corroborates alone would reject the pair -- but the exact, nonempty
    # Gmail thread match already proves transport identity and must not be
    # second-guessed by content matching.
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="GATE5_DISABLED_XYZ_CODE_9",
        thread_id="thread-1",
    )
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Email retry succeeded.",
        thread_id="thread-1",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


def test_exact_thread_retry_pairs_at_an_equal_timestamp():
    # Codex review discussion_r3891196433 ("Allow exact-thread retries at
    # equal timestamps"): an export can assign a failed draft and its
    # immediately successful retry the identical timestamp. The strict `>=`
    # ordering check must not reject the pair when an exact, nonempty
    # thread match already proves which record is the retry.
    same_time = "2026-08-25T14:00:00Z"
    draft = replace(
        _record("d1", same_time, "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
        thread_id="thread-1",
    )
    message = replace(
        _message_record("m1", same_time, "1030", "Gate 4"),
        body="Reason: quotes were stale. Email retry succeeded.",
        thread_id="thread-1",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


def test_hyphenated_re_sent_wording_is_recognized_as_retry_evidence():
    # Codex review discussion_r3888105725 ("Recognize hyphenated re-send
    # wording"): a hyphen is not a word character, so \bresend\b never
    # matched "re-sent"/"re-send".
    assert _RETRY_EVIDENCE.search("Email was re-sent successfully")
    assert _RETRY_EVIDENCE.search("Email will be re-send shortly") is not None


def test_message_center_failure_does_not_contradict_an_email_retry():
    # Codex review discussion_r3888105723 ("Scope follow-up delivery
    # failures to email"): a DIFFERENT channel's failure (the in-app
    # "message center") must not contradict a genuinely successful email
    # retry just because the word "message" also appears in
    # _DELIVERY_FAILURE_EVIDENCE's channel alternation.
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. Email retry succeeded. Message center notification was not delivered.",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


def test_comma_separated_retry_status_still_proves_the_retry():
    # Codex review discussion_r3891196430 ("Keep comma-separated retry
    # status together"): "Email retry, sent successfully" must not be split
    # on the bare comma into two clauses that individually fail the
    # all-three-terms check.
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
    )
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. Email retry, sent successfully.",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


def test_conditional_retry_success_mention_does_not_prove_delivery():
    # Codex review discussion_r3896981866 ("Reject conditional retry-success
    # mentions"): "If the email retry succeeded, no draft will remain"
    # names all three terms in one clause but asserts nothing -- it is
    # hypothetical, not delivery proof.
    draft = _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Reason: quotes were stale. If the email retry succeeded, no draft will remain.",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 2
    assert any(_DRAFT_WARNING in record.warnings for record in merged)


def test_exact_thread_first_attempt_qualified_failure_still_pairs():
    # Codex review discussion_r3895361799 ("Honor first-attempt qualifiers
    # before rejecting thread matches"): a SENT message's own existence is
    # direct proof of eventual delivery; a body that only narrates the
    # FIRST attempt's failure ("Email failed on the first attempt") must
    # not, by itself, block an exact-thread pairing.
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        thread_id="thread-1",
    )
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body="Email failed on the first attempt; draft created.",
        thread_id="thread-1",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


def test_exact_thread_historical_failure_mention_does_not_block_pairing():
    # Codex review discussion_r3887928796 ("Ignore historical delivery
    # failures in retry proof"): a follow-up failure mention narrating an
    # OLDER, unrelated incident ("Email retry succeeded. The previous run's
    # email was not delivered") is not evidence THIS retry ultimately
    # failed. The raw _DELIVERY_FAILURE_EVIDENCE follow-up search must
    # apply the same prior-event screening states_email_failure() already
    # uses, not reject unconditionally on any later failure phrase.
    draft = replace(
        _record("d1", "2026-08-25T14:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING]),
        body="Reason: quotes were stale",
        thread_id="thread-1",
    )
    message = replace(
        _message_record("m1", "2026-08-25T15:00:00Z", "1030", "Gate 4"),
        body=(
            "Reason: quotes were stale. Email retry succeeded. "
            "The previous run's email was not delivered."
        ),
        thread_id="thread-1",
    )

    merged = _reconcile_drafts_and_messages([draft], [message])

    assert len(merged) == 1
    assert merged[0].alert_id == "m1"
    assert _DRAFT_WARNING not in merged[0].warnings


# --- round 32 ------------------------------------------------------------


def test_a_plain_sent_retry_outcome_overrides_an_earlier_failure():
    # Codex review: "Recognize plain sent outcomes for successful retries".
    # Rounds 30-31 added "was/were delivered" and "succeeded" as adverb-free
    # _EMAIL_SUCCESS forms, but not the plainest verb of all: "sent". "Email
    # failed to send initially, but retry email was sent" matched none of
    # them, so the earlier failure was never overridden.
    body = "Email failed to send initially, but retry email was sent"
    assert not states_email_failure(body)


def test_a_negated_plain_sent_outcome_is_still_a_failure():
    # Regression guard: covered by the pre-existing _EMAIL_FAILURE pattern
    # ("email was not sent"), unaffected by adding the plain "was sent" form.
    assert states_email_failure("The retry email was not sent")


def test_a_nested_default_cron_tracking_path_is_flagged():
    # Codex review: "Cover nested paths under the default cron root". The
    # canonical 1530ET live-mirror prompt's own REQUIRED durable-alert call
    # is stream_root('ce16ba6b', 'scheduled_runs', 'alerts', ...) -- TWO
    # path segments between "cron_tracking" and "alerts" -- but the
    # previous pattern only recognized exactly one. With
    # $DCA_CRON_TRACKING_ROOT unset, that real call resolves inside this
    # checkout's own gitignored cron_tracking/ tree just as surely as the
    # single-segment default does.
    #
    # Built from _REPO_ROOT rather than a hard-coded "/home/user/..."
    # literal (AGENTS.md: never a hard-coded absolute path) -- CI checks
    # this repo out to a different absolute path than a local sandbox; see
    # test_a_gitignored_default_cron_tracking_root_is_flagged above for the
    # same fix after the same CI failure on an earlier round.
    nested_alert = _REPO_ROOT / "cron_tracking" / "ce16ba6b" / "scheduled_runs" / "alerts" / "2026-08-27-1530-gate9.txt"
    warnings = detect_channel_warnings("nothing placed", source="alert_file", path_hint=str(nested_alert))
    assert any(code == "GITIGNORED_ALERT_PATH" for code, _ in warnings)


def test_the_production_nested_cron_tracking_root_is_still_not_flagged():
    # Regression guard: the real, configured production root
    # (/home/user/workspace/cron_tracking/...) shares the same nested
    # shape but sits entirely outside this checkout and must stay exempt.
    body = "REPO ALERT FILE written to /home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_a_historical_body_date_is_skipped_in_favor_of_the_transport_timestamp():
    # Codex review: "Screen historical body dates before using them". A
    # partially-parsed Gmail alert with no subject date at all still fell
    # back to an unscreened body "Date:" label, even when that label's ONLY
    # occurrence narrated a PRIOR incident -- "Previous run Date:
    # 2026-08-25" transported on 2026-08-27 misdated the CURRENT alert to
    # 2026-08-25, corrupting coverage and blocking reconciliation with its
    # own same-day alert file.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-27T18:00:00Z"),
        subject="ABORT Gate 4",
        body="Previous run Date: 2026-08-25",
        slot=None,
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 27)


def test_a_current_labelled_body_date_is_still_used_with_no_subject_date():
    # Regression guard: a labelled body "Date:" with no historical framing
    # at all is still recognized as the record's own current date.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-27T18:00:00Z"),
        subject="ABORT Gate 4",
        body="Date: 2026-08-26",
        slot=None,
        gate="Gate 4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_a_historical_stream_word_does_not_win_a_slot():
    # Codex review: "Screen historical stream words before assigning a
    # slot". Step 5's stream-word scan ("if word in lowered: return...")
    # had no prior-event screening at all -- the last branch in
    # detect_slot_evidence still missing it. A partially-parsed alert with
    # strong Gate evidence but no OTHER slot signal, whose only stream-word
    # occurrence narrates a PRIOR firing ("Previous midday run failed; this
    # run cannot reach the repo"), was wrongly attributed to that
    # historical slot.
    #
    # Reproducing this required widening _PRIOR_EVENT itself, not just
    # adding the screening call: "Previous midday run" has a stream word
    # between "previous" and "run", and the existing "previous (?:run|
    # firing|attempt)" alternative required literal adjacency, so it did
    # not recognize this phrasing as historical at all.
    text = "Failing gate: Gate 2. Previous midday run failed; this run cannot reach the repo"
    assert detect_slot_evidence(text) == (None, "")


def test_a_current_stream_word_still_wins_with_no_historical_framing():
    # Regression guard: the ordinary, already-tested bare stream-word case
    # is unaffected by adding prior-event screening to step 5.
    assert detect_slot_evidence("Midday lunch plan") == ("1230", "stream_word")


# --- round 33 ------------------------------------------------------------


def test_a_two_token_historical_clock_qualifier_does_not_win():
    # Codex review: "Recognize multi-token historical run qualifiers".
    # Round 32's widened _PRIOR_EVENT alternative tolerated only ONE
    # intervening token between "previous" and "run", so "Previous 10:30 AM
    # run failed" (two tokens: "10:30" and "AM") was still not recognized
    # as historical -- the bare positional clock scan (step 4) accepted the
    # unscreened first match and assigned the alert to 1030 instead of the
    # current run's own 1130.
    text = "Previous 10:30 AM run failed. This run failed at 11:30 AM"
    assert detect_slot_evidence(text) == ("1130", "clock")


def test_a_zero_token_historical_mention_still_screens_out():
    # Regression guard: the original, literal-adjacency case ("previous run
    # failed") is unaffected by widening the token count to a bounded range.
    assert detect_slot_evidence("Previous run failed at 10:30 AM. This run failed at 11:30 AM") == (
        "1130",
        "clock",
    )


def test_a_one_token_historical_stream_word_still_screens_out():
    # Regression guard: round 32's own one-token case (a stream word, not a
    # clock) still works under the bounded {0,2} token range.
    text = "Failing gate: Gate 2. Previous midday run failed; this run cannot reach the repo"
    assert detect_slot_evidence(text) == (None, "")


# --- round 34 ------------------------------------------------------------


def test_a_successful_resend_with_no_email_mention_still_overrides():
    # Codex review: "Let a successful resend clear the earlier failure".
    # _EMAIL_SUCCESS's subject-scoped alternatives (rounds 31-32) are
    # already fully delivery-scoped by their own immediately-preceding
    # subject word ("resend", "retry", "send"), but every caller ALSO
    # required a separate, literal "email" mention elsewhere in the same
    # clause -- redundant when the subject already IS "email", but
    # actively wrong when it's "resend"/"retry"/"send" instead: "Email
    # failed to send initially, but resend succeeded" has no second
    # "email" anywhere in its success clause.
    body = "Email failed to send initially, but resend succeeded"
    assert not states_email_failure(body)


def test_a_fallback_action_success_naming_only_resend_is_still_not_delivery():
    # Regression guard: round 31's own finding (a bare "succeeded" naming
    # the FALLBACK action, not delivery) is unaffected by waiving the
    # _EMAIL_MENTION requirement for subject-scoped matches -- "resend"
    # itself is not in the fallback-action vocabulary this guards against,
    # but the ORIGINAL round-31 example must still read as a failure.
    body = "Email send failed, but saving the email draft succeeded"
    assert states_email_failure(body)


def test_a_negated_resend_outcome_is_still_a_failure():
    # Regression guard: waiving _EMAIL_MENTION for subject-scoped matches
    # must not defeat the existing before-reach negation screening.
    assert states_email_failure("Email could not be sent. The resend never succeeded.")


def test_a_unc_network_share_artifact_root_is_not_flagged():
    # Codex review: "Exempt external Windows UNC artifact roots". A
    # network share is exactly as legitimately outside this checkout as a
    # POSIX mount point or a Windows drive letter, but neither
    # _BODY_ARTIFACTS_PATH nor _matched_path_is_external recognized the
    # "\\server\share\..." form at all, so the per-mention external-path
    # exemption never applied to it.
    body = r"Saved to \\server\share\cron_tracking\intraday_dca\alerts\x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_a_unc_path_naming_this_checkout_is_still_internal(monkeypatch):
    from pathlib import Path, PureWindowsPath
    from unittest.mock import patch
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path(r"\\server\share\all-stock-dca"))
    with patch('scripts.alert_digest.Path', PureWindowsPath):
        assert not _matched_path_is_external(r"\\server\share\all-stock-dca\artifacts\alerts\x.txt")



def test_a_different_unc_share_is_still_external(monkeypatch):
    from pathlib import Path, PureWindowsPath
    from unittest.mock import patch
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path(r"\\server\share\all-stock-dca"))
    with patch('scripts.alert_digest.Path', PureWindowsPath):
        assert _matched_path_is_external(r"\\otherserver\othershare\artifacts\alerts\x.txt")



def test_a_noun_phrase_no_cap_breach_is_recognized_as_a_denial():
    # Codex review: "Treat `no cap breach` as a negated verdict". "no" is
    # deliberately absent from the broad reach-based negation set (a real
    # GATE5_DISABLED body routinely says "no signal-generation entry point
    # exists", and negating THAT would suppress the verdict it describes),
    # but "No cap breach occurred" -- "no" directly adjacent to the
    # matched "cap breach" -- is a genuine denial with no such conflict.
    assert detect_verdict("Run ABORTED", "No cap breach occurred; quotes were stale") == "ABORT"


def test_a_noun_phrase_no_rejection_is_also_recognized():
    assert detect_verdict("Run ABORTED", "No rejection occurred; quotes were stale") == "ABORT"


def test_the_gate5_disabled_no_entry_point_wording_is_still_recognized():
    # Regression guard: the exact pre-existing case the narrow negation set
    # was built to protect. "no" here sits nowhere near adjacent to the
    # separately-matched "GATE5_DISABLED" token, so it must not disclaim it.
    body = "Failing gate: Gate 5\nGATE5_DISABLED: no signal-generation entry point exists."
    assert detect_gate("DCA ALERT", body) == "GATE5_DISABLED"


# --- round 35 ------------------------------------------------------------


def test_explicit_delivery_failure_wording_is_recognized():
    # Codex review: "Recognize explicit delivery-failure wording". Neither
    # _EMAIL_FAILURE nor _SEND_FAILED matched "Email delivery failed" --
    # the former required "did not happen", the latter required the word
    # "send" rather than "delivery".
    assert states_email_failure("Email delivery failed; draft created")
    assert states_email_failure("Email delivery failure; draft created")


def test_a_because_resolved_cause_does_not_reopen_a_success():
    # Codex review: "Treat resolved causal errors as superseded". The
    # "after"-truncation fix (round 28-ish) only handled "after"; "because"
    # and "once" introduce the exact same resolved-cause shape -- success
    # stated FIRST, a now-superseded problem after it -- and neither is a
    # _CLAUSE_SPLIT boundary, so "network error" stayed in the same
    # undivided, _EMAIL_MENTION-satisfying clause as "Email" and registered
    # as a brand new failure.
    assert not states_email_failure("Email sent successfully because the network error was resolved")
    assert not states_email_failure("Email sent successfully once the network error was resolved")


def test_the_after_truncation_case_still_works():
    # Regression guard: the pre-existing "after" case is unaffected by
    # adding "because"/"once" as further truncation triggers.
    assert not states_email_failure("Email sent successfully after the first email send timed out")


def test_a_negated_success_before_because_is_still_a_failure():
    # Regression guard: the truncation only applies to a genuinely
    # unnegated success, matching the existing "after" case's own guard.
    assert states_email_failure("Email was not sent successfully because the network error was resolved")


def test_a_sent_messages_stale_first_attempt_body_is_not_flagged():
    # Codex review: "Trust sent-message evidence over stale body text". A
    # sent gmail_message record's own existence is direct proof the email
    # was ultimately delivered; its body can still narrate its own earlier,
    # superseded attempt without contradicting that.
    body = "Email send failed on the first attempt; draft created"
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in codes


def test_an_initially_qualified_sent_message_failure_is_also_not_flagged():
    # Unlike the previous test, this body has NO explicit success clause at
    # all -- states_email_failure() alone would still return True here; only
    # the "initially" qualifier plus the gmail_message source together
    # exempt it.
    body = "Email could not be sent initially; draft created"
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" not in codes


def test_a_bare_unqualified_sent_message_failure_claim_is_still_flagged():
    # Regression guard: this is the pre-existing
    # test_sent_message_stating_its_own_failure_is_flagged scenario,
    # reproduced directly against detect_channel_warnings. A sent
    # message's body making a bare, unqualified failure claim -- no
    # "first attempt"/"initially" framing suggesting a later resolution --
    # gives no reason to distrust its own plain, current-tense claim, and
    # must still be flagged exactly as before this round's fix.
    body = "Email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_an_alert_files_first_attempt_failure_is_still_flagged():
    # Regression guard: the _MESSAGE_FIRST_ATTEMPT_QUALIFIER exemption is
    # scoped to source == "gmail_message" only. An alert_file record has no
    # comparable "the file itself is proof of eventual delivery" evidence,
    # so the same qualifying language must not exempt it.
    body = "Email send failed on the first attempt; draft created"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


# --- round 36 ------------------------------------------------------------


def test_the_first_attempt_qualifier_must_share_a_sentence_with_the_failure():
    # Codex review: "Scope the attempt qualifier to the email failure". The
    # qualifier check was body-wide, so "Initially" describing an UNRELATED
    # earlier event (Gate 5 failing) wrongly exempted a genuinely current,
    # unqualified email failure in the next sentence.
    body = "Initially Gate 5 failed. Email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_the_first_attempt_exemptions_still_work_when_co_located():
    # Regression guard: the two pre-existing exemption cases (both round-35)
    # are unaffected by scoping the qualifier check to one sentence.
    for body in (
        "Email send failed on the first attempt; draft created",
        "Email could not be sent initially; draft created",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
        assert "EMAIL_DRAFT_NOT_DELIVERED" not in codes, body


def test_a_bare_claim_is_still_flagged_after_the_qualifier_scoping_fix():
    # Regression guard: the pre-existing bare-claim test.
    body = "Email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_a_non_email_draft_fallback_is_not_an_email_failure():
    # Codex review: "Require draft fallbacks to identify email". The
    # "(?:draft was created|created a draft) instead" alternative had no
    # email-mention requirement at all, breaking _EMAIL_FAILURE's own
    # documented invariant that every one of its patterns already names
    # email -- "Order draft was created instead of submission" has nothing
    # to do with email.
    assert not states_email_failure("Order draft was created instead of submission")


def test_an_email_scoped_draft_fallback_is_still_recognized():
    # Regression guard: moved to _SEND_FAILED, which is CLAUSE+_EMAIL_
    # MENTION-scoped in states_email_failure() -- the legitimate case (the
    # phrase sharing a clause with "email") must still be caught.
    body = "Email connector has no send capability, so a draft was created instead"
    assert states_email_failure(body)


def test_a_negated_delivery_failure_claim_is_ignored():
    # Codex review: "Ignore explicitly negated delivery failures". Neither
    # the _EMAIL_FAILURE nor the _SEND_FAILED matching loop screened for a
    # negation cue at all, so "No email delivery failure occurred" matched
    # unconditionally and registered as a genuine current failure.
    assert not states_email_failure("No email delivery failure occurred")
    assert not states_email_failure("No email send failed here")


def test_an_unnegated_delivery_failure_is_still_recognized():
    # Regression guard: both loops must still recognize a genuine,
    # unnegated failure after adding the negation screen.
    assert states_email_failure("Email delivery failure occurred")
    assert states_email_failure("Email send failed here")


def test_an_and_joined_parallel_slack_claim_is_not_exempted():
    # Codex review: "Keep permitted Slack routes from exempting a separate
    # post". "Posted drift via DCA_DRIFT_SLACK_WEBHOOK and posted the Gate
    # 5 alert to Slack" names TWO separate posts joined by "and"; the
    # exempt check's own "and"-tolerant clause search (needed for a
    # cause-and-effect claim) let the first claim's permitted route wrongly
    # exempt the second, unrelated one too.
    body = "Posted drift via DCA_DRIFT_SLACK_WEBHOOK and posted the Gate 5 alert to Slack"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_the_and_joined_cause_and_effect_exemption_still_works():
    # Regression guard: the pre-existing case this "and"-tolerant exempt
    # search exists for -- a pronoun-led continuation clause -- is
    # unaffected by narrowing the search for a bare, subject-less one.
    body = "Ran drift_detection.py --slack and it posted to Slack."
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_a_with_no_errors_manner_phrase_does_not_negate_the_claim():
    # Codex review: "Bind negation to the channel outcome". The negation
    # check is clause-scoped but otherwise unbounded within that clause, so
    # "Posted the Gate 5 alert to Slack with no errors" had the bare "no"
    # inside a common manner idiom count as negating the claim it merely
    # follows.
    body = "Posted the Gate 5 alert to Slack with no errors"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_a_genuine_same_clause_negation_still_suppresses():
    # Regression guard: the pre-existing same-clause negation case (round
    # 30) is unaffected by stripping only the specific "with no X" idiom.
    body = "Slack is disabled in config, so no Slack alert was posted."
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


# --- round 37 ------------------------------------------------------------


def test_a_negated_success_before_a_subordinate_clause_is_not_mistaken_for_delivery():
    # Codex review: "Bound the success-negation reach to the subordinate
    # clause it negates". The 40-char before-reach used to screen an
    # email-success match for negation was unbounded within that window, so
    # a genuine "no problems" aside in an EARLIER subordinate clause bled
    # across a "when"/"while"/"where" boundary and wrongly negated a LATER,
    # actually-unnegated success mention.
    body = "Email failed to send, but no problems occurred when retry email was sent"
    assert not states_email_failure(body)


def test_an_unbounded_negated_success_is_still_recognized():
    # Regression guard: the pre-existing case the 40-char reach exists for
    # -- a negation directly modifying the success claim in the same clause,
    # with no subordinate-clause boundary between them -- is unaffected by
    # bounding the reach at "when"/"while"/"where".
    assert states_email_failure("Retry email was not successfully sent")


def test_a_success_before_connector_stops_a_later_cross_channel_failure():
    # Codex review: "Also truncate on 'before'". The success-truncation
    # trigger recognized "after"/"because"/"once" but not "before", so a
    # trailing, unrelated problem mention joined by "before" could still
    # re-trigger a new email failure past a genuine, unnegated success.
    body = "Email was sent before DCA_DRIFT_SLACK_WEBHOOK timed out"
    assert not states_email_failure(body)


def test_a_negated_success_before_connector_is_still_a_failure():
    # Regression guard: negation still works across the widened "before"
    # trigger -- this must NOT be truncated into a false success.
    assert states_email_failure("Email was not sent successfully before the connector timed out")


def test_a_later_stream_word_earlier_in_the_lookup_table_does_not_misfire():
    # Codex review: "Order stream-word candidates by text position". Step 5
    # walked the lookup table in TABLE order and returned the first table
    # entry's match, so a body mentioning "midday" (the correct, later slot)
    # after "market-open" (an earlier table entry appearing later in the
    # body) was wrongly attributed to market-open's 0935 slot.
    body = "Failing gate: Gate 2. Midday run failed because market-open data was unavailable"
    assert detect_slot_evidence(body) == ("1230", "stream_word")


def test_a_single_stream_word_mention_is_unaffected_by_the_ordering_fix():
    # Regression guard: a body with only one stream-word mention still
    # resolves to that slot.
    assert detect_slot_evidence("Midday lunch plan") == ("1230", "stream_word")


def test_a_historical_stream_word_mention_is_still_screened_after_the_ordering_fix():
    # Regression guard: round 32's prior-event screening for step 5 is
    # unaffected by switching to position-ordered candidates.
    body = "Failing gate: Gate 2. Previous midday run failed; this run cannot reach the repo"
    assert detect_slot_evidence(body) == (None, "")


def test_an_unpadded_935_slot_declaration_is_normalized():
    # Codex review: "Normalize unpadded 09:35 slot declarations". The
    # labelled/declared-clock alternatives required the 4-digit "0935" form,
    # so the natural unpadded "935" the 09:35 alert's own contract fields
    # use (subject "935 | 2026-08-26 | Gate 5", body "Slot: 935") matched
    # nothing and the record was retained with slot=None. Accept and
    # zero-pad bare "935" here, mirroring _slot_from_filename_token()'s
    # established precedent for the same token.
    body = "935 | 2026-08-26 | Gate 5\nSlot: 935"
    assert detect_slot_evidence(body) == ("0935", "labelled")


def test_an_unpadded_935_declared_clock_is_normalized():
    # Regression guard / extension: the "declared_clock" alternative (the
    # "scheduled 935 ET" phrasing, not just the labelled "Slot:" form) is
    # normalized the same way.
    assert detect_slot_evidence("scheduled 935 ET") == ("0935", "declared_clock")


def test_a_padded_0935_slot_declaration_is_unaffected_by_the_935_alternative():
    # Regression guard: the pre-existing 4-digit form still resolves the
    # same way now that a 3-digit alternative sits beside it in the pattern.
    assert detect_slot_evidence("Slot: 0935") == ("0935", "labelled")
    assert detect_slot_evidence("scheduled 0935 ET") == ("0935", "declared_clock")


def test_other_roster_slots_are_unaffected_by_the_935_alternative():
    # Regression guard: adding "935" as an alternative must not make any
    # other 4-digit roster slot misfire -- none of them is three digits, so
    # there is nothing for the new alternative to collide with.
    assert detect_slot_evidence("Slot: 1030") == ("1030", "labelled")
    assert detect_slot_evidence("scheduled 1330 ET") == ("1330", "declared_clock")


def test_a_reason_stops_before_an_appended_delivery_clause():
    # Codex review: "Stop reason signatures before appended delivery
    # clauses". The reason capture stopped only at a sentence boundary
    # (period/semicolon/newline), so a delivery-outcome clause APPENDED to
    # the reason in the SAME sentence via a comma -- a wording the
    # stated-not-labelled Gate 9 contract permits just as much as a new
    # sentence -- became part of the signature, while a corroborating copy
    # that happened to stop at a plain period did not. The two signatures
    # then compared unequal and one retry was read as two distinct
    # incidents instead of being merged.
    draft = _record("d1", "2026-08-26T18:36:00Z", slot="1030", gate="5")
    draft = replace(draft, body="Aborted because quotes were stale, and email delivery failed")
    alert_file = _file_record("f1", "2026-08-26T18:37:00Z", slot="1030", gate="5")
    alert_file = replace(alert_file, body="Aborted because quotes were stale.")
    assert _reason_signature(draft) == "quotes were stale"
    assert _reason_signature(alert_file) == "quotes were stale"
    assert _corroborates(draft, alert_file)


def test_distinct_reasons_are_still_distinguished_after_the_delivery_clause_fix():
    # Regression guard: the pre-existing case the reason signature exists
    # to catch -- two same-day firings with genuinely different reasons --
    # is unaffected.
    a = _record("a", "2026-08-26T18:36:00Z", slot="1030", gate="5")
    a = replace(a, body="aborted because quotes were stale")
    b = _record("b", "2026-08-26T18:40:00Z", slot="1030", gate="5")
    b = replace(b, body="aborted because the budget was exhausted")
    assert _reason_signature(a) == "quotes were stale"
    assert _reason_signature(b) == "the budget was exhausted"


def test_a_reason_with_its_own_comma_separated_causes_is_preserved():
    # Regression guard: the delivery-clause stop must not fire on a comma
    # that is part of the reason itself (no channel word follows it) --
    # only truncate when a channel/delivery word actually follows the
    # comma.
    record = _record("c", "2026-08-26T18:36:00Z", slot="1030", gate="5")
    record = replace(record, body="Aborted because quotes were stale, prices unavailable, and volume was low")
    assert _reason_signature(record) == "quotes were stale prices unavailable and volume was low"


def test_a_new_sentence_delivery_clause_is_still_handled_after_the_comma_fix():
    # Regression guard: the pre-existing new-SENTENCE case (the comment
    # this fix sits beside) is unaffected -- it already stopped at the
    # period, before the comma-stop logic even runs.
    record = _record("e", "2026-08-26T18:36:00Z", slot="1030", gate="5")
    record = replace(
        record,
        body="Aborted because quotes were stale. Email send attempt failed; draft created.",
    )
    assert _reason_signature(record) == "quotes were stale"


def test_the_first_attempt_exemption_does_not_bind_across_clauses():
    # Codex review: "Bind the first-attempt exemption to the surviving
    # failure". "initially" and the retry's failure sat in the SAME
    # sentence (no sentence-boundary punctuation between them), so the
    # sentence-wide co-occurrence check exempted the whole sentence even
    # though "initially" modifies only the FIRST, superseded clause -- the
    # retry clause that actually survives as the final state carries no
    # qualifier of its own.
    body = "Email failed initially, but retry email could not be sent"
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_the_first_attempt_exemption_still_binds_within_one_clause():
    # Regression guard: the two pre-existing exemption cases (round 35),
    # where the qualifier and the failure share ONE clause with no
    # comma/but boundary between them, are unaffected by requiring
    # clause-level co-location.
    for body in (
        "Email send failed on the first attempt; draft created",
        "Email could not be sent initially; draft created",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
        assert "EMAIL_DRAFT_NOT_DELIVERED" not in codes, body


def test_a_bare_claim_is_still_flagged_after_the_clause_binding_fix():
    # Regression guard: the pre-existing bare-claim test is unaffected.
    body = "Email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_an_unrelated_prior_sentence_initially_is_still_screened_after_the_clause_fix():
    # Regression guard: the round-36 sentence-scoping fix ("Initially Gate
    # 5 failed. Email could not be sent.") is unaffected by additionally
    # requiring clause-level co-location within the qualifying sentence.
    body = "Initially Gate 5 failed. Email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_a_later_file_failure_is_not_cleared_by_an_earlier_unrelated_sent_message():
    # Codex review: "Keep earlier sent alerts from clearing later file
    # failures". Gate 9's alert filename is fixed per (day, slot, gate), so
    # a retry loop OVERWRITES the file -- the surviving file here describes
    # a SECOND, LATER firing's own genuine failure, while an EARLIER
    # firing's unrelated successful send still corroborates on (day, slot,
    # gate) because _corroborates() does not check timestamps. The message
    # being older than the file it is paired with means it cannot be proof
    # that THIS later failure was ever resolved.
    #
    # Round 43 ("Keep earlier messages separate from later file firings")
    # went further: an older message is now rejected as a COUNTERPART
    # outright, not merely excluded from clearing the file's warning --
    # not contradicting on verdict/reason is not proof of being the SAME
    # firing when the message provably predates the file's own event time,
    # and pairing them anyway still undercounted two real firings as one.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T16:00:00Z", "1430", "Gate 4")],
        [_file_record("f1", "2026-08-26T18:36:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 2
    file_record = next(r for r in merged if r.source == "alert_file")
    assert _DRAFT_WARNING[0] in {code for code, _ in file_record.warnings}


def test_a_later_sent_message_still_clears_an_earlier_files_failure():
    # Regression guard: the pre-existing case this drop exists for -- a
    # retry that succeeds AFTER the file recorded the initial failure --
    # is unaffected by requiring the message not be older than the file.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T18:36:00Z", "1430", "Gate 4")],
        [
            _file_record(
                "f1",
                "2026-08-26T16:00:00Z",
                "1430",
                "Gate 4",
                warnings=[_DRAFT_WARNING, ("SLACK_DISABLED", "x")],
            )
        ],
    )
    assert len(merged) == 1
    codes = {code for code, _ in merged[0].warnings}
    assert _DRAFT_WARNING[0] not in codes
    assert "SLACK_DISABLED" in codes


def test_a_same_timestamp_sent_message_still_clears_the_files_failure():
    # Regression guard: a message at the EXACT same instant as the slot's
    # own scheduled trigger (not strictly earlier) still counts as proof of
    # that firing's delivery -- the fix only excludes a message strictly
    # OLDER than the slot's own scheduled trigger time. 1430 ET on
    # 2026-08-26 (EDT, UTC-4) is 18:30 UTC -- the file's own stored
    # timestamp no longer matters to this comparison (see
    # test_a_slot_aware_bound_replaces_the_files_noon_stamp below), only the
    # slot string does.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T18:30:00Z", "1430", "Gate 4")],
        [_file_record("f1", "2026-08-26T16:00:00Z", "1430", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] not in {code for code, _ in merged[0].warnings}


# --- round 38 ------------------------------------------------------------


def test_the_first_attempt_exemption_checks_every_qualifying_sentence():
    # Codex review: "Check every failure sentence before granting the
    # exemption". The clause-level fix (round 37) still returned as soon as
    # the FIRST sentence with an all-qualified failure was found, without
    # checking later sentences. "Email send failed initially. Retry email
    # could not be sent." splits into two sentences on the period -- the
    # first is fully qualified ("initially" shares its own clause with
    # "send failed"), but the second names the actual SURVIVING,
    # unqualified failure states_email_failure() reports, and was never
    # reached.
    body = "Email send failed initially. Retry email could not be sent."
    codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "EMAIL_DRAFT_NOT_DELIVERED" in codes


def test_the_first_attempt_exemption_still_works_across_every_qualifying_sentence():
    # Regression guard: the pre-existing single-qualifying-sentence
    # exemption cases, and the multi-clause case from round 37, are
    # unaffected by requiring EVERY qualifying sentence to pass, not just
    # the first found.
    for body in (
        "Email send failed on the first attempt; draft created",
        "Email could not be sent initially; draft created",
        "Email failed initially, but retry email could not be sent",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="gmail_message")}
        is_exempted = "EMAIL_DRAFT_NOT_DELIVERED" not in codes
        expected_exempted = body != "Email failed initially, but retry email could not be sent"
        assert is_exempted == expected_exempted, body


def test_a_bare_claim_is_still_flagged_after_the_whole_body_scoping_fix():
    # Regression guard: the pre-existing bare-claim and unrelated-prior-
    # sentence cases are unaffected.
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {
        code for code, _ in detect_channel_warnings("Email could not be sent.", source="gmail_message")
    }
    assert "EMAIL_DRAFT_NOT_DELIVERED" in {
        code
        for code, _ in detect_channel_warnings(
            "Initially Gate 5 failed. Email could not be sent.", source="gmail_message"
        )
    }


def test_slack_channel_check_requires_a_delivery_outcome_not_just_the_word():
    # Codex review: "Require an actual Slack-delivery claim". The channel
    # pattern was the bare word "slack", so any affirmative, unnegated
    # mention at all -- including a purely informational one that never
    # claims anything was routed there -- tripped SLACK_DISABLED.
    for body in ("Checked Slack configuration", "Slack status was healthy"):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" not in codes, body


def test_slack_channel_check_still_flags_a_genuine_delivery_claim():
    # Regression guard: the pre-existing worked example this check exists
    # to catch -- and the exempt/negation cases -- are unaffected by
    # requiring a delivery-outcome word.
    codes = {code for code, _ in detect_channel_warnings("Posted the Gate 5 alert to Slack", source="alert_file")}
    assert "SLACK_DISABLED" in codes
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Ran drift_detection.py --slack and it posted to Slack.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Slack is disabled in config, so no Slack alert was posted.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        code
        for code, _ in detect_channel_warnings("Posted the Gate 5 alert to Slack with no errors", source="alert_file")
    }
    assert "SLACK_DISABLED" in codes


def test_gitignored_path_check_requires_a_path_component_boundary():
    # Codex review: "Require path-component boundaries in ignored-path
    # matches". Neither alternative was anchored at its start, so a
    # directory name that merely ENDS with the target text -- not the
    # target text itself as a path component -- matched too.
    for body in (
        "workspace/mycron_tracking/morning_dca/alerts/x.txt",
        "workspace/durable_artifacts/alerts/x.txt",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "GITIGNORED_ALERT_PATH" not in codes, body


def test_gitignored_path_check_still_flags_the_real_components():
    # Regression guard: the actual gitignored components -- at the start of
    # a mention, after a path separator, or after a Windows separator --
    # are unaffected by requiring a boundary.
    #
    # The start-of-string case ("artifacts/alerts/x was written", no file
    # extension) deliberately avoids a period inside the path token itself:
    # round 45 added a write-claim requirement (see
    # test_gitignored_path_requires_a_write_claim), and _SENTENCE_SPLIT cuts
    # on "." -- a path ending "x.txt" would split "was written" into a
    # SEPARATE sentence from the path match, stranding the write claim this
    # check now requires outside the path's own sentence.
    for body in (
        "wrote to artifacts/alerts/x.txt",
        "wrote to cron_tracking/morning_dca/alerts/x.txt",
        "wrote to workspace\\cron_tracking\\alerts\\x.txt",
        "artifacts/alerts/x was written",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "GITIGNORED_ALERT_PATH" in codes, body


def test_a_slot_aware_bound_replaces_the_files_noon_stamp():
    # Codex review: "Compare retries against the file's actual event time".
    # A contract-named alert file is stamped at a synthetic ET noon
    # regardless of its actual slot (see load_alert_files), so round 37's
    # fix -- comparing a message straight against record.timestamp_utc --
    # misjudged a genuine PRE-noon retry (0935/1030/1130) as "older than
    # the file" and left a resolved failure stuck stale. A message sent at
    # 09:40 AM ET (13:40 UTC) for a 0935 firing is well before the file's
    # noon-ET stamp (16:00 UTC) but is still a legitimate same-morning
    # retry -- at or after the slot's OWN scheduled trigger (09:35 ET =
    # 13:35 UTC).
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T13:40:00Z", "0935", "Gate 4")],
        [_file_record("f1", "2026-08-26T16:00:00Z", "0935", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] not in {code for code, _ in merged[0].warnings}


def test_a_message_before_its_own_slot_trigger_does_not_clear_the_failure():
    # Regression guard / extension: a message sent BEFORE the slot's own
    # scheduled trigger cannot be a retry of that firing at all, and must
    # not clear the file's failure -- 1030 ET is 14:30 UTC on 2026-08-26;
    # a message at 13:00 UTC (9:00 AM ET) predates the slot's own trigger.
    #
    # Round 43: also rejected as a counterpart outright (see the comment on
    # test_a_later_file_failure_is_not_cleared_by_an_earlier_unrelated_
    # sent_message above), so the two stay separate records.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T13:00:00Z", "1030", "Gate 4")],
        [_file_record("f1", "2026-08-26T16:00:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 2
    file_record = next(r for r in merged if r.source == "alert_file")
    assert _DRAFT_WARNING[0] in {code for code, _ in file_record.warnings}


# --- round 39 ------------------------------------------------------------


def test_gitignored_path_check_matches_a_nested_artifacts_descendant():
    # Codex review: "Match every descendant of the ignored artifacts root".
    # "alerts" had to be a DIRECT child of "artifacts", but a bare
    # .gitignore "artifacts/" entry discards the whole subtree at any
    # depth -- "artifacts/archive/alerts/x.txt" is exactly as gitignored as
    # "artifacts/alerts/x.txt".
    codes = {
        code
        for code, _ in detect_channel_warnings("wrote to artifacts/archive/alerts/x.txt", source="alert_file")
    }
    assert "GITIGNORED_ALERT_PATH" in codes


def test_gitignored_path_check_still_matches_the_direct_child_form():
    # Regression guard: the pre-existing direct-child and cron_tracking
    # forms are unaffected by widening "artifacts" to allow nesting.
    for body in (
        "wrote to artifacts/alerts/x.txt",
        "wrote to cron_tracking/morning_dca/alerts/x.txt",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "GITIGNORED_ALERT_PATH" in codes, body


def test_a_nested_external_artifacts_path_is_still_exempted():
    # Regression guard: widening the bare GITIGNORED_ALERT_PATH pattern to
    # match a nested nested "artifacts/.../alerts" form means it now also
    # matches that shape INSIDE an absolute, external path -- so
    # _BODY_ARTIFACTS_PATH (which the exemption search relies on) had to be
    # widened the exact same way, or a legitimate external durable root
    # with this nesting would be wrongly flagged.
    body = "Durable alert written to /mnt/durable/artifacts/archive/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_states_email_failure_recognizes_the_bare_fallback_form():
    # Codex review: "Recognize bare email-failure fallback wording". Neither
    # "Email failed; draft created" nor "Email failed and was saved as a
    # draft" names "delivery" or "send", so neither matched _EMAIL_FAILURE
    # or _SEND_FAILED, and states_email_failure() returned False on
    # explicit non-delivery evidence.
    assert states_email_failure("Email failed; draft created")
    assert states_email_failure("Email failed and was saved as a draft")


def test_a_negated_bare_email_failure_is_still_ignored():
    # Regression guard: the new bare "email failed" alternative inherits
    # the same before-reach negation screening every other _EMAIL_FAILURE
    # alternative already gets in states_email_failure()'s matching loop.
    assert not states_email_failure("Email did not fail to send")


def test_off_contract_alert_file_uses_its_own_declared_body_date(tmp_path):
    # Codex review: "Use declared dates for off-contract alert files". A
    # file with no valid contract filename falls back to its filesystem
    # mtime, which is not authoritative -- a later copy or restore
    # reassigns it to the copy day. A body-stated "Date:" field should
    # correct that the same way it already does for a Gmail record.
    path = tmp_path / "incident.txt"
    path.write_text("Slot: 1030\nFailing gate: Gate 4\nDate: 2026-08-20\nSome narrative.", encoding="utf-8")
    copy_time = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(path, (copy_time, copy_time))
    (record,) = load_alert_files(tmp_path)
    assert record.date_evidence == ""
    assert record.et_date == date(2026, 8, 20)


def test_a_contract_named_file_keeps_its_filename_date_over_a_historical_body_date():
    # Regression guard: a file with a VALID contract filename is still
    # exempt from a body-stated date -- its filename date is authoritative
    # by construction, and a body can narrate an unrelated PRIOR date under
    # the same "Date:" label without misdating the current, correctly-named
    # file.
    record = _file_record("2026-08-26-1030ET-gate4.txt", "2026-08-26T17:00:00Z", "1030", "Gate 4")
    record = replace(record, body="Previous run Date: 2026-08-25 aborted differently.", date_evidence="filename")
    assert record.et_date == date(2026, 8, 26)


def test_a_malformed_contract_filename_still_prefers_a_genuine_body_date(tmp_path):
    # Regression guard: an off-contract file whose GARBLED filename happens
    # to contain a bare ISO-shaped substring (round 9's own worked example,
    # "2026-13-40-1030-gate5.txt") must not have that non-date block the
    # fall-through to the body's own genuine "Date:" field -- the "subject"
    # search that protects a Gmail record's real subject line is deliberately
    # skipped entirely for a file record, whose "subject" is only ever its
    # filename.
    path = tmp_path / "2026-13-40-1030-gate5.txt"
    path.write_text("Slot: 1030\nFailing gate: Gate 5\nDate: 2026-08-20\nSome narrative.", encoding="utf-8")
    (record,) = load_alert_files(tmp_path)
    assert record.date_evidence == ""
    assert record.et_date == date(2026, 8, 20)


def test_reason_code_requires_an_identifier_boundary():
    # Codex review: "Require token boundaries for named reason codes". A
    # bare `str.find` substring search let "NOT_GATE5_DISABLED" count as an
    # occurrence of "GATE5_DISABLED" -- and the negation screen's
    # word-boundary pattern (`\bnot\b`) never caught it either, since the
    # underscore joining "NOT_" to "GATE5_DISABLED" is itself a WORD
    # character, so there is no word/non-word transition for `\b` to
    # anchor on.
    gate, evidence = detect_gate_evidence("", "Verdict: NOT_GATE5_DISABLED; Failing gate: Gate 2")
    assert (gate, evidence) == ("Gate 2", "labelled")


def test_reason_code_still_matches_its_own_bare_form():
    # Regression guard: the pre-existing bare, negated, and prior-event
    # reason-code cases are unaffected by requiring an identifier boundary.
    assert detect_gate_evidence("", "Verdict: GATE5_DISABLED because the entry point is not available") == (
        "GATE5_DISABLED",
        "reason_code",
    )
    assert detect_gate_evidence("", "This was not GATE5_DISABLED, but this run is GATE5_DISABLED") == (
        "GATE5_DISABLED",
        "reason_code",
    )
    assert detect_gate_evidence("", "Previous run was GATE5_DISABLED; this run failed at Gate 2") == (
        "Gate 2",
        "free_text",
    )


# --- round 40 ------------------------------------------------------------


def test_states_email_failure_splits_although_introduced_retries():
    # Codex review: "Split retry outcomes introduced by 'although'".
    # "although"/"though" were not _CLAUSE_SPLIT boundaries, so the whole
    # sentence stayed one clause and the final override check compared the
    # CLAUSE's start position (before the failure) against last_failure_end
    # instead of the later success's own position, so the success could
    # never satisfy `start >= last_failure_end`.
    assert not states_email_failure("Email could not be sent initially although retry email was sent successfully")
    assert not states_email_failure("Email could not be sent initially though retry email was sent successfully")


def test_states_email_failure_unaffected_by_the_although_split():
    # Regression guard: the pre-existing bare-failure and cross-channel
    # "and"-joined cases are unaffected by adding "although"/"though" as
    # clause boundaries.
    assert states_email_failure("Email could not be sent.")
    assert not states_email_failure("Slack could not send and email sent successfully")


def test_an_unpadded_935_et_label_is_normalized():
    # Codex review: "Accept the unpadded explicit '935 ET' label". The
    # explicit-ET branch (step 3, distinct from the labelled/declared-clock
    # branches already normalized in round 37) still required the 4-digit
    # "0935" form, so a compliant "935 ET" subject label matched nothing and
    # the record was retained with slot=None.
    assert detect_slot_evidence("935 ET | 2026-08-26 | Gate 5") == ("0935", "explicit_et")


def test_a_padded_explicit_et_label_is_unaffected():
    # Regression guard: the pre-existing 4-digit explicit-ET form, and
    # prior-event screening for this step, are unaffected.
    assert detect_slot_evidence("1030 ET | 2026-08-26 | Gate 4") == ("1030", "explicit_et")
    assert detect_slot_evidence("Previous run at 935 ET. This run failed at 1130 ET") == ("1130", "explicit_et")


def test_slack_delivery_outcome_requires_a_verb_not_a_bare_noun():
    # Codex review: "Require verbs for Slack delivery outcomes". The
    # delivery-outcome guard's "alert" alternative had an optional suffix
    # (matching the bare noun) and "notif...ication" is itself only ever a
    # noun form, so purely informational text ("Checked Slack alert
    # configuration", "Slack notification status was healthy") still
    # counted as a delivery claim.
    for body in ("Checked Slack alert configuration", "Slack notification status was healthy"):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" not in codes, body


def test_slack_delivery_outcome_still_matches_genuine_verbal_claims():
    # Regression guard: the pre-existing genuine-claim example, and verbal
    # (not bare-noun) forms of "alert" and "notification", are unaffected.
    for body in (
        "Posted the Gate 5 alert to Slack",
        "Alerted the team via Slack",
        "Sent a Slack notification for the Gate 5 alert",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" in codes, body


def test_verdict_negation_recognizes_without_and_never():
    # Codex review: "Recognize 'without' and 'never' as verdict negation".
    # The shared negation set (used by both detect_gate_evidence's
    # reason-code screening and detect_verdict's specific-verdict
    # screening) recognized neither "without" nor "never", so a denial
    # phrased either way still classified as the disclaimed verdict.
    assert detect_verdict("", "Run ABORTED; completed without rejection") == "ABORT"
    assert detect_verdict("", "Run ABORTED; the order was never rejected") == "ABORT"


def test_verdict_negation_still_matches_a_genuine_rejection():
    # Regression guard: a genuine, unnegated REJECTED verdict, and the
    # pre-existing "not"-negated case, are unaffected.
    assert detect_verdict("", "Run ABORTED; the order was rejected") == "REJECTED"
    assert detect_verdict("", "Run ABORTED; the order was not rejected") == "ABORT"


def test_returned_status_failure_excludes_success_codes():
    # Codex review: "Restrict returned status failures to error codes". Any
    # three-digit code counted as a send failure, so "Email send returned
    # 200 OK" -- a SUCCESS response -- registered as non-delivery.
    assert not states_email_failure("Email send returned 200 OK")


def test_returned_status_failure_still_matches_error_codes():
    # Regression guard: the worked example this branch exists for (401,
    # itself a 4xx code), plus a 5xx code, are unaffected; a 3xx redirect
    # is (correctly) still not treated as a failure.
    assert states_email_failure("Email send returned 401 Unauthorized")
    assert states_email_failure("Email send returned 503 Service Unavailable")
    assert not states_email_failure("Email send returned 302 Found")


def test_task_name_reference_screening_recognizes_overlap_with():
    # Codex review: "Treat 'overlap with' task names as references".
    # GATE4_EXPECTED_OVERLAP is this repo's own reason code for exactly
    # this condition, but the task-name reference cue only recognized
    # "conflict"/"collision"/"clash" with, so "Expected overlap with
    # 1030-dca-recurring-buy" in a 1530 ET alert's body was read as
    # self-identifying the OTHER run's slot instead of being screened as a
    # reference to it.
    body = "1530 ET | GATE4_EXPECTED_OVERLAP\nExpected overlap with 1030-dca-recurring-buy"
    assert detect_slot_evidence(body) == ("1530", "explicit_et")


def test_task_name_reference_screening_still_recognizes_conflict_with():
    # Regression guard: the pre-existing "conflict with" reference-cue case
    # is unaffected by adding "overlap with".
    body = "Gate 4 conflict with a prior run; Slot: 1130-mornig-buying-run"
    assert detect_slot_evidence(body) == ("1130", "task_name")


# --- round 41 ------------------------------------------------------------


def test_states_email_failure_binds_negation_to_the_failed_email_claim():
    # Codex review: "Bind failure negation to the failed email claim". The
    # 40-char before-reach for an _EMAIL_FAILURE match was a raw slice, not
    # bounded at a causal-clause opener, so "No delivery occurred because
    # email failed" had the leading "No" (negating "delivery occurred", a
    # DIFFERENT clause) wrongly discard the affirmative "email failed"
    # match in the "because"-introduced causal clause.
    assert states_email_failure("No delivery occurred because email failed")


def test_states_email_failure_negation_still_binds_same_clause():
    # Regression guard: the pre-existing same-clause negation cases (round
    # 36) are unaffected by bounding the reach at "because".
    assert not states_email_failure("No email delivery failure occurred")
    assert not states_email_failure("No email send failed here")
    assert states_email_failure("Email delivery failure occurred")


def test_slack_channel_negation_binds_to_the_delivery_claim_not_a_because_clause():
    # Codex review: "Bind channel negation to the Slack delivery claim".
    # "because" was not a _CLAUSE_SPLIT boundary, so "No alert reached the
    # operator because it was posted only to Slack" stayed one clause and
    # the leading "No" (negating "alert reached the operator") reached
    # across "because" to wrongly suppress the affirmative Slack-delivery
    # claim in the causal clause.
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "No alert reached the operator because it was posted only to Slack", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" in codes


def test_slack_channel_negation_still_binds_same_clause():
    # Regression guard: the pre-existing same-clause negation case is
    # unaffected by adding "because" as a _CLAUSE_SPLIT boundary.
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Slack is disabled in config, so no Slack alert was posted.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_gitignored_path_check_matches_a_file_directly_under_the_ignored_root():
    # Codex review: "Match alert files directly beneath ignored roots". A
    # bare .gitignore "artifacts/" entry discards its WHOLE subtree, so a
    # fallback alert written directly there -- with no "alerts" directory
    # anywhere in the path at all -- is just as gitignored as one nested
    # under "artifacts/alerts/".
    for body in (
        "wrote alert to artifacts/2026-08-26-1030-gate5.txt",
        "wrote alert to artifacts/archive/2026-08-26-1030-gate5.txt",
    ):
        codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
        assert "GITIGNORED_ALERT_PATH" in codes, body


def test_gitignored_path_check_still_rejects_a_prefixed_sibling():
    # Regression guard: the pre-existing path-component-boundary case
    # (round 39) is unaffected by dropping the "alerts" requirement.
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "workspace/cron_tracking_backup/morning_dca/alerts/2026-08-26-1030-gate5.txt", source="alert_file"
        )
    }
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_a_nested_external_path_with_no_alerts_component_is_still_exempted():
    # Regression guard: dropping the "alerts" requirement on the bare
    # GITIGNORED_ALERT_PATH pattern means it now also matches that shape
    # INSIDE an absolute, external path with no "alerts" directory either --
    # so _BODY_ARTIFACTS_PATH (which the external-path exemption search
    # relies on) needed the identical widening, or a legitimate external
    # durable root written directly under "artifacts/" would be wrongly
    # flagged.
    body = "Durable alert written to /mnt/durable/artifacts/2026-08-26-1030-gate5.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_verdict_negation_binds_to_the_rejected_predicate_not_a_different_one():
    # Codex review: "Bind verdict negation to the rejected predicate". The
    # fixed-width before-reach for the specific-verdict screen was not
    # bounded at "because", so "The order was never placed because the
    # broker rejected it" had "never" (modifying "placed", a DIFFERENT
    # predicate) wrongly negate the affirmative "rejected" match in the
    # causal clause -- corrupting a genuine REJECTED verdict back to the
    # generic ABORT subject.
    assert detect_verdict("Run ABORTED", "The order was never placed because the broker rejected it") == "REJECTED"


def test_verdict_negation_still_binds_same_clause():
    # Regression guard: the pre-existing "without"/"never" same-clause
    # negation cases (round 40) are unaffected by adding "because" as a
    # _CLAUSE_SPLIT boundary.
    assert detect_verdict("", "Run ABORTED; completed without rejection") == "ABORT"
    assert detect_verdict("", "Run ABORTED; the order was never rejected") == "ABORT"
    assert detect_verdict("", "Run ABORTED; the order was rejected") == "REJECTED"


def test_merge_compares_delivery_proof_against_the_files_own_mtime():
    # Codex review: "Compare delivery proof with the overwritten firing".
    # The slot's nominal trigger time (round 39's fix) is shared by EVERY
    # firing of that (day, slot), so it cannot distinguish an early,
    # successful firing from a LATER same-slot retry that overwrote the
    # file with a genuine failure. A 12:30 slot that sends successfully at
    # 12:35 ET and then fires the same gate/reason again at 14:30 ET with
    # email delivery failing must not have the file's failure cleared by
    # the earlier, unrelated success -- the file's own mtime (14:30 ET =
    # 18:30 UTC, when Gate 9 actually overwrote it) is the real ground
    # truth for which firing survives on disk.
    #
    # Round 43 ("Keep earlier messages separate from later file firings"):
    # this earlier message is also rejected as a COUNTERPART outright now,
    # not merely excluded from clearing the warning -- these are two real,
    # distinct firings of the same slot/gate, and pairing them on content
    # alone still undercounted them as one.
    earlier_success = _message_record("m1", "2026-08-26T16:35:00Z", "1230", "Gate 4")
    file_mtime_ns = int(datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    later_file = replace(
        _file_record("f1", "2026-08-26T17:00:00Z", "1230", "Gate 4", warnings=[_DRAFT_WARNING]),
        revision=str(file_mtime_ns),
    )
    merged = merge_cross_source([earlier_success], [later_file])
    assert len(merged) == 2
    file_record = next(r for r in merged if r.source == "alert_file")
    assert _DRAFT_WARNING[0] in {code for code, _ in file_record.warnings}


def test_merge_still_clears_a_genuine_retry_after_the_files_mtime():
    # Regression guard: a message sent AFTER the file's own mtime -- the
    # genuine retry that resolved THAT firing -- still clears the warning.
    file_mtime_ns = int(datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    later_file = replace(
        _file_record("f1", "2026-08-26T17:00:00Z", "1230", "Gate 4", warnings=[_DRAFT_WARNING]),
        revision=str(file_mtime_ns),
    )
    later_retry = _message_record("m2", "2026-08-26T18:40:00Z", "1230", "Gate 4")
    merged = merge_cross_source([later_retry], [later_file])
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] not in {code for code, _ in merged[0].warnings}


def test_merge_falls_back_to_the_slot_scheduled_time_without_a_revision():
    # Regression guard: a record with no revision at all (the shape every
    # pre-existing merge_cross_source test in this file already uses) still
    # falls back to the coarser slot-scheduled-time bound from round 39,
    # rather than raising or silently treating every message as proof.
    merged = merge_cross_source(
        [_message_record("m1", "2026-08-26T13:40:00Z", "0935", "Gate 4")],
        [_file_record("f1", "2026-08-26T16:00:00Z", "0935", "Gate 4", warnings=[_DRAFT_WARNING])],
    )
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] not in {code for code, _ in merged[0].warnings}


def test_task_name_reference_screening_recognizes_collided_with():
    # Codex review: "Treat 'collided with' task names as references". The
    # past-tense VERB form "collided" was missing alongside the noun
    # "collision" already in the cue, so "This run collided with
    # 1030-dca-recurring-buy" in a 1530 ET alert's body was read as
    # self-identifying the OTHER run's slot.
    body = "1530 ET | GATE4_EXPECTED_OVERLAP\nThis run collided with 1030-dca-recurring-buy"
    assert detect_slot_evidence(body) == ("1530", "explicit_et")


# --- round 42 ------------------------------------------------------------


def test_merge_cross_source_preserves_the_drafts_ledger_identity():
    # Codex review: "Keep ledger identity stable across source
    # reconciliation". merge_cross_source() relabels a corroborated draft
    # under the surviving file's own (source, alert_id), so a draft
    # ledgered as new on one run reappeared as "new" again once its file
    # counterpart showed up, even though nothing about the underlying event
    # or its warnings changed.
    draft = _record("d1", "2026-08-26T18:36:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    pre_merge_key = draft.key
    file_record = _file_record("2026-08-26-1030ET-gate4.txt", "2026-08-26T17:00:00Z", "1030", "Gate 4")
    (merged,) = merge_cross_source([draft], [file_record])
    assert merged.key != pre_merge_key
    assert pre_merge_key in merged.prior_keys


def test_merge_cross_source_ledger_alias_does_not_hide_a_genuine_new_warning():
    # Regression guard: the alias must be computed from the FINAL combined
    # warning set, not the draft's bare pre-merge key -- otherwise a run
    # where the file contributes a genuinely NEW warning code would alias
    # to the draft's unchanged key and wrongly suppress that discovery,
    # exactly the failure mode AlertRecord.key's own docstring says this
    # identity must never produce.
    draft = _record("d1", "2026-08-26T18:36:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    seen = {draft.key}
    file_with_new_warning = replace(
        _file_record("2026-08-26-1030ET-gate4.txt", "2026-08-26T17:00:00Z", "1030", "Gate 4"),
        warnings=(("SLACK_DISABLED", "x"),),
    )
    (merged,) = merge_cross_source([draft], [file_with_new_warning])
    is_new = merged.key not in seen and not any(alias in seen for alias in merged.prior_keys)
    assert is_new


def test_merge_cross_source_ledger_alias_recognizes_an_unchanged_merge():
    # Regression guard: the steady-state case the fix exists for -- a
    # second run where the file contributes nothing new beyond the draft's
    # own warnings -- is recognized as already-seen via the alias.
    draft = _record("d1", "2026-08-26T18:36:00Z", "1030", "Gate 4", warnings=[_DRAFT_WARNING])
    seen = {draft.key}
    file_record = _file_record("2026-08-26-1030ET-gate4.txt", "2026-08-26T17:00:00Z", "1030", "Gate 4")
    (merged,) = merge_cross_source([draft], [file_record])
    is_new = merged.key not in seen and not any(alias in seen for alias in merged.prior_keys)
    assert not is_new


def test_states_email_failure_examines_every_success_outcome_in_a_clause():
    # Codex review: "Examine every success outcome in a clause". A colon
    # (not a _CLAUSE_SPLIT boundary) joined two retry outcomes into one
    # clause -- "First retry email was not sent successfully: second retry
    # email sent successfully." -- and the override scan's `.search()`
    # stopped at the first, NEGATED success without ever reaching the
    # later, genuinely unnegated one. The root cause went one level deeper
    # than just the search call: the earlier "negated success is itself a
    # failure" branch also extended last_failure_end to the WHOLE clause's
    # end rather than the negated match's own end, swallowing the later
    # success's position too.
    body = "Email failed initially. First retry email was not sent successfully: second retry email sent successfully."
    assert not states_email_failure(body)


def test_states_email_failure_still_flags_a_bare_negated_success():
    # Regression guard: the pre-existing bare negated-success case (no
    # later real success anywhere) is unaffected.
    assert states_email_failure("Retry email was not successfully sent")


def test_states_email_failure_still_clears_a_normal_two_sentence_retry():
    # Regression guard: the ordinary two-SENTENCE retry case (a genuine
    # period boundary between failure and success, no colon involved) is
    # unaffected.
    assert not states_email_failure("Email failed to send. Retry email sent successfully.")


def test_gitignored_path_requires_a_write_claim_not_an_avoided_one():
    # Codex review: "Require a write claim for body-only ignored paths".
    # The bare GITIGNORED_ALERT_PATH pattern had no negation entry for
    # "instead of", so "Used the durable stream_root path instead of
    # artifacts/alerts/x.txt" -- which names the gitignored path only to
    # say it was AVOIDED -- was still treated as affirmative storage
    # evidence.
    body = "Used the durable stream_root path instead of artifacts/alerts/x.txt"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_channel_claim_before_instead_of_is_still_affirmed():
    # Regression guard: "instead of" is forward-only -- it must not
    # suppress a claim that comes BEFORE it in the same clause.
    codes = {
        code for code, _ in detect_channel_warnings("Posted to Slack instead of using email", source="alert_file")
    }
    assert "SLACK_DISABLED" in codes


def test_a_genuine_gitignored_write_claim_is_still_flagged():
    # Regression guard: an ordinary, unqualified write claim is unaffected.
    codes = {code for code, _ in detect_channel_warnings("wrote to artifacts/alerts/x.txt", source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_permitted_slack_marker_must_bind_to_the_delivered_claim():
    # Codex review: "Bind permitted Slack markers to the delivered claim".
    # The bare "drift" exemption reached across "because" into an unrelated
    # CAUSAL clause explaining why drift detection was unavailable, not
    # identifying a permitted route for the actual, unsupported Slack post.
    body = "Posted the Gate 5 alert to Slack because drift detection was unavailable"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_permitted_slack_marker_still_exempts_the_and_continuation_case():
    # Regression guard: the pre-existing "and"-continuation and "and"-parallel
    # exempt-route cases (round 36) are unaffected by truncating at "because".
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Ran drift_detection.py --slack and it posted to Slack.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Posted drift via DCA_DRIFT_SLACK_WEBHOOK and posted the Gate 5 alert to Slack", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" in codes


def test_permitted_slack_marker_still_exempts_a_genuine_permitted_route():
    # Regression guard: the pre-existing, unqualified permitted-route case
    # is unaffected.
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Posted drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_reason_signature_skips_a_negated_cause():
    # Codex review: "Skip negated reason matches during reconciliation".
    # _reason_signature() screened only for prior-event wording, so "Aborted
    # not because quotes were stale; because budget was exhausted." captured
    # the DENIED cause ("quotes were stale") instead of the real one stated
    # second.
    record = _record("a", "2026-08-26T18:00:00Z", "1030", "Gate 4")
    record = replace(record, body="Aborted not because quotes were stale; because budget was exhausted.")
    assert _reason_signature(record) == "budget was exhausted"


def test_reason_signature_still_matches_an_unnegated_cause():
    # Regression guard: the pre-existing bare and labelled reason-signature
    # cases are unaffected.
    record = _record("b", "2026-08-26T18:00:00Z", "1030", "Gate 4")
    record = replace(record, body="aborted because quotes were stale")
    assert _reason_signature(record) == "quotes were stale"
    record2 = _record("c", "2026-08-26T18:00:00Z", "1030", "Gate 4")
    record2 = replace(record2, body="Reason: budget exhausted")
    assert _reason_signature(record2) == "budget exhausted"


def test_corroborates_treats_a_negated_cause_reconciliation_as_distinct_incidents():
    # Regression guard / integration: the exact scenario the finding warns
    # about -- two same-day, same-slot firings that would have wrongly
    # corroborated on the denied cause -- are correctly treated as distinct.
    a = _file_record(
        "a", "2026-08-26T18:00:00Z", "1030", "Gate 4"
    )
    a = replace(a, body="Aborted not because quotes were stale; because budget was exhausted.")
    b = _file_record("b", "2026-08-26T18:05:00Z", "1030", "Gate 4")
    b = replace(b, body="Aborted because quotes were stale.")
    assert not _corroborates(a, b)


# --- round 43 ------------------------------------------------------------


def test_merge_keeps_an_older_message_as_a_separate_record_from_a_later_file():
    # Codex review: "Keep earlier messages separate from later file
    # firings". Rounds 41-42 kept the file's own warning from being cleared
    # by an older, unrelated message, but merge_cross_source() still PAIRED
    # the two into one merged record -- not contradicting on verdict/reason
    # is not proof of being the SAME firing when the message provably
    # predates the file's own event time (_file_event_utc). Pairing them
    # anyway still reduced two real firings to one, undercounting gate
    # frequency, coverage, and repeat-loop evidence even though the
    # warning itself was correctly preserved.
    file_mtime_ns = int(datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    later_file = replace(
        _file_record("f1", "2026-08-26T17:00:00Z", "1230", "Gate 4", warnings=[_DRAFT_WARNING]),
        revision=str(file_mtime_ns),
    )
    earlier_success = _message_record("m1", "2026-08-26T16:35:00Z", "1230", "Gate 4")
    merged = merge_cross_source([earlier_success], [later_file])
    assert len(merged) == 2
    sources = {r.source for r in merged}
    assert sources == {"gmail_message", "alert_file"}
    file_record = next(r for r in merged if r.source == "alert_file")
    assert _DRAFT_WARNING[0] in {code for code, _ in file_record.warnings}


def test_merge_still_pairs_a_genuine_later_retry_with_its_file():
    # Regression guard: a message sent AFTER the file's own event time --
    # the genuine retry that resolved THAT firing -- is still accepted as
    # its counterpart and the two still merge into one record.
    file_mtime_ns = int(datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    later_file = replace(
        _file_record("f1", "2026-08-26T17:00:00Z", "1230", "Gate 4", warnings=[_DRAFT_WARNING]),
        revision=str(file_mtime_ns),
    )
    later_retry = _message_record("m2", "2026-08-26T18:40:00Z", "1230", "Gate 4")
    merged = merge_cross_source([later_retry], [later_file])
    assert len(merged) == 1
    assert _DRAFT_WARNING[0] not in {code for code, _ in merged[0].warnings}


def test_merge_still_rejects_a_draft_counterpart_that_genuinely_contradicts():
    # Regression guard: the pre-existing contradiction-based rejection
    # (verdict/reason mismatch) is unaffected by adding the message-timing
    # gate -- a draft candidate is still evaluated by _corroborates() alone,
    # with no timing gate applied to it at all.
    draft_a = _record("d1", "2026-08-26T18:00:00Z", "1030", "Gate 4", verdict="ABORT")
    draft_a = replace(draft_a, body="aborted because quotes were stale")
    draft_b = _record("d2", "2026-08-26T18:05:00Z", "1030", "Gate 4", verdict="ABORT")
    draft_b = replace(draft_b, body="aborted because the budget was exhausted")
    file_record = _file_record("f1", "2026-08-26T18:10:00Z", "1030", "Gate 4")
    file_record = replace(file_record, body="aborted because quotes were stale")
    merged = merge_cross_source([draft_a, draft_b], [file_record])
    assert len(merged) == 2
    remaining_draft = next(r for r in merged if r.source == "gmail_draft")
    assert remaining_draft.alert_id == "d2"


# --- round 44 ------------------------------------------------------------


def test_declared_date_excludes_a_compound_end_date_label():
    # Codex review: "Restrict body date matches to the alert's event
    # field". _DECLARED_ET_DATE matched any phrase ending in "date", so a
    # Gate 3 body quoting config/trading_window.json's own end_date --
    # "Trading window end date: 2026-10-30" -- was read as the ALERT's own
    # declared event date instead of its transport day.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:00:00Z"),
        subject="Gate 3 alert",
        body="Trading window end date: 2026-10-30",
        slot="1030",
        gate="3",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 26)


def test_declared_date_still_matches_ordinary_prose_before_the_label():
    # Regression guard: the pre-existing cases where "Date:" follows
    # ordinary sentence prose (not one of the excluded compound-label
    # modifiers) are unaffected.
    for body in ("Previous run Date: 2026-08-25", "ABORT Gate 4 -- Date: 2026-08-26"):
        assert _DECLARED_ET_DATE.search(body) is not None, body


def test_declared_date_still_matches_a_bare_label():
    # Regression guard: the ordinary, unqualified "Date:" label is
    # unaffected.
    record = AlertRecord(
        alert_id="b",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:00:00Z"),
        subject="x",
        body="Date: 2026-08-20",
        slot="1030",
        gate="4",
        verdict="ABORT",
    )
    assert record.et_date == date(2026, 8, 20)


def test_permitted_slack_marker_does_not_reach_forward_across_and():
    # Codex review: "Stop later Slack markers from exempting an earlier
    # post". When the unsupported Slack claim precedes an "and"-joined
    # marker, the exempt search fell through to searching the WHOLE
    # (unbounded-forward) clause, so "Posted the Gate 5 alert to Slack and
    # then encountered an unhandled provider error" let the trailing,
    # unrelated "unhandled" exempt the earlier post.
    body = "Posted the Gate 5 alert to Slack and then encountered an unhandled provider error"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_permitted_slack_marker_still_reaches_forward_on_a_genuine_continuation():
    # Regression guard / extension: a genuine pronoun-led continuation
    # AFTER the match (the same shape already exempted when it precedes
    # the match) still reaches forward correctly.
    #
    # Uses "the drift finding" rather than the original "the Gate 5 alert"
    # as the posted subject: round 49 added a route/alert-type validation
    # (see test_slack_permitted_route_requires_a_matching_alert_type) that
    # this body would otherwise now correctly fail -- DCA_DRIFT_SLACK_WEBHOOK
    # is scoped to drift findings only, and claiming a routine Gate 5 alert
    # went through it is exactly the violation that check exists to catch.
    # The forward-reach mechanism this test targets is unaffected by the
    # swap; "drift" and "DCA_DRIFT_SLACK_WEBHOOK" are both still in reach of
    # each other across "and it used...".
    body = "Posted the drift finding to Slack and it used DCA_DRIFT_SLACK_WEBHOOK"
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_permitted_slack_marker_forward_truncation_does_not_affect_backward_cases():
    # Regression guard: the pre-existing backward-reaching cases (round 36)
    # -- match in a LATER segment, exempt cue in an EARLIER one -- are
    # unaffected by adding the symmetric forward bound.
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Ran drift_detection.py --slack and it posted to Slack.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        code
        for code, _ in detect_channel_warnings(
            "Posted drift via DCA_DRIFT_SLACK_WEBHOOK and posted the Gate 5 alert to Slack", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" in codes


# --- round 45 ------------------------------------------------------------


def test_gitignored_path_requires_a_write_claim():
    # Codex review: "Require a write claim before flagging ignored paths".
    # The bare path token is purely informational here -- explaining the
    # rule itself and saying a DIFFERENT, durable path was used -- not a
    # claim that this alert was ever stored under the gitignored one.
    body = "the bare artifacts/alerts path is gitignored; used the durable stream_root path."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_gitignored_path_with_a_write_claim_still_flagged():
    # Regression guard: a body that DOES claim a write to the gitignored
    # path is unaffected by requiring a write-claim verb.
    for body in (
        "Durable alert written to artifacts/alerts/2026-08-26-1430-gate4.txt",
        "wrote to artifacts/alerts/x.txt",
        "Saved to artifacts/alerts/x.txt",
        "Durable alert stored at cron_tracking/morning_dca/alerts/x.txt",
        "created artifacts/alerts/x.txt",
    ):
        codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
        assert "GITIGNORED_ALERT_PATH" in codes, body


def test_gitignored_path_write_claim_is_clause_scoped():
    # The write-claim check is scoped to the mention's own clause, mirroring
    # the Slack delivery-outcome check: a write claim in an UNRELATED clause
    # must not count toward a separate, bare mention of the gitignored path.
    body = "Wrote the summary file, but the artifacts/alerts path was only mentioned in the log"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_gitignored_path_external_exemption_still_applies_without_a_write_claim():
    # Regression guard: the pre-existing external-path exemption
    # (_gitignored_mention_is_external) is independent of, and unaffected
    # by, the new write-claim requirement -- an external path is exempt
    # whether or not its own clause happens to use a write verb.
    body = "Saved to /mnt/durable/artifacts/alerts/x.txt"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_task_name_reference_cue_is_bound_to_its_own_sentence():
    # Codex review: "Bound task-reference cues to the current sentence".
    # "Conflict with" in the FIRST sentence must not disqualify a task name
    # naming THIS run's own identity in a later, unrelated sentence -- there
    # is no comma/semicolon/but/however/while/whereas/yet between the two
    # sentences for the old, sentence-unbounded clause search to stop at.
    slot, evidence = detect_slot_evidence("Conflict with a legacy order. Current task 1030-dca-recurring-buy aborted")
    assert slot == "1030"
    assert evidence == "task_name"


def test_task_name_reference_cue_still_applies_within_the_same_sentence():
    # Regression guard: the pre-existing same-sentence (semicolon-bounded)
    # disclaiming case is unaffected by adding the outer sentence bound.
    slot, evidence = detect_slot_evidence("Gate 4 conflict with a prior run; Slot: 1130-mornig-buying-run")
    assert slot == "1130"
    assert evidence == "task_name"


def test_task_name_reference_cue_still_applies_within_the_same_clause():
    # Regression guard: a "conflict with" cue immediately before the task
    # name, in the same sentence and clause, still disclaims it as another
    # run's identity rather than this record's own.
    slot, evidence = detect_slot_evidence("Conflict with 1030-dca-recurring-buy")
    assert slot != "1030" or evidence != "task_name"


def test_permitted_slack_marker_requires_concrete_route_evidence():
    # Codex review: "Require an actual route before exempting Slack
    # delivery". The bare word "drift" describes the alert's CONTENT --
    # what was observed -- not proof that the drift webhook route was
    # actually used to deliver THIS post; the thing posted here is the
    # routine Gate 5 alert, not a drift finding.
    body = "Posted the Gate 5 alert to Slack after observing drift"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_permitted_slack_marker_bare_uncaught_no_longer_exempts():
    # Same gap, the other removed bare word: "uncaught" alone describes
    # what happened, not that the uncaught-failure webhook was used.
    body = "Posted the Gate 5 alert to Slack after an uncaught exception"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_permitted_slack_marker_concrete_routes_still_exempt():
    # Regression guard: the three routes that DO name concrete evidence --
    # the two webhook env vars, and the drift_detection.py --slack
    # invocation -- are unaffected by removing the bare content words.
    for body in (
        "Posted the drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK.",
        "Sent through DCA_ALERT_SLACK_WEBHOOK after an uncaught exception.",
        "Ran drift_detection.py --slack and it posted to Slack.",
    ):
        codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" not in codes, body


# --- round 46 ------------------------------------------------------------


def test_gitignored_path_write_claim_survives_a_file_extension_period():
    # Codex review: "Keep filename extensions with their write claims". The
    # write verb follows the path here, and the path's own ".txt" extension
    # must not be misread as the sentence boundary that determines what
    # text the write-claim check is allowed to see.
    body = "artifacts/alerts/x.txt was written"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


@pytest.mark.parametrize("path_form", ["posix", "windows", "unc"])
@pytest.mark.parametrize("relative_path", ["artifacts/alerts/x.txt", "cron_tracking/morning_dca/alerts/x.txt"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("template, written", [
    ("Durable alert written to {path}", True),
    ("{path} was written", True),
    ("Wrote {path}. This path was not used.", True),
    ("Did not write {path}.", False),
    ("{path} is gitignored. Wrote a durable copy elsewhere.", False),
])
def test_gitignored_dotted_parent_paths_preserve_claim_scope(
    tmp_path, monkeypatch, path_form, relative_path, external, template, written,
):
    if path_form == "posix":
        checkout = (tmp_path / "checkout.with.dots").resolve()
    elif path_form == "windows":
        checkout = PureWindowsPath("C:/workspace.with.dots/checkout")
    else:
        checkout = PureWindowsPath("//server/share.with.dots/checkout")
    monkeypatch.setattr("scripts.alert_digest._REPO_ROOT", Path(str(checkout)))
    location = checkout.with_name("external.with.dots") if external else checkout
    alert_path = location.joinpath(*relative_path.split("/"))
    body = template.format(path=alert_path)
    codes = {code for code, _ in detect_channel_warnings(body, source="alert_file")}
    assert ("GITIGNORED_ALERT_PATH" in codes) == (written and not external)


def test_gitignored_path_extension_period_masking_does_not_affect_other_sentences():
    # Regression guard: the exact case _PATH_WRITE_CLAIM was added for --
    # a genuine sentence-ending period elsewhere in the body -- still
    # correctly separates the informational half from an unrelated one.
    body = "the bare artifacts/alerts path is gitignored; used the durable stream_root path."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_gitignored_path_extension_period_masking_does_not_affect_negation():
    # Regression guard: negation/exemption scoping around a path match with
    # an extension is unaffected by masking its internal period.
    body = "Used the durable stream_root path instead of artifacts/alerts/x.txt"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_gitignored_path_extension_masking_preserves_external_exemption():
    # Regression guard: an external absolute path with a file extension is
    # still recognized and exempted after masking.
    body = "Saved to /mnt/durable/artifacts/alerts/x.txt"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_states_email_failure_examines_every_send_failed_outcome_in_a_clause():
    # Codex review: "Examine every generic failure outcome in a clause". A
    # colon (not a _CLAUSE_SPLIT boundary) joins two outcomes into one
    # clause; the first, negated match ("not throttled") must not stop the
    # scan before it reaches the later, genuinely unnegated "send failed".
    assert states_email_failure("Email send was not throttled: retry send failed")


def test_states_email_failure_send_failed_negation_does_not_reach_past_a_prior_match():
    # Regression guard for the deeper fix this required: the second match's
    # own before-reach must not extend past the END of the first match, or
    # the "not" negating the FIRST match wrongly negates the second too.
    assert states_email_failure("Email send was not throttled: retry send failed")
    assert not states_email_failure("Email send was not throttled")


def test_states_email_failure_still_flags_a_single_send_failed_match():
    # Regression guard: the ordinary single-match case is unaffected.
    assert states_email_failure("Email send failed after three attempts")
    assert not states_email_failure("Slack send failed.")


def test_et_date_falls_through_to_body_date_after_a_malformed_subject_date():
    # Codex review: "Fall through after malformed subject dates". The
    # subject's ISO-shaped token is not a real calendar date (month 13),
    # so this must retry the body's own labelled date instead of falling
    # straight through to the transport day.
    record = AlertRecord(
        alert_id="x",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-27T18:00:00Z"),
        subject="1030 ET | 2026-13-40 | Gate 5",
        body="Date: 2026-08-20",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
    )
    assert record.et_date == date(2026, 8, 20)


def test_et_date_still_falls_back_to_transport_day_with_no_body_date():
    # Regression guard: a malformed subject date with no usable body date
    # at all still falls back to the transport day, not an exception.
    record = AlertRecord(
        alert_id="x",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-27T18:00:00Z"),
        subject="1030 ET | 2026-13-40 | Gate 5",
        body="no date mentioned here",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
    )
    assert record.et_date == date(2026, 8, 27)


def test_et_date_still_uses_a_valid_subject_date():
    # Regression guard: a genuinely valid subject date is unaffected and
    # still wins over the body.
    record = AlertRecord(
        alert_id="x",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-27T18:00:00Z"),
        subject="1030 ET | 2026-08-26 | Gate 5",
        body="Date: 2026-08-20",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
    )
    assert record.et_date == date(2026, 8, 26)


def test_task_name_reference_cue_is_checked_after_the_task_name_too():
    # Codex review: "Check reference cues after task names". When the task
    # name is the clause's own grammatical subject, the reference cue
    # follows it instead of preceding it -- a before-only search missed
    # this and attributed the record to the OTHER task before ever
    # reaching the subject's own explicit slot declaration.
    slot, evidence = detect_slot_evidence("1530 ET | GATE4_EXPECTED_OVERLAP\n1030-dca-recurring-buy collided with this run")
    assert slot == "1530"
    assert evidence == "explicit_et"


def test_task_name_reference_cue_after_task_name_still_scoped_to_its_clause():
    # Regression guard: a reference cue AFTER the task name but in a
    # DIFFERENT clause (split by "but") must not disqualify the task name.
    slot, evidence = detect_slot_evidence(
        "1030-dca-recurring-buy fired normally, but conflict with 1130-mornig-buying-run happened later"
    )
    assert slot == "1030"
    assert evidence == "task_name"


def test_task_name_reference_cue_before_check_still_applies():
    # Regression guard: the pre-existing before-only cases are unaffected
    # by adding the after check.
    slot, evidence = detect_slot_evidence("Conflict with a legacy order. Current task 1030-dca-recurring-buy aborted")
    assert slot == "1030"
    assert evidence == "task_name"
    slot, evidence = detect_slot_evidence("Gate 4 conflict with a prior run; Slot: 1130-mornig-buying-run")
    assert slot == "1130"
    assert evidence == "task_name"


# --- round 47 ------------------------------------------------------------


def test_gitignored_path_extension_masking_preserves_a_trailing_sentence_boundary():
    # Codex review: "Preserve sentence boundaries after path extensions".
    # The greedy path pattern sweeps the trailing sentence-ending period in
    # too ("artifacts/alerts/x.txt." as one match); masking THAT period as
    # well as the extension's own merged the next sentence in, letting its
    # "not" reach back and wrongly negate the match.
    body = "Wrote artifacts/alerts/x.txt. This path was not used."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_gitignored_path_extension_masking_still_masks_internal_periods():
    # Regression guard: round 46's own case -- a write claim following an
    # extension with no trailing sentence period -- is unaffected by only
    # skipping the match's OWN last character.
    body = "artifacts/alerts/x.txt was written"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_gitignored_path_extension_masking_regression_guards_unaffected():
    # Regression guard: the round-45 informational case and the external-
    # path exemption, both involving a path with a file extension, are
    # unaffected by only skipping the match's trailing period.
    body = "the bare artifacts/alerts path is gitignored; used the durable stream_root path."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes
    body = "Saved to /mnt/durable/artifacts/alerts/x.txt"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_task_name_after_cue_requires_a_current_run_reference():
    # Codex review: "Distinguish self-identifying task subjects from
    # references". The collision cue after the task name names "a legacy
    # order", not this run -- it says nothing about whose identity the
    # task name itself is, and the subject explicitly self-labels it as
    # "Current task".
    slot, evidence = detect_slot_evidence("Current task 1030-dca-recurring-buy collided with a legacy order and aborted")
    assert slot == "1030"
    assert evidence == "task_name"


def test_task_name_after_cue_still_disqualifies_a_current_run_reference():
    # Regression guard: round 46's own exact reported example -- the
    # after-cue's object explicitly names "this run" -- still disqualifies.
    slot, evidence = detect_slot_evidence("1530 ET | GATE4_EXPECTED_OVERLAP\n1030-dca-recurring-buy collided with this run")
    assert slot == "1530"
    assert evidence == "explicit_et"


def test_message_first_attempt_qualifier_ignores_a_non_email_send_failure():
    # Codex review: "Scope first-attempt qualification to email failures".
    # The backup connector's own failure clause never names email at all,
    # so it must not count as an unqualified EMAIL failure -- the only
    # actual email failure ("initially") is properly qualified as history.
    body = "Email send failed initially, but the backup connector send failed."
    assert _message_failure_is_first_attempt_qualified(body)
    assert detect_channel_warnings(body, source="gmail_message") == ()


def test_message_first_attempt_qualifier_still_requires_every_email_failure_qualified():
    # Regression guard: an unqualified failure that DOES name email in its
    # own clause still denies the exemption.
    body = "Email send failed initially, and email could not be sent."
    assert not _message_failure_is_first_attempt_qualified(body)


def test_explicit_et_recognizes_colon_form_slot_declarations():
    # Codex review: "Recognize colon-form ET slot declarations". Natural,
    # unambiguous Gate 9 subjects using the colon clock form must resolve
    # the same as the compact 4-digit form already does.
    slot, evidence = detect_slot_evidence("9:35 ET | 2026-08-26 | Gate 5")
    assert slot == "0935"
    assert evidence == "explicit_et"
    slot, evidence = detect_slot_evidence("13:30 ET | 2026-08-26 | Gate 5")
    assert slot == "1330"
    assert evidence == "explicit_et"


def test_explicit_et_colon_form_regression_guards():
    # Regression guard: the pre-existing compact-digit forms (padded and
    # unpadded) are unaffected, and a non-roster colon-ET time does not
    # spuriously resolve via this branch.
    slot, evidence = detect_slot_evidence("1030 ET | 2026-08-26 | Gate 5")
    assert slot == "1030"
    assert evidence == "explicit_et"
    slot, evidence = detect_slot_evidence("935 ET | 2026-08-26 | Gate 5")
    assert slot == "0935"
    assert evidence == "explicit_et"
    assert detect_slot_evidence("Fired at 9:07 PM ET, off-schedule") == (None, "")


# --- round 48 ------------------------------------------------------------


def test_slack_delivery_outcome_recognizes_route_verbs():
    # Codex review: "Recognize route verbs as Slack delivery outcomes".
    # Ordinary alternative delivery phrasing -- none of "post"/"send"/
    # "deliver"/"notify"/"alert" matched any of these.
    for body in (
        "Routed the Gate 5 alert to Slack",
        "Slack received the Gate 5 alert",
        "The alert went to Slack",
    ):
        codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
        assert "SLACK_DISABLED" in codes, body


def test_slack_delivery_outcome_route_verbs_regression_guards():
    # Regression guard: purely informational mentions and the permitted-
    # route exemption are unaffected by the new verb alternatives.
    codes = {c for c, _ in detect_channel_warnings("Checked Slack configuration", source="alert_file")}
    assert "SLACK_DISABLED" not in codes
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_task_name_reference_cue_recognizes_comparative_mentions():
    # Codex review: "Prefer explicit slots over comparative task mentions".
    # A Gate 4 alert naming the OTHER task it cross-checked orders against
    # is exactly as much a reference to another run's identity as a
    # "conflict with" mention, and must not outrank the record's own
    # explicit "1530 ET" declaration.
    slot, evidence = detect_slot_evidence("1530 ET | Gate 4\nCompared orders against 1030-dca-recurring-buy")
    assert slot == "1530"
    assert evidence == "explicit_et"
    slot, evidence = detect_slot_evidence("1530 ET | Gate 4\nChecked fills against 1030-dca-recurring-buy")
    assert slot == "1530"
    assert evidence == "explicit_et"


def test_task_name_reference_cue_comparative_regression_guards():
    # Regression guard: the pre-existing "conflict with" cue and a genuine
    # self-identifying task name with no comparative wording are unaffected.
    slot, evidence = detect_slot_evidence("Conflict with a legacy order. Current task 1030-dca-recurring-buy aborted")
    assert slot == "1030"
    assert evidence == "task_name"
    slot, evidence = detect_slot_evidence("Current task 1030-dca-recurring-buy aborted")
    assert slot == "1030"
    assert evidence == "task_name"


def test_states_email_failure_recognizes_went_out_as_success():
    # Codex review: "Recognize 'went out' as a retry success". Ordinary
    # delivery-success phrasing for a final retry, alongside "sent",
    # "delivered", and "succeeded".
    assert not states_email_failure("Email send failed initially, but the retry email went out")


def test_states_email_failure_went_out_regression_guards():
    # Regression guard: a genuine, unresolved failure is still flagged.
    assert states_email_failure("Email send failed after three attempts")
    assert states_email_failure("Email could not be sent, and the retry also failed.")


# --- round 49 ------------------------------------------------------------


def test_gitignored_path_recognizes_append_as_a_write_claim():
    # Codex review: "Recognize append as a path write claim". The Gate 9
    # fallback prompts' own prescribed verb, alongside "wrote"/"saved"/
    # "stored"/"created".
    body = "Appended the alert to artifacts/alerts/x.txt"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_gitignored_path_append_regression_guards():
    # Regression guard: the pre-existing write-claim verbs and the
    # informational/no-write-claim case are unaffected.
    codes = {c for c, _ in detect_channel_warnings("wrote to artifacts/alerts/x.txt", source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes
    body = "the bare artifacts/alerts path is gitignored; used the durable stream_root path."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_slack_permitted_route_requires_a_matching_alert_type():
    # Codex review: "Validate alert type before exempting Slack routes".
    # config/notifications.json scopes DCA_ALERT_SLACK_WEBHOOK to uncaught-
    # exception failures only, not routine Gate N outcomes -- naming the
    # env var alone is not proof the alert is that type.
    body = "Posted the Gate 5 alert to Slack via DCA_ALERT_SLACK_WEBHOOK"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes
    # The symmetric drift-webhook gap: DCA_DRIFT_SLACK_WEBHOOK is scoped to
    # drift findings from scripts/drift_detection.py only.
    body = "Posted the Gate 5 alert to Slack via DCA_DRIFT_SLACK_WEBHOOK"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_slack_permitted_route_still_exempt_with_a_matching_alert_type():
    # Regression guard: each webhook still exempts a genuinely type-matched
    # claim, and drift_detection.py/--slack (which need no separate topic
    # check) are unaffected.
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Sent through DCA_ALERT_SLACK_WEBHOOK after an uncaught exception.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Ran drift_detection.py --slack and it posted to Slack.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_states_email_failure_treats_errors_after_despite_as_superseded():
    # Codex review: "Treat errors after 'despite' as superseded". Success
    # stated FIRST, with the trailing error only explaining what didn't
    # prevent delivery -- the same success-first shape as the pre-existing
    # "after"/"because"/"once"/"before" forms.
    assert not states_email_failure("Email sent successfully despite a provider error")


def test_states_email_failure_despite_regression_guards():
    # Regression guard: a genuine, unresolved failure with no success
    # wording at all is still flagged.
    assert states_email_failure("Email send failed after three attempts")


def test_detect_verdict_recognizes_adjectival_prior_event_qualifiers():
    # Codex review: "Recognize adjectival prior-event qualifiers". "prior"
    # as a bare adjective (not just "prior to this") marks the same kind of
    # historical mention "previous <noun>" already covers.
    assert detect_verdict("ABORT", "The previous order was rejected; this run aborted") == "ABORT"
    assert detect_verdict("ABORT", "A prior attempt was rejected. This run aborted.") == "ABORT"


def test_detect_verdict_prior_event_regression_guards():
    # Regression guard: the pre-existing "previous run/firing/attempt" and
    # "prior to this" forms, and a genuine CURRENT-run REJECTED verdict
    # with no historical qualifier, are unaffected.
    assert detect_verdict("Run ABORTED", "Previous run failed at Gate 2. This run aborted.") == "ABORT"
    assert detect_verdict("Gate 7 order rejected", "cap breach mentioned in passing") == "REJECTED"
    assert detect_verdict(subject="", body="the order was rejected") == "REJECTED"


# --- round 50 ------------------------------------------------------------


def test_states_email_failure_inspects_every_success_before_truncating():
    # Codex review: "Inspect every success before truncating resolved
    # errors". A colon (not a _CLAUSE_SPLIT boundary) joins a negated first
    # attempt to a genuinely unnegated later success; the trailing
    # "despite a provider error" must be treated as superseded by the
    # SECOND success, not re-examined as a fresh, current failure.
    assert not states_email_failure(
        "Email was not sent successfully: retry email sent successfully despite a provider error"
    )


def test_success_negation_before_reach_does_not_cross_a_prior_match():
    # Regression guard for the deeper fix this required: a second success
    # match's own before-reach must not extend past the END of a first
    # match already examined in the same span, or a negation before the
    # FIRST match wrongly negates the SECOND one too. Round 42's own
    # worked example for the override scan specifically.
    assert not states_email_failure(
        "First retry email was not sent successfully: second retry email sent successfully."
    )


def test_states_email_failure_success_truncation_regression_guards():
    # Regression guard: the ordinary single-failure and single-negated-
    # success cases are unaffected.
    assert states_email_failure("Email send failed after three attempts")
    assert states_email_failure("Email was not sent successfully")


def test_states_email_failure_recognizes_plain_draft_outcomes():
    # Codex review: "Treat explicit email drafts as non-delivery". Each of
    # these directly establishes non-delivery without an "instead"
    # qualifier.
    assert states_email_failure("Email draft created")
    assert states_email_failure("Email was saved as a draft")
    assert states_email_failure("Email remained in drafts")


def test_states_email_failure_plain_draft_outcome_can_be_overridden():
    # Regression guard: a later, genuine send-success still overrides a
    # plain draft-outcome sentence, the same as any other _SEND_FAILED
    # match.
    assert not states_email_failure("Email draft created. Retry email sent successfully.")


def test_states_email_failure_plain_draft_outcome_still_requires_email_in_clause():
    # Regression guard: the pre-existing _EMAIL_MENTION.search(clause)
    # scoping is unaffected -- an unrelated draft with no email mention in
    # its own clause still does not count.
    assert not states_email_failure("Order draft was created instead of submission")


def test_channel_warning_negation_preserves_without_delay_and_errors():
    # Codex review: "Preserve affirmative claims made without delay or
    # errors". The bare "without" in these idioms describes the ABSENCE of
    # a problem, not a denial of the action itself.
    codes = {c for c, _ in detect_channel_warnings("Posted the Gate 5 alert to Slack without delay", source="alert_file")}
    assert "SLACK_DISABLED" in codes
    codes = {c for c, _ in detect_channel_warnings("Wrote artifacts/alerts/x.txt without errors", source="alert_file")}
    assert "GITIGNORED_ALERT_PATH" in codes


def test_channel_warning_without_manner_phrase_regression_guards():
    # Regression guard: a genuine "without" disclaimer -- naming the
    # channel NOT used, rather than describing HOW a real action went --
    # is unaffected.
    codes = {
        c for c, _ in detect_channel_warnings("Posted the Gate 5 alert without using Slack", source="alert_file")
    }
    assert "SLACK_DISABLED" not in codes


# --- round 51 ------------------------------------------------------------


def test_reason_signature_recognizes_a_free_form_clause_before_the_verdict():
    # Codex review: "Preserve incidents whose free-form reasons differ".
    # Neither body uses "reason:"/"because"/"due to"/"owing to" at all, but
    # both DO state a real, DIFFERENT reason immediately before the verdict
    # word.
    r1 = _record("a", "2026-08-26T14:30:00Z", "1030", "5", verdict="ABORT")
    r1 = replace(r1, body="Quotes were stale; aborted")
    r2 = _record("b", "2026-08-26T14:35:00Z", "1030", "5", verdict="ABORT")
    r2 = replace(r2, body="Budget was exhausted; aborted")
    assert _reason_signature(r1) == "quotes were stale"
    assert _reason_signature(r2) == "budget was exhausted"
    assert not _corroborates(r1, r2)


def test_reason_signature_free_form_fallback_regression_guards():
    # Regression guard: the pre-existing labelled/causal forms still take
    # priority over the new fallback, and two records that genuinely state
    # NO reason at all (neither cue nor a free-form clause before a verdict
    # word) do not corroborate without event-level identity.
    r1 = _record("a", "2026-08-26T14:30:00Z", "1030", "5", verdict="ABORT")
    r1 = replace(r1, body="Aborted because quotes were stale")
    r2 = _record("b", "2026-08-26T14:35:00Z", "1030", "5", verdict="ABORT")
    r2 = replace(r2, body="Aborted because quotes were stale")
    assert _reason_signature(r1) == _reason_signature(r2) == "quotes were stale"
    assert _corroborates(r1, r2)
    r3 = _record("c", "2026-08-26T14:30:00Z", "1030", "5", verdict="ABORT")
    r3 = replace(r3, body="GATE5_DISABLED")
    r4 = _record("d", "2026-08-26T14:35:00Z", "1030", "5", verdict="ABORT")
    r4 = replace(r4, body="GATE5_DISABLED")
    assert _reason_signature(r3) is None
    assert _reason_signature(r4) is None
    assert not _corroborates(r3, r4)


def test_file_event_utc_rejects_an_implausible_copied_file_mtime():
    # Codex review: "Do not treat copied-file mtime as the alert event
    # time". A file named for 2026-08-26 (date_evidence="filename") but
    # with an mtime three weeks later cannot be the real Gate 9 write --
    # that mtime is a copy/restore artifact, and the coarser nominal-
    # trigger bound is used instead.
    copy_mtime_ns = str(int(parse_timestamp("2026-09-15T12:00:00Z").timestamp() * 1_000_000_000))
    record = AlertRecord(
        alert_id="x",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T17:00:00Z"),
        subject="",
        body="",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
        revision=copy_mtime_ns,
        date_evidence="filename",
    )
    assert _file_event_utc(record) == _slot_scheduled_utc(record.et_date, record.slot)


def test_file_event_utc_mtime_plausibility_regression_guards():
    # Regression guard: a genuine, same-day mtime is still trusted, and a
    # record with no authoritative filename date (date_evidence !=
    # "filename") has nothing to cross-check against, so its mtime is
    # trusted as before too.
    genuine_mtime_ns = str(int(parse_timestamp("2026-08-26T15:31:00Z").timestamp() * 1_000_000_000))
    record = AlertRecord(
        alert_id="x",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T17:00:00Z"),
        subject="",
        body="",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
        revision=genuine_mtime_ns,
        date_evidence="filename",
    )
    assert _file_event_utc(record) == parse_timestamp("2026-08-26T15:31:00Z")

    no_filename_evidence_mtime_ns = str(int(parse_timestamp("2026-09-15T12:00:00Z").timestamp() * 1_000_000_000))
    record2 = AlertRecord(
        alert_id="y",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T17:00:00Z"),
        subject="",
        body="",
        slot="1030",
        gate="5",
        verdict="ABORT",
        warnings=(),
        revision=no_filename_evidence_mtime_ns,
        date_evidence="",
    )
    assert _file_event_utc(record2) == parse_timestamp("2026-09-15T12:00:00Z")


# --- round 52 ------------------------------------------------------------


def test_slack_drift_webhook_accepts_the_documented_failure_alert_fallback():
    # Codex review: "Allow failure alerts through the fallback Slack
    # webhook". config/notifications.json names DCA_DRIFT_SLACK_WEBHOOK as
    # optional_failure_alert.fallback_webhook_env -- an uncaught-exception
    # alert legitimately routes through it when DCA_ALERT_SLACK_WEBHOOK is
    # unset.
    body = "Posted the failure alert to Slack via DCA_DRIFT_SLACK_WEBHOOK after an uncaught exception."
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_slack_drift_webhook_fallback_regression_guards():
    # Regression guard: a genuinely wrong-type claim through the drift
    # webhook is still flagged, and the genuine drift-topic case is
    # unaffected.
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the Gate 5 alert to Slack via DCA_DRIFT_SLACK_WEBHOOK", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" in codes
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK.", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_states_email_failure_ignores_a_historical_success():
    # Codex review: "Ignore historical successes when resolving email
    # failures". The override scan's position check alone let a success
    # mention that merely RECOUNTS an earlier run's history clear the
    # CURRENT run's own genuine failure, just because it comes later in
    # the text.
    assert states_email_failure("Email could not be sent. The previous run's email sent successfully.")


def test_states_email_failure_historical_success_regression_guards():
    # Regression guard: a genuine CURRENT retry success still overrides
    # the earlier failure it actually resolves.
    assert not states_email_failure("Email send failed initially, but the retry email sent successfully.")
    assert states_email_failure("Email send failed after three attempts")


# --- round 53 ------------------------------------------------------------


def test_states_email_failure_ignores_a_historical_email_failure():
    # Codex review: "Ignore historical email failures when deriving
    # current delivery" -- the mirror image of the historical-success fix
    # just above. A historical FAILURE positioned after the current
    # sentence's own success must not become last_failure_end just
    # because it comes later in the text.
    assert not states_email_failure("Email sent successfully. The previous run's email failed.")


def test_states_email_failure_ignores_a_historical_send_failed_match():
    # Regression guard / extension: the same historical screen applies to
    # the generic _SEND_FAILED match site, not just _EMAIL_FAILURE.
    assert not states_email_failure("Email sent successfully. The previous run email send failed.")


def test_states_email_failure_ignores_a_historical_negated_success():
    # Regression guard / extension: and to the negated-success-is-a-
    # failure site too.
    assert not states_email_failure("Email sent successfully. The previous run email was not sent successfully.")


def test_states_email_failure_historical_failure_regression_guards():
    # Regression guard: a genuine CURRENT failure (no historical framing
    # at all) is still flagged, and the colon-joined multi-match case from
    # round 46 is unaffected.
    assert states_email_failure("Email send failed after three attempts")
    assert states_email_failure("Email send was not throttled: retry send failed")
    assert states_email_failure("Email sent successfully despite a provider error") is False


def test_collect_records_raises_when_a_nonempty_draft_export_is_entirely_unusable(tmp_path):
    # Codex review: "Fail when a nonempty export yields no usable
    # records". Several items sharing the same broken shape (no date
    # field at all, as after a schema change) must not silently produce
    # a clean zero-drafts digest.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps({"drafts": [{"id": "r1", "subject": "a"}, {"id": "r2", "subject": "b"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="none had a usable timestamp"):
        collect_records(drafts_path=path, alerts_dir=None, read_stdin=False)


def test_collect_records_raises_when_a_nonempty_message_export_is_entirely_unusable(tmp_path):
    # Regression guard / extension: the identical check on the messages
    # source.
    path = tmp_path / "messages.json"
    path.write_text(
        json.dumps({"messages": [{"id": "r1", "subject": "a"}, {"id": "r2", "subject": "b"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="none had a usable timestamp"):
        collect_records(drafts_path=None, alerts_dir=None, read_stdin=False, messages_path=path)


def test_collect_records_still_tolerates_a_single_isolated_malformed_item(tmp_path):
    # Regression guard: a single malformed item (this function's own
    # pre-existing, documented tolerance -- see
    # test_collect_records_counts_drafts_it_had_to_skip) must not now
    # raise just because it happens to be the ONLY item offered.
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": [{"id": "r1", "subject": "no date here"}]}), encoding="utf-8")
    records, skipped, _ = collect_records(drafts_path=path, alerts_dir=None, read_stdin=False)
    assert records == [] and skipped == 1


def test_collect_records_still_tolerates_isolated_items_among_valid_ones(tmp_path):
    # Regression guard: a malformed item alongside genuinely valid ones is
    # still just counted as skipped, not raised -- only a source where
    # NOTHING loaded at all is a health failure.
    path = tmp_path / "drafts.json"
    path.write_text(
        json.dumps(
            {
                "drafts": [
                    {"id": "r1", "subject": "no date here"},
                    _draft("r2", "2026-08-26T14:30:00Z", "1030 ET | 2026-08-26 | Gate 5", "Email send failed"),
                ]
            }
        ),
        encoding="utf-8",
    )
    records, skipped, _ = collect_records(drafts_path=path, alerts_dir=None, read_stdin=False, keep_unmatched=True)
    assert len(records) == 1 and skipped == 1


# --- round 54 ------------------------------------------------------------


def test_detect_verdict_scopes_rejected_to_order_outcomes():
    # Codex review: "Scope rejection verdicts to order outcomes". REJECTED
    # is documented as an order outcome, not a catch-all for any channel's
    # rejection -- an email/Slack/repo-access rejection must not outrank
    # the alert's own explicit generic ABORT subject.
    assert detect_verdict("... ABORT", "Email was rejected by the provider; run aborted") == "ABORT"


def test_detect_verdict_rejected_order_context_regression_guards():
    # Regression guard: a genuine order/broker rejection in the body is
    # still recognized, and an explicit subject-level REJECTED is still
    # trusted outright (no context requirement for subject matches).
    assert detect_verdict("Run ABORTED", "the order was rejected by the broker") == "REJECTED"
    assert detect_verdict("Gate 7 order rejected", "cap breach mentioned in passing") == "REJECTED"


def test_manner_phrase_stripping_preserves_a_genuine_with_no_negation():
    # Codex review: "Restrict manner-phrase stripping to benign
    # qualifiers". The old unbounded `with no \w+` stripped a genuine
    # negation too -- "no messages" here is the real denial that anything
    # was posted, not a benign manner phrase like "with no errors".
    codes = {c for c, _ in detect_channel_warnings("Slack was checked with no messages posted", source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_manner_phrase_stripping_benign_qualifiers_regression_guards():
    # Regression guard: the benign "with no errors/issues" idiom this
    # check exists for is still stripped, so the affirmative claim it
    # follows is still correctly flagged.
    codes = {
        c for c, _ in detect_channel_warnings("Posted the Gate 5 alert to Slack with no errors", source="alert_file")
    }
    assert "SLACK_DISABLED" in codes
    codes = {
        c for c, _ in detect_channel_warnings("Wrote artifacts/alerts/x.txt with no issues", source="alert_file")
    }
    assert "GITIGNORED_ALERT_PATH" in codes


def test_detect_verdict_does_not_resurrect_a_prior_only_outcome():
    # Codex review: "Keep prior-only outcomes unclassified". A specific
    # verdict appearing ONLY in a sentence that explicitly narrates the
    # PREVIOUS run's history, with the CURRENT incident using unrecognized
    # wording, must stay unclassified rather than reporting the historical
    # outcome as if it were the current one.
    body = "Previous run had a cap breach. Current run stopped because repository access was unavailable"
    assert detect_verdict("Gate 2", body) is None


def test_detect_verdict_prior_only_outcome_regression_guards():
    # Regression guard: a genuine CURRENT verdict elsewhere in the body,
    # and an explicit subject verdict, are both unaffected by no longer
    # resurrecting prior-only matches in the last-resort tier.
    assert detect_verdict("", "Previous run aborted at Gate 2. This run skipped at Gate 5") == "SKIPPED"
    assert detect_verdict("ABORTED at Gate 4", "one order was rejected earlier") == "ABORT"


# --- round 55 ------------------------------------------------------------


def test_states_email_failure_recognizes_never_sent():
    # Codex review: "Recognize 'never sent' as email non-delivery". "was
    # never sent"/"was never delivered" state non-delivery just as
    # directly as the existing "was not sent"/"was not delivered" forms.
    assert states_email_failure("Email was never sent")
    assert states_email_failure("Email was never delivered")


def test_states_email_failure_never_sent_regression_guards():
    # Regression guard: the pre-existing "was not" form and a genuine
    # single current failure are unaffected.
    assert states_email_failure("Email was not sent")
    assert states_email_failure("Email send failed after three attempts")


# --- round 56 ------------------------------------------------------------


def test_message_first_attempt_qualifier_screens_a_historical_second_failure():
    # Codex review: "Screen non-current failures before denying the
    # exemption". The second sentence's failure narrates a PREVIOUS run,
    # not this message's own delivery -- states_email_failure() itself
    # already ignores it as history, but _message_failure_is_first_attempt_
    # qualified() collected it unscreened and denied the exemption purely
    # because a historical mention had no first-attempt qualifier of its
    # own to carry.
    body = "Email send failed initially. The previous run email failed."
    assert states_email_failure(body)
    assert _message_failure_is_first_attempt_qualified(body)
    assert detect_channel_warnings(body, source="gmail_message") == ()


def test_message_first_attempt_qualifier_historical_screen_regression_guards():
    # Regression guard: every pre-existing scoping rule this helper's own
    # docstring documents is unaffected by also screening historical
    # mentions -- an unrelated qualifier, an unqualified retry clause
    # sharing a sentence with a qualified first attempt, a later sentence's
    # own unqualified failure, and the plain unqualified/qualified single-
    # failure cases all still resolve exactly as before.
    assert not _message_failure_is_first_attempt_qualified("Initially Gate 5 failed. Email could not be sent.")
    assert not _message_failure_is_first_attempt_qualified(
        "Email failed initially, but retry email could not be sent"
    )
    assert not _message_failure_is_first_attempt_qualified(
        "Email send failed initially. Retry email could not be sent."
    )
    assert not _message_failure_is_first_attempt_qualified("Email could not be sent.")
    assert _message_failure_is_first_attempt_qualified("Email send failed on the first attempt; draft created")


def test_states_email_failure_recognizes_perfect_passive_retry_success():
    # Codex review: "Recognize perfect-passive retry successes". "has/have
    # been sent/delivered" reports a completed retry exactly as plainly as
    # the already-recognized simple past "was/were sent/delivered" -- the
    # only reason the earlier failure survived as this message's state was
    # that _EMAIL_SUCCESS_SUBJECT_SCOPED required "was"/"were" specifically.
    assert not states_email_failure("Email failed initially, but retry email has now been sent.")
    assert not states_email_failure("Email failed initially, but retry email has been sent.")
    assert not states_email_failure("Email failed initially, but retry email have been sent.")
    assert not states_email_failure("Email failed initially, but retry email has been delivered.")


def test_perfect_passive_retry_success_regression_guards():
    # Regression guard: the pre-existing simple-past form, a genuine
    # negated perfect-passive ("has never been sent"), and a plain
    # unqualified failure are all unaffected.
    assert not states_email_failure("Email send failed initially, but retry email was sent")
    assert states_email_failure("Email could not be sent, and it has never been sent.")
    assert states_email_failure("Email send failed.")


def test_ledger_persists_merged_records_alias_keys(tmp_path, capsys):
    # Codex review: "Persist merged records' alias keys in the ledger".
    # merge_cross_source() relabels a corroborated draft under the
    # surviving alert file's own identity and records the draft's own
    # pre-merge key as an alias in `prior_keys` (see
    # test_merge_cross_source_preserves_the_drafts_ledger_identity), but
    # the CLI's ledger write only ever persisted `record.key`, dropping
    # every alias. If the file is later cleaned up -- or `--alerts-dir` is
    # simply omitted from a later run -- the draft reverts to standing
    # alone under its own standalone key, which was never saved, so it is
    # reported as new again for an alert the ledger already reported.
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("d1", "2026-08-26T19:30:00Z", "ABORT 1030 ET Gate 4", "Reason: quotes were stale")
                ]
            }
        ),
        encoding="utf-8",
    )
    alerts_dir = tmp_path / "alerts"
    alerts_dir.mkdir()
    (alerts_dir / "2026-08-26-1030ET-gate4.txt").write_text(
        "ABORT 1030 ET Gate 4\nReason: quotes were stale", encoding="utf-8"
    )
    root = str(tmp_path / "ledger")

    main(["--drafts", str(drafts), "--alerts-dir", str(alerts_dir), "--ledger", "--ledger-root", root, "--json"])
    capsys.readouterr()

    # The file's counterpart disappears (cleaned up, or the next run omits
    # --alerts-dir): the same draft now stands alone under its own
    # standalone key.
    main(["--drafts", str(drafts), "--ledger", "--ledger-root", root, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_since_last_digest"] == []


def test_ledger_persisted_alias_keys_regression_guards(tmp_path, capsys):
    # Regression guard: a genuinely new alert on the second run (the
    # draft's own key changed) is still reported as new, and the
    # first-run behavior -- a fresh ledger reporting everything as new --
    # is unaffected by also persisting alias keys.
    drafts = tmp_path / "drafts.json"
    drafts.write_text(
        json.dumps(
            {
                "drafts": [
                    _draft("d1", "2026-08-26T19:30:00Z", "ABORT 1030 ET Gate 4", "Reason: quotes were stale")
                ]
            }
        ),
        encoding="utf-8",
    )
    alerts_dir = tmp_path / "alerts"
    alerts_dir.mkdir()
    (alerts_dir / "2026-08-26-1030ET-gate4.txt").write_text(
        "ABORT 1030 ET Gate 4\nReason: quotes were stale", encoding="utf-8"
    )
    root = str(tmp_path / "ledger")

    main(["--drafts", str(drafts), "--alerts-dir", str(alerts_dir), "--ledger", "--ledger-root", root, "--json"])
    first_run = json.loads(capsys.readouterr().out)
    assert len(first_run["new_since_last_digest"]) == 1

    other_drafts = tmp_path / "drafts2.json"
    other_drafts.write_text(
        json.dumps({"drafts": [_draft("d2", "2026-08-27T19:30:00Z", "ABORT 1130 ET Gate 4", "y")]}),
        encoding="utf-8",
    )
    main(["--drafts", str(other_drafts), "--ledger", "--ledger-root", root, "--json"])
    second_run = json.loads(capsys.readouterr().out)
    assert len(second_run["new_since_last_digest"]) == 1


# --- round 57 ------------------------------------------------------------


def test_states_email_failure_recognizes_modified_went_out_success():
    # Codex review: "Allow modifiers in retry-success phrases". The "went
    # out" success form required the subject and "went out" adjacent, so a
    # bounded adverb -- "finally", "eventually", "successfully" -- ordinary
    # phrasing for the exact same outcome, matched nothing.
    assert not states_email_failure("Email failed initially, but the retry email finally went out")
    assert not states_email_failure("Email failed initially, but the retry email eventually went out")
    assert not states_email_failure("Email failed initially, but the retry email successfully went out")


def test_modified_went_out_success_regression_guards():
    # Regression guard: the pre-existing unmodified "went out" form, and a
    # NEGATING word in that same gap ("never"), are both unaffected -- the
    # modifier alternation is a fixed short list of affirming adverbs, not
    # an unbounded gap that could admit a negation and flip a genuine
    # failure into a false success match.
    assert not states_email_failure("Email send failed initially, but the retry email went out")
    assert states_email_failure("Email send failed, and the retry email never went out")
    assert states_email_failure("Email send failed after three attempts")


def test_task_name_reference_cue_recognizes_validated_against():
    # Codex review: "Prefer explicit subject slots over task references".
    # "validated ... against" is the same "checked/compared against" shape
    # with a third verb Gate 4 alerts also use, and was missing from the
    # cue -- the referenced task name outranked the record's own explicit
    # "1530 ET" subject declaration.
    slot, evidence = detect_slot_evidence("1530 ET | Gate 4\nValidated orders against 1030-dca-recurring-buy")
    assert slot == "1530"
    assert evidence == "explicit_et"


def test_task_name_reference_cue_validated_against_regression_guards():
    # Regression guard: the pre-existing "compared"/"checked ... against"
    # forms and a genuine self-identifying task name are unaffected.
    slot, evidence = detect_slot_evidence("1530 ET | Gate 4\nCompared orders against 1030-dca-recurring-buy")
    assert slot == "1530"
    assert evidence == "explicit_et"
    slot, evidence = detect_slot_evidence("1530 ET | Gate 4\nChecked fills against 1030-dca-recurring-buy")
    assert slot == "1530"
    assert evidence == "explicit_et"
    slot, evidence = detect_slot_evidence("Current task 1030-dca-recurring-buy aborted")
    assert slot == "1030"
    assert evidence == "task_name"


# --- round 58 ------------------------------------------------------------


def test_detect_verdict_classifies_named_reason_codes():
    # Codex review: "Classify named executor verdicts as verdicts". A
    # compliant "Verdict: GATE5_DISABLED"/"Verdict: GATE4_EXPECTED_OVERLAP"
    # alert states the reason code as its own outcome -- previously
    # recognized only by detect_gate_evidence(), so detect_verdict()
    # returned None and the report filed it under "BY VERDICT:
    # unclassified" despite it being one of the two explicitly named,
    # expected outcomes this corpus tracks.
    assert detect_verdict("Verdict: GATE5_DISABLED", "Failing gate: Gate 5") == "GATE5_DISABLED"
    assert detect_verdict("Verdict: GATE4_EXPECTED_OVERLAP", "Failing gate: Gate 4") == "GATE4_EXPECTED_OVERLAP"


def test_detect_verdict_reason_code_regression_guards():
    # Regression guard: the pre-existing specific/generic verdict patterns,
    # a disclaimed reason-code mention falling through to a genuine ABORT
    # elsewhere in the body, a purely historical reason-code mention, and a
    # reason code affirmed only after being disclaimed once are all
    # unaffected -- mirroring detect_gate_evidence()'s own screening.
    assert detect_verdict("Run ABORTED", "cap breach at Gate 6") == "CAP_BREACH"
    assert detect_verdict("", "This is not GATE5_DISABLED; failing gate: Gate 2") is None
    assert detect_verdict("", "This is not GATE5_DISABLED; the run ABORTED") == "ABORT"
    assert detect_verdict("", "Previous run was GATE5_DISABLED; this run failed at Gate 2") is None
    assert detect_verdict("This was not GATE5_DISABLED, but this run is GATE5_DISABLED", "") == "GATE5_DISABLED"


def test_states_email_failure_recognizes_was_successful_retry():
    # Codex review: "Recognize successful retry-send outcomes". "was/were
    # successful" is another plain, adjective-form delivery outcome
    # alongside the existing "succeeded"/"was sent"/"was delivered" forms.
    assert not states_email_failure("Email failed initially, but retry email send was successful")
    assert not states_email_failure("Email failed initially, but the resend was successful")


def test_was_successful_retry_regression_guards():
    # Regression guard: the pre-existing "succeeded"/"was sent" forms, a
    # negated "was not successful", and a plain unqualified failure are
    # all unaffected.
    assert not states_email_failure("Email send failed initially, but the resend succeeded")
    assert states_email_failure("Email send failed, and the retry was not successful")
    assert states_email_failure("Email send failed.")


# --- round 59 ------------------------------------------------------------


def test_detect_verdict_binds_rejection_context_to_its_own_clause():
    # Codex review: "Bind rejection context to the rejected clause".
    # _REJECTED_ORDER_CONTEXT was checked across the whole SENTENCE, so an
    # unrelated "order" mention in an EARLIER clause satisfied it for a
    # rejection that has nothing to do with an order -- returning REJECTED
    # over the sentence's own explicit ABORT.
    body = "No order was placed, but email was rejected by the provider; this run aborted"
    assert detect_verdict("", body) == "ABORT"


def test_detect_verdict_rejection_context_regression_guards():
    # Regression guard: a genuine order rejection (context in the SAME
    # clause as the rejection) still returns REJECTED, including when the
    # first REJECTED match's own clause lacks context but a later one in
    # the same sentence has it.
    assert detect_verdict("", "The order was rejected at Gate 7") == "REJECTED"
    assert detect_verdict("", "The email was rejected, but the order was rejected too") == "REJECTED"
    assert detect_verdict("Run ABORTED", "cap breach at Gate 6") == "CAP_BREACH"


def test_states_email_failure_recognizes_completion_adverbs_before_passive_send():
    # Codex review: "Allow completion adverbs before passive send verbs".
    # The passive "was/were sent/delivered" form required the auxiliary
    # and the verb adjacent, so "the email was eventually sent" -- the
    # same bounded-adverb shape already allowed for "went out" -- matched
    # nothing.
    assert not states_email_failure("Email failed initially, but the email was eventually sent")
    assert not states_email_failure("Email failed initially, but the email was finally sent")
    assert not states_email_failure("Email failed initially, but retry email has eventually been delivered")


def test_completion_adverbs_before_passive_send_regression_guards():
    # Regression guard: the pre-existing unmodified "was sent" form and a
    # genuine "was never sent" negation are both unaffected.
    assert not states_email_failure("Email send failed initially, but the retry email was sent")
    assert states_email_failure("Email send failed, and the retry email was never sent")
    assert states_email_failure("Email send failed.")


# --- round 60 ------------------------------------------------------------


def test_states_email_failure_ends_generic_failure_at_the_matched_outcome():
    # Codex review: "End generic failures at the matched outcome". A colon
    # (not a _CLAUSE_SPLIT boundary) can join a failed attempt and its
    # successful retry into one clause -- "Email send failed initially:
    # retry email sent successfully" -- and last_failure_end was extended
    # to the whole CLAUSE's end for an unnegated _SEND_FAILED match rather
    # than the match's own end, swallowing the later, already-successful
    # retry so the override scan below could never see it.
    assert not states_email_failure("Email send failed initially: retry email sent successfully")


def test_generic_failure_matched_outcome_regression_guards():
    # Regression guard: the pre-existing colon-joined-clause fix this
    # mirrors (a negated failure followed by a genuine one in the same
    # clause) and a plain unqualified failure are both unaffected.
    assert states_email_failure("Email send was not throttled: retry send failed")
    assert states_email_failure("Email send failed after three attempts")


def test_reason_signature_excludes_delivery_failure_causes():
    # Codex review: "Exclude delivery-failure causes from incident
    # reasons". Gate 9 has the executor append non-delivery evidence to
    # the alert file, so a file copy's own "because" can narrate WHY THE
    # EMAIL FAILED, not why the run aborted -- and being checked ahead of
    # the free-form fallback, it returned that instead of the shared
    # reason the plain email copy (with no delivery clause to append)
    # correctly falls through to, so the two copies of one incident no
    # longer corroborated and were double-counted.
    email_copy = AlertRecord(
        alert_id="a",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    file_copy = AlertRecord(
        alert_id="b",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Email send failed because the provider timed out.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(email_copy) == "quotes were stale"
    assert _reason_signature(file_copy) == "quotes were stale"
    assert _corroborates(email_copy, file_copy)


def test_reason_signature_delivery_failure_exclusion_regression_guards():
    # Regression guard: the pre-existing "because"/"reason:" forms, and a
    # genuine reason that happens to mention "email" only AFTER the cue
    # (not narrating a channel failure before it), are all unaffected.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Aborted because quotes were stale",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record) == "quotes were stale"
    record2 = AlertRecord(
        alert_id="b",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Reason: budget exhausted",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record2) == "budget exhausted"
    record3 = AlertRecord(
        alert_id="c",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Aborted because the email queue backed up",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record3) == "the email queue backed up"


# --- round 61 ------------------------------------------------------------


def test_reason_signature_excludes_every_channel_failure_cause():
    # Codex review: "Exclude every channel-failure cause from incident
    # reasons". The round-60 fix screened only email-specific wording
    # ("email"/"send failed"), so a non-email channel's own delivery
    # failure -- "Slack post failed because the webhook timed out." --
    # still bypassed it: neither _EMAIL_MENTION nor the email-specific
    # failure patterns match "Slack post failed", so the file copy's
    # "because" match returned the delivery cause instead of the shared
    # incident reason, breaking corroboration the same way the email case
    # did.
    email_copy = AlertRecord(
        alert_id="a",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    slack_file_copy = AlertRecord(
        alert_id="b",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Slack post failed because the webhook timed out.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    notion_file_copy = AlertRecord(
        alert_id="c",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Notion sync failed because the API was down.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(slack_file_copy) == "quotes were stale"
    assert _reason_signature(notion_file_copy) == "quotes were stale"
    assert _corroborates(email_copy, slack_file_copy)
    assert _corroborates(email_copy, notion_file_copy)


def test_channel_failure_cause_exclusion_regression_guards():
    # Regression guard: the pre-existing "because"/"reason:" forms, the
    # round-60 email-specific case, and a genuine reason that legitimately
    # mentions "retry" AFTER the cue (not narrating a channel failure
    # before it) are all unaffected.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Aborted because quotes were stale",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record) == "quotes were stale"
    record2 = AlertRecord(
        alert_id="b",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Reason: budget exhausted",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record2) == "budget exhausted"
    record3 = AlertRecord(
        alert_id="c",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Aborted because a retry was necessary for the order",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record3) == "a retry was necessary for the order"


# --- round 62 ------------------------------------------------------------


def test_reason_signature_screens_channel_error_verbs_beyond_failed():
    # Codex review: "Screen all channel-error verbs from reason
    # extraction". The round-61 fix only recognized "failed"/"fails"/
    # "failing" as the channel-failure verb, missing two other ordinary
    # channel-error verbs this corpus uses: "errored" and "was/were
    # rejected" (the webhook/channel sense, not an order rejection).
    email_copy = AlertRecord(
        alert_id="a",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    slack_errored = AlertRecord(
        alert_id="b",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Slack post errored because the webhook stalled.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    message_rejected = AlertRecord(
        alert_id="c",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Message delivery was rejected because the webhook refused it.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(slack_errored) == "quotes were stale"
    assert _reason_signature(message_rejected) == "quotes were stale"
    assert _corroborates(email_copy, slack_errored)
    assert _corroborates(email_copy, message_rejected)


def test_channel_error_verbs_regression_guards():
    # Regression guard: a genuine ORDER rejection reason (no channel word
    # in the cue's prefix, so _REASON_CUE_CHANNEL_MENTION never matches)
    # is unaffected, and the pre-existing "failed" form still works.
    record = AlertRecord(
        alert_id="a",
        source="gmail_draft",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT Gate 4",
        body="Aborted because the order was rejected",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record) == "the order was rejected"
    record2 = AlertRecord(
        alert_id="b",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Slack post failed because the webhook timed out.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(record2) == "quotes were stale"


def test_load_alert_files_finds_files_nested_below_an_alerts_directory(tmp_path):
    # Codex review: "Include descendants of discovered alert directories".
    # An offline or archived export can partition alerts by day beneath
    # an alerts/ directory -- morning_dca/alerts/2026-08-26/<file>.txt --
    # and checking only the file's IMMEDIATE parent for "alerts" rejected
    # every file one level deeper, silently omitting it from a scan
    # pointed at the cron-tracking root.
    nested = tmp_path / "morning_dca" / "alerts" / "2026-08-26"
    nested.mkdir(parents=True)
    (nested / "2026-08-26-1030-gate5.txt").write_text("ABORT 1030 ET Gate 5", encoding="utf-8")
    records = load_alert_files(tmp_path)
    assert [r.alert_id for r in records] == ["morning_dca/alerts/2026-08-26/2026-08-26-1030-gate5.txt"]


def test_nested_alerts_directory_regression_guards(tmp_path):
    # Regression guard: a root-level diagnostic file with no alerts/
    # ancestor anywhere in its path is still excluded, and the
    # pre-existing shallow (one-level) alerts/ placement still works.
    (tmp_path / "diagnostics").mkdir()
    (tmp_path / "diagnostics" / "run_notes.txt").write_text(
        "Nothing alert-shaped here, just Gate 5 mentioned in passing.", encoding="utf-8"
    )
    shallow_alerts = tmp_path / "midday_dca" / "alerts"
    shallow_alerts.mkdir(parents=True)
    (shallow_alerts / "2026-08-26-1230-gate5.txt").write_text("ABORT 1230 ET Gate 5", encoding="utf-8")
    records = load_alert_files(tmp_path)
    assert [r.alert_id for r in records] == ["midday_dca/alerts/2026-08-26-1230-gate5.txt"]


# --- round 63 ------------------------------------------------------------


def test_detect_verdict_keeps_current_outcome_before_prior_event_cause():
    # Codex review: "Keep current outcomes before prior-event causes".
    # _PRIOR_EVENT_CLAUSE_SPLIT did not split on "because", so "This run
    # ABORTED because the previous run left stale state" had the causal
    # explanation's own "previous run" reach BACKWARD across "because" and
    # mark the CURRENT run's own stated ABORTED as historical too.
    assert detect_verdict("", "This run ABORTED because the previous run left stale state") == "ABORT"


def test_current_outcome_before_prior_event_cause_regression_guards():
    # Regression guard: the pre-existing worked example for this exact
    # splitter (a contrast BEFORE the comma correctly marks GATE5_DISABLED
    # as current, not historical) is unaffected.
    body = "Unlike the previous run, this run is GATE5_DISABLED because no entry point is available"
    assert detect_gate("", body) == "GATE5_DISABLED"
    assert detect_verdict(body, "") == "GATE5_DISABLED"


def test_detect_gate_ignores_informational_reason_code_mentions():
    # Codex review: "Ignore informational reason-code mentions". A reason
    # code used as a noun-phrase modifier for a config/routing object --
    # "Unable to load the GATE5_DISABLED routing configuration" -- is not
    # a classification of this run's own outcome, but nothing screened it
    # out: "Unable" is not one of the _REASON_NEGATION_* cues, and there
    # is no prior-event marker at all. It outranked the alert's own
    # explicit "Failing gate: Gate 2" label.
    subject = "1030 ET | Gate 2 ABORT"
    body = "Failing gate: Gate 2. Unable to load the GATE5_DISABLED routing configuration."
    assert detect_gate(subject, body) == "Gate 2"
    assert detect_verdict(subject, body) == "ABORT"


def test_detect_gate_ignores_reason_codes_modifying_configuration_objects():
    subject = "1030 ET | Gate 2 ABORT"
    body = "Failing gate: Gate 2. Unable to load the GATE5_DISABLED fallback routing configuration."
    assert detect_gate(subject, body) == "Gate 2"
    assert detect_verdict(subject, body) == "ABORT"


def test_informational_reason_code_mentions_regression_guards():
    # Regression guard: a genuine reason-code classification (no
    # config/routing noun immediately after the code) is unaffected.
    assert detect_gate("Verdict: GATE5_DISABLED", "Failing gate: Gate 5") == "GATE5_DISABLED"
    assert detect_verdict("Verdict: GATE5_DISABLED", "Failing gate: Gate 5") == "GATE5_DISABLED"


def test_reason_code_configuration_modifier_regression_guards():
    body = "Verdict: GATE5_DISABLED because the entry point is not available"
    assert detect_gate("slot alert", body) == "GATE5_DISABLED"
    assert detect_verdict("slot alert", body) == "GATE5_DISABLED"


def test_reason_code_causal_configuration_text_is_a_verdict():
    body = "Verdict: GATE5_DISABLED because routing configuration is missing"
    assert detect_gate("slot alert", body) == "GATE5_DISABLED"
    assert detect_verdict("slot alert", body) == "GATE5_DISABLED"


def test_reason_signature_excludes_blocked_channel_failures():
    # Codex review: "Exclude blocked channel failures from incident
    # reasons". "was/were blocked" is a third ordinary channel-error
    # outcome (alongside "failed"/"errored"/"was rejected") that bypassed
    # the delivery-cause screen: "Slack post was blocked because the
    # webhook refused it" still won over the shared incident reason.
    email_copy = AlertRecord(
        alert_id="a",
        source="gmail_message",
        timestamp_utc=parse_timestamp("2026-08-26T18:36:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    slack_blocked = AlertRecord(
        alert_id="b",
        source="alert_file",
        timestamp_utc=parse_timestamp("2026-08-26T18:37:00Z"),
        subject="ABORT 1030 ET Gate 4",
        body="Quotes were stale; aborted. Slack post was blocked because the webhook refused it.",
        slot="1030",
        gate="Gate 4",
        verdict="ABORT",
    )
    assert _reason_signature(slack_blocked) == "quotes were stale"
    assert _corroborates(email_copy, slack_blocked)


def test_states_email_failure_recognizes_rejected_email_delivery_outcomes():
    # Codex review: "Recognize rejected email-delivery outcomes". Three
    # ordinary non-delivery statements -- "was rejected", "was blocked",
    # "errored" -- named neither "could not/never be sent" nor
    # "failed"/"delivery failed", so states_email_failure() returned False
    # on explicit non-delivery evidence.
    assert states_email_failure("Email delivery was rejected by the provider")
    assert states_email_failure("Email send was blocked")
    assert states_email_failure("Email send errored")


def test_states_email_failure_recognizes_email_delivery_success_subjects():
    # Codex review: "Recognize email-delivery success subjects". "email
    # delivery" is a two-word subject just as common as the bare stems
    # ("email"/"send"/"resend"/"retry") already handled, and the subject
    # and its outcome verb were required directly adjacent, so "email
    # delivery succeeded"/"email delivery was successful" matched nothing.
    assert not states_email_failure("Email failed initially, but email delivery succeeded")
    assert not states_email_failure("Email failed initially, but email delivery was successful")


def test_email_delivery_wording_regression_guards():
    # Regression guard: the pre-existing bare-stem success/failure forms
    # and a genuine unresolved failure are all unaffected.
    assert not states_email_failure("Email failed initially, but the email was eventually sent")
    assert states_email_failure("Email send failed.")
    assert states_email_failure("Email send failed, and the retry email was never sent")


def test_slack_route_prefers_the_route_actually_used():
    # Codex review: "Prefer the Slack route actually used". A clause can
    # name BOTH permitted webhook env vars without both being the route
    # actually used -- "... via DCA_DRIFT_SLACK_WEBHOOK with
    # DCA_ALERT_SLACK_WEBHOOK unset" states which var was used (via) and
    # which was merely absent, but checking DCA_ALERT_SLACK_WEBHOOK first,
    # unconditionally, validated against the WRONG route's topic
    # requirement and flagged a supported drift delivery as disabled.
    body = "Posted the drift alert to Slack via DCA_DRIFT_SLACK_WEBHOOK with DCA_ALERT_SLACK_WEBHOOK unset"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_bare_slack_option_does_not_exempt_a_routine_alert():
    body = "Posted the Gate 5 alert to Slack using unrelated_tool --slack"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_drift_command_with_slack_option_remains_permitted():
    body = "Posted the drift finding to Slack with scripts/drift_detection.py --slack"
    codes = {c for c, _ in detect_channel_warnings(body, source="alert_file")}
    assert "SLACK_DISABLED" not in codes


def test_slack_route_preference_regression_guards():
    # Regression guard: a single-mention clause (no "via"/"using"/
    # "through") still falls back to the pre-existing disambiguation, and
    # an actually-unsupported route via the exception-only webhook is
    # still flagged.
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the Gate 5 alert to Slack via DCA_ALERT_SLACK_WEBHOOK", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" in codes
    codes = {
        c
        for c, _ in detect_channel_warnings(
            "Posted the drift finding to Slack and it used DCA_DRIFT_SLACK_WEBHOOK", source="alert_file"
        )
    }
    assert "SLACK_DISABLED" not in codes


def test_recognizes_persisted_writes_to_ignored_alert_paths():
    # Codex review: "Recognize persisted writes to ignored alert paths".
    # "Persisted"/"Logged"/"Copied"/"Emitted" all state the same durable-
    # write outcome as "wrote"/"saved"/"stored"/"created"/"appended", but
    # none of those were recognized by _PATH_WRITE_CLAIM.
    for verb in ("Persisted", "Logged", "Copied", "Emitted"):
        body = f"{verb} the alert to artifacts/alerts/x.txt"
        codes = {c for c, _ in detect_channel_warnings(body, source="gmail_message")}
        assert "GITIGNORED_ALERT_PATH" in codes, body


def test_persisted_writes_regression_guards():
    # Regression guard: the pre-existing no-write-claim exemption (a
    # purely informational mention of the gitignored path) is unaffected.
    body = "the bare artifacts/alerts path is gitignored; used the durable stream_root path instead."
    codes = {c for c, _ in detect_channel_warnings(body, source="gmail_message")}
    assert "GITIGNORED_ALERT_PATH" not in codes


def test_recognizes_successful_slack_delivery_predicates():
    # Codex review: "Recognize successful Slack delivery predicates".
    # "was successful"/"succeeded" state genuine Slack delivery outcomes
    # that _SLACK_DELIVERY_OUTCOME did not recognize, so a routine alert's
    # own Slack claim read as purely informational and was exempted.
    codes = {c for c, _ in detect_channel_warnings("The Slack notification was successful", source="alert_file")}
    assert "SLACK_DISABLED" in codes
    codes = {c for c, _ in detect_channel_warnings("The Slack webhook call succeeded", source="alert_file")}
    assert "SLACK_DISABLED" in codes


def test_successful_slack_delivery_predicates_regression_guards():
    # Regression guard: a purely informational Slack mention (no delivery
    # outcome verb of any kind in its own clause) is still exempted.
    codes = {c for c, _ in detect_channel_warnings("Slack alert configuration status: ok", source="alert_file")}
    assert "SLACK_DISABLED" not in codes
