from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from app.discord_notifications import DiscordNotConfigured
from app.source_credentials import SourceNotConfigured
from app.storage import JobStorage
from scripts.run_scheduled_scan import (
    HIMALAYAS_INTERVAL,
    SOURCE_INTERVALS,
    himalayas_scan_due,
    print_summary,
    run_scheduled_scan,
    scheduler_lock,
    source_scan_due,
    write_scheduler_state,
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


def source_summary(errors: list | None = None) -> dict:
    return {
        "jobs_fetched": 3,
        "jobs_created": 2,
        "duplicates_skipped": 1,
        "records_received": 3,
        "records_filtered": 0,
        "records_rejected": 0,
        "errors": errors or [],
    }


def notification_summary(errors: list | None = None) -> dict:
    return {
        "status": "failed" if errors else "ok",
        "threshold": 40,
        "eligible_jobs": 2,
        "baseline_count": 0,
        "notifications_attempted": 2,
        "notifications_delivered": 2 if not errors else 1,
        "notifications_skipped": 0,
        "notifications_attention_required": 0,
        "errors": errors or [],
    }


def resolver_summary(errors: list | None = None) -> dict:
    return {
        "jobs_considered": 2,
        "jobs_resolved": 1,
        "jobs_already_resolved": 3,
        "jobs_pending": 1,
        "jobs_manual_required": 0,
        "provider_jobs_attempted": 0,
        "provider_jobs_resolved": 0,
        "provider_jobs_deferred": 0,
        "provider_jobs_dynamic_required": 0,
        "provider_jobs_ambiguous": 0,
        "provider_jobs_manual_required": 0,
        "provider_jobs_failed": 0,
        "errors": errors or [],
    }


def enrichment_summary(errors: list | None = None) -> dict:
    return {
        "jobs_eligible": 2,
        "jobs_attempted": 2,
        "jobs_enriched": 1,
        "jobs_already_enriched": 3,
        "jobs_deferred": 0,
        "jobs_dynamic_required": 1,
        "jobs_already_dynamic_required": 0,
        "jobs_manual_required": 0,
        "jobs_already_manual_required": 0,
        "jobs_failed": 0,
        "errors": errors or [],
    }


def dynamic_resolution_summary(errors: list | None = None) -> dict:
    return {
        "jobs_eligible": 2,
        "jobs_attempted": 1,
        "jobs_resolved": 1,
        "jobs_deferred": 1,
        "jobs_ambiguous": 0,
        "jobs_manual_required": 0,
        "jobs_failed": 0,
        "errors": errors or [],
    }


def dynamic_enrichment_summary(errors: list | None = None) -> dict:
    return {
        "jobs_eligible": 2,
        "jobs_attempted": 1,
        "jobs_enriched": 1,
        "jobs_deferred": 0,
        "jobs_unapproved": 1,
        "jobs_manual_required": 0,
        "jobs_failed": 0,
        "errors": errors or [],
    }


def test_dynamic_stages_run_in_resolution_and_enrichment_order(
    tmp_path: Path,
) -> None:
    calls = []

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        resolver_runner=lambda storage: calls.append("resolver")
        or resolver_summary(),
        dynamic_resolution_runner=lambda storage: calls.append(
            "dynamic_resolution"
        )
        or dynamic_resolution_summary(),
        enrichment_runner=lambda storage: calls.append("enrichment")
        or enrichment_summary(),
        dynamic_enrichment_runner=lambda storage: calls.append(
            "dynamic_enrichment"
        )
        or dynamic_enrichment_summary(),
    )

    assert calls == [
        "resolver",
        "dynamic_resolution",
        "enrichment",
        "dynamic_enrichment",
    ]
    assert summary["errors"] == []


def test_unbalanced_dynamic_summary_is_rejected(tmp_path: Path) -> None:
    invalid = dynamic_resolution_summary()
    invalid["jobs_resolved"] = 9

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        resolver_runner=None,
        dynamic_resolution_runner=lambda storage: invalid,
        enrichment_runner=None,
        dynamic_enrichment_runner=None,
    )

    assert summary["dynamic_resolution"] == {"status": "failed"}
    assert summary["errors"] == [
        {"source": "dynamic_resolution", "error_type": "ValueError"}
    ]


