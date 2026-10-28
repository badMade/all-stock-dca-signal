💡 **What:** Replaced `datetime.strptime` with native string slicing for fixed clock times (in `scripts/ingest_market_data.py`) and with `datetime.fromisoformat()` for UTC timestamps (in `scripts/model_usage_log.py`, `scripts/generate_credit_usage_proxy_report.py`, `scripts/workspace_probe.py`, and `scripts/tests/test_run_classification.py`).

🎯 **Why:** `datetime.strptime` is remarkably slow in Python because it involves regex parsing and locale lock overhead. When dealing with strict, fixed-width formats inside data ingestion loops or script initializations, we can bypass this bottleneck entirely.

📊 **Impact & Measurement:**
Before:
`old: 1.0338021360000198` (for "%H:%M")
`old: 1.3935956239999996` (for ISO with 'Z')
After:
`new: 0.11754236300001253` (for "%H:%M")
`new: 0.04945816900001887` (for ISO with 'Z')
Speedup: ~9x for "%H:%M" via string split, ~28x for ISO format via `fromisoformat`.

✅ **Verification:**
- Ran targeted tests: `.venv/bin/python -m pytest scripts/tests/test_ingest_market_data.py scripts/tests/test_model_usage_log.py scripts/tests/test_generate_credit_usage_proxy_report.py scripts/tests/test_workspace_probe.py scripts/tests/test_run_classification.py -q`
- Passed linter check: `ruff check .`
- Smoke test: `.venv/bin/python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`

⚠️ **Known Limitations:** None. Safety boundaries such as `ValueError` for invalid dates and components are natively upheld by `fromisoformat` and integer parsing.

🔒 **Secrets Confirmation:** Confirmed no secrets were added.

🧭 **Entry-Point Coverage:** The changes affect `scripts/` initializations and parsing utilities heavily used during scheduled job artifacts gathering and telemetry generation.
