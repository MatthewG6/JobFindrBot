import json
import os
from pathlib import Path

import pytest
import requests

from app.discord_notifications import (
    DiscordCredentialError,
    DiscordDeliveryError,
    DiscordNotConfigured,
    DiscordRateLimited,
    DiscordWebhookClient,
    DiscordWebhookUnavailable,
    job_notification_payload,
    load_discord_webhook_url,
    public_job_url,
    run_discord_notifications,
    validate_discord_webhook_url,
)
from app.storage import JobStorage


WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1234567890/"
    "abcdefghijklmnopqrstuvwxyz_ABCDEFG-1234567890"
)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        headers: dict | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = 300 <= status_code < 400

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class RecordingClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[dict] = []

    def send_job(self, job: dict) -> str:
        self.jobs.append(job)
        if self.fail:
            raise DiscordDeliveryError("sanitized")
        return f"message-{job['id']}"


def save_scored_job(
    storage: JobStorage,
    identifier: int,
    score: int,
) -> dict:
    return storage.save_job(
        {
            "title": f"Software Engineer {identifier}",
            "company": "Example Company",
            "location": "Remote",
            "url": f"https://example.com/jobs/{identifier}",
            "source": "test",
            "fit_score": score,
            "score_reasons": ["Target role match: software engineer"],
            "red_flags": [],
        }
    )


def test_validate_discord_webhook_url_accepts_only_expected_endpoint() -> None:
    assert validate_discord_webhook_url(WEBHOOK_URL) == WEBHOOK_URL

    invalid_urls = [
        "http://discord.com/api/webhooks/123/token-token-token-token",
        "https://discord.com.evil.example/api/webhooks/123/token-token-token",
        "https://discord.com/api/webhooks/not-a-number/token-token-token",
        "https://discord.com/api/webhooks/123/short",
        f"{WEBHOOK_URL}?wait=true",
        f"{WEBHOOK_URL}#fragment",
        "https://discord.com:bad/api/webhooks/123/token-token-token-token",
    ]
    for invalid_url in invalid_urls:
        with pytest.raises(DiscordCredentialError):
            validate_discord_webhook_url(invalid_url)


def test_discord_credentials_are_private_and_reject_symlinks(
    tmp_path: Path,
) -> None:
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir(mode=0o755)
    path = credentials_dir / "discord.json"
    path.write_text(json.dumps({"webhook_url": WEBHOOK_URL}), encoding="utf-8")
    os.chmod(path, 0o644)

    assert load_discord_webhook_url(path) == WEBHOOK_URL
    assert credentials_dir.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600

    link = credentials_dir / "link.json"
    link.symlink_to(path)
    with pytest.raises(DiscordCredentialError):
        load_discord_webhook_url(link)


def test_missing_discord_credentials_are_not_configured(tmp_path: Path) -> None:
    with pytest.raises(DiscordNotConfigured):
        load_discord_webhook_url(tmp_path / "missing.json")


