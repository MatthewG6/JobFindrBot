#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application_profile import (
    APPLICATION_PROFILE_SCHEMA_VERSION,
    DOCUMENT_MEDIA_TYPES,
    MAX_APPLICATION_DOCUMENT_BYTES,
    ApplicationProfile,
    ApprovedDocument,
    ApprovedDocumentMetadata,
    approve_document,
    application_profile_lock,
    configured_application_profile_path,
    ensure_owner_only_directory,
    fsync_directory_best_effort,
    load_application_profile,
    normalize_slug,
    parse_application_profile,
    reject_symlinked_components,
    validate_application_profile,
    verify_document_approval,
    write_application_profile,
)
from app.models import utc_now
from app.sensitive_data import (
    MacOSKeychainStore,
    SensitiveCategory,
    SensitiveDataError,
    SensitiveReusePolicy,
    SensitiveValueCipher,
)
from app.storage import DEFAULT_DB_PATH, JobStorage


DOCUMENT_ADD_TRANSACTION_SCHEMA_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Jobbot's private application profile."
    )
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("--profile-name", default="Primary")
    initialize.add_argument("--approval-name", default="Owner")

    set_field = subparsers.add_parser("set-field")
    set_field.add_argument("field_name")
    set_field.add_argument(
        "--category",
        choices=[category.value for category in SensitiveCategory],
        required=True,
    )
    set_field.add_argument(
        "--reuse-policy",
        choices=[policy.value for policy in SensitiveReusePolicy],
        default=SensitiveReusePolicy.CONFIRM_FIRST.value,
    )
    set_field.add_argument("--approved-by")
    set_field.add_argument("--confirm-retention", action="store_true")

    add_document = subparsers.add_parser("add-document")
    add_document.add_argument("source", type=Path)
    add_document.add_argument("--document-id", required=True)
    add_document.add_argument(
        "--kind",
        choices=[
            "resume",
            "cover_letter",
            "transcript",
            "portfolio",
            "other",
        ],
        required=True,
    )
    add_document.add_argument("--label", required=True)
    add_document.add_argument("--revision", required=True)
    add_document.add_argument(
        "--approved-for",
        action="append",
        required=True,
    )
    add_document.add_argument("--approved-by")
    add_document.add_argument("--default", action="store_true")

    retire_document = subparsers.add_parser("retire-document")
    retire_document.add_argument("document_id")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--require-ready", action="store_true")

    subparsers.add_parser("list")
    return parser


def _profile_path(explicit: Path | None) -> Path:
    return explicit or configured_application_profile_path()


def _load_cipher() -> SensitiveValueCipher:
    return SensitiveValueCipher(MacOSKeychainStore().load_key())


def _updated_profile(profile: ApplicationProfile, **updates) -> ApplicationProfile:
    return ApplicationProfile.model_validate(
        {
            **profile.model_dump(mode="json"),
            **updates,
            "updated_at": utc_now(),
        }
    )


