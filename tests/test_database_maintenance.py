from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import stat
import sys

import pytest
from tinydb import TinyDB

from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


def test_legacy_database_is_backed_up_before_schema_marker(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("jobs").insert(
        {"title": "Legacy job", "url": "https://example.com/legacy"}
    )
    database.close()
    legacy_content = json.loads(database_path.read_text(encoding="utf-8"))

    storage = JobStorage(database_path)

    backups = list((tmp_path / "backups").glob("jobs-migration-v1-*.json"))
    assert storage.schema_version() == CURRENT_SCHEMA_VERSION
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == legacy_content
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_daily_backup_is_deduplicated_and_retained(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job(
        {
            "title": "Example",
            "url": "https://example.com/job",
        }
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)

    first = storage.create_backup(now=start, retention=2)
    duplicate = storage.create_backup(now=start, retention=2)
    storage.create_backup(now=start + timedelta(days=1), retention=2)
    latest = storage.create_backup(now=start + timedelta(days=2), retention=2)

    backups = list((tmp_path / "backups").glob("jobs-daily-*.json"))
    assert first["created"] is True
    assert duplicate["created"] is False
    assert latest["removed"] == 1
    assert len(backups) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)


def test_failed_migration_rolls_back_partial_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("jobs").insert({"title": "Original"})
    database.close()
    original = json.loads(database_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside"
    outside.write_text("unchanged", encoding="utf-8")
    database_path.with_suffix(".json.rollback").symlink_to(outside)

    class FailingMigrationStorage(JobStorage):
        def _apply_migration(self, version: int) -> None:
            self.jobs_table.insert({"title": "Partial"})
            raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        FailingMigrationStorage(database_path)

    assert json.loads(database_path.read_text(encoding="utf-8")) == original
    assert outside.read_text(encoding="utf-8") == "unchanged"
    storage = JobStorage(database_path)
    assert [job["title"] for job in storage.list_jobs()] == ["Original"]


def test_migration_directory_open_failure_does_not_report_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("jobs").insert({"title": "Original"})
    database.close()
    real_open = os.open
    failed = False

    def fail_database_directory_open(path, flags, *args, **kwargs):
        nonlocal failed
        if not failed and Path(path) == tmp_path:
            failed = True
            raise OSError("directory open failed")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_database_directory_open)

    storage = JobStorage(database_path)

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION
    assert [job["title"] for job in storage.list_jobs()] == ["Original"]
    assert failed is True


def test_schema_validation_rejects_newer_and_duplicate_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    storage = JobStorage(database_path)
    storage.db.close()
    database = TinyDB(database_path)
    metadata = database.table("schema_metadata")
    record = metadata.all()[0]
    metadata.update({"version": 999}, doc_ids=[record.doc_id])
    database.close()

    with pytest.raises(ValueError, match="unsupported"):
        storage.list_jobs()

    duplicate_path = tmp_path / "duplicate.json"
    duplicate = TinyDB(duplicate_path)
    duplicate.table("schema_metadata").insert_multiple(
        [
            {"key": "schema_version", "version": 1},
            {"key": "schema_version", "version": 999},
        ]
    )
    duplicate.close()
    with pytest.raises(ValueError, match="duplicate"):
        JobStorage(duplicate_path)


def test_database_and_lock_permissions_survive_rollback(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    storage = JobStorage(database_path)
    storage.save_job({"title": "Original", "url": "https://example.com/1"})

    with pytest.raises(RuntimeError):
        with storage.transaction():
            storage.jobs_table.insert({"title": "Partial"})
            raise RuntimeError("rollback")

    assert database_path.stat().st_mode & 0o777 == 0o600
    assert database_path.parent.stat().st_mode & 0o777 == 0o700
    assert database_path.with_suffix(".json.lock").stat().st_mode & 0o777 == 0o600
    assert [job["title"] for job in storage.list_jobs()] == ["Original"]


def test_custom_storage_does_not_change_shared_parent_mode(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    storage = JobStorage(shared / "jobs.json")

    assert shared.stat().st_mode & 0o777 == 0o755
    assert storage._db_path.stat().st_mode & 0o777 == 0o600


def test_backup_directory_symlink_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "database" / "jobs.json"
    storage = JobStorage(database_path)
    storage.save_job({"title": "Example", "url": "https://example.com/1"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (database_path.parent / "backups").symlink_to(outside)

    with pytest.raises(ValueError, match="Backup directory"):
        storage.create_backup()

    assert list(outside.iterdir()) == []


def test_backup_retention_ignores_malformed_names(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job({"title": "Example", "url": "https://example.com/1"})
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(exist_ok=True)
    decoy = backup_directory / "jobs-daily-9999-99-99.json"
    decoy.write_text("decoy", encoding="utf-8")
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for offset in range(3):
        storage.create_backup(now=start + timedelta(days=offset), retention=2)

    valid_backups = list(backup_directory.glob("jobs-daily-2026-*.json"))
    assert len(valid_backups) == 2
    assert decoy.read_text(encoding="utf-8") == "decoy"


def test_forged_daily_backup_is_replaced_with_verified_snapshot(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job({"title": "Example", "url": "https://example.com/1"})
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(exist_ok=True)
    forged = backup_directory / "jobs-daily-2026-08-04.json"
    forged.write_text("FORGED", encoding="utf-8")

    result = storage.create_backup(
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert result["created"] is True
    assert json.loads(forged.read_text(encoding="utf-8"))["jobs"]
    checksum = forged.with_suffix(".json.sha256")
    assert checksum.is_file()
    assert checksum.stat().st_mode & 0o777 == 0o600


def test_backup_retention_counts_only_checksum_valid_pairs(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job({"title": "Example", "url": "https://example.com/1"})
    start = datetime(2026, 8, 1, tzinfo=UTC)
    storage.create_backup(now=start, retention=2)
    storage.create_backup(now=start + timedelta(days=1), retention=2)
    corrupt = tmp_path / "backups" / "jobs-daily-2026-08-02.json"
    corrupt.write_text("corrupt", encoding="utf-8")

    result = storage.create_backup(
        now=start + timedelta(days=2),
        retention=2,
    )

    assert result["removed"] == 1
    assert (tmp_path / "backups" / "jobs-daily-2026-08-01.json").is_file()
    assert not corrupt.exists()
    assert (tmp_path / "backups" / "jobs-daily-2026-08-03.json").is_file()


def test_transaction_is_atomic_when_process_exits_without_cleanup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    storage = JobStorage(database_path)
    storage.save_job({"title": "Original", "url": "https://example.com/1"})
    code = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "from app.storage import JobStorage",
            f"storage = JobStorage(Path({str(database_path)!r}))",
            "with storage.transaction():",
            "    storage.jobs_table.insert({'title': 'Partial'})",
            "    os._exit(17)",
        ]
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
    )

    assert completed.returncode == 17
    reopened = JobStorage(database_path)
    assert [job["title"] for job in reopened.list_jobs()] == ["Original"]
    assert not list(tmp_path.glob(".jobs.json.transaction.*.json"))


def test_nested_transactions_are_rejected_without_losing_outer_writes(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job({"title": "Original", "url": "https://example.com/1"})

    with storage.transaction():
        storage.jobs_table.insert({"title": "Outer"})
        with pytest.raises(RuntimeError, match="Nested"):
            with storage.transaction():
                storage.jobs_table.insert({"title": "Inner"})

    assert [job["title"] for job in storage.list_jobs()] == [
        "Original",
        "Outer",
    ]


def test_second_storage_instance_cannot_write_inside_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    first = JobStorage(database_path)
    second = JobStorage(database_path)

    with first.transaction():
        first.jobs_table.insert({"title": "Outer"})
        with pytest.raises(RuntimeError, match="owns"):
            second.save_job({"title": "Lost", "url": "https://example.com/2"})

    second.save_job({"title": "After", "url": "https://example.com/3"})
    assert [job["title"] for job in first.list_jobs()] == ["Outer", "After"]


def test_second_storage_raw_table_cannot_write_inside_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    first = JobStorage(database_path)
    second = JobStorage(database_path)

    with first.transaction():
        first.jobs_table.insert({"title": "Committed"})
        with pytest.raises(ValueError):
            second.jobs_table.insert({"title": "Lost"})

    assert [job["title"] for job in first.list_jobs()] == ["Committed"]


def test_peer_close_failure_releases_transaction_ownership(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "jobs.json"
    first = JobStorage(database_path)
    second = JobStorage(database_path)
    real_close = second.db.close

    def fail_close() -> None:
        raise OSError("close failed")

    monkeypatch.setattr(second.db, "close", fail_close)

    with pytest.raises(OSError, match="close failed"):
        with first.transaction():
            pass

    monkeypatch.setattr(second.db, "close", real_close)
    assert first.list_jobs() == []
    assert second.list_jobs() == []


def test_second_storage_instance_cannot_open_inside_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    first = JobStorage(database_path)

    with first.transaction():
        first.jobs_table.insert({"title": "Committed"})
        with pytest.raises(RuntimeError, match="active transaction"):
            JobStorage(database_path)

    assert [job["title"] for job in first.list_jobs()] == ["Committed"]


def test_post_commit_directory_fsync_failure_does_not_report_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job({"title": "Original", "url": "https://example.com/1"})
    real_fsync = os.fsync
    failed = False

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with storage.transaction():
        storage.jobs_table.insert({"title": "Committed"})

    assert [job["title"] for job in storage.list_jobs()] == [
        "Original",
        "Committed",
    ]
    assert failed is True


def test_post_commit_directory_open_failure_does_not_report_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    real_open = os.open
    failed = False

    def fail_directory_open(path, flags, *args, **kwargs):
        nonlocal failed
        if not failed and Path(path) == tmp_path:
            failed = True
            raise OSError("directory open failed")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_directory_open)

    with storage.transaction():
        storage.jobs_table.insert({"title": "Committed"})

    assert [job["title"] for job in storage.list_jobs()] == ["Committed"]
    assert failed is True


def test_post_commit_directory_close_failure_does_not_report_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    real_close = os.close
    failed = False
    leaked_descriptor = None

    def fail_directory_close(descriptor: int) -> None:
        nonlocal failed, leaked_descriptor
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            leaked_descriptor = descriptor
            raise OSError("directory close failed")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", fail_directory_close)

    with storage.transaction():
        storage.jobs_table.insert({"title": "Committed"})

    monkeypatch.setattr(os, "close", real_close)
    if leaked_descriptor is not None:
        real_close(leaked_descriptor)
    assert [job["title"] for job in storage.list_jobs()] == ["Committed"]
    assert failed is True
