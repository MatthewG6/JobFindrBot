from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictInt

from app.candidate_profile import CandidateProfile
from app.models import JobPosting
from app.scoring import SCORING_VERSION, score_job
from app.scoring_benchmark import (
    BenchmarkSplit,
    FitLabel,
    ScoringLabel,
    classify_score,
    evaluate_scoring,
    posting_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE_PATH = PROJECT_ROOT / "data" / "scoring_label_queue.json"
DEFAULT_LABELS_PATH = PROJECT_ROOT / "data" / "scoring_labels.json"
QUEUE_SCHEMA_VERSION = 1
CALIBRATION_TARGET = 25
VALIDATION_TARGET = 50
SESSION_TARGET = CALIBRATION_TARGET + VALIDATION_TARGET
MIN_LABELING_DESCRIPTION_CHARS = 500
SESSION_QUOTAS: dict[BenchmarkSplit, dict[FitLabel, int]] = {
    "calibration": {"reject": 14, "review": 7, "strong": 4},
    "validation": {"reject": 29, "review": 13, "strong": 8},
}
BLIND_JOB_FIELDS = (
    "id",
    "title",
    "company",
    "location",
    "description",
    "salary_text",
    "posted_text",
    "posted_at",
    "source",
    "url",
    "application_url",
    "enrichment_status",
)


class LabelingSessionError(ValueError):
    pass


class LabelingSessionNotFound(LabelingSessionError):
    pass


class LabelingSessionIncomplete(LabelingSessionError):
    pass


class DuplicateScoringLabel(LabelingSessionError):
    pass


class QueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: StrictInt = Field(gt=0)
    job_id: StrictInt = Field(gt=0)
    posting_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_url: HttpUrl
    review_url: HttpUrl
    posting: JobPosting
    split: BenchmarkSplit
    predicted_label: FitLabel


class LabelingSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt
    created_at: datetime
    entries: list[QueueEntry]


class LabelingProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    labeled: int
    calibration_total: int
    calibration_labeled: int
    validation_total: int
    validation_labeled: int
    phase: str
    complete: bool


class LabelingJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    split: BenchmarkSplit
    split_position: int
    split_total: int
    job: dict


class ScoringLabelStore:
    def __init__(
        self,
        *,
        queue_path: Path = DEFAULT_QUEUE_PATH,
        labels_path: Path = DEFAULT_LABELS_PATH,
    ) -> None:
        if queue_path.parent.resolve() != labels_path.parent.resolve():
            raise ValueError("Scoring queue and labels must share a directory")
        self.queue_path = queue_path
        self.labels_path = labels_path
        self.lock_path = labels_path.parent / "scoring_labels.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.labels_path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlinked_components(self.labels_path.parent)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load_session(self) -> LabelingSession:
        return _load_session(self.queue_path)

    def load_labels(self) -> list[ScoringLabel]:
        return _load_labels(self.labels_path)

    def write_session(self, session: LabelingSession) -> None:
        _atomic_json_write(
            self.queue_path,
            session.model_dump(mode="json"),
        )

    def write_labels(self, labels: list[ScoringLabel]) -> None:
        _atomic_json_write(
            self.labels_path,
            [label.model_dump(mode="json", exclude_none=True) for label in labels],
        )


def reject_symlinked_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in (absolute, *absolute.parents):
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError("Scoring label path must not contain symlinks")


def _atomic_json_write(path: Path, payload: object) -> None:
    reject_symlinked_components(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            descriptor = -1
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _read_json(path: Path, *, missing: object) -> object:
    if not path.exists():
        return missing
    if not path.is_file() or path.is_symlink():
        raise ValueError("Scoring label data must be a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Scoring label data is not readable JSON") from error


def _load_session(path: Path) -> LabelingSession:
    content = _read_json(path, missing=None)
    if content is None:
        raise LabelingSessionNotFound("Scoring labeling session does not exist")
    session = LabelingSession.model_validate(content)
    if session.schema_version != QUEUE_SCHEMA_VERSION:
        raise ValueError("Unsupported scoring labeling session version")
    if len(session.entries) != SESSION_TARGET:
        raise ValueError("Scoring labeling session has an invalid size")
    job_ids = [entry.job_id for entry in session.entries]
    sequences = [entry.sequence for entry in session.entries]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("Scoring labeling session contains duplicate jobs")
    if sequences != list(range(1, SESSION_TARGET + 1)):
        raise ValueError("Scoring labeling session sequence is invalid")
    quotas = {
        split: {
            label: sum(
                entry.split == split and entry.predicted_label == label
                for entry in session.entries
            )
            for label in ("reject", "review", "strong")
        }
        for split in ("calibration", "validation")
    }
    if quotas != SESSION_QUOTAS:
        raise ValueError("Scoring labeling session strata are invalid")
    for entry in session.entries:
        if (
            posting_fingerprint(entry.posting) != entry.posting_fingerprint
            or str(entry.posting.url) != str(entry.job_url)
        ):
            raise ValueError("Scoring labeling session snapshot is invalid")
    return session


def _load_labels(path: Path) -> list[ScoringLabel]:
    content = _read_json(path, missing=[])
    if not isinstance(content, list):
        raise ValueError("Scoring labels must be a JSON list")
    labels = [ScoringLabel.model_validate(item) for item in content]
    job_ids = [label.job_id for label in labels]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("Scoring labels contain duplicate job IDs")
    return labels


def _validate_session_labels(
    session: LabelingSession,
    labels: list[ScoringLabel],
) -> list[ScoringLabel]:
    entries = {entry.job_id: entry for entry in session.entries}
    for label in labels:
        entry = entries.get(label.job_id)
        if entry is None:
            raise ValueError("Scoring label is not part of the fixed session")
        if (
            label.posting_fingerprint != entry.posting_fingerprint
            or str(label.job_url) != str(entry.job_url)
            or label.split != entry.split
        ):
            raise ValueError("Scoring label does not match its session entry")
    return labels


def _stable_job_order(job: dict) -> str:
    posting = JobPosting.model_validate(job)
    fingerprint = posting_fingerprint(posting)
    return hashlib.sha256(f"jobbot-labeling-v1:{fingerprint}".encode()).hexdigest()


def _stable_entry_order(
    item: tuple[BenchmarkSplit, FitLabel, dict],
) -> str:
    split, _, job = item
    posting = JobPosting.model_validate(job)
    fingerprint = posting_fingerprint(posting)
    return hashlib.sha256(
        f"jobbot-labeling-v1:{split}:{fingerprint}".encode()
    ).hexdigest()


def _eligible_jobs(
    jobs: list[dict],
    profile: CandidateProfile,
) -> dict[FitLabel, list[dict]]:
    buckets: dict[FitLabel, list[dict]] = {
        "reject": [],
        "review": [],
        "strong": [],
    }
    for job in jobs:
        if job.get("scoring_version") != SCORING_VERSION:
            continue
        if len(str(job.get("description") or "").strip()) < (
            MIN_LABELING_DESCRIPTION_CHARS
        ):
            continue
        try:
            posting = JobPosting.model_validate(job)
        except ValueError:
            continue
        predicted = classify_score(score_job(posting, profile).score, profile)
        buckets[predicted].append(job)
    for bucket in buckets.values():
        bucket.sort(key=_stable_job_order)
    return buckets


def create_labeling_session(
    jobs: list[dict],
    store: ScoringLabelStore,
    profile: CandidateProfile,
) -> LabelingSession:
    with store.locked():
        try:
            return store.load_session()
        except LabelingSessionNotFound:
            pass
        if store.load_labels():
            raise ValueError(
                "Existing scoring labels require their original session queue"
            )

        buckets = _eligible_jobs(jobs, profile)
        required = {
            label: sum(quotas[label] for quotas in SESSION_QUOTAS.values())
            for label in ("reject", "review", "strong")
        }
        for label, count in required.items():
            if len(buckets[label]) < count:
                raise ValueError(
                    f"Scoring labeling requires {count} {label} jobs; "
                    f"only {len(buckets[label])} are eligible"
                )

        selected: list[tuple[BenchmarkSplit, FitLabel, dict]] = []
        offsets: dict[FitLabel, int] = {label: 0 for label in required}
        for split in ("calibration", "validation"):
            split_selected: list[tuple[BenchmarkSplit, FitLabel, dict]] = []
            for label in ("reject", "review", "strong"):
                count = SESSION_QUOTAS[split][label]
                start = offsets[label]
                end = start + count
                split_selected.extend(
                    (split, label, job) for job in buckets[label][start:end]
                )
                offsets[label] = end
            selected.extend(sorted(split_selected, key=_stable_entry_order))

        entries = []
        for sequence, (split, predicted, job) in enumerate(selected, start=1):
            posting = JobPosting.model_validate(job)
            entries.append(
                QueueEntry(
                    sequence=sequence,
                    job_id=job["id"],
                    posting_fingerprint=posting_fingerprint(posting),
                    job_url=posting.url,
                    review_url=job.get("application_url") or posting.url,
                    posting=posting,
                    split=split,
                    predicted_label=predicted,
                )
            )
        session = LabelingSession(
            schema_version=QUEUE_SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            entries=entries,
        )
        store.write_session(session)
        return session


def labeling_progress(store: ScoringLabelStore) -> LabelingProgress:
    with store.locked():
        session = store.load_session()
        labels = _validate_session_labels(session, store.load_labels())
    labeled_ids = {label.job_id for label in labels}
    calibration = [entry for entry in session.entries if entry.split == "calibration"]
    validation = [entry for entry in session.entries if entry.split == "validation"]
    calibration_labeled = sum(entry.job_id in labeled_ids for entry in calibration)
    validation_labeled = sum(entry.job_id in labeled_ids for entry in validation)
    labeled = calibration_labeled + validation_labeled
    phase = (
        "complete"
        if labeled == len(session.entries)
        else "calibration"
        if calibration_labeled < len(calibration)
        else "validation"
    )
    return LabelingProgress(
        total=len(session.entries),
        labeled=labeled,
        calibration_total=len(calibration),
        calibration_labeled=calibration_labeled,
        validation_total=len(validation),
        validation_labeled=validation_labeled,
        phase=phase,
        complete=labeled == len(session.entries),
    )


def next_labeling_job(
    store: ScoringLabelStore,
) -> LabelingJob | None:
    with store.locked():
        session = store.load_session()
        labels = _validate_session_labels(session, store.load_labels())
    labeled_ids = {label.job_id for label in labels}
    entry = next(
        (item for item in session.entries if item.job_id not in labeled_ids),
        None,
    )
    if entry is None:
        return None
    same_split = [item for item in session.entries if item.split == entry.split]
    split_position = same_split.index(entry) + 1
    posting_data = entry.posting.model_dump(mode="json")
    blind_job = {
        key: posting_data.get(key)
        for key in BLIND_JOB_FIELDS
        if key in posting_data
    }
    blind_job["id"] = entry.job_id
    blind_job["application_url"] = str(entry.review_url)
    return LabelingJob(
        sequence=entry.sequence,
        split=entry.split,
        split_position=split_position,
        split_total=len(same_split),
        job=blind_job,
    )


def record_scoring_label(
    store: ScoringLabelStore,
    *,
    job_id: int,
    label: FitLabel,
) -> ScoringLabel:
    with store.locked():
        session = store.load_session()
        labels = _validate_session_labels(session, store.load_labels())
        entry = next(
            (item for item in session.entries if item.job_id == job_id),
            None,
        )
        if entry is None:
            raise LabelingSessionError(f"Job {job_id} is not in the labeling queue")
        if any(item.job_id == job_id for item in labels):
            raise DuplicateScoringLabel(f"Job {job_id} already has a label")
        labeled_ids = {item.job_id for item in labels}
        current_entry = next(
            item for item in session.entries if item.job_id not in labeled_ids
        )
        if entry.job_id != current_entry.job_id:
            raise LabelingSessionError(
                "Scoring labels must be recorded in fixed queue order"
            )
        decision = ScoringLabel(
            job_id=job_id,
            posting_fingerprint=entry.posting_fingerprint,
            job_url=entry.job_url,
            label=label,
            split=entry.split,
            labeled_at=datetime.now(UTC),
        )
        store.write_labels([*labels, decision])
        return decision


def session_benchmark(
    store: ScoringLabelStore,
    profile: CandidateProfile,
    *,
    split: BenchmarkSplit,
) -> dict:
    progress = labeling_progress(store)
    if split == "validation" and (
        progress.validation_labeled < progress.validation_total
    ):
        raise LabelingSessionIncomplete(
            "Validation results remain hidden until all held-out jobs are labeled"
        )
    with store.locked():
        session = store.load_session()
        labels = _validate_session_labels(session, store.load_labels())
    jobs = [
        {"id": entry.job_id, **entry.posting.model_dump(mode="json")}
        for entry in session.entries
    ]
    return evaluate_scoring(jobs, labels, profile, split=split)


def label_comparison(
    store: ScoringLabelStore,
    profile: CandidateProfile,
    *,
    job_id: int,
) -> dict:
    with store.locked():
        session = store.load_session()
        labels = _validate_session_labels(session, store.load_labels())
    entry = next(
        (item for item in session.entries if item.job_id == job_id),
        None,
    )
    decision = next((item for item in labels if item.job_id == job_id), None)
    if entry is None or decision is None:
        raise LabelingSessionError("Labeled job comparison is unavailable")
    if entry.split != "calibration":
        raise LabelingSessionError(
            "Held-out validation predictions remain hidden until completion"
        )
    scored = score_job(entry.posting, profile)
    return {
        "actual_label": decision.label,
        "predicted_label": classify_score(scored.score, profile),
        "fit_score": scored.score,
        "confidence": scored.confidence,
        "confidence_band": scored.confidence_band,
        "dimensions": [item.model_dump(mode="json") for item in scored.dimensions],
        "reasons": scored.reasons,
        "red_flags": scored.red_flags,
    }
