from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from app.models import (
    ApplicationStatus,
    InvalidApplicationTransition,
    JobPosting,
)
from app.storage import JobStorage


def approve_application_in_process(
    db_path: str,
    application_id: int,
    barrier: Any,
    result_queue: Any,
) -> None:
    storage = JobStorage(Path(db_path))
    barrier.wait()
    try:
        storage.add_application_event(
            application_id,
            message="Application approved",
            status=ApplicationStatus.QUEUED,
            approval_kind="start",
            approved_by="Matthew",
            expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
        )
        result_queue.put("approved")
    except InvalidApplicationTransition:
        result_queue.put("rejected")
    except Exception as error:
        result_queue.put(f"error:{type(error).__name__}:{error}")


def save_test_job(storage: JobStorage) -> dict:
    job = JobPosting(
        title="Junior Software Engineer",
        company="Example Company",
        location="Minneapolis, MN",
        url="https://example.com/job",
        source="test",
    )
    return storage.save_job(job)


def test_create_application_starts_with_approval_status(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)

    application = storage.create_application(saved_job["id"])

    assert application["job_id"] == saved_job["id"]
    assert application["status"] == "awaiting_start_approval"
    assert application["current_step"] == (
        "Application candidate created; waiting for approval to start"
    )
    assert len(application["events"]) == 1
    assert application["events"][0]["status"] == "awaiting_start_approval"


def test_create_application_does_not_duplicate_same_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)

    first_application = storage.create_application(saved_job["id"])
    second_application = storage.create_application(saved_job["id"])

    assert first_application["id"] == second_application["id"]
    assert len(storage.list_applications()) == 1


def test_add_application_event_updates_status_and_history(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)
    application = storage.create_application(saved_job["id"])

    queued_application = storage.add_application_event(
        application["id"],
        message="Application approved",
        status=ApplicationStatus.QUEUED,
        approval_kind="start",
        approved_by="Matthew",
    )
    updated_application = storage.add_application_event(
        application["id"],
        message="Starting application in Workday",
        status=ApplicationStatus.IN_PROGRESS,
        current_step="Signing in",
        provider="workday",
    )

    assert updated_application["status"] == "in_progress"
    assert updated_application["current_step"] == "Signing in"
    assert updated_application["provider"] == "workday"
    assert len(queued_application["events"]) == 2
    assert len(updated_application["events"]) == 3
    assert updated_application["events"][2]["message"] == (
        "Starting application in Workday"
    )
    assert updated_application["events"][2]["current_step"] == "Signing in"
    assert updated_application["events"][2]["provider"] == "workday"


def test_add_application_event_rejects_invalid_status_jump(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")
    saved_job = save_test_job(storage)
    application = storage.create_application(saved_job["id"])

    with pytest.raises(
        ValueError,
        match=(
            "Cannot transition application from awaiting_start_approval "
            "to submitted"
        ),
    ):
        storage.add_application_event(
            application["id"],
            message="Submitted without approval",
            status=ApplicationStatus.SUBMITTED,
        )

    unchanged_application = storage.get_application(application["id"])
    assert unchanged_application is not None
    assert unchanged_application["status"] == "awaiting_start_approval"
    assert len(unchanged_application["events"]) == 1


def test_create_application_requires_existing_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="Job 99 does not exist"):
        storage.create_application(99)


def test_concurrent_approvals_are_serialized(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.json"
    setup_storage = JobStorage(db_path)
    saved_job = save_test_job(setup_storage)
    application = setup_storage.create_application(saved_job["id"])
    storage_a = JobStorage(db_path)
    storage_b = JobStorage(db_path)
    barrier = Barrier(2)

    def approve(storage: JobStorage) -> dict:
        barrier.wait()
        return storage.add_application_event(
            application["id"],
            message="Application approved",
            status=ApplicationStatus.QUEUED,
            approval_kind="start",
            approved_by="Matthew",
            expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(approve, storage_a),
            executor.submit(approve, storage_b),
        ]

    successful_approvals = 0
    rejected_approvals = 0
    for future in futures:
        try:
            future.result()
            successful_approvals += 1
        except InvalidApplicationTransition:
            rejected_approvals += 1

    assert successful_approvals == 1
    assert rejected_approvals == 1
    saved_application = JobStorage(db_path).get_application(application["id"])
    assert saved_application is not None
    assert saved_application["status"] == "queued"
    assert len(saved_application["events"]) == 2


def test_competing_candidate_actions_are_atomic(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.json"
    setup_storage = JobStorage(db_path)
    saved_job = save_test_job(setup_storage)
    application = setup_storage.create_application(saved_job["id"])
    storage_a = JobStorage(db_path)
    storage_b = JobStorage(db_path)
    barrier = Barrier(2)

    def approve() -> dict:
        barrier.wait()
        return storage_a.add_application_event(
            application["id"],
            message="Application approved",
            status=ApplicationStatus.QUEUED,
            approval_kind="start",
            approved_by="Matthew",
            expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
        )

    def reject() -> dict:
        barrier.wait()
        return storage_b.add_application_event(
            application["id"],
            message="Application rejected",
            status=ApplicationStatus.SKIPPED,
            expected_status=ApplicationStatus.AWAITING_START_APPROVAL,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve), executor.submit(reject)]

    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result()["status"])
        except InvalidApplicationTransition:
            outcomes.append("conflict")

    assert outcomes.count("conflict") == 1
    assert set(outcomes) in ({"queued", "conflict"}, {"skipped", "conflict"})
    saved_application = JobStorage(db_path).get_application(application["id"])
    assert saved_application is not None
    assert saved_application["status"] in {"queued", "skipped"}
    assert len(saved_application["events"]) == 2


def test_approvals_are_serialized_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.json"
    setup_storage = JobStorage(db_path)
    saved_job = save_test_job(setup_storage)
    application = setup_storage.create_application(saved_job["id"])
    process_context = multiprocessing.get_context("spawn")
    barrier = process_context.Barrier(2)
    result_queue = process_context.Queue()
    processes = [
        process_context.Process(
            target=approve_application_in_process,
            args=(str(db_path), application["id"], barrier, result_queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    outcomes = sorted(result_queue.get(timeout=2) for _ in processes)
    assert outcomes == ["approved", "rejected"]

    saved_application = JobStorage(db_path).get_application(application["id"])
    assert saved_application is not None
    assert saved_application["status"] == "queued"
    assert len(saved_application["events"]) == 2
