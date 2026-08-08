from datetime import UTC, datetime
import json
from pathlib import Path
import socket

import pytest
import requests
from tinydb import TinyDB

from app.employer_resolver import resolve_employer_sites, set_manual_application_url
from app.ingestion import ingest_job
from app.job_enrichment import (
    DynamicPageRequired,
    EnrichmentPayload,
    EnrichmentPayloadError,
    EnrichmentRequestError,
    MAX_RESPONSE_BYTES,
    enrich_resolved_jobs,
    greenhouse_payload,
    lever_payload,
    parse_job_posting_json_ld,
    pinned_https_get,
    request_json,
    request_static_html,
)
from app.models import JobPosting
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


NOW = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        content: bytes | None = None,
        status_code: int = 200,
        headers: dict | None = None,
    ) -> None:
        self.payload = payload
        self.content = (
            content
            if content is not None
            else json.dumps(payload).encode("utf-8")
        )
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = 300 <= status_code < 400
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("private response detail")

    def json(self):
        return self.payload

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


def job(
    *,
    url: str,
    source: str,
    description: str = "Short alert text.",
) -> JobPosting:
    return JobPosting(
        title="Junior Software Engineer",
        company="Example",
        location="Remote, United States",
        url=url,
        source=source,
        description=description,
    )


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


def test_duplicate_official_payload_enriches_without_network(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    provider = ingest_job(
        job(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]
    ingest_job(
        job(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
            description=(
                "Build React and TypeScript cloud services. "
                "Collaborate with an experienced product engineering team."
            ),
        ),
        storage,
    )
    resolve_employer_sites(storage)

    summary = enrich_resolved_jobs(
        storage,
        now=NOW,
        fetcher=lambda posting: (_ for _ in ()).throw(
            AssertionError("network should not be used")
        ),
    )
    enriched = storage.get_job(provider["id"])

    assert summary["jobs_enriched"] == 1
    assert enriched["enrichment_method"] == "captured_official_payload"
    assert "React and TypeScript" in enriched["description"]
    assert enriched["fit_score"] > provider["fit_score"]
    assert len(storage.list_job_source_records(provider["id"])) == 2


def test_greenhouse_adapter_fetches_one_matching_posting() -> None:
    request = {}

    def get(url: str, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return FakeResponse(
            {
                "id": 456,
                "title": "Junior Software Engineer",
                "content": "<p>Build reliable Python services.</p>",
            }
        )

    payload = greenhouse_payload(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "application_url": (
                "https://job-boards.greenhouse.io/example/jobs/456"
            ),
        },
        get=get,
    )

    assert request["url"].endswith("/boards/example/jobs/456")
    assert request["params"] == {"content": "true"}
    assert request["allow_redirects"] is False
    assert payload.description == "Build reliable Python services."
    assert payload.method == "greenhouse_api"


def test_greenhouse_adapter_rejects_wrong_posting_identity() -> None:
    with pytest.raises(EnrichmentPayloadError, match="wrong posting"):
        greenhouse_payload(
            {
                "title": "Junior Software Engineer",
                "company": "Example",
                "application_url": (
                    "https://job-boards.greenhouse.io/example/jobs/456"
                ),
            },
            get=lambda *args, **kwargs: FakeResponse(
                {
                    "id": 999,
                    "title": "Junior Software Engineer",
                    "content": "Build reliable Python services.",
                }
            ),
        )


def test_api_fetch_bounds_streamed_response_and_closes_it() -> None:
    response = FakeResponse(content=b"x" * (MAX_RESPONSE_BYTES + 1))

    with pytest.raises(EnrichmentRequestError, match="too large"):
        request_json(
            "https://api.example.com/posting",
            get=lambda *args, **kwargs: response,
        )

    assert response.closed is True


def test_lever_adapter_extracts_requirements_salary_and_apply_url() -> None:
    def get(url: str, **kwargs):
        return FakeResponse(
            {
                "id": "abc-123",
                "text": "Junior Software Engineer",
                "descriptionPlain": "Build reliable services.",
                "lists": [
                    {
                        "text": "Requirements",
                        "content": "<li>Python</li><li>TypeScript</li>",
                    }
                ],
                "additionalPlain": "Equal opportunity employer.",
                "categories": {"commitment": "Full-time"},
                "workplaceType": "remote",
                "salaryRange": {
                    "currency": "USD",
                    "interval": "year",
                    "min": 70000,
                    "max": 90000,
                },
                "applyUrl": "https://jobs.lever.co/example/abc-123/apply",
            }
        )

    payload = lever_payload(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "application_url": "https://jobs.lever.co/example/abc-123",
        },
        get=get,
    )

    assert "Python TypeScript" in payload.description
    assert payload.salary_text == "$70,000 - $90,000 year"
    assert payload.employment_type == "Full-time"
    assert payload.workplace_type == "remote"
    assert payload.apply_url.endswith("/apply")


def test_lever_malformed_lists_are_a_terminal_payload_error() -> None:
    with pytest.raises(EnrichmentPayloadError, match="lists are invalid"):
        lever_payload(
            {
                "title": "Junior Software Engineer",
                "company": "Example",
                "application_url": "https://jobs.lever.co/example/abc-123",
            },
            get=lambda *args, **kwargs: FakeResponse(
                {
                    "id": "abc-123",
                    "text": "Junior Software Engineer",
                    "descriptionPlain": "Build reliable services.",
                    "lists": None,
                }
            ),
        )


def test_json_ld_parser_requires_one_matching_company_and_title() -> None:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Junior Software Engineer",
        "hiringOrganization": {"name": "Example, Inc."},
        "description": "<p>Build React applications and Python APIs.</p>",
        "employmentType": ["FULL_TIME", "PERMANENT"],
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 80000,
                "maxValue": 100000,
                "unitText": "YEAR",
            },
        },
        "url": "https://careers.example.com/jobs/456",
    }
    html = (
        '<script type="application/ld+json">'
        + json.dumps({"@graph": [posting]})
        + "</script>"
    )

    payload = parse_job_posting_json_ld(
        html,
        {
            "title": "Junior Software Engineer",
            "company": "Example",
        },
        "https://careers.example.com/jobs/456",
    )

    assert payload.description == "Build React applications and Python APIs."
    assert payload.salary_text == "$80,000 - $100,000 YEAR"
    assert payload.employment_type == "FULL_TIME, PERMANENT"

    posting["hiringOrganization"] = {"name": "Different Company"}
    mismatched = (
        '<script type="application/ld+json">'
        + json.dumps(posting)
        + "</script>"
    )
    with pytest.raises(DynamicPageRequired):
        parse_job_posting_json_ld(
            mismatched,
            {"title": "Junior Software Engineer", "company": "Example"},
            "https://careers.example.com/jobs/456",
        )


