from datetime import UTC, datetime, timedelta
import json
import plistlib
from pathlib import Path
import subprocess

import pytest

from scripts.install_launchd_scheduler import (
    LAUNCHD_LABEL,
    SCAN_INTERVAL_SECONDS,
    activate_launch_agent,
    install_and_activate,
    install_plist,
    launchd_configuration,
    wait_for_scheduler_health,
)


def test_launchd_configuration_runs_every_thirty_minutes() -> None:
    configuration = launchd_configuration()

    assert configuration["Label"] == LAUNCHD_LABEL
    assert configuration["StartInterval"] == 1800
    assert SCAN_INTERVAL_SECONDS == 1800
    assert configuration["RunAtLoad"] is True
    assert configuration["ProgramArguments"][0].endswith(
        "/.venv/bin/python"
    )
    assert configuration["ProgramArguments"][1].endswith(
        "/scripts/run_scheduled_scan.py"
    )
    assert configuration["WorkingDirectory"].endswith("/JobFindrBot")


def test_install_plist_writes_valid_launchd_file(tmp_path: Path) -> None:
    plist_path = install_plist(tmp_path / "com.jobfindrbot.scan.plist")

    with plist_path.open("rb") as plist_file:
        configuration = plistlib.load(plist_file)

    assert configuration["Label"] == LAUNCHD_LABEL
    assert configuration["StartInterval"] == 1800
    assert plist_path.stat().st_mode & 0o777 == 0o644


def test_activate_launch_agent_reloads_and_kickstarts_service(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(command, check, **kwargs):
        calls.append((command, check))

    plist_path = tmp_path / "com.jobfindrbot.scan.plist"
    service = activate_launch_agent(plist_path, run_command=fake_run)

    assert service.endswith(f"/{LAUNCHD_LABEL}")
    assert [call[0][1] for call in calls] == [
        "bootout",
        "bootstrap",
        "enable",
        "kickstart",
        "print",
    ]
    assert calls[0][1] is False
    assert all(check is True for _, check in calls[1:])
    kickstart_command = next(
        command for command, _ in calls if command[1] == "kickstart"
    )
    assert "-k" not in kickstart_command


def test_wait_for_scheduler_health_requires_new_success(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    started_at = datetime.now(UTC)
    health_path.write_text(
        json.dumps(
            {
                "completed_at": (started_at + timedelta(seconds=1)).isoformat(),
                "error_count": 0,
                "install_nonce": "fresh-nonce",
                "success": True,
            }
        ),
        encoding="utf-8",
    )

    health = wait_for_scheduler_health(
        started_at,
        "fresh-nonce",
        health_path=health_path,
        timeout_seconds=1,
    )

    assert health["success"] is True


def test_wait_for_scheduler_health_rejects_failed_run(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    started_at = datetime.now(UTC)
    health_path.write_text(
        json.dumps(
            {
                "completed_at": (started_at + timedelta(seconds=1)).isoformat(),
                "error_count": 1,
                "install_nonce": "fresh-nonce",
                "success": False,
            }
        ),
        encoding="utf-8",
    )

    try:
        wait_for_scheduler_health(
            started_at,
            "fresh-nonce",
            health_path=health_path,
            timeout_seconds=1,
        )
    except RuntimeError as error:
        assert "reported errors" in str(error)
    else:
        raise AssertionError("failed preflight health was accepted")


def test_wait_for_scheduler_health_rejects_stale_nonce(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    started_at = datetime.now(UTC)
    health_path.write_text(
        json.dumps(
            {
                "completed_at": (started_at + timedelta(days=1)).isoformat(),
                "error_count": 0,
                "install_nonce": "old-nonce",
                "success": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TimeoutError):
        wait_for_scheduler_health(
            started_at,
            "fresh-nonce",
            health_path=health_path,
            timeout_seconds=0,
        )


def test_failed_upgrade_restores_previous_plist(tmp_path: Path) -> None:
    plist_path = tmp_path / "com.jobfindrbot.scan.plist"
    previous = {"Label": LAUNCHD_LABEL, "StartInterval": 3600}
    with plist_path.open("wb") as plist_file:
        plistlib.dump(previous, plist_file)

    bootstrap_attempts = 0

    def fake_run(command, check, **kwargs):
        nonlocal bootstrap_attempts
        if command[1] == "print":
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "bootstrap":
            bootstrap_attempts += 1
            if bootstrap_attempts == 1:
                raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(subprocess.CalledProcessError):
        install_and_activate(plist_path, run_command=fake_run)

    with plist_path.open("rb") as plist_file:
        restored = plistlib.load(plist_file)
    assert restored == previous
    assert bootstrap_attempts == 2
