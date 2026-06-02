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
    assert "Target role match: junior" in scored_job.reasons
    assert "Target tech match: react" in scored_job.reasons
    assert "Target tech match: typescript" in scored_job.reasons


def test_penalizes_senior_roles() -> None:
    job = make_job(
        title="Senior Software Architect",
        description="Requires 7+ years of experience leading teams.",
    )

    scored_job = score_job(job)

    assert scored_job.score < 0
    assert "Red flag: senior" in scored_job.red_flags
    assert "Red flag: architect" in scored_job.red_flags
    assert "Red flag: 7+ years" in scored_job.red_flags


def test_rewards_target_locations() -> None:
    job = make_job(
        title="Entry-Level Application Developer",
        location="Rochester, MN",
        description="Remote-friendly role supporting Minnesota teams.",
    )

    scored_job = score_job(job)

    assert scored_job.score > 0
    assert "Target location match: rochester" in scored_job.reasons
    assert "Target location match: remote" in scored_job.reasons
    assert "Target location match: minnesota" in scored_job.reasons


def test_contract_only_unpaid_role_gets_red_flags() -> None:
    job = make_job(
        title="Junior Software Developer",
        description="Contract only unpaid role for portfolio experience.",
    )

    scored_job = score_job(job)

    assert scored_job.red_flags == [
        "Red flag: contract only",
        "Red flag: unpaid",
    ]
