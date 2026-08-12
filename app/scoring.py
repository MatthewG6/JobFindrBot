import re
from typing import TYPE_CHECKING, Iterable, Literal

from app.candidate_profile import CandidateProfile, default_candidate_profile
from app.models import (
    JobPosting,
    ScoreDimension,
    ScoreDimensionName,
    ScoreEvidence,
    ScoredJob,
)

if TYPE_CHECKING:
    from app.storage import JobStorage


SCORING_VERSION = 2
MAX_RESCORE_BATCH = 1_000
SourceName = Literal["title", "location", "description"]
EXPERIENCE_NUMBER_WORDS = {
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
EXPERIENCE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?P<years>\d{1,2}|five|six|seven|eight|nine|ten)"
    r"[\s-]*(?:\+\s*)?(?:years?|yrs?)(?![a-z0-9])",
    re.IGNORECASE,
)


def signal_pattern(signal: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in re.split(r"[\s-]+", signal) if part]
    expression = r"[\s-]+".join(parts)
    return re.compile(rf"(?<![a-z0-9]){expression}(?![a-z0-9])", re.IGNORECASE)


def canonical_signal(signal: str) -> str:
    return re.sub(r"[\s-]+", "", signal.strip().lower())


def matching_evidence(
    *,
    dimension: ScoreDimensionName,
    kind: Literal["match", "exclusion", "risk"],
    signals: Iterable[str],
    fields: tuple[tuple[SourceName, str], ...],
) -> list[ScoreEvidence]:
    evidence = []
    seen = set()
    for signal in signals:
        canonical = canonical_signal(signal)
        if canonical in seen:
            continue
        for source, text in fields:
            if signal_pattern(signal).search(text):
                evidence.append(
                    ScoreEvidence(
                        dimension=dimension,
                        kind=kind,
                        signal=signal,
                        source=source,
                    )
                )
                seen.add(canonical)
                break
    return evidence


def configured_experience_floor(signals: Iterable[str]) -> int | None:
    years = []
    for signal in signals:
        match = EXPERIENCE_PATTERN.search(signal)
        if match is None:
            continue
        value = match.group("years").lower()
        years.append(
            int(value) if value.isdigit() else EXPERIENCE_NUMBER_WORDS[value]
        )
    return min(years) if years else None


def experience_evidence(
    signals: Iterable[str],
    fields: tuple[tuple[SourceName, str], ...],
) -> list[ScoreEvidence]:
    floor = configured_experience_floor(signals)
    if floor is None:
        return []
    for source, text in fields:
        normalized_text = re.sub(
            r"\b([Yy][Rr][Ss]?)\.(?=\s+[a-z])",
            r"\1",
            text,
        )
        for match in EXPERIENCE_PATTERN.finditer(normalized_text):
            sentence_start = max(
                normalized_text.rfind(boundary, 0, match.start())
                for boundary in ".!?;"
            )
            sentence_ends = [
                position
                for boundary in ".!?;"
                if (position := normalized_text.find(boundary, match.end())) >= 0
            ]
            sentence_end = min(sentence_ends, default=len(normalized_text))
            sentence = normalized_text[sentence_start + 1 : sentence_end]
            if re.search(r"\bexperience\b", sentence, re.IGNORECASE) is None:
                continue
            value = match.group("years").lower()
            years = (
                int(value)
                if value.isdigit()
                else EXPERIENCE_NUMBER_WORDS[value]
            )
            if years >= floor:
                return [
                    ScoreEvidence(
                        dimension="seniority",
                        kind="exclusion",
                        signal=f"{years}+ years",
                        source=source,
                    )
                ]
    return []


def role_linked_seniority_evidence(
    seniority_signals: Iterable[str],
    role_signals: Iterable[str],
    description: str,
) -> list[ScoreEvidence]:
    for role in role_signals:
        for role_match in signal_pattern(role).finditer(description):
            prefix_window = description[
                max(0, role_match.start() - 80) : role_match.start()
            ]
            prefix_window = re.sub(
                r"\bsr\.",
                "sr",
                prefix_window,
                flags=re.IGNORECASE,
            )
            prefix = re.split(r"[.!?;]", prefix_window)[-1]
            for seniority in seniority_signals:
                seniority_expression = (
                    r"(?:senior(?:[\s-]+level)?|sr\.?)"
                    if canonical_signal(seniority) == "senior"
                    else signal_pattern(seniority).pattern
                )
                linked_pattern = re.compile(
                    rf"{seniority_expression}"
                    r"(?:[\s-]+[a-z0-9+#.]+){0,3}[\s-]*$",
                    re.IGNORECASE,
                )
                if linked_pattern.search(prefix) is None:
                    continue
                return [
                    ScoreEvidence(
                        dimension="seniority",
                        kind="exclusion",
                        signal=seniority,
                        source="description",
                    )
                ]
    return []


