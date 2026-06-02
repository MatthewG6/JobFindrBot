from hashlib import sha256

from app.models import JobPosting


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def job_key(job: JobPosting) -> str:
    normalized_company = normalize_text(job.company)
    normalized_title = normalize_text(job.title)
    normalized_location = normalize_text(job.location)
    return f"{normalized_company}|{normalized_title}|{normalized_location}"


def content_hash(company: str, title: str, location: str) -> str:
    key = "|".join(
        [
            normalize_text(company),
            normalize_text(title),
            normalize_text(location),
        ]
    )
    return sha256(key.encode("utf-8")).hexdigest()


def job_content_hash(job: JobPosting) -> str:
    return content_hash(job.company, job.title, job.location)
