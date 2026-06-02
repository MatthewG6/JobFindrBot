from datetime import UTC, datetime

from pydantic import BaseModel, Field, HttpUrl


class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    url: HttpUrl
    source: str
    description: str = ""
    posted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScoredJob(BaseModel):
    job: JobPosting
    score: int
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
