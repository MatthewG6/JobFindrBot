from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.email_ingestion import (
    JobAlertParseError,
    UntrustedJobAlertEmail,
    UntrustedSourceUrl,
    UnsupportedJobAlertEmail,
    ingest_job_alert_email,
    parse_indeed_jobs,
    parse_job_alert_email,
    parse_linkedin_jobs,
)
from app.models import JobAlertEmail, JobPosting
from app.storage import JobStorage


LINKEDIN_FIXTURE = Path("tests/fixtures/linkedin_job_alert.txt")
INDEED_FIXTURE = Path("tests/fixtures/indeed_job_alert.txt")
RECEIVED_AT = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)
PROCESS_INGEST_SCRIPT = r"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.email_ingestion import ingest_job_alert_email
from app.models import JobAlertEmail
from app.storage import JobStorage

db_path = Path(sys.argv[1])
index = int(sys.argv[2])
same_message = sys.argv[3] == "1"
body = Path("tests/fixtures/linkedin_job_alert.txt").read_text(encoding="utf-8")
message_id = "fake-linkedin-message-1"
if not same_message:
    body = body.replace("Northwind Systems", f"Northwind Systems {index}")
    body = body.replace("Contoso Labs", f"Contoso Labs {index}")
    body = body.replace("1234567890", f"1234567{index:03d}")
    body = body.replace("9876543210", f"9876543{index:03d}")
    message_id = f"fake-linkedin-process-{index}"

