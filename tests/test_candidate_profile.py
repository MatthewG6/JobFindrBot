from pathlib import Path

import pytest

from app.candidate_profile import (
    configured_candidate_profile_path,
    load_candidate_profile,
    secure_local_profile,
)
from app.models import JobPosting
from app.scoring import score_job


def test_default_candidate_profile_preserves_scoring_contract() -> None:
    profile = load_candidate_profile(
        Path("config/candidate_profile.example.yaml")
    )
    job = JobPosting(
        title="Junior Software Engineer",
        company="Example",
        location="Minneapolis, MN",
        url="https://example.com/job",
        source="test",
        description="Build React and TypeScript cloud features.",
    )

    scored = score_job(job, profile)

    assert profile.thresholds.review == 40
    assert profile.thresholds.strong == 75
    assert profile.schema_version == 2
    assert sum(profile.dimension_weights.model_dump().values()) == 100
    assert profile.remote_or_hybrid_required_location_keywords == []
    assert profile.remote_or_hybrid_required_regions == []
    assert profile.require_preferred_location_for_strong is False
    assert scored.score == 100


def test_legacy_candidate_profile_migrates_in_memory(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        """
schema_version: 1
profile_name: Test
approval_name: Tester
target_role_keywords: [junior, software engineer]
target_technology_keywords: [python]
preferred_location_keywords: [remote]
red_flag_keywords: [senior, unpaid]
weights:
  role_keyword: 1
  technology_keyword: 1
  location_keyword: 1
  red_flag_keyword: -1
thresholds:
  review: 40
  strong: 75
""".strip(),
        encoding="utf-8",
    )

    profile = load_candidate_profile(path)

    assert profile.schema_version == 2
    assert profile.target_role_keywords == ["software engineer"]
    assert "junior" in profile.preferred_seniority_keywords
    assert profile.excluded_seniority_keywords == ["senior"]
    assert profile.risk_keywords == ["unpaid"]


def test_legacy_profile_requires_a_role_beyond_seniority(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        """
schema_version: 1
profile_name: Test
approval_name: Tester
target_role_keywords: [junior]
target_technology_keywords: [python]
preferred_location_keywords: [remote]
red_flag_keywords: [senior]
weights:
  role_keyword: 1
  technology_keyword: 1
  location_keyword: 1
  red_flag_keyword: -1
thresholds:
  review: 40
  strong: 75
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target role beyond seniority"):
        load_candidate_profile(path)


def test_candidate_profile_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        """
schema_version: 2
profile_name: Test
approval_name: Tester
target_role_keywords: [developer]
target_technology_keywords: [python]
preferred_location_keywords: [remote]
preferred_seniority_keywords: [junior]
excluded_seniority_keywords: [senior]
risk_keywords: [unpaid]
dimension_weights:
  role: 30
  seniority: 15
  skills: 30
  location: 15
  risk: 10
thresholds:
  review: 1
  strong: 2
unexpected: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_candidate_profile(path)


def test_candidate_profile_requires_constrained_locations_to_be_preferred(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.yaml"
    content = Path("config/candidate_profile.example.yaml").read_text(
        encoding="utf-8"
    ).replace(
        "remote_or_hybrid_required_location_keywords: []",
        "remote_or_hybrid_required_location_keywords: [chicago]",
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="must also be preferred"):
        load_candidate_profile(path)


@pytest.mark.parametrize(
    ("review", "strong"),
    [(0, 75), (40, 101), (75, 75)],
)
def test_candidate_profile_rejects_invalid_normalized_thresholds(
    tmp_path: Path,
    review: int,
    strong: int,
) -> None:
    path = tmp_path / "profile.yaml"
    content = Path("config/candidate_profile.example.yaml").read_text(
        encoding="utf-8"
    )
    content = content.replace("review: 40", f"review: {review}")
    content = content.replace("strong: 75", f"strong: {strong}")
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_candidate_profile(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "true"),
        ("schema_version", "3"),
        ("role_keyword", "true"),
    ],
)
def test_candidate_profile_rejects_unsupported_or_non_integer_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    schema_version = value if field == "schema_version" else "1"
    role_weight = value if field == "role_keyword" else "1"
    path = tmp_path / "profile.yaml"
    path.write_text(
        f"""
schema_version: {schema_version}
profile_name: Test
approval_name: Tester
target_role_keywords: [developer]
target_technology_keywords: [python]
preferred_location_keywords: [remote]
red_flag_keywords: [senior]
weights:
  role_keyword: {role_weight}
  technology_keyword: 1
  location_keyword: 1
  red_flag_keyword: -1
thresholds:
  review: 1
  strong: 2
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_candidate_profile(path)


def test_config_selects_candidate_profile_relative_to_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "candidate_profile: private/profile.yaml\n",
        encoding="utf-8",
    )

    assert configured_candidate_profile_path(config) == (
        tmp_path / "private" / "profile.yaml"
    )


def test_candidate_profile_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    profile = outside / "profile.yaml"
    profile.write_text("private", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks"):
        secure_local_profile(linked / "profile.yaml")

    assert outside.stat().st_mode & 0o777 == 0o755
