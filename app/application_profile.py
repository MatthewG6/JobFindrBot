from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import Lock, RLock
from typing import Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
    ValidationError,
)
import yaml

from app.models import utc_now
from app.sensitive_data import SensitiveValueCipher


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_APPLICATION_PROFILE_PATH = (
    PROJECT_ROOT / "credentials" / "application_profile.yaml"
)
APPLICATION_PROFILE_SCHEMA_VERSION = 1
REQUIRED_READY_FIELDS = frozenset({"legal_name", "email", "phone"})
DOCUMENT_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
}
DOCUMENT_APPROVAL_SIGNATURE_PURPOSE = "document_approval"
MAX_APPLICATION_DOCUMENT_BYTES = 20_000_000
_PROFILE_LOCKS: dict[Path, RLock] = {}
_PROFILE_LOCKS_GUARD = Lock()


class SensitiveMetadataStorage(Protocol):
    def list_sensitive_values(
        self,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> list[dict]: ...

    def read_sensitive_value(
        self,
        secret_id: str,
        *,
        purpose: str,
        reuse_approved_by: str | None = None,
    ) -> str: ...


def normalize_slug(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized):
        raise ValueError(
            f"{label} must start with a letter and contain only lowercase "
            "letters, numbers, and underscores"
        )
    return normalized


class ApprovedDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    kind: Literal[
        "resume",
        "cover_letter",
        "transcript",
        "portfolio",
        "other",
    ]
    label: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    media_type: Literal[
        "application/pdf",
        "application/rtf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    ]
    revision: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_for: list[str] = Field(min_length=1, max_length=32)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    active: StrictBool = True
    default: StrictBool = False

    @field_validator("document_id")
    @classmethod
    def normalize_document_id(cls, value: str) -> str:
        return normalize_slug(value, "Document ID")

    @field_validator("label", "revision", "approved_by")
    @classmethod
    def strip_nonblank_values(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Document metadata must not be blank")
        return normalized

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.strip()
        path = Path(normalized)
        if path.is_absolute() or not normalized or ".." in path.parts:
            raise ValueError("Document path must be relative to the profile")
        if path.name in {"", "."}:
            raise ValueError("Document path must identify a file")
        return path.as_posix()

    @field_validator("approved_for")
    @classmethod
    def normalize_approved_for(cls, values: list[str]) -> list[str]:
        normalized = [
            normalize_slug(value, "Document purpose") for value in values
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Document purposes must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_document_state(self) -> "ApprovedDocumentMetadata":
        suffix = Path(self.path).suffix.lower()
        expected_media_type = DOCUMENT_MEDIA_TYPES.get(suffix)
        if expected_media_type is None:
            raise ValueError("Document type is not approved")
        if self.media_type != expected_media_type:
            raise ValueError("Document media type does not match its extension")
        if self.default and not self.active:
            raise ValueError("A default document must be active")
        return self


class ApprovedDocument(ApprovedDocumentMetadata):
    approval_key_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    approval_signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def document_approval_payload(
    profile_id: str,
    document: ApprovedDocumentMetadata,
) -> bytes:
    payload = {
        "profile_id": normalize_slug(profile_id, "Profile ID"),
        "schema_version": APPLICATION_PROFILE_SCHEMA_VERSION,
        "document": document.model_dump(mode="json"),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def approve_document(
    profile_id: str,
    metadata: ApprovedDocumentMetadata | dict,
    sensitive_cipher: SensitiveValueCipher,
) -> ApprovedDocument:
    parsed = ApprovedDocumentMetadata.model_validate(metadata)
    signature = sensitive_cipher.sign_metadata(
        document_approval_payload(profile_id, parsed),
        purpose=DOCUMENT_APPROVAL_SIGNATURE_PURPOSE,
    )
    return ApprovedDocument(
        **parsed.model_dump(),
        approval_key_id=sensitive_cipher.key_id,
        approval_signature=signature,
    )


def verify_document_approval(
    profile_id: str,
    document: ApprovedDocument,
    sensitive_cipher: SensitiveValueCipher,
) -> None:
    if document.approval_key_id != sensitive_cipher.key_id:
        raise ValueError("Approved document key does not match the owner key")
    metadata = ApprovedDocumentMetadata.model_validate(
        document.model_dump(
            exclude={"approval_key_id", "approval_signature"}
        )
    )
    if not sensitive_cipher.verify_metadata_signature(
        document_approval_payload(profile_id, metadata),
        document.approval_signature,
        purpose=DOCUMENT_APPROVAL_SIGNATURE_PURPOSE,
    ):
        raise ValueError(
            f"Approved document metadata is not authentic: {document.document_id}"
        )


class ApplicationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt
    profile_id: str
    profile_name: str = Field(min_length=1, max_length=128)
    approval_name: str = Field(min_length=1, max_length=128)
    documents: list[ApprovedDocument] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("profile_id")
    @classmethod
    def normalize_profile_id(cls, value: str) -> str:
        return normalize_slug(value, "Profile ID")

    @field_validator("profile_name", "approval_name")
    @classmethod
    def strip_profile_names(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Application profile names must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_contract(self) -> "ApplicationProfile":
        if self.schema_version != APPLICATION_PROFILE_SCHEMA_VERSION:
            raise ValueError("Unsupported application profile schema version")

        document_ids = [document.document_id for document in self.documents]
        document_paths = [document.path for document in self.documents]
        for values, label in (
            (document_ids, "Document IDs"),
            (document_paths, "Document paths"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")

        defaults: set[tuple[str, str]] = set()
        for document in self.documents:
            if not document.active or not document.default:
                continue
            for purpose in document.approved_for:
                key = (document.kind, purpose)
                if key in defaults:
                    raise ValueError(
                        "Only one active default document is allowed per "
                        "kind and purpose"
                    )
                defaults.add(key)
        return self


def configured_application_profile_path(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> Path:
    try:
        content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError("Jobbot config is not readable YAML") from error
    if not isinstance(content, dict):
        raise ValueError("Jobbot config must be a YAML object")
    configured = content.get("application_profile")
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("Jobbot config must select an application profile")
    path = Path(configured.strip())
    if not path.is_absolute():
        path = config_path.parent / path
    return path


def parse_application_profile(path: Path) -> ApplicationProfile:
    secure_application_profile(path)
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError("Application profile is not readable YAML") from error
    if not isinstance(content, dict):
        raise ValueError("Application profile must be a YAML object")
    try:
        return ApplicationProfile.model_validate(content)
    except ValidationError:
        raise ValueError("Application profile is invalid") from None


def reject_symlinked_components(path: Path) -> None:
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    for component in (absolute_path, *absolute_path.parents):
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError("Application profile paths must not contain symlinks")


def ensure_owner_only_directory(path: Path, *, create: bool = False) -> None:
    reject_symlinked_components(path)
    if not path.exists():
        if not create:
            raise ValueError("Application profile directory does not exist")
        path.mkdir(parents=True, mode=0o700)
    path_stat = path.lstat()
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("Application profile directory must be regular")
    if path_stat.st_mode & 0o077:
        raise ValueError("Application profile directory must be owner-only")


@contextmanager
def application_profile_lock(path: Path):
    resolved = path.resolve()
    with _PROFILE_LOCKS_GUARD:
        thread_lock = _PROFILE_LOCKS.setdefault(resolved, RLock())
    with thread_lock:
        ensure_owner_only_directory(path.parent, create=True)
        lock_path = path.parent / f".{path.name}.lock"
        reject_symlinked_components(lock_path)
        if lock_path.is_symlink():
            raise ValueError("Application profile lock must be a regular file")
        with lock_path.open("a+") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def secure_application_profile(path: Path) -> None:
    reject_symlinked_components(path)
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Application profile must be a regular file")
    ensure_owner_only_directory(path.parent)
    os.chmod(path, 0o600)


def resolved_document_path(profile_path: Path, document: ApprovedDocument) -> Path:
    root = profile_path.parent.resolve()
    path = profile_path.parent / document.path
    reject_symlinked_components(path)
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("Document must remain inside the profile directory") from None
    return resolved


def document_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as document_file:
        for block in iter(lambda: document_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_document_file(
    profile_path: Path,
    document: ApprovedDocument,
    *,
    secure_permissions: bool = True,
) -> bytes:
    path = resolved_document_path(profile_path, document)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise ValueError(
            f"Approved document does not exist: {document.document_id}"
        ) from None
    except OSError as error:
        raise ValueError("Approved document could not be opened safely") from error
    try:
        path_stat = os.fstat(descriptor)
        if not stat.S_ISREG(path_stat.st_mode):
            raise ValueError("Approved document must be a regular file")
        if path_stat.st_size > MAX_APPLICATION_DOCUMENT_BYTES:
            raise ValueError("Approved document exceeds the size limit")
        if secure_permissions:
            os.fchmod(descriptor, 0o600)
        elif path_stat.st_mode & 0o077:
            raise ValueError("Approved document must be owner-only")
        with os.fdopen(descriptor, "rb") as document_file:
            descriptor = -1
            content_buffer = bytearray()
            while True:
                remaining = MAX_APPLICATION_DOCUMENT_BYTES - len(content_buffer)
                block = document_file.read(min(1024 * 1024, remaining + 1))
                if not block:
                    break
                content_buffer.extend(block)
                if len(content_buffer) > MAX_APPLICATION_DOCUMENT_BYTES:
                    raise ValueError("Approved document exceeds the size limit")
            content = bytes(content_buffer)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != document.sha256:
        raise ValueError(
            f"Approved document fingerprint changed: {document.document_id}"
        )
    return content


def readiness_issues(
    profile: ApplicationProfile,
    field_names: set[str],
    *,
    fields_verified: bool,
) -> list[str]:
    issues = [
        f"missing required encrypted field: {field_name}"
        for field_name in sorted(
            REQUIRED_READY_FIELDS - field_names
        )
    ]
    if not fields_verified:
        issues.append("encrypted application fields were not verified")
    if not any(
        document.kind == "resume" and document.active
        for document in profile.documents
    ):
        issues.append("missing an active approved resume")
    return issues


def validate_application_profile(
    profile: ApplicationProfile,
    profile_path: Path,
    *,
    storage: SensitiveMetadataStorage | None = None,
    sensitive_cipher: SensitiveValueCipher | None = None,
    require_ready: bool = False,
    secure_permissions: bool = True,
) -> dict:
    secure_application_profile(profile_path)
    for document in profile.documents:
        if sensitive_cipher is None:
            raise ValueError("Document approval verification requires the owner key")
        verify_document_approval(
            profile.profile_id,
            document,
            sensitive_cipher,
        )
        validate_document_file(
            profile_path,
            document,
            secure_permissions=secure_permissions,
        )

    if require_ready and storage is None:
        raise ValueError("Ready validation requires encrypted-value storage")
    field_names: set[str] = set()
    fields_verified = storage is not None
    if storage is not None:
        records = storage.list_sensitive_values(
            scope="application_profile",
            scope_id=profile.profile_id,
        )
        field_names = {record["field_name"] for record in records}
        if len(field_names) != len(records):
            raise ValueError("Application profile contains duplicate encrypted fields")
        secret_ids = {record["secret_id"] for record in records}
        if len(secret_ids) != len(records):
            raise ValueError("Application profile contains duplicate secret IDs")
        for record in records:
            storage.read_sensitive_value(
                record["secret_id"],
                purpose="owner_review",
            )

    issues = readiness_issues(
        profile,
        field_names,
        fields_verified=fields_verified,
    )
    if require_ready and issues:
        raise ValueError("Application profile is not ready: " + "; ".join(issues))
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "field_count": len(field_names),
        "document_count": len(profile.documents),
        "active_document_count": sum(
            1 for document in profile.documents if document.active
        ),
        "ready": not issues,
        "readiness_issues": issues,
    }


def load_application_profile(
    path: Path,
    *,
    storage: SensitiveMetadataStorage,
    sensitive_cipher: SensitiveValueCipher,
    require_ready: bool = False,
) -> ApplicationProfile:
    profile = parse_application_profile(path)
    validate_application_profile(
        profile,
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
        require_ready=require_ready,
    )
    return profile


def write_application_profile(path: Path, profile: ApplicationProfile) -> None:
    profile = ApplicationProfile.model_validate(profile.model_dump(mode="json"))
    reject_symlinked_components(path)
    ensure_owner_only_directory(path.parent, create=True)
    if path.is_symlink():
        raise ValueError("Application profile must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = yaml.safe_dump(
            profile.model_dump(mode="json"),
            sort_keys=False,
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as profile_file:
            descriptor = -1
            profile_file.write(payload)
            profile_file.flush()
            os.fsync(profile_file.fileno())
        os.replace(temporary_path, path)
        fsync_directory_best_effort(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def fsync_directory_best_effort(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
