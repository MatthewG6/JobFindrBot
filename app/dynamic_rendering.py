from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import time
from typing import Callable
from urllib.parse import urlsplit

from app.employer_resolver import (
    PROVIDER_DESTINATION_DOMAINS,
    provider_apply_urls,
)
from app.job_enrichment import (
    DynamicPageRequired,
    EnrichmentPayloadError,
    MAX_RESPONSE_BYTES,
    apply_enrichment,
    parse_job_posting_json_ld,
    parse_retry_at,
    validate_public_dns,
)
from app.job_links import (
    DISCOVERY_DOMAINS,
    hostname_matches,
    public_https_url,
    validate_manual_application_url,
)
from app.storage import JobStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST_PATH = PROJECT_ROOT / "config" / "dynamic_render_allowlist.json"
DEFAULT_RENDER_TIMEOUT_MS = 8_000
DEFAULT_RENDER_SETTLE_MS = 1_500
DEFAULT_DYNAMIC_RESOLUTION_BATCH = 2
DEFAULT_DYNAMIC_ENRICHMENT_BATCH = 2
DYNAMIC_RETRY_INTERVAL = timedelta(hours=24)
ALLOWED_RESOURCE_TYPES = frozenset({"document", "fetch", "script", "xhr"})
PROHIBITED_RENDER_DOMAINS = ("indeed.com", "linkedin.com")
BLOCKED_BROWSER_APIS = """
(() => {
  const blocked = () => { throw new Error('Blocked by Jobbot read-only policy'); };
  for (const name of ['WebSocket', 'EventSource', 'Worker', 'SharedWorker',
                      'WebSocketStream', 'WebTransport', 'RTCPeerConnection',
                      'webkitRTCPeerConnection']) {
    try { Object.defineProperty(globalThis, name, {value: blocked}); } catch (_) {}
  }
  try { Object.defineProperty(navigator, 'sendBeacon', {value: () => false}); }
  catch (_) {}
  try { Object.defineProperty(window, 'open', {value: () => null}); } catch (_) {}
})();
"""


class DynamicRenderError(RuntimeError):
    pass


class DynamicRenderRequestError(DynamicRenderError):
    pass


class DynamicRendererUnavailable(DynamicRenderError):
    pass


class DynamicRenderPayloadError(DynamicRenderError):
    pass


@dataclass(frozen=True)
class RenderedPage:
    html: str
    url: str


def normalized_hostname(value: object) -> str | None:
    url = public_https_url(value)
    if url is None:
        return None
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def validate_allowlist_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Dynamic-render domain must be a string")
    domain = value.strip().lower().rstrip(".")
    if (
        not domain
        or len(domain) > 253
        or "://" in domain
        or "/" in domain
        or "@" in domain
    ):
        raise ValueError("Dynamic-render domain is invalid")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Dynamic-render domain is invalid") from None
    if hostname_matches(ascii_domain, DISCOVERY_DOMAINS):
        raise ValueError("Discovery providers cannot be dynamically rendered")
    labels = ascii_domain.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise ValueError("Dynamic-render domain is invalid")
    return ascii_domain


def load_dynamic_enrichment_domains(
    path: Path = DEFAULT_ALLOWLIST_PATH,
) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Dynamic-render allowlist is not readable JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"enrichment_domains"}:
        raise ValueError("Dynamic-render allowlist has invalid fields")
    values = payload["enrichment_domains"]
    if not isinstance(values, list):
        raise ValueError("Dynamic-render enrichment domains must be a list")
    domains = tuple(validate_allowlist_domain(value) for value in values)
    if len(set(domains)) != len(domains):
        raise ValueError("Dynamic-render enrichment domains contain duplicates")
    return domains


def url_allowed_for_domains(value: object, domains: tuple[str, ...]) -> bool:
    hostname = normalized_hostname(value)
    return hostname is not None and hostname_matches(hostname, domains)


def request_allowed(
    value: object,
    *,
    initial_hostname: str,
    method: str,
    resource_type: str,
) -> bool:
    return (
        method.upper() == "GET"
        and resource_type in ALLOWED_RESOURCE_TYPES
        and normalized_hostname(value) == initial_hostname
    )


