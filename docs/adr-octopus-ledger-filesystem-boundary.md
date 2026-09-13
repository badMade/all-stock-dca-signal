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
symlink directories and shards, nonregular shards, and shards with multiple hard links on
every path that adopts a shard this process has not created (see the amendment below).

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

## Amendment: ownership pinning on the append path (2026-09-13)

The link-count test is an *adoption* check: it establishes that a shard nobody has vouched
for yet is not a hard link to a file outside the run. Re-running it on every append also made
the ledger unusable inside a cloud-mirrored or backed-up directory. Google Drive for Desktop
mirroring a folder hard-links each freshly written file for the 7-11 seconds it spends
copying it, so a shard under active append is aliased most of the time, and a walk-forward
run inside such a tree REFUSED mid-run at whichever step happened to land in one of those
windows. Waiting for the link to clear is not an option at those durations, and the mirror
re-links after every append.

Appends now prove ownership by identity instead. A shard's first append in a run creates it
with `O_CREAT | O_EXCL`, so the pinned inode is one nothing else chose; a pre-existing shard
(a new writer instance over an earlier run's ledger) is adopted only through the unchanged
unaliased check. Every later append requires the name to resolve to that same
`(st_dev, st_ino)`, and a pinned shard opens without `O_CREAT`, so a deleted one fails closed
rather than starting a second shard no checkpoint accounts for.

For the case it covers this is stricter, not looser: a shard swapped for any other file is
refused, including an ordinary single-linked one that the link-count test accepts. What it
permits is an extra link to the inode the ledger already owns, which cannot redirect an
append -- the bytes land on the pinned inode under either name.

`resume()`, unrecorded-shard cleanup and read-only verification have no pin to trust, so they
keep the unaliased requirement and instead WAIT for it, up to `_ALIAS_SETTLE_SECONDS` (30 s;
measured, the mirror releases a quiescent shard within about 15 s). A sync agent's link goes
away when its copy finishes; a planted one is still there at the deadline and is refused with
the same error as before. Waiting on the link count alone would be a hole -- an attacker could
drop it to 1 by unlinking the LEDGER's name rather than their own, leaving the descriptor on a
file only they still name -- so every look also re-checks that the shard name still resolves to
the opened inode, and refuses immediately if it does not.

Shard enumeration keeps only the regular-file floor. It reports names, it does not act on them:
every shard it returns is opened and checked by `resume()` moments later, re-checked before an
unrecorded one is unlinked, or refuses `create()` outright by existing at all. A link count
taken there, off a directory entry with no descriptor to re-check the name against, could only
be a weaker copy of that check.
