import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude._agent_scratch.morning_signal_harness import (
    _merge_results,
    _unwrap,
    build_data_provider,
    extract_candidates,
    parse_now,
)


def test_unwrap():
    assert _unwrap({"data": {"hello": "world"}}) == {"hello": "world"}
    assert _unwrap({"hello": "world"}) == {"hello": "world"}
    assert _unwrap("not a dict") == {}


def test_merge_results():
    batch1 = {"results": [{"symbol": "AAPL", "data": "1"}]}
    batch2 = {"data": {"results": [{"symbol": "GOOG", "data": "2"}]}}
    batch3 = {"results": "not a list"}

    assert _merge_results([batch1, batch2, batch3]) == {
        "results": [{"symbol": "AAPL", "data": "1"}, {"symbol": "GOOG", "data": "2"}]
    }


def test_parse_now():
    dt = parse_now("2026-08-10T10:30:00")
    assert dt.tzinfo.key == "America/New_York"
    assert dt.hour == 10

    dt2 = parse_now("2026-08-10T14:30:00Z")
    assert dt2.tzinfo.key == "America/New_York"
    assert dt2.hour == 10  # 14:30 UTC is 10:30 Eastern EDT


def test_extract_candidates():
    run_result = {
        "artifacts": {
            "signal_rows": [
                {"ticker": "AAPL", "action": "BUY", "multiplier": 1.5, "execution_status": "auto_buy_eligible"}
            ]
        }
    }

    candidates = extract_candidates(run_result)
    assert len(candidates) == 1
    assert candidates[0]["ticker"] == "AAPL"
    assert candidates[0]["auto_buy_eligible"] is True
    assert candidates[0]["manual_review_candidate"] is False


def test_build_data_provider():
    with tempfile.TemporaryDirectory() as td:
        data_file = Path(td) / "data.json"

        # Test valid data
        data_file.write_text(
            json.dumps(
                {
                    "historicals": [{"results": [{"symbol": "AAPL", "bars": []}]}],
                    "quotes": [{"results": [{"quote": {"symbol": "AAPL"}}]}],
                }
            )
        )

        provider = build_data_provider(data_file)

        # Calling functions should return merged results
        hist_results = provider._historicals_fn()
        assert hist_results == {"results": [{"symbol": "AAPL", "bars": []}]}

        quote_results = provider._quotes_fn()
        assert quote_results == {"results": [{"quote": {"symbol": "AAPL"}}]}


def test_build_data_provider_invalid():
    with tempfile.TemporaryDirectory() as td:
        data_file = Path(td) / "data.json"

        # Invalid historicals
        data_file.write_text(json.dumps({"historicals": "not a list", "quotes": []}))

        with pytest.raises(ValueError, match="must be a JSON array"):
            build_data_provider(data_file)

        # Invalid quotes
        data_file.write_text(json.dumps({"historicals": [], "quotes": "not a list"}))

        with pytest.raises(ValueError, match="must be a JSON array"):
            build_data_provider(data_file)


def test_main(capsys):
    from claude._agent_scratch.morning_signal_harness import main

    with tempfile.TemporaryDirectory() as td:
        data_file = Path(td) / "data.json"
        out_file = Path(td) / "out.json"

        data_file.write_text(
            json.dumps(
                {
                    "historicals": [{"results": [{"symbol": "AAPL", "bars": []}]}],
                    "quotes": [{"results": [{"quote": {"symbol": "AAPL"}}]}],
                }
            )
        )

        test_args = [
            "morning_signal_harness.py",
            "--data-file",
            str(data_file),
            "--now",
            "2026-08-10T10:30:00",
            "--out",
            str(out_file),
        ]

        mock_result = {
            "artifacts": {
                "run_id": "test_run",
                "signal_rows": [
                    {"ticker": "AAPL", "action": "BUY", "multiplier": 1.5, "execution_status": "auto_buy_eligible"}
                ],
            }
        }

        # We need to mock MorningDCAExecutor to avoid running real logic
        with patch.object(sys, "argv", test_args):
            with patch("claude._agent_scratch.morning_signal_harness.MorningDCAExecutor") as mock_executor:
                # Setup mock
                mock_instance = MagicMock()
                mock_instance.run.return_value = mock_result
                mock_executor.return_value = mock_instance

                # Run main
                result = main()

                # Assertions
                assert result == 0
                mock_executor.assert_called_once()
                mock_instance.run.assert_called_once()

                # Check output file
                assert out_file.exists()
                out_data = json.loads(out_file.read_bytes())
                assert out_data["run_id"] == "test_run"
                assert out_data["candidate_count"] == 1
                assert out_data["candidates"][0]["ticker"] == "AAPL"

                # Check stdout
                captured = capsys.readouterr()
                assert "Wrote 1 candidate rows to" in captured.err
                assert "test_run" in captured.out
