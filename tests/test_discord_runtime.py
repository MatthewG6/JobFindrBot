from pathlib import Path
import os

import certifi

from app.discord_runtime import prepare_discord_runtime


def test_runtime_loads_local_env_without_overriding_existing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".env").write_text(
        "DISCORD_BOT_TOKEN=file-token\n"
        "DISCORD_APPLICATION_ID=101\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "process-token")
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    prepare_discord_runtime(tmp_path)

    assert os.environ["DISCORD_BOT_TOKEN"] == "process-token"
    assert os.environ["DISCORD_APPLICATION_ID"] == "101"
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert (tmp_path / "data" / "logs").is_dir()


def test_runtime_preserves_explicit_certificate_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")

    prepare_discord_runtime(tmp_path)

    assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"
