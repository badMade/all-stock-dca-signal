"""Tests for the atomic file-write helpers in dca_fileio."""

import csv
import os
import stat
from pathlib import Path

import pytest

from dca_fileio import atomic_open_w, atomic_write_text


def test_atomic_write_text_creates_file(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, '{"ok": true}')
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_atomic_write_text_replaces_existing(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, "data")
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_atomic_open_w_writes_csv(tmp_path):
    target = tmp_path / "rows.csv"
    with atomic_open_w(target, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["a", "b"])
        writer.writeheader()
        writer.writerow({"a": "1", "b": "2"})
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{"a": "1", "b": "2"}]


def test_atomic_open_w_failure_preserves_original(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"prior": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_open_w(target) as handle:
            handle.write("partial garbage")
            raise RuntimeError("crash mid-write")
    # The destination keeps its previous contents and no temp file is left behind.
    assert target.read_text(encoding="utf-8") == '{"prior": 1}'
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_open_w_failure_without_original_leaves_nothing(tmp_path):
    target = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        with atomic_open_w(target) as handle:
            handle.write("partial")
            raise RuntimeError("crash mid-write")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def _expected_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def test_atomic_write_uses_umask_permissions_not_mkstemp_0600(tmp_path):
    """Artifacts must be readable by whoever a plain open() would have let read them.

    ``tempfile.mkstemp`` creates at 0600 and ``os.replace`` preserves the temp
    file's mode, so without an explicit chmod every run_state.json, pointer, and
    CSV would land owner-only -- invisible to a reporting or monitoring process
    running as another user.
    """
    target = tmp_path / "out.json"
    atomic_write_text(target, "{}")
    assert target.stat().st_mode & 0o777 == _expected_mode()


def test_atomic_open_w_matches_plain_open_permissions(tmp_path):
    atomic = tmp_path / "atomic.csv"
    plain = tmp_path / "plain.csv"
    with atomic_open_w(atomic, newline="") as handle:
        handle.write("a,b\n")
    with plain.open("w", newline="", encoding="utf-8") as handle:
        handle.write("a,b\n")
    assert atomic.stat().st_mode & 0o777 == plain.stat().st_mode & 0o777


# --- Permission preservation on atomic replace -------------------------------
# os.replace installs a NEW inode, so unlike open(path, "w") the mode does not
# survive unless it is copied across. A deployment that tightened an artifact to
# 0640 for a monitoring group, or 0600 for sensitive run data, must not have that
# reset to the umask default by the first atomic rewrite (Codex review, PR #219).

@pytest.mark.parametrize("mode", [0o600, 0o640, 0o604, 0o660])
def test_rewriting_an_existing_file_preserves_its_mode(tmp_path, mode):
    target = tmp_path / "artifact.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, mode)

    atomic_write_text(target, '{"updated": true}')

    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert target.read_text(encoding="utf-8") == '{"updated": true}'


def test_a_new_file_gets_the_umask_default_not_mkstemps_0600(tmp_path):
    """The original reason this helper exists: mkstemp creates 0600, which would
    lock out a reporting process running as another user."""
    target = tmp_path / "new.json"
    atomic_write_text(target, "{}")

    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~umask


def test_mode_preservation_survives_repeated_rewrites(tmp_path):
    target = tmp_path / "artifact.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o640)
    for i in range(3):
        atomic_write_text(target, f'{{"n": {i}}}')
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


# --- Temp-file creation does not mutate process-global umask -----------------
# Landing a new artifact at the mode a plain open() would produce used to mean
# READING the umask, and the only way to read it is os.umask(0) followed by a
# restoring call. That pair is process-global: in a multithreaded writer, any
# other thread creating a file or directory between the two calls gets it with
# no mask applied (Codex review, PR #219). The temp file is now opened with an
# explicit 0666 so the kernel applies the umask itself.

@pytest.mark.parametrize("umask_value", [0o022, 0o077, 0o002])
def test_a_new_file_lands_at_the_mode_a_plain_open_would_produce(tmp_path, umask_value):
    previous = os.umask(umask_value)
    try:
        reference = tmp_path / "plain.txt"
        reference.write_text("x", encoding="utf-8")
        written = tmp_path / "atomic.txt"
        atomic_write_text(written, "x")
        assert stat.S_IMODE(written.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)
    finally:
        os.umask(previous)


def test_writing_never_calls_umask(tmp_path, monkeypatch):
    """The guard for the race itself: reading the mask is what mutated it."""

    def _fail(_mask):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("atomic_write_text must not touch the process umask")

    monkeypatch.setattr(os, "umask", _fail)
    atomic_write_text(tmp_path / "run_state.json", "{}")
    assert (tmp_path / "run_state.json").read_text(encoding="utf-8") == "{}"


def test_a_tightened_destination_keeps_its_mode_across_rewrites(tmp_path):
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    os.chmod(target, 0o640)
    atomic_write_text(target, '{"second": true}')
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_no_temp_files_are_left_behind(tmp_path):
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    assert [p.name for p in tmp_path.iterdir()] == ["run_state.json"]


def test_the_temp_file_is_never_more_permissive_than_its_destination(tmp_path):
    """No exposure window: replacing a 0600 artifact under a 022 umask used to
    create the temp at 0644 and only tighten it AFTER the contents were written
    and fsynced, so the complete sensitive contents were readable by any other
    local user through the visible *.tmp file for the whole write (Codex review,
    PR #219)."""
    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, "{}")
        os.chmod(target, 0o600)

        observed: list[int] = []
        with atomic_open_w(target) as handle:
            handle.write('{"sensitive": true}')
            observed = [
                stat.S_IMODE(p.stat().st_mode)
                for p in tmp_path.iterdir()
                if p.name.endswith(".tmp")
            ]

        assert observed == [0o600], f"temp file was {[oct(m) for m in observed]} mid-write"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    finally:
        os.umask(previous)


