from app.candidates import job_qualifies_for_application
from app.models import ApplicationStatus
from app.storage import JobStorage


def list_application_records(
    storage: JobStorage,
    status: ApplicationStatus | str | None = None,
) -> list[dict]:
    """Return application records enriched with their saved job context."""
    status_value = ApplicationStatus(status).value if status else None
    jobs_by_id = {job["id"]: job for job in storage.list_jobs()}
    records: list[dict] = []

    for application in storage.list_applications():
        if status_value and application["status"] != status_value:
            continue

        job = jobs_by_id.get(application["job_id"], {})
        records.append(
            {
                "application_id": application["id"],
                "job_id": application["job_id"],
                "status": application["status"],
                "current_step": application["current_step"],
                "provider": application.get("provider"),
                "created_at": application["created_at"],
                "updated_at": application["updated_at"],
                "job_title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "fit_score": job.get("fit_score"),
                "job_url": job.get("url"),
            }
        )

    return records


def list_pending_application_records(storage: JobStorage) -> list[dict]:
    jobs_by_id = {job["id"]: job for job in storage.list_jobs()}
    return [
        record
        for record in list_application_records(
            storage,
            status=ApplicationStatus.AWAITING_START_APPROVAL,
        )
        if job_qualifies_for_application(jobs_by_id.get(record["job_id"], {}))
    ]
