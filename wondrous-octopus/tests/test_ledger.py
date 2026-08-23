"""Tests for ledger.py: gzip-member appends, crash truncation, tamper refusal."""

from __future__ import annotations

import gzip
import os

import pytest

from ledger import Ledger, LedgerError


def _record(*, kind="forecast", model="persistence", ticker="AAA", origin="2021-06-01", **extra):
    return {
        "schema_version": 1,
        "record_type": kind,
        "model": model,
        "ticker": ticker,
        "origin_date": origin,
        "target_date": "2021-06-02",
        "seed": 0,
        **extra,
    }


class TestAppendAndRead:
    def test_roundtrip_and_sharding(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append(
            [
                _record(),
                _record(kind="score"),
                _record(origin="2022-03-01"),
                _record(model="gbm_quantile"),
            ]
        )
        assert [r["origin_date"] for r in ledger.iter_records(model="persistence", kind="forecast")] == [
            "2021-06-01",
            "2022-03-01",
        ]
        assert len(list(ledger.iter_records(model="persistence", kind="score"))) == 1
        assert len(list(ledger.iter_records(model="gbm_quantile", kind="forecast"))) == 1
        # Year-scoped read.
        assert len(list(ledger.iter_records(model="persistence", kind="forecast", year="2022"))) == 1
        assert ledger.models() == ["gbm_quantile", "persistence"]

    def test_each_append_is_a_valid_gzip_member(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record(payload="first")])
        ledger.append([_record(payload="second")])
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        with gzip.open(shard, "rt") as handle:  # concatenated members read transparently
            lines = handle.readlines()
        assert len(lines) == 2

    def test_append_fsyncs_each_written_shard(self, tmp_path, monkeypatch):
        # flush() alone only moves bytes into the OS page cache, not onto
        # stable storage. checkpoint() fingerprints a shard's post-append
        # size/hash and PUBLISHES that as durable moments later -- if the
        # host loses power before the page cache is written back, the
        # shard on disk can end up shorter than the checkpoint already
        # recorded, and the next resume() sees "missing or shorter" and
        # permanently REFUSES instead of rolling back cleanly, contradicting
        # this module's own crash-consistency contract (Codex review:
        # "Sync ledger bytes before publishing their checkpoint").
        synced_fds = []
        original_fsync = os.fsync

        def _spy_fsync(fd):
            synced_fds.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", _spy_fsync)
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        assert synced_fds  # os.fsync() was actually called on the written shard
        ledger.append([_record(model="gbm_quantile")])
        assert len(synced_fds) == 2  # one fsync per shard-group write

    def test_unknown_record_type_is_refused(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        with pytest.raises(LedgerError, match="unknown record_type"):
            ledger.append([_record(kind="mystery")])

    def test_create_refuses_existing_ledger(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        with pytest.raises(LedgerError, match="already exists"):
            Ledger.create(tmp_path)


class TestResumeContract:
    def test_crash_after_append_is_truncated_back_to_checkpoint(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record(payload="checkpointed")])
        recorded = ledger.fingerprints()
        # Crash simulation: another append lands, but no checkpoint follows.
        ledger.append([_record(payload="lost-to-the-crash")])
        resumed = Ledger.resume(tmp_path, recorded)
        rows = list(resumed.iter_records(model="persistence", kind="forecast"))
        assert [row.get("payload") for row in rows] == ["checkpointed"]
        # The resumed writer continues appending cleanly from the boundary.
        resumed.append([_record(payload="regenerated")])
        rows = list(resumed.iter_records(model="persistence", kind="forecast"))
        assert [row.get("payload") for row in rows] == ["checkpointed", "regenerated"]

    def test_shard_created_after_checkpoint_is_deleted_on_resume(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        ledger.append([_record(model="gbm_quantile")])  # brand-new shard, post-checkpoint
        resumed = Ledger.resume(tmp_path, recorded)
        assert list(resumed.iter_records(model="gbm_quantile", kind="forecast")) == []

    def test_tampered_shard_is_refused(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        shard.write_bytes(bytes(raw))
        with pytest.raises(LedgerError, match="does not match checkpoint"):
            Ledger.resume(tmp_path, recorded)

    def test_missing_shard_is_refused(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        (tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz").unlink()
        with pytest.raises(LedgerError, match="missing or shorter"):
            Ledger.resume(tmp_path, recorded)


class TestVerifyShardsIsReadOnly:
    def test_tampered_shard_is_refused_without_mutating_it(self, tmp_path):
        # Read-only counterpart to Ledger.resume() -- a caller whose own
        # contract forbids any mutation (daily_update.py's --dry-run
        # preview) must still get the SAME tampered-shard refusal
        # (Codex review: "Verify checkpoint integrity during daily dry
        # runs"), but the shard bytes themselves must be untouched
        # afterward, unlike resume()'s truncate/delete recovery.
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        shard.write_bytes(bytes(raw))
        tampered_bytes = shard.read_bytes()
        with pytest.raises(LedgerError, match="does not match checkpoint"):
            Ledger.verify_shards(tmp_path, recorded)
        assert shard.read_bytes() == tampered_bytes  # untouched, not "recovered"

    def test_missing_shard_is_refused(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        (tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz").unlink()
        with pytest.raises(LedgerError, match="missing or shorter"):
            Ledger.verify_shards(tmp_path, recorded)

    def test_an_oversized_shard_passes_but_is_not_truncated(self, tmp_path):
        # resume() truncates bytes past the checkpointed size (they belong
        # to un-checkpointed steps the resumed run re-executes);
        # verify_shards() must accept the SAME shard (the checkpointed
        # PREFIX still matches) without performing that truncation --
        # this preview must not touch the file at all.
        ledger = Ledger.create(tmp_path)
        ledger.append([_record(payload="checkpointed")])
        recorded = ledger.fingerprints()
        ledger.append([_record(payload="not-yet-checkpointed")])
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        oversized_bytes = shard.read_bytes()
        Ledger.verify_shards(tmp_path, recorded)  # must not raise
        assert shard.read_bytes() == oversized_bytes  # still oversized -- not truncated

    def test_an_unrecorded_shard_is_left_in_place(self, tmp_path):
        # resume() deletes a shard created entirely after the last
        # checkpoint; verify_shards() only checks the RECORDED shards
        # (there is nothing to compare an unrecorded one against) and
        # must never delete anything.
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        ledger.append([_record(model="gbm_quantile")])  # brand-new shard, post-checkpoint
        new_shard = tmp_path / "ledger" / "gbm_quantile" / "forecast_2021.jsonl.gz"
        assert new_shard.exists()
        Ledger.verify_shards(tmp_path, recorded)  # must not raise
        assert new_shard.exists()  # still there -- not deleted

    def test_fingerprints_are_deterministic_across_equal_runs(self, tmp_path):
        first = Ledger.create(tmp_path / "a")
        second = Ledger.create(tmp_path / "b")
        for ledger in (first, second):
            ledger.append([_record(payload="same")])
        assert first.fingerprints() == second.fingerprints()

    def test_resume_matches_shards_despite_a_windows_style_relative_path(self, tmp_path, monkeypatch):
        # _shard_relpath() (the write-time key baked into every checkpoint)
        # always joins with a forward slash. On a real Windows filesystem,
        # str(path.relative_to(root)) instead uses a backslash, so every
        # recorded shard used to look "missing" here and refuse the whole
        # resume (Codex review: "Use POSIX separators for ledger
        # fingerprint keys"). This sandbox is POSIX-only, so simulate the
        # platform difference: wrap relative_to()'s result for ledger
        # shard paths only, giving it a backslash str() the same way a
        # WindowsPath would, while as_posix() still gives forward slashes.
        import pathlib

        class _FakeWindowsRelPath:
            def __init__(self, posix_str):
                self._posix = posix_str

            def __str__(self):
                return self._posix.replace("/", "\\")

            def as_posix(self):
                return self._posix

        ledger = Ledger.create(tmp_path)
        ledger.append([_record(payload="checkpointed")])
        recorded = ledger.fingerprints()

        original_relative_to = pathlib.Path.relative_to

        def _fake_relative_to(self, other, **kwargs):
            real = original_relative_to(self, other, **kwargs)
            if str(real).endswith(".jsonl.gz"):
                return _FakeWindowsRelPath(real.as_posix())
            return real

        monkeypatch.setattr(pathlib.Path, "relative_to", _fake_relative_to)
        resumed = Ledger.resume(tmp_path, recorded)
        rows = list(resumed.iter_records(model="persistence", kind="forecast"))
        assert [row.get("payload") for row in rows] == ["checkpointed"]


class TestReadOnlyFingerprintedReads:
    """iter_records() with read_fingerprints set (the --report-only /
    inspect_run.py path) must apply the SAME size + SHA-256 validation
    resume() does, not just use the checkpoint as a byte limit -- otherwise
    a shard tampered or shortened within the checkpointed range is read
    and silently accepted here even though the next resume() would refuse
    this exact ledger (Codex review regression: "Verify checkpointed
    shards before reporting")."""

    def test_tampered_shard_is_refused_on_read(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        shard.write_bytes(bytes(raw))
        reader = Ledger(tmp_path, read_fingerprints=recorded)
        with pytest.raises(LedgerError, match="does not match checkpoint"):
            list(reader.iter_records(model="persistence", kind="forecast"))

    def test_shortened_shard_is_refused_on_read(self, tmp_path):
        ledger = Ledger.create(tmp_path)
        ledger.append([_record()])
        recorded = ledger.fingerprints()
        shard = tmp_path / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
        original = shard.read_bytes()
        shard.write_bytes(original[: len(original) // 2])
        reader = Ledger(tmp_path, read_fingerprints=recorded)
        with pytest.raises(LedgerError, match="shorter than the checkpoint"):
            list(reader.iter_records(model="persistence", kind="forecast"))

    def test_untampered_shard_still_reads_normally(self, tmp_path):
        # Sanity: the new validation doesn't break the ordinary, unmodified
        # case it's meant to leave alone.
        ledger = Ledger.create(tmp_path)
        ledger.append([_record(payload="fine")])
        recorded = ledger.fingerprints()
        reader = Ledger(tmp_path, read_fingerprints=recorded)
        rows = list(reader.iter_records(model="persistence", kind="forecast"))
        assert [row.get("payload") for row in rows] == ["fine"]