def chromium_host_rule(hostname: str, addresses: tuple[str, ...]) -> str:
    ipv4 = next((address for address in addresses if "." in address), None)
    address = ipv4 or addresses[0]
    if ":" in address:
        address = f"[{address}]"
    return f"MAP {hostname} {address}, MAP * ~NOTFOUND"


def remaining_timeout_ms(deadline: float, monotonic: Callable) -> int:
    remaining = int((deadline - monotonic()) * 1_000)
    if remaining < 1:
        raise DynamicRenderRequestError("Dynamic render timed out")
    return remaining


def render_dynamic_page(
    url: str,
    *,
    allowed_domains: tuple[str, ...],
    resolver: Callable = socket.getaddrinfo,
    timeout_ms: int = DEFAULT_RENDER_TIMEOUT_MS,
    settle_ms: int = DEFAULT_RENDER_SETTLE_MS,
    playwright_factory: Callable | None = None,
    monotonic: Callable = time.monotonic,
) -> RenderedPage:
    if timeout_ms < 1 or settle_ms < 0:
        raise ValueError("Dynamic-render timing is invalid")
    deadline = monotonic() + (timeout_ms / 1_000)
    validated_url = public_https_url(url)
    if validated_url is None or not url_allowed_for_domains(
        validated_url,
        allowed_domains,
    ):
        raise DynamicRenderPayloadError("Dynamic-render URL is not approved")
    initial_hostname = normalized_hostname(validated_url)
    assert initial_hostname is not None
    if hostname_matches(initial_hostname, PROHIBITED_RENDER_DOMAINS):
        raise DynamicRenderPayloadError(
            "LinkedIn and Indeed cannot be dynamically rendered"
        )

    if playwright_factory is None:
        return render_dynamic_page_worker(
            validated_url,
            initial_hostname=initial_hostname,
            addresses=None,
            resolver=resolver,
            timeout_ms=remaining_timeout_ms(deadline, monotonic),
            settle_ms=settle_ms,
            monotonic=monotonic,
        )

    addresses = validate_public_dns(validated_url, resolver)
    return render_dynamic_page_in_process(
        validated_url,
        initial_hostname=initial_hostname,
        addresses=tuple(addresses),
        timeout_ms=remaining_timeout_ms(deadline, monotonic),
        settle_ms=settle_ms,
        playwright_factory=playwright_factory,
        monotonic=monotonic,
    )


