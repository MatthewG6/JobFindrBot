from pathlib import Path

from app.ingestion import ingest_job
from app.scanner import scan_jobs
from app.storage import JobStorage


def test_fake_scan_full_local_mvp_workflow(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    fake_jobs = scan_jobs()

    first_results = [ingest_job(job, storage) for job in fake_jobs]
    saved_jobs_after_first_run = storage.list_jobs()

    second_results = [ingest_job(job, storage) for job in fake_jobs]
    saved_jobs_after_second_run = storage.list_jobs()
    top_jobs = storage.list_top_jobs()

    assert len(fake_jobs) == 2
    assert all(result["created"] for result in first_results)
    assert len(saved_jobs_after_first_run) == 2

    assert not any(result["created"] for result in second_results)
    assert len(saved_jobs_after_second_run) == 2

    assert all("content_hash" in job for job in saved_jobs_after_second_run)
    assert all("fit_score" in job for job in saved_jobs_after_second_run)
    assert top_jobs[0]["fit_score"] >= top_jobs[1]["fit_score"]
