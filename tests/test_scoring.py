from app.models import JobPosting
from app.scoring import score_job


def make_job(title: str, description: str = "", location: str = "Minneapolis, MN") -> JobPosting:
    return JobPosting(
        title=title,
        company="Example Company",
        location=location,
        url="https://example.com/job",
        source="test",
        description=description,
    )


def test_scores_junior_friendly_job_higher() -> None:
    job = make_job(
        title="Junior Software Engineer",
        description="Build React and TypeScript features for a cloud product.",
    )

    scored_job = score_job(job)

    assert scored_job.score > 0
    assert "Matches target keyword: junior" in scored_job.reasons


def test_penalizes_senior_roles() -> None:
    job = make_job(
        title="Senior Software Architect",
        description="Requires 7+ years of experience leading teams.",
    )

    scored_job = score_job(job)

    assert scored_job.score < 0
    assert "Penalized keyword: senior" in scored_job.reasons
