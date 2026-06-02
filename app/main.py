from fastapi import Depends, FastAPI

from app.dedupe import job_content_hash
from app.models import JobPosting
from app.scoring import score_job
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
    saved_job = storage.save_job(job)
    return {"id": saved_job["id"], "status": "saved"}


@app.post("/jobs/manual", status_code=201)
def create_manual_job(job: JobPosting, storage: JobStorage = Depends(get_storage)) -> dict:
    scored_job = score_job(job)
    job_data = job.model_dump(mode="json")
    job_data["content_hash"] = job_content_hash(job)
    job_data["fit_score"] = scored_job.score
    job_data["score_reasons"] = scored_job.reasons
    job_data["red_flags"] = scored_job.red_flags

    existing_job = storage.find_duplicate(job_data)
    if existing_job is not None:
        return {
            "created": False,
            "job": existing_job,
            "score": existing_job.get("fit_score", scored_job.score),
            "reasons": existing_job.get("score_reasons", scored_job.reasons),
            "red_flags": existing_job.get("red_flags", scored_job.red_flags),
        }

    saved_job = storage.save_job(job_data)
    return {
        "created": True,
        "job": saved_job,
        "score": scored_job.score,
        "reasons": scored_job.reasons,
        "red_flags": scored_job.red_flags,
    }
