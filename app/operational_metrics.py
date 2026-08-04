from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import stat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_PATH = (
    PROJECT_ROOT / "data" / "metrics" / "scheduler_runs.jsonl"
)
MAX_METRICS_BYTES = 5_000_000


def _reject_symlinked_path_components(path: Path) -> None:
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    for component in (absolute_path, *absolute_path.parents):
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError("Metrics path must not contain symlinks")


def _require_regular_if_exists(path: Path, label: str) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"{label} must be a regular file")


def scheduler_metric(
    summary: dict,
    started_at: datetime,
    completed_at: datetime,
) -> dict:
    gmail = summary.get("gmail")
    discord = summary.get("discord")
    resolver = summary.get("resolver")
    enrichment = summary.get("enrichment")
    sources = {}
    for name, result in summary.get("sources", {}).items():
        source_summary = result.get("summary") or {}
        sources[name] = {
            "due": bool(result.get("due")),
            "status": result.get("status"),
            "jobs_created": source_summary.get("jobs_created", 0),
            "jobs_fetched": source_summary.get("jobs_fetched", 0),
            "error_count": len(source_summary.get("errors", [])),
        }
    return {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": max(
            0,
            round((completed_at - started_at).total_seconds() * 1000),
        ),
        "success": not summary.get("errors"),
        "maintenance": summary.get("maintenance"),
        "gmail": (
            None
            if gmail is None
            else {
                "messages_found": gmail.get("messages_found", 0),
                "messages_processed": gmail.get("messages_processed", 0),
                "jobs_created": gmail.get("jobs_created", 0),
                "error_count": len(gmail.get("errors", [])),
            }
        ),
        "sources": sources,
        "resolver": (
            None
            if resolver is None or resolver.get("status") == "failed"
            else {
                "jobs_considered": resolver.get("jobs_considered", 0),
                "jobs_resolved": resolver.get("jobs_resolved", 0),
                "jobs_pending": resolver.get("jobs_pending", 0),
                "jobs_manual_required": resolver.get(
                    "jobs_manual_required",
                    0,
                ),
                "error_count": len(resolver.get("errors", [])),
            }
        ),
        "enrichment": (
            None
            if enrichment is None or enrichment.get("status") == "failed"
            else {
                "jobs_eligible": enrichment.get("jobs_eligible", 0),
                "jobs_attempted": enrichment.get("jobs_attempted", 0),
                "jobs_enriched": enrichment.get("jobs_enriched", 0),
                "jobs_already_enriched": enrichment.get(
                    "jobs_already_enriched",
                    0,
                ),
                "jobs_deferred": enrichment.get("jobs_deferred", 0),
                "jobs_dynamic_required": enrichment.get(
                    "jobs_dynamic_required",
                    0,
                ),
                "jobs_already_dynamic_required": enrichment.get(
                    "jobs_already_dynamic_required",
                    0,
                ),
                "jobs_manual_required": enrichment.get(
                    "jobs_manual_required",
                    0,
                ),
                "jobs_already_manual_required": enrichment.get(
                    "jobs_already_manual_required",
                    0,
                ),
                "jobs_failed": enrichment.get("jobs_failed", 0),
                "error_count": len(enrichment.get("errors", [])),
            }
        ),
        "discord": (
            None
            if discord is None
            else {
                "status": discord.get("status"),
                "eligible_jobs": discord.get("eligible_jobs", 0),
                "delivered": discord.get("notifications_delivered", 0),
                "attention_required": discord.get(
                    "notifications_attention_required",
                    0,
                ),
                "error_count": len(discord.get("errors", [])),
            }
        ),
        "errors": [
            {
                "source": error.get("source", "unknown"),
                "error_type": error.get("error_type", "UnknownError"),
            }
            for error in summary.get("errors", [])
        ],
    }


def append_scheduler_metric(metric: dict, path: Path = DEFAULT_METRICS_PATH) -> None:
    serialized = json.dumps(metric, allow_nan=False, sort_keys=True)
    _reject_symlinked_path_components(path.parent)
    if path.parent.is_symlink():
        raise ValueError("Metrics directory must be a regular directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(path.parent.lstat().st_mode):
        raise ValueError("Metrics directory must be a regular directory")
    os.chmod(path.parent, 0o700)
    _require_regular_if_exists(path, "Metrics path")
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    _require_regular_if_exists(lock_path, "Metrics lock")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | no_follow,
        0o600,
    )
    with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _reject_symlinked_path_components(path.parent)
            _require_regular_if_exists(path, "Metrics path")
            if path.exists() and path.stat().st_size >= MAX_METRICS_BYTES:
                rotated = path.with_suffix(f"{path.suffix}.1")
                if rotated.is_symlink():
                    raise ValueError("Rotated metrics path must be regular")
                rotated.unlink(missing_ok=True)
                path.replace(rotated)
                os.chmod(rotated, 0o600)
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | no_follow,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as metrics_file:
                os.chmod(path, 0o600)
                metrics_file.write(serialized)
                metrics_file.write("\n")
                metrics_file.flush()
                os.fsync(metrics_file.fileno())
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
