from fastapi import Depends, FastAPI

from app.models import JobPosting
from app.storage import JobStorage

app = FastAPI(title="Job Radar Assistant")


def get_storage() -> JobStorage:
    return JobStorage()


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Job Radar Assistant is running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs")
def list_jobs(storage: JobStorage = Depends(get_storage)) -> list[dict]:
    return storage.list_jobs()


@app.post("/jobs", status_code=201)
def create_job(job: JobPosting, storage: JobStorage = Depends(get_storage)) -> dict[str, int | str]:
    document_id = storage.save_job(job)
    return {"id": document_id, "status": "saved"}
