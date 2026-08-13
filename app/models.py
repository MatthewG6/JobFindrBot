from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.sensitive_data import REDACTED_VALUE, SensitiveCategory


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    url: HttpUrl
    source: str
    description: str = ""
    workplace_type: str | None = None
    source_job_id: str | None = None
    source_message_id: str | None = None
    alert_query: str | None = None
    salary_text: str | None = None
    posted_text: str | None = None
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


ScoreDimensionName = Literal["role", "seniority", "skills", "location", "risk"]
ScoreEvidenceKind = Literal["match", "exclusion", "risk", "uncertainty"]
ScoreEvidenceSource = Literal[
    "title",
    "location",
    "description",
    "workplace_type",
]
ScoreConfidenceBand = Literal["low", "medium", "high"]
WORK_ARRANGEMENT_UNCONFIRMED_SIGNAL = "remote-or-hybrid arrangement unconfirmed"
PREFERRED_LOCATION_UNCONFIRMED_SIGNAL = "preferred location unconfirmed"


class ScoreEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: ScoreDimensionName
    kind: ScoreEvidenceKind
    signal: str
    source: ScoreEvidenceSource


class ScoreDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ScoreDimensionName
    score: int = Field(ge=0, le=100)
    weight: int = Field(ge=0, le=100)
    weighted_points: float = Field(ge=0, le=100)
    summary: str
    evidence: list[ScoreEvidence] = Field(default_factory=list)


class ScoredJob(BaseModel):
    job: JobPosting
    score: int = Field(ge=0, le=100)
    confidence: int = Field(ge=0, le=100)
    confidence_band: ScoreConfidenceBand
    dimensions: list[ScoreDimension]
    evidence: list[ScoreEvidence]
    scoring_version: int = Field(ge=1)
    review_threshold: int = Field(ge=1, le=99)
    strong_threshold: int = Field(ge=2, le=100)
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)


class ApplicationStatus(str, Enum):
    AWAITING_START_APPROVAL = "awaiting_start_approval"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY_FOR_REVIEW = "ready_for_review"
    AWAITING_SUBMIT_APPROVAL = "awaiting_submit_approval"
    APPROVED_TO_SUBMIT = "approved_to_submit"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


APPLICATION_STATUS_TRANSITIONS: dict[
    ApplicationStatus,
    frozenset[ApplicationStatus],
] = {
    ApplicationStatus.AWAITING_START_APPROVAL: frozenset(
        {
            ApplicationStatus.SKIPPED,
        }
    ),
    ApplicationStatus.QUEUED: frozenset(
        {
            ApplicationStatus.IN_PROGRESS,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.IN_PROGRESS: frozenset(
        {
            ApplicationStatus.WAITING_FOR_INPUT,
            ApplicationStatus.READY_FOR_REVIEW,
            ApplicationStatus.AWAITING_SUBMIT_APPROVAL,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.WAITING_FOR_INPUT: frozenset(
        {
            ApplicationStatus.IN_PROGRESS,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.READY_FOR_REVIEW: frozenset(
        {
            ApplicationStatus.IN_PROGRESS,
            ApplicationStatus.AWAITING_SUBMIT_APPROVAL,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.AWAITING_SUBMIT_APPROVAL: frozenset(
        {
            ApplicationStatus.IN_PROGRESS,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.APPROVED_TO_SUBMIT: frozenset(
        {
            ApplicationStatus.IN_PROGRESS,
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.SKIPPED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.FAILED: frozenset({ApplicationStatus.QUEUED}),
    ApplicationStatus.SUBMITTED: frozenset(),
    ApplicationStatus.SKIPPED: frozenset(),
}

PROTECTED_APPLICATION_TRANSITIONS = {
    (
        ApplicationStatus.AWAITING_START_APPROVAL,
        ApplicationStatus.QUEUED,
    ): "start",
    (
        ApplicationStatus.AWAITING_SUBMIT_APPROVAL,
        ApplicationStatus.APPROVED_TO_SUBMIT,
    ): "submit",
}


class InvalidApplicationTransition(ValueError):
    pass


def validate_application_transition(
    current_status: ApplicationStatus,
    next_status: ApplicationStatus,
    approval_kind: str | None = None,
    approved_by: str | None = None,
) -> None:
    required_approval = PROTECTED_APPLICATION_TRANSITIONS.get(
        (current_status, next_status)
    )
    if required_approval is not None:
        if approval_kind != required_approval:
            raise InvalidApplicationTransition(
                f"Transition from {current_status.value} to "
                f"{next_status.value} requires explicit "
                f"{required_approval} approval"
            )
        if not approved_by:
            raise InvalidApplicationTransition(
                f"Transition from {current_status.value} to "
                f"{next_status.value} requires an approver identity"
            )
        return

    allowed_statuses = APPLICATION_STATUS_TRANSITIONS[current_status]
    if next_status not in allowed_statuses:
        raise InvalidApplicationTransition(
            f"Cannot transition application from {current_status.value} "
            f"to {next_status.value}"
        )


NonBlankString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class JobAlertEmail(BaseModel):
    message_id: NonBlankString
    sender: NonBlankString
    subject: NonBlankString
    received_at: datetime
    gmail_labels: set[str]
    authentication_results: NonBlankString
    text_body: str = ""
    html_body: str = ""


class ApplicationTransitionRequest(BaseModel):
    status: ApplicationStatus
    message: NonBlankString
    current_step: NonBlankString | None = None
    provider: NonBlankString | None = None


class ApplicationEvent(BaseModel):
    status: ApplicationStatus
    message: str
    current_step: str | None = None
    provider: str | None = None
    approval_kind: str | None = None
    approved_by: str | None = None
    sensitive_question: str | None = None
    sensitive_category: SensitiveCategory | None = None
    approval_outcome: Literal["pending", "approved", "declined"] | None = None
    value_redacted: Literal[REDACTED_VALUE] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ApplicationRecord(BaseModel):
    job_id: int
    status: ApplicationStatus = ApplicationStatus.AWAITING_START_APPROVAL
    provider: str | None = None
    current_step: str = "Waiting for approval to start"
    events: list[ApplicationEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