# --- Group ownership travels with the mode bits ------------------------------
# os.replace installs a new inode, so copying an artifact's MODE without its
# GROUP silently redirects who the group bits apply to: a 0640 file owned by a
# monitoring group keeps 0640 but lands under the writer's primary group, so
# monitoring loses read access and an unrelated group gains it (Codex review,
# PR #219).

def _a_foreign_gid() -> int | None:
    """A gid this process can set but that is not its own, or None."""
    candidates = [g for g in os.getgroups() if g != os.getgid()]
    if candidates:
        return candidates[0]
    return 1 if os.getuid() == 0 else None


def test_the_destination_group_is_preserved(tmp_path):
    gid = _a_foreign_gid()
    if gid is None:
        pytest.skip("no second group available to this process")
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    os.chown(target, -1, gid)
    os.chmod(target, 0o640)

    atomic_write_text(target, '{"second": true}')

    info = target.stat()
    assert info.st_gid == gid, "the monitoring group must not be swapped for the writer's"
    assert stat.S_IMODE(info.st_mode) == 0o640


def test_the_write_aborts_when_the_group_cannot_be_preserved(tmp_path, monkeypatch):
    """Unprivileged writers cannot chown to a group they do not belong to.
    Degrading the mode kept the replacement from reaching the wrong identities
    but still stripped access from the right ones, so the write aborts instead
    and the destination is left exactly as it was (Codex review, PR #219)."""
    gid = _a_foreign_gid()
    if gid is None:
        pytest.skip("no second group available to this process")
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    os.chown(target, -1, gid)
    os.chmod(target, 0o640)

    def _denied(*_args, **_kwargs):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "fchown", _denied)
    with pytest.raises(OSError, match="refusing to replace"):
        atomic_write_text(target, '{"second": true}')

    info = target.stat()
    assert stat.S_IMODE(info.st_mode) == 0o640, "the destination keeps its mode"
    assert info.st_gid == gid, "and its group"
    assert [p.name for p in tmp_path.iterdir()] == ["run_state.json"]


