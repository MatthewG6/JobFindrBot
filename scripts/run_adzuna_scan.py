from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.gmail_client import DEFAULT_GMAIL_DB_PATH
from app.job_sources import fetch_adzuna_jobs
from app.source_credentials import source_credentials
from app.storage import JobStorage
from scripts.source_scan_utils import ingest_source_jobs


def run_adzuna_scan(storage: JobStorage | None = None) -> dict:
    storage = storage or JobStorage(DEFAULT_GMAIL_DB_PATH)
    credentials = source_credentials("adzuna", ("app_id", "app_key"))
    jobs = fetch_adzuna_jobs(**credentials)
    return ingest_source_jobs(jobs, storage)


def main() -> None:
    summary = run_adzuna_scan()
    print("Adzuna scan complete")
    print(f"Jobs fetched: {summary['jobs_fetched']}")
    print(f"Jobs created: {summary['jobs_created']}")
    print(f"Duplicates skipped: {summary['duplicates_skipped']}")


if __name__ == "__main__":
    main()
