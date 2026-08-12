from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import socket
from urllib.parse import urlsplit

import requests

from app.candidates import qualifies_for_application
from app.candidate_profile import default_candidate_profile
from app.storage import JobStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISCORD_CREDENTIALS_PATH = (
    PROJECT_ROOT / "credentials" / "discord.json"
)
DISCORD_CHANNEL = "discord"
DEFAULT_DISCORD_THRESHOLD = default_candidate_profile().thresholds.review
DEFAULT_MAX_NOTIFICATIONS = 10
DEFAULT_RETRY_AFTER = 1800.0
MAX_RETRY_AFTER = 604800.0
WEBHOOK_TOKEN = re.compile(r"^[A-Za-z0-9._-]{20,200}$")


class DiscordNotConfigured(RuntimeError):
    pass


class DiscordCredentialError(ValueError):
    pass


class DiscordDeliveryError(RuntimeError):
    pass


class DiscordRateLimited(DiscordDeliveryError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("Discord delivery was rate limited")
        self.retry_after = retry_after


class DiscordWebhookUnavailable(DiscordDeliveryError):
    pass


class DiscordRetryableConnectionError(DiscordDeliveryError):
    pass


def validate_discord_webhook_url(value: object) -> str:
    if not isinstance(value, str):
        raise DiscordCredentialError("Discord webhook URL must be a string")
    webhook_url = value.strip()
    try:
        parts = urlsplit(webhook_url)
        port = parts.port
    except ValueError as error:
        raise DiscordCredentialError("Discord webhook URL is invalid") from error
    path_parts = parts.path.split("/")
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() != "discord.com"
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or parts.query
        or parts.fragment
        or len(path_parts) != 5
        or path_parts[:3] != ["", "api", "webhooks"]
        or not path_parts[3].isdigit()
        or WEBHOOK_TOKEN.fullmatch(path_parts[4]) is None
    ):
        raise DiscordCredentialError("Discord webhook URL is invalid")
    return webhook_url


def load_discord_webhook_url(
    credentials_path: Path = DEFAULT_DISCORD_CREDENTIALS_PATH,
) -> str:
    if credentials_path.is_symlink():
        raise DiscordCredentialError(
            "Discord credentials must be stored in a regular file"
        )
    if not credentials_path.exists():
        raise DiscordNotConfigured("Discord webhook is not configured")
    file_stat = credentials_path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise DiscordCredentialError(
            "Discord credentials must be stored in a regular file"
        )
    parent_stat = credentials_path.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
        parent_stat.st_mode
    ):
        raise DiscordCredentialError(
            "Discord credentials directory must be a regular directory"
        )
    os.chmod(credentials_path.parent, 0o700)
    os.chmod(credentials_path, 0o600)
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DiscordCredentialError(
            "Discord credentials are not readable JSON"
        ) from error
    if not isinstance(credentials, dict):
        raise DiscordCredentialError("Discord credentials must be a JSON object")
    return validate_discord_webhook_url(credentials.get("webhook_url"))


def discord_text(value: object, limit: int) -> str:
    text = " ".join(str("" if value is None else value).split())
    for character in "\\`*_{}[]()#+-.!|>~":
        text = text.replace(character, f"\\{character}")
    text = text.replace("://", ":\u200b//")
    text = text.replace("@", "@\u200b")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def public_job_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if len(url) > 2048 or any(ord(character) < 32 for character in url):
        return None
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or "%" in hostname
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    try:
        ipv4_bytes = socket.inet_aton(hostname)
    except OSError:
        pass
    else:
        if not ipaddress.ip_address(ipv4_bytes).is_global:
            return None
    return url


