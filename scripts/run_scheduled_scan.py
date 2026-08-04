from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.gmail_client import DEFAULT_GMAIL_DB_PATH, GmailJobAlertClient
from app.discord_notifications import (
    DiscordNotConfigured,
    run_discord_notifications,
)
from app.source_credentials import SourceNotConfigured
from app.storage import JobStorage
from scripts.run_adzuna_scan import run_adzuna_scan
from scripts.run_employer_watchlist_scan import run_employer_watchlist_scan
from scripts.run_gmail_scan import run_gmail_scan
from scripts.run_himalayas_scan import run_himalayas_scan
from scripts.run_remotive_scan import run_remotive_scan
from scripts.run_usajobs_scan import run_usajobs_scan


DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "scheduler_state.json"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / "scheduled_scan.lock"
DEFAULT_HEALTH_PATH = PROJECT_ROOT / "data" / "scheduler_health.json"
HIMALAYAS_INTERVAL = timedelta(hours=24)
SOURCE_INTERVALS = {
    "himalayas": HIMALAYAS_INTERVAL,
    "remotive": timedelta(hours=6),
    "employer_watchlist": timedelta(hours=6),
    "adzuna": timedelta(hours=6),
    "usajobs": timedelta(hours=6),
}
SourceRunner = Callable[[JobStorage], dict]
NotificationRunner = Callable[[JobStorage], dict]


def default_source_runners() -> dict[str, SourceRunner]:
    return {
        "himalayas": run_himalayas_scan,
        "remotive": run_remotive_scan,
        "employer_watchlist": run_employer_watchlist_scan,
        "adzuna": run_adzuna_scan,
        "usajobs": run_usajobs_scan,
    }


def load_scheduler_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Scheduler state is not readable JSON") from error
    if not isinstance(state, dict):
        raise ValueError("Scheduler state must be a JSON object")
    return state


def write_scheduler_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            descriptor = -1
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, state_path)
        os.chmod(state_path, 0o600)
        directory_descriptor = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def source_scan_due(
    state: dict,
    source: str,
    now: datetime,
    interval: timedelta,
) -> bool:
    value = state.get(f"{source}_last_attempt_at")
    if not isinstance(value, str):
        value = state.get(f"{source}_last_success_at")
    if not isinstance(value, str):
        return True
    try:
        last_success = datetime.fromisoformat(value)
        if last_success.tzinfo is None:
            return True
        last_success = last_success.astimezone(UTC)
        if last_success > now:
            return True
        return now >= last_success + interval
    except (OverflowError, ValueError):
        return True


def himalayas_scan_due(
    state: dict,
    now: datetime,
    interval: timedelta = HIMALAYAS_INTERVAL,
) -> bool:
    return source_scan_due(state, "himalayas", now, interval)


def required_nonnegative_int(summary: dict, key: str) -> int:
    value = summary.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Source summary has invalid {key}")
    return value


def validate_gmail_summary(summary: object) -> dict:
    if not isinstance(summary, dict) or not isinstance(summary.get("errors"), list):
        raise ValueError("Gmail runner returned an invalid summary")
    for key in (
        "messages_found",
        "messages_fetched",
        "messages_processed",
        "messages_skipped",
        "jobs_parsed",
        "jobs_created",
    ):
        required_nonnegative_int(summary, key)
    return summary


def validate_source_summary(summary: object) -> dict:
    if not isinstance(summary, dict) or not isinstance(summary.get("errors"), list):
        raise ValueError("Source runner returned an invalid summary")
    for key in (
        "jobs_fetched",
        "jobs_created",
        "duplicates_skipped",
        "records_received",
        "records_filtered",
        "records_rejected",
    ):
        required_nonnegative_int(summary, key)
    accounted_jobs = summary["jobs_created"] + summary["duplicates_skipped"]
    if accounted_jobs != summary["jobs_fetched"]:
        raise ValueError("Source summary counts do not balance")
    accounted_records = (
        summary["jobs_fetched"]
        + summary["records_filtered"]
        + summary["records_rejected"]
    )
    if accounted_records != summary["records_received"]:
        raise ValueError("Source record counts do not balance")
    return summary


