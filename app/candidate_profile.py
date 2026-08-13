from functools import lru_cache
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_CANDIDATE_PROFILE_PATH = (
    PROJECT_ROOT / "credentials" / "candidate_profile.yaml"
)
EXAMPLE_CANDIDATE_PROFILE_PATH = (
    PROJECT_ROOT / "config" / "candidate_profile.example.yaml"
)


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_keyword: StrictInt = Field(ge=0)
    technology_keyword: StrictInt = Field(ge=0)
    location_keyword: StrictInt = Field(ge=0)
    red_flag_keyword: StrictInt = Field(le=0)


class ScoringThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: StrictInt = Field(ge=1, le=99)
    strong: StrictInt = Field(ge=2, le=100)


class ScoringDimensionWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: StrictInt = Field(ge=0, le=100)
    seniority: StrictInt = Field(ge=0, le=100)
    skills: StrictInt = Field(ge=0, le=100)
    location: StrictInt = Field(ge=0, le=100)
    risk: StrictInt = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_total(self) -> "ScoringDimensionWeights":
        if sum(self.model_dump().values()) != 100:
            raise ValueError("Scoring dimension weights must total 100")
        return self


class LegacyCandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt
    profile_name: str = Field(min_length=1)
    approval_name: str = Field(min_length=1)
    target_role_keywords: list[str] = Field(min_length=1)
    target_technology_keywords: list[str] = Field(min_length=1)
    preferred_location_keywords: list[str] = Field(min_length=1)
    red_flag_keywords: list[str] = Field(min_length=1)
    weights: ScoringWeights
    thresholds: ScoringThresholds

    @field_validator("profile_name", "approval_name")
    @classmethod
    def validate_profile_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Profile name must not be blank")
        return value

    @field_validator(
        "target_role_keywords",
        "target_technology_keywords",
        "preferred_location_keywords",
        "red_flag_keywords",
    )
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(not value for value in normalized):
            raise ValueError("Candidate profile keywords must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Candidate profile keywords must be unique")
        return normalized


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt
    profile_name: str = Field(min_length=1)
    approval_name: str = Field(min_length=1)
    target_role_keywords: list[str] = Field(min_length=1)
    target_technology_keywords: list[str] = Field(min_length=1)
    preferred_location_keywords: list[str] = Field(min_length=1)
    remote_or_hybrid_required_location_keywords: list[str] = Field(
        default_factory=list
    )
    remote_or_hybrid_required_regions: list[
        Literal["twin_cities_seven_county"]
    ] = Field(default_factory=list)
    require_preferred_location_for_strong: bool = False
    preferred_seniority_keywords: list[str] = Field(min_length=1)
    excluded_seniority_keywords: list[str] = Field(default_factory=list)
    risk_keywords: list[str] = Field(default_factory=list)
    dimension_weights: ScoringDimensionWeights
    thresholds: ScoringThresholds

    @field_validator("profile_name", "approval_name")
    @classmethod
    def validate_profile_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Profile name must not be blank")
        return value

    @field_validator(
        "target_role_keywords",
        "target_technology_keywords",
        "preferred_location_keywords",
        "remote_or_hybrid_required_location_keywords",
        "preferred_seniority_keywords",
        "excluded_seniority_keywords",
        "risk_keywords",
    )
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(not value for value in normalized):
            raise ValueError("Candidate profile keywords must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Candidate profile keywords must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "CandidateProfile":
        if self.schema_version != 2:
            raise ValueError("Unsupported candidate profile schema version")
        if self.thresholds.strong <= self.thresholds.review:
            raise ValueError("Strong threshold must be above review threshold")
        required_locations = set(
            self.remote_or_hybrid_required_location_keywords
        )
        if not required_locations.issubset(self.preferred_location_keywords):
            raise ValueError(
                "Remote-or-hybrid-required locations must also be preferred locations"
            )
        return self


def load_candidate_profile(
    path: Path,
) -> CandidateProfile:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("Candidate profile is not readable YAML") from error
    if not isinstance(content, dict):
        raise ValueError("Candidate profile must be a YAML object")
    if content.get("schema_version") == 1:
        return migrate_legacy_candidate_profile(
            LegacyCandidateProfile.model_validate(content)
        )
    return CandidateProfile.model_validate(content)


PREFERRED_SENIORITY_SIGNALS = (
    "entry level",
    "entry-level",
    "junior",
    "new grad",
)
LEGACY_EXCLUDED_SENIORITY_SIGNALS = frozenset(
    {
        "architect",
        "lead",
        "manager",
        "principal",
        "senior",
        "staff",
        "5+ years",
        "6+ years",
        "7+ years",
        "8+ years",
        "9+ years",
        "10+ years",
    }
)
DEFAULT_DIMENSION_WEIGHTS = ScoringDimensionWeights(
    role=30,
    seniority=15,
    skills=30,
    location=15,
    risk=10,
)


def migrate_legacy_candidate_profile(
    legacy: LegacyCandidateProfile,
) -> CandidateProfile:
    if legacy.schema_version != 1:
        raise ValueError("Unsupported candidate profile schema version")
    seniority_aliases = {
        value.replace("-", " ") for value in PREFERRED_SENIORITY_SIGNALS
    }
    role_keywords = [
        value
        for value in legacy.target_role_keywords
        if value.strip().lower().replace("-", " ") not in seniority_aliases
    ]
    if not role_keywords:
        raise ValueError(
            "Legacy candidate profile must include a target role beyond "
            "seniority keywords"
        )
    excluded = [
        value
        for value in legacy.red_flag_keywords
        if value.strip().lower() in LEGACY_EXCLUDED_SENIORITY_SIGNALS
    ]
    risks = [
        value
        for value in legacy.red_flag_keywords
        if value.strip().lower() not in LEGACY_EXCLUDED_SENIORITY_SIGNALS
    ]
    return CandidateProfile(
        schema_version=2,
        profile_name=legacy.profile_name,
        approval_name=legacy.approval_name,
        target_role_keywords=role_keywords,
        target_technology_keywords=legacy.target_technology_keywords,
        preferred_location_keywords=legacy.preferred_location_keywords,
        remote_or_hybrid_required_location_keywords=[],
        remote_or_hybrid_required_regions=[],
        require_preferred_location_for_strong=False,
        preferred_seniority_keywords=list(PREFERRED_SENIORITY_SIGNALS),
        excluded_seniority_keywords=excluded,
        risk_keywords=risks,
        dimension_weights=DEFAULT_DIMENSION_WEIGHTS,
        thresholds=legacy.thresholds,
    )


def configured_candidate_profile_path(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> Path:
    try:
        content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("Jobbot config is not readable YAML") from error
    if not isinstance(content, dict):
        raise ValueError("Jobbot config must be a YAML object")
    configured = content.get("candidate_profile")
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("Jobbot config must select a candidate profile")
    path = Path(configured.strip())
    if not path.is_absolute():
        path = config_path.parent / path
    return path


def reject_symlinked_components(path: Path) -> None:
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    for component in (absolute_path, *absolute_path.parents):
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError("Candidate profile path must not contain symlinks")


def secure_local_profile(path: Path) -> None:
    reject_symlinked_components(path)
    if path.is_symlink():
        raise ValueError("Local candidate profile must be a regular file")
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("Local candidate profile must be a regular file")
    parent_stat = path.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
        parent_stat.st_mode
    ):
        raise ValueError("Candidate profile directory must be regular")
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)


@lru_cache(maxsize=1)
def default_candidate_profile() -> CandidateProfile:
    profile_path = configured_candidate_profile_path()
    if profile_path.exists():
        secure_local_profile(profile_path)
        return load_candidate_profile(profile_path)
    if profile_path != DEFAULT_CANDIDATE_PROFILE_PATH:
        raise ValueError("Configured candidate profile does not exist")
    return load_candidate_profile(EXAMPLE_CANDIDATE_PROFILE_PATH)