message = JobAlertEmail(
    message_id=message_id,
    sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
    subject="Junior software engineer jobs",
    received_at=datetime(2026, 8, 3, 12, 30, tzinfo=UTC),
    gmail_labels={"Jobbot-LinkedIn"},
    authentication_results="mx.google.com; dmarc=pass header.from=linkedin.com",
    text_body=body,
)
print(json.dumps(ingest_job_alert_email(message, JobStorage(db_path))))
"""


def make_email(
    *,
    message_id: str,
    sender: str,
    subject: str,
    fixture: Path,
) -> JobAlertEmail:
    is_linkedin = "linkedin.com" in sender
    return JobAlertEmail(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_at=RECEIVED_AT,
        gmail_labels={
            "Jobbot-LinkedIn" if is_linkedin else "Jobbot-Indeed"
        },
        authentication_results=(
            "mx.google.com; dmarc=pass header.from=linkedin.com"
            if is_linkedin
            else (
                "mx.google.com; dmarc=pass "
                "header.from=jobalert.indeed.com"
            )
        ),
        text_body=fixture.read_text(encoding="utf-8"),
    )


def linkedin_email() -> JobAlertEmail:
    return make_email(
        message_id="fake-linkedin-message-1",
        sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        subject="Junior software engineer jobs",
        fixture=LINKEDIN_FIXTURE,
    )


def indeed_email() -> JobAlertEmail:
    return make_email(
        message_id="fake-indeed-message-1",
        sender="Indeed <donotreply@jobalert.indeed.com>",
        subject="Your job alert is active",
        fixture=INDEED_FIXTURE,
    )


def test_parse_linkedin_jobs_extracts_canonical_jobs() -> None:
    jobs = parse_linkedin_jobs(linkedin_email())

    assert len(jobs) == 2
    assert all(isinstance(job, JobPosting) for job in jobs)
    assert jobs[0].title == "Junior Software Engineer"
    assert jobs[0].company == "Northwind Systems"
    assert jobs[0].location == "Minneapolis, MN"
    assert str(jobs[0].url) == (
        "https://www.linkedin.com/jobs/view/1234567890"
    )
    assert jobs[0].source == "linkedin_email"
    assert jobs[0].source_job_id == "1234567890"
    assert jobs[0].source_message_id == "fake-linkedin-message-1"
    assert jobs[0].alert_query == (
        "junior software engineer in Greater Minneapolis-St. Paul Area"
    )
    assert jobs[0].discovered_at == RECEIVED_AT
    assert "FAKE_TRACKING_VALUE" not in str(jobs[0].url)


def test_parse_indeed_jobs_extracts_details_and_canonical_id() -> None:
    jobs = parse_indeed_jobs(indeed_email())

    assert len(jobs) == 3
    assert jobs[0].title == "Java Full Stack Developer"
    assert jobs[0].company == "Northwind Consulting"
    assert jobs[0].location == "Minneapolis, MN"
    assert jobs[0].salary_text == "$70,000 - $95,000 a year"
    assert jobs[0].posted_text == "4 days ago"
    assert jobs[0].alert_query == "full stack developer in Minnesota"
    assert "Build and maintain Java" in jobs[0].description
    assert str(jobs[0].url) == (
        "https://www.indeed.com/jobs?"
        "q=Java+Full+Stack+Developer+Northwind+Consulting&l=Minneapolis%2C+MN"
    )
    assert "FAKE_JOB_REDIRECT_ONE" not in str(jobs[0].url)
    assert jobs[2].source_job_id == "fake-job-key-123"
    assert str(jobs[2].url) == (
        "https://www.indeed.com/viewjob?jk=fake-job-key-123"
    )


def test_parse_job_alert_email_dispatches_by_sender() -> None:
    assert len(parse_job_alert_email(linkedin_email())) == 2
    assert len(parse_job_alert_email(indeed_email())) == 3


def test_html_only_message_is_retryable_instead_of_marked_processed(
    tmp_path: Path,
) -> None:
    message = linkedin_email().model_copy(
        update={
            "text_body": "",
            "html_body": (
                "<html><body><pre>"
                + LINKEDIN_FIXTURE.read_text(encoding="utf-8")
                + "</pre></body></html>"
            ),
        }
    )

    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(JobAlertParseError):
        ingest_job_alert_email(message, storage)

    assert storage.list_jobs() == []
    assert storage.list_processed_emails() == []


def test_unsupported_sender_is_rejected_without_storage_changes(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    message = linkedin_email().model_copy(
        update={"sender": "Unknown Alerts <alerts@example.com>"}
    )

    with pytest.raises(UnsupportedJobAlertEmail):
        ingest_job_alert_email(message, storage)

    assert storage.list_jobs() == []
    assert storage.list_processed_emails() == []


def test_sender_address_must_match_exactly() -> None:
    message = linkedin_email().model_copy(
        update={
            "sender": (
                "Fake Alerts <jobalerts-noreply@linkedin.com.attacker.example>"
            )
        }
    )

    with pytest.raises(UnsupportedJobAlertEmail):
        parse_job_alert_email(message)


def test_sender_requires_expected_label_and_dmarc_authentication() -> None:
    missing_label = linkedin_email().model_copy(update={"gmail_labels": set()})
    failed_dmarc = linkedin_email().model_copy(
        update={
            "authentication_results": (
                "mx.google.com; dmarc=fail header.from=linkedin.com"
            )
        }
    )
    forged_auth_result = linkedin_email().model_copy(
        update={
            "authentication_results": (
                "attacker.example; dmarc=pass header.from=linkedin.com"
            )
        }
    )
    mixed_auth_results = linkedin_email().model_copy(
        update={
            "authentication_results": (
                "mx.google.com; "
                "dmarc=pass header.from=attacker.example; "
                "dmarc=fail header.from=linkedin.com"
            )
        }
    )
    wrong_indeed_domain = indeed_email().model_copy(
        update={
            "authentication_results": (
                "mx.google.com; dmarc=pass header.from=indeed.com"
            )
        }
    )

    with pytest.raises(UntrustedJobAlertEmail):
        parse_job_alert_email(missing_label)
    with pytest.raises(UntrustedJobAlertEmail):
        parse_job_alert_email(failed_dmarc)
    with pytest.raises(UntrustedJobAlertEmail):
        parse_job_alert_email(forged_auth_result)
    with pytest.raises(UntrustedJobAlertEmail):
        parse_job_alert_email(mixed_auth_results)
    with pytest.raises(UntrustedJobAlertEmail):
        parse_job_alert_email(wrong_indeed_domain)


def test_job_urls_must_use_the_expected_provider_host() -> None:
    linkedin = linkedin_email().model_copy(
        update={
            "text_body": LINKEDIN_FIXTURE.read_text(encoding="utf-8").replace(
                "https://www.linkedin.com/jobs/view/1234567890/",
                "https://attacker.example/jobs/view/1234567890/",
            )
        }
    )
    indeed = indeed_email().model_copy(
        update={
            "text_body": INDEED_FIXTURE.read_text(encoding="utf-8").replace(
                "https://www.indeed.com/viewjob?jk=fake-job-key-123",
                "https://attacker.example/?vjk=fake-job-key-123",
            )
        }
    )

    with pytest.raises(UntrustedSourceUrl):
        parse_linkedin_jobs(linkedin)
    with pytest.raises(UntrustedSourceUrl):
        parse_indeed_jobs(indeed)


def test_provider_urls_must_use_https() -> None:
    message = linkedin_email().model_copy(
        update={
            "text_body": LINKEDIN_FIXTURE.read_text(encoding="utf-8").replace(
                "https://www.linkedin.com/jobs/view/1234567890/",
                "http://www.linkedin.com/jobs/view/1234567890/",
            )
        }
    )

    with pytest.raises(UntrustedSourceUrl):
        parse_linkedin_jobs(message)


def test_partial_parse_is_retryable_instead_of_marked_processed(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    first_section = LINKEDIN_FIXTURE.read_text(encoding="utf-8").split(
        "---------------------------------------------------------"
    )[0]
    message = linkedin_email().model_copy(update={"text_body": first_section})

    with pytest.raises(JobAlertParseError, match="advertised 2 jobs but parsed 1"):
        ingest_job_alert_email(message, storage)

    assert storage.list_jobs() == []
    assert storage.list_processed_emails() == []

    result = ingest_job_alert_email(linkedin_email(), storage)

    assert result["processed"] is True
    assert len(storage.list_jobs()) == 2
    assert len(storage.list_processed_emails()) == 1


def test_linkedin_confirmation_uses_visible_job_count(tmp_path: Path) -> None:
    body = """Your job alert has been created: Software Engineer in Minnesota.
