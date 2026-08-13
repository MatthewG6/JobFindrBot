import json
from pathlib import Path
import stat

import pytest

from app.candidate_profile import load_candidate_profile
from app.models import JobPosting
from app.scoring import score_job, scoring_fields
from app.scoring_labeling import (
    CALIBRATION_TARGET,
    SESSION_TARGET,
    VALIDATION_TARGET,
    DuplicateScoringLabel,
    LabelingSessionIncomplete,
    ScoringLabelStore,
    create_labeling_session,
    labeling_progress,
    next_labeling_job,
    record_scoring_label,
    session_benchmark,
)


PROFILE = load_candidate_profile(Path("config/candidate_profile.example.yaml"))


def make_job(job_id: int, predicted: str, source: str = "test") -> dict:
    if predicted == "strong":
        title = "Junior Software Engineer"
        location = "Remote"
        description = "Build React TypeScript cloud applications. " * 15
    elif predicted == "review":
        title = "Software Engineer"
        location = "Remote"
        description = "Build internal applications and maintain services. " * 12
    else:
        title = "Business Analyst"
        location = "New York"
        description = "Analyze business processes and prepare reports. " * 12
    posting = JobPosting(
        title=title,
        company=f"Example {job_id}",
        location=location,
        url=f"https://example.com/jobs/{job_id}",
        source=source,
        description=description,
    )
    return {
        "id": job_id,
        **posting.model_dump(mode="json"),
        **scoring_fields(score_job(posting, PROFILE)),
    }


def make_session_jobs() -> list[dict]:
    jobs = []
    job_id = 1
    for predicted, count in (("reject", 50), ("review", 40), ("strong", 20)):
        for offset in range(count):
            jobs.append(make_job(job_id, predicted, source=f"source-{offset % 4}"))
            job_id += 1
    return jobs


