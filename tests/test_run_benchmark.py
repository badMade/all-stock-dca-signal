import pytest

from run_benchmark import (
    benchmark_dict_reader,
    benchmark_reader_custom_dict,
    parse_dict_reader,
    parse_reader_custom_dict,
)

# Happy path data
VALID_HEADER = "ticker,model,origin_date,pred_close,pred_open,pred_high,pred_low,close_p10,close_p50,close_p90,open_p10,open_p50,open_p90,high_p10,high_p50,high_p90,low_p10,low_p50,low_p90,target_date,ref_close,extra1,extra2\n"
VALID_ROW = "AAPL,model1,2023-01-01,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0,2023-01-01,150.0,extra,extra\n"
VALID_CSV = VALID_HEADER + (VALID_ROW * 10)


def test_parsing_correctness():
    """Verify that the custom reader extracts the exact same data as DictReader for our fields."""
    dict_reader_output = parse_dict_reader(VALID_CSV)
    custom_reader_output = parse_reader_custom_dict(VALID_CSV)

    assert len(dict_reader_output) == len(custom_reader_output)

    for dr_row, cr_row in zip(dict_reader_output, custom_reader_output):  # noqa: B905
        # We only care about the fields custom reader explicitly parses.
        # Check that the common keys map to the same values.
        for key, cr_val in cr_row.items():
            assert dr_row.get(key) == cr_val


def test_benchmark_dict_reader_happy_path():
    result = benchmark_dict_reader(VALID_CSV)
    assert isinstance(result, float)
    assert result >= 0.0


def test_benchmark_reader_custom_dict_happy_path():
    result = benchmark_reader_custom_dict(VALID_CSV)
    assert isinstance(result, float)
    assert result >= 0.0


def test_benchmark_dict_reader_empty_string():
    result = benchmark_dict_reader("")
    assert isinstance(result, float)
    assert result >= 0.0


def test_benchmark_reader_custom_dict_empty_string():
    # Empty string should not raise StopIteration anymore since we use next(..., None)
    result = benchmark_reader_custom_dict("")
    assert isinstance(result, float)
    assert result >= 0.0


@pytest.mark.parametrize("csv_data", [
    "ticker\nAAPL\n", # Missing columns
    "ticker,model\nAAPL\n", # Ragged row
])
def test_benchmark_reader_custom_dict_ragged_rows(csv_data):
    """Verify benchmark handles missing columns or ragged rows without crashing."""
    result = benchmark_reader_custom_dict(csv_data)
    assert isinstance(result, float)
    assert result >= 0.0


def test_benchmark_dict_reader_malformed_syntax():
    """Verify strict=True raises csv.Error on unescaped quotes inside quoted fields, etc."""
    import csv
    bad_csv = 'ticker,model\n"AAPL"foo,model1\n' # Bad quote
    with pytest.raises(csv.Error):
        parse_dict_reader(bad_csv)


def test_benchmark_reader_custom_dict_malformed_syntax():
    """Verify strict=True raises csv.Error on unescaped quotes."""
    import csv
    bad_csv = 'ticker,model\n"AAPL"foo,model1\n' # Bad quote
    with pytest.raises(csv.Error):
        parse_reader_custom_dict(bad_csv)
