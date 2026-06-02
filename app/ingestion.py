from app.dedupe import job_content_hash
from app.models import JobPosting
from app.scoring import score_job
from app.storage import JobStorage


def ingest_job(job: JobPosting, storage: JobStorage) -> dict:
    """Score, hash, dedupe, and save one job posting.

    This is the main job ingestion pipeline. Routes and scanners should call
    this function instead of repeating the same scoring and storage steps.
    """
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
