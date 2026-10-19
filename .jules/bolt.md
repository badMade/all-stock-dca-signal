## 2026-10-15 - [Avoid datetime.strptime in tight loops]
**Learning:** `datetime.strptime` is remarkably slow in Python because it involves regex parsing and locale lock overhead. When dealing with strict, fixed-width date formats (e.g. `YYYYMMDD`) in a tight loop, it is ~7x faster to use native string slicing and the `datetime.date` constructor.
**Action:** Use native string slicing over `datetime.strptime` for rigid date schemas in tight loops to maximize performance without sacrificing safety, as `int()` and `date()` both natively raise `ValueError` on bad inputs, effectively mirroring `strptime`'s error handling.

## 2026-10-16 - [Cache Path.resolve() in nested loops]
**Learning:** Calling `Path.resolve()` repeatedly inside nested loops (such as correlating N run state paths with M historical purchase records) triggers expensive O(N x M) OS-level filesystem resolution syscalls. This causes severe runtime degradation on large file counts.
**Action:** Replace repeated inline path resolutions with function-scoped upfront resolution caching (e.g., via dictionary lookup or memoization) to flatten the operation to O(N + M). This pattern ensures fast traversal in historical log snapshots while maintaining identical logic parity.
