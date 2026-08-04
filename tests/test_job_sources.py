import json
from pathlib import Path

import pytest
import requests

from app.job_sources import (
    ADZUNA_API_URL,
    GREENHOUSE_API_ROOT,
    LEVER_API_ROOT,
    REMOTIVE_API_URL,
    USAJOBS_API_URL,
    EmployerWatchlistError,
    SourceRequestError,
    fetch_adzuna_jobs,
    fetch_employer_board,
    fetch_remotive_jobs,
    fetch_usajobs_jobs,
    load_employer_watchlist,
    parse_adzuna_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_usajobs_jobs,
)
from app.models import JobPosting


FIXTURES = Path("tests/fixtures")


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.is_redirect = 300 <= status_code < 400

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_provider_parsers_normalize_jobs_and_reject_bad_records() -> None:
    remotive = parse_remotive_jobs(fixture("remotive_jobs.json"))
    adzuna = parse_adzuna_jobs(fixture("adzuna_jobs.json"))
    usajobs = parse_usajobs_jobs(fixture("usajobs_jobs.json"))
    greenhouse = parse_greenhouse_jobs(
        fixture("greenhouse_jobs.json"), "Example Company", "example"
    )
    lever = parse_lever_jobs(fixture("lever_jobs.json"), "Example Company")

    assert all(
        isinstance(job, JobPosting)
        for job in remotive + adzuna + usajobs + greenhouse + lever
    )
    assert len(remotive) == 1
    assert remotive[0].description == "Build & test Python services."
    assert remotive[0].location == "Remote: United States"
    assert adzuna[0].salary_text == "$65,000 - $85,000 a year"
    assert str(adzuna[0].url).startswith("https://www.adzuna.com/")
    assert usajobs[0].source_job_id == "USA-1"
    assert usajobs[0].description == "Develop software for public services."
    assert len(greenhouse) == 1
    assert greenhouse[0].title == "Frontend Engineer"
    assert len(lever) == 1
    assert lever[0].posted_at is not None


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (
            parse_adzuna_jobs,
            {
                "results": [
                    {
                        "title": "Software Engineer",
                        "company": {"display_name": "Example"},
                        "location": {},
                        "redirect_url": "https://adzuna.com.evil.example/job",
                    }
                ]
            },
        ),
        (
            parse_usajobs_jobs,
            {
                "SearchResult": {
                    "SearchResultItems": [
                        {
                            "MatchedObjectDescriptor": {
                                "PositionTitle": "Software Engineer",
                                "PositionURI": "http://www.usajobs.gov/job/1",
                            }
                        }
                    ]
                }
            },
        ),
    ],
)
def test_provider_parsers_reject_untrusted_urls(parser, payload) -> None:
    assert parser(payload) == []


def test_employer_parsers_filter_non_target_locations() -> None:
    greenhouse = {
        "jobs": [
            {
                "id": 1,
                "title": "Software Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
                "location": {"name": "Paris, France"},
            }
        ]
    }
    lever = [
        {
            "id": "1",
            "text": "Software Engineer",
            "hostedUrl": "https://jobs.lever.co/example/1",
            "categories": {"location": "London, UK"},
        }
    ]

    assert parse_greenhouse_jobs(greenhouse, "Example", "example") == []
    assert parse_lever_jobs(lever, "Example") == []


def test_malformed_optional_values_do_not_crash_parsers() -> None:
    usajobs = fixture("usajobs_jobs.json")
    descriptor = usajobs["SearchResult"]["SearchResultItems"][0][
        "MatchedObjectDescriptor"
    ]
    descriptor["PositionRemuneration"] = {"unexpected": "shape"}
    lever = fixture("lever_jobs.json")
    lever[0]["createdAt"] = 10**100

    assert len(parse_usajobs_jobs(usajobs)) == 1
    assert len(parse_lever_jobs(lever, "Example")) == 1
    assert parse_lever_jobs(lever, "Example")[0].posted_at is None


