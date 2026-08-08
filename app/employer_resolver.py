from datetime import UTC, datetime, timedelta
import re
import socket
from typing import Callable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.job_links import (
    hostname_matches,
    job_link_role,
    public_https_url,
    validate_manual_application_url,
)
from app.storage import JobStorage


COMPANY_SUFFIXES = frozenset(
    {"co", "company", "corp", "corporation", "inc", "incorporated", "llc"}
)
LOCATION_ALIASES = {
    "mn": "minnesota",
    "us": "united states",
    "usa": "united states",
}
PROVIDER_DESTINATION_DOMAINS = {
    "adzuna": ("adzuna.com",),
    "himalayas": ("himalayas.app",),
    "remotive": ("remotive.com",),
}
PROVIDER_APPLY_LABELS = {
    "adzuna": frozenset({"apply", "apply for this job", "apply now"}),
    "himalayas": frozenset({"apply", "apply for this job", "apply now"}),
    "remotive": frozenset({"apply for this position"}),
}
PROVIDER_RETRY_INTERVAL = timedelta(hours=6)
PROVIDER_REQUEST_TIMEOUT = 5
DEFAULT_PROVIDER_BATCH_LIMIT = 5


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


def provider_or_official_url(source: str, value: object) -> str:
    url = public_https_url(value)
    if url is None:
        raise ValueError("Provider destination must be a public HTTPS URL")
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    domains = PROVIDER_DESTINATION_DOMAINS.get(source)
    if domains is None:
        raise ValueError("Provider destination source is unsupported")
    if hostname_matches(hostname, domains):
        return url
    return validate_manual_application_url(url)


def provider_apply_urls(source: str, html: str, source_url: str) -> set[str]:
    labels = PROVIDER_APPLY_LABELS.get(source, frozenset())
    if not labels:
        return set()
    candidates = set()
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a", href=True):
        label = " ".join(link.get_text(" ", strip=True).casefold().split())
        if label not in labels:
            continue
        try:
            candidates.add(
                validate_manual_application_url(
                    urljoin(source_url, str(link["href"]).strip())
                )
            )
        except ValueError:
            continue
    return candidates


def provider_resolution_due(job: dict, now: datetime) -> bool:
    if job.get("resolution_error_type") == "DynamicPageRequired":
        return False
    value = job.get("resolution_next_attempt_at")
    if not isinstance(value, str):
        return True
    try:
        next_attempt = datetime.fromisoformat(value)
    except ValueError:
        return True
    if next_attempt.tzinfo is None:
        return True
    return now >= next_attempt.astimezone(UTC)


def provider_attempt_ids(
    jobs: list[dict],
    now: datetime,
    limit: int,
) -> list[int]:
    queues = {
        source: [
            job["id"]
            for job in jobs
            if job.get("source") == source
            and job.get("resolution_status") == "pending"
            and provider_resolution_due(job, now)
        ]
        for source in PROVIDER_DESTINATION_DOMAINS
    }
    selected = []
    offset = 0
    while len(selected) < limit:
        added = False
        for source in PROVIDER_DESTINATION_DOMAINS:
            queue = queues[source]
            if offset < len(queue):
                selected.append(queue[offset])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        offset += 1
    return selected


def resolve_provider_destination(
    job: dict,
    *,
    get: Callable | None,
    dns_resolver: Callable,
) -> dict[str, object]:
    source = str(job.get("source") or "").strip().lower()
    url = job.get("url")
    if source not in PROVIDER_DESTINATION_DOMAINS:
        return {"outcome": "unsupported"}

    from app.job_enrichment import (
        EnrichmentHTTPStatusError,
        EnrichmentRequestError,
        EnrichmentTerminalRequestError,
        request_static_html,
        validate_public_dns,
    )

    try:
        html, final_url = request_static_html(
            str(url or ""),
            timeout=PROVIDER_REQUEST_TIMEOUT,
            get=get,
            resolver=dns_resolver,
            url_validator=lambda value: provider_or_official_url(source, value),
        )
    except EnrichmentTerminalRequestError as error:
        return {
            "outcome": "manual_required",
            "error_type": type(error).__name__,
        }
    except EnrichmentHTTPStatusError as error:
        if error.status_code in {401, 403}:
            return {
                "outcome": "dynamic_required",
                "error_type": "DynamicPageRequired",
            }
        return {"outcome": "failed", "error_type": type(error).__name__}
    except EnrichmentRequestError as error:
        return {"outcome": "failed", "error_type": type(error).__name__}
    except ValueError:
        return {"outcome": "manual_required", "error_type": "InvalidRedirect"}

    try:
        official_url = validate_manual_application_url(final_url)
    except ValueError:
        official_url = None
    if official_url is not None:
        return {
            "outcome": "resolved",
            "url": official_url,
            "method": "provider_redirect",
            "confidence": 0.95,
        }

    candidates = {
        candidate
        for candidate in provider_apply_urls(source, html, final_url)
        if _has_public_dns(candidate, dns_resolver, validate_public_dns)
    }
    if len(candidates) == 1:
        return {
            "outcome": "resolved",
            "url": next(iter(candidates)),
            "method": "provider_apply_link",
            "confidence": 0.9,
        }
    if len(candidates) > 1:
        return {"outcome": "ambiguous", "error_type": "AmbiguousApplyLink"}
    return {"outcome": "dynamic_required", "error_type": "DynamicPageRequired"}


