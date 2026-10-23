"""Tests for dca_provenance: sidecar content, hashing, tamper detection, input rejection."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

import dca_provenance as prov

FETCHED = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)


def _data_file(tmp_path, body=b"Date,Close\n2026-10-21,1.0\n"):
    path = tmp_path / "TICK.csv"
    path.write_bytes(body)
    return path


def test_sha256_file_matches_hashlib_and_streams_in_small_chunks(tmp_path):
    path = _data_file(tmp_path, b"x" * 5000)
    expected = hashlib.sha256(b"x" * 5000).hexdigest()
    assert prov.sha256_file(path) == expected
    assert prov.sha256_file(path, chunk_size=7) == expected


def test_sidecar_name_never_ends_in_csv(tmp_path):
    path = _data_file(tmp_path)
    sidecar = prov.sidecar_path(path)
    assert sidecar.name == "TICK.csv.provenance.json"
    assert not sidecar.name.endswith(".csv")


def test_write_sidecar_records_hash_size_source_and_range(tmp_path):
    path = _data_file(tmp_path)
    out = prov.write_sidecar(
        path,
        source="yahoo_finance_yfinance",
        fetched_at=FETCHED,
        rows=1,
        first_date=date(2026, 10, 21),
        last_date="2026-10-21",
        extra={"tickers": ["A"]},
    )
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["schema_version"] == prov.SCHEMA_VERSION
    assert record["file"] == "TICK.csv"
    assert record["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["size_bytes"] == path.stat().st_size
    assert record["source"] == "yahoo_finance_yfinance"
    assert record["fetched_at"] == FETCHED.isoformat()
    assert record["rows"] == 1
    assert record["first_date"] == "2026-10-21"
    assert record["last_date"] == "2026-10-21"
    assert record["extra"] == {"tickers": ["A"]}


def test_record_is_reproducible_for_the_same_inputs(tmp_path):
    path = _data_file(tmp_path)
    kwargs = {"source": "s", "fetched_at": FETCHED}
    assert prov.build_record(path, **kwargs) == prov.build_record(path, **kwargs)


def test_matches_data_file_detects_a_changed_file(tmp_path):
    path = _data_file(tmp_path)
    prov.write_sidecar(path, source="s", fetched_at=FETCHED)
    assert prov.matches_data_file(path)
    path.write_bytes(b"tampered")
    assert not prov.matches_data_file(path)


def test_read_sidecar_degrades_to_none_when_missing_or_torn(tmp_path):
    path = _data_file(tmp_path)
    assert prov.read_sidecar(path) is None
    assert not prov.matches_data_file(path)
    prov.sidecar_path(path).write_text("{not json", encoding="utf-8")
    assert prov.read_sidecar(path) is None
    prov.sidecar_path(path).write_text("[1, 2]", encoding="utf-8")
    assert prov.read_sidecar(path) is None


def test_naive_fetched_at_is_rejected(tmp_path):
    path = _data_file(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        prov.build_record(path, source="s", fetched_at=datetime(2026, 10, 22, 21, 0))


@pytest.mark.parametrize("bad_source", ["", "   ", None])
def test_blank_source_is_rejected(tmp_path, bad_source):
    path = _data_file(tmp_path)
    with pytest.raises(ValueError, match="source"):
        prov.build_record(path, source=bad_source, fetched_at=FETCHED)


def test_blank_fetched_at_string_and_bad_date_types_are_rejected(tmp_path):
    path = _data_file(tmp_path)
    with pytest.raises(ValueError):
        prov.build_record(path, source="s", fetched_at="  ")
    with pytest.raises(ValueError, match="first_date"):
        prov.build_record(path, source="s", fetched_at=FETCHED, first_date=20261021)


def test_missing_data_file_raises_rather_than_writing_an_empty_hash(tmp_path):
    with pytest.raises(OSError):
        prov.write_sidecar(tmp_path / "absent.csv", source="s", fetched_at=FETCHED)
    assert not (tmp_path / "absent.csv.provenance.json").exists()