def _stage_owner_only(source: Path, staging_path: Path) -> str:
    reject_symlinked_components(source)
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        raise ValueError("Document source does not exist") from None
    except OSError as error:
        raise ValueError("Document source could not be opened safely") from error
    descriptor = -1
    digest = hashlib.sha256()
    try:
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("Document source must be a regular file")
        if source_stat.st_size > MAX_APPLICATION_DOCUMENT_BYTES:
            raise ValueError("Document source exceeds the size limit")
        reject_symlinked_components(staging_path)
        ensure_owner_only_directory(staging_path.parent, create=True)
        descriptor = os.open(
            staging_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(source_descriptor, "rb") as source_file, os.fdopen(
            descriptor, "wb"
        ) as destination_file:
            source_descriptor = -1
            descriptor = -1
            total_bytes = 0
            while True:
                remaining = MAX_APPLICATION_DOCUMENT_BYTES - total_bytes
                block = source_file.read(min(1024 * 1024, remaining + 1))
                if not block:
                    break
                total_bytes += len(block)
                if total_bytes > MAX_APPLICATION_DOCUMENT_BYTES:
                    raise ValueError("Document source exceeds the size limit")
                digest.update(block)
                destination_file.write(block)
            destination_file.flush()
            os.fsync(destination_file.fileno())
        fsync_directory_best_effort(staging_path.parent)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _publish_owner_only_staging(
    staging_path: Path,
    destination: Path,
) -> None:
    reject_symlinked_components(staging_path)
    reject_symlinked_components(destination.parent)
    try:
        os.link(staging_path, destination, follow_symlinks=False)
    except FileExistsError:
        raise ValueError("Application document already exists") from None
    except OSError as error:
        raise ValueError("Application document could not be published") from error
    fsync_directory_best_effort(destination.parent)


def _unlink_owner_only_staging(staging_path: Path) -> bool:
    try:
        staging_path.unlink(missing_ok=True)
    except OSError:
        return False
    if staging_path.parent.exists():
        fsync_directory_best_effort(staging_path.parent)
    return not staging_path.exists() and not staging_path.is_symlink()


def _copy_owner_only(source: Path, destination: Path) -> str:
    ensure_owner_only_directory(destination.parent, create=True)
    staging_path = destination.parent / (
        f".{destination.name}.{uuid4().hex}.staging"
    )
    try:
        digest = _stage_owner_only(source, staging_path)
        _publish_owner_only_staging(staging_path, destination)
        return digest
    finally:
        _unlink_owner_only_staging(staging_path)


def _document_source_sha256(source: Path) -> str:
    reject_symlinked_components(source)
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        raise ValueError("Document source does not exist") from None
    except OSError as error:
        raise ValueError("Document source could not be opened safely") from error
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("Document source must be a regular file")
        if source_stat.st_size > MAX_APPLICATION_DOCUMENT_BYTES:
            raise ValueError("Document source exceeds the size limit")
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            while True:
                remaining = MAX_APPLICATION_DOCUMENT_BYTES - total_bytes
                block = source_file.read(min(1024 * 1024, remaining + 1))
                if not block:
                    break
                total_bytes += len(block)
                if total_bytes > MAX_APPLICATION_DOCUMENT_BYTES:
                    raise ValueError("Document source exceeds the size limit")
                digest.update(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _document_add_request_fingerprint(
    metadata: ApprovedDocumentMetadata,
) -> str:
    payload = metadata.model_dump(
        mode="json",
        exclude={"approved_at"},
    )
    payload["approved_for"] = sorted(payload["approved_for"])
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _document_add_transaction_path(profile_path: Path) -> Path:
    return profile_path.parent / f".{profile_path.name}.document-add.json"


def _write_document_add_transaction(
    profile_path: Path,
    *,
    document_id: str,
    document_path: str,
    staging_path: str,
    request_fingerprint: str | None = None,
) -> None:
    transaction_path = _document_add_transaction_path(profile_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{transaction_path.name}.",
        suffix=".tmp",
        dir=profile_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            {
                "schema_version": DOCUMENT_ADD_TRANSACTION_SCHEMA_VERSION,
                "document_id": document_id,
                "document_path": document_path,
                "staging_path": staging_path,
                "request_fingerprint": request_fingerprint,
            },
            sort_keys=True,
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as transaction_file:
            descriptor = -1
            transaction_file.write(payload)
            transaction_file.flush()
            os.fsync(transaction_file.fileno())
        os.replace(temporary_path, transaction_path)
        fsync_directory_best_effort(profile_path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_document_add_transaction(profile_path: Path) -> dict | None:
    transaction_path = _document_add_transaction_path(profile_path)
    reject_symlinked_components(transaction_path)
    try:
        descriptor = os.open(
            transaction_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(
            "Pending document transaction could not be opened safely"
        ) from error
    try:
        transaction_stat = os.fstat(descriptor)
        if not stat.S_ISREG(transaction_stat.st_mode):
            raise ValueError("Pending document transaction must be regular")
        if transaction_stat.st_mode & 0o077:
            raise ValueError("Pending document transaction must be owner-only")
        if transaction_stat.st_size > 4096:
            raise ValueError("Pending document transaction is invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8") as transaction_file:
            descriptor = -1
            try:
                transaction = json.load(transaction_file)
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError(
                    "Pending document transaction is invalid"
                ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(transaction, dict) or set(transaction) != {
        "schema_version",
        "document_id",
        "document_path",
        "staging_path",
        "request_fingerprint",
    }:
        raise ValueError("Pending document transaction is invalid")
    if (
        type(transaction["schema_version"]) is not int
        or transaction["schema_version"]
        != DOCUMENT_ADD_TRANSACTION_SCHEMA_VERSION
    ):
        raise ValueError("Pending document transaction version is unsupported")
    if (
        not isinstance(transaction["document_id"], str)
        or not isinstance(transaction["document_path"], str)
        or not isinstance(transaction["staging_path"], str)
    ):
        raise ValueError("Pending document transaction is invalid")
    request_fingerprint = transaction["request_fingerprint"]
    if request_fingerprint is not None and (
        not isinstance(request_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", request_fingerprint) is None
    ):
        raise ValueError("Pending document transaction is invalid")
    document_id = normalize_slug(transaction["document_id"], "Document ID")
    document_path = Path(transaction["document_path"])
    staging_path = Path(transaction["staging_path"])
    if (
        document_path.is_absolute()
        or ".." in document_path.parts
        or len(document_path.parts) != 2
        or document_path.parts[0] != "documents"
        or document_path.stem != document_id
        or document_path.suffix.lower() not in DOCUMENT_MEDIA_TYPES
    ):
        raise ValueError("Pending document transaction path is invalid")
    expected_staging_name = re.fullmatch(
        rf"\.{re.escape(document_path.name)}\.[0-9a-f]{{32}}\.staging",
        staging_path.name,
    )
    if (
        staging_path.is_absolute()
        or ".." in staging_path.parts
        or len(staging_path.parts) != 2
        or staging_path.parts[0] != "documents"
        or expected_staging_name is None
    ):
        raise ValueError("Pending document staging path is invalid")
    return {
        "document_id": document_id,
        "document_path": document_path.as_posix(),
        "staging_path": staging_path.as_posix(),
        "request_fingerprint": request_fingerprint,
    }


def _clear_document_add_transaction(profile_path: Path) -> None:
    transaction_path = _document_add_transaction_path(profile_path)
    try:
        transaction_path.unlink(missing_ok=True)
    except OSError:
        return
    fsync_directory_best_effort(profile_path.parent)


def _recover_document_add_transaction(
    profile_path: Path,
) -> tuple[ApprovedDocument, str] | None:
    transaction = _read_document_add_transaction(profile_path)
    if transaction is None:
        return None
    profile = parse_application_profile(profile_path)
    destination = profile_path.parent / transaction["document_path"]
    staging_path = profile_path.parent / transaction["staging_path"]
    matching_ids = [
        document
        for document in profile.documents
        if document.document_id == transaction["document_id"]
    ]
    matching_paths = [
        document
        for document in profile.documents
        if document.path == transaction["document_path"]
    ]
    if matching_ids or matching_paths:
        if (
            len(matching_ids) != 1
            or len(matching_paths) != 1
            or matching_ids[0] is not matching_paths[0]
        ):
            raise ValueError("Pending document transaction conflicts with profile")
        request_fingerprint = transaction["request_fingerprint"]
        document_metadata = ApprovedDocumentMetadata.model_validate(
            matching_ids[0].model_dump(
                exclude={"approval_key_id", "approval_signature"}
            )
        )
        if (
            request_fingerprint is None
            or _document_add_request_fingerprint(document_metadata)
            != request_fingerprint
        ):
            raise ValueError(
                "Pending document transaction does not match the committed document"
            )
        if staging_path.exists() or staging_path.is_symlink():
            if staging_path.is_symlink() or not stat.S_ISREG(
                staging_path.lstat().st_mode
            ):
                raise ValueError("Pending document staging file must be regular")
            if not destination.exists() or destination.is_symlink():
                raise ValueError(
                    "Pending document transaction conflicts with committed file"
                )
            destination_stat = destination.lstat()
            staging_stat = staging_path.lstat()
            if (
                not stat.S_ISREG(destination_stat.st_mode)
                or destination_stat.st_dev != staging_stat.st_dev
                or destination_stat.st_ino != staging_stat.st_ino
            ):
                raise ValueError(
                    "Pending document transaction conflicts with committed file"
                )
            if not _unlink_owner_only_staging(staging_path):
                raise ValueError("Pending document staging cleanup failed")
        return matching_ids[0], request_fingerprint

    reject_symlinked_components(destination.parent)
    reject_symlinked_components(staging_path.parent)
    if staging_path.exists() or staging_path.is_symlink():
        if staging_path.is_symlink() or not stat.S_ISREG(
            staging_path.lstat().st_mode
        ):
            raise ValueError("Pending document staging file must be regular")
        staging_stat = staging_path.lstat()
        if destination.exists() and not destination.is_symlink():
            destination_stat = destination.lstat()
            if (
                stat.S_ISREG(destination_stat.st_mode)
                and destination_stat.st_dev == staging_stat.st_dev
                and destination_stat.st_ino == staging_stat.st_ino
            ):
                destination.unlink()
        if not _unlink_owner_only_staging(staging_path):
            raise ValueError("Pending document staging cleanup failed")
    if destination.parent.exists():
        fsync_directory_best_effort(destination.parent)
    _clear_document_add_transaction(profile_path)
    return None


def initialize_profile(path: Path, args: argparse.Namespace) -> dict:
    with application_profile_lock(path):
        if path.exists() or path.is_symlink():
            raise ValueError("Application profile already exists")
        now = utc_now()
        profile_id = getattr(args, "profile_id", None) or f"profile_{uuid4().hex}"
        profile = ApplicationProfile(
            schema_version=APPLICATION_PROFILE_SCHEMA_VERSION,
            profile_id=profile_id,
            profile_name=args.profile_name,
            approval_name=args.approval_name,
            created_at=now,
            updated_at=now,
        )
        write_application_profile(path, profile)
        return {"profile": str(path), "profile_id": profile.profile_id}


def set_profile_field(path: Path, database: Path, args: argparse.Namespace) -> dict:
    sensitive_cipher = _load_cipher()
    storage = JobStorage(database, sensitive_cipher=sensitive_cipher)
    profile = load_application_profile(
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
    )
    approved_by = args.approved_by or profile.approval_name
    field_name = normalize_slug(args.field_name, "Application field name")
    value = getpass.getpass(f"Value for {field_name}: ")
    if not value:
        raise ValueError("Application field value cannot be blank")
    _, replaced = storage.upsert_profile_sensitive_value(
        profile_id=profile.profile_id,
        field_name=field_name,
        value=value,
        category=args.category,
        reuse_policy=args.reuse_policy,
        approved_by=approved_by,
        retention_confirmed=args.confirm_retention,
    )
    return {
        "field_name": field_name,
        "replaced": replaced,
        "backup_purge_recommended": replaced,
    }


def add_profile_document(
    path: Path,
    args: argparse.Namespace,
    database: Path = DEFAULT_DB_PATH,
) -> dict:
    with application_profile_lock(path):
        return _add_profile_document_locked(path, args, database)


def _add_profile_document_locked(
    path: Path,
    args: argparse.Namespace,
    database: Path,
) -> dict:
    recovered = _recover_document_add_transaction(path)
    sensitive_cipher = _load_cipher()
    storage = JobStorage(database, sensitive_cipher=sensitive_cipher)
    profile = load_application_profile(
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
    )
    suffix = args.source.suffix.lower()
    media_type = DOCUMENT_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise ValueError("Document type is not approved")
    document_id = normalize_slug(args.document_id, "Document ID")
    destination = path.parent / "documents" / f"{document_id}{suffix}"
    recovered_document = recovered[0] if recovered is not None else None
    metadata = ApprovedDocumentMetadata(
        document_id=document_id,
        kind=args.kind,
        label=args.label,
        path=destination.relative_to(path.parent).as_posix(),
        media_type=media_type,
        revision=args.revision,
        sha256=(
            _document_source_sha256(args.source)
            if recovered_document is not None
            and recovered_document.document_id == document_id
            else "0" * 64
        ),
        approved_for=args.approved_for,
        approved_by=args.approved_by or profile.approval_name,
        approved_at=(
            recovered_document.approved_at
            if recovered_document is not None
            and recovered_document.document_id == document_id
            else datetime.now(UTC)
        ),
        active=True,
        default=args.default,
    )
    if (
        recovered_document is not None
        and recovered_document.document_id == document_id
    ):
        if _document_add_request_fingerprint(metadata) != recovered[1]:
            raise ValueError(
                "Recovered document request does not match the committed document"
            )
        _clear_document_add_transaction(path)
        return {
            "document_id": recovered_document.document_id,
            "path": recovered_document.path,
            "sha256": recovered_document.sha256,
        }
    if any(
        document.document_id == document_id or document.path == metadata.path
        for document in profile.documents
    ):
        raise ValueError("Application document already exists")
    if recovered_document is not None:
        _clear_document_add_transaction(path)
    ensure_owner_only_directory(destination.parent, create=True)
    reject_symlinked_components(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Application document already exists")
    staging_path = destination.parent / (
        f".{destination.name}.{uuid4().hex}.staging"
    )
    _write_document_add_transaction(
        path,
        document_id=document_id,
        document_path=metadata.path,
        staging_path=staging_path.relative_to(path.parent).as_posix(),
    )
    try:
        copied_sha256 = _stage_owner_only(args.source, staging_path)
        metadata = ApprovedDocumentMetadata.model_validate(
            {
                **metadata.model_dump(mode="json"),
                "sha256": copied_sha256,
            }
        )
        _write_document_add_transaction(
            path,
            document_id=document_id,
            document_path=metadata.path,
            staging_path=staging_path.relative_to(path.parent).as_posix(),
            request_fingerprint=_document_add_request_fingerprint(metadata),
        )
        _publish_owner_only_staging(staging_path, destination)
        document = approve_document(
            profile.profile_id,
            metadata,
            sensitive_cipher,
        )
        updated = _updated_profile(
            profile,
            documents=[*profile.documents, document],
        )
        write_application_profile(path, updated)
    except BaseException:
        _recover_document_add_transaction(path)
        raise
    if _unlink_owner_only_staging(staging_path):
        _clear_document_add_transaction(path)
    return {
        "document_id": document.document_id,
        "path": document.path,
        "sha256": document.sha256,
    }


def retire_profile_document(
    path: Path,
    document_id: str,
    database: Path = DEFAULT_DB_PATH,
) -> dict:
    with application_profile_lock(path):
        return _retire_profile_document_locked(path, document_id, database)


def _retire_profile_document_locked(
    path: Path,
    document_id: str,
    database: Path,
) -> dict:
    sensitive_cipher = _load_cipher()
    storage = JobStorage(database, sensitive_cipher=sensitive_cipher)
    profile = load_application_profile(
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
    )
    document_id = normalize_slug(document_id, "Document ID")
    found = False
    documents = []
    for document in profile.documents:
        if document.document_id != document_id:
            documents.append(document)
            continue
        verify_document_approval(
            profile.profile_id,
            document,
            sensitive_cipher,
        )
        found = True
        documents.append(
            approve_document(
                profile.profile_id,
                ApprovedDocumentMetadata.model_validate(
                    {
                        **document.model_dump(
                            mode="json",
                            exclude={
                                "approval_key_id",
                                "approval_signature",
                            },
                        ),
                        "active": False,
                        "default": False,
                    }
                ),
                sensitive_cipher,
            )
        )
    if not found:
        raise ValueError("Application document does not exist")
    write_application_profile(
        path,
        _updated_profile(profile, documents=documents),
    )
    return {"document_id": document_id, "retired": True}


def validate_profile(path: Path, database: Path, require_ready: bool) -> dict:
    profile = parse_application_profile(path)
    sensitive_cipher = _load_cipher()
    storage = JobStorage(database, sensitive_cipher=sensitive_cipher)
    return validate_application_profile(
        profile,
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
        require_ready=require_ready,
    )


def list_profile(path: Path, database: Path) -> dict:
    sensitive_cipher = _load_cipher()
    storage = JobStorage(database, sensitive_cipher=sensitive_cipher)
    profile = load_application_profile(
        path,
        storage=storage,
        sensitive_cipher=sensitive_cipher,
    )
    field_names = sorted(
        record["field_name"]
        for record in storage.list_sensitive_values(
            scope="application_profile",
            scope_id=profile.profile_id,
        )
    )
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "profile_name": profile.profile_name,
        "fields": field_names,
        "documents": [
            {
                "document_id": document.document_id,
                "kind": document.kind,
                "label": document.label,
                "revision": document.revision,
                "approved_for": document.approved_for,
                "active": document.active,
                "default": document.default,
            }
            for document in profile.documents
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = _profile_path(args.profile)
        if args.command == "initialize":
            result = initialize_profile(path, args)
        elif args.command == "set-field":
            result = set_profile_field(path, args.database, args)
        elif args.command == "add-document":
            result = add_profile_document(path, args, args.database)
        elif args.command == "retire-document":
            result = retire_profile_document(
                path,
                args.document_id,
                args.database,
            )
        elif args.command == "validate":
            result = validate_profile(
                path,
                args.database,
                args.require_ready,
            )
        elif args.command == "list":
            result = list_profile(path, args.database)
        else:
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, SensitiveDataError, ValueError, RuntimeError) as error:
        print(f"Application profile operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