def test_the_destination_owner_is_preserved(tmp_path):
    """os.replace installs a new inode owned by the WRITER, so a shared-directory
    rewrite by another service account silently moved the owner bits to that
    account -- unlike the in-place open(path, "w") this replaces (Codex review,
    PR #219)."""
    if os.getuid() != 0:
        pytest.skip("changing a file's owner is root-only")
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    os.chown(target, 12345, 12345)
    os.chmod(target, 0o620)

    atomic_write_text(target, '{"second": true}')

    info = target.stat()
    assert (info.st_uid, info.st_gid) == (12345, 12345)
    assert stat.S_IMODE(info.st_mode) == 0o620


def test_the_write_aborts_when_ownership_cannot_be_reproduced(tmp_path, monkeypatch):
    """An unprivileged writer cannot chown, and completing the swap would hand
    the file to the writer's identity (Codex review, PR #219)."""
    if os.getuid() != 0:
        pytest.skip("test needs to create a foreign-owned file first")
    target = tmp_path / "run_state.json"
    atomic_write_text(target, "{}")
    os.chown(target, 12345, 12345)
    os.chmod(target, 0o620)

    def _denied(*_args, **_kwargs):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "fchown", _denied)
    with pytest.raises(OSError, match="refusing to replace"):
        atomic_write_text(target, '{"second": true}')

    info = target.stat()
    assert (info.st_uid, stat.S_IMODE(info.st_mode)) == (12345, 0o620)


def test_a_restrictive_rewrite_aborts_when_chmod_is_rejected(tmp_path, monkeypatch):
    """On a filesystem that rejects chmod, swallowing the failure let os.replace
    put a umask-derived 0644 temp file over a 0600 artifact -- permanently
    exposing it on the very write that promised to preserve its permissions
    (Codex review, PR #219)."""
    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, '{"original": true}')
        os.chmod(target, 0o600)

        def _rejected(*_args, **_kwargs):
            raise OSError("chmod not supported on this filesystem")

        monkeypatch.setattr(os, "fchmod", _rejected)
        monkeypatch.setattr(os, "chmod", _rejected)
        with pytest.raises(OSError, match="refusing to replace"):
            atomic_write_text(target, '{"sensitive": true}')

        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == '{"original": true}'
        assert [p.name for p in tmp_path.iterdir()] == ["run_state.json"]
    finally:
        os.umask(previous)


def test_a_write_narrower_than_its_destination_also_aborts(tmp_path, monkeypatch):
    """Narrowing is not the safe direction it looks like: under umask 077 the
    temp file is 0600, and replacing a 0640 artifact with it revokes the
    monitoring group's read access with nothing logged (Codex review, PR #219)."""
    previous = os.umask(0o077)
    try:
        target = tmp_path / "grouped.json"
        atomic_write_text(target, '{"original": true}')
        os.chmod(target, 0o640)

        def _rejected(*_args, **_kwargs):
            raise OSError("chmod not supported on this filesystem")

        monkeypatch.setattr(os, "fchmod", _rejected)
        monkeypatch.setattr(os, "chmod", _rejected)
        with pytest.raises(OSError, match="revoking"):
            atomic_write_text(target, '{"new": true}')

        assert target.read_text(encoding="utf-8") == '{"original": true}'
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    finally:
        os.umask(previous)


