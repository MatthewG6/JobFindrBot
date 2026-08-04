from datetime import UTC, datetime, timedelta
import ipaddress
import json
import socket
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import certifi
from pydantic import BaseModel, ConfigDict, Field
import requests
from urllib3 import HTTPSConnectionPool
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.employer_resolver import normalize_company, normalize_title
from app.job_links import (
    job_link_role,
    public_https_url,
    validate_manual_application_url,
)
from app.models import JobPosting
from app.scoring import score_job
from app.storage import JobStorage


ENRICHMENT_VERSION = 1
MAX_DESCRIPTION_CHARS = 200_000
MIN_DESCRIPTION_CHARS = 20
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
RETRY_INTERVAL = timedelta(hours=24)


class EnrichmentError(RuntimeError):
    pass


class EnrichmentRequestError(EnrichmentError):
    pass


class EnrichmentHTTPStatusError(EnrichmentRequestError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Static page returned HTTP {status_code}")


class EnrichmentPayloadError(EnrichmentError):
    pass


class EnrichmentTerminalRequestError(EnrichmentError):
    pass


class DynamicPageRequired(EnrichmentError):
    pass


class EnrichmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        min_length=MIN_DESCRIPTION_CHARS,
        max_length=MAX_DESCRIPTION_CHARS,
    )
    salary_text: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    apply_url: str | None = None
    method: str
    source_url: str


def clean_text(value: object) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(
        " ",
        strip=True,
    )


def optional_clean_text(value: object) -> str | None:
    cleaned = clean_text(value)
    return cleaned or None


def safe_salary_text(value: object) -> str | None:
    if not isinstance(value, dict):
        return optional_clean_text(value)
    currency = str(value.get("currency") or "").strip().upper()
    interval = str(value.get("interval") or "").strip()
    minimum = value.get("min")
    maximum = value.get("max")
    if isinstance(minimum, bool) or isinstance(maximum, bool):
        return None
    if not isinstance(minimum, (int, float)) and not isinstance(
        maximum,
        (int, float),
    ):
        return None
    symbol = "$" if currency == "USD" else f"{currency} " if currency else ""
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        amount = f"{symbol}{minimum:,.0f} - {symbol}{maximum:,.0f}"
    else:
        amount = f"{symbol}{minimum if minimum is not None else maximum:,.0f}"
    return f"{amount} {interval}".strip()


def json_ld_salary_text(value: object) -> str | None:
    if not isinstance(value, dict):
        return safe_salary_text(value)
    amount = value.get("value")
    if not isinstance(amount, dict):
        return safe_salary_text(value)
    return safe_salary_text(
        {
            "currency": value.get("currency"),
            "interval": amount.get("unitText"),
            "min": amount.get("minValue") or amount.get("value"),
            "max": amount.get("maxValue") or amount.get("value"),
        }
    )


def validate_payload_identity(
    job: dict,
    *,
    title: object,
    company: object | None = None,
) -> None:
    if normalize_title(title) != normalize_title(job.get("title")):
        raise EnrichmentPayloadError("Enrichment title does not match the job")
    if company is not None and normalize_company(company) != normalize_company(
        job.get("company")
    ):
        raise EnrichmentPayloadError("Enrichment company does not match the job")


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    path = [unquote(item) for item in parts.path.split("/") if item]
    if hostname != "job-boards.greenhouse.io" or len(path) != 3:
        return None
    if path[1] != "jobs" or not path[0] or not path[2].isdigit():
        return None
    return path[0], path[2]


def parse_lever_url(url: str) -> tuple[str, str, str] | None:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    path = [unquote(item) for item in parts.path.split("/") if item]
    if hostname not in {"jobs.lever.co", "jobs.eu.lever.co"} or len(path) != 2:
        return None
    if not path[0] or not path[1]:
        return None
    api_host = "api.eu.lever.co" if hostname == "jobs.eu.lever.co" else "api.lever.co"
    return api_host, path[0], path[1]


