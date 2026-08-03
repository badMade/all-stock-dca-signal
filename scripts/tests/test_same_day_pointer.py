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
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Repo root too: the scripts modules under test import the shared top-level
# dca_fileio helper, which direct invocation (python scripts/tests/test_*.py)
# cannot resolve from scripts/ alone.
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

import same_day_pointer as same_day_pointer_module  # noqa: E402
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
    data = json.loads(pointer_path.read_bytes())
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
    data = json.loads(pointer_path.read_bytes())
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


def test_select_uses_perplexity_primary_alias_when_canonical_pointer_missing(tmp_path):
    canonical_root = tmp_path / "intraday_dca"
    canonical_day_dir = canonical_root / "20260721_1130ET"
    alias_root = tmp_path / "perplexity_primary_20260721"
    alias_state = _write_state(
        alias_root,
        "2026-07-21_1130ET",
        _scheduled_state(stream="intraday"),
    )
    alias_pointer = alias_root / "latest_intraday_run.json"
    alias_pointer.write_text(
        json.dumps(
            {
                "run_id": "intraday_live_20260721_1130ET",
                "run_state_path": str(alias_state),
                "trading_day": TODAY,
                "stream": "intraday",
                "pointer_status": STATUS_VALID,
                "same_day_required": True,
                "valid_scheduled_evidence": True,
                "updated_at_utc": "2026-07-21T15:35:00Z",
            }
        ),
        encoding="utf-8",
    )

    state = select_same_day_run_state(
        stream="intraday",
        expected_root=canonical_root,
        trading_day_dir=canonical_day_dir,
        pointer_path=canonical_root / "latest_intraday_run.json",
        fallback_pointer_paths=[(alias_pointer, alias_root)],
        today=TODAY,
    )

    assert state is not None
    assert state["trading_day"] == TODAY
    assert state["run_classification"]["stream"] == "intraday"


def test_select_uses_default_perplexity_primary_alias_for_1130_catchup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(same_day_pointer_module, "CRON_TRACKING_ROOT", tmp_path)
    canonical_root = tmp_path / "intraday_dca"
    alias_root = tmp_path / "perplexity_primary_20260721"
    alias_state = _write_state(
        alias_root,
        "2026-07-21_1130ET",
        _scheduled_state(stream="intraday"),
    )
    alias_pointer = alias_root / "latest_intraday_run.json"
    alias_pointer.write_text(
        json.dumps(
            {
                "run_id": "intraday_live_20260721_1130ET",
                "run_state_path": str(alias_state),
                "trading_day": TODAY,
                "stream": "intraday",
                "pointer_status": STATUS_VALID,
                "same_day_required": True,
                "valid_scheduled_evidence": True,
                "updated_at_utc": "2026-07-21T15:35:00Z",
            }
        ),
        encoding="utf-8",
    )

    state = select_same_day_run_state(
        stream="intraday",
        expected_root=canonical_root,
        trading_day_dir=canonical_root / "20260721_1130ET",
        pointer_path=canonical_root / "latest_intraday_run.json",
        today=TODAY,
    )

    assert state is not None
    assert state["trading_day"] == TODAY
    assert state["run_classification"]["stream"] == "intraday"


def test_select_rejects_alias_pointer_without_alias_root(tmp_path):
    canonical_root = tmp_path / "intraday_dca"
    alias_root = tmp_path / "perplexity_primary_20260721"
    alias_state = _write_state(
        alias_root,
        "2026-07-21_1130ET",
        _scheduled_state(stream="intraday"),
    )
    alias_pointer = alias_root / "latest_intraday_run.json"
    alias_pointer.write_text(
        json.dumps(
            {
                "run_id": "intraday_live_20260721_1130ET",
                "run_state_path": str(alias_state),
                "trading_day": TODAY,
                "stream": "intraday",
                "pointer_status": STATUS_VALID,
                "same_day_required": True,
                "valid_scheduled_evidence": True,
                "updated_at_utc": "2026-07-21T15:35:00Z",
            }
        ),
        encoding="utf-8",
    )

    state = select_same_day_run_state(
        stream="intraday",
        expected_root=canonical_root,
        pointer_path=canonical_root / "latest_intraday_run.json",
        fallback_pointer_paths=[alias_pointer],
        today=TODAY,
    )

    assert state is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


