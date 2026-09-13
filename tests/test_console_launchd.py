"""Contract tests for the checkout-only launchd console renderer."""

from __future__ import annotations

import base64
import importlib.util
import plistlib
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "launchd" / "render_console_agent.py"
PASSWORD = "s3cret-pw-abcdefg"
CREDENTIAL = f"operator:{PASSWORD}".encode()


@pytest.fixture(scope="module")
def renderer():
    """Import the non-package deployment script so coverage sees its branches."""
    spec = importlib.util.spec_from_file_location("render_console_agent_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _auth_file(tmp_path: Path, *, mode: int = 0o600) -> Path:
    path = tmp_path / "console-auth"
    path.write_text(f"operator:{PASSWORD}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _print_args(auth_file: Path, root: Path, *extra: str) -> list[str]:
    return [
        "--auth-file",
        str(auth_file),
        "--python",
        sys.executable,
        "--cron-tracking-root",
        str(root),
        "--print",
        *extra,
    ]


def _render(renderer, capsys, args: list[str]) -> tuple[dict, bytes]:
    assert renderer.main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    encoded = captured.out.encode("utf-8")
    return plistlib.loads(encoded), encoded


def test_renders_expected_program_arguments_and_paths(renderer, tmp_path, capsys):
    auth_file = _auth_file(tmp_path)
    root = tmp_path / "artifacts"
    octopus_run = tmp_path / "octopus-run"
    octopus_run.mkdir()
    base_arguments = [
        sys.executable,
        str(REPO / "from.py"),
        "--local",
        "--no-browser",
        "--port",
        "8765",
        "--auth-file",
        str(auth_file),
    ]
    forms = (
        ([], []),
        (
            [
                "--trusted-host",
                "mac.tailnet.ts.net",
                "--behind-proxy",
                "--octopus-run",
                str(octopus_run),
            ],
            [
                "--trusted-host",
                "mac.tailnet.ts.net",
                "--behind-proxy",
                "--octopus-run",
                str(octopus_run),
            ],
        ),
    )

    for cli_suffix, expected_suffix in forms:
        definition, _ = _render(renderer, capsys, _print_args(auth_file, root, *cli_suffix))
        assert definition["ProgramArguments"] == base_arguments + expected_suffix
        assert definition["ProgramArguments"][0] == sys.executable
        assert definition["Label"] == "com.all-stock-dca.console"
        assert definition["WorkingDirectory"] == str(REPO)
        assert definition["KeepAlive"] == {"SuccessfulExit": False}
        assert definition["RunAtLoad"] is True
        assert definition["ThrottleInterval"] == 15
        assert definition["ExitTimeOut"] == 30
        assert definition["ProcessType"] == "Background"
        assert definition["StandardOutPath"] == str(root / "console" / "launchd.out.log")
        assert definition["StandardErrorPath"] == str(root / "console" / "launchd.err.log")
        assert definition["EnvironmentVariables"] == {
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "DCA_CRON_TRACKING_ROOT": str(root),
        }
        assert not any("PASSWORD" in name for name in definition["EnvironmentVariables"])
        assert "--bind" not in definition["ProgramArguments"]
        assert "--root" not in definition["ProgramArguments"]


def test_custom_root_is_used_for_both_environment_and_logs(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "ignored-environment-root"))

    definition, _ = _render(renderer, capsys, _print_args(auth_file, custom_root))

    assert definition["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(custom_root)
    assert definition["StandardOutPath"] == str(custom_root / "console" / "launchd.out.log")
    assert definition["StandardErrorPath"] == str(custom_root / "console" / "launchd.err.log")


def test_root_from_environment_is_used_when_flag_absent(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    environment_root = tmp_path / "environment-root"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(environment_root))

    definition, _ = _render(
        renderer,
        capsys,
        ["--auth-file", str(auth_file), "--python", sys.executable, "--print"],
    )

    assert definition["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(environment_root)
    assert definition["StandardOutPath"] == str(environment_root / "console" / "launchd.out.log")
    assert definition["StandardErrorPath"] == str(environment_root / "console" / "launchd.err.log")


def test_relative_root_is_absolutized_for_env_and_logs(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    flag_definition, _ = _render(renderer, capsys, _print_args(auth_file, Path("rel-root")))
    flag_root = tmp_path / "rel-root"

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", "rel-env-root")
    environment_args = ["--auth-file", str(auth_file), "--python", sys.executable, "--print"]
    environment_definition, _ = _render(renderer, capsys, environment_args)
    environment_root = tmp_path / "rel-env-root"

    # An explicitly empty flag mirrors dca_paths' empty environment behavior:
    # it is unset and therefore falls through to the configured environment
    # root instead of becoming Path(".") (Rock 4 review).
    empty_flag_definition, _ = _render(
        renderer,
        capsys,
        [*environment_args, "--cron-tracking-root", ""],
    )

    for definition, expected_root in (
        (flag_definition, flag_root),
        (environment_definition, environment_root),
        (empty_flag_definition, environment_root),
    ):
        assert definition["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == str(expected_root)
        assert definition["StandardOutPath"] == str(expected_root / "console" / "launchd.out.log")
        assert definition["StandardErrorPath"] == str(expected_root / "console" / "launchd.err.log")
        assert Path(definition["StandardOutPath"]).is_absolute()


def test_rendered_plist_contains_no_secret(renderer, tmp_path, capsys):
    auth_file = _auth_file(tmp_path)

    _, contents = _render(renderer, capsys, _print_args(auth_file, tmp_path / "artifacts"))

    assert PASSWORD.encode("utf-8") not in contents
    assert CREDENTIAL not in contents
    # plistlib represents bytes as base64 <data>.  Guard that form too so a
    # future refactor cannot serialize load_auth_file()'s return value while
    # still passing only the cleartext assertions (Rock 4 review finding).
    assert base64.b64encode(CREDENTIAL) not in contents


def test_renderer_fails_closed(renderer, tmp_path, monkeypatch):
    auth_file = _auth_file(tmp_path)
    loose_auth_file = tmp_path / "loose-auth"
    loose_auth_file.write_text(f"operator:{PASSWORD}\n", encoding="utf-8")
    loose_auth_file.chmod(0o644)
    not_a_directory = tmp_path / "octopus-file"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    missing_python = tmp_path / "missing-python"
    common = ["--auth-file", str(auth_file), "--python", sys.executable, "--print"]
    refused_arguments = (
        ["--auth-file", str(loose_auth_file), "--python", sys.executable, "--print"],
        [*common, "--behind-proxy"],
        [*common, "--octopus-run", str(not_a_directory)],
        ["--auth-file", str(auth_file), "--python", str(missing_python), "--print"],
        [*common, "--bind", "192.0.2.10"],
        [*common, "--label", "not-allowed"],
    )

    for arguments in refused_arguments:
        with pytest.raises(SystemExit) as error:
            renderer.main(arguments)
        assert error.value.code == 2

    monkeypatch.setattr(renderer, "DEFAULT_PYTHON", tmp_path / "missing-default-python")
    with pytest.raises(SystemExit) as error:
        renderer.main(["--auth-file", str(auth_file), "--print"])
    assert error.value.code == 2


def test_default_python_is_used_when_flag_absent(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    monkeypatch.setattr(renderer, "DEFAULT_PYTHON", Path(sys.executable))

    definition, _ = _render(
        renderer,
        capsys,
        [
            "--auth-file",
            str(auth_file),
            "--cron-tracking-root",
            str(tmp_path / "artifacts"),
            "--print",
        ],
    )

    assert definition["ProgramArguments"][0] == sys.executable


def test_install_writes_agent_and_creates_log_dir(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    home = tmp_path / "home"
    root = tmp_path / "artifacts"
    monkeypatch.setenv("HOME", str(home))

    assert renderer.main([
        "--auth-file",
        str(auth_file),
        "--python",
        sys.executable,
        "--cron-tracking-root",
        str(root),
        "--install",
    ]) == 0
    capsys.readouterr()

    agent = home / "Library" / "LaunchAgents" / "com.all-stock-dca.console.plist"
    assert agent.is_file()
    assert stat.S_IMODE(agent.stat().st_mode) == 0o644
    assert (root / "console").is_dir()
    definition = plistlib.loads(agent.read_bytes())
    assert definition["StandardOutPath"] == str(root / "console" / "launchd.out.log")


def test_install_prints_bootout_before_bootstrap(renderer, tmp_path, monkeypatch, capsys):
    auth_file = _auth_file(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert renderer.main([
        "--auth-file",
        str(auth_file),
        "--python",
        sys.executable,
        "--cron-tracking-root",
        str(tmp_path / "artifacts"),
        "--install",
    ]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert lines == [
        "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist   # load + start",
        "launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state|pid'             # status",
        "launchctl kickstart -k gui/$(id -u)/com.all-stock-dca.console                            # restart an UNCHANGED definition (e.g. after rotating the auth file)",
        "launchctl bootout gui/$(id -u)/com.all-stock-dca.console                                 # stop + unload; REQUIRED before bootstrap whenever the plist changed",
        "# apply pair, printed last, for a changed plist:",
        "launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist",
    ]
    apply_pair = lines[-1]
    assert apply_pair.index("launchctl bootout") < apply_pair.index("launchctl bootstrap")
