from app.models import JobPosting, ScoredJob


GOOD_KEYWORDS = [
    "junior",
    "new grad",
    "entry-level",
    "entry level",
    "software engineer",
    "software developer",
    "frontend",
    "front-end",
    "full-stack",
    "full stack",
    "java",
    "react",
    "typescript",
    "aws",
    "cloud",
]

BAD_KEYWORDS = [
    "senior",
    "staff",
    "principal",
    "lead",
    "architect",
    "manager",
    "5+ years",
    "6+ years",
    "7+ years",
    "8+ years",
    "contract only",
    "unpaid",
]


def score_job(job: JobPosting) -> ScoredJob:
    text = f"{job.title} {job.location} {job.description}".lower()
    score = 0
    reasons: list[str] = []
    red_flags: list[str] = []

    for keyword in GOOD_KEYWORDS:
        if keyword in text:
            score += 10
            reasons.append(f"Matches target keyword: {keyword}")

    for keyword in BAD_KEYWORDS:
        if keyword in text:
            score -= 25
            red_flags.append(f"Penalized keyword: {keyword}")

    return ScoredJob(job=job, score=score, reasons=reasons, red_flags=red_flags)
