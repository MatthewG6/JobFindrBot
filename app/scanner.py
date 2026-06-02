from app.models import JobPosting


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
