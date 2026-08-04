from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket
from urllib.parse import urlsplit

import pytest
import requests
from tinydb import TinyDB
from urllib3.exceptions import ProtocolError

from app.employer_resolver import (
    postings_match,
    resolve_employer_sites,
    set_manual_application_url,
)
from app.job_links import job_link_role
from app.ingestion import ingest_job
from app.models import JobPosting
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


NOW = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        *,
        content: bytes = b"<html></html>",
        status_code: int = 200,
        headers: dict | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("request failed")

    def iter_content(self, chunk_size: int):
        yield self.content

    def close(self) -> None:
        return None


def public_dns(*args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


def posting(
    *,
    source: str,
    url: str,
    location: str = "Minneapolis, MN",
    company: str = "Example, Inc.",
) -> JobPosting:
    return JobPosting(
        title="Junior Software Engineer",
        company=company,
        location=location,
        url=url,
        source=source,
        description="Build reliable services.",
    )


def test_duplicate_ingestion_preserves_official_link_and_resolves(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )
    result = ingest_job(
        posting(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
        ),
        storage,
    )

    summary = resolve_employer_sites(storage)
    job = storage.list_jobs()[0]

    assert result["created"] is False
    assert len(storage.list_jobs()) == 1
    assert {link["role"] for link in storage.list_job_links(job["id"])} == {
        "discovery",
        "official",
    }
    assert summary["jobs_resolved"] == 1
    assert job["resolution_status"] == "resolved"
    assert job["application_url"].startswith(
        "https://job-boards.greenhouse.io/"
    )

    second = resolve_employer_sites(storage)
    assert second["jobs_resolved"] == 0
    assert second["jobs_already_resolved"] == 1
    assert len(storage.list_job_links(job["id"])) == 2


def test_exact_posting_match_resolves_location_alias(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    provider = ingest_job(
        posting(
            source="indeed_email",
            url="https://www.indeed.com/viewjob?jk=abc",
        ),
        storage,
    )["job"]
    ingest_job(
        posting(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
            location="Minneapolis, Minnesota",
        ),
        storage,
    )

    summary = resolve_employer_sites(storage)
    resolved = storage.get_job(provider["id"])

    assert summary["jobs_resolved"] == 1
    assert resolved["resolution_method"] == "exact_official_posting_match"
    assert resolved["resolution_confidence"] == 0.95


def test_ambiguous_official_matches_require_manual_review(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    provider = ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
            location="Remote, USA",
        ),
        storage,
    )["job"]
    for source, url, location in (
        (
            "greenhouse",
            "https://job-boards.greenhouse.io/example/jobs/1",
            "Remote, USA",
        ),
        (
            "lever",
            "https://jobs.lever.co/example/2",
            "Remote - United States",
        ),
    ):
        ingest_job(
            posting(
                source=source,
                url=url,
                location=location,
                company=(
                    "Example Inc" if source == "greenhouse" else "Example LLC"
                ),
            ),
            storage,
        )

    summary = resolve_employer_sites(storage)
    unresolved = storage.get_job(provider["id"])

    assert summary["jobs_manual_required"] == 1
    assert unresolved["resolution_status"] == "manual_required"
    assert unresolved["application_url"] is None


def test_manual_handoff_rejects_provider_and_private_urls(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]

    for invalid in (
        "https://www.linkedin.com/jobs/view/456",
        "https://localhost/jobs/456",
        "https://2130706433/jobs/456",
        "http://careers.example.com/jobs/456",
    ):
        with pytest.raises(ValueError):
            set_manual_application_url(storage, job["id"], invalid)

    resolved = set_manual_application_url(
        storage,
        job["id"],
        "https://careers.example.com/jobs/456",
    )

    assert resolved["resolution_status"] == "resolved"
    assert resolved["resolution_method"] == "manual_handoff"
    assert resolved["enrichment_status"] == "pending"


def test_schema_two_migrates_existing_jobs_and_links(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 1}
    )
    database.table("jobs").insert(
        {
            "title": "Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://jobs.lever.co/example/123",
            "source": "lever",
        }
    )
    database.close()

    storage = JobStorage(database_path)
    job = storage.list_jobs()[0]

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 4
    assert job["resolution_status"] == "resolved"
    assert job["application_url"] == job["url"]
    assert storage.list_job_links(job["id"])[0]["role"] == "official"


