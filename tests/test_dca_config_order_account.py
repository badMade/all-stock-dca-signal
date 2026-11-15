"""Tests for ``dca_config.resolve_order_account`` (written before the implementation).

Why: the order-history helper scripts (``merge_order_pages``, ``find_order_baseline``, ``assemble_order_pages``,
``reshape_order_pages``, ``same_day_top_up_gate``, ``gate_failure_diagnostics``) each REQUIRE ``--account <number>``,
and the live prompts fill it with ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}``. The shell expands that, so the real
account number sits on the command line, in process listings and in any transcript that records the command
(AGENTS.md secrets policy). ``primary_gates`` already reads it from the environment instead. This is the one shared
resolver the six scripts will use, built on the repo's existing ``resolve_broker_account`` so the shape rule
(6 to 12 digits) and the "value withheld from the error" rule are not copied.

Contract:
  * ``resolve_order_account(cli_value=None)`` returns a validated account number;
  * with no CLI value it reads ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (after letting ``dca_env`` load the operator's
    ``.env``, which never overrides a variable that is already set); an explicit CLI value is still honoured;
  * unset or empty fails closed with ``MissingConfigEnvError`` naming the VARIABLE; malformed, or the read-only
    literal, fails with ``InvalidAccountNumberError``; neither message ever contains the value.
"""

from __future__ import annotations

import os

import pytest

import dca_config
import dca_env

ACCOUNT = "999000111"  # conftest.py's synthetic value; any other account-shaped literal trips the secrets guard
VAR = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"


def test_reads_the_account_from_the_environment(monkeypatch):
    monkeypatch.setenv(VAR, ACCOUNT)
    assert dca_config.resolve_order_account() == ACCOUNT


def test_strips_stray_whitespace_from_the_environment_value(monkeypatch):
    monkeypatch.setenv(VAR, f" {ACCOUNT}\n")
    assert dca_config.resolve_order_account() == ACCOUNT


def test_an_explicit_cli_value_is_still_honoured_and_wins(monkeypatch):
    monkeypatch.setenv(VAR, ACCOUNT)
    assert dca_config.resolve_order_account("123456") == "123456"


def test_a_literal_placeholder_as_the_cli_value_expands_from_the_environment(monkeypatch):
    monkeypatch.setenv(VAR, ACCOUNT)
    assert dca_config.resolve_order_account("${" + VAR + "}") == ACCOUNT


@pytest.mark.parametrize("unset", ["delete", "empty"])
def test_an_unset_or_empty_environment_account_fails_closed_naming_the_variable(monkeypatch, unset):
    if unset == "delete":
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, "")
    with pytest.raises(dca_config.MissingConfigEnvError, match=VAR):
        dca_config.resolve_order_account()


@pytest.mark.parametrize("bad", ["not-an-account", "12345", "9" * 13, "12 34"])
def test_a_malformed_account_is_refused_and_the_value_is_withheld(monkeypatch, bad):
    monkeypatch.setenv(VAR, bad)
    with pytest.raises(dca_config.InvalidAccountNumberError) as excinfo:
        dca_config.resolve_order_account()
    assert bad not in str(excinfo.value)


def test_the_read_only_literal_is_not_an_order_account(monkeypatch):
    # resolve_broker_account accepts it for the read-only morning modes; an order script must not.
    monkeypatch.setenv(VAR, dca_config.READ_ONLY_ACCOUNT)
    with pytest.raises(dca_config.InvalidAccountNumberError):
        dca_config.resolve_order_account()


def test_the_operators_dotenv_is_loaded_when_the_variable_is_not_exported(monkeypatch, tmp_path):
    # The executor sandbox does not inherit the operator's shell (docs/local_env_file.md); the helper must let
    # dca_env fill the variable from the gitignored .env, exactly as importing dca_paths does.
    env_file = tmp_path / ".env"
    env_file.write_text(f"{VAR}={ACCOUNT}\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv(dca_env.DISABLE_FLAG, raising=False)
    monkeypatch.setattr(dca_env, "REPO_ENV_PATH", env_file)
    try:
        assert dca_config.resolve_order_account() == ACCOUNT
    finally:
        os.environ.pop(VAR, None)  # load_dotenv writes os.environ directly, outside monkeypatch's bookkeeping


def test_an_exported_variable_wins_over_the_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{VAR}=123456\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    monkeypatch.setenv(VAR, ACCOUNT)
    monkeypatch.delenv(dca_env.DISABLE_FLAG, raising=False)
    monkeypatch.setattr(dca_env, "REPO_ENV_PATH", env_file)
    assert dca_config.resolve_order_account() == ACCOUNT
