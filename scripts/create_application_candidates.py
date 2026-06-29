import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.candidates import (
    DEFAULT_APPLICATION_THRESHOLD,
    create_application_candidates,
)
from app.storage import JobStorage


def run_candidate_creation(
    storage: JobStorage | None = None,
    threshold: int = DEFAULT_APPLICATION_THRESHOLD,
) -> dict:
    storage = storage or JobStorage()
    created_applications = create_application_candidates(storage, threshold)
    jobs_by_id = {job["id"]: job for job in storage.list_jobs()}

    return {
        "threshold": threshold,
        "created_count": len(created_applications),
        "candidates": [
            {
                "title": jobs_by_id[application["job_id"]]["title"],
                "company": jobs_by_id[application["job_id"]]["company"],
                "fit_score": jobs_by_id[application["job_id"]].get(
                    "fit_score",
                    0,
                ),
                "status": application["status"],
            }
            for application in created_applications
        ],
    }


def print_summary(summary: dict) -> None:
    print(f"Application threshold: {summary['threshold']}")
    print(f"New candidates created: {summary['created_count']}")

    if not summary["candidates"]:
        print("No new application candidates met the threshold.")
        return

    for candidate in summary["candidates"]:
        print(
            f"- {candidate['title']} at {candidate['company']} "
            f"| fit score: {candidate['fit_score']} "
            f"| status: {candidate['status']}"
        )


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create application candidates from strong saved jobs.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_APPLICATION_THRESHOLD,
        help=(
            "Minimum fit score required to create a candidate "
            f"(default: {DEFAULT_APPLICATION_THRESHOLD})."
        ),
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    summary = run_candidate_creation(threshold=args.threshold)
    print_summary(summary)


if __name__ == "__main__":
    main()
