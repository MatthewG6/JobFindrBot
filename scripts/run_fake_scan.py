from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion import ingest_job
from app.scanner import scan_jobs
from app.storage import JobStorage


def run_fake_scan(storage: JobStorage | None = None) -> dict:
    storage = storage or JobStorage()
    jobs = scan_jobs()
    results = [ingest_job(job, storage) for job in jobs]

    created_count = sum(1 for result in results if result["created"])
    duplicate_count = len(results) - created_count

    return {
        "jobs_scanned": len(jobs),
        "jobs_created": created_count,
        "duplicates_skipped": duplicate_count,
        "jobs": [
            {
                "title": result["job"]["title"],
                "fit_score": result["score"],
                "created": result["created"],
            }
            for result in results
        ],
    }


def print_summary(summary: dict) -> None:
    print("Fake scan complete")
    print(f"Jobs scanned: {summary['jobs_scanned']}")
    print(f"Jobs created: {summary['jobs_created']}")
    print(f"Duplicates skipped: {summary['duplicates_skipped']}")
    print("Saved job titles:")

    for job in summary["jobs"]:
        status = "created" if job["created"] else "duplicate"
        print(f"- {job['title']} | fit score: {job['fit_score']} | {status}")


def main() -> None:
    summary = run_fake_scan()
    print_summary(summary)


if __name__ == "__main__":
    main()
