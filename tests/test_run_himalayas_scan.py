import json
from pathlib import Path

from app.scanner import parse_himalayas_jobs
from app.storage import JobStorage
from scripts.run_himalayas_scan import run_himalayas_scan


FIXTURE_PATH = Path("tests/fixtures/himalayas_jobs.json")


def test_run_himalayas_scan_creates_jobs_and_skips_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_jobs = parse_himalayas_jobs(payload)
    storage = JobStorage(tmp_path / "jobs.json")
    monkeypatch.setattr(
        "scripts.run_himalayas_scan.fetch_himalayas_jobs",
        lambda: fixture_jobs,
    )

    first_summary = run_himalayas_scan(storage)
    second_summary = run_himalayas_scan(storage)

    assert first_summary["jobs_fetched"] == 2
    assert first_summary["jobs_created"] == 2
    assert first_summary["duplicates_skipped"] == 0
    assert second_summary["jobs_created"] == 0
    assert second_summary["duplicates_skipped"] == 2
    assert all(job["url"] for job in first_summary["jobs"])
    assert len(storage.list_jobs()) == 2