def render_dynamic_page_in_process(
    validated_url: str,
    *,
    initial_hostname: str,
    addresses: tuple[str, ...],
    timeout_ms: int,
    settle_ms: int,
    playwright_factory: Callable,
    monotonic: Callable = time.monotonic,
) -> RenderedPage:
    deadline = monotonic() + (timeout_ms / 1_000)

    browser = None
    context = None
    try:
        with playwright_factory() as playwright:
            executable_path = getattr(playwright.chromium, "executable_path", None)
            if not isinstance(executable_path, str) or not Path(
                executable_path
            ).is_file():
                raise DynamicRendererUnavailable(
                    "Playwright Chromium is not installed"
                )
            launch_timeout = remaining_timeout_ms(deadline, monotonic)
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    timeout=launch_timeout,
                    args=[
                        "--host-resolver-rules="
                        f"{chromium_host_rule(initial_hostname, addresses)}",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-sync",
                        "--dns-prefetch-disable",
                        "--metrics-recording-only",
                        "--no-first-run",
                        "--no-proxy-server",
                    ],
                )
            except Exception as error:
                raise DynamicRendererUnavailable(
                    f"Playwright Chromium could not launch ({type(error).__name__})"
                ) from None
            context = browser.new_context(
                accept_downloads=False,
                java_script_enabled=True,
                ignore_https_errors=False,
                permissions=[],
                service_workers="block",
            )
            context.set_default_timeout(
                remaining_timeout_ms(deadline, monotonic)
            )
            context.add_init_script(BLOCKED_BROWSER_APIS)

            def route_request(route, request) -> None:
                if request_allowed(
                    request.url,
                    initial_hostname=initial_hostname,
                    method=request.method,
                    resource_type=request.resource_type,
                ):
                    route.continue_()
                else:
                    route.abort("blockedbyclient")

            context.route("**/*", route_request)
            page = context.new_page()

            def close_additional_page(additional_page) -> None:
                if additional_page is page:
                    return
                try:
                    additional_page.close()
                except Exception:
                    pass

            context.on("page", close_additional_page)
            page.goto(
                validated_url,
                wait_until="domcontentloaded",
                timeout=remaining_timeout_ms(deadline, monotonic),
            )
            if settle_ms:
                if settle_ms >= remaining_timeout_ms(deadline, monotonic):
                    raise DynamicRenderRequestError("Dynamic render timed out")
                page.wait_for_timeout(settle_ms)
            remaining_timeout_ms(deadline, monotonic)
            final_url = public_https_url(page.url)
            if final_url is None or normalized_hostname(final_url) != initial_hostname:
                raise DynamicRenderPayloadError(
                    "Dynamic-render navigation left the approved host"
                )
            html = page.locator("html").evaluate(
                """
                (element, limit) => {
                  const html = element.outerHTML;
                  return new TextEncoder().encode(html).byteLength <= limit
                    ? html
                    : null;
                }
                """,
                MAX_RESPONSE_BYTES,
                timeout=remaining_timeout_ms(deadline, monotonic),
            )
            remaining_timeout_ms(deadline, monotonic)
            if not isinstance(html, str):
                raise DynamicRenderPayloadError("Dynamic-render page is too large")
            return RenderedPage(html=html, url=final_url)
    except DynamicRenderError:
        raise
    except Exception as error:
        raise DynamicRenderRequestError(
            f"Dynamic render failed ({type(error).__name__})"
        ) from None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def dynamic_render_worker_entry(
    connection,
    validated_url: str,
    initial_hostname: str,
    addresses: tuple[str, ...] | None,
    resolver: Callable,
    timeout_ms: int,
    settle_ms: int,
) -> None:
    try:
        os.setsid()
    except OSError:
        connection.send(
            ("unavailable", "Dynamic-render process isolation is unavailable")
        )
        connection.close()
        return
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise DynamicRendererUnavailable(
                "Playwright runtime is not installed"
            ) from None

        if addresses is None:
            addresses = validate_public_dns(validated_url, resolver)
        rendered = render_dynamic_page_in_process(
            validated_url,
            initial_hostname=initial_hostname,
            addresses=addresses,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
            playwright_factory=sync_playwright,
        )
        connection.send(("ok", rendered.html, rendered.url))
    except DynamicRendererUnavailable as error:
        connection.send(("unavailable", str(error)))
    except DynamicRenderPayloadError as error:
        connection.send(("payload", str(error)))
    except Exception as error:
        connection.send(("request", type(error).__name__))
    finally:
        connection.close()


def stop_dynamic_render_worker(process) -> None:
    if process.pid is None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    process.join(timeout=0.2)


def render_dynamic_page_worker(
    validated_url: str,
    *,
    initial_hostname: str,
    addresses: tuple[str, ...] | None,
    timeout_ms: int,
    settle_ms: int,
    resolver: Callable = socket.getaddrinfo,
    monotonic: Callable = time.monotonic,
) -> RenderedPage:
    deadline = monotonic() + (timeout_ms / 1_000)
    context = multiprocessing.get_context("fork")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=dynamic_render_worker_entry,
        args=(
            child_connection,
            validated_url,
            initial_hostname,
            addresses,
            resolver,
            remaining_timeout_ms(deadline, monotonic),
            settle_ms,
        ),
    )
    process.start()
    child_connection.close()
    result = None
    try:
        while process.is_alive():
            remaining_seconds = (deadline - monotonic())
            if remaining_seconds <= 0:
                break
            if parent_connection.poll(min(remaining_seconds, 0.05)):
                result = parent_connection.recv()
                break
        if result is None and parent_connection.poll():
            result = parent_connection.recv()
    finally:
        if result is None or process.is_alive():
            stop_dynamic_render_worker(process)
        else:
            process.join(timeout=0.2)
        parent_connection.close()
        if not process.is_alive():
            process.close()

    if result is None:
        raise DynamicRenderRequestError("Dynamic render timed out")
    status, *values = result
    if status == "ok":
        return RenderedPage(html=values[0], url=values[1])
    if status == "unavailable":
        raise DynamicRendererUnavailable(values[0])
    if status == "payload":
        raise DynamicRenderPayloadError(values[0])
    raise DynamicRenderRequestError(f"Dynamic render failed ({values[0]})")


