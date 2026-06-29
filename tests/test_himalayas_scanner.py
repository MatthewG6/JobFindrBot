import json
from pathlib import Path

import requests

from app.ingestion import ingest_job
from app.models import JobPosting
from app.scanner import fetch_himalayas_jobs, parse_himalayas_jobs
from app.storage import JobStorage


FIXTURE_PATH = Path("tests/fixtures/himalayas_jobs.json")


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_parse_himalayas_jobs_returns_valid_job_postings() -> None:
    jobs = parse_himalayas_jobs(load_fixture())

    assert len(jobs) == 2
    assert all(isinstance(job, JobPosting) for job in jobs)
    assert jobs[0].title == "Junior Software Engineer"
    assert jobs[0].company == "Example Remote Company"
    assert jobs[0].location == "Remote: United States"
    assert jobs[0].source == "himalayas"
    assert jobs[0].description == "Build React and TypeScript features."
    assert jobs[1].location == "Remote (Worldwide)"


def test_fetch_himalayas_jobs_uses_filtered_request(monkeypatch) -> None:
    request_details: dict = {}

    def fake_get(url: str, **kwargs) -> FakeResponse:
        request_details["url"] = url
        request_details.update(kwargs)
        return FakeResponse(load_fixture())

    monkeypatch.setattr("app.scanner.requests.get", fake_get)

    jobs = fetch_himalayas_jobs(timeout=7)

    assert len(jobs) == 2
    assert request_details["params"]["country"] == "US"
    assert request_details["params"]["seniority"] == "Entry-level"
    assert request_details["params"]["sort"] == "recent"
    assert request_details["timeout"] == 7


def test_himalayas_jobs_can_be_ingested(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    jobs = parse_himalayas_jobs(load_fixture())

    results = [ingest_job(job, storage) for job in jobs]
    saved_jobs = storage.list_jobs()

    assert all(result["created"] for result in results)
    assert len(saved_jobs) == 2
    assert all(job["source"] == "himalayas" for job in saved_jobs)
    assert all("fit_score" in job for job in saved_jobs)
    assert all("content_hash" in job for job in saved_jobs)


def test_fetch_himalayas_jobs_propagates_timeout(monkeypatch) -> None:
    def fake_get(*args, **kwargs):
        raise requests.Timeout("request timed out")

    monkeypatch.setattr("app.scanner.requests.get", fake_get)

    try:
        fetch_himalayas_jobs()
    except requests.Timeout as error:
        assert str(error) == "request timed out"
    else:
        raise AssertionError("Expected requests.Timeout")
