import json
from pathlib import Path

import pytest

from app.candidate_profile import load_candidate_profile
from app.dedupe import content_hash
from app.models import JobPosting
from app.scoring_benchmark import (
    evaluate_scoring,
    load_scoring_labels,
    posting_fingerprint,
)


def test_scoring_benchmark_reports_validation_metrics(tmp_path: Path) -> None:
    labels_data = []
    jobs = []
    for job_id in range(1, 51):
        if job_id <= 20:
            label = "reject"
            title = "Business Analyst"
            location = "New York"
            description = "Analyze business operations."
        elif job_id <= 35:
            label = "review"
            title = "Junior Software Engineer"
            location = "Remote"
            description = "Build internal services."
        else:
            label = "strong"
            title = "Junior Frontend Full Stack Software Engineer"
            location = "Minneapolis, Minnesota"
            description = "Build React TypeScript cloud applications."
        posting_data = {
            "title": title,
            "company": "Example",
            "location": location,
            "url": f"https://example.com/jobs/{job_id}",
            "source": "benchmark",
            "description": description,
        }
        fingerprint = posting_fingerprint(
            JobPosting.model_validate(posting_data)
        )
        labels_data.append(
            {
                "job_id": job_id,
                "posting_fingerprint": fingerprint,
                "job_url": f"https://example.com/jobs/{job_id}",
                "label": label,
                "split": "validation",
            }
        )
        jobs.append(
            {
                "id": job_id,
                **posting_data,
                "content_hash": content_hash("Example", title, location),
                "fit_score": -999,
            }
        )
    labels_data.append(
        {
            "job_id": 51,
            "posting_fingerprint": "0" * 64,
            "job_url": "https://example.com/jobs/51",
            "label": "strong",
            "split": "calibration",
        }
    )
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(labels_data),
        encoding="utf-8",
    )
    labels = load_scoring_labels(path)

    result = evaluate_scoring(
        jobs,
        labels,
        load_candidate_profile(Path("config/candidate_profile.example.yaml")),
    )

    assert result["jobs_evaluated"] == 50
    assert result["valid_for_accuracy_claim"] is True
    assert result["accuracy"] == 1.0
    assert result["balanced_accuracy"] == 1.0
    assert result["reviewable_precision"] == 1.0


def test_scoring_labels_reject_duplicate_job_ids(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        '[{"job_id": 1, "posting_fingerprint": "' + "0" * 64 + '", '
        '"job_url": "https://example.com/1", "label": "reject"}, '
        '{"job_id": 1, "posting_fingerprint": "' + "1" * 64 + '", '
        '"job_url": "https://example.com/2", "label": "strong"}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_scoring_labels(path)


def test_benchmark_refuses_accuracy_claim_with_missing_jobs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "labels.json"
    labels = [
        {
            "job_id": job_id,
            "posting_fingerprint": "0" * 64,
            "job_url": f"https://example.com/jobs/{job_id}",
            "label": ("reject", "review", "strong")[job_id % 3],
            "split": "validation",
        }
        for job_id in range(1, 51)
    ]
    path.write_text(json.dumps(labels), encoding="utf-8")

    result = evaluate_scoring(
        [
            {
                "id": 1,
                "title": "Business Analyst",
                "company": "Example",
                "location": "New York",
                "url": "https://example.com/jobs/1",
                "source": "benchmark",
                "content_hash": "0" * 64,
            }
        ],
        load_scoring_labels(path),
        load_candidate_profile(Path("config/candidate_profile.example.yaml")),
    )

    assert result["jobs_missing"] == 49
    assert result["valid_for_accuracy_claim"] is False
    assert result["accuracy"] is None
    assert result["reviewable_precision"] is None


def test_benchmark_recomputes_identity_and_detects_description_drift(
    tmp_path: Path,
) -> None:
    original = JobPosting(
        title="Business Analyst",
        company="Example",
        location="New York",
        url="https://example.com/jobs/1",
        source="benchmark",
        description="Analyze business operations.",
    )
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            [
                {
                    "job_id": 1,
                    "posting_fingerprint": posting_fingerprint(original),
                    "job_url": str(original.url),
                    "label": "reject",
                }
            ]
        ),
        encoding="utf-8",
    )
    changed = original.model_dump(mode="json")
    changed.update(
        {
            "id": 1,
            "description": "React TypeScript cloud software engineering.",
            "content_hash": "0" * 64,
        }
    )

    result = evaluate_scoring(
        [changed],
        load_scoring_labels(path),
        load_candidate_profile(Path("config/candidate_profile.example.yaml")),
    )

    assert result["job_identity_mismatches"] == 1
    assert result["jobs_evaluated"] == 0
    assert result["valid_for_accuracy_claim"] is False


@pytest.mark.parametrize("job_id", [True, "1", 1.0])
def test_scoring_labels_require_strict_integer_ids(
    tmp_path: Path,
    job_id: object,
) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            [
                {
                    "job_id": job_id,
                    "posting_fingerprint": "0" * 64,
                    "job_url": "https://example.com/1",
                    "label": "reject",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_scoring_labels(path)