def validate_notification_summary(summary: object) -> dict:
    if not isinstance(summary, dict) or not isinstance(summary.get("errors"), list):
        raise ValueError("Notification runner returned an invalid summary")
    if summary.get("status") not in {
        "deferred",
        "initialized",
        "ok",
        "failed",
    }:
        raise ValueError("Notification runner returned an invalid status")
    for key in (
        "threshold",
        "eligible_jobs",
        "baseline_count",
        "notifications_attempted",
        "notifications_delivered",
        "notifications_skipped",
        "notifications_attention_required",
    ):
        required_nonnegative_int(summary, key)
    if summary["notifications_delivered"] > summary["notifications_attempted"]:
        raise ValueError("Notification summary counts do not balance")
    status = summary["status"]
    has_errors = bool(summary["errors"])
    if (status == "failed") != has_errors:
        raise ValueError("Notification summary status does not match errors")
    if status in {"initialized", "deferred"} and (
        summary["notifications_attempted"]
        or summary["notifications_delivered"]
    ):
        raise ValueError("Inactive notification summary contains attempts")
    if status != "initialized" and summary["baseline_count"]:
        raise ValueError("Notification summary contains an invalid baseline")
    return summary


def default_gmail_runner(storage: JobStorage) -> dict:
    client = GmailJobAlertClient.from_local_oauth(allow_interactive=False)
    return run_gmail_scan(storage=storage, client=client)


def default_notification_runner(storage: JobStorage) -> dict:
    return run_discord_notifications(storage)


def run_scheduled_scan(
    *,
    storage: JobStorage | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    now: datetime | None = None,
    gmail_runner: SourceRunner = default_gmail_runner,
    source_runners: Mapping[str, SourceRunner] | None = None,
    himalayas_runner: SourceRunner | None = None,
    notification_runner: NotificationRunner | None = None,
) -> dict:
    storage = storage or JobStorage(DEFAULT_GMAIL_DB_PATH)
    now = now or datetime.now(UTC)
    errors: list[dict[str, str]] = []
    if source_runners is not None and himalayas_runner is not None:
        raise ValueError("Use source_runners or himalayas_runner, not both")
    if source_runners is not None:
        runners = dict(source_runners)
    elif himalayas_runner is not None:
        runners = {"himalayas": himalayas_runner}
    else:
        runners = default_source_runners()
    unknown_sources = set(runners) - set(SOURCE_INTERVALS)
    if unknown_sources:
        raise ValueError("Scheduler received an unknown source")

    state_changed = False
    try:
        state = load_scheduler_state(state_path)
    except ValueError as error:
        state = {}
        state_changed = True
        errors.append(
            {"source": "scheduler_state", "error_type": type(error).__name__}
        )

    summary: dict = {
        "gmail": None,
        "sources": {},
        "discord": None,
        "errors": errors,
    }

    try:
        gmail_summary = validate_gmail_summary(gmail_runner(storage))
        summary["gmail"] = gmail_summary
        if gmail_summary["errors"]:
            errors.append({"source": "gmail", "error_type": "GmailMessageErrors"})
    except Exception as error:
        errors.append({"source": "gmail", "error_type": type(error).__name__})

    state_writable = True
    for source, runner in runners.items():
        due = source_scan_due(state, source, now, SOURCE_INTERVALS[source])
        source_result = {"due": due, "status": "not_due", "summary": None}
        summary["sources"][source] = source_result
        if not due:
            continue
        if not state_writable:
            source_result["status"] = "failed"
            continue

        attempt_key = f"{source}_last_attempt_at"
        previous_attempt = state.get(attempt_key)
        state[attempt_key] = now.isoformat()
        try:
            write_scheduler_state(state_path, state)
            state_changed = False
        except Exception as error:
            if previous_attempt is None:
                state.pop(attempt_key, None)
            else:
                state[attempt_key] = previous_attempt
            source_result["status"] = "failed"
            state_writable = False
            errors.append(
                {"source": "scheduler_state", "error_type": type(error).__name__}
            )
            continue

        try:
            source_summary = validate_source_summary(runner(storage))
            source_result["summary"] = source_summary
            if source_summary["errors"]:
                source_result["status"] = "failed"
                errors.append(
                    {"source": source, "error_type": "SourceItemErrors"}
                )
                continue
            source_result["status"] = "ok"
            state[f"{source}_last_success_at"] = now.isoformat()
            state_changed = True
        except SourceNotConfigured:
            if previous_attempt is None:
                state.pop(attempt_key, None)
            else:
                state[attempt_key] = previous_attempt
            try:
                write_scheduler_state(state_path, state)
            except Exception as error:
                state_writable = False
                errors.append(
                    {
                        "source": "scheduler_state",
                        "error_type": type(error).__name__,
                    }
                )
            source_result["status"] = "not_configured"
        except Exception as error:
            source_result["status"] = "failed"
            errors.append({"source": source, "error_type": type(error).__name__})

    if state_changed:
        try:
            write_scheduler_state(state_path, state)
        except Exception as error:
            errors.append(
                {"source": "scheduler_state", "error_type": type(error).__name__}
            )

    if notification_runner is not None:
        try:
            discord_summary = validate_notification_summary(
                notification_runner(storage)
            )
            summary["discord"] = discord_summary
            if discord_summary["errors"]:
                errors.append(
                    {"source": "discord", "error_type": "DiscordDeliveryErrors"}
                )
        except DiscordNotConfigured:
            summary["discord"] = {"status": "not_configured"}
        except Exception as error:
            summary["discord"] = {"status": "failed"}
            errors.append(
                {"source": "discord", "error_type": type(error).__name__}
            )

    return summary