def dimension(
    name: ScoreDimensionName,
    score: int,
    weight: int,
    summary: str,
    evidence: list[ScoreEvidence],
) -> ScoreDimension:
    return ScoreDimension(
        name=name,
        score=score,
        weight=weight,
        weighted_points=round(score * weight / 100, 2),
        summary=summary,
        evidence=evidence,
    )


def scoring_confidence(job: JobPosting, evidence: list[ScoreEvidence]) -> int:
    confidence = 0
    if job.title.strip():
        confidence += 15
    if job.company.strip():
        confidence += 5
    if job.location.strip():
        confidence += 10

    description_length = len(job.description.strip())
    if description_length >= 500:
        confidence += 40
    elif description_length >= 200:
        confidence += 32
    elif description_length >= 80:
        confidence += 22
    elif description_length:
        confidence += 10

    if job.salary_text and job.salary_text.strip():
        confidence += 5
    if job.posted_at is not None or (job.posted_text and job.posted_text.strip()):
        confidence += 5
    confidence += min(20, len(evidence) * 4)
    return min(100, confidence)


def confidence_band(confidence: int) -> Literal["low", "medium", "high"]:
    if confidence >= 75:
        return "high"
    if confidence >= 50:
        return "medium"
    return "low"


def score_job(
    job: JobPosting,
    profile: CandidateProfile | None = None,
) -> ScoredJob:
    profile = profile or default_candidate_profile()
    title = job.title
    location = job.location
    description = job.description

    role_evidence = matching_evidence(
        dimension="role",
        kind="match",
        signals=profile.target_role_keywords,
        fields=(("title", title), ("description", description)),
    )
    role_title_match = any(item.source == "title" for item in role_evidence)
    role_score = 100 if role_title_match else 65 if role_evidence else 0
    role_summary = (
        "Target role appears in the title"
        if role_title_match
        else "Target role appears only in the description"
        if role_evidence
        else "No target role evidence"
    )

    preferred_seniority = matching_evidence(
        dimension="seniority",
        kind="match",
        signals=profile.preferred_seniority_keywords,
        fields=(("title", title), ("description", description)),
    )
    title_level_exclusions = [
        signal
        for signal in profile.excluded_seniority_keywords
        if not any(
            marker in canonical_signal(signal)
            for marker in ("year", "yrs", "experience")
        )
    ]
    experience_exclusions = [
        signal
        for signal in profile.excluded_seniority_keywords
        if any(
            marker in canonical_signal(signal)
            for marker in ("year", "yrs", "experience")
        )
    ]
    excluded_seniority = matching_evidence(
        dimension="seniority",
        kind="exclusion",
        signals=title_level_exclusions,
        fields=(("title", title),),
    )
    if (
        "senior" in {canonical_signal(value) for value in title_level_exclusions}
        and signal_pattern("sr").search(title)
        and not any(
            canonical_signal(item.signal) == "senior"
            for item in excluded_seniority
        )
    ):
        excluded_seniority.append(
            ScoreEvidence(
                dimension="seniority",
                kind="exclusion",
                signal="senior",
                source="title",
            )
        )
    excluded_seniority.extend(
        role_linked_seniority_evidence(
            title_level_exclusions,
            profile.target_role_keywords,
            description,
        )
    )
    excluded_seniority.extend(
        experience_evidence(
            experience_exclusions,
            (("title", title), ("description", description)),
        )
    )
    seniority_evidence = preferred_seniority + excluded_seniority
    if excluded_seniority:
        seniority_score = 0
        seniority_summary = "Excluded seniority or experience requirement found"
    elif preferred_seniority:
        seniority_score = 100
        seniority_summary = "Preferred early-career seniority found"
    else:
        seniority_score = 60
        seniority_summary = "Seniority is not explicit"

    skill_evidence = matching_evidence(
        dimension="skills",
        kind="match",
        signals=profile.target_technology_keywords,
        fields=(("title", title), ("description", description)),
    )
    skill_target = min(
        3,
        len(
            {
                canonical_signal(value)
                for value in profile.target_technology_keywords
            }
        ),
    )
    skill_score = (
        round(100 * min(len(skill_evidence), skill_target) / skill_target)
        if skill_target
        else 0
    )
    skill_summary = (
        f"{len(skill_evidence)} target technology signal(s) found"
        if skill_evidence
        else "No target technology evidence"
    )

    location_evidence = matching_evidence(
        dimension="location",
        kind="match",
        signals=profile.preferred_location_keywords,
        fields=(("location", location), ("description", description)),
    )
    location_field_match = any(
        item.source == "location" for item in location_evidence
    )
    location_score = 100 if location_field_match else 75 if location_evidence else 40
    location_summary = (
        "Preferred location appears in the location field"
        if location_field_match
        else "Preferred location appears only in the description"
        if location_evidence
        else "No preferred location evidence"
    )

    risk_evidence = matching_evidence(
        dimension="risk",
        kind="risk",
        signals=profile.risk_keywords,
        fields=(("title", title), ("description", description)),
    )
    risk_score = 0 if risk_evidence else 100
    risk_summary = (
        "Explicit candidate risk found"
        if risk_evidence
        else "No explicit candidate risk found"
    )

    weights = profile.dimension_weights
    dimensions = [
        dimension("role", role_score, weights.role, role_summary, role_evidence),
        dimension(
            "seniority",
            seniority_score,
            weights.seniority,
            seniority_summary,
            seniority_evidence,
        ),
        dimension(
            "skills",
            skill_score,
            weights.skills,
            skill_summary,
            skill_evidence,
        ),
        dimension(
            "location",
            location_score,
            weights.location,
            location_summary,
            location_evidence,
        ),
        dimension("risk", risk_score, weights.risk, risk_summary, risk_evidence),
    ]
    score = round(sum(item.weighted_points for item in dimensions))
    if not role_evidence or excluded_seniority or risk_evidence:
        score = min(score, profile.thresholds.review - 1)

    evidence = [item for item in role_evidence]
    evidence.extend(seniority_evidence)
    evidence.extend(skill_evidence)
    evidence.extend(location_evidence)
    evidence.extend(risk_evidence)
    reasons = []
    reason_prefixes = {
        "role": "Target role match",
        "seniority": "Preferred seniority match",
        "skills": "Target tech match",
        "location": "Target location match",
    }
    for item in evidence:
        if item.kind == "match":
            reasons.append(f"{reason_prefixes[item.dimension]}: {item.signal}")
    red_flags = [
        f"Red flag: {item.signal}"
        for item in evidence
        if item.kind in {"exclusion", "risk"}
    ]
    confidence = scoring_confidence(job, evidence)
    return ScoredJob(
        job=job,
        score=score,
        confidence=confidence,
        confidence_band=confidence_band(confidence),
        dimensions=dimensions,
        evidence=evidence,
        scoring_version=SCORING_VERSION,
        review_threshold=profile.thresholds.review,
        reasons=reasons,
        red_flags=red_flags,
    )


