"""Atomic file-write helpers for run artifacts and cross-run pointer files.

Artifact and pointer writes must never leave a torn (partially-written) file on
disk: readers of ``run_state.json`` and the ``latest_*_run.json`` pointers
deliberately degrade on parse errors, so a torn write silently erases prior-run
context (e.g. the same-day allocation guard's view of earlier fills) instead of
crashing. Writing to a temp file in the destination directory and swapping it in
with :func:`os.replace` makes every write all-or-nothing.

Standard library only; imports nothing from the rest of the project so any
module (including ``scripts/*``) can use it without cycles.
"""

from __future__ import annotations

import csv
import os
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Dict, Generator, Iterator, NamedTuple, Sequence
from uuid import uuid4


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``).

    The temp file is created in ``path``'s directory so the final rename never
    crosses a filesystem boundary. On any failure the temp file is removed and
    the previous contents of ``path`` (if any) are left untouched.
    """
    with atomic_open_w(path, encoding=encoding) as handle:
        handle.write(text)


# Retry budget for temp-file name collisions; a uuid4 collision is already
# vanishingly unlikely, so this only bounds a pathological loop.
_TEMP_NAME_ATTEMPTS = 5


@contextmanager
def atomic_open_w(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Iterator[IO[str]]:
    """Context manager yielding a text handle whose contents replace ``path`` atomically.

    Drop-in for ``path.open("w", ...)`` in artifact writers (including
    ``csv.writer`` targets — pass ``newline=""`` exactly as with ``open``). The
    data is flushed and fsynced before the swap, and the parent directory is
    fsynced after it, so after a successful exit the destination holds the
    complete new contents (and keeps holding them across a host crash — without
    the directory fsync the rename itself could be lost on power failure and an
    old ``run_state.json``/pointer could reappear). On an exception inside the
    ``with`` block the destination is untouched.

    The file lands with normal umask-derived permissions, not ``mkstemp``'s
    owner-only ``0600`` — ``os.replace`` keeps the temp file's mode, so a temp
    file created at 0600 would make every artifact this repo writes unreadable
    by a reporting or monitoring process running as a different user.
    """
    target = Path(path)
    # Read the destination's mode BEFORE the temp file is created, and apply it
    # BEFORE any content is written. Doing it after the write left a real
    # exposure window: replacing a 0600 artifact under the common 022 umask
    # created the temp at 0644, so the complete sensitive contents were readable
    # by any other local user through the visible *.tmp file for the whole
    # duration of the write and fsync -- even though the finished file went back
    # to 0600 (Codex review, PR #219).
    existing = _target_identity(target)
    fd, tmp_path = _create_temp_file(target)
    # Until os.fdopen() takes ownership, nothing else will ever close this
    # descriptor: the cleanup below unlinks the temp PATH, which does not free
    # the open fd. A caller that catches and retries failed artifact writes
    # therefore leaked one descriptor per attempt and could eventually fail
    # unrelated I/O with EMFILE (Codex review, PR #219).
    try:
        if existing is not None:
            mode, uid, gid = existing.mode, existing.uid, existing.gid
            if not _preserve_ownership(fd, uid, gid):
                # Abort rather than degrade. Reducing the mode to owner-only kept
                # the replacement from reaching the WRONG identities, but it
                # still stripped access from the RIGHT ones: a 0640 artifact
                # owned by svc-a:monitoring came back 0600 owned by the writer,
                # so both the owning service and its monitoring group silently
                # lost the access the in-place open(path, "w") this replaces
                # would have preserved (Codex review, PR #219).
                #
                # This fires only when the destination's uid/gid differ from the
                # temp file's AND they cannot be reproduced, which does not
                # happen when a stream writes artifacts it owns. When it does
                # fire it is a real deployment mismatch, and a loud failure
                # before any content is written beats silent permission drift on
                # a trading artifact.
                raise OSError(
                    f"refusing to replace {target}: it is owned by uid={uid} "
                    f"gid={gid}, which this process cannot reproduce on the "
                    f"replacement, so the atomic swap would change the file's "
                    f"owner and revoke access the current mode grants. Run the "
                    f"writer as the owning user, add it to the owning group, or "
                    f"set the setgid bit on the directory."
                )
            if not _set_mode(fd, tmp_path, mode):
                _refuse_if_mode_differs(fd, target, mode)
        handle = os.fdopen(fd, "w", encoding=encoding, newline=newline)
    except BaseException:
        # os.fdopen takes ownership even when it then fails (an unknown encoding,
        # say), and closes the descriptor on its way out -- so an unconditional
        # close here raised EBADF, replacing the caller's real exception and
        # skipping the temp-file cleanup entirely (Codex review, PR #219).
        try:
            os.close(fd)
        except OSError:
            pass
        _discard(tmp_path)
        raise

    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        _refuse_if_destination_changed(target, existing)
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    except BaseException:
        # The original destination file is never touched. ``handle`` owns the
        # descriptor from here on, and ``with`` has already closed it.
        _discard(tmp_path)
        raise


def _refuse_if_destination_changed(target: Path, snapshot: _Identity | None) -> None:
    """Abort when the destination is no longer the file this write inspected.

    The identity is read once, before the temp file is created, and the write
    body that follows runs for as long as the caller takes. Anything that
    happens to the destination in that window -- another process tightening a
    0644 artifact to 0600, or replacing it outright -- was invisible: the temp
    file still carried the stale identity and the swap silently undid the
    change (Codex review, PR #219).

    Re-reading immediately before ``os.replace`` narrows that window from the
    whole write body to the few instructions between here and the rename. It
    cannot close it -- rename-after-check is inherently racy -- but it turns a
    guaranteed silent overwrite into a vanishingly unlikely one, and reports the
    common case instead of hiding it.
    """
    current = _target_identity(target)
    if current == snapshot:
        return
    if snapshot is None:
        raise OSError(
            f"refusing to replace {target}: it did not exist when this write "
            f"began and has appeared since, so replacing it would discard "
            f"whatever created it"
        )
    raise OSError(
        f"refusing to replace {target}: it changed while this write was in "
        f"progress (was mode={snapshot.mode:04o} uid={snapshot.uid} "
        f"gid={snapshot.gid} inode={snapshot.ino}, now "
        + (
            f"mode={current.mode:04o} uid={current.uid} gid={current.gid} "
            f"inode={current.ino}"
            if current is not None
            else "absent"
        )
        + "), so the swap would discard that change"
    )


def _discard(tmp_path: Path) -> None:
    """Best-effort removal of an abandoned temp file."""
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _create_temp_file(target: Path) -> tuple[int, Path]:
    """``mkstemp`` equivalent that lets the KERNEL apply the process umask.

    ``mkstemp`` hardcodes 0600, so landing a new artifact at the permissions a
    plain ``open(path, "w")`` would produce meant reading the umask -- and the
    only way to read it is ``os.umask(0)`` followed by a restoring call. That
    pair is process-global: in a multithreaded writer, any other thread creating
    a file or directory in the window between the two calls gets it with NO mask
    applied, which is how a restrictive deployment umask silently stops
    protecting artifacts (Codex review, PR #219).

    Opening with an explicit 0666 asks the kernel to apply the umask itself,
    atomically, with no global state to restore. ``O_EXCL`` keeps the
    uniqueness guarantee; ``O_BINARY`` (Windows only, absent elsewhere) matches
    what ``mkstemp`` requests so the caller's ``newline`` handling is the only
    line-ending translation in play.
    """
    directory = target.parent if str(target.parent) else Path(".")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    for _ in range(_TEMP_NAME_ATTEMPTS):
        candidate = directory / f"{target.name}.{uuid4().hex}.tmp"
        try:
            return os.open(candidate, flags, 0o666), candidate
        except FileExistsError:  # pragma: no cover - requires a uuid4 collision
            continue
    raise OSError(  # pragma: no cover - requires repeated uuid4 collisions
        f"could not create a unique temporary file for {target} "
        f"after {_TEMP_NAME_ATTEMPTS} attempts"
    )


def _preserve_ownership(fd: int, uid: int, gid: int) -> bool:
    """Give the temp file the destination's owner and group. True if it has both.

    Copying an artifact's MODE without its OWNERSHIP silently redirects who the
    permission bits apply to. ``os.replace`` installs a new inode owned by the
    writer, so a 0640 file owned by ``svc-a:monitoring`` and rewritten by
    ``svc-b`` through a shared directory keeps 0640 while monitoring loses its
    read access and svc-b's primary group gains it; a group-writable 0620
    artifact likewise moves its owner bits from svc-a to svc-b. Plain
    ``open(path, "w")`` truncates in place and changes neither
    (Codex review, PR #219).

    Reproducing them is privileged: changing a file's group requires membership
    in the target group, and changing its owner is root-only. Failing here is
    therefore an ordinary outcome for an unprivileged writer, not an exceptional
    one -- the caller reduces the mode to owner-only rather than granting access
    to identities the artifact was never shared with.
    """
    try:
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid) == (uid, gid):
            return True
        os.fchown(fd, uid, gid)
        return True
    except (OSError, AttributeError):  # AttributeError: no fchown on Windows
        return False


class _Identity(NamedTuple):
    """What a destination looked like, and enough of it to notice a swap.

    ``ino``/``dev`` are not used to reproduce anything -- they are there so the
    check immediately before ``os.replace`` can tell that the file inspected at
    the start of the write is still the file being replaced.
    """

    mode: int
    uid: int
    gid: int
    ino: int
    dev: int


def _target_identity(target: Path) -> _Identity | None:
    """The identity an existing destination should keep, or None if new.

    ``os.replace`` installs a NEW inode, so without carrying this across, a
    deployment that had tightened an artifact to 0640 for a monitoring group, or
    to 0600 for sensitive run data, would have that reset by the first atomic
    rewrite -- either exposing the artifact or stripping group access something
    else depends on. ``open(path, "w")`` truncates in place and leaves
    permissions untouched, and this is the drop-in for it.

    A destination that does not exist yet returns None and needs nothing:
    :func:`_create_temp_file` already created the temp file at 0666-under-umask,
    exactly what a plain ``open`` would have produced.

    Only "not there" is treated as new. A broad ``except OSError`` also
    swallowed a transient EIO or ESTALE on a network filesystem, and reporting
    "no existing file" on those makes the write silently skip every preservation
    step below -- replacing a restrictive artifact with umask-derived mode and
    writer ownership, which is exactly what this function exists to prevent
    (Codex review, PR #219). Any other error propagates and aborts the write.
    """
    try:
        info = target.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return _Identity(info.st_mode & 0o7777, info.st_uid, info.st_gid, info.st_ino, info.st_dev)


def _set_mode(fd: int, tmp_path: Path, mode: int) -> bool:
    """chmod the open temp file, by descriptor where possible. True on success.

    ``fchmod`` acts on the descriptor already held, so there is no window in
    which the path could be swapped between the check and the change. It is a
    POSIX capability; the path-based fallback covers platforms without it.

    The result is reported rather than swallowed: on a filesystem that rejects
    chmod, a failure here means the temp file keeps its umask-derived mode, and
    replacing a restrictive destination with it would permanently widen access.
    The caller decides, because whether that is harmless or a leak depends on
    which direction the modes differ (Codex review, PR #219).
    """
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:  # pragma: no cover - platform dependent
            os.chmod(tmp_path, mode)
        return True
    except OSError:
        return False


def _refuse_if_mode_differs(fd: int, target: Path, intended: int) -> None:
    """Abort the write unless the temp file already carries the intended mode.

    Reached only when chmod failed. Continuing would ``os.replace`` a
    umask-derived temp file over the destination, so the artifact silently ends
    up at permissions nobody chose -- on the write that was supposed to preserve
    the ones it had.

    Both directions matter. Wider is a leak: 0644 over a 0600 artifact exposes
    it. Narrower is an outage of the quiet kind: under umask 077 the temp file
    is 0600, and replacing a 0640 artifact with it revokes the monitoring or
    reporting group's read access with nothing logged (Codex review, PR #219).
    An earlier version only refused the widening case for that reason.

    Equality is the one safe outcome, and it is the common one -- the temp file
    usually already carries the mode chmod was asked for, in which case the
    failure changed nothing and the write proceeds.
    """
    # No blanket except: applying the mode has ALREADY failed, so if the mode
    # cannot even be inspected there is no basis for calling the replacement
    # safe, and returning here let an unverified inode replace the destination
    # (Codex review, PR #219). The error propagates and aborts the write.
    current = os.fstat(fd).st_mode & 0o7777
    if current == intended:
        return
    widened = current & ~intended & 0o777
    narrowed = intended & ~current & 0o777
    raise OSError(
        f"refusing to replace {target}: its mode is {intended:04o} but the "
        f"replacement could not be chmodded and would land at {current:04o}"
        + (f", granting {widened:04o} the destination does not" if widened else "")
        + (f", revoking {narrowed:04o} the destination grants" if narrowed else "")
    )


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory so a completed rename survives power loss.

    Directory fds are a POSIX capability; on platforms (or filesystems) where
    opening/fsyncing a directory fails, the rename still happened — durability
    just degrades to the pre-fsync behavior, so errors are swallowed rather than
    failing an otherwise-successful write.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def order_csv_covers(path: Path, expected_rows: Sequence[Dict[str, Any]]) -> bool:
    """True when every expected order already appears in the CSV at ``path``.

    validate_run_artifacts checks only the header, so a stale order CSV from an
    earlier attempt passes it while omitting orders the freshly-written state
    records. Comparing the actual rows is what catches that (Codex review).
    """
    expected = {
        str(row.get("order_id") or f"ticker:{row.get('ticker')}")
        for row in expected_rows
    }
    if not expected:
        return True
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            present = {
                str(row.get("order_id") or f"ticker:{row.get('ticker')}")
                for row in csv.DictReader(handle)
            }
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return expected.issubset(present)
