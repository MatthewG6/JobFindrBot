import json
import os
from pathlib import Path
import stat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_CREDENTIALS_PATH = (
    PROJECT_ROOT / "credentials" / "source_api.json"
)


class SourceNotConfigured(RuntimeError):
    pass


class SourceCredentialError(ValueError):
    pass


def load_private_source_credentials(
    credentials_path: Path = DEFAULT_SOURCE_CREDENTIALS_PATH,
) -> dict:
    if credentials_path.is_symlink():
        raise SourceCredentialError(
            "Source credentials must be stored in a regular file"
        )
    if not credentials_path.exists():
        return {}
    file_stat = credentials_path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise SourceCredentialError(
            "Source credentials must be stored in a regular file"
        )
    parent_stat = credentials_path.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
        parent_stat.st_mode
    ):
        raise SourceCredentialError(
            "Source credentials directory must be a regular directory"
        )
    os.chmod(credentials_path.parent, 0o700)
    os.chmod(credentials_path, 0o600)
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceCredentialError(
            "Source credentials are not readable JSON"
        ) from error
    if not isinstance(credentials, dict):
        raise SourceCredentialError("Source credentials must be a JSON object")
    return credentials


def source_credentials(
    source: str,
    required_fields: tuple[str, ...],
    credentials_path: Path = DEFAULT_SOURCE_CREDENTIALS_PATH,
) -> dict[str, str]:
    private_credentials = load_private_source_credentials(credentials_path)
    source_values = private_credentials.get(source, {})
    if not isinstance(source_values, dict):
        raise SourceCredentialError(f"{source} credentials must be an object")

    result: dict[str, str] = {}
    for field in required_fields:
        environment_name = f"{source}_{field}".upper()
        value = os.environ.get(environment_name, source_values.get(field))
        if not isinstance(value, str) or not value.strip():
            raise SourceNotConfigured(
                f"{source} is not configured; missing {field}"
            )
        result[field] = value.strip()
    return result
