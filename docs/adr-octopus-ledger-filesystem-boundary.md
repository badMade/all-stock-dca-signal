# Octopus ledger filesystem boundary

Status: accepted for the ledger recovery security fix.

## Context

A supplied run directory can contain a ledger shard symlink to a file outside that run.
The old resume implementation followed the link while verifying a checkpoint prefix, then
reopened its pathname for truncation. Matching checkpoint metadata did not establish that
the opened file belonged to the ledger. A pathname replacement between hashing and
truncation could also redirect the write. Unrecorded-shard cleanup and later append calls
used the same mutable directory paths.

The repository had no descriptor-based no-follow helper suitable for mutation. Its
resolve-and-check readers protect ordinary read paths but cannot pin a file across a
validation-to-write transition. The harness's advisory lock serializes cooperating writers;
it does not make supplied filesystem aliases trustworthy.

## Decision

Keep the implementation local to `wondrous-octopus/ledger.py`, using only the standard
library. Treat the operator's selected run directory as the root of authority; resolve that
selection once when constructing the ledger and open its cached canonical components without
following replacements. Create missing components relative to pinned parents. Pin the ledger
and model directories using file descriptors. Below the selected run directory, refuse
symlink directories and shards, nonregular shards, and shards with multiple hard links.

Validate checkpoint keys as portable `model/kind_YYYY.jsonl.gz` paths and validate sizes and
SHA-256 representations before recovery. Limit enumeration to model directories and their
shards, refusing deeper directory nesting. Hash the checkpoint prefix and truncate through
the same open, checked descriptor. Remove unrecorded shards relative to a pinned model
directory. Apply the same opening rules to create/append so a resumed writer cannot bypass
the recovery boundary, and share the read-only checkpoint prefix verification routine.

The boundary prevents supplied aliases and pathname swaps from redirecting operations to
an independently opened outside file. It is not a substitute for filesystem permissions:
an actor able to modify an already-open inode directly can still corrupt its contents.
Checkpoint fingerprints remain corruption checks, not signatures authenticating a producer.

## Compatibility and alternatives

Checkpoint keys and gzip bytes are unchanged. Longer valid shards still lose only their
uncheckpointed tails, subsequent appends continue from the verified prefix, and entirely
uncheckpointed ordinary shards are still removed. Read-only verification never performs
recovery writes. A run directory alias chosen by the operator is accepted; aliases inside
its ledger are refused.

Python's `dir_fd` operations are Unix-only, and `O_NOFOLLOW`/`O_DIRECTORY` are platform
extensions. Platforms missing these primitives, including Windows, fail closed with
`LedgerError` before ledger creation, append, resume, or checkpoint verification. General
read-only report and inspection APIs are unchanged. Supporting secure Windows mutation
requires a separately reviewed native handle/reparse-point implementation; a `Path.open()`
fallback would restore the flaw. No runtime dependency is introduced.

Checking `is_symlink()` or `resolve()` before a separate write was rejected because a
pathname can change afterward. Leaf-only `O_NOFOLLOW` was rejected because a parent can
redirect both recovery and cleanup. Disabling crash-tail truncation was rejected because
deterministic recovery is a documented public contract.

References: [Python descriptor-relative operations](https://docs.python.org/3/library/os.html#os.supports_dir_fd),
[platform-specific open flags](https://docs.python.org/3/library/os.html#os.O_NOFOLLOW).

## Verification

`wondrous-octopus/tests/test_ledger_security.py` uses only disposable test directories to
exercise supplied file/directory symlinks, hard links, FIFO replacement, path and size
validation, swaps before open and after hashing, cleanup-parent replacement, subsequent
append, and missing platform capabilities. Existing ledger and harness tests preserve
crash recovery, missing/tampered shard refusal, checkpoint key compatibility, and
resume-equals-uninterrupted behavior. No tests use the real ticker archive or send orders.
