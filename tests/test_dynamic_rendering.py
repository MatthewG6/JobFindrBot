from datetime import UTC, datetime, timedelta
import builtins
import json
import multiprocessing
import os
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

import pytest
from tinydb import TinyDB

from app.dynamic_rendering import (
    DynamicRenderPayloadError,
    DynamicRenderRequestError,
    DynamicRendererUnavailable,
    RenderedPage,
    dynamic_render_worker_entry,
    enrich_dynamic_jobs,
    load_dynamic_enrichment_domains,
    render_dynamic_page,
    render_dynamic_page_worker,
    request_allowed,
    resolve_dynamic_provider_sites,
    stop_dynamic_render_worker,
)
from app.ingestion import ingest_job
from app.models import JobPosting
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)


def posting(
    *,
    source: str,
    url: str,
    company: str = "Example",
    description: str = "",
) -> JobPosting:
    return JobPosting(
        title="Junior Software Engineer",
        company=company,
        location="Remote, United States",
        source=source,
        url=url,
        description=description,
    )


def public_dns(*args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


def mark_provider_dynamic(storage: JobStorage, job_id: int) -> None:
    storage.record_job_resolution_attempt(
        job_id,
        attempted_at=NOW,
        next_attempt_at=None,
        error_type="DynamicPageRequired",
    )
    storage.update_job_resolution(
        job_id,
        status="pending",
        application_url=None,
        method="provider_dynamic_required",
        confidence=None,
    )


def write_allowlist(path: Path, *domains: str) -> Path:
    path.write_text(
        json.dumps({"enrichment_domains": list(domains)}),
        encoding="utf-8",
    )
    return path


def test_request_policy_allows_only_same_host_get_resources() -> None:
    assert request_allowed(
        "https://jobs.example.com/api/posting",
        initial_hostname="jobs.example.com",
        method="GET",
        resource_type="fetch",
    )
    assert not request_allowed(
        "https://analytics.example.net/script.js",
        initial_hostname="jobs.example.com",
        method="GET",
        resource_type="script",
    )
    assert not request_allowed(
        "https://jobs.example.com/track",
        initial_hostname="jobs.example.com",
        method="POST",
        resource_type="fetch",
    )
    assert not request_allowed(
        "https://jobs.example.com/logo.png",
        initial_hostname="jobs.example.com",
        method="GET",
        resource_type="image",
    )


class FakeRequest:
    def __init__(self, url: str, method: str, resource_type: str) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type


class FakeConnection:
    def __init__(self) -> None:
        self.messages = []
        self.closed = False

    def send(self, value) -> None:
        self.messages.append(value)

    def close(self) -> None:
        self.closed = True


class FakeRoute:
    def __init__(self) -> None:
        self.action = None

    def continue_(self) -> None:
        self.action = "continue"

    def abort(self, reason: str) -> None:
        self.action = f"abort:{reason}"


class FakePage:
    def __init__(self, context) -> None:
        self.context = context
        self.url = "https://jobs.example.com/posting/1"
        self.actions = []
        self.closed = False
        self.content_hook = lambda: None

    def goto(self, url: str, **kwargs) -> None:
        self.url = url
        for request in (
            FakeRequest(url, "GET", "document"),
            FakeRequest("https://analytics.example.net/a.js", "GET", "script"),
            FakeRequest("https://jobs.example.com/track", "POST", "fetch"),
        ):
            route = FakeRoute()
            self.context.route_handler(route, request)
            self.actions.append(route.action)

    def wait_for_timeout(self, timeout: int) -> None:
        return None

    def content(self) -> str:
        return "<html><body>rendered</body></html>"

    def locator(self, selector: str):
        assert selector == "html"
        return FakeLocator(self)

    def close(self) -> None:
        self.closed = True


class FakeLocator:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.timeout = None

    def evaluate(self, expression, limit, *, timeout):
        self.timeout = timeout
        self.page.content_hook()
        html = self.page.content()
        return html if len(html.encode("utf-8")) <= limit else None


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage(self)
        self.closed = False
        self.timeout = None
        self.route_handler = None
        self.init_script = None
        self.page_handler = None

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def new_page(self) -> FakePage:
        return self.page

    def add_init_script(self, script: str) -> None:
        self.init_script = script

    def route(self, pattern: str, handler) -> None:
        self.route_handler = handler

    def on(self, event: str, handler) -> None:
        assert event == "page"
        self.page_handler = handler

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.context_kwargs = None
        self.closed = False

    def new_context(self, **kwargs) -> FakeContext:
        self.context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.launch_kwargs = None
        self.executable_path = str(Path(__file__))

    def launch(self, **kwargs) -> FakeBrowser:
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, *args) -> None:
        return None


