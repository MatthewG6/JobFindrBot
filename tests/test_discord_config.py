import pytest

from app.discord_config import DiscordConfigError, DiscordSettings


def valid_environment() -> dict[str, str]:
    return {
        "DISCORD_BOT_TOKEN": "test-token",
        "DISCORD_APPLICATION_ID": "101",
        "DISCORD_GUILD_ID": "202",
        "DISCORD_CHANNEL_ID": "303",
        "DISCORD_ALLOWED_USER_ID": "404",
    }


def test_discord_settings_load_required_environment() -> None:
    settings = DiscordSettings.from_mapping(valid_environment())

    assert settings.application_id == 101
    assert settings.guild_id == 202
    assert settings.channel_id == 303
    assert settings.allowed_user_id == 404
    assert settings.token == "test-token"


def test_discord_settings_report_all_missing_values() -> None:
    with pytest.raises(DiscordConfigError) as error:
        DiscordSettings.from_mapping({})

    message = str(error.value)
    assert "DISCORD_BOT_TOKEN" in message
    assert "DISCORD_APPLICATION_ID" in message
    assert "DISCORD_GUILD_ID" in message
    assert "DISCORD_CHANNEL_ID" in message
    assert "DISCORD_ALLOWED_USER_ID" in message


@pytest.mark.parametrize(
    "key,value",
    [
        ("DISCORD_APPLICATION_ID", "not-a-number"),
        ("DISCORD_GUILD_ID", "0"),
        ("DISCORD_CHANNEL_ID", "-1"),
        ("DISCORD_ALLOWED_USER_ID", ""),
    ],
)
def test_discord_settings_reject_invalid_ids(key: str, value: str) -> None:
    environment = valid_environment()
    environment[key] = value

    with pytest.raises(DiscordConfigError, match=key):
        DiscordSettings.from_mapping(environment)


def test_discord_settings_do_not_expose_token_in_repr() -> None:
    settings = DiscordSettings.from_mapping(valid_environment())

    assert "test-token" not in repr(settings)
