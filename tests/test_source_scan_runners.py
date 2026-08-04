from pathlib import Path

from app.job_sources import ParsedJobs
from app.models import JobPosting
from app.storage import JobStorage
from scripts.run_adzuna_scan import run_adzuna_scan
from scripts.run_employer_watchlist_scan import run_employer_watchlist_scan
from scripts.source_scan_utils import ingest_source_jobs


def job(source: str, identifier: str) -> JobPosting:
    return JobPosting(
        title="Junior Software Engineer",
        company="Example",
        location="Remote",
        url=f"https://example.com/jobs/{identifier}",
        source=source,
        source_job_id=identifier,
    )


def test_credentialed_runner_passes_credentials_without_logging(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    captured = {}
    monkeypatch.setattr(
        "scripts.run_adzuna_scan.source_credentials",
        lambda *args: {"app_id": "private-id", "app_key": "private-key"},
    )

    def fetch(**kwargs):
        captured.update(kwargs)
        return [job("adzuna", "1")]

    monkeypatch.setattr("scripts.run_adzuna_scan.fetch_adzuna_jobs", fetch)

    summary = run_adzuna_scan(JobStorage(tmp_path / "jobs.json"))

    assert captured == {"app_id": "private-id", "app_key": "private-key"}
    assert summary["jobs_created"] == 1
    assert "private" not in capsys.readouterr().out


def test_employer_watchlist_isolates_board_failures(
    tmp_path: Path, monkeypatch
) -> None:
    entries = [
        {"provider": "greenhouse", "site": "good", "company": "Good"},
        {"provider": "lever", "site": "private-site", "company": "Bad"},
    ]
    monkeypatch.setattr(
        "scripts.run_employer_watchlist_scan.load_employer_watchlist",
        lambda: entries,
    )

    def fetch(entry):
        if entry["site"] == "private-site":
            raise RuntimeError("private network diagnostic")
        return [job("greenhouse", "1")]

    monkeypatch.setattr(
        "scripts.run_employer_watchlist_scan.fetch_employer_board", fetch
    )

    summary = run_employer_watchlist_scan(
        JobStorage(tmp_path / "jobs.json")
    )

    assert summary["jobs_created"] == 1
    assert summary["errors"] == [
        {
            "provider": "lever",
            "site": "private-site",
            "error_type": "RuntimeError",
        }
    ]


def test_all_rejected_provider_records_fail_source_health(
    tmp_path: Path,
) -> None:
    jobs = ParsedJobs(records_received=2, records_rejected=2)

    summary = ingest_source_jobs(
        jobs,
        JobStorage(tmp_path / "jobs.json"),
    )

    assert summary["jobs_fetched"] == 0
    assert summary["records_received"] == 2
    assert summary["records_rejected"] == 2
    assert summary["errors"] == [{"error_type": "AllRecordsRejected"}]
