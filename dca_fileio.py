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

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``).

    The temp file is created in ``path``'s directory so the final rename never
    crosses a filesystem boundary. On any failure the temp file is removed and
    the previous contents of ``path`` (if any) are left untouched.
    """
    with atomic_open_w(path, encoding=encoding) as handle:
        handle.write(text)


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
    """
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent) or ".", prefix=target.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    except BaseException:
        # Best-effort cleanup; the original destination file is never touched.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
