from datetime import UTC, datetime
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
from typing import Callable
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_LABEL = "com.jobfindrbot.scan"
DEFAULT_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
)
SCAN_INTERVAL_SECONDS = 30 * 60
HEALTH_PATH = PROJECT_ROOT / "data" / "scheduler_health.json"
HEALTH_TIMEOUT_SECONDS = 30


def launchd_configuration(
    project_root: Path = PROJECT_ROOT,
    install_nonce: str | None = None,
) -> dict:
    python_path = project_root / ".venv" / "bin" / "python"
    runner_path = project_root / "scripts" / "run_scheduled_scan.py"
    log_directory = project_root / "logs"
    if not python_path.is_file():
        raise FileNotFoundError(f"Virtualenv Python not found at {python_path}")
    if not runner_path.is_file():
        raise FileNotFoundError(f"Scheduled runner not found at {runner_path}")

    environment = {"PYTHONUNBUFFERED": "1"}
    if install_nonce:
        environment["JOBBOT_SCHEDULER_INSTALL_NONCE"] = install_nonce

    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(python_path), str(runner_path)],
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "StartInterval": SCAN_INTERVAL_SECONDS,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": environment,
        "StandardOutPath": str(log_directory / "scheduled-scan.log"),
        "StandardErrorPath": str(log_directory / "scheduled-scan-error.log"),
    }


def write_plist(plist_path: Path, configuration: dict) -> Path:
    PROJECT_ROOT.joinpath("logs").mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{plist_path.name}.",
        suffix=".tmp",
        dir=plist_path.parent,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        plistlib.dump(configuration, temporary_file, sort_keys=True)
    try:
        temporary_path.chmod(0o644)
        temporary_path.replace(plist_path)
        plist_path.chmod(0o644)
    finally:
        temporary_path.unlink(missing_ok=True)
    return plist_path


def install_plist(
    plist_path: Path = DEFAULT_PLIST_PATH,
    install_nonce: str | None = None,
) -> Path:
    return write_plist(
        plist_path,
        launchd_configuration(install_nonce=install_nonce),
    )


def launchd_targets() -> tuple[str, str]:
    domain = f"gui/{os.getuid()}"
    return domain, f"{domain}/{LAUNCHD_LABEL}"


def launch_agent_is_loaded(run_command: Callable = subprocess.run) -> bool:
    _, service = launchd_targets()
    result = run_command(
        ["launchctl", "print", service],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def stop_launch_agent(run_command: Callable = subprocess.run) -> None:
    _, service = launchd_targets()
    run_command(
        ["launchctl", "bootout", service],
        check=False,
        capture_output=True,
        text=True,
    )


def activate_launch_agent(
    plist_path: Path,
    run_command: Callable = subprocess.run,
) -> str:
    domain, service = launchd_targets()
    common_options = {"capture_output": True, "text": True}
    run_command(
        ["launchctl", "bootout", service],
        check=False,
        **common_options,
    )
    run_command(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
        **common_options,
    )
    run_command(
        ["launchctl", "enable", service],
        check=True,
        **common_options,
    )
    run_command(
        ["launchctl", "kickstart", service],
        check=True,
        **common_options,
    )
    run_command(
        ["launchctl", "print", service],
        check=True,
        **common_options,
    )
    return service


def wait_for_scheduler_health(
    started_at: datetime,
    install_nonce: str,
    health_path: Path = HEALTH_PATH,
    timeout_seconds: int = HEALTH_TIMEOUT_SECONDS,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
            completed_at = datetime.fromisoformat(health["completed_at"])
        except (KeyError, OSError, ValueError, json.JSONDecodeError, TypeError):
            time.sleep(0.25)
            continue
        if (
            completed_at.tzinfo is not None
            and completed_at >= started_at
            and health.get("install_nonce") == install_nonce
        ):
            if health.get("success") is not True:
                raise RuntimeError("LaunchAgent preflight scan reported errors")
            return health
        time.sleep(0.25)
    raise TimeoutError(
        "LaunchAgent did not complete its preflight scan; check local logs"
    )


def restore_plist_bytes(plist_path: Path, content: bytes) -> None:
    configuration = plistlib.loads(content)
    if not isinstance(configuration, dict):
        raise ValueError("Previous launchd configuration is invalid")
    write_plist(plist_path, configuration)


def install_and_activate(
    plist_path: Path = DEFAULT_PLIST_PATH,
    run_command: Callable = subprocess.run,
    wait_for_health: Callable = wait_for_scheduler_health,
) -> tuple[Path, str]:
    previous_content = plist_path.read_bytes() if plist_path.is_file() else None
    was_loaded = launch_agent_is_loaded(run_command)
    install_nonce = uuid.uuid4().hex

    try:
        install_plist(plist_path, install_nonce=install_nonce)
        started_at = datetime.now(UTC)
        service = activate_launch_agent(plist_path, run_command=run_command)
        wait_for_health(started_at, install_nonce)
        return plist_path, service
    except BaseException as install_error:
        try:
            if previous_content is None:
                plist_path.unlink(missing_ok=True)
                stop_launch_agent(run_command)
            else:
                restore_plist_bytes(plist_path, previous_content)
                if was_loaded:
                    activate_launch_agent(plist_path, run_command=run_command)
                else:
                    stop_launch_agent(run_command)
        except Exception as rollback_error:
            raise RuntimeError(
                "Scheduler installation and rollback both failed"
            ) from rollback_error
        raise install_error


def main() -> None:
    plist_path, service = install_and_activate()
    print(f"Installed launchd configuration: {plist_path}")
    print(f"Service: {service}")
    print(f"Interval seconds: {SCAN_INTERVAL_SECONDS}")
    print("Preflight scan: passed")


if __name__ == "__main__":
    main()
