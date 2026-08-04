import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.employer_resolver import (
    resolve_employer_sites,
    set_manual_application_url,
)
from app.storage import JobStorage


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve discovery jobs to official application URLs.",
    )
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--application-url")
    args = parser.parse_args(arguments)
    if (args.job_id is None) != (args.application_url is None):
        parser.error("--job-id and --application-url must be used together")
    return args


def main() -> None:
    args = parse_args()
    storage = JobStorage()
    if args.job_id is not None:
        job = set_manual_application_url(
            storage,
            args.job_id,
            args.application_url,
        )
        result = {
            "job_id": job["id"],
            "resolution_status": job["resolution_status"],
            "resolution_method": job["resolution_method"],
        }
    else:
        result = resolve_employer_sites(storage)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
