import asyncio
from pathlib import Path

import pytest

from app.discord_config import DiscordSettings
from app.discord_service import (
    DiscordAccessPolicy,
    DiscordCommandService,
    DiscordContextStorage,
    DiscordNotificationError,
    DiscordNotificationService,
)
from app.storage import JobStorage


def make_settings() -> DiscordSettings:
    return DiscordSettings(
        token="test-token",
        application_id=101,
        guild_id=202,
        channel_id=303,
        allowed_user_id=404,
    )


class FakeGateway:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, channel_id: int, content: str) -> str:
        if self.fail:
            raise RuntimeError("channel unavailable")
        self.sent.append((channel_id, content))
        return "message-1"


def test_access_policy_requires_exact_user_guild_and_channel() -> None:
    policy = DiscordAccessPolicy(make_settings())

    assert policy.is_authorized(user_id=404, guild_id=202, channel_id=303)
    assert not policy.is_authorized(user_id=999, guild_id=202, channel_id=303)
    assert not policy.is_authorized(user_id=404, guild_id=999, channel_id=303)
    assert not policy.is_authorized(user_id=404, guild_id=202, channel_id=999)
    assert not policy.is_authorized(user_id=404, guild_id=None, channel_id=303)


def test_interaction_claim_survives_storage_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.json"
    first = DiscordContextStorage(JobStorage(db_path))
    second = DiscordContextStorage(JobStorage(db_path))

    assert first.claim_interaction("interaction-1") is True
    assert second.claim_interaction("interaction-1") is False


def test_context_can_be_recovered_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.json"
    first = DiscordContextStorage(JobStorage(db_path))
    first.save_context(
        message_id="message-1",
        channel_id=303,
        job_id=7,
        application_id=9,
    )

    recovered = DiscordContextStorage(JobStorage(db_path)).get_context(
        "message-1"
    )

    assert recovered is not None
    assert recovered["job_id"] == 7
    assert recovered["application_id"] == 9
    assert recovered["channel_id"] == 303


def test_test_notification_uses_configured_channel() -> None:
    gateway = FakeGateway()
    service = DiscordNotificationService(make_settings(), gateway)

    message_id = asyncio.run(service.send_test_notification())

    assert message_id == "message-1"
    assert gateway.sent == [
        (
            303,
            "JobFindrBot test notification: Discord connection is working.",
        )
    ]


def test_notification_failure_is_sanitized() -> None:
    service = DiscordNotificationService(make_settings(), FakeGateway(fail=True))

    with pytest.raises(
        DiscordNotificationError,
        match="Unable to send Discord notification",
    ) as error:
        asyncio.run(service.send_test_notification())

    assert "channel unavailable" not in str(error.value)


def test_status_command_reports_counts_without_starting_work(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    service = DiscordCommandService(storage)

    result = service.status_message()

    assert "Saved jobs: 0" in result
    assert "Tracked applications: 0" in result
    assert "No application action was started." in result


def test_help_command_only_lists_safe_foundation_commands(tmp_path: Path) -> None:
    service = DiscordCommandService(JobStorage(tmp_path / "jobs.json"))

    result = service.help_message()

    assert "/status" in result
    assert "/help" in result
    assert "/test-notification" in result
    assert "submit" not in result.lower()