def test_create_labeling_session_is_fixed_stratified_and_idempotent(
    tmp_path: Path,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()

    first = create_labeling_session(jobs, store, PROFILE)
    second = create_labeling_session(list(reversed(jobs)), store, PROFILE)

    assert len(first.entries) == SESSION_TARGET == 75
    assert first == second
    calibration_predictions = [
        entry.predicted_label
        for entry in first.entries
        if entry.split == "calibration"
    ]
    assert calibration_predictions != sorted(calibration_predictions)
    assert sum(entry.split == "calibration" for entry in first.entries) == (
        CALIBRATION_TARGET
    )
    assert sum(entry.split == "validation" for entry in first.entries) == (
        VALIDATION_TARGET
    )
    assert {
        split: {
            label: sum(
                entry.split == split and entry.predicted_label == label
                for entry in first.entries
            )
            for label in ("reject", "review", "strong")
        }
        for split in ("calibration", "validation")
    } == {
        "calibration": {"reject": 14, "review": 7, "strong": 4},
        "validation": {"reject": 29, "review": 13, "strong": 8},
    }


def test_create_labeling_session_requires_enough_jobs_per_stratum(
    tmp_path: Path,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )

    with pytest.raises(ValueError, match="eligible"):
        create_labeling_session(
            [make_job(job_id, "reject") for job_id in range(1, 76)],
            store,
            PROFILE,
        )


def test_create_session_refuses_orphaned_existing_labels(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    store.labels_path.write_text("[]", encoding="utf-8")
    store.labels_path.write_text(
        json.dumps(
            [
                {
                    "job_id": 999,
                    "posting_fingerprint": "0" * 64,
                    "job_url": "https://example.com/jobs/999",
                    "label": "reject",
                    "split": "validation",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="original session"):
        create_labeling_session(make_session_jobs(), store, PROFILE)


def test_next_job_is_blind_and_recording_is_owner_only_and_immutable(
    tmp_path: Path,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    create_labeling_session(jobs, store, PROFILE)

    next_job = next_labeling_job(store)

    assert next_job is not None
    assert "fit_score" not in next_job.job
    assert "score_dimensions" not in next_job.job
    assert "predicted_label" not in next_job.model_dump(mode="json")

    result = record_scoring_label(
        store,
        job_id=next_job.job["id"],
        label="strong",
    )

    assert result.label == "strong"
    assert stat.S_IMODE(store.labels_path.stat().st_mode) == 0o600
    assert json.loads(store.labels_path.read_text(encoding="utf-8"))[0][
        "job_id"
    ] == next_job.job["id"]

    with pytest.raises(DuplicateScoringLabel):
        record_scoring_label(
            store,
            job_id=next_job.job["id"],
            label="reject",
        )


def test_live_posting_drift_does_not_change_fixed_snapshot(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    session = create_labeling_session(jobs, store, PROFILE)
    target = session.entries[0]
    changed_jobs = [dict(job) for job in jobs]
    changed = next(job for job in changed_jobs if job["id"] == target.job_id)
    changed["description"] = "The posting changed after queue creation."

    next_job = next_labeling_job(store)
    assert next_job is not None
    assert next_job.job["description"] != changed["description"]
    decision = record_scoring_label(
        store,
        job_id=target.job_id,
        label="review",
    )
    assert decision.posting_fingerprint == target.posting_fingerprint


def test_legacy_queue_without_optional_workplace_type_remains_valid(
    tmp_path: Path,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    create_labeling_session(make_session_jobs(), store, PROFILE)
    content = json.loads(store.queue_path.read_text(encoding="utf-8"))
    for entry in content["entries"]:
        entry["posting"].pop("workplace_type", None)
    store.queue_path.write_text(json.dumps(content), encoding="utf-8")

    assert labeling_progress(store).total == SESSION_TARGET


def test_workplace_type_value_remains_integrity_bound(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    create_labeling_session(make_session_jobs(), store, PROFILE)
    content = json.loads(store.queue_path.read_text(encoding="utf-8"))
    content["entries"][0]["posting"]["workplace_type"] = "hybrid"
    store.queue_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot"):
        labeling_progress(store)


def test_recording_rejects_out_of_order_label(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    session = create_labeling_session(jobs, store, PROFILE)

    with pytest.raises(ValueError, match="queue order"):
        record_scoring_label(
            store,
            job_id=session.entries[-1].job_id,
            label="reject",
        )


def test_progress_rejects_label_metadata_tampering(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    create_labeling_session(jobs, store, PROFILE)
    next_job = next_labeling_job(store)
    assert next_job is not None
    record_scoring_label(
        store,
        job_id=next_job.job["id"],
        label="review",
    )
    content = json.loads(store.labels_path.read_text(encoding="utf-8"))
    content[0]["split"] = (
        "validation" if content[0]["split"] == "calibration" else "calibration"
    )
    store.labels_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ValueError, match="session entry"):
        labeling_progress(store)


def test_progress_rejects_skipped_or_reordered_label_prefix(
    tmp_path: Path,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    session = create_labeling_session(jobs, store, PROFILE)
    second = session.entries[1]
    store.labels_path.write_text(
        json.dumps(
            [
                {
                    "job_id": second.job_id,
                    "posting_fingerprint": second.posting_fingerprint,
                    "job_url": str(second.job_url),
                    "label": "review",
                    "split": second.split,
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exact session prefix"):
        labeling_progress(store)

    store.labels_path.unlink()
    for entry in session.entries[:2]:
        record_scoring_label(
            store,
            job_id=entry.job_id,
            label="review",
        )
    content = json.loads(store.labels_path.read_text(encoding="utf-8"))
    store.labels_path.write_text(
        json.dumps(list(reversed(content))),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exact session prefix"):
        labeling_progress(store)


def test_progress_rejects_queue_snapshot_tampering(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    create_labeling_session(make_session_jobs(), store, PROFILE)
    content = json.loads(store.queue_path.read_text(encoding="utf-8"))
    content["entries"][0]["posting"]["description"] = "tampered"
    store.queue_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot"):
        labeling_progress(store)


def test_progress_rejects_review_url_or_stratum_tampering(tmp_path: Path) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    create_labeling_session(make_session_jobs(), store, PROFILE)
    original = json.loads(store.queue_path.read_text(encoding="utf-8"))

    changed_url = json.loads(json.dumps(original))
    changed_url["entries"][0]["review_url"] = "https://example.com/changed"
    store.queue_path.write_text(json.dumps(changed_url), encoding="utf-8")
    with pytest.raises(ValueError, match="session fingerprint"):
        labeling_progress(store)

    same_label_by_split = {}
    for index, entry in enumerate(original["entries"]):
        same_label_by_split[(entry["predicted_label"], entry["split"])] = index
    pair = next(
        (
            same_label_by_split[(label, "calibration")],
            same_label_by_split[(label, "validation")],
        )
        for label in ("reject", "review", "strong")
        if (label, "calibration") in same_label_by_split
        and (label, "validation") in same_label_by_split
    )
    changed_split = json.loads(json.dumps(original))
    first, second = pair
    changed_split["entries"][first]["split"] = "validation"
    changed_split["entries"][second]["split"] = "calibration"
    store.queue_path.write_text(json.dumps(changed_split), encoding="utf-8")
    with pytest.raises(ValueError, match="session fingerprint"):
        labeling_progress(store)


def test_validation_benchmark_remains_hidden_until_holdout_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ScoringLabelStore(
        queue_path=tmp_path / "queue.json",
        labels_path=tmp_path / "labels.json",
    )
    jobs = make_session_jobs()
    session = create_labeling_session(jobs, store, PROFILE)

    with pytest.raises(LabelingSessionIncomplete):
        session_benchmark(store, split="validation")

    for entry in session.entries:
        record_scoring_label(
            store,
            job_id=entry.job_id,
            label=entry.predicted_label,
        )

    result = session_benchmark(store, split="validation")

    assert result["valid_for_accuracy_claim"] is True
    assert result["accuracy"] == 1.0
    assert labeling_progress(store).validation_labeled == VALIDATION_TARGET

    def fail_if_live_scorer_runs(*args, **kwargs):
        raise AssertionError("completed benchmark must not run the live scorer")

    monkeypatch.setattr("app.scoring_labeling.score_job", fail_if_live_scorer_runs)
    frozen_result = session_benchmark(store, split="validation")
    assert frozen_result == result
