from pathlib import Path

from tinydb import TinyDB

from app.models import JobPosting


DEFAULT_DB_PATH = Path("data/jobs.json")


class JobStorage:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(db_path)

    def save_job(self, job: JobPosting) -> int:
        return self.db.insert(job.model_dump(mode="json"))

    def list_jobs(self) -> list[dict]:
        return self.db.all()