def _has_public_dns(
    url: str,
    dns_resolver: Callable,
    validator: Callable,
) -> bool:
    try:
        validator(url, dns_resolver)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


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


def resolve_employer_sites(
    storage: JobStorage,
    *,
    destination_get: Callable | None = None,
    dns_resolver: Callable = socket.getaddrinfo,
    now: datetime | None = None,
    provider_batch_limit: int = DEFAULT_PROVIDER_BATCH_LIMIT,
) -> dict:
    if provider_batch_limit < 0:
        raise ValueError("Provider resolution batch limit cannot be negative")
    now = now or datetime.now(UTC)
    jobs = storage.list_jobs()
    provider_attempt_order = provider_attempt_ids(
        jobs,
        now,
        provider_batch_limit,
    )
    provider_jobs_selected = set(provider_attempt_order)
    jobs_by_id = {job["id"]: job for job in jobs}
    selected_jobs = [jobs_by_id[job_id] for job_id in provider_attempt_order]
    processing_jobs = selected_jobs + [
        job for job in jobs if job["id"] not in provider_jobs_selected
    ]
    ensure_job_link_provenance(storage, jobs)
    official_urls, link_errors = official_urls_by_job(storage)
    official_jobs = [job for job in jobs if official_urls.get(job["id"])]
    summary = {
        "jobs_considered": 0,
        "jobs_resolved": 0,
        "jobs_already_resolved": 0,
        "jobs_pending": 0,
        "jobs_manual_required": 0,
        "provider_jobs_attempted": 0,
        "provider_jobs_resolved": 0,
        "provider_jobs_deferred": 0,
        "provider_jobs_dynamic_required": 0,
        "provider_jobs_ambiguous": 0,
        "provider_jobs_manual_required": 0,
        "provider_jobs_failed": 0,
        "errors": link_errors,
    }

    for job in processing_jobs:
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
            source = str(job.get("source") or "").strip().lower()
            if (
                source in PROVIDER_DESTINATION_DOMAINS
                and job.get("resolution_status") != "manual_required"
            ):
                if job["id"] not in provider_jobs_selected:
                    summary["provider_jobs_deferred"] += 1
                    summary["jobs_pending"] += 1
                    continue
                result = resolve_provider_destination(
                    job,
                    get=destination_get,
                    dns_resolver=dns_resolver,
                )
                summary["provider_jobs_attempted"] += 1
                outcome = result["outcome"]
                error_type = result.get("error_type")
                next_attempt_at = (
                    now + PROVIDER_RETRY_INTERVAL
                    if outcome == "failed"
                    else None
                )
                storage.record_job_resolution_attempt(
                    job["id"],
                    attempted_at=now,
                    next_attempt_at=next_attempt_at,
                    error_type=(str(error_type) if error_type else None),
                )
                if outcome == "resolved":
                    resolved_url = str(result["url"])
                    with storage.transaction():
                        storage.add_job_link(
                            job["id"],
                            url=resolved_url,
                            source="employer_resolver",
                            source_job_id=job.get("source_job_id"),
                            role="official",
                        )
                        storage.update_job_resolution(
                            job["id"],
                            status="resolved",
                            application_url=resolved_url,
                            method=str(result["method"]),
                            confidence=float(result["confidence"]),
                        )
                    official_urls.setdefault(job["id"], set()).add(resolved_url)
                    official_jobs.append(job)
                    summary["jobs_resolved"] += 1
                    summary["provider_jobs_resolved"] += 1
                    continue
                if outcome in {"ambiguous", "manual_required"}:
                    storage.update_job_resolution(
                        job["id"],
                        status="manual_required",
                        application_url=None,
                        method=(
                            "ambiguous_provider_apply_link"
                            if outcome == "ambiguous"
                            else "provider_destination_unavailable"
                        ),
                        confidence=None,
                    )
                    summary["jobs_manual_required"] += 1
                    if outcome == "ambiguous":
                        summary["provider_jobs_ambiguous"] += 1
                    else:
                        summary["provider_jobs_manual_required"] += 1
                    continue
                if outcome == "dynamic_required":
                    storage.update_job_resolution(
                        job["id"],
                        status="pending",
                        application_url=None,
                        method="provider_dynamic_required",
                        confidence=None,
                    )
                    summary["provider_jobs_dynamic_required"] += 1
                    summary["jobs_pending"] += 1
                    continue
                summary["provider_jobs_failed"] += 1
                summary["jobs_pending"] += 1
                continue
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
        storage.update_job_resolution(
            job_id,
            status="resolved",
            application_url=url,
            method="manual_handoff",
            confidence=1.0,
        )
        return storage.update_job_enrichment(
            job_id,
            {
                "enrichment_status": "pending",
                "enrichment_method": None,
                "enrichment_source_url": None,
                "enrichment_version": None,
                "enriched_at": None,
                "enrichment_last_attempt_at": None,
                "enrichment_next_attempt_at": None,
                "enrichment_error_type": None,
            },
        )
