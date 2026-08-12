from pathlib import Path

import pytest

from app.candidate_profile import ScoringThresholds, load_candidate_profile
from app.models import JobPosting
from app.scoring import (
    MAX_RESCORE_BATCH,
    SCORING_VERSION,
    rescore_stale_jobs,
    score_job,
)
from app.storage import JobStorage


PROFILE = load_candidate_profile(Path("config/candidate_profile.example.yaml"))


def make_job(
    title: str,
    description: str = "",
    location: str = "Minneapolis, MN",
) -> JobPosting:
    return JobPosting(
        title=title,
        company="Example Company",
        location=location,
        url="https://example.com/job",
        source="test",
        description=description,
    )


def test_strong_match_has_normalized_dimensions_and_evidence() -> None:
    scored = score_job(
        make_job(
            "Junior Software Engineer",
            "Build React and TypeScript features for a cloud product.",
        ),
        PROFILE,
    )

    assert scored.score == 100
    assert 0 <= scored.confidence <= 100
    assert scored.confidence_band == "medium"
    assert scored.scoring_version == SCORING_VERSION == 2
    assert scored.review_threshold == PROFILE.thresholds.review
    assert [item.name for item in scored.dimensions] == [
        "role",
        "seniority",
        "skills",
        "location",
        "risk",
    ]
    assert sum(item.weight for item in scored.dimensions) == 100
    assert "Target role match: software engineer" in scored.reasons
    assert "Preferred seniority match: junior" in scored.reasons
    assert "Target tech match: react" in scored.reasons
    assert any(
        item.dimension == "skills"
        and item.signal == "typescript"
        and item.source == "description"
        for item in scored.evidence
    )


def test_excluded_seniority_caps_score_below_review_threshold() -> None:
    scored = score_job(
        make_job(
            "Senior Software Architect",
            "Requires 7+ years of experience leading teams.",
        ),
        PROFILE,
    )

    assert 0 <= scored.score <= 39
    assert next(
        item.score for item in scored.dimensions if item.name == "seniority"
    ) == 0
    assert scored.red_flags == [
        "Red flag: architect",
        "Red flag: senior",
        "Red flag: 7+ years",
    ]


