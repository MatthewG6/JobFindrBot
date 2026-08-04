from datetime import UTC, datetime
import html
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import ValidationError
import requests

from app.models import JobPosting


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMPLOYER_WATCHLIST_PATH = (
    PROJECT_ROOT / "config" / "employer_watchlist.json"
)
REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"
ADZUNA_API_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"
USAJOBS_API_URL = "https://data.usajobs.gov/api/search"
GREENHOUSE_API_ROOT = "https://boards-api.greenhouse.io/v1/boards"
LEVER_API_ROOT = "https://api.lever.co/v0/postings"
SITE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
TARGET_TITLE = re.compile(
    r"(?:software|developer|front[ -]?end|full[ -]?stack|"
    r"application (?:developer|engineer)|cloud engineer|devops|"
    r"site reliability|(?:back[ -]?end|platform|web|react|python|java) engineer)",
    re.IGNORECASE,
)
TARGET_LOCATION = re.compile(
    r"(?:remote|united states|u\.s\.|usa|americas|minnesota|"
    r"minneapolis|saint paul|st\. paul|rochester|\bmn\b)",
    re.IGNORECASE,
)


class EmployerWatchlistError(ValueError):
    pass


class SourceRequestError(RuntimeError):
    pass


class ParsedJobs(list[JobPosting]):
    def __init__(
        self,
        jobs=(),
        *,
        records_received: int = 0,
        records_filtered: int = 0,
        records_rejected: int = 0,
    ) -> None:
        super().__init__(jobs)
        self.records_received = records_received
        self.records_filtered = records_filtered
        self.records_rejected = records_rejected

    def extend_batch(self, batch: list[JobPosting]) -> None:
        self.extend(batch)
        self.records_received += getattr(batch, "records_received", len(batch))
        self.records_filtered += getattr(batch, "records_filtered", 0)
        self.records_rejected += getattr(batch, "records_rejected", 0)


def clean_html(value: object) -> str:
    text = html.unescape(str(value or ""))
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def parse_source_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def parse_epoch_milliseconds(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def trusted_provider_url(
    url: object,
    provider_domains: tuple[str, ...],
    *,
    upgrade_http: bool = False,
) -> str:
    value = str(url or "").strip()
    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower()
    if parts.scheme == "http" and upgrade_http:
        parts = parts._replace(scheme="https")
        value = urlunsplit(parts)
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in provider_domains
        )
    ):
        raise ValueError("Job URL is not on the expected HTTPS provider")
    return value


def request_json(source: str, url: str, **kwargs):
    try:
        response = requests.get(url, allow_redirects=False, **kwargs)
        status_code = getattr(response, "status_code", 200)
        if 300 <= status_code < 400 or getattr(response, "is_redirect", False):
            raise SourceRequestError(f"{source} request was redirected")
        response.raise_for_status()
        return response.json()
    except SourceRequestError:
        raise
    except requests.RequestException as error:
        raise SourceRequestError(
            f"{source} request failed ({type(error).__name__})"
        ) from None
    except ValueError:
        raise SourceRequestError(f"{source} returned invalid JSON") from None


def required_text(item: dict, key: str) -> str:
    raw_value = item.get(key)
    if not isinstance(raw_value, str):
        raise ValueError(f"Job record has invalid {key}")
    value = raw_value.strip()
    if not value:
        raise ValueError(f"Job record is missing {key}")
    return value


