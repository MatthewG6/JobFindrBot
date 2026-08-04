from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.gmail_client import DEFAULT_GMAIL_DB_PATH, GmailJobAlertClient
from app.storage import JobStorage


def run_gmail_scan(
    storage: JobStorage | None = None,
    client: GmailJobAlertClient | None = None,
) -> dict:
    storage = storage or JobStorage(DEFAULT_GMAIL_DB_PATH)
    client = client or GmailJobAlertClient.from_local_oauth()
    return client.scan(storage)


def print_summary(summary: dict) -> None:
    print("Gmail job alert scan complete")
    print(f"Messages found: {summary['messages_found']}")
    print(f"Messages fetched: {summary['messages_fetched']}")
    print(f"Messages processed: {summary['messages_processed']}")
    print(f"Messages skipped: {summary['messages_skipped']}")
    print(f"Jobs parsed: {summary['jobs_parsed']}")
    print(f"Jobs created: {summary['jobs_created']}")
    print(f"Errors: {len(summary['errors'])}")
    for error in summary["errors"]:
        print(f"- {error['message_id']}: {error['error_type']}")


def main() -> None:
    summary = run_gmail_scan()
    print_summary(summary)
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
