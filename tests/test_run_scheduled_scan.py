from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.storage import JobStorage
from scripts.run_scheduled_scan import (
    HIMALAYAS_INTERVAL,
    himalayas_scan_due,
    print_summary,
    run_scheduled_scan,
    scheduler_lock,
)


NOW = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def gmail_summary(errors: list | None = None) -> dict:
    return {
        "messages_found": 2,
        "messages_fetched": 2,
        "messages_processed": 2,
        "messages_skipped": 0,
        "jobs_parsed": 5,
        "jobs_created": 4,
        "errors": errors or [],
    }


def himalayas_summary() -> dict:
    return {
        "jobs_fetched": 3,
        "jobs_created": 2,
        "duplicates_skipped": 1,
        "jobs": [],
    }


def test_first_scheduled_run_scans_both_sources(tmp_path: Path) -> None:
    calls: list[str] = []

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: calls.append("gmail") or gmail_summary(),
        himalayas_runner=lambda storage: (
            calls.append("himalayas") or himalayas_summary()
        ),
    )

    assert calls == ["gmail", "himalayas"]
    assert summary["errors"] == []
    assert summary["himalayas_due"] is True
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_himalayas_runs_at_most_once_per_day(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    storage = JobStorage(tmp_path / "jobs.json")
    himalayas_calls = 0

    def run_himalayas(storage: JobStorage) -> dict:
        nonlocal himalayas_calls
        himalayas_calls += 1
        return himalayas_summary()

    first = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        himalayas_runner=run_himalayas,
    )
    second = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW + timedelta(minutes=30),
        gmail_runner=lambda storage: gmail_summary(),
        himalayas_runner=run_himalayas,
    )
    third = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW + HIMALAYAS_INTERVAL,
        gmail_runner=lambda storage: gmail_summary(),
        himalayas_runner=run_himalayas,
    )

    assert first["himalayas_due"] is True
    assert second["himalayas_due"] is False
    assert third["himalayas_due"] is True
    assert himalayas_calls == 2


def test_far_future_himalayas_timestamp_is_treated_as_corrupt() -> None:
    state = {
        "himalayas_last_success_at": (NOW + timedelta(seconds=1)).isoformat()
    }

    assert himalayas_scan_due(state, NOW) is True


def test_maximum_himalayas_timestamp_does_not_overflow() -> None:
    state = {"himalayas_last_success_at": "9999-12-31T23:59:59+00:00"}

    assert himalayas_scan_due(state, NOW) is True


def test_extreme_timestamp_offsets_do_not_overflow() -> None:
    upper = {"himalayas_last_success_at": "9999-12-31T23:59:59-23:59"}
    lower = {"himalayas_last_success_at": "0001-01-01T00:00:00+23:59"}

    assert himalayas_scan_due(upper, NOW) is True
    assert himalayas_scan_due(lower, NOW) is True


def test_source_failures_are_isolated_and_sanitized(tmp_path: Path) -> None:
    def fail_gmail(storage: JobStorage) -> dict:
        raise RuntimeError("private diagnostic")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=fail_gmail,
        himalayas_runner=lambda storage: himalayas_summary(),
    )

    assert summary["himalayas"] == himalayas_summary()
    assert summary["errors"] == [
        {"source": "gmail", "error_type": "RuntimeError"}
    ]


def test_gmail_message_errors_fail_the_scheduled_run(tmp_path: Path) -> None:
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(
            [{"message_id": "private", "error_type": "ParseError"}]
        ),
        himalayas_runner=lambda storage: himalayas_summary(),
    )

    assert summary["errors"] == [
        {"source": "gmail", "error_type": "GmailMessageErrors"}
    ]


def test_malformed_gmail_summary_is_isolated(tmp_path: Path) -> None:
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: {},
        himalayas_runner=lambda storage: himalayas_summary(),
    )

    assert summary["gmail"] is None
    assert summary["himalayas"] == himalayas_summary()
    assert summary["errors"] == [
        {"source": "gmail", "error_type": "ValueError"}
    ]


def test_malformed_himalayas_summary_does_not_throttle_retry(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        himalayas_runner=lambda storage: {},
    )

    assert summary["himalayas"] is None
    assert summary["errors"] == [
        {"source": "himalayas", "error_type": "ValueError"}
    ]
    assert not state_path.exists()


def test_corrupt_state_is_reported_and_repaired(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        himalayas_runner=lambda storage: himalayas_summary(),
    )

    assert summary["errors"] == [
        {"source": "scheduler_state", "error_type": "ValueError"}
    ]
    assert himalayas_scan_due({}, NOW) is True
    assert "himalayas_last_success_at" in state_path.read_text(encoding="utf-8")


def test_print_summary_does_not_print_message_error_details(capsys) -> None:
    summary = {
        "gmail": gmail_summary(
            [{"message_id": "private-id", "error_type": "PrivateError"}]
        ),
        "himalayas": None,
        "himalayas_due": False,
        "errors": [
            {"source": "gmail", "error_type": "GmailMessageErrors"}
        ],
    }

    print_summary(summary)
    output = capsys.readouterr().out

    assert "private-id" not in output
    assert "PrivateError" not in output
    assert "GmailMessageErrors" in output


def test_scheduler_lock_prevents_overlapping_runs(tmp_path: Path) -> None:
    lock_path = tmp_path / "scan.lock"

    with scheduler_lock(lock_path) as first_acquired:
        with scheduler_lock(lock_path) as second_acquired:
            assert first_acquired is True
            assert second_acquired is False
