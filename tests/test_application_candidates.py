from pathlib import Path

import pytest

from app.candidates import CANDIDATE_PROFILE, create_application_candidates
from app.scoring import SCORING_VERSION
from app.storage import JobStorage


def save_scored_job(
    storage: JobStorage,
    title: str,
    fit_score: object,
    scoring_version: int = SCORING_VERSION,
) -> dict:
    slug = title.lower().replace(" ", "-")
    return storage.save_job(
        {
            "title": title,
            "company": "Example Company",
            "location": "Remote",
            "url": f"https://example.com/jobs/{slug}",
            "source": "test",
            "fit_score": fit_score,
            "scoring_version": scoring_version,
            "content_hash": slug,
        }
    )


def test_high_scoring_job_creates_application_candidate(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    high_score_job = save_scored_job(storage, "Junior Software Engineer", 80)

    created = create_application_candidates(storage)

    assert len(created) == 1
    assert created[0]["job_id"] == high_score_job["id"]
    assert created[0]["status"] == "awaiting_start_approval"
    assert created[0]["current_step"] == (
        f"Awaiting {CANDIDATE_PROFILE.approval_name} approval"
    )
    assert created[0]["provider"] is None


def test_low_scoring_job_does_not_create_candidate(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Low Score Job", 74)

    created = create_application_candidates(storage)

    assert created == []
    assert storage.list_applications() == []


def test_stale_high_score_does_not_create_candidate(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(
        storage,
        "Legacy Strong Match",
        90,
        scoring_version=1,
    )

    created = create_application_candidates(storage)

    assert created == []
    assert storage.list_applications() == []


def test_candidate_threshold_can_be_raised(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Custom Threshold Match", 90)

    created = create_application_candidates(storage, threshold=90)

    assert len(created) == 1


def test_candidate_threshold_cannot_be_below_strong(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Below Strong Candidate", 70)

    with pytest.raises(ValueError, match="below the active Strong"):
        create_application_candidates(storage, threshold=70)


def test_missing_and_invalid_fit_scores_do_not_create_candidates(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job(
        {
            "title": "Missing Score",
            "company": "Example Company",
            "location": "Remote",
            "url": "https://example.com/jobs/missing-score",
            "source": "test",
            "content_hash": "missing-score",
        }
    )
    save_scored_job(storage, "String Score", "90")
    save_scored_job(storage, "Not A Number Score", float("nan"))

    created = create_application_candidates(storage)

    assert created == []
    assert storage.list_applications() == []


def test_running_candidate_creation_twice_does_not_duplicate(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Strong Match", 90)

    first_created = create_application_candidates(storage)
    second_created = create_application_candidates(storage)

    assert len(first_created) == 1
    assert second_created == []
    assert len(storage.list_applications()) == 1


def test_candidate_records_threshold_event(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Strong Match", 85)

    application = create_application_candidates(storage)[0]
    initial_event = application["events"][0]

    assert initial_event["status"] == "awaiting_start_approval"
    assert initial_event["message"] == (
        "Application candidate created because fit score 85 met threshold 75"
    )
    assert initial_event["created_at"]


def test_existing_application_record_is_respected(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_scored_job(storage, "Existing Candidate", 95)
    existing = storage.create_application(saved_job["id"])

    created = create_application_candidates(storage)
    applications = storage.list_applications()

    assert created == []
    assert len(applications) == 1
    assert applications[0]["id"] == existing["id"]
    assert len(applications[0]["events"]) == 1
