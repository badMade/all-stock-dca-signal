"""Credential redaction at console output and restored-history boundaries."""

import json
import os
import sys

import pytest

from dca_console_jobs import Command, JobManager, redact


@pytest.mark.parametrize(
    ("message", "private_values"),
    [
        ("Request failed: Authorization: Bearer synthetic-bearer-value", ("synthetic-bearer-value",)),
        (
            "Request failed: https://synthetic-user:synthetic-password@example.invalid/orders",
            ("synthetic-user", "synthetic-password"),
        ),
        ('Request failed: {"oauth2AccessToken": "synthetic-oauth-value"}', ("synthetic-oauth-value",)),
    ],
)
def test_console_redacts_common_connector_credentials(message, private_values):
    rendered = redact(message)
    assert "Request failed:" in rendered
    assert "[redacted]" in rendered.lower()
    assert all(value not in rendered for value in private_values)


def test_console_redaction_preserves_ordinary_diagnostics():
    message = "AAPL: lookup failed with HTTP 503; retrying in 2 seconds.\nNext ticker: MSFT."
    assert redact(message) == message


@pytest.mark.parametrize("label", ["secret=", "token:", "password=", "client_secret="])
def test_console_redacts_complete_labeled_private_key_before_rewriting_header(label):
    body = "U1lOVEhFVElDX0xPQ0FMX0tFWV9CT0RZ"
    message = (
        f"Operation failed: {label}-----BEGIN PRIVATE KEY-----\n"
        f"{body}\n-----END PRIVATE KEY-----\nNext output retained."
    )
    rendered = redact(message)
    assert body not in rendered
    assert "[redacted]" in rendered.lower()
    assert rendered.endswith("Next output retained.")


@pytest.mark.parametrize("armor", ["closed", "nested", "mismatched", "incomplete", "overlong"])
def test_private_key_child_output_stays_redacted_across_lines_and_chunks(tmp_path, armor):
    # These bodies encode synthetic test labels; they are not actual key material.
    body = "U1lOVEhFVElDX0xPQ0FMX0tFWV9CT0RZ"
    inner_body = "U1lOVEhFVElDX0lOTkVSX0tFWV9CT0RZ"
    tail_body = "U1lOVEhFVElDX1RBSUxfS0VZX0JPRFk="
    header = "-----BEGIN PRIVATE KEY-----\n"
    footer = "-----END PRIVATE KEY-----\n"
    parts = [header, body + "\n"]
    if armor == "closed":
        parts += [footer, "Next output retained.\n"]
    elif armor == "nested":
        parts += [header, inner_body + "\n", footer, tail_body + "\n", footer, "Next output retained.\n"]
    elif armor == "mismatched":
        parts += ["-----END CERTIFICATE-----\n", tail_body]
    elif armor == "incomplete":
        parts += [tail_body]
    else:
        parts += ["x" * 32_768 + "\n", tail_body + "\n"]
    env = dict(os.environ)
    # Use a neutral variable name so literal environment-secret matching cannot
    # hide a failure to recognize streamed private-key armor.
    env["DCA_TEST_OUTPUT_PARTS"] = json.dumps(parts)
    code = (
        "import json,os,time; "
        "parts=json.loads(os.environ['DCA_TEST_OUTPUT_PARTS']); "
        "[(os.write(1,part.encode()),time.sleep(0.1)) for part in parts]"
    )
    command = Command((sys.executable, "-c", code), tmp_path, env, timeout=5)
    manager = JobManager(tmp_path / "jobs")
    try:
        manager.submit("demo-dca", "Private output", lambda job_id, log: manager.run_command(command, log))
        assert manager.worker is not None
        manager.worker.join(timeout=8)
        assert not manager.worker.is_alive()
        result = manager.snapshot()[0]
        assert result["status"] == "completed"
        persisted = (manager.root / f"{result['id']}.json").read_text(encoding="utf-8")
        for rendered in (result["log"], persisted):
            assert all(value not in rendered for value in (body, inner_body, tail_body))
        assert len(result["log"]) <= 24_000
        if armor in {"closed", "nested"}:
            assert result["log"].endswith("Next output retained.\n")
    finally:
        manager.close()


@pytest.mark.parametrize("field", ["log", "label"])
def test_restored_fields_redact_complete_secrets_before_truncation(tmp_path, monkeypatch, field):
    private_value = "opaque-restored-credential-value"
    monkeypatch.setenv("DCA_CONSOLE_TEST_SECRET", private_value)
    split = 10
    value = (
        "older output\n" + private_value + "z" * (24_000 - len(private_value[split:]))
        if field == "log"
        else "x" * (500 - split) + private_value + " trailing label"
    )
    root = tmp_path / "jobs"
    root.mkdir()
    payload = {
        "id": "restored-security-check",
        "status": "completed",
        "returncode": 0,
        "action": "demo-dca",
        "label": "DCA demo",
        "log": "Completed normally.",
        "started_at": None,
        "finished_at": None,
        field: value,
    }
    (root / "restored-security-check.json").write_text(json.dumps(payload), encoding="utf-8")
    manager = JobManager(root)
    try:
        restored = manager.snapshot()[0][field]
        exposed_fragment = private_value[split:] if field == "log" else private_value[:split]
        assert exposed_fragment not in restored
        assert private_value not in restored
        assert len(restored) <= (24_000 if field == "log" else 500)
    finally:
        manager.close()


@pytest.mark.parametrize("newline_after_secret", [True, False])
def test_overlong_child_line_discards_secret_suffix_until_newline_or_eof(
    tmp_path,
    monkeypatch,
    newline_after_secret,
):
    private_value = "opaque-streamed-credential-value"
    monkeypatch.setenv("DCA_CONSOLE_TEST_SECRET", private_value)
    ending = "\nNext output retained.\n" if newline_after_secret else ""
    code = (
        "import os,sys,time; value=os.environ['DCA_CONSOLE_TEST_SECRET']; "
        "os.write(1,('x'*(32768-10)+value[:10]).encode()); time.sleep(0.2); "
        "os.write(1,(value[10:]+sys.argv[1]).encode())"
    )
    command = Command((sys.executable, "-c", code, ending), tmp_path, dict(os.environ), timeout=5)
    manager = JobManager(tmp_path / "jobs")
    try:
        manager.submit("demo-dca", "Long output", lambda job_id, log: manager.run_command(command, log))
        assert manager.worker is not None
        manager.worker.join(timeout=8)
        assert not manager.worker.is_alive()
        result = manager.snapshot()[0]
        assert result["status"] == "completed"
        persisted = (manager.root / f"{result['id']}.json").read_text(encoding="utf-8")
        for rendered in (result["log"], persisted):
            assert private_value not in rendered
            assert private_value[10:] not in rendered
            assert "[long output line omitted]" in rendered
        assert len(result["log"]) <= 24_000
        if newline_after_secret:
            assert result["log"].endswith("Next output retained.\n")
    finally:
        manager.close()