def test_renderer_pins_host_and_blocks_side_effecting_requests() -> None:
    playwright = FakePlaywright()

    rendered = render_dynamic_page(
        "https://jobs.example.com/posting/1",
        allowed_domains=("example.com",),
        resolver=public_dns,
        settle_ms=0,
        playwright_factory=lambda: FakePlaywrightManager(playwright),
    )

    browser = playwright.chromium.browser
    assert rendered.html == "<html><body>rendered</body></html>"
    assert browser.context.page.actions == [
        "continue",
        "abort:blockedbyclient",
        "abort:blockedbyclient",
    ]
    assert any(
        value.startswith("--host-resolver-rules=MAP jobs.example.com ")
        for value in playwright.chromium.launch_kwargs["args"]
    )
    assert any(
        "MAP * ~NOTFOUND" in value
        for value in playwright.chromium.launch_kwargs["args"]
    )
    assert "--no-proxy-server" in playwright.chromium.launch_kwargs["args"]
    assert playwright.chromium.launch_kwargs["timeout"] <= 8_000
    assert browser.context_kwargs["accept_downloads"] is False
    assert browser.context_kwargs["service_workers"] == "block"
    assert "WebSocket" in browser.context.init_script
    assert browser.context.closed is True
    assert browser.closed is True

    popup = FakePage(browser.context)
    popup_route = FakeRoute()
    browser.context.route_handler(
        popup_route,
        FakeRequest("https://internal.example/submit", "POST", "document"),
    )
    browser.context.page_handler(popup)
    assert popup_route.action == "abort:blockedbyclient"
    assert popup.closed is True


def test_content_extraction_cannot_cross_total_deadline() -> None:
    playwright = FakePlaywright()
    clock = [0.0]
    playwright.chromium.browser.context.page.content_hook = lambda: clock.__setitem__(
        0,
        9.0,
    )

    with pytest.raises(DynamicRenderRequestError, match="timed out"):
        render_dynamic_page(
            "https://jobs.example.com/posting/1",
            allowed_domains=("example.com",),
            resolver=public_dns,
            settle_ms=0,
            playwright_factory=lambda: FakePlaywrightManager(playwright),
            monotonic=lambda: clock[0],
        )


def test_worker_process_is_killed_at_total_deadline(monkeypatch) -> None:
    def stall(*args) -> None:
        time.sleep(5)

    monkeypatch.setattr(
        "app.dynamic_rendering.dynamic_render_worker_entry",
        stall,
    )
    started = time.monotonic()

    with pytest.raises(DynamicRenderRequestError, match="timed out"):
        render_dynamic_page_worker(
            "https://jobs.example.com/posting/1",
            initial_hostname="jobs.example.com",
            addresses=("93.184.216.34",),
            timeout_ms=50,
            settle_ms=0,
        )

    assert time.monotonic() - started < 1


def test_worker_eof_is_sanitized_as_a_request_error(monkeypatch) -> None:
    def exit_without_result(connection, *args) -> None:
        connection.close()

    monkeypatch.setattr(
        "app.dynamic_rendering.dynamic_render_worker_entry",
        exit_without_result,
    )

    with pytest.raises(
        DynamicRenderRequestError,
        match="worker exited",
    ):
        render_dynamic_page_worker(
            "https://jobs.example.com/posting/1",
            initial_hostname="jobs.example.com",
            addresses=("93.184.216.34",),
            timeout_ms=1_000,
            settle_ms=0,
        )


