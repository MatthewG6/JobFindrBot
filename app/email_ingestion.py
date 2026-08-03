import re
from email.utils import parseaddr
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from app.ingestion import ingest_job
from app.models import JobAlertEmail, JobPosting
from app.storage import JobStorage


LINKEDIN_SENDER = "jobalerts-noreply@linkedin.com"
INDEED_SENDER = "donotreply@jobalert.indeed.com"
LINKEDIN_LABEL = "Jobbot-LinkedIn"
INDEED_LABEL = "Jobbot-Indeed"
LINKEDIN_SEPARATOR = re.compile(r"^-{20,}\s*$", re.MULTILINE)
LINKEDIN_JOB_ID = re.compile(r"/jobs/view/(\d+)")
LINKEDIN_JOB_COUNT = re.compile(
    r"^(\d+)\s+new\s+jobs?\s+match(?:es)?\s+your\s+preferences\.?$",
    re.IGNORECASE,
)
LINKEDIN_LOCATION = re.compile(
    r"(?:\bremote\b|\bhybrid\b|\bunited states\b|"
    r"\bminnesota\b|\bwisconsin\b|,\s*[A-Z]{2}(?:\b|$)|\barea\b)",
    re.IGNORECASE,
)
INDEED_ALERT_HEADING = re.compile(
    r"^(\d+)\s+new\s+(.+?)\s+jobs?\s+in\s+(.+?)\s*$",
    re.IGNORECASE,
)
POSTED_TEXT = re.compile(
    r"^(?:just posted|today|\d+\+?\s+(?:hours?|days?)\s+ago)$",
    re.IGNORECASE,
)
SALARY_TEXT = re.compile(
    r"(?:\$[\d,]+|\b(?:an hour|a year|per hour|per year)\b)",
    re.IGNORECASE,
)
INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class UnsupportedJobAlertEmail(ValueError):
    pass


class UntrustedJobAlertEmail(ValueError):
    pass


class JobAlertParseError(ValueError):
    pass


class UntrustedSourceUrl(ValueError):
    pass


def email_body_text(message: JobAlertEmail) -> str:
    if message.text_body.strip():
        return message.text_body
    raise JobAlertParseError(
        f"Message {message.message_id} has no plain-text MIME body"
    )


def provider_host(url: str, provider_domain: str) -> bool:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        hostname == provider_domain
        or hostname.endswith(f".{provider_domain}")
    )


def linkedin_alert_query(text: str) -> str | None:
    for line in text.splitlines():
        clean_line = line.strip()
        prefix = "Your job alert for "
        if clean_line.lower().startswith(prefix.lower()):
            return clean_line[len(prefix) :].strip() or None
    return None


def canonicalize_linkedin_url(url: str) -> tuple[str, str | None]:
    if not provider_host(url, "linkedin.com"):
        raise UntrustedSourceUrl("LinkedIn job URL is not on linkedin.com")

    match = LINKEDIN_JOB_ID.search(url)
    if match is not None:
        source_job_id = match.group(1)
        return (
            f"https://www.linkedin.com/jobs/view/{source_job_id}",
            source_job_id,
        )

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), None