@pytest.mark.parametrize(
    ("fetcher", "expected_url", "payload", "kwargs"),
    [
        (
            fetch_remotive_jobs,
            REMOTIVE_API_URL,
            {"jobs": []},
            {"timeout": 7, "limit": 12},
        ),
        (
            fetch_adzuna_jobs,
            ADZUNA_API_URL,
            {"results": []},
            {"app_id": "id", "app_key": "key", "timeout": 7},
        ),
        (
            fetch_usajobs_jobs,
            USAJOBS_API_URL,
            {"SearchResult": {"SearchResultItems": []}},
            {"api_key": "key", "user_agent": "owner@example.com", "timeout": 7},
        ),
    ],
)
def test_provider_fetchers_use_official_endpoints(
    monkeypatch, fetcher, expected_url, payload, kwargs
) -> None:
    request = {}

    def fake_get(url: str, **request_kwargs):
        request["url"] = url
        request.update(request_kwargs)
        return FakeResponse(payload)

    monkeypatch.setattr("app.job_sources.requests.get", fake_get)

    assert fetcher(**kwargs) == []
    assert request["url"] == expected_url
    assert request["timeout"] == 7
    assert request["allow_redirects"] is False
    assert request["headers"]["User-Agent"]
    if fetcher is fetch_adzuna_jobs:
        assert request["params"]["app_id"] == "id"
        assert request["params"]["app_key"] == "key"
    if fetcher is fetch_usajobs_jobs:
        assert request["headers"]["Authorization-Key"] == "key"


def test_fetcher_rejects_invalid_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.job_sources.requests.get", lambda *args, **kwargs: FakeResponse([])
    )

    with pytest.raises(ValueError):
        fetch_remotive_jobs()


def test_usajobs_fetcher_rejects_malformed_nested_items(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.job_sources.requests.get",
        lambda *args, **kwargs: FakeResponse(
            {"SearchResult": {"SearchResultItems": {}}}
        ),
    )

    with pytest.raises(ValueError, match="USAJOBS"):
        fetch_usajobs_jobs("key", "owner@example.com")


def test_fetcher_propagates_timeout(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise requests.Timeout("private network detail")

    monkeypatch.setattr("app.job_sources.requests.get", timeout)

    with pytest.raises(SourceRequestError, match="Timeout") as error:
        fetch_remotive_jobs()
    assert "private network detail" not in str(error.value)


def test_adzuna_http_error_does_not_expose_credentials(monkeypatch) -> None:
    class UnauthorizedResponse(FakeResponse):
        def raise_for_status(self) -> None:
            raise requests.HTTPError(
                "401 for https://api.adzuna.com/search?app_key=private-key"
            )

    monkeypatch.setattr(
        "app.job_sources.requests.get",
        lambda *args, **kwargs: UnauthorizedResponse({}),
    )

    with pytest.raises(SourceRequestError) as error:
        fetch_adzuna_jobs("private-id", "private-key")

    assert "private-id" not in str(error.value)
    assert "private-key" not in str(error.value)


def test_watchlist_validates_entries_and_duplicates(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            [
                {"provider": "greenhouse", "site": "example", "company": "Example"},
                {"provider": "lever", "site": "example", "company": "Example"},
            ]
        ),
        encoding="utf-8",
    )
    assert len(load_employer_watchlist(valid)) == 2

    for entries in (
        [{"provider": "lever", "site": "../secret", "company": "Example"}],
        [
            {"provider": "lever", "site": "same", "company": "One"},
            {"provider": "lever", "site": "SAME", "company": "Two"},
        ],
    ):
        invalid = tmp_path / "invalid.json"
        invalid.write_text(json.dumps(entries), encoding="utf-8")
        with pytest.raises(EmployerWatchlistError):
            load_employer_watchlist(invalid)