def test_json_ld_parser_rejects_ambiguous_matching_postings() -> None:
    posting = {
        "@type": "JobPosting",
        "title": "Junior Software Engineer",
        "hiringOrganization": {"name": "Example"},
        "description": "Build reliable Python services for customers.",
    }
    html = (
        '<script type="application/ld+json">'
        + json.dumps([posting, posting])
        + "</script>"
    )

    with pytest.raises(DynamicPageRequired, match="no unique"):
        parse_job_posting_json_ld(
            html,
            {"title": "Junior Software Engineer", "company": "Example"},
            "https://careers.example.com/jobs/456",
        )


def test_static_fetch_rejects_private_dns_before_request() -> None:
    calls = 0

    def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("request must not run")

    def private_dns(*args, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]

    with pytest.raises(EnrichmentRequestError, match="not public"):
        request_static_html(
            "https://careers.example.com/jobs/456",
            get=get,
            resolver=private_dns,
        )
    assert calls == 0


def test_static_fetch_validates_redirect_and_bounds_response() -> None:
    responses = [
        FakeResponse(status_code=302, headers={"Location": "/jobs/final"}),
        FakeResponse(
            content=b"<html>ok</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        ),
    ]

    html, final_url = request_static_html(
        "https://careers.example.com/jobs/456",
        get=lambda *args, **kwargs: responses.pop(0),
        resolver=public_dns,
    )

    assert html == "<html>ok</html>"
    assert final_url == "https://careers.example.com/jobs/final"


def test_static_fetch_revalidates_redirect_dns_and_closes_response() -> None:
    redirect = FakeResponse(
        status_code=302,
        headers={"Location": "https://private.example/jobs/final"},
    )

    def resolver(hostname: str, *args, **kwargs):
        address = "127.0.0.1" if hostname == "private.example" else "93.184.216.34"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    with pytest.raises(EnrichmentRequestError, match="not public"):
        request_static_html(
            "https://careers.example.com/jobs/456",
            get=lambda *args, **kwargs: redirect,
            resolver=resolver,
        )

    assert redirect.closed is True


def test_pinned_https_transport_connects_to_validated_ip_with_tls_hostname() -> None:
    captured = {}

    class RawResponse:
        status = 200
        headers = {"Content-Type": "text/html"}

        def stream(self, chunk_size: int):
            yield b"<html>ok</html>"

        def release_conn(self) -> None:
            captured["released"] = True

    class Pool:
        def __init__(self, host: str, **kwargs) -> None:
            captured["host"] = host
            captured["pool_kwargs"] = kwargs

        def urlopen(self, method: str, target: str, **kwargs):
            captured["method"] = method
            captured["target"] = target
            captured["request_kwargs"] = kwargs
            return RawResponse()

        def close(self) -> None:
            captured["pool_closed"] = True

    response = pinned_https_get(
        "https://careers.example.com/jobs/456?source=email",
        ("93.184.216.34",),
        timeout=20,
        pool_factory=Pool,
    )
    content = b"".join(response.iter_content(1024))
    response.close()

    assert captured["host"] == "93.184.216.34"
    assert captured["pool_kwargs"]["server_hostname"] == (
        "careers.example.com"
    )
    assert captured["pool_kwargs"]["assert_hostname"] == (
        "careers.example.com"
    )
    assert captured["pool_kwargs"]["ca_certs"]
    assert captured["request_kwargs"]["headers"]["Host"] == (
        "careers.example.com"
    )
    assert captured["target"] == "/jobs/456?source=email"
    assert content == b"<html>ok</html>"
    assert captured["released"] is True
    assert captured["pool_closed"] is True


def test_enrichment_runner_marks_dynamic_and_defers_failures(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    dynamic = ingest_job(
        job(
            source="manual",
            url="https://careers.example.com/jobs/dynamic",
            description="",
        ),
        storage,
    )["job"]
    failed = ingest_job(
        JobPosting(
            title="Junior Python Developer",
            company="Second Example",
            location="Remote, United States",
            url="https://careers.second.example/jobs/failed",
            source="manual",
            description="",
        ),
        storage,
    )["job"]

    def fetcher(posting: dict) -> EnrichmentPayload:
        if posting["id"] == dynamic["id"]:
            raise DynamicPageRequired("dynamic")
        raise EnrichmentRequestError("failed")

    first = enrich_resolved_jobs(storage, now=NOW, fetcher=fetcher)
    second = enrich_resolved_jobs(storage, now=NOW, fetcher=fetcher)

    assert first["jobs_dynamic_required"] == 1
    assert first["jobs_failed"] == 1
    assert second["jobs_attempted"] == 0
    assert second["jobs_deferred"] == 1
    assert second["jobs_already_dynamic_required"] == 1
    assert storage.get_job(dynamic["id"])["enrichment_status"] == (
        "dynamic_required"
    )
    assert storage.get_job(failed["id"])["enrichment_status"] == "failed"


def test_enrichment_runner_honors_batch_cap_and_malformed_retry(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    first = ingest_job(
        job(
            source="manual",
            url="https://careers.example.com/jobs/first",
            description="",
        ),
        storage,
    )["job"]
    second = ingest_job(
        JobPosting(
            title="Junior Python Developer",
            company="Second Example",
            location="Remote, United States",
            url="https://careers.second.example/jobs/second",
            source="manual",
            description="",
        ),
        storage,
    )["job"]
    storage.update_job_enrichment(
        first["id"],
        {"enrichment_next_attempt_at": "not-a-timestamp"},
    )
    attempted = []

    def fetcher(posting: dict) -> EnrichmentPayload:
        attempted.append(posting["id"])
        return EnrichmentPayload(
            description="Build reliable Python services for customers.",
            method="test",
            source_url=posting["application_url"],
        )

    summary = enrich_resolved_jobs(
        storage,
        now=NOW,
        max_jobs=1,
        fetcher=fetcher,
    )

    assert summary["jobs_attempted"] == 1
    assert summary["jobs_enriched"] == 1
    assert summary["jobs_deferred"] == 1
    assert attempted == [first["id"]]
    assert storage.get_job(second["id"])["enrichment_status"] == "pending"


def test_dynamic_job_recovers_from_later_official_source_payload(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        job(
            source="manual",
            url="https://careers.example.com/jobs/456",
            description="",
        ),
        storage,
    )["job"]
    first = enrich_resolved_jobs(
        storage,
        now=NOW,
        fetcher=lambda posting: (_ for _ in ()).throw(
            DynamicPageRequired("dynamic")
        ),
    )
    ingest_job(
        job(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
            description=(
                "Build reliable Python and TypeScript services for customers."
            ),
        ),
        storage,
    )

    second = enrich_resolved_jobs(
        storage,
        now=NOW,
        fetcher=lambda posting: (_ for _ in ()).throw(
            AssertionError("network should not be used")
        ),
    )

    assert first["jobs_dynamic_required"] == 1
    assert second["jobs_enriched"] == 1
    assert storage.get_job(saved["id"])["enrichment_status"] == "enriched"


def test_terminal_payload_error_requires_manual_review_without_retry(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        job(
            source="manual",
            url="https://careers.example.com/jobs/terminal",
            description="",
        ),
        storage,
    )["job"]
    attempts = 0

    def fetcher(posting: dict) -> EnrichmentPayload:
        nonlocal attempts
        attempts += 1
        raise EnrichmentPayloadError("identity mismatch")

    first = enrich_resolved_jobs(storage, now=NOW, fetcher=fetcher)
    second = enrich_resolved_jobs(
        storage,
        now=NOW.replace(day=6),
        fetcher=fetcher,
    )

    assert attempts == 1
    assert first["jobs_manual_required"] == 1
    assert first["errors"] == []
    assert second["jobs_already_manual_required"] == 1
    assert storage.get_job(saved["id"])["enrichment_status"] == (
        "manual_required"
    )


def test_manual_url_correction_resets_terminal_enrichment_for_retry(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        job(
            source="manual",
            url="https://careers.example.com/jobs/wrong",
            description="",
        ),
        storage,
    )["job"]
    enrich_resolved_jobs(
        storage,
        now=NOW,
        fetcher=lambda posting: (_ for _ in ()).throw(
            EnrichmentPayloadError("identity mismatch")
        ),
    )
    corrected = set_manual_application_url(
        storage,
        saved["id"],
        "https://careers.example.com/jobs/corrected",
    )
    calls = []

    summary = enrich_resolved_jobs(
        storage,
        now=NOW,
        fetcher=lambda posting: calls.append(posting["application_url"])
        or EnrichmentPayload(
            description="Build reliable Python services for customers.",
            method="test",
            source_url=posting["application_url"],
        ),
    )

    assert corrected["enrichment_status"] == "pending"
    assert corrected["enrichment_error_type"] is None
    assert calls == ["https://careers.example.com/jobs/corrected"]
    assert summary["jobs_enriched"] == 1


def test_source_snapshot_preserves_richer_duplicate_capture(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    rich_description = "Build reliable Python services for enterprise customers."
    saved = ingest_job(
        job(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
            description=rich_description,
        ),
        storage,
    )["job"]
    ingest_job(
        job(
            source="greenhouse",
            url="https://job-boards.greenhouse.io/example/jobs/456",
            description="",
        ),
        storage,
    )

    record = storage.list_job_source_records(saved["id"])[0]

    assert record["posting"]["description"] == rich_description


def test_duplicate_provenance_writes_are_atomic(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    storage = JobStorage(database_path)
    saved = ingest_job(
        job(
            source="linkedin_email",
            url="https://www.linkedin.com/jobs/view/123",
        ),
        storage,
    )["job"]
    links_before = storage.list_job_links(saved["id"])
    records_before = storage.list_job_source_records(saved["id"])

    class FailingStorage(JobStorage):
        def _register_source_record(self, *args, **kwargs) -> None:
            raise RuntimeError("source record failed")

    failing = FailingStorage(database_path)
    with pytest.raises(RuntimeError, match="source record failed"):
        ingest_job(
            job(
                source="greenhouse",
                url="https://job-boards.greenhouse.io/example/jobs/456",
                description="Build reliable Python services for customers.",
            ),
            failing,
        )

    reloaded = JobStorage(database_path)
    assert reloaded.list_job_links(saved["id"]) == links_before
    assert reloaded.list_job_source_records(saved["id"]) == records_before


def test_schema_three_backfills_source_records(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 2}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://jobs.lever.co/example/abc",
            "source": "lever",
            "description": "Build reliable services.",
            "resolution_status": "resolved",
            "application_url": "https://jobs.lever.co/example/abc",
        }
    )
    database.close()

    storage = JobStorage(database_path)
    saved = storage.list_jobs()[0]

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 5
    assert saved["enrichment_status"] == "enriched"
    assert len(storage.list_job_source_records(saved["id"])) == 1


def test_schema_three_does_not_treat_placeholder_text_as_enriched(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 2}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://jobs.lever.co/example/abc",
            "source": "lever",
            "description": "N/A",
            "resolution_status": "resolved",
            "application_url": "https://jobs.lever.co/example/abc",
        }
    )
    database.close()

    storage = JobStorage(database_path)

    assert storage.list_jobs()[0]["enrichment_status"] == "pending"


def test_failed_schema_three_migration_leaves_schema_two_unchanged(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 2}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://jobs.lever.co/example/abc",
            "source": "lever",
            "description": "Build reliable services.",
        }
    )
    database.close()
    original = database_path.read_bytes()

    class FailingStorage(JobStorage):
        def _register_source_record(self, *args, **kwargs) -> None:
            super()._register_source_record(*args, **kwargs)
            raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        FailingStorage(database_path)

    assert database_path.read_bytes() == original
