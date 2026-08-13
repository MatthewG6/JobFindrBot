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
    assert migrated["score_strong_threshold"] is None
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
        "score_strong_threshold": 75,
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
        "score_strong_threshold": 75,
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


def test_score_validation_accepts_exact_unconfirmed_location_cap(
    tmp_path: Path,
) -> None:
    from app.candidate_profile import default_candidate_profile
    from app.scoring import score_job, scoring_fields

    profile = default_candidate_profile().model_copy(
        update={
            "preferred_location_keywords": ["minneapolis"],
            "remote_or_hybrid_required_location_keywords": ["minneapolis"],
        }
    )
    posting = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Minneapolis, MN",
        url="https://example.com/unconfirmed",
        source="test",
        description=(
            "Build Java, TypeScript, and React services for a local product "
            "team."
        ),
    )
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(posting)
    updates = scoring_fields(score_job(posting, profile))

    assert updates["fit_score"] == profile.thresholds.strong - 1
    assert updates["score_strong_threshold"] == profile.thresholds.strong
    storage.update_job_score(saved["id"], updates, profile=profile)

    updates["fit_score"] += 1
    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_score(saved["id"], updates, profile=profile)


def test_score_validation_rejects_forged_strong_threshold(tmp_path: Path) -> None:
    from app.candidate_profile import default_candidate_profile
    from app.scoring import score_job, scoring_fields

    profile = default_candidate_profile()
    posting = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Minneapolis, MN",
        url="https://example.com/forged-threshold",
        source="test",
        description="Build Java, TypeScript, and React services.",
    )
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(posting)
    updates = scoring_fields(score_job(posting, profile))
    updates["score_strong_threshold"] = 92
    updates["fit_score"] = 91

    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_score(saved["id"], updates)


def test_score_validation_recomputes_required_location_uncertainty(
    tmp_path: Path,
) -> None:
    from app.candidate_profile import load_candidate_profile
    from app.scoring import score_job, scoring_fields

    profile = load_candidate_profile(
        Path("config/candidate_profile.example.yaml")
    ).model_copy(
        update={
            "preferred_location_keywords": [
                "remote",
                "rochester",
                "united states",
                "usa",
                "u.s.",
            ],
            "remote_or_hybrid_required_regions": [
                "twin_cities_seven_county"
            ],
            "require_preferred_location_for_strong": True,
        }
    )
    posting = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Minneapolis, KS",
        url="https://example.com/forged-location-evidence",
        source="test",
        description="Build Java, TypeScript, and React services onsite.",
    )
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(posting)
    updates = scoring_fields(score_job(posting, profile))
    uncertainty = "preferred location unconfirmed"
    updates["score_evidence"] = [
        item for item in updates["score_evidence"] if item["signal"] != uncertainty
    ]
    for dimension in updates["score_dimensions"]:
        dimension["evidence"] = [
            item for item in dimension["evidence"] if item["signal"] != uncertainty
        ]
    updates["fit_score"] = round(
        sum(item["weighted_points"] for item in updates["score_dimensions"])
    )

    with pytest.raises(ValueError, match="invalid"):
        storage.update_job_score(saved["id"], updates, profile=profile)