def test_first_scheduled_run_scans_all_configured_sources(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runners = {
        source: (
            lambda storage, source=source: calls.append(source)
            or source_summary()
        )
        for source in SOURCE_INTERVALS
    }

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: calls.append("gmail") or gmail_summary(),
        source_runners=runners,
    )

    assert calls == ["gmail", *SOURCE_INTERVALS]
    assert summary["errors"] == []
    assert all(result["status"] == "ok" for result in summary["sources"].values())
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    expected_state_keys = {
        f"{source}_{event}_at"
        for source in SOURCE_INTERVALS
        for event in ("last_attempt", "last_success")
    }
    assert set(state) == expected_state_keys
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_sources_follow_their_own_intervals(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    storage = JobStorage(tmp_path / "jobs.json")
    calls: list[str] = []
    runners = {
        source: (
            lambda storage, source=source: calls.append(source)
            or source_summary()
        )
        for source in SOURCE_INTERVALS
    }

    run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners=runners,
    )
    second = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW + timedelta(hours=6),
        gmail_runner=lambda storage: gmail_summary(),
        source_runners=runners,
    )

    assert calls.count("himalayas") == 1
    frequent_sources = set(SOURCE_INTERVALS) - {"himalayas"}
    assert all(calls.count(source) == 2 for source in frequent_sources)
    assert second["sources"]["himalayas"]["status"] == "not_due"
    assert second["sources"]["remotive"]["status"] == "ok"


def test_gmail_runs_on_every_scheduler_wake(tmp_path: Path) -> None:
    calls = 0

    def gmail(storage: JobStorage) -> dict:
        nonlocal calls
        calls += 1
        return gmail_summary()

    state_path = tmp_path / "state.json"
    storage = JobStorage(tmp_path / "jobs.json")
    for minute in (0, 30, 60):
        run_scheduled_scan(
            storage=storage,
            state_path=state_path,
            now=NOW + timedelta(minutes=minute),
            gmail_runner=gmail,
            source_runners={"remotive": lambda storage: source_summary()},
        )

    assert calls == 3


def test_discord_runs_after_job_sources(tmp_path: Path) -> None:
    calls: list[str] = []

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: calls.append("gmail") or gmail_summary(),
        source_runners={
            "remotive": lambda storage: calls.append("remotive")
            or source_summary()
        },
        resolver_runner=lambda storage: calls.append("resolver")
        or resolver_summary(),
        enrichment_runner=lambda storage: calls.append("enrichment")
        or enrichment_summary(),
        notification_runner=lambda storage: calls.append("discord")
        or notification_summary(),
    )

    assert calls == [
        "gmail",
        "remotive",
        "resolver",
        "enrichment",
        "discord",
    ]
    assert summary["discord"]["status"] == "ok"
    assert summary["errors"] == []


def test_resolver_failure_is_isolated_before_discord(tmp_path: Path) -> None:
    calls: list[str] = []

    def fail_resolver(storage: JobStorage) -> dict:
        calls.append("resolver")
        raise RuntimeError("private resolver detail")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        resolver_runner=fail_resolver,
        notification_runner=lambda storage: calls.append("discord")
        or notification_summary(),
    )

    assert calls == ["resolver", "discord"]
    assert summary["resolver"] == {"status": "failed"}
    assert summary["errors"] == [
        {"source": "resolver", "error_type": "RuntimeError"}
    ]


def test_enrichment_failure_is_isolated_before_discord(tmp_path: Path) -> None:
    calls: list[str] = []

    def fail_enrichment(storage: JobStorage) -> dict:
        calls.append("enrichment")
        raise RuntimeError("private enrichment detail")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        resolver_runner=lambda storage: resolver_summary(),
        enrichment_runner=fail_enrichment,
        notification_runner=lambda storage: calls.append("discord")
        or notification_summary(),
    )

    assert calls == ["enrichment", "discord"]
    assert summary["enrichment"] == {"status": "failed"}
    assert summary["errors"] == [
        {"source": "enrichment", "error_type": "RuntimeError"}
    ]


def test_discord_not_configured_does_not_fail_scheduler(tmp_path: Path) -> None:
    def not_configured(storage: JobStorage) -> dict:
        raise DiscordNotConfigured("private webhook detail")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        notification_runner=not_configured,
    )

    assert summary["discord"] == {"status": "not_configured"}
    assert summary["errors"] == []


def test_discord_delivery_errors_fail_health_without_details(
    tmp_path: Path,
) -> None:
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        notification_runner=lambda storage: notification_summary(
            [{"job_id": 999, "error_type": "PrivateDeliveryError"}]
        ),
    )

    assert summary["discord"]["status"] == "failed"
    assert summary["errors"] == [
        {"source": "discord", "error_type": "DiscordDeliveryErrors"}
    ]


