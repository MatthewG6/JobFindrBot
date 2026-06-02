from pathlib import Path

from app.storage import JobStorage
from scripts.run_html_fixture_scan import run_html_fixture_scan


def test_run_html_fixture_scan_creates_jobs_and_skips_duplicates(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    first_summary = run_html_fixture_scan(storage)
    second_summary = run_html_fixture_scan(storage)

    assert first_summary["jobs_parsed"] == 3
    assert first_summary["jobs_created"] == 3
    assert first_summary["duplicates_skipped"] == 0
    assert second_summary["jobs_created"] == 0
    assert second_summary["duplicates_skipped"] == 3
    assert len(storage.list_jobs()) == 3
