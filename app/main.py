from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.applications import (
    list_application_records,
    list_pending_application_records,
)
from app.candidates import (
    DEFAULT_APPLICATION_THRESHOLD,
    create_application_candidates,
)
from app.candidate_profile import default_candidate_profile
from app.ingestion import ingest_job
from app.models import (
    ApplicationStatus,
    ApplicationTransitionRequest,
    InvalidApplicationTransition,
    JobPosting,
)
from app.scanner import scan_jobs
from app.scoring_labeling import (
    DEFAULT_LABELS_PATH,
    DEFAULT_QUEUE_PATH,
    DuplicateScoringLabel,
    LabelingSessionIncomplete,
    LabelingSessionNotFound,
    ScoringLabelStore,
    create_labeling_session,
    label_comparison,
    labeling_progress,
    next_labeling_job,
    record_scoring_label,
    session_benchmark,
)
from app.storage import JobStorage

CANDIDATE_PROFILE = default_candidate_profile()
app = FastAPI(title="Jobbot")
LABELING_PAGE = Path(__file__).resolve().parent / "static" / "scoring_review.html"


class ScoringLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Literal["reject", "review", "strong"]


@app.middleware("http")
async def prevent_scoring_response_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/scoring/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_storage() -> JobStorage:
    return JobStorage()


def get_scoring_label_store() -> ScoringLabelStore:
    return ScoringLabelStore(
        queue_path=DEFAULT_QUEUE_PATH,
        labels_path=DEFAULT_LABELS_PATH,
    )


def require_local_request(request: Request) -> None:
    client_host = request.client.host if request.client is not None else None
    host_header = request.headers.get("host", "")
    hostname = urlsplit(f"//{host_header}").hostname
    local_hosts = {"127.0.0.1", "::1", "localhost", "testclient"}
    origin = request.headers.get("origin")
    origin_parts = urlsplit(origin) if origin else None
    origin_hostname = origin_parts.hostname if origin_parts else None
    if (
        client_host not in {"127.0.0.1", "::1", "testclient"}
        or hostname not in local_hosts
        or (origin_hostname is not None and origin_hostname not in local_hosts)
        or (
            origin_parts is not None
            and origin_parts.netloc.lower() != host_header.lower()
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="The scoring review surface is available only on this computer",
        )


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Jobbot is running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/scoring/review",
    dependencies=[Depends(require_local_request)],
    include_in_schema=False,
)
def scoring_review_page() -> FileResponse:
    return FileResponse(
        LABELING_PAGE,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'none'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post(
    "/scoring/labels/session",
    dependencies=[Depends(require_local_request)],
)
def create_scoring_labeling_session(
    storage: JobStorage = Depends(get_storage),
    label_store: ScoringLabelStore = Depends(get_scoring_label_store),
) -> dict:
    try:
        create_labeling_session(
            storage.list_jobs(),
            label_store,
            CANDIDATE_PROFILE,
        )
        return labeling_progress(label_store).model_dump(mode="json")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get(
    "/scoring/labels/session",
    dependencies=[Depends(require_local_request)],
)
def get_scoring_labeling_session(
    label_store: ScoringLabelStore = Depends(get_scoring_label_store),
) -> dict:
    try:
        return labeling_progress(label_store).model_dump(mode="json")
    except LabelingSessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get(
    "/scoring/labels/next",
    dependencies=[Depends(require_local_request)],
)
def get_next_scoring_label(
    label_store: ScoringLabelStore = Depends(get_scoring_label_store),
) -> dict | None:
    try:
        item = next_labeling_job(label_store)
        return None if item is None else item.model_dump(mode="json")
    except LabelingSessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/scoring/labels/{job_id}",
    dependencies=[Depends(require_local_request)],
)
def submit_scoring_label(
    job_id: int,
    request: ScoringLabelRequest,
    label_store: ScoringLabelStore = Depends(get_scoring_label_store),
) -> dict:
    try:
        decision = record_scoring_label(
            label_store,
            job_id=job_id,
            label=request.label,
        )
        comparison = (
            label_comparison(
                label_store,
                CANDIDATE_PROFILE,
                job_id=job_id,
            )
            if decision.split == "calibration"
            else None
        )
        return {
            "decision": decision.model_dump(mode="json"),
            "comparison": comparison,
            "progress": labeling_progress(label_store).model_dump(mode="json"),
        }
    except DuplicateScoringLabel as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get(
    "/scoring/benchmark",
    dependencies=[Depends(require_local_request)],
)
def get_scoring_benchmark(
    split: Literal["calibration", "validation"] = "calibration",
    label_store: ScoringLabelStore = Depends(get_scoring_label_store),
) -> dict:
    try:
        return session_benchmark(
            label_store,
            CANDIDATE_PROFILE,
            split=split,
        )
    except (LabelingSessionIncomplete, LabelingSessionNotFound) as error:
        status_code = 404 if isinstance(error, LabelingSessionNotFound) else 409
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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
        approved_by=CANDIDATE_PROFILE.approval_name,
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
        approved_by=CANDIDATE_PROFILE.approval_name,
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
