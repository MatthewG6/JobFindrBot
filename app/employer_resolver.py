import re

from app.job_links import job_link_role, validate_manual_application_url
from app.storage import JobStorage


COMPANY_SUFFIXES = frozenset(
    {"co", "company", "corp", "corporation", "inc", "incorporated", "llc"}
)
LOCATION_ALIASES = {
    "mn": "minnesota",
    "us": "united states",
    "usa": "united states",
}


def normalized_words(value: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def normalize_company(value: object) -> str:
    words = normalized_words(value)
    while words and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_title(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def normalize_location(value: object) -> str:
    words = [LOCATION_ALIASES.get(word, word) for word in normalized_words(value)]
    return " ".join(words)


def locations_compatible(first: object, second: object) -> bool:
    first_location = normalize_location(first)
    second_location = normalize_location(second)
    if not first_location or not second_location:
        return False
    return first_location == second_location


def postings_match(first: dict, second: dict) -> bool:
    return (
        normalize_company(first.get("company"))
        == normalize_company(second.get("company"))
        and normalize_title(first.get("title"))
        == normalize_title(second.get("title"))
        and locations_compatible(first.get("location"), second.get("location"))
    )


def official_urls_by_job(
    storage: JobStorage,
) -> tuple[dict[int, set[str]], list[dict[str, object]]]:
    urls: dict[int, set[str]] = {}
    errors: list[dict[str, object]] = []
    for link in storage.list_job_links():
        if link.get("role") != "official":
            continue
        job_id = link.get("job_id")
        url = link.get("url")
        if isinstance(job_id, int) and isinstance(url, str):
            try:
                validated_url = validate_manual_application_url(url)
            except ValueError:
                errors.append(
                    {"job_id": job_id, "error_type": "InvalidOfficialLink"}
                )
                continue
            urls.setdefault(job_id, set()).add(validated_url)
    return urls, errors


def ensure_job_link_provenance(storage: JobStorage, jobs: list[dict]) -> None:
    existing = {
        (link.get("job_id"), link.get("url"))
        for link in storage.list_job_links()
    }
    for job in jobs:
        key = (job["id"], job.get("url"))
        if key in existing or not isinstance(job.get("url"), str):
            continue
        storage.add_job_link(
            job["id"],
            url=job["url"],
            source=str(job.get("source") or "unknown"),
            source_job_id=job.get("source_job_id"),
            role=job_link_role(job.get("source"), job["url"]),
        )


def resolve_employer_sites(storage: JobStorage) -> dict:
    jobs = storage.list_jobs()
    ensure_job_link_provenance(storage, jobs)
    official_urls, link_errors = official_urls_by_job(storage)
    official_jobs = [job for job in jobs if official_urls.get(job["id"])]
    summary = {
        "jobs_considered": 0,
        "jobs_resolved": 0,
        "jobs_already_resolved": 0,
        "jobs_pending": 0,
        "jobs_manual_required": 0,
        "errors": link_errors,
    }

    for job in jobs:
        if job.get("resolution_status") == "resolved" and job.get(
            "application_url"
        ):
            try:
                validate_manual_application_url(job["application_url"])
            except ValueError:
                storage.update_job_resolution(
                    job["id"],
                    status="manual_required",
                    application_url=None,
                    method="invalid_existing_application_url",
                    confidence=None,
                )
                summary["jobs_considered"] += 1
                summary["jobs_manual_required"] += 1
                summary["errors"].append(
                    {
                        "job_id": job["id"],
                        "error_type": "InvalidApplicationUrl",
                    }
                )
                continue
            else:
                summary["jobs_already_resolved"] += 1
                continue
        summary["jobs_considered"] += 1
        attached_urls = official_urls.get(job["id"], set())
        if len(attached_urls) == 1:
            storage.update_job_resolution(
                job["id"],
                status="resolved",
                application_url=next(iter(attached_urls)),
                method="captured_official_link",
                confidence=1.0,
            )
            summary["jobs_resolved"] += 1
            continue
        if len(attached_urls) > 1:
            storage.update_job_resolution(
                job["id"],
                status="manual_required",
                application_url=None,
                method="multiple_official_links",
                confidence=None,
            )
            summary["jobs_manual_required"] += 1
            continue

        matched_urls = {
            url
            for candidate in official_jobs
            if candidate["id"] != job["id"] and postings_match(job, candidate)
            for url in official_urls[candidate["id"]]
        }
        if len(matched_urls) == 1:
            storage.update_job_resolution(
                job["id"],
                status="resolved",
                application_url=next(iter(matched_urls)),
                method="exact_official_posting_match",
                confidence=0.95,
            )
            summary["jobs_resolved"] += 1
        elif len(matched_urls) > 1:
            storage.update_job_resolution(
                job["id"],
                status="manual_required",
                application_url=None,
                method="ambiguous_official_posting_match",
                confidence=None,
            )
            summary["jobs_manual_required"] += 1
        else:
            if job.get("resolution_status") == "manual_required":
                summary["jobs_manual_required"] += 1
            else:
                summary["jobs_pending"] += 1
    return summary


def set_manual_application_url(
    storage: JobStorage,
    job_id: int,
    application_url: object,
) -> dict:
    url = validate_manual_application_url(application_url)
    with storage.transaction():
        if storage.get_job(job_id) is None:
            raise ValueError("Job does not exist")
        storage.add_job_link(
            job_id,
            url=url,
            source="manual",
            role="official",
        )
        return storage.update_job_resolution(
            job_id,
            status="resolved",
            application_url=url,
            method="manual_handoff",
            confidence=1.0,
        )