Software Engineer I
Example Systems
Minneapolis, MN
View job: https://www.linkedin.com/jobs/view/1112223334/
---------------------------------------------------------
See all jobs: https://www.linkedin.com/jobs/search-results/?keywords=software
"""
    message = linkedin_email().model_copy(
        update={
            "message_id": "fake-linkedin-confirmation",
            "text_body": body,
        }
    )

    storage = JobStorage(tmp_path / "jobs.json")
    result = ingest_job_alert_email(message, storage)

    assert result["processed"] is True
    assert result["parsed_count"] == 1
    assert storage.list_jobs()[0]["alert_query"] == (
        "Software Engineer in Minnesota"
    )


def test_complete_linkedin_email_accepts_provider_count_mismatch(
    tmp_path: Path,
) -> None:
    body = LINKEDIN_FIXTURE.read_text(encoding="utf-8").replace(
        "2 new jobs match your preferences.",
        "7 new jobs match your preferences.",
    )
    message = linkedin_email().model_copy(
        update={
            "message_id": "fake-linkedin-count-mismatch",
            "text_body": body,
        }
    )

    result = ingest_job_alert_email(
        message,
        JobStorage(tmp_path / "jobs.json"),
    )

    assert result["processed"] is True
    assert result["parsed_count"] == 2


def test_linkedin_footer_without_recognized_heading_is_retryable(
    tmp_path: Path,
) -> None:
    message = linkedin_email().model_copy(
        update={
            "message_id": "fake-linkedin-footer-only",
            "text_body": (
                "Unrecognized LinkedIn message\n"
                "See all jobs: https://www.linkedin.com/jobs/search-results/\n"
            ),
        }
    )
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(JobAlertParseError, match="no recognized job count"):
        ingest_job_alert_email(message, storage)

    assert storage.list_processed_emails() == []


def test_linkedin_more_visible_jobs_than_advertised_is_retryable(
    tmp_path: Path,
) -> None:
    body = LINKEDIN_FIXTURE.read_text(encoding="utf-8").replace(
        "2 new jobs match your preferences.",
        "1 new job matches your preferences.",
    )
    message = linkedin_email().model_copy(
        update={
            "message_id": "fake-linkedin-extra-visible-job",
            "text_body": body,
        }
    )
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(
        JobAlertParseError,
        match="advertised 1 jobs but parsed 2",
    ):
        ingest_job_alert_email(message, storage)

    assert storage.list_processed_emails() == []


def test_indeed_activation_notice_is_processed_as_zero_jobs(
    tmp_path: Path,
) -> None:
    message = indeed_email().model_copy(
        update={
            "message_id": "fake-indeed-activation",
            "text_body": (
                "Your job alert is active\n\n"
                "You'll receive your first daily job alert when jobs become "
                "available.\n"
            ),
        }
    )

    storage = JobStorage(tmp_path / "jobs.json")
    result = ingest_job_alert_email(message, storage)

    assert result["processed"] is True
    assert result["parsed_count"] == 0
    assert storage.list_jobs() == []


def test_indeed_active_heading_alone_is_not_a_zero_job_notice(
    tmp_path: Path,
) -> None:
    message = indeed_email().model_copy(
        update={
            "message_id": "fake-indeed-incomplete-activation",
            "text_body": "Your job alert is active\nUnrecognized content\n",
        }
    )
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(JobAlertParseError, match="no recognized job count"):
        ingest_job_alert_email(message, storage)

    assert storage.list_processed_emails() == []


def test_recognized_zero_job_alert_is_marked_processed(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    message = linkedin_email().model_copy(
        update={
            "message_id": "fake-zero-job-message",
            "text_body": (
                "Your job alert for software engineer in Minnesota\n"
                "0 new jobs match your preferences.\n"
            ),
        }
    )

    result = ingest_job_alert_email(message, storage)

    assert result["processed"] is True
    assert result["parsed_count"] == 0
    assert storage.list_jobs() == []
    assert len(storage.list_processed_emails()) == 1


def test_linkedin_wrapped_url_and_state_location_are_ingested(
    tmp_path: Path,
) -> None:
    body = """Your job alert for software engineer in Minnesota
