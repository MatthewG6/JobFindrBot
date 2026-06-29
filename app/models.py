from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    url: HttpUrl
    source: str
    description: str = ""
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class ScoredJob(BaseModel):
    job: JobPosting
    score: int
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)


class ApplicationStatus(str, Enum):
    AWAITING_START_APPROVAL = "awaiting_start_approval"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY_FOR_REVIEW = "ready_for_review"
    AWAITING_SUBMIT_APPROVAL = "awaiting_submit_approval"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplicationEvent(BaseModel):
    status: ApplicationStatus
    message: str
    created_at: datetime = Field(default_factory=utc_now)


class ApplicationRecord(BaseModel):
    job_id: int
    status: ApplicationStatus = ApplicationStatus.AWAITING_START_APPROVAL
    provider: str | None = None
    current_step: str = "Waiting for approval to start"
    events: list[ApplicationEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
