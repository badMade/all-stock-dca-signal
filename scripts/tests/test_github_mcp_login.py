"""Tests for scripts/github_mcp_login.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts import github_mcp_login


def test_main_missing_binary(monkeypatch, capsys) -> None:
    with patch("pathlib.Path.exists", return_value=False):
        code = github_mcp_login.main()
        assert code == 1
        captured = capsys.readouterr().err
        assert "not found" in captured


@patch("scripts.github_mcp_login.subprocess.Popen")
def test_main_successful_login(mock_popen) -> None:
    proc = MagicMock()
    # Sequence of output lines: startup logs -> browser opened -> tool response JSON
    proc.stdout.readline.side_effect = [
        "time=... level=INFO msg=\"server connecting\"\n",
        "time=... level=INFO msg=\"opened browser for GitHub authorization\"\n",
        '{"id": 2, "result": {"login": "testuser"}}\n',
    ]
    mock_popen.return_value = proc

    with patch("pathlib.Path.exists", return_value=True):
        code = github_mcp_login.main()
        assert code == 0
        proc.terminate.assert_called_once()