def test_malformed_discord_summary_is_isolated(tmp_path: Path) -> None:
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        notification_runner=lambda storage: {},
    )

    assert summary["discord"] == {"status": "failed"}
    assert summary["errors"] == [
        {"source": "discord", "error_type": "ValueError"}
    ]


def test_contradictory_discord_summary_fails_validation(tmp_path: Path) -> None:
    invalid = notification_summary()
    invalid["status"] = "failed"

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={},
        notification_runner=lambda storage: invalid,
    )

    assert summary["discord"] == {"status": "failed"}
    assert summary["errors"] == [
        {"source": "discord", "error_type": "ValueError"}
    ]


def test_source_not_configured_is_not_a_scheduler_error(tmp_path: Path) -> None:
    def not_configured(storage: JobStorage) -> dict:
        raise SourceNotConfigured("contains private configuration detail")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"adzuna": not_configured},
    )

    assert summary["sources"]["adzuna"]["status"] == "not_configured"
    assert summary["errors"] == []
    assert json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    ) == {}


def test_source_failures_are_isolated_and_sanitized(tmp_path: Path) -> None:
    def fail(storage: JobStorage) -> dict:
        raise RuntimeError("private diagnostic")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={
            "remotive": fail,
            "himalayas": lambda storage: source_summary(),
        },
    )

    assert summary["sources"]["remotive"]["status"] == "failed"
    assert summary["sources"]["himalayas"]["status"] == "ok"
    assert summary["errors"] == [
        {"source": "remotive", "error_type": "RuntimeError"}
    ]


def test_partial_source_errors_respect_attempt_interval(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    calls = 0

    def partial_failure(storage: JobStorage) -> dict:
        nonlocal calls
        calls += 1
        return source_summary(
            [{"provider": "lever", "error_type": "Timeout"}]
        )

    first = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"employer_watchlist": partial_failure},
    )
    second = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW + timedelta(minutes=30),
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"employer_watchlist": partial_failure},
    )

    assert first["sources"]["employer_watchlist"]["status"] == "failed"
    assert first["errors"] == [
        {"source": "employer_watchlist", "error_type": "SourceItemErrors"}
    ]
    assert second["sources"]["employer_watchlist"]["status"] == "not_due"
    assert calls == 1
    assert "employer_watchlist_last_attempt_at" in state_path.read_text(
        encoding="utf-8"
    )


def test_malformed_source_summary_respects_attempt_interval(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    first = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"himalayas": lambda storage: {}},
    )

    second = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW + timedelta(minutes=30),
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"himalayas": lambda storage: {}},
    )

    assert first["sources"]["himalayas"]["status"] == "failed"
    assert first["errors"] == [
        {"source": "himalayas", "error_type": "ValueError"}
    ]
    assert second["sources"]["himalayas"]["status"] == "not_due"
    assert "himalayas_last_attempt_at" in state_path.read_text(encoding="utf-8")


def test_attempt_is_durable_before_runner_can_terminate(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    storage = JobStorage(tmp_path / "jobs.json")
    calls = 0

    def terminate(storage: JobStorage) -> dict:
        nonlocal calls
        calls += 1
        raise SystemExit(9)

    try:
        run_scheduled_scan(
            storage=storage,
            state_path=state_path,
            now=NOW,
            gmail_runner=lambda storage: gmail_summary(),
            source_runners={"remotive": terminate},
        )
    except SystemExit:
        pass

    second = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW + timedelta(minutes=30),
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"remotive": terminate},
    )

    assert calls == 1
    assert second["sources"]["remotive"]["status"] == "not_due"


def test_attempt_write_failure_blocks_provider_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def source(storage: JobStorage) -> dict:
        nonlocal calls
        calls += 1
        return source_summary()

    monkeypatch.setattr(
        "scripts.run_scheduled_scan.write_scheduler_state",
        lambda *args: (_ for _ in ()).throw(OSError("private")),
    )

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"remotive": source},
    )

    assert calls == 0
    assert summary["sources"]["remotive"]["status"] == "failed"
    assert summary["errors"] == [
        {"source": "scheduler_state", "error_type": "OSError"}
    ]