# --- Recovery-context pointer contract ---------------------------------------
#
# Codex review: "Define a usable contract for the recovery pointer". A recovery
# pointer (latest_midday_recovery_run.json) had no representable state:
# pointer_status='valid' is pinned to valid_scheduled_evidence=true, which a
# recovery artifact must never claim, and every other status means unusable.


def _recovery_state(trading_day=TODAY, stream="midday_recovery", **overrides):
    rc = {
        "run_origin": "scheduled_recovery",
        "run_type": "midday_dca_live_recovery",
        "manual_run": False,
        "stream": stream,
        "trading_day": trading_day,
        "run_id": "midday_recovery_20260721_1305ET",
        "state_schema_version": "midday-live-v2-recovery",
        "valid_scheduled_evidence": False,
        "valid_recovery_context": True,
        "same_day_top_up_context_only": True,
    }
    rc.update(overrides)
    return {
        "run_id": "midday_recovery_20260721_1305ET",
        "trading_day": trading_day,
        "run_classification": rc,
    }


def _recovery_pointer(state_path, trading_day=TODAY, stream="midday_recovery", status=None):
    from same_day_pointer import STATUS_VALID_RECOVERY_CONTEXT

    return {
        "run_id": "midday_recovery_20260721_1305ET",
        "run_state_path": str(state_path),
        "stream": stream,
        "trading_day": trading_day,
        "pointer_status": STATUS_VALID_RECOVERY_CONTEXT if status is None else status,
        "same_day_required": True,
        "valid_scheduled_evidence": False,
        "updated_at_utc": "2026-07-21T17:05:00Z",
    }


def test_derive_valid_recovery_context_accepts_recovery_state():
    from same_day_pointer import derive_valid_recovery_context

    assert derive_valid_recovery_context(_recovery_state()) is True


def test_derive_valid_recovery_context_rejects_scheduled_and_manual():
    from same_day_pointer import derive_valid_recovery_context

    assert derive_valid_recovery_context(_scheduled_state()) is False
    assert derive_valid_recovery_context(_scheduled_state(manual=True)) is False
    assert derive_valid_recovery_context(None) is False
    assert derive_valid_recovery_context({}) is False


def test_derive_valid_recovery_context_requires_positive_assertions():
    # Merely omitting the scheduled flags is not enough -- the artifact must
    # positively assert recovery context AND disclaim scheduled evidence, so a
    # half-written classification can never be folded into a later run's guard.
    from same_day_pointer import derive_valid_recovery_context

    assert derive_valid_recovery_context(_recovery_state(valid_recovery_context=None)) is False
    assert derive_valid_recovery_context(_recovery_state(valid_scheduled_evidence=True)) is False
    assert derive_valid_recovery_context(_recovery_state(manual_run=True)) is False


def test_derive_valid_recovery_context_rejects_contract_rejected_state():
    state = _recovery_state()
    state["artifact_contract_failure"] = {"reason": "missing artifact"}

    from same_day_pointer import derive_valid_recovery_context

    assert derive_valid_recovery_context(state) is False


def test_validate_recovery_pointer_accepts_recovery_artifact(tmp_path):
    from same_day_pointer import STATUS_VALID_RECOVERY_CONTEXT, validate_recovery_pointer

    root = tmp_path / "midday_dca"
    state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())

    result = validate_recovery_pointer(
        _recovery_pointer(state_path),
        expected_stream="midday_recovery",
        expected_root=root,
        today=TODAY,
    )

    assert result.status == STATUS_VALID_RECOVERY_CONTEXT
    # Usable, but never scheduled evidence for the slot it recovered.
    assert result.valid_scheduled_evidence is False


def test_validate_recovery_pointer_rejects_non_recovery_stream(tmp_path):
    from same_day_pointer import STATUS_WRONG_STREAM, validate_recovery_pointer

    root = tmp_path / "midday_dca"
    state_path = _write_state(root, "20260721_1230ET", _scheduled_state(stream="midday"))

    result = validate_recovery_pointer(
        _recovery_pointer(state_path, stream="midday"),
        expected_stream="midday",
        expected_root=root,
        today=TODAY,
    )

    assert result.status == STATUS_WRONG_STREAM
    assert result.valid_scheduled_evidence is False


