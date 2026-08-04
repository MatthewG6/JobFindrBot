from pathlib import Path

import pytest
from tinydb import TinyDB

from app.employer_resolver import (
    postings_match,
    resolve_employer_sites,
    set_manual_application_url,
)
from app.job_links import job_link_role
from app.ingestion import ingest_job
from app.models import JobPosting
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


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

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 2
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