1 new job matches your preferences.

Software Engineer I
Example Systems
Minnesota
View job:
https://www.linkedin.com/jobs/view/1112223334/?trackingId=FAKE
"""
    message = linkedin_email().model_copy(update={"text_body": body})

    storage = JobStorage(tmp_path / "jobs.json")

    result = ingest_job_alert_email(message, storage)
    jobs = storage.list_jobs()

    assert result["processed"] is True
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Minnesota"
    assert jobs[0]["source_job_id"] == "1112223334"


def test_failed_ledger_write_rolls_back_jobs_and_allows_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    original_mark = storage.mark_email_processed

    def fail_to_mark(*args, **kwargs):
        raise OSError("simulated ledger failure")

    monkeypatch.setattr(storage, "mark_email_processed", fail_to_mark)
    with pytest.raises(OSError, match="simulated ledger failure"):
        ingest_job_alert_email(linkedin_email(), storage)

    assert storage.list_jobs() == []
    assert storage.list_processed_emails() == []

    monkeypatch.setattr(storage, "mark_email_processed", original_mark)
    result = ingest_job_alert_email(linkedin_email(), storage)

    assert result["created_count"] == 2
    assert len(storage.list_jobs()) == 2
    assert len(storage.list_processed_emails()) == 1


def test_email_ingestion_scores_dedupes_and_marks_message_processed(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    first_result = ingest_job_alert_email(indeed_email(), storage)
    second_result = ingest_job_alert_email(indeed_email(), storage)

    assert first_result["processed"] is True
    assert first_result["parsed_count"] == 3
    assert first_result["created_count"] == 2
    assert second_result["processed"] is False
    assert second_result["already_processed"] is True
    assert len(storage.list_jobs()) == 2
    assert all("fit_score" in job for job in storage.list_jobs())
    assert len(storage.list_processed_emails()) == 1
    assert storage.list_processed_emails()[0]["message_id"] == (
        "fake-indeed-message-1"
    )


def test_processed_message_ledger_survives_new_storage_instance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "jobs.json"
    first_storage = JobStorage(db_path)
    ingest_job_alert_email(linkedin_email(), first_storage)

    second_storage = JobStorage(db_path)
    result = ingest_job_alert_email(linkedin_email(), second_storage)

    assert result["already_processed"] is True
    assert len(second_storage.list_jobs()) == 2
    assert len(second_storage.list_processed_emails()) == 1


def test_concurrent_email_ingestion_does_not_duplicate_jobs_or_ledger(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "jobs.json"

    def ingest() -> dict:
        return ingest_job_alert_email(linkedin_email(), JobStorage(db_path))

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ingest(), range(8)))

    storage = JobStorage(db_path)
    assert len(storage.list_jobs()) == 2
    assert len(storage.list_processed_emails()) == 1
    assert sum(result["processed"] for result in results) == 1
    assert sum(result["already_processed"] for result in results) == 7
    assert sum(result["created_count"] for result in results) == 2


@pytest.mark.parametrize("same_message", [True, False])
def test_process_concurrent_email_ingestion_is_consistent(
    tmp_path: Path,
    same_message: bool,
) -> None:
    db_path = tmp_path / "jobs.json"
    worker_count = 4

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                PROCESS_INGEST_SCRIPT,
                str(db_path),
                str(index),
                "1" if same_message else "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(worker_count)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]
    for process, (_, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, stderr
    results = [json.loads(stdout) for stdout, _ in outputs]

    storage = JobStorage(db_path)
    if same_message:
        assert len(storage.list_jobs()) == 2
        assert len(storage.list_processed_emails()) == 1
        assert sum(result["processed"] for result in results) == 1
    else:
        assert len(storage.list_jobs()) == worker_count * 2
        assert len(storage.list_processed_emails()) == worker_count
        assert all(result["processed"] for result in results)
