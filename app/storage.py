import fcntl
from pathlib import Path
from threading import Lock, RLock, local
from types import TracebackType

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
_DATABASE_LOCKS: dict[Path, "DatabaseLock"] = {}
_DATABASE_LOCKS_GUARD = Lock()


class DatabaseLock:
    """Serialize TinyDB access across threads and local processes."""

    def __init__(self, db_path: Path) -> None:
        self._thread_lock = RLock()
        self._local = local()
        self._lock_path = db_path.with_suffix(f"{db_path.suffix}.lock")

    def __enter__(self) -> "DatabaseLock":
        self._thread_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        lock_file = None

        try:
            if depth == 0:
                lock_file = self._lock_path.open("a+")
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


def database_lock(db_path: Path) -> DatabaseLock:
    resolved_path = db_path.resolve()
    with _DATABASE_LOCKS_GUARD:
        return _DATABASE_LOCKS.setdefault(
            resolved_path,
            DatabaseLock(resolved_path),
        )


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = database_lock(db_path)
        with self._lock:
            self.db = TinyDB(db_path)
            self.jobs_table = self.db.table("jobs")
            self.applications_table = self.db.table("applications")

    def find_duplicate(self, job_data: dict) -> dict | None:
        with self._lock:
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

            return {"id": existing.doc_id, **existing}

    def save_job(self, job_data: dict | JobPosting) -> dict:
        with self._lock:
            if isinstance(job_data, JobPosting):
                job = job_data
                data = job.model_dump(mode="json")
                data["content_hash"] = job_content_hash(job)
            else:
                data = dict(job_data)

            existing = self.find_duplicate(data)
            if existing is not None:
                return existing

            document_id = self.jobs_table.insert(data)
            return {"id": document_id, **data}

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [{"id": job.doc_id, **job} for job in self.jobs_table.all()]

    def list_top_jobs(self) -> list[dict]:
        return sorted(
            self.list_jobs(),
            key=lambda job: job.get("fit_score", 0),
            reverse=True,
        )

    def clear_jobs(self) -> None:
        with self._lock:
            self.jobs_table.truncate()

    def get_application(self, application_id: int) -> dict | None:
        with self._lock:
            application = self.applications_table.get(doc_id=application_id)
            if application is None:
                return None
            return {"id": application.doc_id, **application}

    def list_applications(self) -> list[dict]:
        with self._lock:
            return [
                {"id": application.doc_id, **application}
                for application in self.applications_table.all()
            ]

    def create_application(
        self,
        job_id: int,
        initial_message: str | None = None,
        current_step: str | None = None,
    ) -> dict:
        with self._lock:
            if not self.jobs_table.contains(doc_id=job_id):
                raise ValueError(f"Job {job_id} does not exist")

            applications = Query()
            existing = self.applications_table.get(applications.job_id == job_id)
            if existing is not None:
                return {"id": existing.doc_id, **existing}

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
            return {"id": document_id, **application.model_dump(mode="json")}

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
        with self._lock:
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