def dynamic_retry_due(value: object, now: datetime) -> bool:
    retry_at = parse_retry_at(value)
    return retry_at is None or retry_at <= now


def candidate_has_public_dns(url: str, resolver: Callable) -> bool:
    try:
        validate_public_dns(url, resolver)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def dynamic_provider_attempt_order(
    jobs: list[dict],
    now: datetime,
    limit: int,
    start_index: int = 0,
) -> list[int]:
    queues = {
        source: [
            job["id"]
            for job in jobs
            if str(job.get("source") or "").strip().lower() == source
            and dynamic_retry_due(
                job.get("dynamic_resolution_next_attempt_at"),
                now,
            )
        ]
        for source in PROVIDER_DESTINATION_DOMAINS
    }
    sources = list(PROVIDER_DESTINATION_DOMAINS)
    start_index %= len(sources)
    sources = sources[start_index:] + sources[:start_index]
    selected = []
    offset = 0
    while len(selected) < limit:
        added = False
        for source in sources:
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


def resolve_dynamic_provider_sites(
    storage: JobStorage,
    *,
    now: datetime | None = None,
    max_jobs: int = DEFAULT_DYNAMIC_RESOLUTION_BATCH,
    renderer: Callable = render_dynamic_page,
    resolver: Callable = socket.getaddrinfo,
) -> dict:
    if not 0 <= max_jobs <= DEFAULT_DYNAMIC_RESOLUTION_BATCH:
        raise ValueError("Dynamic-resolution batch size must be between zero and two")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    all_jobs = storage.list_jobs()
    eligible_jobs = [
        job
        for job in all_jobs
        if str(job.get("source") or "").strip().lower()
        in PROVIDER_DESTINATION_DOMAINS
        and job.get("resolution_status") == "pending"
        and job.get("resolution_error_type") == "DynamicPageRequired"
    ]
    prior_attempts = sum(
        count
        for job in all_jobs
        if isinstance(
            count := job.get("dynamic_resolution_attempt_count"),
            int,
        )
        and not isinstance(count, bool)
        and count >= 0
    )
    attempt_order = dynamic_provider_attempt_order(
        eligible_jobs,
        now,
        max_jobs,
        start_index=prior_attempts,
    )
    attempted_ids = set(attempt_order)
    jobs_by_id = {job["id"]: job for job in eligible_jobs}
    processing_jobs = [jobs_by_id[job_id] for job_id in attempt_order] + [
        job for job in eligible_jobs if job["id"] not in attempted_ids
    ]
    summary = {
        "jobs_eligible": 0,
        "jobs_attempted": 0,
        "jobs_resolved": 0,
        "jobs_deferred": 0,
        "jobs_ambiguous": 0,
        "jobs_manual_required": 0,
        "jobs_failed": 0,
        "errors": [],
    }
    for job in processing_jobs:
        source = str(job.get("source") or "").strip().lower()
        domains = PROVIDER_DESTINATION_DOMAINS.get(source)
        summary["jobs_eligible"] += 1
        if job["id"] not in attempted_ids:
            summary["jobs_deferred"] += 1
            continue
        assert domains is not None
        summary["jobs_attempted"] += 1
        try:
            rendered = renderer(
                str(job.get("url") or ""),
                allowed_domains=domains,
                resolver=resolver,
            )
            candidates = {
                candidate
                for candidate in provider_apply_urls(
                    source,
                    rendered.html,
                    rendered.url,
                )
                if candidate_has_public_dns(candidate, resolver)
            }
            if len(candidates) != 1:
                error_type = (
                    "DynamicDestinationAmbiguous"
                    if len(candidates) > 1
                    else "DynamicDestinationMissing"
                )
                with storage.transaction():
                    storage.record_job_dynamic_resolution_attempt(
                        job["id"],
                        attempted_at=now,
                        next_attempt_at=None,
                        error_type=error_type,
                    )
                    storage.update_job_resolution(
                        job["id"],
                        status="manual_required",
                        application_url=None,
                        method=(
                            "ambiguous_dynamic_provider_apply_link"
                            if len(candidates) > 1
                            else "dynamic_provider_destination_unavailable"
                        ),
                        confidence=None,
                    )
                if len(candidates) > 1:
                    summary["jobs_ambiguous"] += 1
                else:
                    summary["jobs_manual_required"] += 1
                continue
            application_url = validate_manual_application_url(
                next(iter(candidates))
            )
            with storage.transaction():
                storage.record_job_dynamic_resolution_attempt(
                    job["id"],
                    attempted_at=now,
                    next_attempt_at=None,
                    error_type=None,
                )
                storage.add_job_link(
                    job["id"],
                    url=application_url,
                    source="dynamic_renderer",
                    source_job_id=job.get("source_job_id"),
                    role="official",
                )
                storage.update_job_resolution(
                    job["id"],
                    status="resolved",
                    application_url=application_url,
                    method="playwright_provider_apply_link",
                    confidence=0.85,
                )
            summary["jobs_resolved"] += 1
        except DynamicRendererUnavailable:
            raise
        except (DynamicRenderPayloadError, ValueError) as error:
            with storage.transaction():
                storage.record_job_dynamic_resolution_attempt(
                    job["id"],
                    attempted_at=now,
                    next_attempt_at=None,
                    error_type=type(error).__name__,
                )
                storage.update_job_resolution(
                    job["id"],
                    status="manual_required",
                    application_url=None,
                    method="dynamic_provider_destination_unavailable",
                    confidence=None,
                )
            summary["jobs_manual_required"] += 1
        except Exception as error:
            storage.record_job_dynamic_resolution_attempt(
                job["id"],
                attempted_at=now,
                next_attempt_at=now + DYNAMIC_RETRY_INTERVAL,
                error_type=type(error).__name__,
            )
            summary["jobs_failed"] += 1
    return summary


