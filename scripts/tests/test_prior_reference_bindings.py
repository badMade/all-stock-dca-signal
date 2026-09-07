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