def optional_text(value: object, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Job record has an invalid text field")
    return value.strip() or default


def source_identifier(value: object, *, required: bool = False) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        if value is None and not required:
            return None
        raise ValueError("Job record has an invalid identifier")
    identifier = str(value).strip()
    if not identifier:
        if required:
            raise ValueError("Job record is missing an identifier")
        return None
    return identifier


def target_location(location: str) -> bool:
    return TARGET_LOCATION.search(location) is not None


def parse_remotive_jobs(payload: dict) -> ParsedJobs:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    jobs = ParsedJobs(records_received=len(items))
    for item in items:
        if not isinstance(item, dict):
            jobs.records_rejected += 1
            continue
        try:
            location = optional_text(
                item.get("candidate_required_location"),
                "Remote",
            )
            jobs.append(
                JobPosting(
                    title=required_text(item, "title"),
                    company=required_text(item, "company_name"),
                    location=(
                        "Remote"
                        if location.casefold() == "remote"
                        else f"Remote: {location}"
                    ),
                    url=trusted_provider_url(
                        item.get("url"),
                        ("remotive.com",),
                    ),
                    source="remotive",
                    description=clean_html(item.get("description")),
                    source_job_id=source_identifier(item.get("id")),
                    salary_text=optional_text(item.get("salary")),
                    posted_at=parse_source_date(item.get("publication_date")),
                )
            )
        except (ValidationError, ValueError):
            jobs.records_rejected += 1
            continue
    return jobs


def fetch_remotive_jobs(
    timeout: int = 20,
    limit: int = 100,
) -> ParsedJobs:
    payload = request_json(
        "remotive",
        REMOTIVE_API_URL,
        params={"category": "software-dev", "limit": limit},
        headers={"User-Agent": "JobFindrBot/1.0 (personal job search)"},
        timeout=timeout,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Remotive returned an invalid response")
    return parse_remotive_jobs(payload)


def adzuna_salary(item: dict) -> str | None:
    minimum = item.get("salary_min")
    maximum = item.get("salary_max")
    if not isinstance(minimum, (int, float)) and not isinstance(
        maximum, (int, float)
    ):
        return None
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        return f"${minimum:,.0f} - ${maximum:,.0f} a year"
    value = minimum if isinstance(minimum, (int, float)) else maximum
    return f"${value:,.0f} a year"


def parse_adzuna_jobs(payload: dict) -> ParsedJobs:
    items = payload.get("results", []) if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    jobs = ParsedJobs(records_received=len(items))
    for item in items:
        if not isinstance(item, dict):
            jobs.records_rejected += 1
            continue
        company_data = item.get("company") or {}
        location_data = item.get("location") or {}
        if not isinstance(company_data, dict) or not isinstance(
            location_data, dict
        ):
            jobs.records_rejected += 1
            continue
        try:
            location = optional_text(
                location_data.get("display_name"),
                "Minnesota",
            )
            jobs.append(
                JobPosting(
                    title=required_text(item, "title"),
                    company=required_text(company_data, "display_name"),
                    location=location,
                    url=trusted_provider_url(
                        item.get("redirect_url"),
                        ("adzuna.com",),
                        upgrade_http=True,
                    ),
                    source="adzuna",
                    description=clean_html(item.get("description")),
                    source_job_id=source_identifier(item.get("id")),
                    salary_text=adzuna_salary(item),
                    posted_at=parse_source_date(item.get("created")),
                )
            )
        except (ValidationError, ValueError):
            jobs.records_rejected += 1
            continue
    return jobs


def fetch_adzuna_jobs(
    app_id: str,
    app_key: str,
    timeout: int = 20,
) -> ParsedJobs:
    payload = request_json(
        "adzuna",
        ADZUNA_API_URL,
        params={
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "what": "software engineer",
            "where": "Minnesota",
            "sort_by": "date",
            "max_days_old": 7,
            "full_time": 1,
            "content-type": "application/json",
        },
        headers={"User-Agent": "JobFindrBot/1.0 (personal job search)"},
        timeout=timeout,
    )
    if not isinstance(payload, dict) or not isinstance(
        payload.get("results"), list
    ):
        raise ValueError("Adzuna returned an invalid response")
    return parse_adzuna_jobs(payload)


def usajobs_salary(descriptor: dict) -> str | None:
    ranges = descriptor.get("PositionRemuneration") or []
    if (
        not isinstance(ranges, list)
        or not ranges
        or not isinstance(ranges[0], dict)
    ):
        return None
    salary = ranges[0]
    minimum = salary.get("MinimumRange")
    maximum = salary.get("MaximumRange")
    interval = str(salary.get("RateIntervalCode") or "Per Year")
    if not minimum and not maximum:
        return None
    if minimum and maximum:
        return f"${minimum} - ${maximum} {interval}"
    return f"${minimum or maximum} {interval}"


def parse_usajobs_jobs(payload: dict) -> ParsedJobs:
    search_result = (
        payload.get("SearchResult") or {} if isinstance(payload, dict) else {}
    )
    if not isinstance(search_result, dict):
        return ParsedJobs()
    items = search_result.get("SearchResultItems", [])
    items = items if isinstance(items, list) else []
    jobs = ParsedJobs(records_received=len(items))
    for item in items:
        if not isinstance(item, dict):
            jobs.records_rejected += 1
            continue
        descriptor = item.get("MatchedObjectDescriptor") or {}
        if not isinstance(descriptor, dict):
            jobs.records_rejected += 1
            continue
        try:
            organization = optional_text(
                descriptor.get("OrganizationName")
                or descriptor.get("DepartmentName"),
                "United States Government",
            )
            location = optional_text(
                descriptor.get("PositionLocationDisplay"),
                "United States",
            )
            jobs.append(
                JobPosting(
                    title=required_text(descriptor, "PositionTitle"),
                    company=organization,
                    location=location,
                    url=trusted_provider_url(
                        descriptor.get("PositionURI"),
                        ("usajobs.gov",),
                    ),
                    source="usajobs",
                    description=clean_html(
                        descriptor.get("QualificationSummary")
                    ),
                    source_job_id=source_identifier(
                        item.get("MatchedObjectId")
                        or descriptor.get("PositionID")
                    ),
                    salary_text=usajobs_salary(descriptor),
                    posted_at=parse_source_date(
                        descriptor.get("PublicationStartDate")
                    ),
                )
            )
        except (ValidationError, ValueError):
            jobs.records_rejected += 1
            continue
    return jobs


def fetch_usajobs_jobs(
    api_key: str,
    user_agent: str,
    timeout: int = 20,
) -> ParsedJobs:
    payload = request_json(
        "usajobs",
        USAJOBS_API_URL,
        params={
            "Keyword": "Software",
            "LocationName": "Minnesota",
            "HiringPath": "public;graduates",
            "DatePosted": 7,
            "ResultsPerPage": 100,
            "Fields": "Full",
            "SortField": "opendate",
            "SortDirection": "Desc",
        },
        headers={
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
        },
        timeout=timeout,
    )
    search_result = payload.get("SearchResult") if isinstance(payload, dict) else None
    if not isinstance(search_result, dict) or not isinstance(
        search_result.get("SearchResultItems"), list
    ):
        raise ValueError("USAJOBS returned an invalid response")
    return parse_usajobs_jobs(payload)


def load_employer_watchlist(
    watchlist_path: Path = DEFAULT_EMPLOYER_WATCHLIST_PATH,
) -> list[dict[str, str]]:
    try:
        entries = json.loads(watchlist_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmployerWatchlistError(
            "Employer watchlist is not readable JSON"
        ) from error
    if not isinstance(entries, list):
        raise EmployerWatchlistError("Employer watchlist must be a JSON array")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EmployerWatchlistError("Watchlist entries must be objects")
        provider = str(entry.get("provider") or "").strip().lower()
        site = str(entry.get("site") or "").strip()
        company = str(entry.get("company") or "").strip()
        key = (provider, site.lower())
        if (
            provider not in {"greenhouse", "lever"}
            or SITE_TOKEN.fullmatch(site) is None
            or not company
            or key in seen
        ):
            raise EmployerWatchlistError("Watchlist entry is invalid or duplicate")
        seen.add(key)
        normalized.append(
            {"provider": provider, "site": site, "company": company}
        )
    return normalized


def parse_greenhouse_jobs(
    payload: dict,
    company: str,
    site: str,
) -> ParsedJobs:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    jobs = ParsedJobs(records_received=len(items))
    company = company.strip()
    if not company or SITE_TOKEN.fullmatch(site) is None:
        jobs.records_rejected = len(items)
        return jobs
    for item in items:
        if not isinstance(item, dict):
            jobs.records_rejected += 1
            continue
        try:
            title = required_text(item, "title")
        except ValueError:
            jobs.records_rejected += 1
            continue
        if TARGET_TITLE.search(title) is None:
            jobs.records_filtered += 1
            continue
        location_data = item.get("location")
        if location_data is None:
            location_data = {}
        if not isinstance(location_data, dict):
            jobs.records_rejected += 1
            continue
        try:
            location = optional_text(location_data.get("name"), "Unspecified")
        except ValueError:
            jobs.records_rejected += 1
            continue
        if not target_location(location):
            jobs.records_filtered += 1
            continue
        try:
            job_id = source_identifier(item.get("id"), required=True)
            jobs.append(
                JobPosting(
                    title=title,
                    company=company,
                    location=location,
                    url=(
                        "https://job-boards.greenhouse.io/"
                        f"{quote(site, safe='')}/jobs/"
                        f"{quote(job_id, safe='')}"
                    ),
                    source="greenhouse",
                    description=clean_html(item.get("content")),
                    source_job_id=job_id,
                    posted_at=parse_source_date(item.get("updated_at")),
                )
            )
        except (ValidationError, ValueError):
            jobs.records_rejected += 1
            continue
    return jobs


def parse_lever_jobs(payload: list, company: str) -> ParsedJobs:
    items = payload if isinstance(payload, list) else []
    jobs = ParsedJobs(records_received=len(items))
    company = company.strip()
    if not company:
        jobs.records_rejected = len(items)
        return jobs
    for item in items:
        if not isinstance(item, dict):
            jobs.records_rejected += 1
            continue
        try:
            title = required_text(item, "text")
        except ValueError:
            jobs.records_rejected += 1
            continue
        if TARGET_TITLE.search(title) is None:
            jobs.records_filtered += 1
            continue
        categories = item.get("categories")
        if categories is None:
            categories = {}
        if not isinstance(categories, dict):
            jobs.records_rejected += 1
            continue
        try:
            location = optional_text(
                categories.get("location"),
                "Unspecified",
            )
        except ValueError:
            jobs.records_rejected += 1
            continue
        if not target_location(location):
            jobs.records_filtered += 1
            continue
        description = (
            item.get("descriptionPlain")
            or item.get("openingPlain")
            or item.get("description")
            or ""
        )
        try:
            jobs.append(
                JobPosting(
                    title=title,
                    company=company,
                    location=location,
                    url=trusted_provider_url(
                        item.get("hostedUrl"),
                        ("lever.co",),
                    ),
                    source="lever",
                    description=clean_html(description),
                    source_job_id=source_identifier(item.get("id")),
                    posted_at=parse_epoch_milliseconds(item.get("createdAt")),
                )
            )
        except (ValidationError, ValueError):
            jobs.records_rejected += 1
            continue
    return jobs


def fetch_employer_board(
    entry: dict[str, str],
    timeout: int = 20,
) -> ParsedJobs:
    provider = entry["provider"]
    site = quote(entry["site"], safe="")
    if provider == "greenhouse":
        payload = request_json(
            "greenhouse",
            f"{GREENHOUSE_API_ROOT}/{site}/jobs",
            params={"content": "true"},
            headers={"User-Agent": "JobFindrBot/1.0 (personal job search)"},
            timeout=timeout,
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("jobs"), list
        ):
            raise ValueError("Greenhouse returned an invalid response")
        return parse_greenhouse_jobs(payload, entry["company"], entry["site"])

    if provider != "lever":
        raise ValueError("Employer board provider is not supported")

    payload = request_json(
        "lever",
        f"{LEVER_API_ROOT}/{site}",
        params={"mode": "json"},
        headers={"User-Agent": "JobFindrBot/1.0 (personal job search)"},
        timeout=timeout,
    )
    if not isinstance(payload, list):
        raise ValueError("Lever returned an invalid response")
    return parse_lever_jobs(payload, entry["company"])