def test_validate_recovery_pointer_rejects_scheduled_target(tmp_path):
    # A scheduled artifact must not be discoverable through the recovery path.
    from same_day_pointer import STATUS_PENDING, validate_recovery_pointer

    root = tmp_path / "midday_dca"
    state_path = _write_state(
        root, "20260721_1230ET_recovery_1305ET", _scheduled_state(stream="midday_recovery")
    )

    result = validate_recovery_pointer(
        _recovery_pointer(state_path),
        expected_stream="midday_recovery",
        expected_root=root,
        today=TODAY,
    )

    assert result.status == STATUS_PENDING
    assert result.valid_scheduled_evidence is False


def test_validate_recovery_pointer_enforces_same_day(tmp_path):
    from same_day_pointer import STATUS_PRIOR_DATE, validate_recovery_pointer

    root = tmp_path / "midday_dca"
    state_path = _write_state(
        root, "20260720_1230ET_recovery_1305ET", _recovery_state(trading_day="2026-07-20")
    )

    result = validate_recovery_pointer(
        _recovery_pointer(state_path, trading_day="2026-07-20"),
        expected_stream="midday_recovery",
        expected_root=root,
        today=TODAY,
    )

    assert result.status == STATUS_PRIOR_DATE
    assert result.valid_scheduled_evidence is False


def test_scheduled_validate_pointer_still_rejects_recovery_pointer(tmp_path):
    # The scheduled path must be unchanged by the new status: a recovery
    # pointer is never scheduled evidence, whichever stream it is asked about.
    from same_day_pointer import STATUS_VALID_RECOVERY_CONTEXT

    root = tmp_path / "midday_dca"
    state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())

    result = validate_pointer(
        _recovery_pointer(state_path),
        expected_stream="midday_recovery",
        expected_root=root,
        today=TODAY,
    )

    assert result.status == STATUS_VALID_RECOVERY_CONTEXT
    assert result.valid_scheduled_evidence is False
    assert result.is_valid is False
    assert bool(result) is False


def test_recovery_status_is_rejected_for_scheduled_consumers():
    from same_day_pointer import (
        REJECTED_STATUSES,
        STATUS_VALID_RECOVERY_CONTEXT,
        is_recovery_context_status,
    )

    assert STATUS_VALID_RECOVERY_CONTEXT in REJECTED_STATUSES
    assert is_recovery_context_status(STATUS_VALID_RECOVERY_CONTEXT) is True
    assert is_recovery_context_status(STATUS_VALID) is False


def test_derive_valid_recovery_context_requires_full_contract():
    # Codex review: "Validate all required recovery fields". Validating only the
    # four flags would let a partial or truncated artifact -- right booleans, no
    # stream or run identity -- be trusted as usable context, contradicting
    # schemas/recovery_run_classification.schema.json.
    from same_day_pointer import derive_valid_recovery_context

    # The four-field subset that used to pass.
    assert derive_valid_recovery_context({
        "run_classification": {
            "run_origin": "scheduled_recovery",
            "manual_run": False,
            "valid_recovery_context": True,
            "valid_scheduled_evidence": False,
        }
    }) is False

    for missing in ("stream", "run_type", "run_id", "state_schema_version",
                    "same_day_top_up_context_only"):
        state = _recovery_state()
        del state["run_classification"][missing]
        assert derive_valid_recovery_context(state) is False, missing

    # A non-recovery stream name, a blank identity, and a claimed in-place
    # recovery_rerun are each rejected.
    assert derive_valid_recovery_context(_recovery_state(stream="midday")) is False
    assert derive_valid_recovery_context(_recovery_state(run_id="  ")) is False
    assert derive_valid_recovery_context(_recovery_state(recovery_rerun=True)) is False
    # recovery_rerun=False is explicitly allowed.
    assert derive_valid_recovery_context(_recovery_state(recovery_rerun=False)) is True


