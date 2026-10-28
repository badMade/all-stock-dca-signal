## 2026-10-15 - [Avoid datetime.strptime in tight loops]
**Learning:** `datetime.strptime` is remarkably slow in Python because it involves regex parsing and locale lock overhead. When dealing with strict, fixed-width date formats (e.g. `YYYYMMDD`) in a tight loop, it is ~7x faster to use native string slicing and the `datetime.date` constructor.
**Action:** Use native string slicing over `datetime.strptime` for rigid date schemas in tight loops to maximize performance without sacrificing safety, as `int()` and `date()` both natively raise `ValueError` on bad inputs, effectively mirroring `strptime`'s error handling.

## 2026-10-15 - [Avoid Path.resolve() in iterative loops]
**Learning:** `Path.resolve()` executes expensive, synchronous POSIX `stat`/`lstat` system calls across the filesystem. When placed inside tight loops or `rglob` scanners (like directory-walking logic), it forces $O(N \times M)$ overhead, introducing severe disk I/O latency.
**Action:** When evaluating exclusion paths inside loops, always pre-compute a set of resolved paths (e.g., `covered_resolved = {p.resolve() for p in self.covered_files}`) outside the loop. Then, resolve candidate loop objects exactly once and use an $O(1)$ set lookup (`if candidate.resolve() in covered_resolved`).
## 2026-10-16 - [Cache Path.resolve() in nested loops]
**Learning:** Calling `Path.resolve()` repeatedly inside nested loops (such as correlating N run state paths with M historical purchase records) triggers expensive O(N x M) OS-level filesystem resolution syscalls. This causes severe runtime degradation on large file counts.
**Action:** Replace repeated inline path resolutions with function-scoped upfront resolution caching (e.g., via dictionary lookup or memoization) to flatten the operation to O(N + M). This pattern ensures fast traversal in historical log snapshots while maintaining identical logic parity.
## 2026-10-19 - [Avoid Path.rglob().stat() for max mtime queries]
**Learning:** Chaining `Path.rglob()` and `Path.stat()` causes $O(N)$ system calls for large directory trees because `rglob` constructs path objects before making individual `lstat` calls.
**Action:** Use `os.scandir` with a manual stack to yield directory entries directly and use `entry.stat().st_mtime` to eliminate repeated system I/O bounds while scanning directories.

## 2026-10-21 - [Avoid uncompiled re.match in inner loops]
**Learning:** Python's internal regex cache (`re._cache`) caps out and incurs hash-lookup overhead per call. Uncompiled `re.match` inside a loop over rows or files causes performance sink.
**Action:** Lift the pattern to a module-level `_PATTERN = re.compile(r"...")` and call `_PATTERN.match(...)` for ~2.3x speedup.

## 2026-10-22 - [Use fromisoformat or string slicing to bypass strptime in hot loops]
**Learning:** `datetime.strptime` involves regex compilation and locale locking, which makes it remarkably slow for fixed-format dates and times in hot loops (e.g. log ingestion, time window validation). Using `datetime.fromisoformat()` with timezone substitution or explicit string slicing is >5x faster and respects strict formatting with identical `ValueError` handling.
**Action:** Always prefer `fromisoformat` (or `int()` slicing for simple clock times like `%H:%M`) over `strptime` to eliminate CPU overhead inside data ingestion loops.
## 2026-10-23 - [Hoist uncompiled re.search to compiled regex]
**Learning:** Inline `re.search` involves overhead by maintaining caching on the `re` module.
**Action:** When searching regex patterns (e.g., matching AM/PM fields in `dca_console_data.py`), hoist the regex into a global `re.compile` instance to avoid the overhead of the cache lookups.
## 2026-10-27 - [Avoid uncompiled re.search in hot paths]
**Learning:** Calling `re.search` with an inline string pattern repeatedly incurs internal regex cache lookup and locking overhead. In `scripts/run_classification.py`'s `_et_minutes` and `_et_seconds` time parsing logic, replacing inline `re.search` with a module-level pre-compiled `re.compile()` object yielded a ~1.6x speedup while exactly preserving all safety and fallback behavior (including tolerance for unexpected suffixes or decimals).
**Action:** Always pre-compile regexes at the module level when calling them inside iterative loops or parsing hot paths to maximize performance without sacrificing exact behavioral parity.
