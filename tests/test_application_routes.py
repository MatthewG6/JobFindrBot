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
        message="Application approved",
        status=ApplicationStatus.QUEUED,
        approval_kind="start",
        approved_by="Matthew",
    )
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
        message="Application approved",
        status=ApplicationStatus.QUEUED,
        approval_kind="start",
        approved_by="Matthew",
    )
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


def test_create_application_candidates_route_creates_records(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    strong_job = save_job(storage, "Strong Match", 85)
    save_job(storage, "Below Threshold", 70)

    response = client.post("/applications/candidates")
    summary = response.json()

    assert response.status_code == 201
    assert summary["threshold"] == 75
    assert summary["created_count"] == 1
    assert summary["candidates"] == [
        {
            "application_id": 1,
            "job_id": strong_job["id"],
            "status": "awaiting_start_approval",
            "current_step": "Awaiting Matthew approval",
            "job_title": "Strong Match",
            "company": "Example Company",
            "fit_score": 85,
            "job_url": "https://example.com/jobs/strong-match",
        }
    ]
    assert len(storage.list_applications()) == 1


def test_create_application_candidates_route_supports_threshold(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    save_job(storage, "Custom Threshold Match", 70)

    response = client.post("/applications/candidates?threshold=70")
    summary = response.json()

    assert response.status_code == 201
    assert summary["threshold"] == 70
    assert summary["created_count"] == 1
    assert summary["candidates"][0]["job_title"] == "Custom Threshold Match"


def test_create_application_candidates_route_does_not_duplicate_records(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    save_job(storage, "Strong Match", 90)

    first_response = client.post("/applications/candidates")
    second_response = client.post("/applications/candidates")

    assert first_response.json()["created_count"] == 1
    assert second_response.status_code == 201
    assert second_response.json() == {
        "threshold": 75,
        "created_count": 0,
        "candidates": [],
    }
    assert len(storage.list_applications()) == 1


def test_approve_and_complete_application_workflow(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])
    application_id = application["id"]

    approve_response = client.post(f"/applications/{application_id}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "queued"
    assert approve_response.json()["current_step"] == (
        "Queued to begin application"
    )

    transitions = [
        {
            "status": "in_progress",
            "message": "Application started",
            "current_step": "Entering work history",
            "provider": "workday",
        },
        {
            "status": "waiting_for_input",
            "message": "Need an answer from Matthew",
        },
        {
            "status": "in_progress",
            "message": "Matthew supplied the answer",
        },
        {
            "status": "ready_for_review",
            "message": "Application draft is ready for review",
        },
        {
            "status": "awaiting_submit_approval",
            "message": "Application reviewed; waiting to submit",
        },
    ]

    response = approve_response
    for transition in transitions:
        response = client.post(
            f"/applications/{application_id}/transitions",
            json=transition,
        )
        assert response.status_code == 200

    submit_approval_response = client.post(
        f"/applications/{application_id}/approve-submit"
    )
    assert submit_approval_response.status_code == 200
    assert submit_approval_response.json()["status"] == "approved_to_submit"

    response = client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "submitted", "message": "Application submitted"},
    )
    assert response.status_code == 200
    completed_application = response.json()
    assert completed_application["status"] == "submitted"
    assert completed_application["provider"] == "workday"
    assert len(completed_application["events"]) == 9
    assert completed_application["events"][1]["approval_kind"] == "start"
    assert completed_application["events"][1]["approved_by"] == "Matthew"
    assert completed_application["events"][7]["approval_kind"] == "submit"
    assert completed_application["events"][7]["approved_by"] == "Matthew"


def test_reject_application_candidate(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Rejected Match", 80)
    application = storage.create_application(saved_job["id"])

    response = client.post(f"/applications/{application['id']}/reject")

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    assert response.json()["current_step"] == "No application action planned"
    assert response.json()["events"][-1]["message"] == (
        "Application candidate rejected"
    )


def test_application_transition_rejects_invalid_status_jump(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])

    response = client.post(
        f"/applications/{application['id']}/transitions",
        json={
            "status": "submitted",
            "message": "Attempted to skip the approval workflow",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "Cannot transition application from awaiting_start_approval "
            "to submitted"
        )
    }
    unchanged_application = storage.get_application(application["id"])
    assert unchanged_application is not None
    assert unchanged_application["status"] == "awaiting_start_approval"
    assert len(unchanged_application["events"]) == 1


def test_application_actions_return_not_found(tmp_path: Path) -> None:
    client, _ = make_test_client(tmp_path)

    approve_response = client.post("/applications/99/approve")
    transition_response = client.post(
        "/applications/99/transitions",
        json={"status": "queued", "message": "Missing application"},
    )

    assert approve_response.status_code == 404
    assert transition_response.status_code == 404
    assert approve_response.json() == {
        "detail": "Application 99 does not exist"
    }


def test_application_transition_validates_request_body(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])

    response = client.post(
        f"/applications/{application['id']}/transitions",
        json={"status": "not-a-status", "message": ""},
    )

    assert response.status_code == 422


def test_application_transition_rejects_whitespace_only_fields(
    tmp_path: Path,
) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])

    response = client.post(
        f"/applications/{application['id']}/transitions",
        json={
            "status": "queued",
            "message": "   ",
            "current_step": "   ",
            "provider": "   ",
        },
    )

    assert response.status_code == 422


def test_generic_transition_cannot_bypass_start_approval(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])

    response = client.post(
        f"/applications/{application['id']}/transitions",
        json={"status": "queued", "message": "Skip start approval"},
    )

    assert response.status_code == 409
    assert "requires explicit start approval" in response.json()["detail"]


def test_generic_transition_cannot_bypass_submit_approval(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])
    application_id = application["id"]
    client.post(f"/applications/{application_id}/approve")
    client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "in_progress", "message": "Application started"},
    )
    client.post(
        f"/applications/{application_id}/transitions",
        json={
            "status": "awaiting_submit_approval",
            "message": "Waiting for submit approval",
        },
    )

    response = client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "submitted", "message": "Skip submit approval"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Cannot transition application from awaiting_submit_approval "
        "to submitted"
    )


def test_candidate_actions_require_initial_approval_status(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Strong Match", 90)
    application = storage.create_application(saved_job["id"])
    application_id = application["id"]
    first_approval = client.post(f"/applications/{application_id}/approve")

    repeated_approval = client.post(f"/applications/{application_id}/approve")
    late_rejection = client.post(f"/applications/{application_id}/reject")

    assert first_approval.status_code == 200
    assert repeated_approval.status_code == 409
    assert late_rejection.status_code == 409
    assert "current status is queued" in repeated_approval.json()["detail"]


def test_terminal_application_cannot_transition(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Rejected Match", 80)
    application = storage.create_application(saved_job["id"])
    application_id = application["id"]
    client.post(f"/applications/{application_id}/reject")

    response = client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "queued", "message": "Try to reopen"},
    )

    assert response.status_code == 409
    saved_application = storage.get_application(application_id)
    assert saved_application is not None
    assert saved_application["status"] == "skipped"
    assert len(saved_application["events"]) == 2


def test_failed_application_can_be_requeued(tmp_path: Path) -> None:
    client, storage = make_test_client(tmp_path)
    saved_job = save_job(storage, "Retry Match", 90)
    application = storage.create_application(saved_job["id"])
    application_id = application["id"]
    client.post(f"/applications/{application_id}/approve")
    client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "failed", "message": "Provider timed out"},
    )

    response = client.post(
        f"/applications/{application_id}/transitions",
        json={"status": "queued", "message": "Retry application"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
