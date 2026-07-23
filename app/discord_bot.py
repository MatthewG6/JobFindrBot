from collections.abc import Awaitable, Callable
from typing import Protocol

import discord

from app.discord_config import DiscordSettings
from app.discord_service import (
    DiscordAccessPolicy,
    DiscordCommandService,
    DiscordContextStorage,
    DiscordNotificationError,
    DiscordNotificationService,
)
from app.storage import JobStorage


class InteractionResponse(Protocol):
    def send_message(
        self,
        content: str,
        *,
        ephemeral: bool,
    ) -> Awaitable[None]:
        """Respond to a Discord interaction."""

    def defer(
        self,
        *,
        ephemeral: bool,
        thinking: bool,
    ) -> Awaitable[None]:
        """Acknowledge an interaction before slower external work."""


class InteractionFollowup(Protocol):
    def send(
        self,
        content: str,
        *,
        ephemeral: bool,
    ) -> Awaitable[object]:
        """Send a result after the initial interaction acknowledgement."""


class CommandInteraction(Protocol):
    id: int
    guild_id: int | None
    channel_id: int
    response: InteractionResponse
    followup: InteractionFollowup

    @property
    def user(self) -> object:
        """Discord user with an integer id attribute."""


class DiscordInteractionController:
    def __init__(
        self,
        access_policy: DiscordAccessPolicy,
        context_storage: DiscordContextStorage,
        command_service: DiscordCommandService,
        notification_service: DiscordNotificationService,
    ) -> None:
        self.access_policy = access_policy
        self.context_storage = context_storage
        self.command_service = command_service
        self.notification_service = notification_service

    async def authorize_and_claim(
        self,
        interaction: CommandInteraction,
    ) -> bool:
        user_id = getattr(interaction.user, "id", None)
        if not isinstance(user_id, int) or not self.access_policy.is_authorized(
            user_id=user_id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        ):
            await interaction.response.send_message(
                "This JobFindrBot command is not authorized here.",
                ephemeral=True,
            )
            return False

        if not self.context_storage.claim_interaction(str(interaction.id)):
            await interaction.response.send_message(
                "This Discord interaction was already processed.",
                ephemeral=True,
            )
            return False
        return True

    async def handle_status(self, interaction: CommandInteraction) -> None:
        if not await self.authorize_and_claim(interaction):
            return
        await interaction.response.send_message(
            self.command_service.status_message(),
            ephemeral=True,
        )

    async def handle_help(self, interaction: CommandInteraction) -> None:
        if not await self.authorize_and_claim(interaction):
            return
        await interaction.response.send_message(
            self.command_service.help_message(),
            ephemeral=True,
        )

    async def handle_test_notification(
        self,
        interaction: CommandInteraction,
    ) -> None:
        if not await self.authorize_and_claim(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.notification_service.send_test_notification()
        except DiscordNotificationError:
            await interaction.followup.send(
                "Discord could not send the test notification. "
                "Check the configured channel and bot permissions.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Test notification sent to the configured channel.",
            ephemeral=True,
        )


class DiscordPyGateway:
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    async def send_message(self, channel_id: int, content: str) -> str:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        send: Callable[..., Awaitable[discord.Message]] | None = getattr(
            channel,
            "send",
            None,
        )
        if send is None:
            raise TypeError("Configured Discord channel cannot receive messages")

        message = await send(content)
        return str(message.id)


class JobFindrDiscordClient(discord.Client):
    def __init__(
        self,
        settings: DiscordSettings,
        storage: JobStorage,
    ) -> None:
        super().__init__(
            intents=discord.Intents.none(),
            application_id=settings.application_id,
        )
        self.settings = settings
        self.command_guild = discord.Object(id=settings.guild_id)
        self.tree = discord.app_commands.CommandTree(self)
        gateway = DiscordPyGateway(self)
        self.controller = DiscordInteractionController(
            access_policy=DiscordAccessPolicy(settings),
            context_storage=DiscordContextStorage(storage),
            command_service=DiscordCommandService(storage),
            notification_service=DiscordNotificationService(settings, gateway),
        )
        self.register_commands()

    def register_commands(self) -> None:
        @self.tree.command(
            name="status",
            description="Show JobFindrBot's local status without starting work.",
            guild=self.command_guild,
        )
        async def status(interaction: discord.Interaction) -> None:
            await self.controller.handle_status(interaction)

        @self.tree.command(
            name="help",
            description="Show the safe commands currently available.",
            guild=self.command_guild,
        )
        async def help_command(interaction: discord.Interaction) -> None:
            await self.controller.handle_help(interaction)

        @self.tree.command(
            name="test-notification",
            description="Send a connection test to the configured channel.",
            guild=self.command_guild,
        )
        async def test_notification(interaction: discord.Interaction) -> None:
            await self.controller.handle_test_notification(interaction)

    async def setup_hook(self) -> None:
        await self.tree.sync(guild=self.command_guild)


def build_discord_client(
    settings: DiscordSettings,
    storage: JobStorage | None = None,
) -> JobFindrDiscordClient:
    return JobFindrDiscordClient(settings, storage or JobStorage())
