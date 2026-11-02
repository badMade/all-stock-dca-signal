"""shadow.py: the one shadow-record writer for co-movement signals 3 to 5.

What these tests pin down (docs/adr-comovement-signals.md, signals 3 to 5 are shadow or
log only):
  * one append-only JSON-lines file per signal, under a directory the caller picks;
  * every line carries as_of, inputs_known_at, signal, payload and a content hash;
  * the writer never writes a suggestion file the SizingAdjustmentReader would read
    (``ml_sizing_adjustment.json``, signal 1 and 2's ``*_suggestions.json``), never
    writes under a ``config/`` directory, and never accepts a record whose inputs were
    known after its as-of date;
  * none of the shadow modules is a SizingSuggester (they may not influence sizing).

All payloads below are toy dictionaries, not market data.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from comovement_signals import (
    CLUSTER_CAP_FILENAME,
    SELLOFF_FILENAME,
    SHADOW_FILENAMES,
    SIGNAL_CHEAPER_LEG_TILT,
    SIGNAL_PAIR_BREAKDOWN,
    SIGNAL_ROTATION_LOG,
    ShadowRecord,
    ShadowResult,
    ShadowWriter,
    content_hash,
    validate_shadow_filename,
    write_shadow_result,
)
from dca_shared_interfaces.forecasting import SizingSuggester

PACKAGE = Path(__file__).resolve().parents[2] / "comovement_signals"
SHADOW_MODULES = ("shadow.py", "pair_breakdown.py", "cheaper_leg_tilt.py", "rotation_log.py")
AS_OF = date(2026, 1, 9)


def _record(signal=SIGNAL_PAIR_BREAKDOWN, payload=None, as_of=AS_OF, known=date(2026, 1, 8)):
    return ShadowRecord(signal=signal, as_of=as_of, inputs_known_at=known, payload=payload or {"k": 1})


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_file_names_are_the_three_spec_names():
    assert SHADOW_FILENAMES == {
        SIGNAL_PAIR_BREAKDOWN: "comovement_pair_breakdown_shadow.jsonl",
        SIGNAL_CHEAPER_LEG_TILT: "comovement_tilt_shadow.jsonl",
        SIGNAL_ROTATION_LOG: "comovement_rotation_log.jsonl",
    }


@pytest.mark.parametrize("name", [
    "ml_sizing_adjustment.json",          # the file SizingAdjustmentReader reads
    CLUSTER_CAP_FILENAME,                 # signal 1's suggestion file
    SELLOFF_FILENAME,                     # signal 2's suggestion file
    "comovement_tilt_suggestions.json",   # anything that looks like a suggestion file
    "comovement_tilt_shadow.json",        # right stem, wrong (non-append) format
    "budgets.json",                       # a config/ file name
    "anything.jsonl",                     # not one of the three
])
def test_refuses_suggestion_config_and_unknown_file_names(name):
    with pytest.raises(ValueError):
        validate_shadow_filename(name)


@pytest.mark.parametrize("name", sorted(SHADOW_FILENAMES.values()))
def test_accepts_only_the_three_shadow_names(name):
    assert validate_shadow_filename(name) == name


@pytest.mark.parametrize("parts", [("config",), ("repo", "config"), ("repo", "config", "sub")])
def test_refuses_a_config_directory(tmp_path, parts):
    with pytest.raises(ValueError):
        ShadowWriter(tmp_path.joinpath(*parts))


def test_unknown_signal_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ShadowWriter(tmp_path).append(_record(signal="cluster_cap"))


def test_record_fields_and_content_hash(tmp_path):
    result = ShadowWriter(tmp_path).append(_record(payload={"pair": ["A1", "B1"], "z": -1.5}))
    assert result.written and result.status == "written"
    (line,) = _lines(tmp_path / SHADOW_FILENAMES[SIGNAL_PAIR_BREAKDOWN])
    for key in ("as_of", "inputs_known_at", "signal", "payload", "content_hash"):
        assert key in line
    assert line["as_of"] == "2026-01-09" and line["inputs_known_at"] == "2026-01-08"
    assert line["signal"] == SIGNAL_PAIR_BREAKDOWN
    assert line["payload"] == {"pair": ["A1", "B1"], "z": -1.5}
    assert line["live_effect"] == "none"
    # The hash is SHA-256 of the canonical JSON (sorted keys, no spaces) of every other
    # field on the line, so a reader can re-derive it and detect an edited line.
    body = {k: v for k, v in line.items() if k != "content_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert line["content_hash"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert content_hash(body) == line["content_hash"]


def test_append_only_preserves_earlier_lines(tmp_path):
    writer = ShadowWriter(tmp_path)
    path = tmp_path / SHADOW_FILENAMES[SIGNAL_PAIR_BREAKDOWN]
    writer.append(_record(payload={"n": 1}))
    first = path.read_bytes()
    writer.append(_record(payload={"n": 2}, as_of=date(2026, 1, 10), known=date(2026, 1, 9)))
    after = path.read_bytes()
    assert after.startswith(first)  # nothing earlier was rewritten
    assert [line["payload"]["n"] for line in _lines(path)] == [1, 2]


def test_identical_record_is_not_appended_twice(tmp_path):
    writer = ShadowWriter(tmp_path)
    assert writer.append(_record()).status == "written"
    again = writer.append(_record())
    assert not again.written and again.status == "duplicate"
    assert len(_lines(tmp_path / SHADOW_FILENAMES[SIGNAL_PAIR_BREAKDOWN])) == 1


def test_each_signal_writes_only_its_own_file(tmp_path):
    writer = ShadowWriter(tmp_path)
    writer.append(_record(signal=SIGNAL_CHEAPER_LEG_TILT))
    # Only the tilt file exists: the other two were not created, and no lock is left.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["comovement_tilt_shadow.jsonl"]
    writer.append(_record(signal=SIGNAL_PAIR_BREAKDOWN))
    writer.append(_record(signal=SIGNAL_ROTATION_LOG))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted(SHADOW_FILENAMES.values())
    assert not any(n.endswith(".json") for n in names)  # nothing the reader would load


def test_other_files_in_the_directory_are_untouched(tmp_path):
    other = tmp_path / "ml_sizing_adjustment.json"
    other.write_text('{"untouched": true}')
    before = other.read_bytes()
    ShadowWriter(tmp_path).append(_record())
    assert other.read_bytes() == before


def test_refuses_inputs_known_after_as_of(tmp_path):
    # Lookahead guard at the writer: a record built from data dated after its own as-of
    # date is refused, whatever produced it.
    with pytest.raises(ValueError):
        ShadowWriter(tmp_path).append(_record(as_of=date(2026, 1, 9), known=date(2026, 1, 10)))
    assert list(tmp_path.iterdir()) == []


def test_refuses_non_finite_payload(tmp_path):
    with pytest.raises(ValueError):
        ShadowWriter(tmp_path).append(_record(payload={"z": float("nan")}))
    assert list(tmp_path.iterdir()) == []


def test_failed_result_writes_nothing(tmp_path):
    failed = ShadowResult(signal=SIGNAL_ROTATION_LOG, as_of=AS_OF, ok=False, reason="stale_prices")
    assert write_shadow_result(failed, ShadowWriter(tmp_path)) == []
    assert list(tmp_path.iterdir()) == []


def test_ok_result_writes_each_record(tmp_path):
    ok = ShadowResult(signal=SIGNAL_ROTATION_LOG, as_of=AS_OF, ok=True, reason="ok",
                      records=(_record(signal=SIGNAL_ROTATION_LOG, payload={"v": 1}),
                               _record(signal=SIGNAL_ROTATION_LOG, payload={"v": 2})))
    results = write_shadow_result(ok, ShadowWriter(tmp_path))
    assert [r.status for r in results] == ["written", "written"]
    assert len(_lines(tmp_path / SHADOW_FILENAMES[SIGNAL_ROTATION_LOG])) == 2


def test_result_signal_mismatch_is_refused(tmp_path):
    bad = ShadowResult(signal=SIGNAL_ROTATION_LOG, as_of=AS_OF, ok=True, reason="ok",
                       records=(_record(signal=SIGNAL_PAIR_BREAKDOWN),))
    with pytest.raises(ValueError):
        write_shadow_result(bad, ShadowWriter(tmp_path))


# --- These signals are not allowed to influence sizing yet -----------------------------


def _tree(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(), filename=name)


@pytest.mark.parametrize("name", SHADOW_MODULES)
def test_shadow_modules_exist_inside_the_boundary_tested_package(name):
    # test_boundary.py scans every .py under comovement_signals/, so living here means the
    # stdlib-only / no-sibling-import boundary test covers these modules too.
    assert (PACKAGE / name).is_file()


@pytest.mark.parametrize("name", SHADOW_MODULES)
def test_shadow_modules_do_not_touch_the_suggestion_contract(name):
    tree = _tree(name)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert "dca_shared_interfaces.forecasting" not in imported
    assert not any(i.startswith("comovement_signals.suggester") or i.startswith("comovement_signals.publish")
                   for i in imported)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "suggest" not in defined


def test_writer_is_not_a_sizing_suggester(tmp_path):
    assert not isinstance(ShadowWriter(tmp_path), SizingSuggester)