@pytest.mark.parametrize("content", ["[]", "not-json", "{}"])
def test_discord_credentials_reject_invalid_content(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "discord.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DiscordCredentialError):
        load_discord_webhook_url(path)


def test_job_payload_is_bounded_and_disables_mentions() -> None:
    job = {
        "id": "7" * 7000,
        "title": "@everyone " + "x" * 400,
        "company": "@here Example",
        "location": "Remote",
        "url": "https://example.com/jobs/7",
        "source": "test",
        "fit_score": 48,
        "salary_text": "$80,000",
        "score_reasons": ["reason " + "x" * 500] * 4,
        "red_flags": ["warning " + "x" * 500] * 3,
    }

    payload = job_notification_payload(job)
    embed = payload["embeds"][0]

    assert payload["allowed_mentions"] == {"parse": []}
    assert "@everyone" not in embed["title"]
    assert "@here" not in embed["fields"][0]["value"]
    assert len(embed["title"]) <= 256
    assert all(len(field["value"]) <= 1024 for field in embed["fields"])
    assert len(embed["footer"]["text"]) <= 2048
    embed_character_count = len(embed["title"]) + len(
        embed["footer"]["text"]
    ) + sum(
        len(field["name"]) + len(field["value"])
        for field in embed["fields"]
    )
    assert embed_character_count < 6000


def test_public_job_url_rejects_local_and_credentialed_urls() -> None:
    assert public_job_url("https://example.com/job") is not None
    assert public_job_url("http://example.com/job") is None
    assert public_job_url("https://localhost/job") is None
    assert public_job_url("https://localhost./job") is None
    assert public_job_url("https://127.0.0.1/job") is None
    assert public_job_url("https://127.1/job") is None
    assert public_job_url("https://2130706433/job") is None
    assert public_job_url("https://user:pass@example.com/job") is None
    assert public_job_url("https://example.com/" + "x" * 2048) is None


def test_job_payload_neutralizes_untrusted_markdown_links() -> None:
    payload = job_notification_payload(
        {
            "id": 1,
            "title": "[Trusted](https://phish.example)",
            "company": "Example",
            "fit_score": 50,
        }
    )

    title = payload["embeds"][0]["title"]
    assert "[Trusted](" not in title
    assert "https://" not in title


def test_webhook_client_posts_with_confirmation_and_no_redirects(
    monkeypatch,
) -> None:
    request = {}

    def post(url: str, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return FakeResponse({"id": "discord-message-id"})

    monkeypatch.setattr("app.discord_notifications.requests.post", post)
    client = DiscordWebhookClient(WEBHOOK_URL, timeout=9)

    message_id = client.send_test()

    assert message_id == "discord-message-id"
    assert request["url"] == WEBHOOK_URL
    assert request["params"] == {"wait": "true"}
    assert request["timeout"] == 9
    assert request["allow_redirects"] is False
    assert request["json"]["allowed_mentions"] == {"parse": []}


def test_webhook_redirect_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: FakeResponse({}, status_code=302),
    )

    with pytest.raises(DiscordDeliveryError, match="redirected"):
        DiscordWebhookClient(WEBHOOK_URL).send_test()


def test_webhook_http_error_does_not_expose_token(monkeypatch) -> None:
    class ErrorResponse(FakeResponse):
        def raise_for_status(self) -> None:
            raise requests.HTTPError(f"401 for {WEBHOOK_URL}")

    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: ErrorResponse({}),
    )

    with pytest.raises(DiscordDeliveryError) as error:
        DiscordWebhookClient(WEBHOOK_URL).send_test()

    assert WEBHOOK_URL not in str(error.value)
    assert "abcdefghijklmnopqrstuvwxyz" not in str(error.value)


def test_webhook_rate_limit_uses_retry_after_header(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: FakeResponse(
            {"retry_after": 1},
            status_code=429,
            headers={"Retry-After": "90.5"},
        ),
    )

    with pytest.raises(DiscordRateLimited) as error:
        DiscordWebhookClient(WEBHOOK_URL).send_test()

    assert error.value.retry_after == 90.5


