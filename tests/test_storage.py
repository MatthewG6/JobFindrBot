from pathlib import Path

import pytest
from tinydb import TinyDB

from app.models import JobPosting
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


def make_job(title: str = "Junior Software Engineer") -> JobPosting:
    return JobPosting(
        title=title,
        company="Example Company",
        location="Minneapolis, MN",
        url="https://example.com/job",
        source="test",
        description="Build Python services for a local product team.",
    )


def test_save_job_returns_document_id(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = make_job()

    saved_job = storage.save_job(job)

    assert saved_job["id"] == 1


def test_list_jobs_returns_saved_jobs(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = make_job()

    storage.save_job(job)
    jobs = storage.list_jobs()

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Junior Software Engineer"
    assert jobs[0]["company"] == "Example Company"


def test_save_job_does_not_duplicate_same_company_title_and_location(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = make_job()

    first_saved_job = storage.save_job(job)
    second_saved_job = storage.save_job(job)
    jobs = storage.list_jobs()

    assert first_saved_job["id"] == 1
    assert second_saved_job["id"] == 1
    assert len(jobs) == 1


def test_save_job_dedupe_ignores_extra_spacing_and_capitalization(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    first_job = make_job()
    duplicate_job = JobPosting(
        title="  junior software engineer  ",
        company="example company",
        location="MINNEAPOLIS, MN",
        url="https://example.com/duplicate-job",
        source="test",
    )

    storage.save_job(first_job)
    duplicate_saved_job = storage.save_job(duplicate_job)
    jobs = storage.list_jobs()

    assert duplicate_saved_job["id"] == 1
    assert len(jobs) == 1


def test_mark_email_processed_is_idempotent(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    first = storage.mark_email_processed(
        message_id="fake-message-id",
        source="linkedin_email",
        job_count=2,
        created_count=2,
    )
    second = storage.mark_email_processed(
        message_id="fake-message-id",
        source="linkedin_email",
        job_count=99,
        created_count=99,
    )

    assert first == second
    assert len(storage.list_processed_emails()) == 1
    assert storage.get_processed_email("fake-message-id") == first


def test_schema_six_backfills_legacy_scoring_state(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 5}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://example.com/jobs/1",
            "source": "test",
            "fit_score": 7,
            "score_reasons": ["legacy"],
            "red_flags": [],
        }
    )
    database.close()

    storage = JobStorage(database_path)
    migrated = storage.list_jobs()[0]

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 6
    assert migrated["scoring_version"] == 1
    assert migrated["score_review_threshold"] is None
    assert migrated["score_confidence"] is None
    assert migrated["score_confidence_band"] is None
    assert migrated["score_dimensions"] == []
    assert migrated["score_evidence"] == []


def test_update_job_score_rejects_invalid_nested_contract(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(make_job())
    invalid = {
        "fit_score": 80,
        "score_confidence": 70,
        "score_confidence_band": "medium",
        "score_dimensions": [
            {
                "name": name,
                "score": 80,
                "weight": 20,
                "weighted_points": 99,
                "summary": "invalid",
                "evidence": [],
            }
            for name in ("role", "seniority", "skills", "location", "risk")
        ],
        "score_evidence": [],
        "scoring_version": 2,
        "score_review_threshold": 40,
        "score_reasons": [],
        "red_flags": [],
    }

    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_score(saved["id"], invalid)


def test_enrichment_cannot_bypass_score_validation(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(make_job())
    invalid = {
        "fit_score": 1000,
        "score_confidence": 70,
        "score_confidence_band": "medium",
        "score_dimensions": [],
        "score_evidence": [],
        "scoring_version": 2,
        "score_review_threshold": 40,
        "score_reasons": [],
        "red_flags": [],
        "enrichment_status": "enriched",
    }

    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_enrichment(saved["id"], invalid)


def test_score_validation_enforces_exact_review_cap(tmp_path: Path) -> None:
    from app.scoring import score_job, scoring_fields

    storage = JobStorage(tmp_path / "jobs.json")
    posting = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Remote",
        url="https://example.com/risky",
        source="test",
        description=(
            "Contract only role building React TypeScript cloud systems."
        ),
    )
    saved = storage.save_job(posting)
    updates = scoring_fields(score_job(posting))
    assert updates["fit_score"] == 39
    updates["fit_score"] = 84

    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_score(saved["id"], updates)
