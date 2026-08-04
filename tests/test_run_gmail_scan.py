from pathlib import Path

from app.storage import JobStorage
from scripts.run_gmail_scan import print_summary, run_gmail_scan


class FakeClient:
    def scan(self, storage: JobStorage) -> dict:
        assert isinstance(storage, JobStorage)
        return {
            "messages_found": 2,
            "messages_fetched": 2,
            "messages_processed": 2,
            "messages_skipped": 0,
            "jobs_parsed": 5,
            "jobs_created": 4,
            "errors": [],
        }


def test_run_gmail_scan_uses_supplied_client_and_storage(tmp_path: Path) -> None:
    summary = run_gmail_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        client=FakeClient(),
    )

    assert summary["messages_processed"] == 2
    assert summary["jobs_created"] == 4


def test_print_summary_reports_counts(capsys, tmp_path: Path) -> None:
    print_summary(FakeClient().scan(JobStorage(tmp_path / "jobs.json")))

    output = capsys.readouterr().out
    assert "Gmail job alert scan complete" in output
    assert "Messages processed: 2" in output
    assert "Jobs created: 4" in output
    assert "Errors: 0" in output


def test_print_summary_reports_sanitized_error_metadata(
    capsys,
    tmp_path: Path,
) -> None:
    summary = FakeClient().scan(JobStorage(tmp_path / "jobs.json"))
    summary["errors"] = [
        {"message_id": "gmail-message-1", "error_type": "GmailMessageError"}
    ]

    print_summary(summary)

    output = capsys.readouterr().out
    assert "gmail-message-1: GmailMessageError" in output
