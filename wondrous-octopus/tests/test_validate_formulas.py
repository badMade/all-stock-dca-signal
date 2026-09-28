from __future__ import annotations

import io
import sys

import numpy as np
import pytest

import validate_formulas


@pytest.fixture(autouse=True)
def clean_failures():
    """Ensure FAILURES is clear before and after each test."""
    validate_formulas.FAILURES.clear()
    yield
    validate_formulas.FAILURES.clear()


def test_check_passes():
    """Test check() on identical arrays."""
    got = np.array([1.0, 2.0, 3.0])
    expected = np.array([1.0, 2.0, 3.0])
    validate_formulas.check("identical", got, expected, tol=1e-5)
    assert not validate_formulas.FAILURES


def test_check_fails_on_deviation():
    """Test check() catches arrays differing beyond the tolerance."""
    got = np.array([1.0, 2.0, 3.0])
    expected = np.array([1.0, 2.1, 3.0])
    validate_formulas.check("deviation", got, expected, tol=1e-5)
    assert "deviation" in validate_formulas.FAILURES


def test_check_fails_on_nan_mismatch():
    """Test check() catches mismatched NaNs even if tolerance is met."""
    got = np.array([1.0, np.nan, 3.0])
    expected = np.array([1.0, 2.0, 3.0])
    validate_formulas.check("nan_mismatch", got, expected, tol=1e-5)
    assert "nan_mismatch" in validate_formulas.FAILURES


def test_check_fails_on_shape_mismatch():
    """Test check() catches shape mismatches."""
    got = np.array([1.0, 2.0, 3.0])
    expected = np.array([1.0, 2.0])
    validate_formulas.check("shape_mismatch", got, expected, tol=1e-5)
    assert "shape_mismatch" in validate_formulas.FAILURES


def test_synthetic_frame_generator():
    """Test synthetic_frame produces expected DataFrame shapes."""
    df = validate_formulas.synthetic_frame(n=100)
    assert len(df) == 100
    expected_cols = {"Date", "Open", "High", "Low", "Close", "Adj Close", "Dividends", "Stock Splits", "Volume"}
    assert set(df.columns).issuperset(expected_cols)


def test_main_executes_cleanly(monkeypatch):
    """Test the main block executes to completion when all formulas are correct."""
    # Ensure it doesn't sys.exit(1) due to left-over failures
    validate_formulas.FAILURES.clear()

    # Capture stdout so we don't spam the test output
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    validate_formulas.main()

    output = captured.getvalue()
    assert "RESULT: all checks passed" in output
    assert not validate_formulas.FAILURES


def test_main_exits_on_failure(monkeypatch):
    """Test the main block exits with code 1 if failures were recorded."""
    validate_formulas.FAILURES.clear()

    # Capture stdout
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    # We monkeypatch check to forcefully record a failure
    def fake_check(label, got, expected, *, tol, note=""):
        validate_formulas.FAILURES.append("forced_failure")
        return

    monkeypatch.setattr(validate_formulas, "check", fake_check)

    with pytest.raises(SystemExit) as exc_info:
        validate_formulas.main()

    assert exc_info.value.code == 1
    output = captured.getvalue()
    assert "CHECK(S) FAILED" in output  # Since check is called multiple times
    assert "forced_failure" in output
    validate_formulas.FAILURES.clear()
