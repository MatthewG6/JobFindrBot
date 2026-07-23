from collections.abc import Callable
from pathlib import Path
import os
import plistlib
import subprocess


LAUNCH_AGENT_LABEL = "com.matthewglassman.jobfindrbot.discord"


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def build_launch_agent(project_root: Path) -> dict:
    python_path = project_root / ".venv" / "bin" / "python"
    bot_script = project_root / "scripts" / "run_discord_bot.py"
    log_directory = project_root / "data" / "logs"

    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(python_path), str(bot_script)],
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_directory / "discord-bot.log"),
        "StandardErrorPath": str(log_directory / "discord-bot.error.log"),
    }


class LaunchAgentManager:
    def __init__(
        self,
        project_root: Path,
        home_directory: Path | None = None,
        user_id: int | None = None,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.project_root = project_root.resolve()
        self.home_directory = (home_directory or Path.home()).resolve()
        self.user_id = user_id if user_id is not None else os.getuid()
        self.command_runner = command_runner
        self.plist_path = (
            self.home_directory
            / "Library"
            / "LaunchAgents"
            / f"{LAUNCH_AGENT_LABEL}.plist"
        )

    @property
    def domain(self) -> str:
        return f"gui/{self.user_id}"

    @property
    def service_target(self) -> str:
        return f"{self.domain}/{LAUNCH_AGENT_LABEL}"

    def validate_runtime(self) -> None:
        required_paths = [
            self.project_root / ".venv" / "bin" / "python",
            self.project_root / "scripts" / "run_discord_bot.py",
            self.project_root / ".env",
        ]
        missing = [str(path) for path in required_paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Discord service runtime is incomplete: " + ", ".join(missing)
            )
        (self.project_root / ".env").chmod(0o600)

    def write_plist(self) -> None:
        self.validate_runtime()
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        (self.project_root / "data" / "logs").mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = self.plist_path.with_suffix(".plist.tmp")
        temporary_path.write_bytes(
            plistlib.dumps(build_launch_agent(self.project_root))
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(self.plist_path)

    def install(self) -> None:
        already_installed = self.plist_path.exists()
        if already_installed:
            self.command_runner(
                [
                    "launchctl",
                    "bootout",
                    self.domain,
                    str(self.plist_path),
                ],
                check=False,
            )

        self.write_plist()
        self.command_runner(
            [
                "launchctl",
                "bootstrap",
                self.domain,
                str(self.plist_path),
            ],
            check=True,
        )
        self.restart()

    def restart(self) -> None:
        self.command_runner(
            ["launchctl", "kickstart", "-k", self.service_target],
            check=True,
        )

    def status(self) -> str:
        result = self.command_runner(
            ["launchctl", "print", self.service_target],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def uninstall(self) -> None:
        if not self.plist_path.exists():
            return

        self.command_runner(
            [
                "launchctl",
                "bootout",
                self.domain,
                str(self.plist_path),
            ],
            check=False,
        )
        self.plist_path.unlink()
