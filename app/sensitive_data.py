from __future__ import annotations

import base64
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


KEYCHAIN_SERVICE = "com.jobfindrbot.sensitive-data"
KEYCHAIN_ACCOUNT = "jobbot-local-owner"
ENCRYPTION_ALGORITHM = "AES-256-GCM"
ENCRYPTION_VERSION = 1
RECOVERY_SCHEMA_VERSION = 1
REDACTED_VALUE = "[REDACTED]"


class SensitiveDataError(RuntimeError):
    """A failure that does not include key material or plaintext values."""


class SensitiveDataKeyUnavailable(SensitiveDataError):
    """The expected owner key does not exist in Keychain."""


class SensitiveCategory(str, Enum):
    CONTACT = "contact"
    PERSONAL_IDENTIFIER = "personal_identifier"
    WORK_AUTHORIZATION = "work_authorization"
    COMPENSATION = "compensation"
    DEMOGRAPHIC = "demographic"
    DISABILITY = "disability"
    VETERAN = "veteran"
    BACKGROUND = "background"
    SIGNATURE = "signature"
    LEGAL_ATTESTATION = "legal_attestation"
    OTHER = "other"


class SensitiveReusePolicy(str, Enum):
    CONFIRM_FIRST = "confirm_first"
    NEVER_REUSE = "never_reuse"


NO_RETENTION_BY_DEFAULT = frozenset(
    {
        SensitiveCategory.DEMOGRAPHIC,
        SensitiveCategory.DISABILITY,
        SensitiveCategory.VETERAN,
        SensitiveCategory.BACKGROUND,
        SensitiveCategory.SIGNATURE,
        SensitiveCategory.LEGAL_ATTESTATION,
    }
)


class EncryptedValueEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt = Field(ge=1, le=ENCRYPTION_VERSION)
    algorithm: str = Field(pattern=f"^{re.escape(ENCRYPTION_ALGORITHM)}$")
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    nonce: str = Field(min_length=16, max_length=16)
    ciphertext: str = Field(min_length=24)


class SensitiveValueRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    scope: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    scope_id: str | None = Field(default=None, max_length=128)
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    category: SensitiveCategory
    reuse_policy: SensitiveReusePolicy
    retention_confirmed: StrictBool
    approved_by: str = Field(min_length=1, max_length=128)
    encrypted_value: EncryptedValueEnvelope
    created_at: datetime
    updated_at: datetime


def encryption_context(
    *,
    secret_id: str,
    scope: str,
    scope_id: str | None,
    field_name: str,
) -> bytes:
    context = {
        "field_name": field_name,
        "scope": scope,
        "scope_id": scope_id,
        "secret_id": secret_id,
        "version": ENCRYPTION_VERSION,
    }
    return json.dumps(
        context,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class SensitiveValueCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Sensitive-data key must contain exactly 32 bytes")
        self._key = bytes(key)
        self.key_id = hashlib.sha256(self._key).hexdigest()[:16]

    def encrypt(self, value: str, *, context: bytes) -> EncryptedValueEnvelope:
        if not value:
            raise ValueError("Sensitive value cannot be blank")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            value.encode("utf-8"),
            context,
        )
        return EncryptedValueEnvelope(
            version=ENCRYPTION_VERSION,
            algorithm=ENCRYPTION_ALGORITHM,
            key_id=self.key_id,
            nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        )

    def decrypt(
        self,
        envelope: EncryptedValueEnvelope | dict,
        *,
        context: bytes,
    ) -> str:
        parsed = EncryptedValueEnvelope.model_validate(envelope)
        if parsed.key_id != self.key_id:
            raise SensitiveDataError("Sensitive-data key does not match record")
        try:
            plaintext = AESGCM(self._key).decrypt(
                base64.urlsafe_b64decode(parsed.nonce),
                base64.urlsafe_b64decode(parsed.ciphertext),
                context,
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise SensitiveDataError(
                "Sensitive value could not be authenticated"
            ) from error


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class MacOSKeychainStore:
    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
        runner: RunCommand = subprocess.run,
    ) -> None:
        self.service = service
        self.account = account
        self._runner = runner

    def load_key(self) -> bytes:
        self._require_macos()
        result = self._runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-s",
                self.service,
                "-a",
                self.account,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SensitiveDataKeyUnavailable(
                "Jobbot sensitive-data key is not available in macOS Keychain"
            )
        return self._decode_key(result.stdout.strip())

    def initialize(self) -> SensitiveValueCipher:
        try:
            return SensitiveValueCipher(self.load_key())
        except SensitiveDataKeyUnavailable:
            key = secrets.token_bytes(32)
            self._store_key(key)
            return SensitiveValueCipher(key)

    def export_recovery(self, path: Path) -> str:
        key = self.load_key()
        cipher = SensitiveValueCipher(key)
        payload = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "service": self.service,
            "account": self.account,
            "key_id": cipher.key_id,
            "key": base64.urlsafe_b64encode(key).decode("ascii"),
        }
        _write_owner_only_file(
            path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        return cipher.key_id

    def restore_recovery(self, path: Path) -> str:
        key = self.load_recovery_key(path)
        cipher = SensitiveValueCipher(key)
        self._store_key(key)
        return cipher.key_id

    def load_recovery_key(self, path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise SensitiveDataError("Recovery package must be a regular file")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SensitiveDataError("Recovery package must be owner-only")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SensitiveDataError("Recovery package is invalid") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != RECOVERY_SCHEMA_VERSION
            or payload.get("service") != self.service
            or payload.get("account") != self.account
        ):
            raise SensitiveDataError("Recovery package does not match Jobbot")
        key = self._decode_key(payload.get("key"))
        cipher = SensitiveValueCipher(key)
        if not secrets.compare_digest(
            cipher.key_id,
            str(payload.get("key_id") or ""),
        ):
            raise SensitiveDataError("Recovery package checksum is invalid")
        return key

    def _store_key(self, key: bytes) -> None:
        self._require_macos()
        encoded = base64.urlsafe_b64encode(key).decode("ascii")
        result = self._runner(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                self.account,
                "-w",
            ],
            input=f"{encoded}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SensitiveDataError(
                "Unable to store Jobbot sensitive-data key in macOS Keychain"
            )

    @staticmethod
    def _decode_key(encoded: Any) -> bytes:
        if not isinstance(encoded, str):
            raise SensitiveDataError("Sensitive-data key is invalid")
        try:
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise SensitiveDataError("Sensitive-data key is invalid") from error
        if len(key) != 32:
            raise SensitiveDataError("Sensitive-data key is invalid")
        return key

    @staticmethod
    def _require_macos() -> None:
        if platform.system() != "Darwin":
            raise SensitiveDataError(
                "macOS Keychain is required for Jobbot sensitive data"
            )


def redact_sensitive_mapping(value: Any) -> Any:
    """Recursively redact common secret-bearing keys before logging."""
    exact_sensitive_keys = {
        "answer",
        "sensitive_value",
        "value",
    }
    sensitive_key_fragments = {
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "signature",
        "token",
    }
    if isinstance(value, dict):
        return {
            key: (
                REDACTED_VALUE
                if str(key).lower() in exact_sensitive_keys
                or any(
                    fragment in str(key).lower()
                    for fragment in sensitive_key_fragments
                )
                else redact_sensitive_mapping(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_mapping(item) for item in value)
    return value


def _write_owner_only_file(path: Path, content: bytes) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise SensitiveDataError("Recovery destination must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise SensitiveDataError("Recovery directory must be a regular directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as recovery_file:
            descriptor = -1
            recovery_file.write(content)
            recovery_file.flush()
            os.fsync(recovery_file.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
