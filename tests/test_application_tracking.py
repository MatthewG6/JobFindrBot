from pathlib import Path

import pytest

from app.models import ApplicationStatus, JobPosting
from app.storage import JobStorage


def save_test_job(storage: JobStorage) -> dict:
    job = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Minneapolis, MN",
        url="https://example.com/job",
        source="test",
    )
    return storage.save_job(job)


def test_create_application_starts_with_approval_status(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)

    application = storage.create_application(saved_job["id"])

    assert application["job_id"] == saved_job["id"]
    assert application["status"] == "awaiting_start_approval"
    assert application["current_step"] == (
        "Application candidate created; waiting for approval to start"
    )
    assert len(application["events"]) == 1
    assert application["events"][0]["status"] == "awaiting_start_approval"


def test_create_application_does_not_duplicate_same_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)

    first_application = storage.create_application(saved_job["id"])
    second_application = storage.create_application(saved_job["id"])

    assert first_application["id"] == second_application["id"]
    assert len(storage.list_applications()) == 1


def test_add_application_event_updates_status_and_history(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)
    application = storage.create_application(saved_job["id"])

    updated_application = storage.add_application_event(
        application["id"],
        message="Starting application in Workday",
        status=ApplicationStatus.IN_PROGRESS,
        current_step="Signing in",
        provider="workday",
    )

    assert updated_application["status"] == "in_progress"
    assert updated_application["current_step"] == "Signing in"
    assert updated_application["provider"] == "workday"
    assert len(updated_application["events"]) == 2
    assert updated_application["events"][1]["message"] == (
        "Starting application in Workday"
    )


def test_create_application_requires_existing_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="Job 99 does not exist"):
        storage.create_application(99)
