from collections.abc import Mapping
from dataclasses import dataclass, field
import os


class DiscordConfigError(ValueError):
    """Raised when required Discord configuration is missing or invalid."""


DISCORD_ENVIRONMENT_KEYS = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_APPLICATION_ID",
    "DISCORD_GUILD_ID",
    "DISCORD_CHANNEL_ID",
    "DISCORD_ALLOWED_USER_ID",
)


def positive_int(environment: Mapping[str, str], key: str) -> int:
    raw_value = environment.get(key, "").strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise DiscordConfigError(
            f"{key} must be a positive Discord identifier"
        ) from error

    if value <= 0:
        raise DiscordConfigError(f"{key} must be a positive Discord identifier")
    return value


@dataclass(frozen=True, slots=True)
class DiscordSettings:
    token: str = field(repr=False)
    application_id: int
    guild_id: int
    channel_id: int
    allowed_user_id: int

    @classmethod
    def from_mapping(
        cls,
        environment: Mapping[str, str],
    ) -> "DiscordSettings":
        missing = [
            key
            for key in DISCORD_ENVIRONMENT_KEYS
            if not environment.get(key, "").strip()
        ]
        if missing:
            raise DiscordConfigError(
                "Missing required Discord configuration: "
                + ", ".join(missing)
            )

        return cls(
            token=environment["DISCORD_BOT_TOKEN"].strip(),
            application_id=positive_int(
                environment,
                "DISCORD_APPLICATION_ID",
            ),
            guild_id=positive_int(environment, "DISCORD_GUILD_ID"),
            channel_id=positive_int(environment, "DISCORD_CHANNEL_ID"),
            allowed_user_id=positive_int(
                environment,
                "DISCORD_ALLOWED_USER_ID",
            ),
        )

    @classmethod
    def from_environment(cls) -> "DiscordSettings":
        return cls.from_mapping(os.environ)