def job_embed(job: dict) -> dict:
    embed = {
        "title": discord_text(job.get("title") or "Untitled job", 256),
        "color": 0x2F855A,
        "fields": [
            {
                "name": "Company",
                "value": discord_text(job.get("company") or "Unknown", 256),
                "inline": True,
            },
            {
                "name": "Location",
                "value": discord_text(
                    job.get("location") or "Unspecified",
                    256,
                ),
                "inline": True,
            },
            {
                "name": "Fit score",
                "value": discord_text(
                    f"{job.get('fit_score')}/100"
                    if job.get("scoring_version") == 2
                    else job.get("fit_score"),
                    32,
                ),
                "inline": True,
            },
            {
                "name": "Source",
                "value": discord_text(job.get("source") or "Unknown", 100),
                "inline": True,
            },
        ],
        "footer": {
            "text": discord_text(
                f"Jobbot job #{job.get('id', 'unknown')}",
                2048,
            )
        },
    }
    url = public_job_url(
        job.get("apply_url")
        or job.get("application_url")
        or job.get("url")
    )
    if url is not None:
        embed["url"] = url
    salary = discord_text(job.get("salary_text"), 256)
    if salary:
        embed["fields"].append(
            {"name": "Salary", "value": salary, "inline": True}
        )
    confidence = job.get("score_confidence")
    confidence_band = job.get("score_confidence_band")
    if (
        isinstance(confidence, int)
        and not isinstance(confidence, bool)
        and confidence_band in {"low", "medium", "high"}
    ):
        embed["fields"].append(
            {
                "name": "Score confidence",
                "value": f"{confidence}/100 ({confidence_band})",
                "inline": True,
            }
        )
    dimensions = job.get("score_dimensions")
    if isinstance(dimensions, list):
        dimension_parts = []
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            score = item.get("score")
            if isinstance(name, str) and isinstance(score, int):
                dimension_parts.append(f"{name.title()} {score}")
        breakdown = discord_text(" | ".join(dimension_parts), 500)
        if breakdown:
            embed["fields"].append(
                {"name": "Fit breakdown", "value": breakdown}
            )
    reasons = job.get("score_reasons") or []
    if isinstance(reasons, list):
        reason_text = discord_text("\n".join(map(str, reasons[:4])), 800)
        if reason_text:
            embed["fields"].append(
                {"name": "Why it matched", "value": reason_text}
            )
    red_flags = job.get("red_flags") or []
    if isinstance(red_flags, list) and red_flags:
        warning_text = discord_text("\n".join(map(str, red_flags[:3])), 600)
        embed["fields"].append(
            {"name": "Watch-outs", "value": warning_text}
        )
    timestamp = job.get("discovered_at") or job.get("created_at")
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            embed["timestamp"] = parsed.astimezone(UTC).isoformat()
        except (OverflowError, ValueError):
            pass
    return embed


def job_notification_payload(job: dict) -> dict:
    return {
        "username": "Jobbot",
        "allowed_mentions": {"parse": []},
        "embeds": [job_embed(job)],
    }


