from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.gmail_client import DEFAULT_GMAIL_DB_PATH
from app.job_sources import ParsedJobs, fetch_employer_board, load_employer_watchlist
from app.storage import JobStorage
from scripts.source_scan_utils import ingest_source_jobs


def run_employer_watchlist_scan(storage: JobStorage | None = None) -> dict:
    storage = storage or JobStorage(DEFAULT_GMAIL_DB_PATH)
    jobs = ParsedJobs()
    errors: list[dict[str, str]] = []
    for entry in load_employer_watchlist():
        try:
            jobs.extend_batch(fetch_employer_board(entry))
        except Exception as error:
            errors.append(
                {
                    "provider": entry["provider"],
                    "site": entry["site"],
                    "error_type": type(error).__name__,
                }
            )
    return ingest_source_jobs(jobs, storage, errors)


def main() -> None:
    summary = run_employer_watchlist_scan()
    print("Employer watchlist scan complete")
    print(f"Jobs fetched: {summary['jobs_fetched']}")
    print(f"Jobs created: {summary['jobs_created']}")
    print(f"Duplicates skipped: {summary['duplicates_skipped']}")
    print(f"Board errors: {len(summary['errors'])}")
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