def print_summary(summary: dict) -> None:
    print(f"Jobbot scheduled scan at {datetime.now(UTC).isoformat()}")
    gmail = summary["gmail"]
    if gmail is None:
        print("Gmail: failed")
    else:
        print(
            "Gmail: "
            f"found={gmail['messages_found']} "
            f"processed={gmail['messages_processed']} "
            f"created={gmail['jobs_created']} "
            f"errors={len(gmail['errors'])}"
        )

    for source, result in summary["sources"].items():
        source_summary = result["summary"]
        if result["status"] in {"not_due", "not_configured"}:
            print(f"{source}: {result['status'].replace('_', ' ')}")
        elif source_summary is None:
            print(f"{source}: failed")
        else:
            print(
                f"{source}: status={result['status']} "
                f"fetched={source_summary['jobs_fetched']} "
                f"created={source_summary['jobs_created']} "
                f"duplicates={source_summary['duplicates_skipped']} "
                f"rejected={source_summary['records_rejected']} "
                f"errors={len(source_summary['errors'])}"
            )

    discord = summary.get("discord")
    if discord is None:
        print("discord: disabled")
    elif discord["status"] == "not_configured":
        print("discord: not configured")
    elif discord == {"status": "failed"}:
        print("discord: failed")
    else:
        print(
            "discord: "
            f"status={discord['status']} "
            f"eligible={discord['eligible_jobs']} "
            f"baseline={discord['baseline_count']} "
            f"attempted={discord['notifications_attempted']} "
            f"delivered={discord['notifications_delivered']} "
            f"attention={discord['notifications_attention_required']} "
            f"errors={len(discord['errors'])}"
        )

    print(f"Source errors: {len(summary['errors'])}")
    for error in summary["errors"]:
        print(f"- {error['source']}: {error['error_type']}")


@contextmanager
def scheduler_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_scheduler_health(
    success: bool,
    error_count: int,
    health_path: Path = DEFAULT_HEALTH_PATH,
) -> None:
    write_scheduler_state(
        health_path,
        {
            "completed_at": datetime.now(UTC).isoformat(),
            "error_count": error_count,
            "install_nonce": os.environ.get("JOBBOT_SCHEDULER_INSTALL_NONCE"),
            "success": success,
        },
    )


def main() -> None:
    with scheduler_lock() as acquired:
        if not acquired:
            print("Jobbot scheduled scan skipped: another scan is running")
            return
        try:
            summary = run_scheduled_scan(
                notification_runner=default_notification_runner,
            )
            print_summary(summary)
            success = not summary["errors"]
            write_scheduler_health(success, len(summary["errors"]))
        except Exception:
            write_scheduler_health(False, 1)
            raise
        if not success:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