def test_a_refused_write_does_not_leak_a_descriptor(tmp_path, monkeypatch):
    """_refuse_if_more_permissive raises BEFORE os.fdopen takes ownership of the
    descriptor, and unlinking the temp PATH does not free an open fd -- so a
    caller that retries failed artifact writes leaked one per attempt and could
    eventually hit EMFILE on unrelated I/O (Codex review, PR #219)."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("descriptor accounting needs /proc")

    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, "{}")
        os.chmod(target, 0o600)

        def _rejected(*_args, **_kwargs):
            raise OSError("chmod not supported on this filesystem")

        monkeypatch.setattr(os, "fchmod", _rejected)
        monkeypatch.setattr(os, "chmod", _rejected)

        before = len(os.listdir(fd_dir))
        for _ in range(50):
            with pytest.raises(OSError):
                atomic_write_text(target, '{"sensitive": true}')
        assert len(os.listdir(fd_dir)) == before
    finally:
        os.umask(previous)


def test_a_failing_write_body_does_not_leak_a_descriptor(tmp_path):
    """The other pre-existing path: an exception raised inside the with block."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("descriptor accounting needs /proc")

    target = tmp_path / "run_state.json"
    before = len(os.listdir(fd_dir))
    for _ in range(50):
        with pytest.raises(ValueError):
            with atomic_open_w(target) as handle:
                handle.write("partial")
                raise ValueError("writer blew up")
    assert len(os.listdir(fd_dir)) == before
    assert not target.exists()


def test_a_transient_stat_failure_aborts_rather_than_discarding_preservation(tmp_path):
    """A broad except OSError reported "no existing file" on a transient EIO or
    ESTALE, so the write skipped every preservation step and replaced a
    restrictive artifact with umask-derived mode (Codex review, PR #219)."""
    import errno
    from unittest import mock

    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, '{"original": true}')
        os.chmod(target, 0o600)

        real_stat = Path.stat

        def _flaky(self, *args, **kwargs):
            if self.name == "run_state.json":
                raise OSError(errno.EIO, "Input/output error")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", _flaky), pytest.raises(OSError):
            atomic_write_text(target, '{"sensitive": true}')

        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == '{"original": true}'
    finally:
        os.umask(previous)


def test_an_absent_destination_is_still_treated_as_new(tmp_path):
    fresh = tmp_path / "new.json"
    atomic_write_text(fresh, "{}")
    assert fresh.read_text(encoding="utf-8") == "{}"


def test_a_failing_fdopen_surfaces_its_own_error_and_cleans_up(tmp_path):
    """os.fdopen takes ownership even when it then fails, and closes the fd on
    its way out -- so an unconditional close raised EBADF, replacing the
    caller's real exception and skipping cleanup (Codex review, PR #219)."""
    target = tmp_path / "run_state.json"
    with pytest.raises(LookupError):
        atomic_write_text(target, "{}", encoding="does-not-exist")
    assert list(tmp_path.iterdir()) == [], "the temp file must not survive"


def test_an_unreadable_replacement_mode_aborts(tmp_path, monkeypatch):
    """Applying the mode has already failed; if it cannot even be inspected there
    is no basis for calling the replacement safe (Codex review, PR #219)."""
    import errno
    from unittest import mock

    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, '{"original": true}')
        os.chmod(target, 0o600)

        def _rejected(*_args, **_kwargs):
            raise OSError("chmod not supported")

        def _blind(*_args, **_kwargs):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "fchmod", _rejected)
        monkeypatch.setattr(os, "chmod", _rejected)
        with mock.patch.object(os, "fstat", _blind), pytest.raises(OSError):
            atomic_write_text(target, '{"sensitive": true}')

        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == '{"original": true}'
    finally:
        os.umask(previous)


def test_a_chmod_failure_that_changed_nothing_still_proceeds(tmp_path, monkeypatch):
    """The common case: the temp file already carries the mode chmod was asked
    for, so the failure is immaterial and the write must not abort."""
    previous = os.umask(0o022)
    try:
        target = tmp_path / "plain.json"
        atomic_write_text(target, "{}")
        os.chmod(target, 0o644)  # exactly what 0666-under-umask-022 produces

        def _rejected(*_args, **_kwargs):
            raise OSError("chmod not supported on this filesystem")

        monkeypatch.setattr(os, "fchmod", _rejected)
        monkeypatch.setattr(os, "chmod", _rejected)
        atomic_write_text(target, '{"ok": true}')

        assert target.read_text(encoding="utf-8") == '{"ok": true}'
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    finally:
        os.umask(previous)


