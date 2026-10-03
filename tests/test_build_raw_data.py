import json
import subprocess
import sys
from pathlib import Path


def test_build_raw_data(tmp_path):
    script = Path(__file__).resolve().parents[1] / "build_raw_data.py"
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    # Verify the file was created in the temporary directory
    output_file = tmp_path / "market_open_raw_data.json"
    assert output_file.exists()

    # Load and verify the JSON contents
    data = json.loads(output_file.read_bytes())

    assert set(data) == {"daily", "minute"}

    # Ensure dictionaries are populated
    assert len(data["daily"]) > 0
    assert len(data["minute"]) > 0

    # Ensure both dicts have the exact same set of tickers
    assert set(data["daily"].keys()) == set(data["minute"].keys())

    required_keys = {"begins_at", "open_price", "close_price", "high_price", "low_price", "volume", "session"}
    for ticker in data["daily"]:
        ticker_daily_data = data["daily"][ticker]
        assert isinstance(ticker_daily_data, list)
        assert len(ticker_daily_data) == 1
        daily_point = ticker_daily_data[0]
        assert set(daily_point.keys()) == required_keys

        ticker_minute_data = data["minute"][ticker]
        assert isinstance(ticker_minute_data, list)
        assert len(ticker_minute_data) == 1
        minute_point = ticker_minute_data[0]
        assert set(minute_point.keys()) == required_keys

        assert isinstance(daily_point["begins_at"], str)
        assert isinstance(daily_point["open_price"], str)
        assert isinstance(daily_point["volume"], int)
        assert isinstance(minute_point["begins_at"], str)
        assert isinstance(minute_point["open_price"], str)
        assert isinstance(minute_point["volume"], int)
