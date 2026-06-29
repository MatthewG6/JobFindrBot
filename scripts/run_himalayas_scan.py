from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion import ingest_job
from app.scanner import fetch_himalayas_jobs
from app.storage import JobStorage


def run_himalayas_scan(storage: JobStorage | None = None) -> dict:
    storage = storage or JobStorage()
    jobs = fetch_himalayas_jobs()
    results = [ingest_job(job, storage) for job in jobs]
    created_count = sum(1 for result in results if result["created"])

    return {
        "jobs_fetched": len(jobs),
        "jobs_created": created_count,
        "duplicates_skipped": len(results) - created_count,
        "jobs": [
            {
                "title": result["job"]["title"],
                "company": result["job"]["company"],
                "url": result["job"]["url"],
                "fit_score": result["score"],
                "created": result["created"],
            }
            for result in results
        ],
    }


def print_summary(summary: dict) -> None:
    print("Himalayas scan complete")
    print(f"Jobs fetched: {summary['jobs_fetched']}")
    print(f"Jobs created: {summary['jobs_created']}")
    print(f"Duplicates skipped: {summary['duplicates_skipped']}")
    print("Source: Himalayas (https://himalayas.app)")

    for job in summary["jobs"]:
        status = "created" if job["created"] else "duplicate"
        print(
            f"- {job['title']} at {job['company']} "
            f"| fit score: {job['fit_score']} | {status}"
        )
        print(f"  {job['url']}")


def main() -> None:
    summary = run_himalayas_scan()
    print_summary(summary)


if __name__ == "__main__":
    main()