def test_worker_fails_closed_when_process_isolation_is_unavailable(
    monkeypatch,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(
        os,
        "setsid",
        lambda: (_ for _ in ()).throw(OSError("private detail")),
    )

    dynamic_render_worker_entry(
        connection,
        "https://jobs.example.com/posting/1",
        "jobs.example.com",
        ("93.184.216.34",),
        public_dns,
        8_000,
        0,
    )

    assert connection.messages == [
        ("unavailable", "Dynamic-render process isolation is unavailable")
    ]
    assert connection.closed is True


def test_dns_resolution_is_inside_total_deadline() -> None:
    def stall_dns(*args, **kwargs):
        time.sleep(5)
        return public_dns()

    started = time.monotonic()

    with pytest.raises(DynamicRenderRequestError, match="timed out"):
        render_dynamic_page(
            "https://jobs.example.com/posting/1",
            allowed_domains=("example.com",),
            resolver=stall_dns,
            timeout_ms=50,
            settle_ms=0,
        )

    assert time.monotonic() - started < 1


def test_worker_group_is_killed_after_leader_exits() -> None:
    parent_socket, child_socket = socket.socketpair()
    context = multiprocessing.get_context("fork")

    def exit_with_surviving_child(child_socket_fd: int) -> None:
        os.setsid()
        worker_socket = socket.socket(fileno=child_socket_fd)
        if os.fork() == 0:
            worker_socket.sendall(b"ready")
            time.sleep(5)
            os._exit(0)
        worker_socket.close()
        os._exit(0)

    process = context.Process(
        target=exit_with_surviving_child,
        args=(child_socket.fileno(),),
    )
    process.start()
    child_socket.close()
    parent_socket.settimeout(1)

    assert parent_socket.recv(5) == b"ready"
    process.join(timeout=1)
    assert process.is_alive() is False

    stop_dynamic_render_worker(process)

    assert parent_socket.recv(1) == b""
    parent_socket.close()
    process.close()


def test_missing_chromium_is_a_systemic_error() -> None:
    playwright = FakePlaywright()
    playwright.chromium.executable_path = "/missing/chromium"

    with pytest.raises(DynamicRendererUnavailable):
        render_dynamic_page(
            "https://jobs.example.com/posting/1",
            allowed_domains=("example.com",),
            resolver=public_dns,
            playwright_factory=lambda: FakePlaywrightManager(playwright),
        )


def test_missing_playwright_runtime_is_a_systemic_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def fail_playwright_import(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("private detail")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_playwright_import)

    with pytest.raises(
        DynamicRendererUnavailable,
        match="Playwright runtime is not installed",
    ):
        render_dynamic_page(
            "https://jobs.example.com/posting/1",
            allowed_domains=("example.com",),
            resolver=public_dns,
        )


def test_chromium_launch_failure_is_a_systemic_error() -> None:
    playwright = FakePlaywright()

    def fail_launch(**kwargs):
        raise RuntimeError("private detail")

    playwright.chromium.launch = fail_launch

    with pytest.raises(DynamicRendererUnavailable):
        render_dynamic_page(
            "https://jobs.example.com/posting/1",
            allowed_domains=("example.com",),
            resolver=public_dns,
            playwright_factory=lambda: FakePlaywrightManager(playwright),
        )


def test_allowlist_rejects_discovery_domains_and_duplicates(tmp_path: Path) -> None:
    linkedin = write_allowlist(tmp_path / "linkedin.json", "linkedin.com")
    duplicate = write_allowlist(
        tmp_path / "duplicate.json",
        "jobs.example.com",
        "jobs.example.com",
    )

    with pytest.raises(ValueError, match="Discovery providers"):
        load_dynamic_enrichment_domains(linkedin)
    with pytest.raises(ValueError, match="duplicates"):
        load_dynamic_enrichment_domains(duplicate)


@pytest.mark.parametrize(
    "url,domain",
    (
        ("https://www.linkedin.com/jobs/view/1", "linkedin.com"),
        ("https://www.indeed.com/viewjob?jk=1", "indeed.com"),
    ),
)
def test_renderer_centrally_rejects_linkedin_and_indeed(
    url: str,
    domain: str,
) -> None:
    with pytest.raises(DynamicRenderPayloadError):
        render_dynamic_page(
            url,
            allowed_domains=(domain,),
            resolver=public_dns,
            playwright_factory=lambda: pytest.fail(
                "Prohibited providers must be rejected before browser startup"
            ),
        )


def test_dynamic_provider_resolution_stores_official_provenance(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        posting(
            source="remotive",
            url="https://remotive.com/remote-jobs/software-dev/example-1",
        ),
        storage,
    )["job"]
    mark_provider_dynamic(storage, saved["id"])
    html = """
      <a href="https://careers.example.com/jobs/1">
        Apply for this position
      </a>
    """

    summary = resolve_dynamic_provider_sites(
        storage,
        now=NOW,
        renderer=lambda *args, **kwargs: RenderedPage(
            html=html,
            url="https://remotive.com/remote-jobs/software-dev/example-1",
        ),
        resolver=public_dns,
    )
    resolved = storage.get_job(saved["id"])

    assert summary["jobs_resolved"] == 1
    assert resolved["application_url"] == "https://careers.example.com/jobs/1"
    assert resolved["resolution_method"] == "playwright_provider_apply_link"
    assert resolved["dynamic_resolution_attempt_count"] == 1
    assert storage.list_job_links(saved["id"])[-1]["source"] == (
        "dynamic_renderer"
    )


def test_dynamic_provider_failure_sets_durable_cooldown(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    storage = JobStorage(database_path)
    saved = ingest_job(
        posting(
            source="himalayas",
            url="https://himalayas.app/companies/example/jobs/1",
        ),
        storage,
    )["job"]
    mark_provider_dynamic(storage, saved["id"])
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DynamicRenderRequestError("private detail")

    first = resolve_dynamic_provider_sites(
        storage,
        now=NOW,
        renderer=fail,
        resolver=public_dns,
    )
    reopened = JobStorage(database_path)
    second = resolve_dynamic_provider_sites(
        reopened,
        now=NOW + timedelta(hours=1),
        renderer=fail,
        resolver=public_dns,
    )
    deferred = reopened.get_job(saved["id"])

    assert calls == 1
    assert first["jobs_failed"] == 1
    assert second["jobs_deferred"] == 1
    assert deferred["dynamic_resolution_error_type"] == (
        "DynamicRenderRequestError"
    )
    assert datetime.fromisoformat(
        deferred["dynamic_resolution_next_attempt_at"]
    ) == NOW + timedelta(hours=24)


def test_unavailable_renderer_fails_the_dynamic_stage(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        posting(
            source="himalayas",
            url="https://himalayas.app/companies/example/jobs/1",
        ),
        storage,
    )["job"]
    mark_provider_dynamic(storage, saved["id"])

    with pytest.raises(DynamicRendererUnavailable):
        resolve_dynamic_provider_sites(
            storage,
            now=NOW,
            renderer=lambda *args, **kwargs: (_ for _ in ()).throw(
                DynamicRendererUnavailable("not installed")
            ),
            resolver=public_dns,
        )

    assert storage.get_job(saved["id"])["dynamic_resolution_attempt_count"] == 0


def test_dynamic_provider_batch_executes_round_robin(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    sources = ("adzuna",) * 7 + ("himalayas", "remotive")
    domains = {
        "adzuna": "www.adzuna.com/details",
        "himalayas": "himalayas.app/companies/example/jobs",
        "remotive": "remotive.com/remote-jobs/software-dev",
    }
    for index, source in enumerate(sources):
        saved = ingest_job(
            posting(
                source=source,
                url=f"https://{domains[source]}/{index}",
                company=f"Example {index}",
            ),
            storage,
        )["job"]
        mark_provider_dynamic(storage, saved["id"])
    hosts = []

    def fail(url: str, **kwargs):
        hosts.append(urlsplit(url).hostname)
        raise DynamicRenderRequestError("private detail")

    first = resolve_dynamic_provider_sites(
        storage,
        now=NOW,
        max_jobs=2,
        renderer=fail,
        resolver=public_dns,
    )
    second = resolve_dynamic_provider_sites(
        storage,
        now=NOW + timedelta(hours=24),
        max_jobs=2,
        renderer=fail,
        resolver=public_dns,
    )

    assert hosts == [
        "www.adzuna.com",
        "himalayas.app",
        "remotive.com",
        "www.adzuna.com",
    ]
    assert first["jobs_attempted"] == 2
    assert first["jobs_deferred"] == 7
    assert second["jobs_attempted"] == 2
    assert second["jobs_deferred"] == 7


def test_dynamic_batch_caps_cannot_be_overridden(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="between zero and two"):
        resolve_dynamic_provider_sites(storage, max_jobs=3)
    with pytest.raises(ValueError, match="between zero and two"):
        enrich_dynamic_jobs(
            storage,
            max_jobs=3,
            allowlist_path=write_allowlist(tmp_path / "approved.json"),
        )


def test_linkedin_and_indeed_are_never_rendered(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    for source, url in (
        ("linkedin_email", "https://www.linkedin.com/jobs/view/1"),
        ("indeed_email", "https://www.indeed.com/viewjob?jk=1"),
    ):
        saved = ingest_job(posting(source=source, url=url), storage)["job"]
        mark_provider_dynamic(storage, saved["id"])

    summary = resolve_dynamic_provider_sites(
        storage,
        now=NOW,
        renderer=lambda *args, **kwargs: pytest.fail(
            "Discovery providers must not be rendered"
        ),
        resolver=public_dns,
    )

    assert summary["jobs_eligible"] == 0
    assert summary["jobs_attempted"] == 0


def test_dynamic_enrichment_requires_allowlist_and_matches_identity(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        posting(
            source="manual",
            url="https://careers.example.com/jobs/1",
        ),
        storage,
    )["job"]
    storage.update_job_enrichment(
        saved["id"],
        {"enrichment_status": "dynamic_required"},
    )
    html = """
      <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Junior Software Engineer",
          "hiringOrganization": {"name": "Example"},
          "description": "Build reliable software for customers and teammates."
        }
      </script>
    """
    calls = 0

    def render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return RenderedPage(
            html=html,
            url="https://careers.example.com/jobs/1",
        )

    unapproved = enrich_dynamic_jobs(
        storage,
        now=NOW,
        allowlist_path=write_allowlist(tmp_path / "empty.json"),
        renderer=render,
        resolver=public_dns,
    )
    approved = enrich_dynamic_jobs(
        storage,
        now=NOW,
        allowlist_path=write_allowlist(
            tmp_path / "approved.json",
            "example.com",
        ),
        renderer=render,
        resolver=public_dns,
    )
    enriched = storage.get_job(saved["id"])

    assert unapproved["jobs_unapproved"] == 1
    assert calls == 1
    assert approved["jobs_enriched"] == 1
    assert enriched["enrichment_method"] == "playwright_jobposting_jsonld"


def test_dynamic_enrichment_failure_sets_durable_cooldown(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.json"
    allowlist_path = write_allowlist(
        tmp_path / "approved.json",
        "example.com",
    )
    storage = JobStorage(database_path)
    saved = ingest_job(
        posting(
            source="manual",
            url="https://careers.example.com/jobs/1",
        ),
        storage,
    )["job"]
    storage.update_job_enrichment(
        saved["id"],
        {"enrichment_status": "dynamic_required"},
    )
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DynamicRenderRequestError("private detail")

    first = enrich_dynamic_jobs(
        storage,
        now=NOW,
        allowlist_path=allowlist_path,
        renderer=fail,
        resolver=public_dns,
    )
    reopened = JobStorage(database_path)
    second = enrich_dynamic_jobs(
        reopened,
        now=NOW + timedelta(hours=1),
        allowlist_path=allowlist_path,
        renderer=fail,
        resolver=public_dns,
    )
    deferred = reopened.get_job(saved["id"])

    assert calls == 1
    assert first["jobs_failed"] == 1
    assert second["jobs_deferred"] == 1
    assert deferred["enrichment_error_type"] == "DynamicRenderRequestError"
    assert datetime.fromisoformat(deferred["enrichment_next_attempt_at"]) == (
        NOW + timedelta(hours=24)
    )


def test_dynamic_enrichment_rejects_private_rendered_application_url(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved = ingest_job(
        posting(
            source="manual",
            url="https://careers.example.com/jobs/1",
        ),
        storage,
    )["job"]
    storage.update_job_enrichment(
        saved["id"],
        {"enrichment_status": "dynamic_required"},
    )
    html = """
      <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Junior Software Engineer",
          "hiringOrganization": {"name": "Example"},
          "description": "Build reliable software for customers and teammates.",
          "url": "https://internal.example/jobs/1"
        }
      </script>
    """

    def selective_dns(hostname, *args, **kwargs):
        address = "127.0.0.1" if hostname == "internal.example" else (
            "93.184.216.34"
        )
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    summary = enrich_dynamic_jobs(
        storage,
        now=NOW,
        allowlist_path=write_allowlist(
            tmp_path / "approved.json",
            "example.com",
        ),
        renderer=lambda *args, **kwargs: RenderedPage(
            html=html,
            url="https://careers.example.com/jobs/1",
        ),
        resolver=selective_dns,
    )

    assert summary["jobs_manual_required"] == 1
    assert storage.get_job(saved["id"])["enrichment_status"] == (
        "manual_required"
    )


def test_schema_five_backfills_dynamic_attempt_state(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.json"
    database = TinyDB(database_path)
    database.table("schema_metadata").insert(
        {"key": "schema_version", "version": 4}
    )
    database.table("jobs").insert(
        {
            "title": "Junior Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://himalayas.app/companies/example/jobs/1",
            "source": "himalayas",
            "resolution_status": "pending",
        }
    )
    database.close()

    storage = JobStorage(database_path)
    migrated = storage.list_jobs()[0]

    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 7
    assert migrated["dynamic_resolution_attempt_count"] == 0
    assert migrated["dynamic_resolution_last_attempt_at"] is None
    assert migrated["dynamic_resolution_next_attempt_at"] is None
    assert migrated["dynamic_resolution_error_type"] is None
