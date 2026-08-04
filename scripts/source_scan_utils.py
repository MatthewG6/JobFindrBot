from collections.abc import Iterable

from app.ingestion import ingest_job
from app.models import JobPosting
from app.storage import JobStorage


def ingest_source_jobs(
    jobs: Iterable[JobPosting],
    storage: JobStorage,
    errors: list[dict[str, str]] | None = None,
) -> dict:
    records_received = getattr(jobs, "records_received", None)
    records_filtered = getattr(jobs, "records_filtered", 0)
    records_rejected = getattr(jobs, "records_rejected", 0)
    job_list = list(jobs)
    if records_received is None:
        records_received = len(job_list)
    combined_errors = list(errors or [])
    if records_received and not job_list and records_rejected:
        combined_errors.append({"error_type": "AllRecordsRejected"})

    results = [ingest_job(job, storage) for job in job_list]
    created_count = sum(1 for result in results if result["created"])
    return {
        "jobs_fetched": len(results),
        "jobs_created": created_count,
        "duplicates_skipped": len(results) - created_count,
        "records_received": records_received,
        "records_filtered": records_filtered,
        "records_rejected": records_rejected,
        "errors": combined_errors,
        "jobs": [
            {
                "title": result["job"]["title"],
                "company": result["job"]["company"],
                "url": result["job"]["url"],
                "fit_score": result["score"],
                "created": result["created"],
            }
            for result in results
        ],
    }
