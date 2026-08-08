import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.dynamic_rendering import (
    DEFAULT_ALLOWLIST_PATH,
    enrich_dynamic_jobs,
    resolve_dynamic_provider_sites,
)
from app.storage import JobStorage


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render approved dynamic job pages without interaction.",
    )
    parser.add_argument("--max-resolution-jobs", type=int, default=2)
    parser.add_argument("--max-enrichment-jobs", type=int, default=2)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST_PATH,
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    storage = JobStorage()
    result = {
        "resolution": resolve_dynamic_provider_sites(
            storage,
            max_jobs=args.max_resolution_jobs,
        ),
        "enrichment": enrich_dynamic_jobs(
            storage,
            max_jobs=args.max_enrichment_jobs,
            allowlist_path=args.allowlist,
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
