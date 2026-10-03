"""Allowlisted, permission-checked loader for the operator's local ``.env`` file.

Why this exists: the scheduled executors run in a sandbox whose shell does not
inherit the operator's macOS environment, so ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``
and ``DCA_ALERT_EMAIL_ADDRESS`` arrived unset on 2026-09-23 (Gate 7 account pin
and the email alert channel both failed). Every executor shell command starts
fresh, so sourcing a file in a prompt only helps the one command it prefixes;
loading from :mod:`dca_paths` at import makes every ``python3 -m scripts.*`` call
see the same values.

The contract is deliberately narrow:

* only the names in :data:`ALLOWED_KEYS` are read; anything else in the file is
  ignored, never exported;
* a variable that is already set and non-empty is never overridden, so a real
  export (CI, a cloud clone, a developer shell) always wins over the file;
* ``DCA_DISABLE_DOTENV`` turns the whole thing off (``conftest.py`` sets it so
  the suite never reads a real operator file);
* the file must be a regular, non-symlink file owned by the invoking user with
  no group/world access (mode ``0600``), mirroring the console's ``--auth-file``
  rules. A violation raises :class:`ValueError` naming the rule, never the
  contents, and so fails closed at import instead of silently using a secret
  any local user could read;
* a missing file is a no-op: a checkout without one behaves exactly as before.

Values are parsed as ``KEY=VALUE`` (optional ``export `` prefix, optional
matching single or double quotes). Inline ``# comments`` after a value are NOT
stripped; put comments on their own line. Standard library only; imports nothing
from the rest of the project so :mod:`dca_paths` can import it without a cycle.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import MutableMapping, Optional

ALLOWED_KEYS = (
    "DCA_ALERT_EMAIL_ADDRESS",
    "DCA_CRON_TRACKING_ROOT",
    "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER",
)
DISABLE_FLAG = "DCA_DISABLE_DOTENV"

# The operator's file lives beside the modules, i.e. the repo root in a checkout.
# Module-level (not recomputed per call) so a test can point it elsewhere.
REPO_ENV_PATH = Path(__file__).resolve().parent / ".env"

# A handful of short lines; anything larger is not an env file we should parse.
_MAX_BYTES = 4096


def _read_checked(path: Path) -> Optional[str]:
    """Return the file's text, ``None`` if absent, or raise ``ValueError`` on a rule breach."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)  # a FIFO must not hang the import
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP is how O_NOFOLLOW reports a symlink; never echo the path's contents.
        raise ValueError(f"{path.name}: cannot be opened ({exc.strerror}); symlinks are refused") from None
    try:
        # Validate on the descriptor we will read, so the bytes provably come from
        # the checked inode (no check/use race on the path).
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{path.name}: must be a regular file")
        if os.name == "posix":
            if st.st_uid != os.getuid():
                raise ValueError(f"{path.name}: must be owned by the invoking user")
            if st.st_mode & 0o077:
                raise ValueError(f"{path.name}: must not be group/world accessible; run: chmod 600 {path.name}")
        if st.st_size > _MAX_BYTES:
            raise ValueError(f"{path.name}: larger than {_MAX_BYTES} bytes")
        data = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path.name}: not valid UTF-8") from None


def _parse(text: str) -> dict:
    """Parse allowlisted ``KEY=VALUE`` lines; blank values are skipped."""
    parsed = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in ALLOWED_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            parsed[key] = value
    return parsed


def load_dotenv(
    path: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
) -> list:
    """Export allowlisted keys from ``path`` into ``environ`` and return the names set.

    Args:
        path: The file to read; defaults to :data:`REPO_ENV_PATH` (read at call time).
        environ: The mapping to update; defaults to ``os.environ``.

    Returns:
        Names actually exported, in :data:`ALLOWED_KEYS` order. Empty when the file is
        absent, the loader is disabled, or every key was already set.

    Raises:
        ValueError: The file exists but breaks a permission/shape rule (see module doc).

    Side effects:
        Mutates ``environ``; never overrides a variable that is already non-empty.
    """
    env = os.environ if environ is None else environ
    if env.get(DISABLE_FLAG):
        return []
    text = _read_checked(Path(REPO_ENV_PATH if path is None else path))
    if text is None:
        return []
    loaded = []
    parsed = _parse(text)
    for key in ALLOWED_KEYS:
        if key in parsed and not env.get(key):
            env[key] = parsed[key]
            loaded.append(key)
    return loaded
