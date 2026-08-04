import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.candidate_profile import default_candidate_profile
from app.scoring_benchmark import evaluate_scoring, load_scoring_labels
from app.storage import JobStorage


DEFAULT_LABELS_PATH = PROJECT_ROOT / "data" / "scoring_labels.json"


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Jobbot scoring against human labels.",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_PATH,
        help="Path to the private scoring-label JSON file.",
    )
    parser.add_argument(
        "--split",
        choices=("calibration", "validation"),
        default="validation",
        help="Label split to evaluate.",
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    result = evaluate_scoring(
        JobStorage().list_jobs(),
        load_scoring_labels(args.labels),
        default_candidate_profile(),
        split=args.split,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
