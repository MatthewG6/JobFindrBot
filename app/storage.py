from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from threading import Lock, RLock, local
from types import TracebackType
from typing import Iterator
import tempfile
from weakref import WeakSet

from tinydb import Query, TinyDB

from app.dedupe import job_content_hash
from app.models import (
    ApplicationEvent,
    ApplicationRecord,
    ApplicationStatus,
    InvalidApplicationTransition,
    JobPosting,
    utc_now,
    validate_application_transition,
)


DEFAULT_DB_PATH = Path("data/jobs.json")
CURRENT_SCHEMA_VERSION = 1
DEFAULT_BACKUP_RETENTION = 14
_DATABASE_LOCKS: dict[Path, "DatabaseLock"] = {}
_DATABASE_LOCKS_GUARD = Lock()


class DatabaseLock:
    """Serialize TinyDB access across threads and local processes."""

    def __init__(self, db_path: Path) -> None:
        self._thread_lock = RLock()
        self._local = local()
        self._instances: WeakSet[JobStorage] = WeakSet()
        self._lock_path = db_path.with_suffix(f"{db_path.suffix}.lock")

    def __enter__(self) -> "DatabaseLock":
        self._thread_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        lock_file = None

        try:
            if depth == 0:
                if self._lock_path.is_symlink():
                    raise ValueError("Database lock must be a regular file")
                lock_file = self._lock_path.open("a+")
                os.chmod(self._lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self._local.lock_file = lock_file
            self._local.depth = depth + 1
        except Exception:
            if lock_file is not None:
                lock_file.close()
            self._thread_lock.release()
            raise

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        depth = self._local.depth - 1
        self._local.depth = depth

        try:
            if depth == 0:
                lock_file = self._local.lock_file
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
                    del self._local.lock_file
        finally:
            self._thread_lock.release()

    @property
    def depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @property
    def transaction_owner(self) -> object | None:
        return getattr(self._local, "transaction_owner", None)

    @transaction_owner.setter
    def transaction_owner(self, owner: object | None) -> None:
        if owner is None:
            if hasattr(self._local, "transaction_owner"):
                del self._local.transaction_owner
            return
        self._local.transaction_owner = owner

    def register(self, storage: "JobStorage") -> None:
        self._instances.add(storage)

    def close_other_instances(self, owner: "JobStorage") -> None:
        for storage in tuple(self._instances):
            if storage is not owner:
                storage.db.close()


def database_lock(db_path: Path) -> DatabaseLock:
    resolved_path = db_path.resolve()
    with _DATABASE_LOCKS_GUARD:
        return _DATABASE_LOCKS.setdefault(
            resolved_path,
            DatabaseLock(resolved_path),
        )


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        if db_path.parent.is_symlink():
            raise ValueError("Database directory must be a regular directory")
        directory_existed = db_path.parent.exists()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        parent_stat = db_path.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError("Database directory must be a regular directory")
        if not directory_existed or db_path.resolve() == DEFAULT_DB_PATH.resolve():
            os.chmod(db_path.parent, 0o700)
        if db_path.is_symlink():
            raise ValueError("Database must be a regular file")
        self._db_path = db_path
        self._lock = database_lock(db_path)
        with self._lock:
            if self._lock.transaction_owner is not None:
                raise RuntimeError(
                    "Cannot open storage during an active transaction"
                )
            self._cleanup_stale_temporary_databases()
            self.db = TinyDB(self._db_path)
            os.chmod(self._db_path, 0o600)
            self._set_tables()
            self._ensure_schema()
            self._lock.register(self)

    def _cleanup_stale_temporary_databases(self) -> None:
        prefixes = (
            f".{self._db_path.name}.migration.",
            f".{self._db_path.name}.transaction.",
        )
        for path in self._db_path.parent.iterdir():
            if not path.name.startswith(prefixes) or path.is_symlink():
                continue
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(path_stat.st_mode):
                path.unlink(missing_ok=True)

    def _set_tables(self) -> None:
        self.jobs_table = self.db.table("jobs")
        self.applications_table = self.db.table("applications")
        self.processed_emails_table = self.db.table("processed_emails")
        self.job_notifications_table = self.db.table("job_notifications")
        self.notification_channels_table = self.db.table(
            "notification_channels"
        )
        self.schema_metadata_table = self.db.table("schema_metadata")

    def _ensure_schema(self) -> None:
        metadata = Query()
        records = self.schema_metadata_table.search(
            metadata.key == "schema_version"
        )
        if len(records) > 1:
            raise ValueError("Database contains duplicate schema versions")
        if not records:
            version = 0
        else:
            version = records[0].get("version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise ValueError("Database schema version is invalid")
        if version > CURRENT_SCHEMA_VERSION:
            raise ValueError("Database schema is newer than this Jobbot build")

        if version < CURRENT_SCHEMA_VERSION:
            self._write_backup(
                kind=f"migration-v{version + 1}",
                now=utc_now(),
            )
            self._migrate_schema_copy(version)
        self._validate_schema()

    def _migrate_schema_copy(self, version: int) -> None:
        source = self._db_path.read_bytes()
        descriptor, migration_name = tempfile.mkstemp(
            prefix=f".{self._db_path.name}.migration.",
            suffix=".json",
            dir=self._db_path.parent,
        )
        migration_path = Path(migration_name)
        live_database = self.db
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as migration_file:
                descriptor = -1
                migration_file.write(source)
                migration_file.flush()
                os.fsync(migration_file.fileno())

            migration_database = TinyDB(migration_path)
            self.db = migration_database
            self._set_tables()
            metadata = Query()
            while version < CURRENT_SCHEMA_VERSION:
                next_version = version + 1
                self._apply_migration(next_version)
                now = utc_now().isoformat()
                records = self.schema_metadata_table.search(
                    metadata.key == "schema_version"
                )
                if len(records) > 1:
                    raise ValueError("Database contains duplicate schema versions")
                if records:
                    self.schema_metadata_table.update(
                        {"version": next_version, "updated_at": now},
                        doc_ids=[records[0].doc_id],
                    )
                else:
                    self.schema_metadata_table.insert(
                        {
                            "key": "schema_version",
                            "version": next_version,
                            "updated_at": now,
                        }
                    )
                version = next_version
            self._validate_schema()
            migration_database.close()
            live_database.close()
            os.chmod(migration_path, 0o600)
            migration_path.replace(self._db_path)
            migration_path = None
            self._fsync_directory_best_effort()
        except BaseException:
            try:
                self.db.close()
            except Exception:
                pass
            try:
                live_database.close()
            except Exception:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if migration_path is not None:
                migration_path.unlink(missing_ok=True)
            self.db = TinyDB(self._db_path)
            self._set_tables()

    def _fsync_directory_best_effort(self) -> None:
        try:
            directory_descriptor = os.open(self._db_path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
        finally:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass

    def _validate_schema(self) -> None:
        metadata = Query()
        records = self.schema_metadata_table.search(
            metadata.key == "schema_version"
        )
        if len(records) != 1:
            raise ValueError("Database schema metadata is invalid")
        version = records[0].get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != CURRENT_SCHEMA_VERSION
        ):
            raise ValueError("Database schema version is unsupported")

    def _apply_migration(self, version: int) -> None:
        if version == 1:
            return
        raise ValueError(f"Unsupported database migration: {version}")

    def schema_version(self) -> int:
        with self._access():
            self._validate_schema()
            return CURRENT_SCHEMA_VERSION

    def create_backup(
        self,
        *,
        now: datetime | None = None,
        retention: int = DEFAULT_BACKUP_RETENTION,
    ) -> dict:
        if retention < 1:
            raise ValueError("Backup retention must be positive")
        with self._access():
            now = now or utc_now()
            backup_path = self._write_backup(kind="daily", now=now)
            removed = self._prune_backups("daily", retention)
            return {
                "created": backup_path is not None,
                "removed": removed,
                "schema_version": self.schema_version(),
            }

    def _backup_directory(self) -> Path:
        return self._db_path.parent / "backups"

    def _prepare_backup_directory(self) -> Path:
        backup_directory = self._backup_directory()
        if backup_directory.is_symlink():
            raise ValueError("Backup directory must be a regular directory")
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_stat = backup_directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("Backup directory must be a regular directory")
        os.chmod(backup_directory, 0o700)
        return backup_directory

    def _write_backup(self, *, kind: str, now: datetime) -> Path | None:
        if not self._db_path.exists():
            return None
        content = self._db_path.read_bytes()
        if not content:
            return None
        backup_directory = self._prepare_backup_directory()
        if kind == "daily":
            timestamp = now.astimezone(UTC).date().isoformat()
        else:
            timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_directory / (
            f"{self._db_path.stem}-{kind}-{timestamp}.json"
        )
        checksum_path = backup_path.with_suffix(f"{backup_path.suffix}.sha256")
        if backup_path.exists():
            if backup_path.is_symlink() or not stat.S_ISREG(
                backup_path.lstat().st_mode
            ):
                raise ValueError("Backup destination must be a regular file")
            if kind != "daily":
                raise ValueError("Migration backup destination already exists")
            if self._backup_checksum_valid(backup_path, checksum_path):
                os.chmod(backup_path, 0o600)
                os.chmod(checksum_path, 0o600)
                return None
            backup_path.unlink()
            if checksum_path.exists():
                if checksum_path.is_symlink() or not checksum_path.is_file():
                    raise ValueError("Backup checksum must be a regular file")
                checksum_path.unlink()

        self._write_private_file(backup_path, content)
        checksum = hashlib.sha256(content).hexdigest().encode("ascii") + b"\n"
        self._write_private_file(checksum_path, checksum)
        directory_descriptor = os.open(backup_directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return backup_path

    def _write_private_file(self, path: Path, content: bytes) -> None:
        if path.is_symlink():
            raise ValueError("Private file destination must be regular")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as backup_file:
                descriptor = -1
                backup_file.write(content)
                backup_file.flush()
                os.fsync(backup_file.fileno())
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def _backup_checksum_valid(
        self,
        backup_path: Path,
        checksum_path: Path,
    ) -> bool:
        if (
            not checksum_path.exists()
            or checksum_path.is_symlink()
            or not checksum_path.is_file()
        ):
            return False
        try:
            recorded = checksum_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        actual = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        return recorded == actual

    def _prune_backups(self, kind: str, retention: int) -> int:
        backup_directory = self._backup_directory()
        if not backup_directory.exists():
            return 0
        pattern = re.compile(
            rf"^{re.escape(self._db_path.stem)}-{re.escape(kind)}-"
            r"\d{4}-\d{2}-\d{2}\.json$"
        )
        dated_backups = []
        removed = 0
        for path in backup_directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match is None or not path.is_file() or path.is_symlink():
                continue
            date_text = path.name.removesuffix(".json").rsplit("-daily-", 1)[-1]
            try:
                backup_date = datetime.strptime(date_text, "%Y-%m-%d").date()
            except ValueError:
                continue
            checksum_path = path.with_suffix(f"{path.suffix}.sha256")
            if not self._backup_checksum_valid(path, checksum_path):
                path.unlink()
                if checksum_path.is_file() and not checksum_path.is_symlink():
                    checksum_path.unlink()
                removed += 1
                continue
            dated_backups.append((backup_date, path))
        backups = [
            path
            for _, path in sorted(dated_backups, reverse=True)
        ]
        for backup in backups[retention:]:
            if backup.is_file() and not backup.is_symlink():
                backup.unlink()
                checksum_path = backup.with_suffix(
                    f"{backup.suffix}.sha256"
                )
                if checksum_path.is_file() and not checksum_path.is_symlink():
                    checksum_path.unlink()
                removed += 1
        return removed

    def _reload(self) -> None:
        self.db.close()
        if self._db_path.is_symlink():
            raise ValueError("Database must be a regular file")
        self.db = TinyDB(self._db_path)
        os.chmod(self._db_path, 0o600)
        self._set_tables()
        self._validate_schema()

    @contextmanager
    def _access(self) -> Iterator[None]:
        with self._lock:
            owner = self._lock.transaction_owner
            if owner is not None and owner is not self:
                raise RuntimeError(
                    "Another storage instance owns the active transaction"
                )
            if self._lock.depth == 1:
                self._reload()
            yield

    @contextmanager
    def transaction(self) -> Iterator["JobStorage"]:
        """Commit multi-step writes with an atomic database-file replacement."""
        with self._access():
            if self._lock.depth != 1:
                raise RuntimeError("Nested database transactions are unsupported")
            self._lock.transaction_owner = self
            descriptor = -1
            transaction_path = None
            live_database = self.db
            try:
                self._lock.close_other_instances(self)
                snapshot = self._db_path.read_bytes()
                descriptor, transaction_name = tempfile.mkstemp(
                    prefix=f".{self._db_path.name}.transaction.",
                    suffix=".json",
                    dir=self._db_path.parent,
                )
                transaction_path = Path(transaction_name)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as transaction_file:
                    descriptor = -1
                    transaction_file.write(snapshot)
                    transaction_file.flush()
                    os.fsync(transaction_file.fileno())

                transaction_database = TinyDB(transaction_path)
                self.db = transaction_database
                self._set_tables()
                yield self
                self._validate_schema()
                transaction_database.close()
                live_database.close()
                os.chmod(transaction_path, 0o600)
                transaction_path.replace(self._db_path)
                transaction_path = None
                self._fsync_directory_best_effort()
            except BaseException:
                try:
                    self.db.close()
                except Exception:
                    pass
                try:
                    live_database.close()
                except Exception:
                    pass
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if transaction_path is not None:
                    transaction_path.unlink(missing_ok=True)
                self.db = TinyDB(self._db_path)
                self._set_tables()
                self._lock.transaction_owner = None

    def find_duplicate(self, job_data: dict) -> dict | None:
        with self._access():
            jobs = Query()
            content_hash = job_data.get("content_hash")
            url = job_data.get("url")

            existing = None
            if url:
                existing = self.jobs_table.get(jobs.url == url)

            if existing is None and content_hash:
                existing = self.jobs_table.get(jobs.content_hash == content_hash)

            if existing is None:
                return None

            return {**existing, "id": existing.doc_id}

    def save_job(self, job_data: dict | JobPosting) -> dict:
        saved_job, _ = self.save_job_with_status(job_data)
        return saved_job

    def save_job_with_status(
        self,
        job_data: dict | JobPosting,
    ) -> tuple[dict, bool]:
        with self._access():
            if isinstance(job_data, JobPosting):
                job = job_data
                data = job.model_dump(mode="json")
                data["content_hash"] = job_content_hash(job)
            else:
                data = dict(job_data)

            existing = self.find_duplicate(data)
            if existing is not None:
                return existing, False

            document_id = self.jobs_table.insert(data)
            return {**data, "id": document_id}, True

    def list_jobs(self) -> list[dict]:
        with self._access():
            return [{**job, "id": job.doc_id} for job in self.jobs_table.all()]

    def list_top_jobs(self) -> list[dict]:
        return sorted(
            self.list_jobs(),
            key=lambda job: job.get("fit_score", 0),
            reverse=True,
        )

    def clear_jobs(self) -> None:
        with self._access():
            self.jobs_table.truncate()

    def get_processed_email(self, message_id: str) -> dict | None:
        with self._access():
            emails = Query()
            email = self.processed_emails_table.get(
                emails.message_id == message_id
            )
            if email is None:
                return None
            return {**email, "id": email.doc_id}

    def list_processed_emails(self) -> list[dict]:
        with self._access():
            return [
                {**email, "id": email.doc_id}
                for email in self.processed_emails_table.all()
            ]

    def mark_email_processed(
        self,
        message_id: str,
        source: str,
        job_count: int,
        created_count: int,
    ) -> dict:
        with self._access():
            existing = self.get_processed_email(message_id)
            if existing is not None:
                return existing

            data = {
                "message_id": message_id,
                "source": source,
                "job_count": job_count,
                "created_count": created_count,
                "processed_at": utc_now().isoformat(),
            }
            document_id = self.processed_emails_table.insert(data)
            return {**data, "id": document_id}

    def notification_channel_initialized(self, channel: str) -> bool:
        with self._access():
            channels = Query()
            return self.notification_channels_table.contains(
                channels.channel == channel
            )

    def notification_channel_deferred(self, channel: str) -> bool:
        with self._access():
            channels = Query()
            existing = self.notification_channels_table.get(
                channels.channel == channel
            )
            if existing is None:
                return False
            try:
                retry_after = datetime.fromisoformat(
                    existing["retry_after_at"]
                )
                if retry_after.tzinfo is None:
                    retry_after = retry_after.replace(tzinfo=UTC)
                return utc_now() < retry_after.astimezone(UTC)
            except (KeyError, OverflowError, TypeError, ValueError):
                return False

    def notification_channel_disabled(self, channel: str) -> bool:
        with self._access():
            channels = Query()
            existing = self.notification_channels_table.get(
                channels.channel == channel
            )
            return bool(existing and existing.get("disabled"))

    def synchronize_notification_channel(
        self,
        channel: str,
        configuration_id: str | None,
    ) -> None:
        if configuration_id is None:
            return
        with self._access():
            channels = Query()
            existing = self.notification_channels_table.get(
                channels.channel == channel
            )
            if existing is None or existing.get("configuration_id") == (
                configuration_id
            ):
                return
            self.notification_channels_table.update(
                {
                    "configuration_id": configuration_id,
                    "disabled": False,
                    "disabled_error_type": None,
                    "retry_after_at": None,
                    "updated_at": utc_now().isoformat(),
                },
                doc_ids=[existing.doc_id],
            )

    def disable_notification_channel(
        self,
        channel: str,
        error_type: str,
    ) -> None:
        with self._access():
            channels = Query()
            existing = self.notification_channels_table.get(
                channels.channel == channel
            )
            if existing is None:
                raise ValueError("Notification channel is not initialized")
            self.notification_channels_table.update(
                {
                    "disabled": True,
                    "disabled_error_type": error_type,
                    "updated_at": utc_now().isoformat(),
                },
                doc_ids=[existing.doc_id],
            )

    def defer_notification_channel(
        self,
        channel: str,
        retry_after: timedelta,
    ) -> None:
        with self._access():
            channels = Query()
            existing = self.notification_channels_table.get(
                channels.channel == channel
            )
            if existing is None:
                raise ValueError("Notification channel is not initialized")
            now = utc_now()
            self.notification_channels_table.update(
                {
                    "retry_after_at": (now + retry_after).isoformat(),
                    "updated_at": now.isoformat(),
                },
                doc_ids=[existing.doc_id],
            )

    def initialize_notification_channel(
        self,
        channel: str,
        baseline_job_ids: list[int],
        configuration_id: str | None = None,
    ) -> tuple[bool, int]:
        with self._access():
            channels = Query()
            if self.notification_channels_table.contains(
                channels.channel == channel
            ):
                return False, 0

            notifications = Query()
            now = utc_now().isoformat()
            baseline_count = 0
            for job_id in baseline_job_ids:
                existing = self.job_notifications_table.get(
                    (notifications.channel == channel)
                    & (notifications.job_id == job_id)
                )
                if existing is not None:
                    continue
                self.job_notifications_table.insert(
                    {
                        "channel": channel,
                        "job_id": job_id,
                        "status": "baseline",
                        "attempts": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                baseline_count += 1

            self.notification_channels_table.insert(
                {
                    "channel": channel,
                    "configuration_id": configuration_id,
                    "disabled": False,
                    "initialized_at": now,
                }
            )
            return True, baseline_count

    def list_job_notifications(self, channel: str | None = None) -> list[dict]:
        with self._access():
            notifications = self.job_notifications_table.all()
            if channel is not None:
                notifications = [
                    item for item in notifications if item.get("channel") == channel
                ]
            return [
                {**notification, "id": notification.doc_id}
                for notification in notifications
            ]

    def list_attention_required_notifications(
        self,
        channel: str,
    ) -> list[dict]:
        return [
            notification
            for notification in self.list_job_notifications(channel)
            if notification.get("status") in {"pending", "unknown"}
        ]

    def get_job_notification(self, channel: str, job_id: int) -> dict | None:
        with self._access():
            notifications = Query()
            notification = self.job_notifications_table.get(
                (notifications.channel == channel)
                & (notifications.job_id == job_id)
            )
            if notification is None:
                return None
            return {**notification, "id": notification.doc_id}

    def reserve_job_notification(
        self,
        channel: str,
        job_id: int,
    ) -> bool:
        with self._access():
            notifications = Query()
            existing = self.job_notifications_table.get(
                (notifications.channel == channel)
                & (notifications.job_id == job_id)
            )
            now = utc_now()
            if existing is not None:
                status = existing.get("status")
                if status in {"baseline", "delivered", "pending", "unknown"}:
                    return False

                self.job_notifications_table.update(
                    {
                        "status": "pending",
                        "attempts": (
                            existing.get("attempts", 0) + 1
                            if isinstance(existing.get("attempts", 0), int)
                            and not isinstance(existing.get("attempts", 0), bool)
                            else 1
                        ),
                        "updated_at": now.isoformat(),
                    },
                    doc_ids=[existing.doc_id],
                )
                return True

            self.job_notifications_table.insert(
                {
                    "channel": channel,
                    "job_id": job_id,
                    "status": "pending",
                    "attempts": 1,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            return True

    def complete_job_notification(
        self,
        channel: str,
        job_id: int,
        external_id: str,
    ) -> None:
        self._update_job_notification(
            channel,
            job_id,
            {"status": "delivered", "external_id": external_id},
        )

    def fail_job_notification(
        self,
        channel: str,
        job_id: int,
        error_type: str,
    ) -> None:
        self._update_job_notification(
            channel,
            job_id,
            {"status": "failed", "error_type": error_type},
        )

    def mark_job_notification_unknown(
        self,
        channel: str,
        job_id: int,
        error_type: str,
        external_id: str | None = None,
    ) -> None:
        updates = {"status": "unknown", "error_type": error_type}
        if external_id is not None:
            updates["external_id"] = external_id
        self._update_job_notification(channel, job_id, updates)

    def resolve_job_notification(
        self,
        channel: str,
        job_id: int,
        resolution: str,
    ) -> None:
        if resolution not in {"delivered", "retry"}:
            raise ValueError("Unsupported notification resolution")
        with self._access():
            notifications = Query()
            existing = self.job_notifications_table.get(
                (notifications.channel == channel)
                & (notifications.job_id == job_id)
            )
            if existing is None or existing.get("status") not in {
                "pending",
                "unknown",
            }:
                raise ValueError("Notification does not require attention")
            updates = {
                "status": "delivered" if resolution == "delivered" else "failed",
                "error_type": (
                    "ManuallyMarkedDelivered"
                    if resolution == "delivered"
                    else "ManualRetryApproved"
                ),
                "updated_at": utc_now().isoformat(),
            }
            self.job_notifications_table.update(
                updates,
                doc_ids=[existing.doc_id],
            )

    def _update_job_notification(
        self,
        channel: str,
        job_id: int,
        updates: dict,
    ) -> None:
        with self._access():
            notifications = Query()
            existing = self.job_notifications_table.get(
                (notifications.channel == channel)
                & (notifications.job_id == job_id)
            )
            if existing is None:
                raise ValueError("Job notification reservation does not exist")
            self.job_notifications_table.update(
                {**updates, "updated_at": utc_now().isoformat()},
                doc_ids=[existing.doc_id],
            )

    def get_application(self, application_id: int) -> dict | None:
        with self._access():
            application = self.applications_table.get(doc_id=application_id)
            if application is None:
                return None
            return {**application, "id": application.doc_id}

    def list_applications(self) -> list[dict]:
        with self._access():
            return [
                {**application, "id": application.doc_id}
                for application in self.applications_table.all()
            ]

    def create_application(
        self,
        job_id: int,
        initial_message: str | None = None,
        current_step: str | None = None,
    ) -> dict:
        with self._access():
            if not self.jobs_table.contains(doc_id=job_id):
                raise ValueError(f"Job {job_id} does not exist")

            applications = Query()
            existing = self.applications_table.get(applications.job_id == job_id)
            if existing is not None:
                return {**existing, "id": existing.doc_id}

            message = (
                initial_message
                or "Application candidate created; waiting for approval to start"
            )
            next_step = current_step or message
            event = ApplicationEvent(
                status=ApplicationStatus.AWAITING_START_APPROVAL,
                message=message,
                current_step=next_step,
            )
            application = ApplicationRecord(
                job_id=job_id,
                current_step=next_step,
                events=[event],
            )
            document_id = self.applications_table.insert(
                application.model_dump(mode="json")
            )
            return {**application.model_dump(mode="json"), "id": document_id}

    def add_application_event(
        self,
        application_id: int,
        message: str,
        status: ApplicationStatus | str | None = None,
        current_step: str | None = None,
        provider: str | None = None,
        approval_kind: str | None = None,
        approved_by: str | None = None,
        expected_status: ApplicationStatus | str | None = None,
    ) -> dict:
        with self._access():
            application = self.get_application(application_id)
            if application is None:
                raise ValueError(f"Application {application_id} does not exist")

            current_status = ApplicationStatus(application["status"])
            if expected_status is not None:
                required_status = ApplicationStatus(expected_status)
                if current_status != required_status:
                    raise InvalidApplicationTransition(
                        f"Application {application_id} must be "
                        f"{required_status.value} for this action; "
                        f"current status is {current_status.value}"
                    )

            next_status = ApplicationStatus(status or current_status)
            if status is not None:
                validate_application_transition(
                    current_status,
                    next_status,
                    approval_kind=approval_kind,
                    approved_by=approved_by,
                )

            next_step = current_step or message
            next_provider = (
                provider if provider is not None else application.get("provider")
            )
            event = ApplicationEvent(
                status=next_status,
                message=message,
                current_step=next_step,
                provider=next_provider,
                approval_kind=approval_kind,
                approved_by=approved_by,
            )
            events = [*application["events"], event.model_dump(mode="json")]
            updates = {
                "status": next_status.value,
                "current_step": next_step,
                "events": events,
                "updated_at": utc_now().isoformat(),
            }
            if provider is not None:
                updates["provider"] = provider

            self.applications_table.update(updates, doc_ids=[application_id])
            updated_application = self.get_application(application_id)
            if updated_application is None:
                raise RuntimeError("Application disappeared after update")
            return updated_application


def save_job(job_data: dict) -> dict:
    return JobStorage().save_job(job_data)


def list_jobs() -> list[dict]:
    return JobStorage().list_jobs()


def list_top_jobs() -> list[dict]:
    return JobStorage().list_top_jobs()


def clear_jobs() -> None:
    JobStorage().clear_jobs()