def request_json(
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 20,
    get: Callable | None = None,
) -> object:
    request_get = get or requests.get
    response = None
    try:
        response = request_get(
            url,
            params=params,
            headers={"User-Agent": "Jobbot/1.0 (personal job search)"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        status_code = getattr(response, "status_code", 200)
        if 300 <= status_code < 400 or getattr(response, "is_redirect", False):
            raise EnrichmentRequestError("Enrichment API redirected")
        if status_code in {404, 410}:
            raise EnrichmentTerminalRequestError(
                f"Enrichment API returned HTTP {status_code}"
            )
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                raise EnrichmentRequestError(
                    "Enrichment response has an invalid content length"
                ) from None
            if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                raise EnrichmentRequestError("Enrichment response is too large")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise EnrichmentRequestError("Enrichment response is too large")
        return json.loads(bytes(content).decode("utf-8"))
    except EnrichmentError:
        raise
    except requests.RequestException as error:
        raise EnrichmentRequestError(
            f"Enrichment request failed ({type(error).__name__})"
        ) from None
    except ValueError:
        raise EnrichmentPayloadError("Enrichment API returned invalid JSON") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def greenhouse_payload(job: dict, get: Callable | None = None) -> EnrichmentPayload:
    application_url = validate_manual_application_url(job.get("application_url"))
    parsed = parse_greenhouse_url(application_url)
    if parsed is None:
        raise EnrichmentPayloadError("Greenhouse application URL is invalid")
    board, job_id = parsed
    api_url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{quote(board, safe='')}/jobs/{quote(job_id, safe='')}"
    )
    data = request_json(api_url, params={"content": "true"}, get=get)
    if not isinstance(data, dict) or str(data.get("id")) != job_id:
        raise EnrichmentPayloadError("Greenhouse returned the wrong posting")
    validate_payload_identity(job, title=data.get("title"))
    description = clean_text(data.get("content"))
    if not description:
        raise EnrichmentPayloadError("Greenhouse posting has no description")
    return EnrichmentPayload(
        description=description,
        method="greenhouse_api",
        source_url=api_url,
    )


def lever_payload(job: dict, get: Callable | None = None) -> EnrichmentPayload:
    application_url = validate_manual_application_url(job.get("application_url"))
    parsed = parse_lever_url(application_url)
    if parsed is None:
        raise EnrichmentPayloadError("Lever application URL is invalid")
    api_host, site, posting_id = parsed
    api_url = (
        f"https://{api_host}/v0/postings/"
        f"{quote(site, safe='')}/{quote(posting_id, safe='')}"
    )
    data = request_json(api_url, params={"mode": "json"}, get=get)
    if not isinstance(data, dict) or str(data.get("id")) != posting_id:
        raise EnrichmentPayloadError("Lever returned the wrong posting")
    validate_payload_identity(job, title=data.get("text"))
    lists = data.get("lists", [])
    if not isinstance(lists, list):
        raise EnrichmentPayloadError("Lever posting lists are invalid")
    description_parts = [
        data.get("descriptionPlain") or data.get("description"),
        *(
            item.get("content")
            for item in lists
            if isinstance(item, dict)
        ),
        data.get("additionalPlain") or data.get("additional"),
    ]
    description = " ".join(
        text for value in description_parts if (text := clean_text(value))
    )
    if not description:
        raise EnrichmentPayloadError("Lever posting has no description")
    categories = data.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    apply_url = data.get("applyUrl")
    if apply_url is not None:
        apply_url = validate_manual_application_url(apply_url)
    return EnrichmentPayload(
        description=description,
        salary_text=(
            optional_clean_text(data.get("salaryDescriptionPlain"))
            or safe_salary_text(data.get("salaryRange"))
        ),
        employment_type=optional_clean_text(categories.get("commitment")),
        workplace_type=optional_clean_text(data.get("workplaceType")),
        apply_url=apply_url,
        method="lever_api",
        source_url=api_url,
    )


def json_ld_objects(value: object) -> list[dict]:
    if isinstance(value, list):
        return [item for value_item in value for item in json_ld_objects(value_item)]
    if not isinstance(value, dict):
        return []
    objects = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        objects.extend(
            item for graph_item in graph for item in json_ld_objects(graph_item)
        )
    return objects


def is_job_posting(value: dict) -> bool:
    item_type = value.get("@type")
    if isinstance(item_type, str):
        return item_type.casefold() == "jobposting"
    if isinstance(item_type, list):
        return any(
            isinstance(item, str) and item.casefold() == "jobposting"
            for item in item_type
        )
    return False


def json_ld_company(value: dict) -> str | None:
    organization = value.get("hiringOrganization")
    if isinstance(organization, dict):
        return optional_clean_text(organization.get("name"))
    return None


def parse_job_posting_json_ld(
    html: str,
    job: dict,
    source_url: str,
) -> EnrichmentPayload:
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in json_ld_objects(value):
            if not is_job_posting(item):
                continue
            try:
                company = json_ld_company(item)
                if company is None:
                    continue
                validate_payload_identity(
                    job,
                    title=item.get("title"),
                    company=company,
                )
            except EnrichmentPayloadError:
                continue
            description = clean_text(item.get("description"))
            if description:
                matches.append((item, description))
    if len(matches) != 1:
        raise DynamicPageRequired("Page has no unique matching JobPosting data")
    item, description = matches[0]
    apply_url = item.get("url")
    if apply_url is not None:
        apply_url = validate_manual_application_url(apply_url)
    return EnrichmentPayload(
        description=description,
        salary_text=json_ld_salary_text(item.get("baseSalary")),
        employment_type=(
            ", ".join(map(str, item["employmentType"]))
            if isinstance(item.get("employmentType"), list)
            else optional_clean_text(item.get("employmentType"))
        ),
        apply_url=apply_url,
        method="jobposting_jsonld",
        source_url=source_url,
    )


def validate_public_dns(
    url: str,
    resolver: Callable = socket.getaddrinfo,
) -> tuple[str, ...]:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise EnrichmentRequestError("Enrichment URL has no hostname")
    try:
        records = resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise EnrichmentRequestError("Enrichment hostname could not be resolved") from None
    addresses = {record[4][0] for record in records if record[4]}
    try:
        unsafe = any(
            not ipaddress.ip_address(address.split("%", 1)[0]).is_global
            for address in addresses
        )
    except ValueError:
        unsafe = True
    if not addresses or unsafe:
        raise EnrichmentRequestError("Enrichment hostname is not public")
    return tuple(sorted(address.split("%", 1)[0] for address in addresses))


class PinnedHTTPSResponse:
    def __init__(self, response: object, pool: object) -> None:
        self._response = response
        self._pool = pool
        self.status_code = response.status
        self.headers = response.headers
        self.is_redirect = 300 <= response.status < 400
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise EnrichmentRequestError(
                f"Static page returned HTTP {self.status_code}"
            )

    def iter_content(self, chunk_size: int):
        yield from self._response.stream(chunk_size)

    def close(self) -> None:
        try:
            self._response.release_conn()
        finally:
            self._pool.close()


def pinned_https_get(
    url: str,
    addresses: tuple[str, ...],
    *,
    timeout: int,
    pool_factory: Callable = HTTPSConnectionPool,
) -> PinnedHTTPSResponse:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").encode("idna").decode("ascii")
    request_target = parts.path or "/"
    if parts.query:
        request_target = f"{request_target}?{parts.query}"
    last_error = None
    for address in addresses:
        pool = pool_factory(
            address,
            port=443,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            assert_hostname=hostname,
            server_hostname=hostname,
        )
        try:
            response = pool.urlopen(
                "GET",
                request_target,
                headers={
                    "Host": hostname,
                    "User-Agent": "Jobbot/1.0 (personal job search)",
                },
                redirect=False,
                preload_content=False,
                assert_same_host=False,
            )
            return PinnedHTTPSResponse(response, pool)
        except Urllib3HTTPError as error:
            last_error = error
            pool.close()
    error_type = type(last_error).__name__ if last_error is not None else "NoAddress"
    raise EnrichmentRequestError(
        f"Static page request failed ({error_type})"
    ) from None


def request_static_html(
    url: str,
    *,
    timeout: int = 20,
    get: Callable | None = None,
    resolver: Callable = socket.getaddrinfo,
    url_validator: Callable[[object], str] = validate_manual_application_url,
) -> tuple[str, str]:
    current_url = url_validator(url)
    for redirect_count in range(MAX_REDIRECTS + 1):
        addresses = validate_public_dns(current_url, resolver)
        response = None
        try:
            if get is None:
                response = pinned_https_get(
                    current_url,
                    addresses,
                    timeout=timeout,
                )
            else:
                response = get(
                    current_url,
                    headers={"User-Agent": "Jobbot/1.0 (personal job search)"},
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            status_code = getattr(response, "status_code", 200)
            if 300 <= status_code < 400:
                location = response.headers.get("Location")
                if not location or redirect_count >= MAX_REDIRECTS:
                    raise EnrichmentRequestError("Static page redirect is invalid")
                current_url = url_validator(urljoin(current_url, location))
                continue
            if status_code in {404, 410}:
                raise EnrichmentTerminalRequestError(
                    f"Static page returned HTTP {status_code}"
                )
            if status_code >= 400:
                raise EnrichmentHTTPStatusError(status_code)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise EnrichmentRequestError("Static page is not HTML")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError):
                    raise EnrichmentRequestError(
                        "Static page has an invalid content length"
                    ) from None
                if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                    raise EnrichmentRequestError("Static page is too large")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise EnrichmentRequestError("Static page is too large")
            encoding = getattr(response, "encoding", None) or "utf-8"
            html = bytes(content).decode(encoding, errors="replace")
            return html, current_url
        except EnrichmentError:
            raise
        except (requests.RequestException, Urllib3HTTPError) as error:
            raise EnrichmentRequestError(
                f"Static page request failed ({type(error).__name__})"
            ) from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    raise EnrichmentRequestError("Static page has too many redirects")


def source_record_payload(
    storage: JobStorage,
    job: dict,
) -> EnrichmentPayload | None:
    candidates = []
    for record in storage.list_job_source_records(job["id"]):
        if job_link_role(record.get("source"), record.get("url")) != "official":
            continue
        posting = record.get("posting")
        if not isinstance(posting, dict):
            continue
        description = clean_text(posting.get("description"))
        if len(description) < MIN_DESCRIPTION_CHARS:
            continue
        try:
            validate_payload_identity(
                job,
                title=posting.get("title"),
                company=posting.get("company"),
            )
        except EnrichmentPayloadError:
            continue
        candidates.append((len(description), record, posting, description))
    if not candidates:
        return None
    _, record, posting, description = max(candidates, key=lambda item: item[0])
    return EnrichmentPayload(
        description=description,
        salary_text=optional_clean_text(posting.get("salary_text")),
        employment_type=optional_clean_text(posting.get("employment_type")),
        workplace_type=optional_clean_text(posting.get("workplace_type")),
        method="captured_official_payload",
        source_url=record["url"],
    )


def fetch_enrichment_payload(
    job: dict,
    *,
    get: Callable | None = None,
    resolver: Callable = socket.getaddrinfo,
) -> EnrichmentPayload:
    application_url = validate_manual_application_url(job.get("application_url"))
    if parse_greenhouse_url(application_url) is not None:
        return greenhouse_payload(job, get=get)
    if parse_lever_url(application_url) is not None:
        return lever_payload(job, get=get)
    html, final_url = request_static_html(
        application_url,
        get=get,
        resolver=resolver,
    )
    return parse_job_posting_json_ld(html, job, final_url)


def apply_enrichment(
    storage: JobStorage,
    job: dict,
    payload: EnrichmentPayload,
    now: datetime,
) -> dict:
    updates = {
        "description": payload.description,
        "salary_text": payload.salary_text or job.get("salary_text"),
        "employment_type": (
            payload.employment_type or job.get("employment_type")
        ),
        "workplace_type": payload.workplace_type or job.get("workplace_type"),
        "apply_url": payload.apply_url or job.get("apply_url"),
        "enrichment_status": "enriched",
        "enrichment_method": payload.method,
        "enrichment_source_url": payload.source_url,
        "enrichment_version": ENRICHMENT_VERSION,
        "enriched_at": now.isoformat(),
        "enrichment_last_attempt_at": now.isoformat(),
        "enrichment_next_attempt_at": None,
        "enrichment_error_type": None,
    }
    posting_data = {**job, **updates}
    scored = score_job(JobPosting.model_validate(posting_data))
    updates.update(
        {
            "fit_score": scored.score,
            "score_reasons": scored.reasons,
            "red_flags": scored.red_flags,
        }
    )
    return storage.update_job_enrichment(job["id"], updates)


def parse_retry_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def enrich_resolved_jobs(
    storage: JobStorage,
    *,
    now: datetime | None = None,
    max_jobs: int = 10,
    fetcher: Callable[[dict], EnrichmentPayload] = fetch_enrichment_payload,
) -> dict:
    if max_jobs < 1:
        raise ValueError("Enrichment batch size must be positive")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    summary = {
        "jobs_eligible": 0,
        "jobs_attempted": 0,
        "jobs_enriched": 0,
        "jobs_already_enriched": 0,
        "jobs_deferred": 0,
        "jobs_dynamic_required": 0,
        "jobs_already_dynamic_required": 0,
        "jobs_manual_required": 0,
        "jobs_already_manual_required": 0,
        "jobs_failed": 0,
        "errors": [],
    }
    for job in storage.list_jobs():
        if job.get("resolution_status") != "resolved" or not job.get(
            "application_url"
        ):
            continue
        if job.get("enrichment_status") == "enriched":
            summary["jobs_already_enriched"] += 1
            continue
        captured_payload = source_record_payload(storage, job)
        if (
            job.get("enrichment_status") == "dynamic_required"
            and captured_payload is None
        ):
            summary["jobs_already_dynamic_required"] += 1
            continue
        if (
            job.get("enrichment_status") == "manual_required"
            and captured_payload is None
        ):
            summary["jobs_already_manual_required"] += 1
            continue
        summary["jobs_eligible"] += 1
        retry_at = parse_retry_at(job.get("enrichment_next_attempt_at"))
        if captured_payload is None and retry_at is not None and retry_at > now:
            summary["jobs_deferred"] += 1
            continue
        if summary["jobs_attempted"] >= max_jobs:
            summary["jobs_deferred"] += 1
            continue
        summary["jobs_attempted"] += 1
        try:
            payload = captured_payload or fetcher(job)
            apply_enrichment(storage, job, payload, now)
            summary["jobs_enriched"] += 1
        except DynamicPageRequired:
            storage.update_job_enrichment(
                job["id"],
                {
                    "enrichment_status": "dynamic_required",
                    "enrichment_last_attempt_at": now.isoformat(),
                    "enrichment_next_attempt_at": None,
                    "enrichment_error_type": None,
                },
            )
            summary["jobs_dynamic_required"] += 1
        except (
            EnrichmentPayloadError,
            EnrichmentTerminalRequestError,
            ValueError,
        ) as error:
            storage.update_job_enrichment(
                job["id"],
                {
                    "enrichment_status": "manual_required",
                    "enrichment_last_attempt_at": now.isoformat(),
                    "enrichment_next_attempt_at": None,
                    "enrichment_error_type": type(error).__name__,
                },
            )
            summary["jobs_manual_required"] += 1
        except Exception as error:
            storage.update_job_enrichment(
                job["id"],
                {
                    "enrichment_status": "failed",
                    "enrichment_last_attempt_at": now.isoformat(),
                    "enrichment_next_attempt_at": (now + RETRY_INTERVAL).isoformat(),
                    "enrichment_error_type": type(error).__name__,
                },
            )
            summary["jobs_failed"] += 1
            summary["errors"].append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )
    return summary
