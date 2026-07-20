## 2026-07-17 - Iterating Pandas Dataframes fast
**Learning:** `iterrows()` is extremely slow for Pandas dataframe iterations.
**Action:** Prefer `itertuples(name=None, index=False)` for fast read-only iteration (use `index=True` if you need the index), and `to_dict('records')` when per-row mutation or dict-style column access is required (e.g. non-standard column names like numeric headers).

## 2026-06-29 - [Pandas Iteration Performance]
**Learning:** `iterrows()` is extremely slow for iterating over DataFrames because it creates a Series object for each row. `itertuples()` is much faster for read-only operations as it returns namedtuples. For cases where row mutability is needed or column names are not valid identifiers (like "1x"), `to_dict('records')` offers a massive speedup over `iterrows()` while preserving dict-like access.
**Action:** Always prefer `itertuples()` for read-only iteration and `to_dict('records')` for mutable/dict-access iteration over pandas DataFrames. Never use `iterrows()` unless strictly necessary for type preservation across columns.

## 2025-01-20 - Fast JSON filtering
**Learning:** `rglob` followed by `json.loads` over a large number of historical artifact files is expensive. We can skip the expensive JSON parse by doing a fast substring check (`target_date_iso in raw_text`) before parsing.
**Action:** When scanning many JSON files for a specific value (like a specific trading day), read the file as a raw string and check for the substring before decoding it.

## 2023-10-27 - Pandas iteration bottleneck in analysis tools
**Learning:** Pandas `iterrows()` is extremely slow for large DataFrames because it constructs a new `Series` object for every row. `itertuples()` or `to_dict('records')` are significantly faster (3.5x to 15x in micro-benchmarks). `to_dict('records')` is particularly useful when row mutability is needed, as it avoids `SettingWithCopyWarning` and object overhead. `itertuples(index=True)` combined with positional unpacking (`row[1:]`) is safest when column names might not be valid Python identifiers.
**Action:** When working with Pandas loops in this codebase (especially in the heavier `analysis/` or `trading-strategies/` directories), default to `itertuples()` for read-only scans and `to_dict('records')` for mutable processing steps.
