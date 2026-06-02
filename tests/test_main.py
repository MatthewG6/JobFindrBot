from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_storage
from app.storage import JobStorage


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> Generator[None, None, None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def make_test_client(tmp_path: Path) -> TestClient:
    def get_test_storage() -> JobStorage:
        return JobStorage(tmp_path / "jobs.json")

    app.dependency_overrides[get_storage] = get_test_storage
    return TestClient(app)


def test_health_check_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_job_saves_job(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    response = client.post(
        "/jobs",
        json={
            "title": "Junior Software Engineer",
            "company": "Example Company",
            "location": "Minneapolis, MN",
            "url": "https://example.com/job",
            "source": "test",
            "description": "Build Python APIs for a local product team.",
        },
    )

    assert response.status_code == 201
    response_data = response.json()
    assert response_data["created"] is True
    assert response_data["job"]["id"] == 1
    assert response_data["job"]["title"] == "Junior Software Engineer"
    assert "content_hash" in response_data["job"]


def test_list_jobs_returns_saved_jobs(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.post(
        "/jobs",
        json={
            "title": "Entry-Level Software Developer",
            "company": "Example Company",
            "location": "Remote",
            "url": "https://example.com/remote-job",
            "source": "test",
        },
    )

    response = client.get("/jobs")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["title"] == "Entry-Level Software Developer"


def test_list_top_jobs_returns_highest_fit_score_first(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.post(
        "/jobs/manual",
        json={
            "title": "Senior Software Architect",
            "company": "Example Company",
            "location": "Remote",
            "url": "https://example.com/senior-job",
            "source": "test",
            "description": "Requires 7+ years of experience.",
        },
    )
    client.post(
        "/jobs/manual",
        json={
            "title": "Junior React TypeScript Developer",
            "company": "Better Example Company",
            "location": "Minneapolis, MN",
            "url": "https://example.com/junior-job",
            "source": "test",
            "description": "Build React and TypeScript features for a cloud product.",
        },
    )

    response = client.get("/jobs/top")
    jobs = response.json()

    assert response.status_code == 200
    assert isinstance(jobs, list)
    assert jobs[0]["title"] == "Junior React TypeScript Developer"
    assert jobs[0]["fit_score"] > jobs[1]["fit_score"]


def test_list_top_jobs_handles_jobs_without_fit_score(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job(
        {
            "title": "Unscored Job",
            "company": "Example Company",
            "location": "Minneapolis, MN",
            "url": "https://example.com/unscored-job",
            "source": "test",
        }
    )
    client = make_test_client(tmp_path)
    client.post(
        "/jobs/manual",
        json={
            "title": "Junior React TypeScript Developer",
            "company": "Better Example Company",
            "location": "Minneapolis, MN",
            "url": "https://example.com/junior-job",
            "source": "test",
            "description": "Build React and TypeScript features.",
        },
    )

    response = client.get("/jobs/top")
    jobs = response.json()

    assert response.status_code == 200
    assert isinstance(jobs, list)
    assert jobs[0]["title"] == "Junior React TypeScript Developer"
    assert jobs[1]["title"] == "Unscored Job"
    assert "fit_score" not in jobs[1]


def test_manual_job_post_creates_job(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    response = client.post(
        "/jobs/manual",
        json={
            "title": "Junior Software Engineer",
            "company": "Example Company",
            "location": "Minneapolis, MN",
            "url": "https://example.com/manual-job",
            "source": "manual",
            "description": "Build React and TypeScript features.",
        },
    )

    response_data = response.json()

    assert response.status_code == 201
    assert response_data["created"] is True
    assert response_data["job"]["title"] == "Junior Software Engineer"
    assert response_data["score"] > 0


def test_manual_job_post_does_not_create_duplicate(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    job_data = {
        "title": "Junior Software Engineer",
        "company": "Example Company",
        "location": "Minneapolis, MN",
        "url": "https://example.com/manual-job",
        "source": "manual",
        "description": "Build Python APIs.",
    }

    first_response = client.post("/jobs/manual", json=job_data)
    second_response = client.post("/jobs/manual", json=job_data)
    jobs_response = client.get("/jobs")

    assert first_response.json()["created"] is True
    assert second_response.json()["created"] is False
    assert len(jobs_response.json()) == 1


def test_manual_job_saved_job_includes_score_and_hash_fields(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    response = client.post(
        "/jobs/manual",
        json={
            "title": "Senior Software Architect",
            "company": "Example Company",
            "location": "Remote",
            "url": "https://example.com/senior-job",
            "source": "manual",
            "description": "Requires 7+ years of experience.",
        },
    )

    saved_job = response.json()["job"]

    assert "fit_score" in saved_job
    assert "score_reasons" in saved_job
    assert "red_flags" in saved_job
    assert "content_hash" in saved_job
    assert saved_job["fit_score"] < 0
    assert saved_job["red_flags"]


def test_fake_scan_saves_sample_jobs(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    response = client.post("/scan/fake")
    jobs_response = client.get("/jobs")

    response_data = response.json()

    assert response.status_code == 201
    assert response_data["created_count"] == 2
    assert response_data["duplicate_count"] == 0
    assert len(response_data["results"]) == 2
    assert len(jobs_response.json()) == 2


def test_fake_scan_does_not_duplicate_sample_jobs(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    first_response = client.post("/scan/fake")
    second_response = client.post("/scan/fake")
    jobs_response = client.get("/jobs")

    assert first_response.json()["created_count"] == 2
    assert second_response.json()["created_count"] == 0
    assert second_response.json()["duplicate_count"] == 2
    assert len(jobs_response.json()) == 2