@pytest.mark.parametrize(
    ("entry", "expected_url", "payload"),
    [
        (
            {"provider": "greenhouse", "site": "example", "company": "Example"},
            f"{GREENHOUSE_API_ROOT}/example/jobs",
            {"jobs": []},
        ),
        (
            {"provider": "lever", "site": "example", "company": "Example"},
            f"{LEVER_API_ROOT}/example",
            [],
        ),
    ],
)
def test_employer_board_fetch_uses_public_provider_api(
    monkeypatch, entry, expected_url, payload
) -> None:
    request = {}

    def fake_get(url: str, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return FakeResponse(payload)

    monkeypatch.setattr("app.job_sources.requests.get", fake_get)

    assert fetch_employer_board(entry, timeout=9) == []
    assert request["url"] == expected_url
    assert request["timeout"] == 9
    assert request["allow_redirects"] is False


def test_employer_board_fetch_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError):
        fetch_employer_board(
            {"provider": "unknown", "site": "example", "company": "Example"}
        )


def test_adzuna_http_link_is_safely_upgraded() -> None:
    payload = fixture("adzuna_jobs.json")
    payload["results"][0]["redirect_url"] = "http://www.adzuna.com/details/adz-1"

    jobs = parse_adzuna_jobs(payload)

    assert str(jobs[0].url).startswith("https://www.adzuna.com/")


def test_greenhouse_custom_link_uses_canonical_board_url() -> None:
    payload = fixture("greenhouse_jobs.json")
    payload["jobs"][0]["absolute_url"] = "https://careers.example.com/jobs/201"

    jobs = parse_greenhouse_jobs(payload, "Example", "example")

    assert len(jobs) == 1
    assert str(jobs[0].url) == (
        "https://job-boards.greenhouse.io/example/jobs/201"
    )


def test_provider_redirect_is_rejected_without_forwarding_secrets(
    monkeypatch,
) -> None:
    request = {}

    def redirect(url: str, **kwargs):
        request.update(kwargs)
        return FakeResponse({}, status_code=302)

    monkeypatch.setattr("app.job_sources.requests.get", redirect)

    with pytest.raises(SourceRequestError, match="redirected"):
        fetch_usajobs_jobs("private-key", "owner@example.com")
    assert request["allow_redirects"] is False


def test_parser_reports_rejected_and_filtered_records() -> None:
    remotive = parse_remotive_jobs(fixture("remotive_jobs.json"))
    greenhouse = parse_greenhouse_jobs(
        fixture("greenhouse_jobs.json"), "Example", "example"
    )

    assert remotive.records_received == 3
    assert remotive.records_rejected == 2
    assert remotive.records_filtered == 0
    assert greenhouse.records_received == 2
    assert greenhouse.records_filtered == 1
    assert greenhouse.records_rejected == 0


def test_malformed_text_fields_are_rejected_not_filtered() -> None:
    remotive = {
        "jobs": [
            {
                "id": 1,
                "title": {"software": "engineer"},
                "company_name": ["Example"],
                "url": "https://remotive.com/remote-jobs/1",
            }
        ]
    }
    greenhouse = {
        "jobs": [
            {
                "id": 1,
                "title": "Software Engineer",
                "location": {"name": {"country": "US"}},
            }
        ]
    }
    lever = [
        {
            "id": "1",
            "text": "Software Engineer",
            "hostedUrl": "https://jobs.lever.co/example/1",
            "categories": ["Remote"],
        }
    ]

    remotive_jobs = parse_remotive_jobs(remotive)
    greenhouse_jobs = parse_greenhouse_jobs(
        greenhouse,
        "Example",
        "example",
    )
    lever_jobs = parse_lever_jobs(lever, "Example")

    for jobs in (remotive_jobs, greenhouse_jobs, lever_jobs):
        assert jobs == []
        assert jobs.records_received == 1
        assert jobs.records_filtered == 0
        assert jobs.records_rejected == 1
