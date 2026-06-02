from pathlib import Path

from bs4 import BeautifulSoup

from app.models import JobPosting


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
