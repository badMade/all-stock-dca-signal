import json

from scripts.same_day_pointer import STATUS_PENDING, STATUS_STALE, STATUS_VALID, refresh_latest_pointer


def test_refresh_latest_pointer_creates_valid_pointer(tmp_path):
    pointer_path = tmp_path / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    run_state = {
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False
        }
    }

    result = refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        run_state=run_state
    )

    assert result == pointer_path
    assert pointer_path.exists()

    data = json.loads(pointer_path.read_bytes())
    assert data["run_id"] == "run_123"
    assert data["stream"] == "morning"
    assert data["trading_day"] == "2023-10-26"
    assert data["pointer_status"] == STATUS_VALID
    assert data["valid_scheduled_evidence"] is True


def test_refresh_latest_pointer_handles_none_valid_scheduled_evidence(tmp_path):
    pointer_path = tmp_path / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    run_state = {
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True
        }
    }

    refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        run_state=run_state,
        valid_scheduled_evidence=None
    )

    data = json.loads(pointer_path.read_bytes())
    assert data["valid_scheduled_evidence"] is False
    assert data["pointer_status"] == STATUS_PENDING


def test_refresh_latest_pointer_explicit_evidence_and_status(tmp_path):
    pointer_path = tmp_path / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        valid_scheduled_evidence=True,
        pointer_status="recovery_context_valid",
        updated_at_utc="2023-10-26T12:00:00Z",
        extra={"custom": "field"}
    )

    data = json.loads(pointer_path.read_bytes())
    assert data["pointer_status"] == "recovery_context_valid"
    assert data["updated_at_utc"] == "2023-10-26T12:00:00Z"
    assert data["custom"] == "field"


def test_refresh_latest_pointer_demotes_valid_without_evidence(tmp_path):
    pointer_path = tmp_path / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        valid_scheduled_evidence=False,
        pointer_status=STATUS_VALID
    )

    data = json.loads(pointer_path.read_bytes())
    assert data["pointer_status"] == STATUS_PENDING
    assert data["valid_scheduled_evidence"] is False


def test_refresh_latest_pointer_keeps_other_status_without_evidence(tmp_path):
    pointer_path = tmp_path / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        valid_scheduled_evidence=False,
        pointer_status=STATUS_STALE
    )

    data = json.loads(pointer_path.read_bytes())
    assert data["pointer_status"] == STATUS_STALE
    assert data["valid_scheduled_evidence"] is False


def test_refresh_latest_pointer_creates_directories(tmp_path):
    pointer_path = tmp_path / "nested" / "dirs" / "pointer.json"
    run_state_path = tmp_path / "run_state.json"

    assert not pointer_path.parent.exists()

    result = refresh_latest_pointer(
        pointer_path=pointer_path,
        run_id="run_123",
        run_state_path=run_state_path,
        stream="morning",
        trading_day="2023-10-26",
        valid_scheduled_evidence=True
    )

    assert pointer_path.parent.exists()
    assert pointer_path.exists()
    assert result == pointer_path
