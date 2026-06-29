from pathlib import Path

from tinydb import Query, TinyDB

from app.dedupe import job_content_hash
from app.models import (
    ApplicationEvent,
    ApplicationRecord,
    ApplicationStatus,
    JobPosting,
    utc_now,
)


DEFAULT_DB_PATH = Path("data/jobs.json")


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(db_path)
        self.jobs_table = self.db.table("jobs")
        self.applications_table = self.db.table("applications")

    def find_duplicate(self, job_data: dict) -> dict | None:
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
        return [{"id": job.doc_id, **job} for job in self.jobs_table.all()]

    def list_top_jobs(self) -> list[dict]:
        return sorted(
            self.list_jobs(),
            key=lambda job: job.get("fit_score", 0),
            reverse=True,
        )

    def clear_jobs(self) -> None:
        self.jobs_table.truncate()

    def get_application(self, application_id: int) -> dict | None:
        application = self.applications_table.get(doc_id=application_id)
        if application is None:
            return None
        return {"id": application.doc_id, **application}

    def list_applications(self) -> list[dict]:
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
        event = ApplicationEvent(
            status=ApplicationStatus.AWAITING_START_APPROVAL,
            message=message,
        )
        application = ApplicationRecord(
            job_id=job_id,
            current_step=current_step or message,
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
    ) -> dict:
        application = self.get_application(application_id)
        if application is None:
            raise ValueError(f"Application {application_id} does not exist")

        next_status = ApplicationStatus(status or application["status"])
        event = ApplicationEvent(status=next_status, message=message)
        events = [*application["events"], event.model_dump(mode="json")]
        updates = {
            "status": next_status.value,
            "current_step": current_step or message,
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
