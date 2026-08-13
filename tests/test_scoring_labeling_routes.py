from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.candidate_profile import load_candidate_profile
from app.main import (
    app,
    get_scoring_label_store,
    get_storage,
    require_local_request,
)
from app.models import JobPosting
from app.scoring import score_job, scoring_fields
from app.scoring_labeling import ScoringLabelStore
from app.storage import JobStorage


PROFILE = load_candidate_profile(Path("config/candidate_profile.example.yaml"))


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> Generator[None, None, None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def populate_jobs(storage: JobStorage) -> None:
    job_number = 1
    for predicted, count in (("reject", 50), ("review", 40), ("strong", 20)):
        for offset in range(count):
            if predicted == "strong":
                title = "Junior Software Engineer"
                location = "Remote - United States"
                description = "Build React TypeScript cloud applications. " * 15
            elif predicted == "review":
                title = "Software Engineer"
                location = "Remote"
                description = (
                    "Build internal applications and maintain services. " * 12
                )
            else:
                title = "Business Analyst"
                location = "New York"
                description = (
                    "Analyze business processes and prepare reports. " * 12
                )
            posting = JobPosting(
                title=title,
                company=f"Example {job_number}",
                location=location,
                url=f"https://example.com/jobs/{job_number}",
                source=f"source-{offset % 4}",
                description=description,
            )
            storage.save_job(
                {
                    **posting.model_dump(mode="json"),
                    **scoring_fields(score_job(posting, PROFILE)),
                }
            )
            job_number += 1


def make_client(tmp_path: Path) -> tuple[TestClient, ScoringLabelStore]:
    storage = JobStorage(tmp_path / "jobs.json")
    populate_jobs(storage)
    label_store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_scoring_label_store] = lambda: label_store
    app.dependency_overrides[require_local_request] = lambda: None
    return TestClient(app), label_store


def test_labeling_routes_create_blind_session_and_record_calibration_label(
    tmp_path: Path,
) -> None:
    client, _ = make_client(tmp_path)

    missing = client.get("/scoring/labels/session")
    created = client.post("/scoring/labels/session")
    next_response = client.get("/scoring/labels/next")
    next_payload = next_response.json()
    labeled = client.post(
        f"/scoring/labels/{next_payload['job']['id']}",
        json={"label": "strong"},
    )

    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["total"] == 75
    assert next_response.status_code == 200
    assert next_response.headers["cache-control"] == "no-store"
    assert next_payload["split"] == "calibration"
    assert "fit_score" not in next_payload["job"]
    assert "score_dimensions" not in next_payload["job"]
    assert labeled.status_code == 200
    assert labeled.headers["cache-control"] == "no-store"
    assert labeled.json()["decision"]["label"] == "strong"
    assert labeled.json()["comparison"]["predicted_label"] in {
        "reject",
        "review",
        "strong",
    }
    assert labeled.json()["progress"]["labeled"] == 1

    duplicate = client.post(
        f"/scoring/labels/{next_payload['job']['id']}",
        json={"label": "reject"},
    )
    assert duplicate.status_code == 409
    assert duplicate.headers["cache-control"] == "no-store"

    calibration = client.get(
        "/scoring/benchmark",
        params={"split": "calibration"},
    )
    assert calibration.status_code == 200
    assert calibration.headers["cache-control"] == "no-store"


def test_validation_results_are_not_available_early(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    client.post("/scoring/labels/session")

    response = client.get("/scoring/benchmark", params={"split": "validation"})

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "hidden" in response.json()["detail"].lower()


def test_labeling_page_is_served_only_for_local_clients(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    app.dependency_overrides.pop(require_local_request)
    local_client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )

    response = local_client.get("/scoring/review")

    assert response.status_code == 200
    assert "Jobbot Scoring Review" in response.text
    assert "Diagnostic accuracy" in response.text
    assert "diagnostic_accuracy" in response.text
    assert "confusion_matrix" in response.text
    assert "validation_issues" in response.text
    assert '<section class="empty" id="start-state" hidden>' in response.text
    assert "startState.hidden = true" in response.text
    assert "startState.hidden = false" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers[
        "content-security-policy"
    ]

    remote_client = TestClient(app, client=("203.0.113.8", 50000))
    remote_response = remote_client.get("/scoring/review")

    assert remote_response.status_code == 403


def test_labeling_routes_reject_cross_origin_browser_requests(
    tmp_path: Path,
) -> None:
    client, _ = make_client(tmp_path)
    app.dependency_overrides.pop(require_local_request)
    local_client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )

    response = local_client.post(
        "/scoring/labels/session",
        headers={"origin": "https://attacker.example"},
    )

    assert response.status_code == 403

    wrong_port = local_client.post(
        "/scoring/labels/session",
        headers={"origin": "http://127.0.0.1:9999"},
    )
    assert wrong_port.status_code == 403