def parse_linkedin_jobs(message: JobAlertEmail) -> list[JobPosting]:
    text = email_body_text(message)
    alert_query = linkedin_alert_query(text)
    jobs: list[JobPosting] = []

    for section in LINKEDIN_SEPARATOR.split(text):
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        view_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.lower().startswith("view job:")
            ),
            None,
        )
        if view_index is None:
            continue

        url = lines[view_index].split(":", 1)[1].strip()
        if (
            not url
            and view_index + 1 < len(lines)
            and lines[view_index + 1].lower().startswith(("http://", "https://"))
        ):
            url = lines[view_index + 1]
        before_url = lines[:view_index]
        location_index = next(
            (
                index
                for index in range(len(before_url) - 1, 1, -1)
                if LINKEDIN_LOCATION.search(before_url[index])
            ),
            None,
        )
        if location_index is None:
            continue

        title = before_url[location_index - 2]
        company = before_url[location_index - 1]
        location = before_url[location_index]
        detail_lines = before_url[location_index + 1 :]
        canonical_url, source_job_id = canonicalize_linkedin_url(url)

        try:
            jobs.append(
                JobPosting(
                    title=title,
                    company=company,
                    location=location,
                    url=canonical_url,
                    source="linkedin_email",
                    description=" ".join(detail_lines),
                    source_job_id=source_job_id,
                    source_message_id=message.message_id,
                    alert_query=alert_query,
                    discovered_at=message.received_at,
                )
            )
        except ValidationError:
            continue

    return jobs


def indeed_alert_start(lines: list[str]) -> tuple[int, str | None, int | None]:
    for index, line in enumerate(lines):
        match = INDEED_ALERT_HEADING.match(line)
        if match is not None:
            return (
                index + 1,
                f"{match.group(2)} in {match.group(3)}",
                int(match.group(1)),
            )
    return len(lines), None, None


def split_indeed_company_location(value: str) -> tuple[str, str] | None:
    if " - " not in value:
        return None
    company, location = value.rsplit(" - ", 1)
    if not company.strip() or not location.strip():
        return None
    return company.strip(), location.strip()


def canonicalize_indeed_url(url: str) -> tuple[str | None, str | None]:
    if not provider_host(url, "indeed.com"):
        raise UntrustedSourceUrl("Indeed job URL is not on indeed.com")

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    source_job_id = next(
        (
            query[key][0]
            for key in ("jk", "vjk")
            if query.get(key) and query[key][0]
        ),
        None,
    )
    if source_job_id is None:
        return None, None
    if INDEED_JOB_ID.fullmatch(source_job_id) is None:
        raise UntrustedSourceUrl("Indeed job ID contains invalid characters")
    return (
        f"https://www.indeed.com/viewjob?jk={quote(source_job_id, safe='')}",
        source_job_id,
    )


def parse_indeed_block(
    block: list[str],
    message: JobAlertEmail,
    alert_query: str | None,
) -> JobPosting | None:
    if len(block) < 3 or not block[-1].lower().startswith(("http://", "https://")):
        return None

    company_index = None
    company_location = None
    for index in range(1, len(block) - 1):
        company_location = split_indeed_company_location(block[index])
        if company_location is not None:
            company_index = index
            break
    if company_index is None or company_location is None:
        return None

    title = block[company_index - 1]
    company, location = company_location
    detail_lines = block[company_index + 1 : -1]
    posted_text = next(
        (line for line in reversed(detail_lines) if POSTED_TEXT.match(line)),
        None,
    )
    salary_text = next(
        (line for line in detail_lines if SALARY_TEXT.search(line)),
        None,
    )
    description_lines = [line for line in detail_lines if line != posted_text]
    canonical_url, source_job_id = canonicalize_indeed_url(block[-1])
    if canonical_url is None:
        canonical_url = (
            "https://www.indeed.com/jobs?"
            + urlencode({"q": f"{title} {company}", "l": location})
        )

    try:
        return JobPosting(
            title=title,
            company=company,
            location=location,
            url=canonical_url,
            source="indeed_email",
            description=" ".join(description_lines),
            source_job_id=source_job_id,
            source_message_id=message.message_id,
            alert_query=alert_query,
            salary_text=salary_text,
            posted_text=posted_text,
            discovered_at=message.received_at,
        )
    except ValidationError:
        return None


def parse_indeed_jobs(message: JobAlertEmail) -> list[JobPosting]:
    lines = [line.strip() for line in email_body_text(message).splitlines()]
    start_index, alert_query, _ = indeed_alert_start(lines)
    jobs: list[JobPosting] = []
    block: list[str] = []

    for line in lines[start_index:]:
        if line.startswith("©"):
            break
        if not line:
            continue

        block.append(line)
        if line.lower().startswith(("http://", "https://")):
            job = parse_indeed_block(block, message, alert_query)
            if job is not None:
                jobs.append(job)
            block = []

    return jobs