def test_failed_success_state_write_keeps_durable_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    storage = JobStorage(tmp_path / "jobs.json")
    writes = 0
    calls = 0

    def flaky_write(path: Path, state: dict) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("private")
        write_scheduler_state(path, state)

    def source(storage: JobStorage) -> dict:
        nonlocal calls
        calls += 1
        return source_summary()

    monkeypatch.setattr(
        "scripts.run_scheduled_scan.write_scheduler_state",
        flaky_write,
    )

    first = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"remotive": source},
    )
    second = run_scheduled_scan(
        storage=storage,
        state_path=state_path,
        now=NOW + timedelta(minutes=30),
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"remotive": source},
    )

    assert first["errors"] == [
        {"source": "scheduler_state", "error_type": "OSError"}
    ]
    assert second["sources"]["remotive"]["status"] == "not_due"
    assert calls == 1


def test_unbalanced_source_summary_is_rejected(tmp_path: Path) -> None:
    invalid = source_summary()
    invalid["jobs_fetched"] = 99

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"remotive": lambda storage: invalid},
    )

    assert summary["sources"]["remotive"]["status"] == "failed"
    assert summary["errors"][0]["error_type"] == "ValueError"


def test_gmail_message_errors_fail_the_scheduled_run(tmp_path: Path) -> None:
    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=tmp_path / "state.json",
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(
            [{"message_id": "private", "error_type": "ParseError"}]
        ),
        source_runners={},
    )

    assert summary["errors"] == [
        {"source": "gmail", "error_type": "GmailMessageErrors"}
    ]


def test_corrupt_state_is_reported_and_repaired(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={"himalayas": lambda storage: source_summary()},
    )

    assert summary["errors"] == [
        {"source": "scheduler_state", "error_type": "ValueError"}
    ]
    assert "himalayas_last_success_at" in state_path.read_text(encoding="utf-8")


def test_corrupt_state_is_repaired_even_when_source_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")

    summary = run_scheduled_scan(
        storage=JobStorage(tmp_path / "jobs.json"),
        state_path=state_path,
        now=NOW,
        gmail_runner=lambda storage: gmail_summary(),
        source_runners={
            "remotive": lambda storage: (_ for _ in ()).throw(
                RuntimeError("private")
            )
        },
    )

    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    assert repaired == {"remotive_last_attempt_at": NOW.isoformat()}
    assert summary["errors"] == [
        {"source": "scheduler_state", "error_type": "ValueError"},
        {"source": "remotive", "error_type": "RuntimeError"},
    ]


def test_future_and_extreme_timestamps_are_treated_as_corrupt() -> None:
    future = {"remotive_last_success_at": (NOW + timedelta(seconds=1)).isoformat()}
    maximum = {"remotive_last_success_at": "9999-12-31T23:59:59+00:00"}
    upper_offset = {"remotive_last_success_at": "9999-12-31T23:59:59-23:59"}
    lower_offset = {"remotive_last_success_at": "0001-01-01T00:00:00+23:59"}

    for state in (future, maximum, upper_offset, lower_offset):
        assert source_scan_due(state, "remotive", NOW, timedelta(hours=6)) is True
    assert himalayas_scan_due({}, NOW, HIMALAYAS_INTERVAL) is True


def test_print_summary_does_not_print_private_error_details(capsys) -> None:
    summary = {
        "gmail": gmail_summary(
            [{"message_id": "private-id", "error_type": "PrivateError"}]
        ),
        "sources": {
            "employer_watchlist": {
                "due": True,
                "status": "failed",
                "summary": source_summary(
                    [{"site": "private-site", "error_type": "SecretError"}]
                ),
            }
        },
        "discord": notification_summary(
            [{"job_id": 777, "error_type": "PrivateDiscordError"}]
        ),
        "errors": [
            {"source": "gmail", "error_type": "GmailMessageErrors"},
            {
                "source": "employer_watchlist",
                "error_type": "SourceItemErrors",
            },
            {"source": "discord", "error_type": "DiscordDeliveryErrors"},
        ],
    }

    print_summary(summary)
    output = capsys.readouterr().out

    assert "private-id" not in output
    assert "PrivateError" not in output
    assert "private-site" not in output
    assert "SecretError" not in output
    assert "777" not in output
    assert "PrivateDiscordError" not in output
    assert "GmailMessageErrors" in output


def test_scheduler_lock_prevents_overlapping_runs(tmp_path: Path) -> None:
    lock_path = tmp_path / "scan.lock"

    with scheduler_lock(lock_path) as first_acquired:
        with scheduler_lock(lock_path) as second_acquired:
            assert first_acquired is True
            assert second_acquired is False
