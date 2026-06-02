from app.models import JobPosting


def job_key(job: JobPosting) -> str:
    normalized_company = job.company.strip().lower()
    normalized_title = job.title.strip().lower()
    normalized_location = job.location.strip().lower()
    return f"{normalized_company}|{normalized_title}|{normalized_location}"
