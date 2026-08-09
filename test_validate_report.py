import json
from unittest.mock import mock_open, patch

import pytest

from validate_report import validate


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_file_not_found(mock_print, mock_exit):
    with patch('builtins.open', side_effect=FileNotFoundError):
        with pytest.raises(SystemExit):
            validate()
        mock_print.assert_called_with("Error: testing_report.json not found")
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_invalid_json(mock_print, mock_exit):
    with patch('builtins.open', mock_open(read_data='{invalid json')):
        with pytest.raises(SystemExit):
            validate()
        assert "Error: Invalid JSON format:" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_root_not_list(mock_print, mock_exit):
    with patch('builtins.open', mock_open(read_data='{"key": "value"}')):
        with pytest.raises(SystemExit):
            validate()
        mock_print.assert_called_with("Error: Root element is not a JSON array")
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_item_not_dict(mock_print, mock_exit):
    with patch('builtins.open', mock_open(read_data='["string item"]')):
        with pytest.raises(SystemExit):
            validate()
        mock_print.assert_called_with("Error: Item 0 is not a JSON object")
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_missing_field(mock_print, mock_exit):
    data = [{"passed": True, "duration": 1.5}]  # Missing 'suite'
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        mock_print.assert_called_with("Error: Item 0 missing required field 'suite'")
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_wrong_type(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": "yes", "duration": 1.5}]  # 'passed' should be bool
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        assert "has wrong type" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_wrong_duration_type(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": True, "duration": "1.5"}]  # 'duration' should be int or float
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        assert "has wrong type" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_success(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": True, "duration": 1.5}]
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        mock_print.assert_called_with("Validation successful")
        mock_exit.assert_called_with(0)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_duration_bool(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": True, "duration": True}]
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        assert "has wrong type" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_nan(mock_print, mock_exit):
    with patch('builtins.open', mock_open(read_data='[{"suite": "tests", "passed": true, "duration": NaN}]')):
        with pytest.raises(SystemExit):
            validate()
        assert "Invalid JSON format" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_duration_overflow_to_infinity(mock_print, mock_exit):
    read_data = '[{"suite": "tests", "passed": true, "duration": 1e400}]'
    with patch('builtins.open', mock_open(read_data=read_data)):
        with pytest.raises(SystemExit):
            validate()
        assert "must be a finite non-negative number" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_invalid_utf8(mock_print, mock_exit):
    with patch('builtins.open', mock_open(read_data=b'\xff')):
        with pytest.raises(SystemExit):
            validate()
        assert "Invalid JSON format" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_negative_duration(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": True, "duration": -1}]
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        assert "must be a finite non-negative number" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_negative_duration_float(mock_print, mock_exit):
    data = [{"suite": "tests", "passed": True, "duration": -1.5}]
    with patch('builtins.open', mock_open(read_data=json.dumps(data))):
        with pytest.raises(SystemExit):
            validate()
        assert "must be a finite non-negative number" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_duplicate_key(mock_print, mock_exit):
    read_data = '[{"suite": "tests", "passed": false, "passed": true, "duration": 1.5}]'
    with patch('builtins.open', mock_open(read_data=read_data)):
        with pytest.raises(SystemExit):
            validate()
        assert "Invalid JSON format" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_unreadable_path(mock_print, mock_exit):
    with patch('builtins.open', side_effect=IsADirectoryError("[Errno 21] Is a directory: 'reports'")):
        with pytest.raises(SystemExit):
            validate('reports')
        assert "Unable to read reports" in mock_print.call_args[0][0]
        mock_exit.assert_called_with(1)


@patch('sys.exit', side_effect=SystemExit)
@patch('builtins.print')
def test_validate_custom_filepath(mock_print, mock_exit):
    with patch('builtins.open', side_effect=FileNotFoundError) as mock_file:
        with pytest.raises(SystemExit):
            validate('custom_report.json')
        mock_file.assert_called_with('custom_report.json', 'rb')
        mock_print.assert_called_with("Error: custom_report.json not found")
        mock_exit.assert_called_with(1)