def test_failed_schema_two_migration_leaves_schema_one_unchanged(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 1}
    )
    database.table("jobs").insert(
        {
            "title": "Original",
            "company": "Example",
            "location": "Remote",
            "url": "https://www.linkedin.com/jobs/view/123",
            "source": "linkedin_email",
        }
    )
    database.close()
    original = database_path.read_bytes()

    class FailingResolverMigrationStorage(JobStorage):
        def _apply_migration(self, version: int) -> None:
            if version == 2:
                self.jobs_table.update({"resolution_status": "partial"})
                raise RuntimeError("migration failed")
            super()._apply_migration(version)

    with pytest.raises(RuntimeError, match="migration failed"):
        FailingResolverMigrationStorage(database_path)

    assert database_path.read_bytes() == original


def test_resolver_recovers_missing_base_provenance(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]
    storage.job_links_table.truncate()

    resolve_employer_sites(storage)

    links = storage.list_job_links(job["id"])
    assert len(links) == 1
    assert links[0]["role"] == "discovery"


def test_official_source_name_cannot_spoof_an_unrelated_domain() -> None:
    assert (
        job_link_role("greenhouse", "https://careers.example.com/jobs/1")
        == "discovery"
    )
    assert (
        job_link_role(
            "greenhouse",
            "https://job-boards.greenhouse.io/example/jobs/1",
        )
        == "official"
    )


def test_matching_is_conservative_for_title_symbols_and_remote_regions() -> None:
    common = {"company": "Example", "location": "Remote, United States"}
    assert not postings_match(
        {**common, "title": "C++ Engineer"},
        {**common, "title": "C# Engineer"},
    )
    assert not postings_match(
        {**common, "title": "Software Engineer"},
        {
            "company": "Example",
            "location": "Remote, Europe",
            "title": "Software Engineer",
        },
    )


def test_manual_handoff_rolls_back_link_when_resolution_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]
    original_links = storage.list_job_links(job["id"])

    monkeypatch.setattr(
        storage,
        "update_job_resolution",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with pytest.raises(RuntimeError):
        set_manual_application_url(
            storage,
            job["id"],
            "https://careers.example.com/jobs/456",
        )

    assert storage.list_job_links(job["id"]) == original_links


def test_tampered_official_link_fails_safe(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]
    storage.job_links_table.insert(
        {
            "job_id": job["id"],
            "url": "https://www.linkedin.com/jobs/view/456",
            "source": "manual",
            "role": "official",
        }
    )

    summary = resolve_employer_sites(storage)

    assert summary["errors"] == [
        {"job_id": job["id"], "error_type": "InvalidOfficialLink"}
    ]
    assert storage.get_job(job["id"])["application_url"] is None


def test_adzuna_redirect_resolves_to_official_destination(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="adzuna",
            url="https://www.adzuna.com/details/123",
        ),
        storage,
    )["job"]
    requests_seen = []

    def fake_get(url: str, **kwargs):
        requests_seen.append(url)
        if url == "https://www.adzuna.com/details/123":
            return FakeResponse(
                status_code=302,
                headers={"Location": "https://careers.example.com/jobs/456"},
            )
        return FakeResponse()

    summary = resolve_employer_sites(
        storage,
        destination_get=fake_get,
        dns_resolver=public_dns,
        now=NOW,
    )
    resolved = storage.get_job(job["id"])

    assert requests_seen == [
        "https://www.adzuna.com/details/123",
        "https://careers.example.com/jobs/456",
    ]
    assert summary["provider_jobs_attempted"] == 1
    assert summary["provider_jobs_resolved"] == 1
    assert resolved["application_url"] == "https://careers.example.com/jobs/456"
    assert resolved["resolution_method"] == "provider_redirect"
    assert resolved["resolution_attempt_count"] == 1
    assert storage.list_job_links(job["id"])[-1]["source"] == "employer_resolver"


