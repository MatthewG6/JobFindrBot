import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.job_enrichment import enrich_resolved_jobs
from app.storage import JobStorage


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich resolved jobs from official posting data.",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=10,
        help="Maximum jobs to attempt in this run.",
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    summary = enrich_resolved_jobs(JobStorage(), max_jobs=args.max_jobs)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