def test_recovery_validation_result_is_truthy():
    # Codex review: "Expose a truthy recovery-validation result". A recovery
    # consumer using the class's `if validate_...(...)` idiom must not discard
    # every valid recovery pointer.
    from same_day_pointer import validate_recovery_pointer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())

        result = validate_recovery_pointer(
            _recovery_pointer(state_path),
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert bool(result) is True
        assert result.is_valid_recovery_context is True
        # Still never scheduled evidence.
        assert result.is_valid is False
        assert result.valid_scheduled_evidence is False


def test_scheduled_rejection_of_recovery_pointer_stays_falsey():
    # The same status reached through the SCHEDULED path must stay falsey, so
    # the truthy recovery arm cannot leak into a scheduled consumer.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())

        result = validate_pointer(
            _recovery_pointer(state_path),
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert bool(result) is False
        assert result.is_valid_recovery_context is False
        assert result.recovery_context is False


def test_recovery_pointer_requires_explicit_status():
    # Codex review: "Require the recovery pointer status discriminator". The
    # missing-status allowance exists only for legacy SCHEDULED pointers; a
    # recovery pointer cannot predate a status introduced alongside it.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())
        pointer = _recovery_pointer(state_path)
        del pointer["pointer_status"]

        result = validate_recovery_pointer(
            pointer,
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert result.status == STATUS_MALFORMED
        assert bool(result) is False


def test_legacy_scheduled_pointer_without_status_still_accepted():
    # The compatibility path for scheduled pointers is unchanged.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "morning_dca"
        state_path = _write_state(root, "20260721_1030ET", _scheduled_state())
        result = validate_pointer(
            {
                "run_id": "scheduled_20260721_1520ET",
                "run_state_path": str(state_path),
                "trading_day": TODAY,
                "stream": "morning",
            },
            expected_stream="morning",
            expected_root=root,
            today=TODAY,
        )

        assert result.status == STATUS_VALID
        assert bool(result) is True


def test_recovery_pointer_rejects_bogus_identity_values():
    # The pointer path delegates to the canonical contract, so arbitrary
    # run_type / state_schema_version values are rejected here too.
    from same_day_pointer import STATUS_PENDING, validate_recovery_pointer

    for override in ({"run_type": "bogus"}, {"state_schema_version": "bogus"}):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state(**override)
            )
            result = validate_recovery_pointer(
                _recovery_pointer(state_path),
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )
            assert result.status == STATUS_PENDING, override
            assert bool(result) is False


def test_recovery_pointer_requires_same_day_classification():
    # The target's CLASSIFICATION trading_day must match the ET day, not just
    # the pointer's and the state's top-level day.
    from same_day_pointer import STATUS_PENDING, validate_recovery_pointer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state = _recovery_state()
        state["run_classification"]["trading_day"] = "2026-07-20"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", state)

        result = validate_recovery_pointer(
            _recovery_pointer(state_path),
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert result.status == STATUS_PENDING
        assert bool(result) is False


def test_recovery_pointer_rejects_contradictory_metadata():
    # Codex review: "Reject contradictory recovery pointer metadata". Both of
    # these are rejected by latest_pointer.schema.json, so the helper must
    # reject them too or schema and helper validation disagree.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    for override in (
        {"valid_scheduled_evidence": True},
        {"same_day_required": False},
    ):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer.update(override)

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            # The TARGET is valid; only the pointer's own metadata contradicts.
            assert result.status == STATUS_MALFORMED, override
            assert bool(result) is False, override


def test_recovery_pointer_helper_agrees_with_its_schema():
    # Pin the two together: anything the helper accepts must also satisfy
    # latest_pointer.schema.json, and the contradictory cases must fail both.
    jsonschema = pytest.importorskip("jsonschema")

    from same_day_pointer import validate_recovery_pointer

    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "latest_pointer.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft7Validator(schema)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())

        good = _recovery_pointer(state_path)
        assert list(validator.iter_errors(good)) == []
        assert bool(
            validate_recovery_pointer(
                good,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )
        ) is True

        for override in (
            {"valid_scheduled_evidence": True},
            {"same_day_required": False},
        ):
            bad = _recovery_pointer(state_path)
            bad.update(override)
            assert list(validator.iter_errors(bad)), override
            assert bool(
                validate_recovery_pointer(
                    bad,
                    expected_stream="midday_recovery",
                    expected_root=root,
                    today=TODAY,
                )
            ) is False, override


def test_recovery_pointer_requires_boolean_evidence_and_same_day_fields():
    # Codex review: "Require the recovery pointer evidence field". The schema
    # pins valid_scheduled_evidence to the boolean constant false and
    # same_day_required to true, so an omitted field or a non-boolean like the
    # STRING "false" is schema-invalid -- rejecting only literal True/False
    # would leave the helper trusting pointers a schema consumer refuses.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    bad_variants = [
        {"valid_scheduled_evidence": "false"},
        {"valid_scheduled_evidence": 0},
        {"valid_scheduled_evidence": None},
        "drop_evidence",
        {"same_day_required": "true"},
        {"same_day_required": 1},
        {"same_day_required": None},
        "drop_same_day",
    ]

    for variant in bad_variants:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            if variant == "drop_evidence":
                del pointer["valid_scheduled_evidence"]
            elif variant == "drop_same_day":
                del pointer["same_day_required"]
            else:
                pointer.update(variant)

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_MALFORMED, variant
            assert bool(result) is False, variant


def test_recovery_pointer_requires_updated_at_utc():
    # Codex review: "Require recovery pointer update timestamps". The schema
    # marks updated_at_utc required with format date-time, and a recovery
    # pointer cannot be a legacy artifact, so the helper must enforce it too.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    bad = [{"updated_at_utc": "not-a-timestamp"}, {"updated_at_utc": ""},
           {"updated_at_utc": None}, {"updated_at_utc": 1730000000}, "drop"]

    for variant in bad:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            if variant == "drop":
                del pointer["updated_at_utc"]
            else:
                pointer.update(variant)

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_MALFORMED, variant
            assert bool(result) is False, variant


