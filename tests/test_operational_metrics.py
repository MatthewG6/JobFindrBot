from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from app.operational_metrics import append_scheduler_metric, scheduler_metric


def test_scheduler_metric_is_structured_and_sanitized() -> None:
    started = datetime(2026, 8, 4, tzinfo=UTC)
    summary = {
        "gmail": {
            "messages_found": 2,
            "messages_processed": 1,
            "jobs_created": 1,
            "errors": [],
        },
        "sources": {
            "remotive": {
                "due": True,
                "status": "ok",
                "summary": {
                    "jobs_created": 3,
                    "jobs_fetched": 4,
                    "errors": [],
                },
            }
        },
        "discord": {
            "status": "ok",
            "eligible_jobs": 5,
            "notifications_delivered": 1,
            "notifications_attention_required": 0,
            "errors": [],
        },
        "errors": [],
    }

    metric = scheduler_metric(summary, started, started + timedelta(seconds=2))

    assert metric["duration_ms"] == 2000
    assert metric["success"] is True
    assert metric["sources"]["remotive"]["jobs_created"] == 3
    assert "summary" not in metric["sources"]["remotive"]


def test_append_scheduler_metric_uses_private_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "metrics" / "runs.jsonl"
    append_scheduler_metric({"success": True}, path)
    append_scheduler_metric({"success": False}, path)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records == [{"success": True}, {"success": False}]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert os.stat(path.with_suffix(".jsonl.lock")).st_mode & 0o777 == 0o600


def test_metrics_reject_symlinks_and_nonstandard_json(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("unchanged\n", encoding="utf-8")
    path = tmp_path / "runs.jsonl"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        append_scheduler_metric({"success": True}, path)
    assert target.read_text(encoding="utf-8") == "unchanged\n"

    with pytest.raises(ValueError):
        append_scheduler_metric({"duration": float("nan")}, tmp_path / "safe.jsonl")


def test_metrics_reject_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks"):
        append_scheduler_metric(
            {"success": True},
            linked_parent / "metrics" / "runs.jsonl",
        )

    assert list(outside.iterdir()) == []


def test_metrics_reject_fifo_destination(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="regular file"):
        append_scheduler_metric({"success": True}, path)