@pytest.mark.parametrize(
    "requirement",
    [
        "Requires 5 years of experience.",
        "Requires 5 + years of experience.",
        "Requires five years of experience.",
        "Requires 5-year experience.",
        "Requires 5 yrs. experience.",
        "Requires 5+ yrs. of experience.",
    ],
)
def test_common_experience_requirement_variants_are_excluded(
    requirement: str,
) -> None:
    scored = score_job(
        make_job(
            "Software Engineer",
            f"{requirement} Build React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score <= 39
    assert "Red flag: 5+ years" in scored.red_flags


def test_unrelated_duration_is_not_an_experience_exclusion() -> None:
    scored = score_job(
        make_job(
            "Software Engineer",
            "Our product roadmap spans 5 years. Build React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score >= PROFILE.thresholds.strong
    assert "Red flag: 5+ years" not in scored.red_flags


def test_required_product_duration_is_not_experience() -> None:
    scored = score_job(
        make_job(
            "Software Engineer",
            "Role requires ownership of a 5-year product roadmap. "
            "Build React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score >= PROFILE.thresholds.strong
    assert "Red flag: 5+ years" not in scored.red_flags


def test_abbreviated_duration_does_not_merge_separate_sentences() -> None:
    scored = score_job(
        make_job(
            "Software Engineer",
            "Our product roadmap spans 5 yrs. Experience with React "
            "TypeScript cloud systems is helpful.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score >= PROFILE.thresholds.strong
    assert "Red flag: 5+ years" not in scored.red_flags


@pytest.mark.parametrize(
    "requirement",
    [
        (
            "Experience designing, building, operating, maintaining, "
            "monitoring, troubleshooting, securing, documenting, and scaling "
            "React TypeScript cloud platforms requires 5 years in software "
            "engineering."
        ),
        (
            "Requires 5 years designing, building, operating, maintaining, "
            "monitoring, troubleshooting, securing, documenting, and scaling "
            "cloud platforms with professional software engineering experience."
        ),
    ],
)
def test_long_experience_sentences_are_excluded(requirement: str) -> None:
    scored = score_job(
        make_job(
            "Software Engineer",
            f"{requirement} Build React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score <= 39
    assert "Red flag: 5+ years" in scored.red_flags


def test_seniority_alias_and_role_linked_description_are_excluded() -> None:
    abbreviated = score_job(
        make_job(
            "Sr. Software Engineer",
            "Build React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )
    description_only = score_job(
        make_job(
            "Engineer",
            "This Senior Software Engineer builds React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert abbreviated.score <= 39
    assert "Red flag: senior" in abbreviated.red_flags
    assert description_only.score <= 39
    assert "Red flag: senior" in description_only.red_flags


@pytest.mark.parametrize(
    "description",
    [
        "This Senior-level Software Engineer builds React TypeScript cloud systems.",
        "This Senior Backend Software Engineer builds React TypeScript cloud systems.",
        "This Sr. Software Engineer builds React TypeScript cloud systems.",
    ],
)
def test_role_linked_seniority_variants_are_excluded(
    description: str,
) -> None:
    scored = score_job(
        make_job("Engineer", description, location="Remote"),
        PROFILE,
    )

    assert scored.score <= 39
    assert "Red flag: senior" in scored.red_flags


def test_role_linked_seniority_does_not_cross_sentence_boundaries() -> None:
    scored = score_job(
        make_job(
            "Engineer",
            "A senior leader approves designs. Software Engineer builds "
            "React TypeScript cloud systems.",
            location="Remote",
        ),
        PROFILE,
    )

    assert scored.score >= PROFILE.thresholds.strong
    assert "Red flag: senior" not in scored.red_flags


def test_missing_role_and_explicit_risk_each_cap_reviewability() -> None:
    no_role = score_job(
        make_job("Business Analyst", "React TypeScript cloud reporting."),
        PROFILE,
    )
    risky = score_job(
        make_job(
            "Junior Software Developer",
            "Contract only unpaid role for portfolio experience.",
        ),
        PROFILE,
    )

    assert no_role.score <= 39
    assert risky.score <= 39
    assert risky.red_flags == [
        "Red flag: contract only",
        "Red flag: unpaid",
    ]


def test_phrase_boundaries_prevent_partial_word_red_flags() -> None:
    scored = score_job(
        make_job(
            "Junior Software Engineer",
            "Build React tools while developing leadership skills.",
        ),
        PROFILE,
    )

    assert "Red flag: lead" not in scored.red_flags


def test_title_level_seniority_terms_in_description_do_not_disqualify() -> None:
    scored = score_job(
        make_job(
            "Junior Software Engineer",
            "Lead feature delivery and partner with the hiring manager.",
        ),
        PROFILE,
    )

    assert "Red flag: lead" not in scored.red_flags
    assert "Red flag: manager" not in scored.red_flags
    assert scored.score >= PROFILE.thresholds.review


def test_aliases_do_not_accumulate_dimension_points() -> None:
    hyphenated = score_job(
        make_job("Junior Full-Stack Software Engineer"),
        PROFILE,
    )
    spaced = score_job(
        make_job("Junior Full Stack Software Engineer"),
        PROFILE,
    )

    assert hyphenated.score == spaced.score
    assert len(
        [item for item in hyphenated.evidence if item.dimension == "role"]
    ) == 2

    frontend = score_job(
        make_job("Junior Frontend Front-end Software Engineer"),
        PROFILE,
    )
    assert len(
        [item for item in frontend.evidence if item.dimension == "role"]
    ) == 2


def test_hard_cap_tracks_configured_review_threshold() -> None:
    profile = PROFILE.model_copy(
        update={"thresholds": ScoringThresholds(review=20, strong=75)}
    )

    scored = score_job(make_job("Business Analyst"), profile)

    assert scored.score <= 19


def test_sparse_posting_has_low_confidence_independent_of_fit() -> None:
    scored = score_job(
        make_job(
            "Junior Software Engineer",
            location="Unknown",
        ),
        PROFILE,
    )

    assert scored.score >= PROFILE.thresholds.review
    assert scored.confidence < 50
    assert scored.confidence_band == "low"


def test_rescore_stale_jobs_upgrades_once_and_persists_contract(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = storage.save_job(
        {
            **make_job(
                "Junior Software Engineer",
                "Build React, TypeScript, and cloud features.",
                location="Remote",
            ).model_dump(mode="json"),
            "fit_score": 4,
            "score_reasons": ["legacy"],
            "red_flags": [],
        }
    )

    first = rescore_stale_jobs(storage, profile=PROFILE)
    rescored = storage.get_job(saved["id"])
    second = rescore_stale_jobs(storage, profile=PROFILE)

    assert first == {
        "jobs_considered": 1,
        "jobs_eligible": 1,
        "jobs_attempted": 1,
        "jobs_rescored": 1,
        "jobs_notification_baselined": 0,
        "jobs_deferred": 0,
        "jobs_failed": 0,
        "errors": [],
    }
    assert rescored["scoring_version"] == SCORING_VERSION
    assert 0 <= rescored["fit_score"] <= 100
    assert 0 <= rescored["score_confidence"] <= 100
    assert len(rescored["score_dimensions"]) == 5
    assert isinstance(rescored["score_evidence"], list)
    assert second["jobs_eligible"] == 0
    assert second["jobs_rescored"] == 0


def test_rescore_baselines_historical_matches_atomically(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    strong = storage.save_job(
        {
            **make_job(
                "Junior Software Engineer",
                "Build React, TypeScript, and cloud features.",
                location="Remote",
            ).model_dump(mode="json"),
            "fit_score": 1,
        }
    )
    rejected = storage.save_job(
        {
            **make_job("Business Analyst").model_dump(mode="json"),
            "url": "https://example.com/jobs/analyst",
            "fit_score": 1,
        }
    )

    summary = rescore_stale_jobs(
        storage,
        profile=PROFILE,
        baseline_notification_channel="discord",
    )

    assert summary["jobs_notification_baselined"] == 1
    assert storage.get_job_notification("discord", strong["id"])["status"] == (
        "baseline"
    )
    assert storage.get_job_notification("discord", rejected["id"]) is None


def test_rescore_stale_jobs_sanitizes_item_failures(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    storage.save_job(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "not-a-url",
            "source": "test",
            "fit_score": 4,
        }
    )

    summary = rescore_stale_jobs(storage, profile=PROFILE)

    assert summary["jobs_attempted"] == 1
    assert summary["jobs_failed"] == 1
    assert summary["errors"] == [
        {"job_id": 1, "error_type": "ValidationError"}
    ]


@pytest.mark.parametrize("max_jobs", [-1, MAX_RESCORE_BATCH + 1])
def test_rescore_stale_jobs_rejects_unbounded_batches(
    tmp_path: Path,
    max_jobs: int,
) -> None:
    with pytest.raises(ValueError, match="between zero and 1000"):
        rescore_stale_jobs(
            JobStorage(tmp_path / "jobs.json"),
            profile=PROFILE,
            max_jobs=max_jobs,
        )
