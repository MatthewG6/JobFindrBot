from fastapi import Depends, FastAPI, HTTPException

from app.applications import (
    list_application_records,
    list_pending_application_records,
)
from app.candidates import (
    DEFAULT_APPLICATION_THRESHOLD,
    create_application_candidates,
)
from app.ingestion import ingest_job
from app.models import (
    ApplicationStatus,
    ApplicationTransitionRequest,
    InvalidApplicationTransition,
    JobPosting,
)
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


def add_application_transition(
    application_id: int,
    transition: ApplicationTransitionRequest,
    storage: JobStorage,
    approval_kind: str | None = None,
    approved_by: str | None = None,
    expected_status: ApplicationStatus | None = None,
) -> dict:
    if storage.get_application(application_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"Application {application_id} does not exist",
        )

    try:
        return storage.add_application_event(
            application_id,
            message=transition.message,
            status=transition.status,
            current_step=transition.current_step,
            provider=transition.provider,
            approval_kind=approval_kind,
            approved_by=approved_by,
            expected_status=expected_status,
        )
    except InvalidApplicationTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/applications/{application_id}/approve")
def approve_application_candidate(
    application_id: int,
    storage: JobStorage = Depends(get_storage),
) -> dict:
    return add_application_transition(
        application_id,
        ApplicationTransitionRequest(
            status=ApplicationStatus.QUEUED,
            message="Application approved to start",
            current_step="Queued to begin application",
        ),
        storage,
        approval_kind="start",
        approved_by="Matthew",
        expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
    )


@app.post("/applications/{application_id}/reject")
def reject_application_candidate(
    application_id: int,
    storage: JobStorage = Depends(get_storage),
) -> dict:
    return add_application_transition(
        application_id,
        ApplicationTransitionRequest(
            status=ApplicationStatus.SKIPPED,
            message="Application candidate rejected",
            current_step="No application action planned",
        ),
        storage,
        expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
    )


@app.post("/applications/{application_id}/approve-submit")
def approve_application_submission(
    application_id: int,
    storage: JobStorage = Depends(get_storage),
) -> dict:
    return add_application_transition(
        application_id,
        ApplicationTransitionRequest(
            status=ApplicationStatus.APPROVED_TO_SUBMIT,
            message="Application approved for submission",
            current_step="Approved and ready to submit",
        ),
        storage,
        approval_kind="submit",
        approved_by="Matthew",
        expected_status=ApplicationStatus.AWAITING_SUBMIT_APPROVAL,
    )


@app.post("/applications/{application_id}/transitions")
def transition_application(
    application_id: int,
    transition: ApplicationTransitionRequest,
    storage: JobStorage = Depends(get_storage),
) -> dict:
    return add_application_transition(application_id, transition, storage)


@app.post("/applications/candidates", status_code=201)
def create_application_candidate_records(
    threshold: int = DEFAULT_APPLICATION_THRESHOLD,
    storage: JobStorage = Depends(get_storage),
) -> dict:
    created_applications = create_application_candidates(storage, threshold)
    jobs_by_id = {job["id"]: job for job in storage.list_jobs()}

    return {
        "threshold": threshold,
        "created_count": len(created_applications),
        "candidates": [
            {
                "application_id": application["id"],
                "job_id": application["job_id"],
                "status": application["status"],
                "current_step": application["current_step"],
                "job_title": jobs_by_id[application["job_id"]]["title"],
                "company": jobs_by_id[application["job_id"]]["company"],
                "fit_score": jobs_by_id[application["job_id"]].get(
                    "fit_score",
                    0,
                ),
                "job_url": jobs_by_id[application["job_id"]]["url"],
            }
            for application in created_applications
        ],
    }


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
