"""``scripts.abort_markers.read_markers_strict`` -- the one fail-closed marker reader (2026-11-12).

The kill-switch gap proof and the afternoon backfill each used to carry their own copy of these rules;
both now call this reader, so pinning it here pins both. The lenient ``read_abort_markers`` (watchdog
triage) is tested in ``test_record_evidence_free_abort.py`` and must keep skipping what this refuses.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import abort_markers as am
from scripts import backfill_afternoon_evidence as bf
from scripts import kill_switch_gap_proof as gap

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 9)


def _marker(**overrides) -> dict:
    return {"fired_at_utc": "2026-11-10T00:24:28Z", "gate": "gate1", "kind": "evidence_free_abort",
            "reason_code": "GATE1_OFF_WINDOW", "slot_id": "1430", "trading_day": "2026-11-09", **overrides}


def _write(root: Path, *lines, raw: bytes | None = None) -> Path:
    path = am.marker_path(root, DAY, "1430")
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")
    return path


def test_no_file_is_none_and_an_empty_file_is_an_empty_list(tmp_path):
    assert am.read_markers_strict(tmp_path, DAY, "1430") is None
    _write(tmp_path, raw=b"\n  \n")
    assert am.read_markers_strict(tmp_path, DAY, "1430") == []


def test_well_formed_lines_are_returned_in_order_with_an_et_fire_time(tmp_path):
    _write(tmp_path, _marker(), "", _marker(gate="gate7", reason_code="GATE7_X"))

    markers = am.read_markers_strict(tmp_path, DAY, "1430")

    assert [(m.line, m.gate, m.reason_code) for m in markers] == [(1, "gate1", "GATE1_OFF_WINDOW"),
                                                                    (2, "gate7", "GATE7_X")]
    assert markers[0].fired_at_et == datetime(2026, 11, 9, 19, 24, 28, tzinfo=ET)
    assert markers[0].record["kind"] == "evidence_free_abort"


BAD = {
    "malformed": ("{not json", "line 2 is malformed"),
    "duplicate_key": ('{"kind": "evidence_free_abort", "kind": "evidence_free_abort"}', "line 2 is malformed"),
    "not_an_object": ("[1, 2]", "line 2 is not an evidence-free abort marker"),
    "wrong_kind": (_marker(kind="failed_start"), "line 2 is not an evidence-free abort marker"),
    "other_slot": (_marker(slot_id="1530"), "line 2 is for another slot or day"),
    "other_day": (_marker(trading_day="2026-11-10"), "line 2 is for another slot or day"),
    "naive_time": (_marker(fired_at_utc="2026-11-10T00:24:28"), "line 2 has no timezone-aware fire time"),
    "missing_time": (_marker(fired_at_utc=None), "line 2 has no timezone-aware fire time"),
    "garbage_time": (_marker(fired_at_utc="yesterday"), "line 2 has no timezone-aware fire time"),
}


@pytest.mark.parametrize("case", BAD, ids=list(BAD))
def test_any_bad_line_refuses_the_whole_file(case, tmp_path):
    bad, message = BAD[case]
    _write(tmp_path, _marker(), bad)

    with pytest.raises(am.MarkerFileInvalid, match=message):
        am.read_markers_strict(tmp_path, DAY, "1430")
    # The lenient triage reader still skips the same line and keeps the good one.
    assert len(am.read_abort_markers(tmp_path, DAY, "1430")) == 1


def test_undecodable_file_refuses(tmp_path):
    _write(tmp_path, raw=b"\xff\xfe\x00bad")
    with pytest.raises(am.MarkerFileInvalid, match="unreadable"):
        am.read_markers_strict(tmp_path, DAY, "1430")


@pytest.mark.parametrize("case", BAD, ids=list(BAD))
def test_both_callers_refuse_what_the_shared_reader_refuses(case, tmp_path):
    """The point of the consolidation: one bad line blocks the gap proof AND refuses the backfill."""
    _write(tmp_path, _marker(), BAD[case][0])

    ok, detail = gap.marker_precondition(tmp_path, DAY, "14:30")
    assert not ok and BAD[case][1] in detail
    with pytest.raises(bf.BackfillRefused, match=BAD[case][1]):
        bf.read_abort_marker(tmp_path, DAY, "1430")
