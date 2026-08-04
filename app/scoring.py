from app.candidate_profile import CandidateProfile, default_candidate_profile
from app.models import JobPosting, ScoredJob


def contains_keyword(text: str, keyword: str) -> bool:
    return keyword in text


def score_job(
    job: JobPosting,
    profile: CandidateProfile | None = None,
) -> ScoredJob:
    profile = profile or default_candidate_profile()
    text = f"{job.title} {job.location} {job.description}".lower()
    score = 0
    reasons: list[str] = []
    red_flags: list[str] = []

    for keyword in profile.target_role_keywords:
        if contains_keyword(text, keyword):
            score += profile.weights.role_keyword
            reasons.append(f"Target role match: {keyword}")

    for keyword in profile.target_technology_keywords:
        if contains_keyword(text, keyword):
            score += profile.weights.technology_keyword
            reasons.append(f"Target tech match: {keyword}")

    for keyword in profile.preferred_location_keywords:
        if contains_keyword(text, keyword):
            score += profile.weights.location_keyword
            reasons.append(f"Target location match: {keyword}")

    for keyword in profile.red_flag_keywords:
        if contains_keyword(text, keyword):
            score += profile.weights.red_flag_keyword
            red_flags.append(f"Red flag: {keyword}")

    return ScoredJob(job=job, score=score, reasons=reasons, red_flags=red_flags)
