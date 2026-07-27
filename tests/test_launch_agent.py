from pathlib import Path
import plistlib
from subprocess import CompletedProcess

from app.launch_agent import (
    LAUNCH_AGENT_LABEL,
    LaunchAgentManager,
    build_launch_agent,
)


class FakeCommandRunner:
    def __init__(
        self,
        responses: dict[tuple[str, ...], int] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        default_return_code = 113 if command[:2] == ["launchctl", "print"] else 0
        return_code = self.responses.get(tuple(command), default_return_code)
        return CompletedProcess(
            command,
            return_code,
            stdout="service = running\n" if return_code == 0 else "",
            stderr="launchctl failed\n" if return_code else "",
        )


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "JobFindrBot"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / "scripts").mkdir()
    (project / "scripts" / "run_discord_bot.py").touch()
    (project / ".env").write_text(
        "DISCORD_BOT_TOKEN=test-token\n",
        encoding="utf-8",
    )
    (project / ".env").chmod(0o644)
    return project


def test_launch_agent_contains_no_discord_secret(tmp_path: Path) -> None:
    project = make_project(tmp_path)

    configuration = build_launch_agent(project)
    serialized = plistlib.dumps(configuration).decode()

    assert configuration["Label"] == LAUNCH_AGENT_LABEL
    assert configuration["RunAtLoad"] is True
    assert configuration["KeepAlive"] is True
    assert "DISCORD_BOT_TOKEN" not in serialized
    assert ".env" not in serialized
    assert configuration["ProgramArguments"] == [
        str(project / ".venv" / "bin" / "python"),
        str(project / "scripts" / "run_discord_bot.py"),
    ]
    assert configuration["StandardOutPath"] == str(
        project / "data" / "logs" / "discord-bot.log"
    )
    assert configuration["StandardErrorPath"] == str(
        project / "data" / "logs" / "discord-bot.error.log"
    )


def test_install_writes_private_plist_and_bootstraps_service(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runner = FakeCommandRunner()
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )

    manager.install()

    assert manager.plist_path.is_file()
    assert manager.plist_path.stat().st_mode & 0o777 == 0o600
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    assert manager.log_directory.stat().st_mode & 0o777 == 0o700
    assert (
        manager.log_directory / "discord-bot.log"
    ).stat().st_mode & 0o777 == 0o600
    assert (
        manager.log_directory / "discord-bot.error.log"
    ).stat().st_mode & 0o777 == 0o600
    assert runner.commands == [
        [
            "launchctl",
            "print",
            f"gui/501/{LAUNCH_AGENT_LABEL}",
        ],
        [
            "launchctl",
            "bootstrap",
            "gui/501",
            str(manager.plist_path),
        ],
        [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/501/{LAUNCH_AGENT_LABEL}",
        ],
    ]


def test_install_requires_local_environment_file(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / ".env").unlink()
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=FakeCommandRunner(),
    )

    try:
        manager.install()
    except FileNotFoundError as error:
        assert ".env" in str(error)
    else:
        raise AssertionError("Expected install to require a local .env file")


def test_restart_and_status_use_scoped_launchctl_target(
    tmp_path: Path,
) -> None:
    status_command = (
        "launchctl",
        "print",
        f"gui/501/{LAUNCH_AGENT_LABEL}",
    )
    runner = FakeCommandRunner({status_command: 0})
    manager = LaunchAgentManager(
        project_root=make_project(tmp_path),
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )

    manager.restart()
    result = manager.status()

    assert result == "service = running\n"
    assert runner.commands == [
        [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/501/{LAUNCH_AGENT_LABEL}",
        ],
        [
            "launchctl",
            "print",
            f"gui/501/{LAUNCH_AGENT_LABEL}",
        ],
    ]


def test_uninstall_boots_out_before_removing_plist(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    service_target = f"gui/501/{LAUNCH_AGENT_LABEL}"
    runner = FakeCommandRunner(
        {
            ("launchctl", "print", service_target): 0,
            ("launchctl", "bootout", service_target): 0,
        }
    )
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )
    manager.plist_path.parent.mkdir(parents=True)
    manager.plist_path.write_text("placeholder", encoding="utf-8")

    manager.uninstall()

    assert not manager.plist_path.exists()
    assert runner.commands == [
        ["launchctl", "print", service_target],
        ["launchctl", "bootout", service_target],
    ]


def test_uninstall_recovers_loaded_service_when_plist_is_missing(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    service_target = f"gui/501/{LAUNCH_AGENT_LABEL}"
    runner = FakeCommandRunner(
        {
            ("launchctl", "print", service_target): 0,
            ("launchctl", "bootout", service_target): 0,
        }
    )
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )

    manager.uninstall()

    assert runner.commands == [
        ["launchctl", "print", service_target],
        ["launchctl", "bootout", service_target],
    ]


def test_uninstall_failure_preserves_plist_and_raises(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    service_target = f"gui/501/{LAUNCH_AGENT_LABEL}"
    runner = FakeCommandRunner(
        {
            ("launchctl", "print", service_target): 0,
            ("launchctl", "bootout", service_target): 5,
        }
    )
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )
    manager.plist_path.parent.mkdir(parents=True)
    manager.plist_path.write_text("keep me", encoding="utf-8")

    try:
        manager.uninstall()
    except Exception as error:
        assert getattr(error, "returncode", None) == 5
    else:
        raise AssertionError("Expected failed bootout to abort uninstall")

    assert manager.plist_path.read_text(encoding="utf-8") == "keep me"


def test_reinstall_failure_does_not_replace_plist_or_bootstrap(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    service_target = f"gui/501/{LAUNCH_AGENT_LABEL}"
    runner = FakeCommandRunner(
        {
            ("launchctl", "print", service_target): 0,
            ("launchctl", "bootout", service_target): 5,
        }
    )
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )
    manager.plist_path.parent.mkdir(parents=True)
    manager.plist_path.write_text("original", encoding="utf-8")

    try:
        manager.install()
    except Exception as error:
        assert getattr(error, "returncode", None) == 5
    else:
        raise AssertionError("Expected failed bootout to abort reinstall")

    assert manager.plist_path.read_text(encoding="utf-8") == "original"
    assert runner.commands == [
        ["launchctl", "print", service_target],
        ["launchctl", "bootout", service_target],
    ]


def test_install_aborts_when_loaded_state_cannot_be_determined(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    service_target = f"gui/501/{LAUNCH_AGENT_LABEL}"
    runner = FakeCommandRunner(
        {
            ("launchctl", "print", service_target): 1,
        }
    )
    manager = LaunchAgentManager(
        project_root=project,
        home_directory=tmp_path / "home",
        user_id=501,
        command_runner=runner,
    )

    try:
        manager.install()
    except Exception as error:
        assert getattr(error, "returncode", None) == 1
    else:
        raise AssertionError("Expected launchctl query failure to abort install")

    assert not manager.plist_path.exists()
    assert runner.commands == [["launchctl", "print", service_target]]


def test_manager_rejects_sudo_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.launch_agent.os.geteuid", lambda: 0)

    try:
        LaunchAgentManager(
            project_root=make_project(tmp_path),
            home_directory=tmp_path / "home",
        )
    except PermissionError as error:
        assert "sudo" in str(error)
    else:
        raise AssertionError("Expected root service management to be rejected")