class DiscordWebhookClient:
    def __init__(self, webhook_url: str, timeout: int = 15) -> None:
        self.webhook_url = validate_discord_webhook_url(webhook_url)
        self.timeout = timeout
        self.configuration_id = hashlib.sha256(
            self.webhook_url.encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_local_credentials(cls) -> "DiscordWebhookClient":
        return cls(load_discord_webhook_url())

    def _post(self, payload: dict) -> str:
        try:
            response = requests.post(
                self.webhook_url,
                params={"wait": "true"},
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = getattr(response, "status_code", 200)
            if 300 <= status_code < 400 or getattr(
                response,
                "is_redirect",
                False,
            ):
                raise DiscordDeliveryError("Discord delivery was redirected")
            if status_code == 429:
                raise DiscordRateLimited(self._retry_after(response))
            if status_code in {401, 403, 404}:
                raise DiscordWebhookUnavailable(
                    "Discord webhook is unavailable"
                )
            response.raise_for_status()
            message = response.json()
        except DiscordDeliveryError:
            raise
        except requests.ConnectTimeout:
            raise DiscordRetryableConnectionError(
                "Discord connection timed out"
            ) from None
        except requests.RequestException as error:
            raise DiscordDeliveryError(
                f"Discord delivery failed ({type(error).__name__})"
            ) from None
        except ValueError:
            raise DiscordDeliveryError(
                "Discord delivery returned invalid JSON"
            ) from None
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise DiscordDeliveryError("Discord delivery response is invalid")
        return message_id

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        value: object = getattr(response, "headers", {}).get("Retry-After")
        if value is None:
            try:
                body = response.json()
                value = body.get("retry_after") if isinstance(body, dict) else None
            except ValueError:
                value = None
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER
        if (
            not math.isfinite(retry_after)
            or retry_after < 0
            or retry_after > MAX_RETRY_AFTER
        ):
            return DEFAULT_RETRY_AFTER
        return retry_after

    def send_job(self, job: dict) -> str:
        return self._post(job_notification_payload(job))

    def send_test(self) -> str:
        return self._post(
            {
                "username": "Jobbot",
                "allowed_mentions": {"parse": []},
                "embeds": [
                    {
                        "title": "Jobbot connected",
                        "description": (
                            "High-fit job notifications are ready. "
                            "Application actions still require approval in Jobbot."
                        ),
                        "color": 0x2F855A,
                    }
                ],
            }
        )


def run_discord_notifications(
    storage: JobStorage,
    client: DiscordWebhookClient | None = None,
    threshold: int = DEFAULT_DISCORD_THRESHOLD,
    max_notifications: int = DEFAULT_MAX_NOTIFICATIONS,
) -> dict:
    client = client or DiscordWebhookClient.from_local_credentials()
    eligible_jobs = [
        job
        for job in storage.list_jobs()
        if job.get("scoring_version") == 2
        and qualifies_for_application(job.get("fit_score"), threshold)
    ]
    eligible_jobs.sort(
        key=lambda job: (job.get("fit_score", 0), job.get("id", 0)),
        reverse=True,
    )

    initialized, baseline_count = storage.initialize_notification_channel(
        DISCORD_CHANNEL,
        [job["id"] for job in eligible_jobs],
        getattr(client, "configuration_id", None),
    )
    if initialized:
        return {
            "status": "initialized",
            "threshold": threshold,
            "eligible_jobs": len(eligible_jobs),
            "baseline_count": baseline_count,
            "notifications_attempted": 0,
            "notifications_delivered": 0,
            "notifications_skipped": 0,
            "notifications_attention_required": 0,
            "errors": [],
        }

    storage.synchronize_notification_channel(
        DISCORD_CHANNEL,
        getattr(client, "configuration_id", None),
    )
    attention_required = len(
        storage.list_attention_required_notifications(DISCORD_CHANNEL)
    )
    if storage.notification_channel_disabled(DISCORD_CHANNEL):
        return {
            "status": "failed",
            "threshold": threshold,
            "eligible_jobs": len(eligible_jobs),
            "baseline_count": 0,
            "notifications_attempted": 0,
            "notifications_delivered": 0,
            "notifications_skipped": len(eligible_jobs),
            "notifications_attention_required": attention_required,
            "errors": [{"error_type": "DiscordWebhookUnavailable"}],
        }

    if storage.notification_channel_deferred(DISCORD_CHANNEL):
        deferred_errors = []
        if attention_required:
            deferred_errors.append(
                {"error_type": "DiscordNotificationAttentionRequired"}
            )
        return {
            "status": "failed" if deferred_errors else "deferred",
            "threshold": threshold,
            "eligible_jobs": len(eligible_jobs),
            "baseline_count": 0,
            "notifications_attempted": 0,
            "notifications_delivered": 0,
            "notifications_skipped": len(eligible_jobs),
            "notifications_attention_required": attention_required,
            "errors": deferred_errors,
        }

    attempted = 0
    delivered = 0
    skipped = 0
    errors: list[dict[str, object]] = []
    for job in eligible_jobs:
        if attempted >= max_notifications:
            break
        if not storage.reserve_job_notification(DISCORD_CHANNEL, job["id"]):
            skipped += 1
            continue
        attempted += 1
        try:
            message_id = client.send_job(job)
        except DiscordRateLimited as error:
            storage.fail_job_notification(
                DISCORD_CHANNEL,
                job["id"],
                type(error).__name__,
            )
            storage.defer_notification_channel(
                DISCORD_CHANNEL,
                timedelta(seconds=error.retry_after),
            )
            errors.append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )
            break
        except DiscordWebhookUnavailable as error:
            storage.fail_job_notification(
                DISCORD_CHANNEL,
                job["id"],
                type(error).__name__,
            )
            storage.disable_notification_channel(
                DISCORD_CHANNEL,
                type(error).__name__,
            )
            errors.append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )
            break
        except DiscordRetryableConnectionError as error:
            storage.fail_job_notification(
                DISCORD_CHANNEL,
                job["id"],
                type(error).__name__,
            )
            storage.defer_notification_channel(
                DISCORD_CHANNEL,
                timedelta(seconds=DEFAULT_RETRY_AFTER),
            )
            errors.append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )
            break
        except Exception as error:
            storage.mark_job_notification_unknown(
                DISCORD_CHANNEL,
                job["id"],
                type(error).__name__,
            )
            errors.append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )
            break

        delivered += 1
        try:
            storage.complete_job_notification(
                DISCORD_CHANNEL,
                job["id"],
                message_id,
            )
        except Exception as error:
            storage.mark_job_notification_unknown(
                DISCORD_CHANNEL,
                job["id"],
                type(error).__name__,
                external_id=message_id,
            )
            errors.append(
                {"job_id": job["id"], "error_type": type(error).__name__}
            )

    attention_required = len(
        storage.list_attention_required_notifications(DISCORD_CHANNEL)
    )
    if attention_required and not errors:
        errors.append({"error_type": "DiscordNotificationAttentionRequired"})
    return {
        "status": "ok" if not errors else "failed",
        "threshold": threshold,
        "eligible_jobs": len(eligible_jobs),
        "baseline_count": 0,
        "notifications_attempted": attempted,
        "notifications_delivered": delivered,
        "notifications_skipped": skipped,
        "notifications_attention_required": attention_required,
        "errors": errors,
    }
