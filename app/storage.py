from pathlib import Path

from tinydb import Query, TinyDB

from app.dedupe import job_key
from app.models import JobPosting


DEFAULT_DB_PATH = Path("data/jobs.json")


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(db_path)

    def save_job(self, job: JobPosting) -> int:
        dedupe_key = job_key(job)
        jobs = Query()
        existing = self.db.get(jobs.dedupe_key == dedupe_key)

        if existing is not None:
            return existing.doc_id

        job_data = job.model_dump(mode="json")
        job_data["dedupe_key"] = dedupe_key
        return self.db.insert(job_data)

    def list_jobs(self) -> list[dict]:
        return self.db.all()
