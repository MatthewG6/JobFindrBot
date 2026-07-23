import asyncio
from pathlib import Path

from app.discord_bot import (
    DiscordInteractionController,
    DiscordPyGateway,
    build_discord_client,
)
from app.discord_config import DiscordSettings
from app.discord_service import (
    DiscordAccessPolicy,
    DiscordCommandService,
    DiscordContextStorage,
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


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeInteraction:
    def __init__(
        self,
        interaction_id: int = 1,
        user_id: int = 404,
        guild_id: int | None = 202,
        channel_id: int = 303,
    ) -> None:
        self.id = interaction_id
        self.user = FakeUser(user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = FakeResponse()


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, channel_id: int, content: str) -> str:
        self.sent.append((channel_id, content))
        return "message-1"


def make_controller(
    tmp_path: Path,
) -> tuple[DiscordInteractionController, FakeGateway]:
    settings = make_settings()
    storage = JobStorage(tmp_path / "jobs.json")
    gateway = FakeGateway()
    return (
        DiscordInteractionController(
            access_policy=DiscordAccessPolicy(settings),
            context_storage=DiscordContextStorage(storage),
            command_service=DiscordCommandService(storage),
            notification_service=DiscordNotificationService(settings, gateway),
        ),
        gateway,
    )


def test_unauthorized_interaction_is_rejected_ephemerally(
    tmp_path: Path,
) -> None:
    controller, _ = make_controller(tmp_path)
    interaction = FakeInteraction(user_id=999)

    asyncio.run(controller.handle_status(interaction))

    assert interaction.response.messages == [
        ("This JobFindrBot command is not authorized here.", True)
    ]


def test_duplicate_interaction_does_not_repeat_action(tmp_path: Path) -> None:
    controller, _ = make_controller(tmp_path)
    first = FakeInteraction(interaction_id=77)
    duplicate = FakeInteraction(interaction_id=77)

    asyncio.run(controller.handle_status(first))
    asyncio.run(controller.handle_status(duplicate))

    assert "JobFindrBot is online." in first.response.messages[0][0]
    assert duplicate.response.messages == [
        ("This Discord interaction was already processed.", True)
    ]


def test_test_notification_acknowledges_and_sends_once(tmp_path: Path) -> None:
    controller, gateway = make_controller(tmp_path)
    interaction = FakeInteraction(interaction_id=88)

    asyncio.run(controller.handle_test_notification(interaction))

    assert len(gateway.sent) == 1
    assert interaction.response.messages == [
        ("Test notification sent to the configured channel.", True)
    ]


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str):
        self.messages.append(content)
        return type("Message", (), {"id": 55})()


class FakeDiscordClient:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel
        self.fetch_count = 0

    def get_channel(self, channel_id: int):
        return None

    async def fetch_channel(self, channel_id: int):
        self.fetch_count += 1
        return self.channel


def test_gateway_fetches_uncached_channel_and_returns_message_id() -> None:
    channel = FakeChannel()
    client = FakeDiscordClient(channel)
    gateway = DiscordPyGateway(client)

    message_id = asyncio.run(gateway.send_message(303, "hello"))

    assert message_id == "55"
    assert client.fetch_count == 1
    assert channel.messages == ["hello"]


def test_client_registers_only_safe_foundation_commands(tmp_path: Path) -> None:
    client = build_discord_client(
        make_settings(),
        storage=JobStorage(tmp_path / "jobs.json"),
    )

    commands = {
        command.name
        for command in client.tree.get_commands(
            guild=client.command_guild,
        )
    }

    assert commands == {"help", "status", "test-notification"}
    asyncio.run(client.close())