def scoring_fields(scored: ScoredJob) -> dict:
    return {
        "fit_score": scored.score,
        "score_confidence": scored.confidence,
        "score_confidence_band": scored.confidence_band,
        "score_dimensions": [
            item.model_dump(mode="json") for item in scored.dimensions
        ],
        "score_evidence": [
            item.model_dump(mode="json") for item in scored.evidence
        ],
        "scoring_version": scored.scoring_version,
        "score_review_threshold": scored.review_threshold,
        "score_reasons": scored.reasons,
        "red_flags": scored.red_flags,
    }


def rescore_stale_jobs(
    storage: "JobStorage",
    *,
    profile: CandidateProfile | None = None,
    max_jobs: int = MAX_RESCORE_BATCH,
    baseline_notification_channel: str | None = None,
) -> dict:
    if not 0 <= max_jobs <= MAX_RESCORE_BATCH:
        raise ValueError("Rescore batch size must be between zero and 1000")
    profile = profile or default_candidate_profile()
    jobs = storage.list_jobs()
    stale = [job for job in jobs if job.get("scoring_version") != SCORING_VERSION]
    summary = {
        "jobs_considered": len(jobs),
        "jobs_eligible": len(stale),
        "jobs_attempted": 0,
        "jobs_rescored": 0,
        "jobs_notification_baselined": 0,
        "jobs_deferred": max(0, len(stale) - max_jobs),
        "jobs_failed": 0,
        "errors": [],
    }
    for job in stale[:max_jobs]:
        summary["jobs_attempted"] += 1
        try:
            posting = JobPosting.model_validate(job)
            scored = score_job(posting, profile)
            notification_baselined = 0
            with storage.transaction():
                storage.update_job_score(job["id"], scoring_fields(scored))
                if (
                    baseline_notification_channel is not None
                    and scored.score >= profile.thresholds.review
                ):
                    notification_baselined = storage.baseline_job_notifications(
                        baseline_notification_channel,
                        [job["id"]],
                    )
            summary["jobs_rescored"] += 1
            summary["jobs_notification_baselined"] += notification_baselined
        except Exception as error:
            summary["jobs_failed"] += 1
            summary["errors"].append(
                {"job_id": job.get("id"), "error_type": type(error).__name__}
            )
    return summary
