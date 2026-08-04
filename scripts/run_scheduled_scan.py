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

from app.gmail_client import (
    DEFAULT_GMAIL_DB_PATH,
    GmailJobAlertClient,
)
from app.storage import JobStorage
from scripts.run_gmail_scan import run_gmail_scan
from scripts.run_himalayas_scan import run_himalayas_scan


DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "scheduler_state.json"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / "scheduled_scan.lock"
DEFAULT_HEALTH_PATH = PROJECT_ROOT / "data" / "scheduler_health.json"
HIMALAYAS_INTERVAL = timedelta(hours=24)
SourceRunner = Callable[[JobStorage], dict]


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


def himalayas_scan_due(
    state: dict,
    now: datetime,
    interval: timedelta = HIMALAYAS_INTERVAL,
) -> bool:
    value = state.get("himalayas_last_success_at")
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


def validate_himalayas_summary(summary: object) -> dict:
    if not isinstance(summary, dict):
        raise ValueError("Himalayas runner returned an invalid summary")
    for key in ("jobs_fetched", "jobs_created", "duplicates_skipped"):
        required_nonnegative_int(summary, key)
    return summary


def default_gmail_runner(storage: JobStorage) -> dict:
    client = GmailJobAlertClient.from_local_oauth(allow_interactive=False)
    return run_gmail_scan(storage=storage, client=client)


def default_himalayas_runner(storage: JobStorage) -> dict:
    return run_himalayas_scan(storage=storage)


def run_scheduled_scan(
    *,
    storage: JobStorage | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    now: datetime | None = None,
    gmail_runner: SourceRunner = default_gmail_runner,
    himalayas_runner: SourceRunner = default_himalayas_runner,
) -> dict:
    storage = storage or JobStorage(DEFAULT_GMAIL_DB_PATH)
    now = now or datetime.now(UTC)
    errors: list[dict[str, str]] = []

    try:
        state = load_scheduler_state(state_path)
    except ValueError as error:
        state = {}
        errors.append(
            {"source": "scheduler_state", "error_type": type(error).__name__}
        )

    summary = {
        "gmail": None,
        "himalayas": None,
        "himalayas_due": himalayas_scan_due(state, now),
        "errors": errors,
    }

    try:
        gmail_summary = validate_gmail_summary(gmail_runner(storage))
        summary["gmail"] = gmail_summary
        if gmail_summary.get("errors"):
            errors.append(
                {
                    "source": "gmail",
                    "error_type": "GmailMessageErrors",
                }
            )
    except Exception as error:
        errors.append({"source": "gmail", "error_type": type(error).__name__})

    if summary["himalayas_due"]:
        try:
            himalayas_summary = validate_himalayas_summary(
                himalayas_runner(storage)
            )
            summary["himalayas"] = himalayas_summary
            state["himalayas_last_success_at"] = now.isoformat()
            write_scheduler_state(state_path, state)
        except Exception as error:
            errors.append(
                {"source": "himalayas", "error_type": type(error).__name__}
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

    himalayas = summary["himalayas"]
    if not summary["himalayas_due"]:
        print("Himalayas: not due")
    elif himalayas is None:
        print("Himalayas: failed")
    else:
        print(
            "Himalayas: "
            f"fetched={himalayas['jobs_fetched']} "
            f"created={himalayas['jobs_created']} "
            f"duplicates={himalayas['duplicates_skipped']}"
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
            summary = run_scheduled_scan()
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
