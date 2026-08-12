from pathlib import Path

from app.candidates import DEFAULT_APPLICATION_THRESHOLD
from app.storage import JobStorage
from scripts.create_application_candidates import (
    parse_args,
    print_summary,
    run_candidate_creation,
)


def save_scored_job(
    storage: JobStorage,
    title: str,
    fit_score: int,
) -> None:
    slug = title.lower().replace(" ", "-")
    storage.save_job(
        {
            "title": title,
            "company": "Example Company",
            "location": "Remote",
            "url": f"https://example.com/jobs/{slug}",
            "source": "test",
            "fit_score": fit_score,
            "scoring_version": 2,
            "content_hash": slug,
        }
    )


def test_run_candidate_creation_returns_created_candidate(
    tmp_path: Path,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    save_scored_job(storage, "Strong Match", 85)
    save_scored_job(storage, "Below Threshold", 74)

    summary = run_candidate_creation(storage, threshold=80)

    assert summary["threshold"] == 80
    assert summary["created_count"] == 1
    assert summary["candidates"] == [
        {
            "title": "Strong Match",
            "company": "Example Company",
            "fit_score": 85,
            "status": "awaiting_start_approval",
        }
    ]


def test_print_summary_handles_no_new_candidates(
    tmp_path: Path,
    capsys,
) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    summary = run_candidate_creation(storage)
    print_summary(summary)
    output = capsys.readouterr().out

    assert f"Application threshold: {DEFAULT_APPLICATION_THRESHOLD}" in output
    assert "New candidates created: 0" in output
    assert "No new application candidates met the threshold." in output


def test_parse_args_supports_optional_threshold() -> None:
    default_args = parse_args([])
    custom_args = parse_args(["--threshold", "90"])

    assert default_args.threshold == DEFAULT_APPLICATION_THRESHOLD
    assert custom_args.threshold == 90
