from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.discord_bot import build_discord_client
from app.discord_config import DiscordConfigError, DiscordSettings
from app.discord_runtime import prepare_discord_runtime


def main() -> None:
    prepare_discord_runtime(PROJECT_ROOT)
    try:
        settings = DiscordSettings.from_environment()
    except DiscordConfigError as error:
        raise SystemExit(f"Discord configuration error: {error}") from error

    client = build_discord_client(settings)
    client.run(settings.token)


if __name__ == "__main__":
    main()
