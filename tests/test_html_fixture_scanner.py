from pathlib import Path

from app.ingestion import ingest_job
from app.models import JobPosting
from app.scanner import parse_jobs_from_html_file
from app.storage import JobStorage


FIXTURE_PATH = Path("tests/fixtures/sample_jobs.html")


def test_parse_jobs_from_html_file_returns_expected_jobs() -> None:
    jobs = parse_jobs_from_html_file(str(FIXTURE_PATH))

    assert len(jobs) == 3
    assert all(isinstance(job, JobPosting) for job in jobs)
    assert jobs[0].title == "Junior Software Engineer"
    assert jobs[0].company == "North Star Apps"
    assert jobs[0].location == "Minneapolis, MN"
    assert str(jobs[0].url) == "https://example.com/jobs/junior-software-engineer"
    assert jobs[0].source == "html_fixture"


def test_parsed_html_jobs_can_be_ingested(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    jobs = parse_jobs_from_html_file(str(FIXTURE_PATH))

    results = [ingest_job(job, storage) for job in jobs]
    saved_jobs = storage.list_jobs()

    assert len(saved_jobs) == 3
    assert all(result["created"] for result in results)
    assert all("content_hash" in job for job in saved_jobs)
    assert all("fit_score" in job for job in saved_jobs)
    assert saved_jobs[0]["source"] == "html_fixture"
