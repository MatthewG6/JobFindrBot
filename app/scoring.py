from app.models import JobPosting, ScoredJob


TARGET_ROLE_KEYWORDS = [
    "application developer",
    "entry level",
    "entry-level",
    "frontend",
    "front-end",
    "full stack",
    "full-stack",
    "junior",
    "new grad",
    "software engineer",
    "software developer",
]

TARGET_TECH_KEYWORDS = [
    "aws",
    "cloud",
    "java",
    "react",
    "typescript",
]

TARGET_LOCATION_KEYWORDS = [
    "minneapolis",
    "minnesota",
    "remote",
    "rochester",
    "st. paul",
    "saint paul",
    "twin cities",
]

RED_FLAG_KEYWORDS = [
    "architect",
    "contract only",
    "lead",
    "manager",
    "principal",
    "senior",
    "staff",
    "unpaid",
    "5+ years",
    "6+ years",
    "7+ years",
    "8+ years",
    "9+ years",
    "10+ years",
]


def contains_keyword(text: str, keyword: str) -> bool:
    return keyword in text


def score_job(job: JobPosting) -> ScoredJob:
    text = f"{job.title} {job.location} {job.description}".lower()
    score = 0
    reasons: list[str] = []
    red_flags: list[str] = []

    for keyword in TARGET_ROLE_KEYWORDS:
        if contains_keyword(text, keyword):
            score += 15
            reasons.append(f"Target role match: {keyword}")

    for keyword in TARGET_TECH_KEYWORDS:
        if contains_keyword(text, keyword):
            score += 8
            reasons.append(f"Target tech match: {keyword}")

    for keyword in TARGET_LOCATION_KEYWORDS:
        if contains_keyword(text, keyword):
            score += 10
            reasons.append(f"Target location match: {keyword}")

    for keyword in RED_FLAG_KEYWORDS:
        if contains_keyword(text, keyword):
            score -= 25
            red_flags.append(f"Red flag: {keyword}")

    return ScoredJob(job=job, score=score, reasons=reasons, red_flags=red_flags)
