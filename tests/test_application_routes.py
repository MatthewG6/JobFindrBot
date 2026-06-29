from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_storage
from app.models import ApplicationStatus
from app.storage import JobStorage


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> Generator[None, None, None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def make_test_client(tmp_path: Path) -> tuple[TestClient, JobStorage]:
    storage = JobStorage(tmp_path / "jobs.json")
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app), storage


def save_job(
    storage: JobStorage,
    title: str,
    fit_score: int,
) -> dict:
    slug = title.lower().replace(" ", "-")
    return storage.save_job(
        {
            "title": title,
            "company": "Example Company",
            "location": "Minneapolis, MN",
            "url": f"https://example.com/jobs/{slug}",
            "source": "test",
            "fit_score": fit_score,
            "content_hash": slug,
        }
    )


def test_list_applications_returns_all_records(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    first_job = save_job(storage, "First Job", 85)
    second_job = save_job(storage, "Second Job", 80)
    first_application = storage.create_application(first_job["id"])
    second_application = storage.create_application(second_job["id"])
    storage.add_application_event(
        second_application["id"],
        message="Application started",
        status=ApplicationStatus.IN_PROGRESS,
    )

    response = client.get("/applications")
    applications = response.json()

    assert response.status_code == 200
    assert len(applications) == 2
    assert {
        application["application_id"]
        for application in applications
    } == {first_application["id"], second_application["id"]}


def test_list_pending_applications_only_returns_waiting_records(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    pending_job = save_job(storage, "Pending Job", 90)
    started_job = save_job(storage, "Started Job", 88)
    pending_application = storage.create_application(pending_job["id"])
    started_application = storage.create_application(started_job["id"])
    storage.add_application_event(
        started_application["id"],
        message="Application started",
        status=ApplicationStatus.IN_PROGRESS,
    )

    response = client.get("/applications/pending")
    applications = response.json()

    assert response.status_code == 200
    assert len(applications) == 1
    assert applications[0]["application_id"] == pending_application["id"]
    assert applications[0]["status"] == "awaiting_start_approval"


def test_application_records_include_job_context(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Junior Software Engineer", 92)
    application = storage.create_application(saved_job["id"])

    record = client.get("/applications").json()[0]

    assert record == {
        "application_id": application["id"],
        "job_id": saved_job["id"],
        "status": "awaiting_start_approval",
        "current_step": (
            "Application candidate created; waiting for approval to start"
        ),
        "provider": None,
        "created_at": application["created_at"],
        "updated_at": application["updated_at"],
        "job_title": "Junior Software Engineer",
        "company": "Example Company",
        "location": "Minneapolis, MN",
        "fit_score": 92,
        "job_url": "https://example.com/jobs/junior-software-engineer",
    }


def test_application_record_handles_missing_job_context(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Deleted Job", 90)
    storage.create_application(saved_job["id"])
    storage.clear_jobs()

    response = client.get("/applications")
    record = response.json()[0]

    assert response.status_code == 200
    assert record["job_title"] is None
    assert record["company"] is None
    assert record["location"] is None
    assert record["fit_score"] is None
    assert record["job_url"] is None


def test_list_applications_returns_empty_list_cleanly(tmp_path: Path) -> None:
    client, _ = make_test_client(tmp_path)

    all_response = client.get("/applications")
    pending_response = client.get("/applications/pending")

    assert all_response.status_code == 200
    assert all_response.json() == []
    assert pending_response.status_code == 200
    assert pending_response.json() == []
