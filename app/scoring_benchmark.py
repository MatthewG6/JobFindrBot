import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictInt

from app.candidate_profile import CandidateProfile
from app.models import JobPosting
from app.scoring import score_job


FitLabel = Literal["reject", "review", "strong"]
BenchmarkSplit = Literal["calibration", "validation"]
LABELS: tuple[FitLabel, ...] = ("reject", "review", "strong")
MINIMUM_VALIDATION_LABELS = 50
MINIMUM_LABELS_PER_CLASS = 5
MINIMUM_REVIEWABLE_PREDICTIONS = 10


class ScoringLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: StrictInt = Field(gt=0)
    posting_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_url: HttpUrl
    label: FitLabel
    split: BenchmarkSplit = "validation"
    labeled_at: datetime | None = None


def load_scoring_labels(path: Path) -> list[ScoringLabel]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Scoring labels are not readable JSON") from error
    if not isinstance(content, list):
        raise ValueError("Scoring labels must be a JSON list")
    labels = [ScoringLabel.model_validate(item) for item in content]
    job_ids = [label.job_id for label in labels]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("Scoring labels contain duplicate job IDs")
    return labels


def classify_score(score: object, profile: CandidateProfile) -> FitLabel:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return "reject"
    if score >= profile.thresholds.strong:
        return "strong"
    if score >= profile.thresholds.review:
        return "review"
    return "reject"


def safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def posting_fingerprint(posting: JobPosting) -> str:
    """Bind a label to the complete immutable posting presented for review."""
    payload = posting.model_dump(
        mode="json",
        exclude={"created_at", "discovered_at"},
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_scoring(
    jobs: list[dict],
    labels: list[ScoringLabel],
    profile: CandidateProfile,
    split: BenchmarkSplit = "validation",
) -> dict:
    jobs_by_id = {job.get("id"): job for job in jobs}
    selected = [label for label in labels if label.split == split]
    confusion = {
        actual: {predicted: 0 for predicted in LABELS}
        for actual in LABELS
    }
    missing = 0
    invalid = 0
    identity_mismatches = 0
    correct = 0
    evaluated = 0
    predicted_reviewable = 0
    correct_reviewable = 0

    for label in selected:
        job = jobs_by_id.get(label.job_id)
        if job is None:
            missing += 1
            continue
        try:
            posting = JobPosting.model_validate(job)
        except ValueError:
            invalid += 1
            continue
        if (
            posting_fingerprint(posting) != label.posting_fingerprint
            or str(posting.url) != str(label.job_url)
        ):
            identity_mismatches += 1
            continue
        predicted = classify_score(score_job(posting, profile).score, profile)
        confusion[label.label][predicted] += 1
        evaluated += 1
        if predicted == label.label:
            correct += 1
        if predicted in {"review", "strong"}:
            predicted_reviewable += 1
            if label.label in {"review", "strong"}:
                correct_reviewable += 1

    recalls = []
    for actual in LABELS:
        total = sum(confusion[actual].values())
        recall = safe_ratio(confusion[actual][actual], total)
        if recall is not None:
            recalls.append(recall)

    class_counts = {
        actual: sum(confusion[actual].values()) for actual in LABELS
    }
    validation_issues = []
    if split != "validation":
        validation_issues.append("Only the validation split supports accuracy claims")
    if len(selected) < MINIMUM_VALIDATION_LABELS:
        validation_issues.append(
            f"At least {MINIMUM_VALIDATION_LABELS} validation labels are required"
        )
    if missing:
        validation_issues.append("Every labeled job must exist in the database")
    if invalid:
        validation_issues.append("Every labeled job must be a valid posting")
    if identity_mismatches:
        validation_issues.append("Every label must match its immutable job identity")
    if any(count < MINIMUM_LABELS_PER_CLASS for count in class_counts.values()):
        validation_issues.append(
            f"Each class requires at least {MINIMUM_LABELS_PER_CLASS} labels"
        )
    if predicted_reviewable < MINIMUM_REVIEWABLE_PREDICTIONS:
        validation_issues.append(
            "At least "
            f"{MINIMUM_REVIEWABLE_PREDICTIONS} reviewable predictions are required"
        )
    valid_for_accuracy_claim = not validation_issues
    diagnostic_accuracy = safe_ratio(correct, evaluated)
    diagnostic_balanced_accuracy = (
        round(sum(recalls) / len(recalls), 4) if recalls else None
    )

    return {
        "split": split,
        "labels_selected": len(selected),
        "jobs_evaluated": evaluated,
        "jobs_missing": missing,
        "jobs_invalid": invalid,
        "job_identity_mismatches": identity_mismatches,
        "valid_for_accuracy_claim": valid_for_accuracy_claim,
        "validation_issues": validation_issues,
        "accuracy": diagnostic_accuracy if valid_for_accuracy_claim else None,
        "balanced_accuracy": (
            diagnostic_balanced_accuracy if valid_for_accuracy_claim else None
        ),
        "diagnostic_accuracy": diagnostic_accuracy,
        "diagnostic_balanced_accuracy": diagnostic_balanced_accuracy,
        "reviewable_precision": (
            safe_ratio(correct_reviewable, predicted_reviewable)
            if valid_for_accuracy_claim
            else None
        ),
        "diagnostic_reviewable_precision": safe_ratio(
            correct_reviewable,
            predicted_reviewable,
        ),
        "reviewable_predictions": predicted_reviewable,
        "class_counts": class_counts,
        "confusion_matrix": confusion,
    }
