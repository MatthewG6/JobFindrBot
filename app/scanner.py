from datetime import UTC, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.models import JobPosting


HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"


def text_from_card(card: BeautifulSoup, class_name: str) -> str:
    element = card.select_one(f".{class_name}")
    if element is None:
        return ""
    return element.get_text(strip=True)


def link_from_card(card: BeautifulSoup) -> str:
    element = card.select_one(".job-link")
    if element is None:
        return ""
    return element.get("href", "").strip()


def parse_jobs_from_html_file(file_path: str) -> list[JobPosting]:
    html = Path(file_path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[JobPosting] = []

    for card in soup.select(".job-card"):
        title = text_from_card(card, "job-title")
        company = text_from_card(card, "job-company")
        location = text_from_card(card, "job-location")
        url = link_from_card(card)

        if not all([title, company, location, url]):
            continue

        jobs.append(
            JobPosting(
                title=title,
                company=company,
                location=location,
                url=url,
                source="html_fixture",
                description=text_from_card(card, "job-description"),
            )
        )

    return jobs


def parse_api_date(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    return None


def himalayas_location(job_data: dict) -> str:
    restrictions = job_data.get("locationRestrictions") or []
    location_names = [
        restriction.get("name", "")
        for restriction in restrictions
        if isinstance(restriction, dict) and restriction.get("name")
    ]

    if not location_names:
        return "Remote (Worldwide)"

    return f"Remote: {', '.join(location_names)}"


def parse_himalayas_jobs(payload: dict) -> list[JobPosting]:
    jobs: list[JobPosting] = []

    for job_data in payload.get("jobs", []):
        if not isinstance(job_data, dict):
            continue

        title = str(job_data.get("title") or "").strip()
        company = str(job_data.get("companyName") or "").strip()
        url = str(job_data.get("applicationLink") or "").strip()

        if not all([title, company, url]):
            continue

        description_html = str(job_data.get("description") or "")
        description = BeautifulSoup(description_html, "html.parser").get_text(
            " ",
            strip=True,
        )

        try:
            jobs.append(
                JobPosting(
                    title=title,
                    company=company,
                    location=himalayas_location(job_data),
                    url=url,
                    source="himalayas",
                    description=description,
                    posted_at=parse_api_date(job_data.get("pubDate")),
                )
            )
        except ValidationError:
            continue

    return jobs


def fetch_himalayas_jobs(
    query: str = "software engineer",
    country: str = "US",
    seniority: str = "Entry-level",
    page: int = 1,
    timeout: int = 15,
) -> list[JobPosting]:
    response = requests.get(
        HIMALAYAS_SEARCH_URL,
        params={
            "q": query,
            "country": country,
            "seniority": seniority,
            "sort": "recent",
            "page": page,
        },
        headers={"User-Agent": "JobRadarAssistant/0.1 (personal project)"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_himalayas_jobs(response.json())


def scan_jobs() -> list[JobPosting]:
    """Fake local scanner. Real job source integrations will be added later."""
    return [
        JobPosting(
            title="Junior Software Engineer",
            company="North Star Apps",
            location="Minneapolis, MN",
            url="https://example.com/jobs/north-star-junior-software-engineer",
            source="fake",
            description="Build Python APIs and React features for a small product team.",
        ),
        JobPosting(
            title="Senior Software Architect",
            company="Enterprise Example",
            location="Remote",
            url="https://example.com/jobs/enterprise-senior-architect",
            source="fake",
            description="Requires 7+ years of experience leading architecture decisions.",
        ),
    ]
