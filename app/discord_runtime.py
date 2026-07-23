from pathlib import Path
import os

import certifi
from dotenv import load_dotenv


def prepare_discord_runtime(project_root: Path) -> None:
    """Load local secrets and prepare trusted TLS/log paths.

    Existing process environment values win over `.env`, which lets a future
    service manager or secret store override local development configuration.
    """
    load_dotenv(project_root / ".env", override=False)
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    (project_root / "data" / "logs").mkdir(parents=True, exist_ok=True)
