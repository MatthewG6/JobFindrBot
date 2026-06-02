from pathlib import Path

from app.models import JobPosting
from app.storage import JobStorage


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

    document_id = storage.save_job(job)

    assert document_id == 1


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

    first_document_id = storage.save_job(job)
    second_document_id = storage.save_job(job)
    jobs = storage.list_jobs()

    assert first_document_id == 1
    assert second_document_id == 1
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
    duplicate_document_id = storage.save_job(duplicate_job)
    jobs = storage.list_jobs()

    assert duplicate_document_id == 1
    assert len(jobs) == 1
