import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.discord_notifications import (
    DISCORD_CHANNEL,
    DiscordWebhookClient,
    run_discord_notifications,
)
from app.storage import JobStorage


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test or run Jobbot Discord notifications.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--test",
        action="store_true",
        help="Send one connection test without changing notification state.",
    )
    actions.add_argument(
        "--list-attention",
        action="store_true",
        help="List notifications that require manual reconciliation.",
    )
    actions.add_argument(
        "--mark-delivered",
        type=int,
        metavar="JOB_ID",
        help="Confirm that an unresolved job was delivered.",
    )
    actions.add_argument(
        "--retry",
        type=int,
        metavar="JOB_ID",
        help="Authorize one retry for an unresolved job.",
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    storage = JobStorage()
    if args.list_attention:
        notifications = storage.list_attention_required_notifications(
            DISCORD_CHANNEL
        )
        print(f"Notifications requiring attention: {len(notifications)}")
        for notification in notifications:
            print(
                f"job={notification['job_id']} "
                f"status={notification['status']} "
                f"attempts={notification.get('attempts', 0)}"
            )
        return
    if args.mark_delivered is not None:
        storage.resolve_job_notification(
            DISCORD_CHANNEL,
            args.mark_delivered,
            "delivered",
        )
        print(f"Job {args.mark_delivered} marked delivered")
        return
    if args.retry is not None:
        storage.resolve_job_notification(
            DISCORD_CHANNEL,
            args.retry,
            "retry",
        )
        print(f"Job {args.retry} approved for retry")
        return

    client = DiscordWebhookClient.from_local_credentials()
    if args.test:
        client.send_test()
        print("Discord connection test delivered")
        return

    summary = run_discord_notifications(
        storage,
        client=client,
    )
    print("Discord notification scan complete")
    print(f"Status: {summary['status']}")
    print(f"Eligible jobs: {summary['eligible_jobs']}")
    print(f"Baselined jobs: {summary['baseline_count']}")
    print(f"Notifications attempted: {summary['notifications_attempted']}")
    print(f"Notifications delivered: {summary['notifications_delivered']}")
    print(
        "Notifications requiring attention: "
        f"{summary['notifications_attention_required']}"
    )
    print(f"Errors: {len(summary['errors'])}")
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