def test_webhook_rate_limit_uses_json_when_header_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: FakeResponse(
            {"retry_after": 45.25},
            status_code=429,
        ),
    )

    with pytest.raises(DiscordRateLimited) as error:
        DiscordWebhookClient(WEBHOOK_URL).send_test()

    assert error.value.retry_after == 45.25


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "1e24", "invalid"])
def test_webhook_rate_limit_rejects_unsafe_retry_after(
    monkeypatch,
    value: str,
) -> None:
    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: FakeResponse(
            {},
            status_code=429,
            headers={"Retry-After": value},
        ),
    )

    with pytest.raises(DiscordRateLimited) as error:
        DiscordWebhookClient(WEBHOOK_URL).send_test()

    assert error.value.retry_after == 1800


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_webhook_permanent_errors_are_classified(
    monkeypatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(
        "app.discord_notifications.requests.post",
        lambda *args, **kwargs: FakeResponse({}, status_code=status_code),
    )

    with pytest.raises(DiscordWebhookUnavailable):
        DiscordWebhookClient(WEBHOOK_URL).send_test()


def test_first_notification_run_baselines_existing_jobs(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, 1, 45)
    save_scored_job(storage, 2, 20)
    client = RecordingClient()

    summary = run_discord_notifications(storage, client=client)

    assert summary["status"] == "initialized"
    assert summary["eligible_jobs"] == 1
    assert summary["baseline_count"] == 1
    assert client.jobs == []
    notification = storage.get_job_notification("discord", 1)
    assert notification is not None
    assert notification["status"] == "baseline"


def test_new_high_fit_job_is_delivered_once(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    saved = save_scored_job(storage, 3, 48)
    client = RecordingClient()

    first = run_discord_notifications(storage, client=client)
    second = run_discord_notifications(storage, client=client)

    assert first["notifications_delivered"] == 1
    assert second["notifications_delivered"] == 0
    assert [job["id"] for job in client.jobs] == [saved["id"]]
    notification = storage.get_job_notification("discord", saved["id"])
    assert notification is not None
    assert notification["status"] == "delivered"


def test_uncertain_notification_is_not_retried(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    saved = save_scored_job(storage, 4, 50)

    failed = run_discord_notifications(
        storage,
        client=RecordingClient(fail=True),
    )
    retry_client = RecordingClient()
    second = run_discord_notifications(storage, client=retry_client)

    assert failed["status"] == "failed"
    assert failed["errors"] == [
        {"job_id": saved["id"], "error_type": "DiscordDeliveryError"}
    ]
    assert second["notifications_delivered"] == 0
    assert second["status"] == "failed"
    assert second["notifications_attention_required"] == 1
    assert retry_client.jobs == []
    notification = storage.get_job_notification("discord", saved["id"])
    assert notification is not None
    assert notification["status"] == "unknown"
    assert notification["attempts"] == 1

    storage.resolve_job_notification("discord", saved["id"], "retry")
    recovered = run_discord_notifications(storage, client=RecordingClient())
    assert recovered["notifications_delivered"] == 1


def test_confirmed_delivery_with_ledger_error_is_not_retried(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    saved = save_scored_job(storage, 5, 50)
    client = RecordingClient()
    monkeypatch.setattr(
        storage,
        "complete_job_notification",
        lambda *args: (_ for _ in ()).throw(OSError("disk error")),
    )

    summary = run_discord_notifications(storage, client=client)
    second = run_discord_notifications(storage, client=RecordingClient())

    assert summary["notifications_delivered"] == 1
    assert summary["errors"] == [
        {"job_id": saved["id"], "error_type": "OSError"}
    ]
    assert second["notifications_attempted"] == 0
    notification = storage.get_job_notification("discord", saved["id"])
    assert notification is not None
    assert notification["status"] == "unknown"
    assert notification["external_id"] == f"message-{saved['id']}"


def test_pending_notification_requires_manual_reconciliation(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    saved = save_scored_job(storage, 6, 50)
    assert storage.reserve_job_notification("discord", saved["id"])

    client = RecordingClient()
    summary = run_discord_notifications(storage, client=client)

    assert summary["notifications_attempted"] == 0
    assert summary["status"] == "failed"
    assert summary["notifications_attention_required"] == 1
    assert client.jobs == []

    storage.resolve_job_notification("discord", saved["id"], "delivered")
    resolved = run_discord_notifications(storage, client=client)
    assert resolved["status"] == "ok"
    assert resolved["notifications_attention_required"] == 0


def test_stored_job_id_cannot_override_database_id(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    saved = storage.save_job(
        {
            "id": "corrupt" * 1000,
            "title": "Software Engineer",
            "company": "Example",
            "url": "https://example.com/corrupt-id",
            "fit_score": 50,
        }
    )

    client = RecordingClient()
    summary = run_discord_notifications(storage, client=client)

    assert isinstance(saved["id"], int)
    assert summary["notifications_delivered"] == 1
    assert isinstance(client.jobs[0]["id"], int)


def test_notification_batch_is_bounded(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    for identifier in range(20):
        save_scored_job(storage, identifier + 10, 40 + identifier)
    client = RecordingClient()

    summary = run_discord_notifications(
        storage,
        client=client,
        max_notifications=5,
    )

    assert summary["notifications_attempted"] == 5
    assert summary["notifications_delivered"] == 5
    assert len(client.jobs) == 5


def test_rate_limit_stops_batch_and_defers_later_runs(tmp_path: Path) -> None:
    class RateLimitedClient(RecordingClient):
        def send_job(self, job: dict) -> str:
            self.jobs.append(job)
            raise DiscordRateLimited(3600)

    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    for identifier in range(3):
        save_scored_job(storage, identifier + 30, 50)
    client = RateLimitedClient()

    limited = run_discord_notifications(storage, client=client)
    deferred = run_discord_notifications(storage, client=RecordingClient())

    assert limited["notifications_attempted"] == 1
    assert limited["errors"][0]["error_type"] == "DiscordRateLimited"
    assert len(client.jobs) == 1
    assert deferred["status"] == "deferred"
    assert deferred["notifications_attempted"] == 0


def test_connect_timeout_stops_batch_and_defers_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    client = DiscordWebhookClient(WEBHOOK_URL)
    run_discord_notifications(storage, client=client)
    for identifier in range(3):
        save_scored_job(storage, identifier + 35, 50)

    def timeout(*args, **kwargs):
        raise requests.ConnectTimeout("safe retry")

    monkeypatch.setattr("app.discord_notifications.requests.post", timeout)
    failed = run_discord_notifications(storage, client=client)
    deferred = run_discord_notifications(storage, client=client)

    assert failed["notifications_attempted"] == 1
    assert failed["errors"][0]["error_type"] == (
        "DiscordRetryableConnectionError"
    )
    assert deferred["status"] == "deferred"
    assert deferred["notifications_attempted"] == 0
    failed_records = [
        notification
        for notification in storage.list_job_notifications("discord")
        if notification["status"] == "failed"
    ]
    assert len(failed_records) == 1


def test_uncertain_failure_stops_after_one_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=RecordingClient())
    for identifier in range(3):
        save_scored_job(storage, identifier + 38, 50)
    client = RecordingClient(fail=True)

    summary = run_discord_notifications(storage, client=client)

    assert summary["notifications_attempted"] == 1
    assert summary["notifications_attention_required"] == 1
    assert len(client.jobs) == 1


def test_unavailable_webhook_stops_and_disables_channel(tmp_path: Path) -> None:
    class UnavailableClient(RecordingClient):
        configuration_id = "old-webhook"

        def send_job(self, job: dict) -> str:
            self.jobs.append(job)
            raise DiscordWebhookUnavailable("sanitized")

    storage = JobStorage(tmp_path / "jobs.json")
    run_discord_notifications(storage, client=UnavailableClient())
    for identifier in range(3):
        save_scored_job(storage, identifier + 40, 50)
    unavailable = UnavailableClient()

    failed = run_discord_notifications(storage, client=unavailable)
    disabled = run_discord_notifications(storage, client=UnavailableClient())

    assert failed["notifications_attempted"] == 1
    assert len(unavailable.jobs) == 1
    assert disabled["notifications_attempted"] == 0
    assert disabled["errors"] == [
        {"error_type": "DiscordWebhookUnavailable"}
    ]

    replacement = RecordingClient()
    replacement.configuration_id = "new-webhook"
    recovered = run_discord_notifications(storage, client=replacement)
    assert recovered["notifications_delivered"] == 3
