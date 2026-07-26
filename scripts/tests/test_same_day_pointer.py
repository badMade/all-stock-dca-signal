"""Tests for scripts/same_day_pointer.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_same_day_pointer.py``.

Covers the same-day latest-pointer validation contract:
  * pointer_status classification (valid / stale / malformed / missing /
    pending / prior_date / future_date / outside_root / wrong_stream)
  * valid_scheduled_evidence derived from the target run_state, never asserted
  * refresh writes the required same-day metadata
  * date-specific run_state preferred over the latest pointer
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Repo root too: the scripts modules under test import the shared top-level
# dca_fileio helper, which direct invocation (python scripts/tests/test_*.py)
# cannot resolve from scripts/ alone.
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from same_day_pointer import (  # noqa: E402
    STATUS_FUTURE_DATE,
    STATUS_MALFORMED,
    STATUS_MISSING,
    STATUS_OUTSIDE_ROOT,
    STATUS_PENDING,
    STATUS_PRIOR_DATE,
    STATUS_VALID,
    STATUS_WRONG_STREAM,
    build_pointer_metadata,
    derive_valid_scheduled_evidence,
    refresh_latest_pointer,
    select_same_day_run_state,
    validate_pointer,
)

TODAY = "2026-07-21"


def _scheduled_state(trading_day=TODAY, manual=False, stream="morning"):
    return {
        "run_id": "scheduled_20260721_1520ET",
        "trading_day": trading_day,
        "run_classification": {
            "run_origin": "manual" if manual else "scheduled",
            "manual_run": bool(manual),
            "stream": stream,
        },
    }


def _write_state(root: Path, stream_dir: str, state: dict) -> Path:
    d = root / stream_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / "run_state.json"
    p.write_text(json.dumps(state), encoding="utf-8")
    return p


# --- derive_valid_scheduled_evidence -----------------------------------------

def test_evidence_true_for_scheduled_nonmanual():
    assert derive_valid_scheduled_evidence(_scheduled_state()) is True


def test_evidence_false_for_manual():
    assert derive_valid_scheduled_evidence(_scheduled_state(manual=True)) is False


def test_evidence_false_for_missing_or_malformed():
    assert derive_valid_scheduled_evidence(None) is False
    assert derive_valid_scheduled_evidence({}) is False
    assert derive_valid_scheduled_evidence({"run_classification": "nope"}) is False


def test_evidence_false_for_contract_rejected_state():
    # A run the artifact contract rejected carries a top-level
    # artifact_contract_failure marker; it must not count as scheduled evidence
    # even though its classification still says scheduled/non-manual.
    state = _scheduled_state()
    assert derive_valid_scheduled_evidence(state) is True
    state["artifact_contract_failure"] = {
        "failure": "artifact_contract_failure",
        "reason": "scheduled-run artifact contract failed",
    }
    assert derive_valid_scheduled_evidence(state) is False


def test_evidence_false_when_manual_run_key_absent():
    # Codex review r3622223183: an incomplete classification that omits
    # manual_run entirely must NOT be accepted just because it isn't
    # literally True -- manual_run must be the explicit boolean False.
    state = {
        "run_id": "x",
        "trading_day": TODAY,
        "run_classification": {"run_origin": "scheduled"},
    }
    assert derive_valid_scheduled_evidence(state) is False


# --- build_pointer_metadata --------------------------------------------------

def test_build_metadata_has_required_fields():
    meta = build_pointer_metadata(
        run_id="scheduled_20260721_1520ET",
        run_state_path="/x/run_state.json",
        stream="afternoon",
        trading_day=TODAY,
        valid_scheduled_evidence=True,
    )
    for key in (
        "run_id",
        "run_state_path",
        "stream",
        "trading_day",
        "pointer_status",
        "same_day_required",
        "updated_at_utc",
        "valid_scheduled_evidence",
    ):
        assert key in meta
    assert meta["same_day_required"] is True
    assert meta["valid_scheduled_evidence"] is True


# --- validate_pointer happy path ---------------------------------------------

def test_valid_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_VALID
    assert result.valid_scheduled_evidence is True
    assert bool(result) is True


# --- rejection paths ---------------------------------------------------------

def test_missing_pointer(tmp_path):
    result = validate_pointer(
        tmp_path / "nope.json", expected_stream="morning", expected_root=tmp_path, today=TODAY
    )
    assert result.status == STATUS_MISSING
    assert result.valid_scheduled_evidence is False


def test_malformed_pointer(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text("{not json", encoding="utf-8")
    result = validate_pointer(p, expected_stream="morning", expected_root=tmp_path, today=TODAY)
    assert result.status == STATUS_MALFORMED


def test_missing_required_keys_is_malformed(tmp_path):
    result = validate_pointer(
        {"run_id": "x"}, expected_stream="morning", expected_root=tmp_path, today=TODAY
    )
    assert result.status == STATUS_MALFORMED


def test_prior_date_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260720_1030ET", _scheduled_state(trading_day="2026-07-20"))
    pointer = {
        "run_id": "scheduled_20260720_1520ET",
        "run_state_path": str(state_path),
        "trading_day": "2026-07-20",
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_PRIOR_DATE
    assert result.valid_scheduled_evidence is False


def test_future_date_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260722_1030ET", _scheduled_state(trading_day="2026-07-22"))
    pointer = {
        "run_id": "scheduled_20260722_1520ET",
        "run_state_path": str(state_path),
        "trading_day": "2026-07-22",
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_FUTURE_DATE


def test_wrong_stream_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "midday",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_WRONG_STREAM


def test_outside_root_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    root.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    state_path = _write_state(other, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_OUTSIDE_ROOT


def test_pending_when_target_missing(tmp_path):
    root = tmp_path / "morning_dca"
    root.mkdir(parents=True)
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(root / "20260721_1030ET" / "run_state.json"),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_PENDING
    assert result.valid_scheduled_evidence is False


def test_pending_status_field_never_evidence(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
        "pointer_status": "pending",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_PENDING


def test_manual_target_is_not_evidence(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state(manual=True))
    pointer = {
        "run_id": "manual_x",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_PENDING
    assert result.valid_scheduled_evidence is False


def test_target_wrong_stream_classification_rejected(tmp_path):
    # Codex review r3622685597: a pointer can declare stream="morning" (passing
    # the pointer-level stream check) while its run_state_path target's OWN
    # run_classification.stream is a different stream (e.g. a misplaced/copied
    # midday artifact). The target's own classification stream must be checked
    # independently, not just the pointer's claim.
    root = tmp_path / "morning_dca"
    other_stream_state = {
        "run_id": "midday_live_20260721_1230ET",
        "trading_day": TODAY,
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "midday",
        },
    }
    state_path = _write_state(root, "20260721_1030ET", other_stream_state)
    pointer = {
        "run_id": "midday_live_20260721_1230ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_WRONG_STREAM
    assert result.valid_scheduled_evidence is False


def test_declared_stale_status_rejected_even_when_checks_pass(tmp_path):
    # Codex review r3622223192: a pointer self-tagged with a non-"valid"
    # pointer_status must be rejected even when the recomputed path/date/
    # stream/target checks would otherwise all pass.
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
        "pointer_status": "stale",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == "stale"
    assert result.valid_scheduled_evidence is False


def test_declared_unknown_status_is_malformed(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
        "pointer_status": "not_a_real_status",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_MALFORMED
    assert result.valid_scheduled_evidence is False


def test_target_run_state_prior_day_rejected_even_when_pointer_claims_today(tmp_path):
    # Codex review r3622223207: the pointer's own trading_day can claim today
    # while the target run_state it names is actually a prior-day state
    # (e.g. an unrefreshed/copied file). The target's own trading_day must be
    # checked independently.
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state(trading_day="2026-07-20"))
    pointer = {
        "run_id": "scheduled_20260721_1520ET",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.status == STATUS_PRIOR_DATE
    assert result.valid_scheduled_evidence is False


def test_pointer_claim_not_trusted_over_manual_state(tmp_path):
    # Pointer lies (claims evidence) but target run_state is manual -> rejected.
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state(manual=True))
    pointer = {
        "run_id": "manual_x",
        "run_state_path": str(state_path),
        "trading_day": TODAY,
        "stream": "morning",
        "valid_scheduled_evidence": True,
    }
    result = validate_pointer(pointer, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.valid_scheduled_evidence is False


# --- refresh_latest_pointer --------------------------------------------------

def test_refresh_writes_same_day_metadata(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
    pointer_path = root / "latest_morning_run.json"
    refresh_latest_pointer(
        pointer_path,
        run_id="scheduled_20260721_1520ET",
        run_state_path=state_path,
        stream="morning",
        trading_day=TODAY,
        run_state=_scheduled_state(),
    )
    data = json.loads(pointer_path.read_text())
    assert data["same_day_required"] is True
    assert data["valid_scheduled_evidence"] is True
    assert data["pointer_status"] == STATUS_VALID
    # Round-trips as valid evidence.
    result = validate_pointer(pointer_path, expected_stream="morning", expected_root=root, today=TODAY)
    assert result.is_valid


def test_refresh_derives_false_evidence_for_manual(tmp_path):
    root = tmp_path / "morning_dca"
    state_path = _write_state(root, "20260721_1030ET", _scheduled_state(manual=True))
    pointer_path = root / "latest_morning_run.json"
    refresh_latest_pointer(
        pointer_path,
        run_id="manual_x",
        run_state_path=state_path,
        stream="morning",
        trading_day=TODAY,
        run_state=_scheduled_state(manual=True),
    )
    data = json.loads(pointer_path.read_text())
    assert data["valid_scheduled_evidence"] is False


# --- select_same_day_run_state -----------------------------------------------

def test_select_prefers_date_specific_state(tmp_path):
    root = tmp_path / "morning_dca"
    day_dir = root / "20260721_1030ET"
    _write_state(root, "20260721_1030ET", _scheduled_state())
    # Pointer aims at a stale prior-day state; date-specific file should win.
    stale = _write_state(root, "20260720_1030ET", _scheduled_state(trading_day="2026-07-20"))
    pointer_path = root / "latest_morning_run.json"
    pointer_path.write_text(
        json.dumps(
            {
                "run_id": "scheduled_20260720_1520ET",
                "run_state_path": str(stale),
                "trading_day": "2026-07-20",
                "stream": "morning",
            }
        ),
        encoding="utf-8",
    )
    state = select_same_day_run_state(
        stream="morning",
        expected_root=root,
        trading_day_dir=day_dir,
        pointer_path=pointer_path,
        today=TODAY,
    )
    assert state is not None
    assert state["trading_day"] == TODAY


def test_select_rejects_prior_day_state_in_todays_directory(tmp_path):
    # Codex review r3622223201: a prior-day run_state left (or copied) into
    # today's expected directory must not be trusted just because it lives
    # under trading_day_dir -- its own trading_day must be checked too.
    root = tmp_path / "morning_dca"
    day_dir = root / "20260721_1030ET"
    _write_state(root, "20260721_1030ET", _scheduled_state(trading_day="2026-07-20"))
    state = select_same_day_run_state(
        stream="morning",
        expected_root=root,
        trading_day_dir=day_dir,
        today=TODAY,
    )
    assert state is None


def test_select_rejects_wrong_stream_state_in_todays_directory(tmp_path):
    # Codex review r3623485188: a same-day, scheduled, non-manual state from a
    # DIFFERENT stream (e.g. a copied midday state under the morning root)
    # must not be trusted just because it lives under trading_day_dir -- the
    # pointer path already checks stream identity, so the direct path must too.
    root = tmp_path / "morning_dca"
    day_dir = root / "20260721_1030ET"
    _write_state(root, "20260721_1030ET", _scheduled_state(stream="midday"))
    state = select_same_day_run_state(
        stream="morning",
        expected_root=root,
        trading_day_dir=day_dir,
        today=TODAY,
    )
    assert state is None


def test_select_rejects_missing_stream_state_in_todays_directory(tmp_path):
    # Codex review r3623485188: "fail closed when it is absent" -- a
    # same-day, scheduled, non-manual state whose classification omits
    # stream entirely must not be trusted either, since there is no separate
    # pointer-level stream declaration to have already vetted it.
    root = tmp_path / "morning_dca"
    day_dir = root / "20260721_1030ET"
    state_without_stream = _scheduled_state()
    del state_without_stream["run_classification"]["stream"]
    _write_state(root, "20260721_1030ET", state_without_stream)
    state = select_same_day_run_state(
        stream="morning",
        expected_root=root,
        trading_day_dir=day_dir,
        today=TODAY,
    )
    assert state is None


def test_select_none_when_only_stale_pointer(tmp_path):
    root = tmp_path / "morning_dca"
    stale = _write_state(root, "20260720_1030ET", _scheduled_state(trading_day="2026-07-20"))
    pointer_path = root / "latest_morning_run.json"
    pointer_path.write_text(
        json.dumps(
            {
                "run_id": "scheduled_20260720_1520ET",
                "run_state_path": str(stale),
                "trading_day": "2026-07-20",
                "stream": "morning",
            }
        ),
        encoding="utf-8",
    )
    state = select_same_day_run_state(
        stream="morning",
        expected_root=root,
        trading_day_dir=root / "20260721_1030ET",
        pointer_path=pointer_path,
        today=TODAY,
    )
    assert state is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
