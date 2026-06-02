from pathlib import Path

from tinydb import Query, TinyDB

from app.dedupe import job_content_hash
from app.models import JobPosting


DEFAULT_DB_PATH = Path("data/jobs.json")


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(db_path)
        self.jobs_table = self.db.table("jobs")

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

    def clear_jobs(self) -> None:
        self.jobs_table.truncate()


def save_job(job_data: dict) -> dict:
    return JobStorage().save_job(job_data)


def list_jobs() -> list[dict]:
    return JobStorage().list_jobs()


def clear_jobs() -> None:
    JobStorage().clear_jobs()
