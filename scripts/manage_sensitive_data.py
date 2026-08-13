#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.sensitive_data import (
    MacOSKeychainStore,
    SensitiveDataError,
    SensitiveValueCipher,
    SensitiveValueRecord,
    encryption_context,
)
from app.storage import DEFAULT_DB_PATH, JobStorage
from pydantic import ValidationError


BACKUP_PURGE_CONFIRMATION = "DELETE-JOBBOT-BACKUPS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Jobbot's owner-only sensitive-data key lifecycle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("initialize-key")

    export = subparsers.add_parser("export-recovery")
    export.add_argument("path", type=Path)

    restore = subparsers.add_parser("restore-recovery")
    restore.add_argument("path", type=Path)

    verify = subparsers.add_parser("verify-recovery")
    verify.add_argument("recovery_path", type=Path)
    verify.add_argument("database_path", type=Path)

    purge = subparsers.add_parser("purge-backups")
    purge.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    purge.add_argument("--confirm", required=True)
    return parser


def _sensitive_records(database_path: Path) -> list[dict[str, Any]]:
    if database_path.is_symlink() or not database_path.is_file():
        raise SensitiveDataError("Database snapshot must be a regular file")
    try:
        payload = json.loads(database_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SensitiveDataError("Database snapshot is invalid") from error
    table = payload.get("sensitive_values", {})
    if not isinstance(table, dict):
        raise SensitiveDataError("Sensitive-value table is invalid")
    return [dict(record) for record in table.values()]


def verify_recovery(recovery_path: Path, database_path: Path) -> tuple[str, int]:
    key_store = MacOSKeychainStore()
    cipher = SensitiveValueCipher(key_store.load_recovery_key(recovery_path))
    records = _sensitive_records(database_path)
    for raw_record in records:
        try:
            record = SensitiveValueRecord.model_validate(raw_record)
        except ValidationError:
            raise SensitiveDataError(
                "Sensitive-value table is invalid"
            ) from None
        context = encryption_context(
            secret_id=record.secret_id,
            scope=record.scope,
            scope_id=record.scope_id,
            field_name=record.field_name,
        )
        cipher.decrypt(record.encrypted_value, context=context)
    return cipher.key_id, len(records)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    key_store = MacOSKeychainStore()
    try:
        if args.command == "initialize-key":
            cipher = key_store.initialize()
            print(f"Sensitive-data key ready: key_id={cipher.key_id}")
            return 0
        if args.command == "export-recovery":
            key_id = key_store.export_recovery(args.path)
            print(f"Owner-only recovery package created: key_id={key_id}")
            return 0
        if args.command == "restore-recovery":
            key_id = key_store.restore_recovery(args.path)
            print(f"Sensitive-data key restored: key_id={key_id}")
            return 0
        if args.command == "verify-recovery":
            key_id, record_count = verify_recovery(
                args.recovery_path,
                args.database_path,
            )
            print(
                "Recovery verification passed: "
                f"key_id={key_id} encrypted_records={record_count}"
            )
            return 0
        if args.command == "purge-backups":
            if args.confirm != BACKUP_PURGE_CONFIRMATION:
                raise SensitiveDataError(
                    f"Backup purge requires --confirm {BACKUP_PURGE_CONFIRMATION}"
                )
            removed = JobStorage(args.database).purge_database_backups(
                confirm=True
            )
            print(f"Database backups purged: {removed}")
            return 0
    except (SensitiveDataError, ValueError, RuntimeError) as error:
        print(f"Sensitive-data operation failed: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
