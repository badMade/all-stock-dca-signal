import json
from datetime import date

from scripts.prior_reference_bindings import load_reference_bindings, main


def _write_filled_state(root, *, order_id="order-1", target_price=98.5):
    run_dir = root / "20260903_0940ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-09-03",
                "generated_at": "2026-09-03T09:40:00-04:00",
                "run_classification": {"run_origin": "manual", "run_id": "manual-0940"},
                "buy_candidates": [
                    {
                        "ticker": "AAPL",
                        "status": "filled",
                        "order_id": order_id,
                        "planned_amount_usd": 5.0,
                        "target_price": target_price,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_load_reference_bindings_returns_logged_price_with_owning_order(tmp_path):
    _write_filled_state(tmp_path)

    result = load_reference_bindings([tmp_path], date(2026, 9, 3))

    assert result["verdict"] == "PROCEED"
    assert result["bindings"]["AAPL"] == {
        "amount_usd": 5.0,
        "reference_price": 98.5,
        "order_id": "order-1",
        "resolved": True,
    }


def test_load_reference_bindings_fails_closed_on_missing_root(tmp_path):
    result = load_reference_bindings([tmp_path / "missing"], date(2026, 9, 3))

    assert result["verdict"] == "ABORT"
    assert result["scan_complete"] is False


def test_cli_prints_abort_and_exits_two_for_missing_root(tmp_path, capsys):
    exit_code = main(
        ["--trading-day", "2026-09-03", "--root", str(tmp_path / "missing")]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "ABORT"


# --- optional roots -----------------------------------------------------------------
# A root that legitimately does not exist most days (e.g. final_manual_review, which
# cloud_preflight does not create) must not abort the scan, but only when the caller
# opts that specific root in. Every --root stays strict: a lost mount or an unpulled
# evidence clone must still read as an incomplete scan.


def test_absent_optional_root_does_not_block_proceed(tmp_path):
    _write_filled_state(tmp_path / "stream")
    absent = tmp_path / "final_manual_review"

    result = load_reference_bindings(
        [tmp_path / "stream"], date(2026, 9, 3), optional_roots=[absent]
    )

    assert result["verdict"] == "PROCEED"
    assert result["scan_complete"] is True
    assert result["absent_optional_roots"] == [str(absent)]
    assert result["missing_roots"] == []
    assert result["bindings"]["AAPL"]["resolved"] is True


def test_absent_strict_root_still_aborts_when_an_optional_root_is_also_absent(tmp_path):
    strict_missing = tmp_path / "morning_dca"

    result = load_reference_bindings(
        [strict_missing], date(2026, 9, 3), optional_roots=[tmp_path / "final_manual_review"]
    )

    assert result["verdict"] == "ABORT"
    assert result["scan_complete"] is False
    assert result["missing_roots"] == [str(strict_missing)]


def test_optional_root_that_is_a_file_aborts(tmp_path):
    (tmp_path / "stream").mkdir()
    not_a_dir = tmp_path / "final_manual_review"
    not_a_dir.write_text("not a directory", encoding="utf-8")

    result = load_reference_bindings(
        [tmp_path / "stream"], date(2026, 9, 3), optional_roots=[not_a_dir]
    )

    assert result["verdict"] == "ABORT"
    assert result["scan_complete"] is False
    assert result["unreadable_optional_roots"] == [str(not_a_dir)]


def test_unreadable_artifact_in_a_root_still_aborts_with_optional_roots_present(tmp_path):
    stream = tmp_path / "stream"
    bad_run = stream / "20260903_0940ET"
    bad_run.mkdir(parents=True)
    (bad_run / "run_state.json").write_text("{not json", encoding="utf-8")

    result = load_reference_bindings(
        [stream], date(2026, 9, 3), optional_roots=[tmp_path / "final_manual_review"]
    )

    assert result["verdict"] == "ABORT"
    assert result["scan_complete"] is False
    assert result["skipped_artifacts"]


def test_evidence_in_an_existing_optional_root_resolves_like_a_strict_root(tmp_path):
    (tmp_path / "stream").mkdir()
    optional = tmp_path / "final_manual_review"
    _write_filled_state(optional)

    result = load_reference_bindings(
        [tmp_path / "stream"], date(2026, 9, 3), optional_roots=[optional]
    )

    assert result["verdict"] == "PROCEED"
    assert result["absent_optional_roots"] == []
    assert result["bindings"]["AAPL"] == {
        "amount_usd": 5.0,
        "reference_price": 98.5,
        "order_id": "order-1",
        "resolved": True,
    }


def test_optional_roots_default_keeps_existing_output_shape(tmp_path):
    result = load_reference_bindings([tmp_path], date(2026, 9, 3))

    assert result["verdict"] == "PROCEED"
    assert result["absent_optional_roots"] == []
    assert result["unreadable_optional_roots"] == []


def test_cli_exits_zero_for_absent_optional_root(tmp_path, capsys):
    exit_code = main(
        [
            "--trading-day", "2026-09-03",
            "--root", str(tmp_path),
            "--optional-root", str(tmp_path / "final_manual_review"),
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["verdict"] == "PROCEED"
    assert out["absent_optional_roots"] == [str(tmp_path / "final_manual_review")]


def test_cli_still_exits_two_for_absent_strict_root_alongside_optional_root(tmp_path, capsys):
    exit_code = main(
        [
            "--trading-day", "2026-09-03",
            "--root", str(tmp_path / "missing"),
            "--optional-root", str(tmp_path / "final_manual_review"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "ABORT"