def test_remotive_exact_apply_link_resolves_destination(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="remotive",
            url="https://remotive.com/remote-jobs/software-dev/example-123",
        ),
        storage,
    )["job"]
    html = b"""
        <html><body>
          <a href="https://apply.workable.com/example/j/ABC/">
            Apply for this position
          </a>
        </body></html>
    """

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: FakeResponse(content=html),
        dns_resolver=public_dns,
        now=NOW,
    )
    resolved = storage.get_job(job["id"])

    assert summary["provider_jobs_resolved"] == 1
    assert resolved["application_url"] == (
        "https://apply.workable.com/example/j/ABC/"
    )
    assert resolved["resolution_method"] == "provider_apply_link"


def test_ambiguous_provider_apply_links_require_manual_review(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="remotive",
            url="https://remotive.com/remote-jobs/software-dev/example-123",
        ),
        storage,
    )["job"]
    html = b"""
        <a href="https://jobs.example.com/one">Apply for this position</a>
        <a href="https://jobs.example.com/two">Apply for this position</a>
    """

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: FakeResponse(content=html),
        dns_resolver=public_dns,
        now=NOW,
    )

    assert summary["provider_jobs_ambiguous"] == 1
    assert storage.get_job(job["id"])["resolution_status"] == "manual_required"


def test_transient_provider_failure_sets_cooldown(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="adzuna",
            url="https://www.adzuna.com/details/123",
        ),
        storage,
    )["job"]
    calls = 0

    def failing_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("private detail")

    first = resolve_employer_sites(
        storage,
        destination_get=failing_get,
        dns_resolver=public_dns,
        now=NOW,
    )
    second = resolve_employer_sites(
        storage,
        destination_get=failing_get,
        dns_resolver=public_dns,
        now=NOW + timedelta(hours=1),
    )
    deferred = storage.get_job(job["id"])

    assert calls == 1
    assert first["provider_jobs_failed"] == 1
    assert first["errors"] == []
    assert second["provider_jobs_deferred"] == 1
    assert deferred["resolution_attempt_count"] == 1
    assert deferred["resolution_error_type"] == "EnrichmentRequestError"
    assert datetime.fromisoformat(deferred["resolution_next_attempt_at"]) == (
        NOW + timedelta(hours=6)
    )


def test_stream_failure_sets_provider_cooldown(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="adzuna",
            url="https://www.adzuna.com/details/123",
        ),
        storage,
    )["job"]
    response = FakeResponse()

    def failing_stream(chunk_size: int):
        raise ProtocolError("private detail")
        yield b""

    response.iter_content = failing_stream

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: response,
        dns_resolver=public_dns,
        now=NOW,
    )
    deferred = storage.get_job(job["id"])

    assert summary["provider_jobs_failed"] == 1
    assert summary["errors"] == []
    assert deferred["resolution_status"] == "pending"
    assert deferred["resolution_error_type"] == "EnrichmentRequestError"
    assert datetime.fromisoformat(deferred["resolution_next_attempt_at"]) == (
        NOW + timedelta(hours=6)
    )


def test_provider_apply_link_with_private_dns_is_not_resolved(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="remotive",
            url="https://remotive.com/remote-jobs/software-dev/example-123",
        ),
        storage,
    )["job"]
    html = b"""
        <a href="https://internal.example/jobs/1">Apply for this position</a>
    """

    def private_dns(hostname, *args, **kwargs):
        address = (
            "127.0.0.1"
            if hostname == "internal.example"
            else "93.184.216.34"
        )
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: FakeResponse(content=html),
        dns_resolver=private_dns,
        now=NOW,
    )

    assert summary["provider_jobs_dynamic_required"] == 1
    assert storage.get_job(job["id"])["application_url"] is None