def test_recovery_pointer_accepts_both_utc_spellings():
    from same_day_pointer import STATUS_VALID_RECOVERY_CONTEXT, validate_recovery_pointer

    for stamp in ("2026-07-21T17:05:00Z", "2026-07-21T17:05:00+00:00"):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer["updated_at_utc"] = stamp

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_VALID_RECOVERY_CONTEXT, stamp


def test_recovery_pointer_rejects_contract_failed_target():
    # The state-level marker: a valid-looking classification does not rescue an
    # artifact the contract rejected.
    from same_day_pointer import STATUS_PENDING, validate_recovery_pointer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state = _recovery_state()
        state["artifact_contract_failure"] = {"reason": "missing artifact"}
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", state)

        result = validate_recovery_pointer(
            _recovery_pointer(state_path),
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert result.status == STATUS_PENDING
        assert bool(result) is False


def test_recovery_timestamp_requires_a_timezone_bearing_datetime():
    # Codex review discussion_r3660101883: fromisoformat alone accepts a bare
    # date and a naive timestamp, neither of which is an RFC 3339 date-time or
    # establishes a UTC instant, so the helper trusted pointers the schema
    # rejects.
    from same_day_pointer import _is_iso_utc_timestamp

    for good in ("2026-07-21T17:05:00Z", "2026-07-21T17:05:00+00:00", "2026-07-21T13:05:00-04:00"):
        assert _is_iso_utc_timestamp(good) is True, good
    for bad in ("2026-07-21", "2026-07-21T12:00:00", "", "   ", None, 1730000000, "not-a-time"):
        assert _is_iso_utc_timestamp(bad) is False, bad


def test_recovery_pointer_run_ids_are_compared_exactly():
    # Codex review: "Compare recovery run IDs without normalizing them". Both
    # recovery schemas accept a padded non-blank run_id (pattern "\\S"), so
    # ' run-1 ' and 'run-1' are each schema-valid but are DIFFERENT persisted
    # strings. A stripped comparison let this validator promise an identity
    # match while audit metadata named two different runs.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    for padded in (" midday_recovery_20260721_1305ET",
                   "midday_recovery_20260721_1305ET ",
                   "\tmidday_recovery_20260721_1305ET"):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer["run_id"] = padded

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_MALFORMED, padded
            assert bool(result) is False, padded

    # The exact value still validates.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())
        assert bool(
            validate_recovery_pointer(
                _recovery_pointer(state_path),
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )
        ) is True


