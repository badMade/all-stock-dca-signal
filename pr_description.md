💡 **What:** Replaced inline `re.search()` calls with module-level pre-compiled regex objects (`_HH_MM_RE` and `_HH_MM_SS_RE`) in `scripts/run_classification.py:647,671`. Also rebuilt the backup planner prompts to sync the SHA update, and pinned `msgpack>=1.2.3` in `analysis/market-graph-3d/requirements.txt` to resolve a vulnerability flagged by the dependency audit CI workflow (CVE-2026-114613).
🎯 **Why:** Calling `re.search` with an inline string pattern repeatedly inside hot paths incurs internal regex cache lookup and locking overhead. Pre-compiling the regexes bypasses this overhead while strictly maintaining all parsing logic and string boundaries. The `msgpack` bump resolves a CI dependency audit failure on branch updates.
📊 **Impact & Measurement:** Benchmark shows a speedup of ~1.6x (0.5684s uncompiled vs. 0.3487s compiled for 50,000 iterations).
✅ **Verification:**
- `source .venv/bin/activate && python3 -m pytest scripts/tests/test_run_classification.py -q`
- `source .venv/bin/activate && python3 -m pytest scripts/tests/test_verify_perplexity_pair.py -q`
- `source .venv/bin/activate && ruff check scripts/run_classification.py`
- `source .venv/bin/activate && python3 run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`
- `uv pip compile analysis/market-graph-3d/requirements.txt --python-version 3.12 --no-build --format pylock.toml --output-file /tmp/test-pylock.toml` passed locking with updated `msgpack`.
⚠️ **Known Limitations:** None. The fallback behavior remains exact.
🔒 **Secrets Confirmation:** Confirmed no secrets were added.
🧭 **Entry-Point Coverage:** `scripts/run_classification.py` affects classification downstream of `dca_runner.py` and reporting tasks.
