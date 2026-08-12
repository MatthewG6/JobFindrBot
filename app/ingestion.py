from app.dedupe import job_content_hash
from app.models import JobPosting
from app.scoring import score_job, scoring_fields
from app.storage import JobStorage


def ingest_job(job: JobPosting, storage: JobStorage) -> dict:
    """Score, hash, dedupe, and save one job posting.

    This is the main job ingestion pipeline. Routes and scanners should call
    this function instead of repeating the same scoring and storage steps.
    """
    scored_job = score_job(job)
    job_data = job.model_dump(mode="json")
    job_data["content_hash"] = job_content_hash(job)
    job_data.update(scoring_fields(scored_job))

    saved_job, created = storage.save_job_with_status(job_data)
    if not created:
        return {
            "created": False,
            "job": saved_job,
            "score": saved_job.get("fit_score", scored_job.score),
            "reasons": saved_job.get("score_reasons", scored_job.reasons),
            "red_flags": saved_job.get("red_flags", scored_job.red_flags),
            "confidence": saved_job.get(
                "score_confidence",
                scored_job.confidence,
            ),
        }

    return {
        "created": True,
        "job": saved_job,
        "score": scored_job.score,
        "reasons": scored_job.reasons,
        "red_flags": scored_job.red_flags,
        "confidence": scored_job.confidence,
    }
