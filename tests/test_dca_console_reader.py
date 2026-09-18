"""The shared bounded reader: confinement below one root, explicit limits, labels instead of paths."""

import json
import os

import pytest

from dca_console_reader import MAX_BYTES, MAX_FILES, BoundedReader, RootSpec, iso_date, root_label, roots_from_settings


def test_root_label_hides_the_absolute_path_and_is_stable(tmp_path):
    label = root_label(tmp_path / "cron_tracking")
    assert label.startswith("cron_tracking (") and label.endswith(")")
    assert str(tmp_path) not in label
    assert label == root_label(tmp_path / "cron_tracking")
    assert label != root_label(tmp_path / "other" / "cron_tracking")
    assert root_label("/") == "/ (" + root_label("/").split("(")[1]


def test_iso_date_accepts_only_ten_character_dates():
    assert iso_date("2026-09-08") == "2026-09-08"
    assert iso_date("2026-02-30") is None
    assert iso_date("2026-9-8") is None
    assert iso_date(20260908) is None


@pytest.mark.skipif(os.name != "posix", reason="symlink escape needs POSIX symlinks")
def test_reader_refuses_paths_that_resolve_outside_the_root(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"secret": True}))
    (root / "escape.json").symlink_to(outside)
    warnings = []
    reader = BoundedReader(root, warnings)
    assert reader.text("escape.json") is None
    assert reader.json("../outside.json") == {}
    assert reader.confined("../outside.json") is None
    assert reader.modified_at("escape.json") is None
    assert any("outside" in warning for warning in warnings)
    # Duplicate warnings collapse so a scan over many files stays readable.
    assert len(warnings) == len(set(warnings))


def test_reader_enforces_the_byte_limit_and_reports_corrupt_json(tmp_path):
    (tmp_path / "big.json").write_text("x" * 40)
    (tmp_path / "bad.json").write_text("{")
    (tmp_path / "list.json").write_text("[]")
    warnings = []
    reader = BoundedReader(tmp_path, warnings, max_bytes=16)
    assert reader.text("big.json") is None
    assert reader.json("bad.json") == {}
    assert reader.json("list.json") == {}
    assert reader.json("missing.json") == {}
    assert any("read limit" in warning for warning in warnings)
    assert any("corrupt" in warning for warning in warnings)
    assert any("unsupported shape" in warning for warning in warnings)
    assert BoundedReader(tmp_path, []).max_bytes == MAX_BYTES
    assert BoundedReader(tmp_path, []).max_files == MAX_FILES


def test_entries_and_latest_in_are_bounded_and_relative(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    for name in ("2026-09-01_summary.json", "2026-09-03_summary.json", "notes.txt", "2026-09-02_other.csv"):
        (daily / name).write_text("{}")
    warnings = []
    reader = BoundedReader(tmp_path, warnings, max_files=2)
    assert reader.latest_in("daily", "_summary.json") is None or warnings  # capped scan warns
    assert any("scan limit" in warning for warning in warnings)
    full = BoundedReader(tmp_path, [])
    assert str(full.latest_in("daily", "_summary.json")) == "daily/2026-09-03_summary.json"
    assert full.latest_in("missing", "_summary.json") is None
    listed = full.entries("daily")
    assert {str(path) for path in listed} == {
        "daily/2026-09-01_summary.json", "daily/2026-09-03_summary.json", "daily/notes.txt", "daily/2026-09-02_other.csv",
    }
    assert all(not path.is_absolute() for path in listed)
    assert full.entries("missing") == []
    assert len(full.entries("daily", limit=1)) == 1


def test_root_spec_carries_a_label_and_the_console_settings_order(tmp_path):
    from types import SimpleNamespace

    primary = tmp_path / "cron_tracking"
    extra = tmp_path / "artifacts"
    spec = RootSpec.of(primary)
    assert spec.label == root_label(primary) and spec.kind == "primary" and spec.path == primary
    assert spec.reader([]).root == primary
    roots = roots_from_settings(SimpleNamespace(root=primary, secondary_roots=(extra,)))
    assert [root.kind for root in roots] == ["primary", "secondary"]
    assert roots[1].label == root_label(extra)
    assert str(tmp_path) not in "".join(root.label for root in roots)
