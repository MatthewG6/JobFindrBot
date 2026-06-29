from fastapi import Depends, FastAPI

from app.applications import (
    list_application_records,
    list_pending_application_records,
)
from app.ingestion import ingest_job
from app.models import JobPosting
from app.scanner import scan_jobs
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


@app.get("/jobs/top")
def list_top_jobs(storage: JobStorage = Depends(get_storage)) -> list[dict]:
    return storage.list_top_jobs()


@app.get("/applications")
def list_applications(
    storage: JobStorage = Depends(get_storage),
) -> list[dict]:
    return list_application_records(storage)


@app.get("/applications/pending")
def list_pending_applications(
    storage: JobStorage = Depends(get_storage),
) -> list[dict]:
    return list_pending_application_records(storage)


@app.post("/jobs", status_code=201)
def create_job(job: JobPosting, storage: JobStorage = Depends(get_storage)) -> dict:
    return ingest_job(job, storage)


@app.post("/jobs/manual", status_code=201)
def create_manual_job(job: JobPosting, storage: JobStorage = Depends(get_storage)) -> dict:
    return ingest_job(job, storage)


@app.post("/scan/fake", status_code=201)
def run_fake_scan(storage: JobStorage = Depends(get_storage)) -> dict:
    results = [ingest_job(job, storage) for job in scan_jobs()]
    created_count = sum(1 for result in results if result["created"])
    duplicate_count = len(results) - created_count

    return {
        "created_count": created_count,
        "duplicate_count": duplicate_count,
        "results": results,
    }
