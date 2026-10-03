"""Tests for the allowlisted, permission-checked ``.env`` loader (``dca_env``).

The live executor runs in a sandbox whose shell does not inherit the operator's
macOS environment, so ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` and
``DCA_ALERT_EMAIL_ADDRESS`` arrived unset on 2026-09-23. These tests pin the
loader's narrow contract. Fixture values deliberately avoid account-shaped digit
runs so ``test_config_invariants.py``'s literal grep stays quiet.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dca_env

KEYS = ["DCA_ALERT_EMAIL_ADDRESS", "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"]
REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def test_loads_only_allowlisted_keys(tmp_path):
    f = _write(tmp_path / ".env", "# c\n\nexport DCA_ALERT_EMAIL_ADDRESS='ops@example.com'\n"
               'ROBINHOOD_AGENTIC_ACCOUNT_NUMBER="TEST-ACCT"\nOTHER=x\n')
    env = {}
    assert sorted(dca_env.load_dotenv(f, environ=env)) == KEYS
    assert env["DCA_ALERT_EMAIL_ADDRESS"] == "ops@example.com"
    assert "OTHER" not in env


def test_never_overrides_existing_environment(tmp_path):
    f = _write(tmp_path / ".env", "DCA_ALERT_EMAIL_ADDRESS=file@example.com\n")
    env = {"DCA_ALERT_EMAIL_ADDRESS": "real@example.com"}
    assert dca_env.load_dotenv(f, environ=env) == []
    assert env["DCA_ALERT_EMAIL_ADDRESS"] == "real@example.com"


def test_missing_file_is_a_noop(tmp_path):
    assert dca_env.load_dotenv(tmp_path / ".env", environ={}) == []


def test_rejects_group_or_world_readable_file_without_leaking_value(tmp_path):
    f = _write(tmp_path / ".env", "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER=TEST-ACCT\n", mode=0o644)
    with pytest.raises(ValueError) as exc:
        dca_env.load_dotenv(f, environ={})
    assert "TEST-ACCT" not in str(exc.value)


def test_rejects_symlink(tmp_path):
    real = _write(tmp_path / "real.env", "DCA_ALERT_EMAIL_ADDRESS=a@b.co\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        dca_env.load_dotenv(link, environ={})


def test_disable_flag_is_a_noop(tmp_path):
    f = _write(tmp_path / ".env", "DCA_ALERT_EMAIL_ADDRESS=a@b.co\n")
    env = {"DCA_DISABLE_DOTENV": "1"}
    assert dca_env.load_dotenv(f, environ=env) == []
    assert "DCA_ALERT_EMAIL_ADDRESS" not in env


def test_importing_dca_paths_triggers_the_loader(tmp_path):
    # Added with the hook (not in the approved set): a subprocess keeps the reload
    # from disturbing dca_paths' import-time constants in this test process.
    f = _write(tmp_path / ".env", "DCA_ALERT_EMAIL_ADDRESS=ops@example.com\n")
    code = (
        "import os, pathlib, dca_env\n"
        f"dca_env.REPO_ENV_PATH = pathlib.Path({str(f)!r})\n"
        "import dca_paths\n"
        "print(os.environ.get('DCA_ALERT_EMAIL_ADDRESS'))\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("DCA_DISABLE_DOTENV", "DCA_ALERT_EMAIL_ADDRESS")}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "ops@example.com", out.stderr
