import json
import os
from pathlib import Path

import pytest

from app.source_credentials import (
    SourceCredentialError,
    SourceNotConfigured,
    load_private_source_credentials,
    source_credentials,
)


def test_missing_credentials_are_not_configured(tmp_path: Path) -> None:
    with pytest.raises(SourceNotConfigured):
        source_credentials(
            "adzuna",
            ("app_id", "app_key"),
            tmp_path / "missing.json",
        )


def test_credentials_permissions_are_repaired(tmp_path: Path) -> None:
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir(mode=0o755)
    path = credentials_dir / "source_api.json"
    path.write_text(
        json.dumps({"adzuna": {"app_id": "id", "app_key": "key"}}),
        encoding="utf-8",
    )
    os.chmod(path, 0o644)

    values = source_credentials("adzuna", ("app_id", "app_key"), path)

    assert values == {"app_id": "id", "app_key": "key"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert credentials_dir.stat().st_mode & 0o777 == 0o700


def test_environment_credentials_override_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "source_api.json"
    path.write_text(
        json.dumps({"adzuna": {"app_id": "file-id", "app_key": "file-key"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ADZUNA_APP_ID", "environment-id")

    values = source_credentials("adzuna", ("app_id", "app_key"), path)

    assert values == {"app_id": "environment-id", "app_key": "file-key"}


def test_credentials_reject_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "credentials.json"
    link.symlink_to(target)

    with pytest.raises(SourceCredentialError):
        load_private_source_credentials(link)


@pytest.mark.parametrize("content", ["[]", "not-json"])
def test_credentials_reject_invalid_json_shape(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "source_api.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SourceCredentialError):
        load_private_source_credentials(path)
