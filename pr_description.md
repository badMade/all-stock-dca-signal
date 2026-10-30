### Summary of changes
- Addressed a Critical JSON Parameter Pollution vulnerability flagged in `.jules/sentinel.md` where `json.loads` calls in `dca_console_state.py` and `dca_console_runs.py` silently allowed duplicate keys (which resolves to the last parsed value by default in Python).
- Updated the vulnerable `json.loads` calls (`dca_console_state._read_object`, `dca_console_runs._parsable_json`, and `dca_console_runs._post_trade`) to enforce `object_pairs_hook=_reject_duplicate_keys` to ensure duplicate keys safely fail closed and return `"corrupt"` / `False` respectively.
- Refactored `dca_console_runs.py` and `dca_console_state.py` to use a consistent locally defined `_reject_duplicate_keys` hook to avoid circular imports.
- Created `test_read_object_rejects_duplicate_keys` and `test_post_trade_rejects_duplicate_keys` tests to strictly enforce duplicate-key rejection behavior.

### Exact test commands run
- `source .venv/bin/activate && python -m pytest tests/test_dca_console_state.py tests/test_dca_console_runs.py -q`
- `source .venv/bin/activate && ruff check .`
- `source .venv/bin/activate && python -m pytest -q`
- `source .venv/bin/activate && python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`

### Known limitations/proposed workflow diffs
- None.

### Confirmation of no secrets added
- No secrets were added.

### Entry-point coverage statement
- `run_dca.py` tests correctly passed, ensuring safe fallback states inside the console pipelines.

### Findings Table
| Severity | File:Line | Issue | Entry Point | Confidence % | Minimal Fix |
|---|---|---|---|---|---|
| CRITICAL | `dca_console_state.py:322` | Parameter Pollution via Duplicate JSON Keys allows silent logic override | Console Reader / Runs | 96% | Pass `object_pairs_hook=_reject_duplicate_keys` |
| HIGH | `dca_console_runs.py:487`, `560` | Parameter Pollution via Duplicate JSON Keys | Console Reader / Runs | 96% | Pass `object_pairs_hook=_reject_duplicate_keys` |