# --- The destination is re-checked immediately before the swap ---------------
# The identity is read once, before the temp file exists, and the caller's write
# body then runs for as long as it takes. Anything done to the destination in
# that window was invisible, and the swap silently undid it (Codex review,
# PR #219).

def test_a_destination_tightened_mid_write_is_not_clobbered(tmp_path):
    previous = os.umask(0o022)
    try:
        target = tmp_path / "run_state.json"
        atomic_write_text(target, '{"original": true}')

        with pytest.raises(OSError, match="changed while this write was in progress"):
            with atomic_open_w(target) as handle:
                handle.write('{"new": true}')
                os.chmod(target, 0o600)  # another process tightens it

        assert stat.S_IMODE(target.stat().st_mode) == 0o600, "the tightening survives"
        assert target.read_text(encoding="utf-8") == '{"original": true}'
        assert [p.name for p in tmp_path.iterdir()] == ["run_state.json"]
    finally:
        os.umask(previous)


def test_a_destination_replaced_mid_write_is_not_clobbered(tmp_path):
    """A different inode at the same path means someone else completed a write."""
    target = tmp_path / "run_state.json"
    atomic_write_text(target, '{"original": true}')

    with pytest.raises(OSError, match="changed while this write was in progress"):
        with atomic_open_w(target) as handle:
            handle.write('{"mine": true}')
            atomic_write_text(target, '{"theirs": true}')

    assert target.read_text(encoding="utf-8") == '{"theirs": true}'


def test_a_destination_created_mid_write_is_not_clobbered(tmp_path):
    target = tmp_path / "fresh.json"
    with pytest.raises(OSError, match="has appeared since"):
        with atomic_open_w(target) as handle:
            handle.write('{"mine": true}')
            target.write_text('{"theirs": true}', encoding="utf-8")

    assert target.read_text(encoding="utf-8") == '{"theirs": true}'


def test_ordinary_repeated_writes_are_unaffected(tmp_path):
    """The re-check must not make the normal path fragile."""
    target = tmp_path / "run_state.json"
    for index in range(5):
        atomic_write_text(target, '{"n": %d}' % index)
    assert target.read_text(encoding="utf-8") == '{"n": 4}'


def test_fsync_dir_success(tmp_path):
    from unittest import mock

    from dca_fileio import _fsync_dir
    with mock.patch("os.open", return_value=42) as m_open, \
         mock.patch("os.fsync") as m_fsync, \
         mock.patch("os.close") as m_close:

        _fsync_dir(tmp_path)

        m_open.assert_called_once_with(tmp_path, os.O_RDONLY)
        m_fsync.assert_called_once_with(42)
        m_close.assert_called_once_with(42)


def test_fsync_dir_open_oserror(tmp_path):
    from unittest import mock

    from dca_fileio import _fsync_dir
    with mock.patch("os.open", side_effect=OSError) as m_open, \
         mock.patch("os.fsync") as m_fsync, \
         mock.patch("os.close") as m_close:

        _fsync_dir(tmp_path)

        m_open.assert_called_once_with(tmp_path, os.O_RDONLY)
        m_fsync.assert_not_called()
        m_close.assert_not_called()

def test_fsync_dir_fsync_oserror(tmp_path):
    from unittest import mock

    from dca_fileio import _fsync_dir
    with mock.patch("os.open", return_value=42) as m_open, \
         mock.patch("os.fsync", side_effect=OSError) as m_fsync, \
         mock.patch("os.close") as m_close:

        _fsync_dir(tmp_path)

        m_open.assert_called_once_with(tmp_path, os.O_RDONLY)
        m_fsync.assert_called_once_with(42)
        m_close.assert_called_once_with(42)
