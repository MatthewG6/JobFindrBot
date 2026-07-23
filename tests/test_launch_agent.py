from pathlib import Path
import plistlib
from subprocess import CompletedProcess

from app.launch_agent import (
    LAUNCH_AGENT_LABEL,
    LaunchAgentManager,
    build_launch_agent,
)


class FakeCommandRunner:
    def __init__(self) -> None:
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
        return CompletedProcess(command, 0, stdout="service = running\n")


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
    assert (project / "data" / "logs").is_dir()
    assert runner.commands == [
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
    runner = FakeCommandRunner()
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
    runner = FakeCommandRunner()
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
        [
            "launchctl",
            "bootout",
            "gui/501",
            str(manager.plist_path),
        ]
    ]
