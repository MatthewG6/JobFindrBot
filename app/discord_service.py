from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Protocol

from tinydb import Query

from app.discord_config import DiscordSettings
from app.storage import JobStorage


class DiscordGateway(Protocol):
    def send_message(self, channel_id: int, content: str) -> Awaitable[str]:
        """Send one message and return its Discord message identifier."""


class DiscordNotificationError(RuntimeError):
    """A sanitized notification failure safe to display or log."""


class DiscordAccessPolicy:
    def __init__(self, settings: DiscordSettings) -> None:
        self.settings = settings

    def is_authorized(
        self,
        user_id: int,
        guild_id: int | None,
        channel_id: int,
    ) -> bool:
        return (
            user_id == self.settings.allowed_user_id
            and guild_id == self.settings.guild_id
            and channel_id == self.settings.channel_id
        )


class DiscordContextStorage:
    def __init__(self, storage: JobStorage) -> None:
        self.interactions = storage.db.table("discord_interactions")
        self.contexts = storage.db.table("discord_contexts")

    def claim_interaction(self, interaction_id: str) -> bool:
        interactions = Query()
        if self.interactions.contains(interactions.interaction_id == interaction_id):
            return False

        self.interactions.insert(
            {
                "interaction_id": interaction_id,
                "claimed_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    def save_context(
        self,
        message_id: str,
        channel_id: int,
        job_id: int | None = None,
        application_id: int | None = None,
    ) -> dict:
        contexts = Query()
        context = {
            "message_id": message_id,
            "channel_id": channel_id,
            "job_id": job_id,
            "application_id": application_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        existing = self.contexts.get(contexts.message_id == message_id)
        if existing is None:
            self.contexts.insert(context)
        else:
            self.contexts.update(context, contexts.message_id == message_id)
        return context

    def get_context(self, message_id: str) -> dict | None:
        contexts = Query()
        context = self.contexts.get(contexts.message_id == message_id)
        if context is None:
            return None
        return dict(context)


class DiscordNotificationService:
    TEST_MESSAGE = (
        "JobFindrBot test notification: Discord connection is working."
    )

    def __init__(
        self,
        settings: DiscordSettings,
        gateway: DiscordGateway,
    ) -> None:
        self.settings = settings
        self.gateway = gateway

    async def send_test_notification(self) -> str:
        try:
            return await self.gateway.send_message(
                self.settings.channel_id,
                self.TEST_MESSAGE,
            )
        except Exception as error:
            raise DiscordNotificationError(
                "Unable to send Discord notification"
            ) from error


class DiscordCommandService:
    def __init__(self, storage: JobStorage) -> None:
        self.storage = storage

    def status_message(self) -> str:
        return "\n".join(
            [
                "JobFindrBot is online.",
                f"Saved jobs: {len(self.storage.list_jobs())}",
                (
                    "Tracked applications: "
                    f"{len(self.storage.list_applications())}"
                ),
                "No application action was started.",
            ]
        )

    def help_message(self) -> str:
        return "\n".join(
            [
                "Available JobFindrBot commands:",
                "/status — show local job and application counts",
                "/help — show safe foundation commands",
                "/test-notification — verify the configured Discord channel",
            ]
        )