def test_recovery_pointer_rejects_padded_stream_names():
    # Codex review: "Reject padded recovery pointer stream names". validate_pointer
    # normalized the persisted stream with str(...).strip() before comparing, so
    # ' midday_recovery ' validated here while latest_pointer.schema.json rejects
    # it -- the stream enum is exact. Validation must not repair malformed
    # metadata; rejecting reports wrong_stream, which is the fail-closed
    # direction.
    from same_day_pointer import STATUS_WRONG_STREAM, validate_recovery_pointer

    for padded in (" midday_recovery", "midday_recovery ", " midday_recovery ",
                   "\tmidday_recovery", "midday_recovery\n"):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer["stream"] = padded

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_WRONG_STREAM, padded
            assert bool(result) is False, padded

    # The exact value still validates.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())
        result = validate_recovery_pointer(
            _recovery_pointer(state_path),
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )
        assert bool(result) is True


def test_recovery_timestamp_requires_full_rfc3339_syntax():
    # Codex review: "Enforce RFC 3339 syntax for recovery timestamps".
    # fromisoformat accepts several ISO 8601 spellings that RFC 3339 -- and
    # therefore the schema's `format: date-time` -- does not, so the helper
    # still returned truthy for pointers a schema-aware consumer refuses.
    from same_day_pointer import _is_iso_utc_timestamp

    for bad in (
        "20260721T170500+00:00",      # basic format, no separators
        "20260721T170500Z",
        "2026-W30-2T17:05:00+00:00",  # ISO week date
        "2026-202T17:05:00+00:00",    # ordinal date
        "2026-07-21T17:05:00+00:00:30",  # offset carrying seconds
        "2026-07-21 17:05:00+00:00",  # space separator
        "2026-07-21T17:05+00:00",     # no seconds
        " 2026-07-21T17:05:00Z",      # padded: the schema pattern is the whole string
        "2026-07-21T17:05:00Z ",
        "2026-07-21T17:05:00Z\n",     # `$` would match here; the pattern uses \Z
        "2026-13-21T17:05:00Z",       # syntactically fine, semantically invalid
        "2026-07-32T17:05:00Z",
    ):
        assert _is_iso_utc_timestamp(bad) is False, bad
    # Fractional seconds and a lowercase 'z'/'t' stay valid -- all RFC 3339.
    for good in (
        "2026-07-21T17:05:00.123Z",
        "2026-07-21T17:05:00.123456+00:00",
        "2026-07-21t17:05:00z",
        "2026-07-21T13:05:00-04:00",
    ):
        assert _is_iso_utc_timestamp(good) is True, good


def test_recovery_pointer_rejects_naive_or_date_only_timestamps():
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    for stamp in ("2026-07-21", "2026-07-21T17:05:00"):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer["updated_at_utc"] = stamp

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_MALFORMED, stamp
            assert bool(result) is False, stamp


def test_recovery_pointer_run_id_must_match_its_target():
    # Codex review discussion_r3660101895: a mismatched or null pointer run_id
    # attributes discovered recovery context to one run while run_state_path
    # resolves to another.
    from same_day_pointer import STATUS_MALFORMED, validate_recovery_pointer

    for bad_run_id in ("some_other_run", None, "", "   "):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "midday_dca"
            state_path = _write_state(
                root, "20260721_1230ET_recovery_1305ET", _recovery_state()
            )
            pointer = _recovery_pointer(state_path)
            pointer["run_id"] = bad_run_id

            result = validate_recovery_pointer(
                pointer,
                expected_stream="midday_recovery",
                expected_root=root,
                today=TODAY,
            )

            assert result.status == STATUS_MALFORMED, bad_run_id
            assert bool(result) is False, bad_run_id


def test_recovery_pointer_accepts_a_matching_run_id():
    from same_day_pointer import STATUS_VALID_RECOVERY_CONTEXT, validate_recovery_pointer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "midday_dca"
        state_path = _write_state(root, "20260721_1230ET_recovery_1305ET", _recovery_state())
        pointer = _recovery_pointer(state_path)
        # The fixture's pointer run_id already equals the target's.
        assert pointer["run_id"] == _recovery_state()["run_classification"]["run_id"]

        result = validate_recovery_pointer(
            pointer,
            expected_stream="midday_recovery",
            expected_root=root,
            today=TODAY,
        )

        assert result.status == STATUS_VALID_RECOVERY_CONTEXT
        assert bool(result) is True