def test_manual_provider_resolution_is_not_retried(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="remotive",
            url="https://remotive.com/remote-jobs/software-dev/example-123",
        ),
        storage,
    )["job"]
    storage.update_job_resolution(
        job["id"],
        status="manual_required",
        application_url=None,
        method="ambiguous_provider_apply_link",
        confidence=None,
    )

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: pytest.fail(
            "Manual resolution must not be retried"
        ),
        dns_resolver=public_dns,
        now=NOW,
    )

    assert summary["provider_jobs_attempted"] == 0
    assert summary["jobs_manual_required"] == 1


def test_provider_batch_selection_is_round_robin(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    sources = ("adzuna",) * 7 + ("himalayas", "remotive")
    for index, source in enumerate(sources):
        domain = {
            "adzuna": "www.adzuna.com/details",
            "himalayas": "himalayas.app/companies/example/jobs",
            "remotive": "remotive.com/remote-jobs/software-dev",
        }[source]
        ingest_job(
            posting(
                source=source,
                url=f"https://{domain}/{index}",
                company=f"Example {index}",
            ),
            storage,
        )
    hosts = []

    def failing_get(url: str, **kwargs):
        hosts.append(urlsplit(url).hostname)
        raise requests.Timeout("private detail")

    summary = resolve_employer_sites(
        storage,
        destination_get=failing_get,
        dns_resolver=public_dns,
        now=NOW,
        provider_batch_limit=5,
    )

    assert hosts == [
        "www.adzuna.com",
        "himalayas.app",
        "remotive.com",
        "www.adzuna.com",
        "www.adzuna.com",
    ]
    assert summary["provider_jobs_attempted"] == 5


def test_provider_access_denied_requires_dynamic_rendering(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    job = ingest_job(
        posting(
            source="himalayas",
            url="https://himalayas.app/companies/example/jobs/1",
        ),
        storage,
    )["job"]

    first = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: FakeResponse(status_code=403),
        dns_resolver=public_dns,
        now=NOW,
    )
    second = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: pytest.fail(
            "Dynamic pages must wait for the rendering milestone"
        ),
        dns_resolver=public_dns,
        now=NOW + timedelta(days=1),
    )

    assert first["provider_jobs_dynamic_required"] == 1
    assert second["provider_jobs_attempted"] == 0
    assert storage.get_job(job["id"])["resolution_error_type"] == (
        "DynamicPageRequired"
    )


def test_linkedin_and_indeed_destinations_are_never_fetched(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    for source, url, company in (
        (
            "linkedin_email",
            "https://www.linkedin.com/jobs/view/123",
            "LinkedIn Example",
        ),
        (
            "indeed_email",
            "https://www.indeed.com/viewjob?jk=abc",
            "Indeed Example",
        ),
    ):
        ingest_job(posting(source=source, url=url, company=company), storage)

    summary = resolve_employer_sites(
        storage,
        destination_get=lambda *args, **kwargs: pytest.fail(
            "Discovery provider must not be fetched"
        ),
        dns_resolver=public_dns,
        now=NOW,
    )

    assert summary["provider_jobs_attempted"] == 0
    assert summary["jobs_pending"] == 2


def test_schema_four_backfills_resolution_attempt_state(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 3}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://www.adzuna.com/details/123",
            "source": "adzuna",
            "resolution_status": "pending",
        }
    )
    database.close()

    storage = JobStorage(database_path)
    migrated = storage.list_jobs()[0]

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 4
    assert migrated["resolution_attempt_count"] == 0
    assert migrated["resolution_last_attempt_at"] is None
    assert migrated["resolution_next_attempt_at"] is None
    assert migrated["resolution_error_type"] is None