def enrich_dynamic_jobs(
    storage: JobStorage,
    *,
    now: datetime | None = None,
    max_jobs: int = DEFAULT_DYNAMIC_ENRICHMENT_BATCH,
    allowlist_path: Path = DEFAULT_ALLOWLIST_PATH,
    renderer: Callable = render_dynamic_page,
    resolver: Callable = socket.getaddrinfo,
) -> dict:
    if not 0 <= max_jobs <= DEFAULT_DYNAMIC_ENRICHMENT_BATCH:
        raise ValueError("Dynamic-enrichment batch size must be between zero and two")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    domains = load_dynamic_enrichment_domains(allowlist_path)
    summary = {
        "jobs_eligible": 0,
        "jobs_attempted": 0,
        "jobs_enriched": 0,
        "jobs_deferred": 0,
        "jobs_unapproved": 0,
        "jobs_manual_required": 0,
        "jobs_failed": 0,
        "errors": [],
    }
    for job in storage.list_jobs():
        if (
            job.get("resolution_status") != "resolved"
            or job.get("enrichment_status") != "dynamic_required"
            or not job.get("application_url")
        ):
            continue
        summary["jobs_eligible"] += 1
        application_url = validate_manual_application_url(job["application_url"])
        if not url_allowed_for_domains(application_url, domains):
            summary["jobs_unapproved"] += 1
            continue
        if (
            summary["jobs_attempted"] >= max_jobs
            or not dynamic_retry_due(job.get("enrichment_next_attempt_at"), now)
        ):
            summary["jobs_deferred"] += 1
            continue
        summary["jobs_attempted"] += 1
        try:
            rendered = renderer(
                application_url,
                allowed_domains=domains,
                resolver=resolver,
            )
            payload = parse_job_posting_json_ld(rendered.html, job, rendered.url)
            if payload.apply_url is not None and not candidate_has_public_dns(
                payload.apply_url,
                resolver,
            ):
                raise DynamicRenderPayloadError(
                    "Rendered application URL is not public"
                )
            payload = payload.model_copy(
                update={"method": "playwright_jobposting_jsonld"}
            )
            apply_enrichment(storage, job, payload, now)
            summary["jobs_enriched"] += 1
        except DynamicRendererUnavailable:
            raise
        except (
            DynamicPageRequired,
            EnrichmentPayloadError,
            DynamicRenderPayloadError,
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
                    "enrichment_status": "dynamic_required",
                    "enrichment_last_attempt_at": now.isoformat(),
                    "enrichment_next_attempt_at": (
                        now + DYNAMIC_RETRY_INTERVAL
                    ).isoformat(),
                    "enrichment_error_type": type(error).__name__,
                },
            )
            summary["jobs_failed"] += 1
    return summary
