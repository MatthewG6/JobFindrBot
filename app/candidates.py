from math import isfinite

from app.candidate_profile import default_candidate_profile
from app.storage import JobStorage


CANDIDATE_PROFILE = default_candidate_profile()
DEFAULT_APPLICATION_THRESHOLD = CANDIDATE_PROFILE.thresholds.strong


def qualifies_for_application(value: object, threshold: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isfinite(value) and value >= threshold


def create_application_candidates(
    storage: JobStorage,
    threshold: int = DEFAULT_APPLICATION_THRESHOLD,
) -> list[dict]:
    """Create approval-ready application records for strong saved jobs."""
    jobs = storage.list_jobs()
    existing_job_ids = {
        application["job_id"]
        for application in storage.list_applications()
    }
    created_applications: list[dict] = []

    for job in jobs:
        fit_score = job.get("fit_score", 0)
        if job.get("scoring_version") != 2 or not qualifies_for_application(
            fit_score,
            threshold,
        ):
            continue

        if job["id"] in existing_job_ids:
            continue

        message = (
            f"Application candidate created because fit score {fit_score} "
            f"met threshold {threshold}"
        )
        application = storage.create_application(
            job_id=job["id"],
            initial_message=message,
            current_step=(
                f"Awaiting {CANDIDATE_PROFILE.approval_name} approval"
            ),
        )
        created_applications.append(application)
        existing_job_ids.add(job["id"])

    return created_applications
