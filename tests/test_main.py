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
    assert response.json() == {"id": 1, "status": "saved"}


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