def job_alert_source(message: JobAlertEmail) -> str:
    sender = parseaddr(message.sender)[1].lower()
    if sender == LINKEDIN_SENDER:
        source = "linkedin_email"
        expected_label = LINKEDIN_LABEL
        expected_from_domain = "linkedin.com"
    elif sender == INDEED_SENDER:
        source = "indeed_email"
        expected_label = INDEED_LABEL
        expected_from_domain = "jobalert.indeed.com"
    else:
        raise UnsupportedJobAlertEmail(
            f"Unsupported job alert sender: {message.sender}"
        )

    authentication = message.authentication_results.lower()
    if expected_label not in message.gmail_labels:
        raise UntrustedJobAlertEmail(
            f"Message is missing required Gmail label {expected_label}"
        )
    authentication_segments = [
        segment.strip() for segment in authentication.split(";")
    ]
    domain_pattern = re.compile(
        rf"header\.from={re.escape(expected_from_domain)}(?:\s|$)"
    )
    dmarc_passed = any(
        re.search(r"\bdmarc=pass(?:\s|$)", segment)
        and domain_pattern.search(segment)
        for segment in authentication_segments
    )
    conflicting_result = any(
        re.search(r"\bdmarc=(?!pass(?:\s|$))[^\s]+", segment)
        and domain_pattern.search(segment)
        for segment in authentication_segments
    )
    if (
        not authentication.strip().startswith("mx.google.com;")
        or not dmarc_passed
        or conflicting_result
    ):
        raise UntrustedJobAlertEmail(
            f"Message failed {expected_from_domain} DMARC verification"
        )
    return source


def expected_job_count(message: JobAlertEmail, source: str) -> int | None:
    lines = [line.strip() for line in email_body_text(message).splitlines()]
    if source == "linkedin_email":
        for line in lines:
            match = LINKEDIN_JOB_COUNT.match(line)
            if match is not None:
                return int(match.group(1))
        return None

    _, _, count = indeed_alert_start(lines)
    return count


def validate_parsed_job_count(
    message: JobAlertEmail,
    source: str,
    jobs: list[JobPosting],
) -> None:
    expected_count = expected_job_count(message, source)
    if expected_count is None:
        raise JobAlertParseError(
            f"Message {message.message_id} has no recognized job count"
        )
    if len(jobs) != expected_count:
        raise JobAlertParseError(
            f"Message {message.message_id} advertised {expected_count} jobs "
            f"but parsed {len(jobs)}"
        )


def parse_job_alert_email(message: JobAlertEmail) -> list[JobPosting]:
    source = job_alert_source(message)
    if source == "linkedin_email":
        return parse_linkedin_jobs(message)
    return parse_indeed_jobs(message)


def ingest_job_alert_email(
    message: JobAlertEmail,
    storage: JobStorage,
) -> dict:
    source = job_alert_source(message)
    jobs = parse_job_alert_email(message)
    validate_parsed_job_count(message, source, jobs)

    with storage.transaction():
        existing = storage.get_processed_email(message.message_id)
        if existing is not None:
            return {
                "processed": False,
                "already_processed": True,
                "message": existing,
                "parsed_count": existing["job_count"],
                "created_count": 0,
            }

        results = [ingest_job(job, storage) for job in jobs]
        created_count = sum(result["created"] for result in results)
        processed_email = storage.mark_email_processed(
            message_id=message.message_id,
            source=source,
            job_count=len(jobs),
            created_count=created_count,
        )
        return {
            "processed": True,
            "already_processed": False,
            "message": processed_email,
            "parsed_count": len(jobs),
            "created_count": created_count,
        }
