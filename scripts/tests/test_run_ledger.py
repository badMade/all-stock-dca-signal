"""Tests for the chronological run ledger."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scripts import file_lock, run_ledger
from scripts.run_ledger import LedgerEvent, RunLedger


def _event(event_type, **kw):
    base = dict(
        event_type=event_type,
        trading_day="2026-07-23",
        stream="midday",
        correlation_id="dca-2026-07-23-midday-1230et",
    )
    base.update(kw)
    return LedgerEvent(**base)


def test_append_assigns_monotonic_seq_and_ts(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    e1 = ledger.append(_event(run_ledger.SCHEDULED_RUN), ts="2026-07-23T16:30:00Z")
    e2 = ledger.append(_event(run_ledger.ORDER_ATTEMPT), ts="2026-07-23T16:30:05Z")
    assert e1.seq == 1 and e2.seq == 2
    assert e1.ts == "2026-07-23T16:30:00Z"


def test_append_is_jsonl_one_line_per_event(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = RunLedger(path)
    ledger.append(_event(run_ledger.SCHEDULED_RUN))
    ledger.append(_event(run_ledger.FILL, detail={"symbol": "TXN", "amount": 1.0}))
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line is valid JSON


def test_seq_continues_across_reopen(tmp_path):
    path = tmp_path / "ledger.jsonl"
    RunLedger(path).append(_event(run_ledger.SCHEDULED_RUN))
    # A fresh RunLedger over the same file must continue the sequence.
    e = RunLedger(path).append(_event(run_ledger.ARTIFACT_PUBLISHED))
    assert e.seq == 2


def test_read_all_round_trips_payload(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    ledger.append(_event(run_ledger.FILL, run_id="midday_live_20260723_1230ET", detail={"symbol": "TXN"}))
    events = ledger.read_all()
    assert len(events) == 1
    assert events[0].event_type == run_ledger.FILL
    assert events[0].detail == {"symbol": "TXN"}


def test_filter_by_stream_and_event_type(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    ledger.append(_event(run_ledger.SCHEDULED_RUN, stream="midday"))
    ledger.append(_event(run_ledger.SCHEDULED_RUN, stream="morning"))
    ledger.append(_event(run_ledger.FILL, stream="midday"))
    midday = ledger.filter(stream="midday")
    assert len(midday) == 2
    fills = ledger.filter(event_type=run_ledger.FILL)
    assert len(fills) == 1 and fills[0].stream == "midday"


def test_unknown_event_type_rejected(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(ValueError):
        ledger.append(_event("not_a_real_event"))


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("{not json}\n" + json.dumps({"seq": 5, "event_type": "fill"}) + "\n")
    ledger = RunLedger(path)
    # Next seq must be 6 (max valid seq + 1), ignoring the malformed line.
    e = ledger.append(_event(run_ledger.SCHEDULED_RUN))
    assert e.seq == 6


def test_append_after_crash_truncated_line_keeps_records_separate(tmp_path):
    path = tmp_path / "ledger.jsonl"
    # Simulate a crash: a complete line, then a truncated final line with no newline.
    path.write_text(json.dumps({"seq": 1, "event_type": "fill"}) + "\n" + '{"seq": 2, "event_typ')
    ledger = RunLedger(path)
    ledger.append(_event(run_ledger.SCHEDULED_RUN))
    # The new record must be a separate, valid JSONL line (not merged onto the
    # fragment), so read_all recovers the complete records.
    events = ledger.read_all()
    valid_types = [e.event_type for e in events]
    assert "fill" in valid_types
    assert run_ledger.SCHEDULED_RUN in valid_types
    # And every recovered event parsed cleanly (no merged/garbled line surfaced).
    assert all(e.event_type in run_ledger.EVENT_TYPES or e.event_type == "fill" for e in events)


def test_portable_lock_fallback_appends_and_releases_lockfile(tmp_path, monkeypatch):
    # Force the non-POSIX path: pretend fcntl is unavailable.
    monkeypatch.setattr(file_lock, "fcntl", None)
    path = tmp_path / "ledger.jsonl"
    ledger = RunLedger(path)
    e1 = ledger.append(_event(run_ledger.SCHEDULED_RUN))
    e2 = ledger.append(_event(run_ledger.FILL))
    assert (e1.seq, e2.seq) == (1, 2)
    # The O_EXCL lock-file is released (unlinked) after each append.
    assert not path.with_name(path.name + ".lock").exists()


def test_portable_lock_serializes_concurrent_appends(tmp_path, monkeypatch):
    # Without cross-process serialization, overlapping writers would read the
    # same _next_seq() and assign duplicate sequence numbers.
    monkeypatch.setattr(file_lock, "fcntl", None)
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    workers = 8
    ready = threading.Barrier(workers)

    def worker():
        ready.wait()  # release all writers at once to maximize contention
        ledger.append(_event(run_ledger.ORDER_ATTEMPT))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(e.seq for e in ledger.read_all())
    # No duplicated or skipped sequence numbers despite concurrent writers.
    assert seqs == list(range(1, workers + 1))


def test_append_fails_closed_on_unreadable_existing_ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    ledger = RunLedger(path)
    ledger.append(_event(run_ledger.SCHEDULED_RUN))  # seq 1
    ledger.append(_event(run_ledger.FILL))  # seq 2 -- a populated ledger

    real_open = Path.open

    def flaky_open(self, mode="r", *args, **kwargs):
        # Simulate a write-only ledger: reads raise OSError, appends succeed.
        if self == path and "a" not in mode:
            raise OSError("simulated write-only ledger")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    # The existing ledger cannot be read, so the append must fail closed rather
    # than treat it as empty and allocate a duplicate seq 1.
    with pytest.raises(OSError):
        ledger.append(_event(run_ledger.ORDER_ATTEMPT))


def test_append_rejects_blank_correlation_id(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(ValueError):
        ledger.append(_event(run_ledger.SCHEDULED_RUN, correlation_id=""))
    with pytest.raises(ValueError):
        ledger.append(_event(run_ledger.SCHEDULED_RUN, correlation_id="   "))
    # Nothing was written for the rejected events.
    assert not (tmp_path / "ledger.jsonl").exists() or ledger.read_all() == []


def test_bool_seq_is_not_treated_as_integer_sequence(tmp_path):
    path = tmp_path / "ledger.jsonl"
    # bool is an int subclass; a garbage line {"seq": true} must NOT be read as
    # seq 1, or the next append would collide with the real seq 2 already on file.
    path.write_text(
        json.dumps({"seq": 1, "event_type": "fill"}) + "\n"
        + json.dumps({"seq": 2, "event_type": "fill"}) + "\n"
        + json.dumps({"seq": True, "event_type": "fill"}) + "\n"
    )
    e = RunLedger(path).append(_event(run_ledger.SCHEDULED_RUN))
    assert e.seq == 3  # continues past the bool line to the real max (2)


def test_next_seq_uses_tail_max_not_last_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    # A corrupt/out-of-order final record with a LOWER seq than an earlier one must
    # not move the sequence backward; the next append continues past the real max.
    path.write_text(
        json.dumps({"seq": 20, "event_type": "fill"}) + "\n"
        + json.dumps({"seq": 1, "event_type": "fill"}) + "\n"
    )
    e = RunLedger(path).append(_event(run_ledger.SCHEDULED_RUN))
    assert e.seq == 21


def test_trailing_seqless_line_still_continues_sequence(tmp_path):
    path = tmp_path / "ledger.jsonl"
    # A valid final line without a seq must not abort the tail read to seq 1; the
    # scan continues upward to the real max within the tail buffer.
    path.write_text(
        json.dumps({"seq": 7, "event_type": "fill"}) + "\n"
        + json.dumps({"note": "metadata line, no seq"}) + "\n"
    )
    e = RunLedger(path).append(_event(run_ledger.SCHEDULED_RUN))
    assert e.seq == 8
