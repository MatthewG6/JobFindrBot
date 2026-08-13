import base64
from datetime import UTC, datetime
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from app.models import ApplicationStatus, JobPosting
from app.sensitive_data import (
    ENCRYPTION_ALGORITHM,
    MacOSKeychainStore,
    REDACTED_VALUE,
    SensitiveCategory,
    SensitiveDataError,
    SensitiveReusePolicy,
    SensitiveValueCipher,
    encryption_context,
    redact_sensitive_mapping,
)
from app.storage import CURRENT_SCHEMA_VERSION, JobStorage
from scripts.manage_sensitive_data import verify_recovery


TEST_KEY = bytes(range(32))


def context(field_name: str = "email") -> bytes:
    return encryption_context(
        secret_id="a" * 32,
        scope="application_profile",
        scope_id=None,
        field_name=field_name,
    )


def test_authenticated_encryption_round_trip_and_tamper_detection() -> None:
    cipher = SensitiveValueCipher(TEST_KEY)
    envelope = cipher.encrypt("private@example.com", context=context())

    assert envelope.algorithm == ENCRYPTION_ALGORITHM
    assert "private@example.com" not in envelope.model_dump_json()
    assert cipher.decrypt(envelope, context=context()) == "private@example.com"

    tampered = envelope.model_copy(
        update={"ciphertext": envelope.ciphertext[:-2] + "AA"}
    )
    with pytest.raises(SensitiveDataError, match="authenticated"):
        cipher.decrypt(tampered, context=context())
    with pytest.raises(SensitiveDataError, match="authenticated"):
        cipher.decrypt(envelope, context=context("phone"))


def test_wrong_key_is_rejected_without_disclosing_plaintext() -> None:
    envelope = SensitiveValueCipher(TEST_KEY).encrypt(
        "private@example.com",
        context=context(),
    )

    with pytest.raises(SensitiveDataError, match="does not match") as error:
        SensitiveValueCipher(bytes(reversed(TEST_KEY))).decrypt(
            envelope,
            context=context(),
        )

    assert "private@example.com" not in str(error.value)


class FakeSecurityRunner:
    def __init__(self) -> None:
        self.key: str | None = None
        self.find_error: int | None = None

    def __call__(self, command, **kwargs):
        if "find-generic-password" in command:
            if self.find_error is not None:
                return subprocess.CompletedProcess(
                    command,
                    self.find_error,
                    "",
                    "keychain error",
                )
            if self.key is None:
                return subprocess.CompletedProcess(command, 44, "", "missing")
            return subprocess.CompletedProcess(command, 0, f"{self.key}\n", "")
        if "add-generic-password" in command:
            self.key = command[command.index("-w") + 1]
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected command: {command}")


def test_keychain_initialize_export_and_restore_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = FakeSecurityRunner()
    monkeypatch.setattr("app.sensitive_data.platform.system", lambda: "Darwin")
    store = MacOSKeychainStore(runner=runner)

    cipher = store.initialize()
    recovery_path = tmp_path / "recovery.json"
    exported_key_id = store.export_recovery(recovery_path)
    runner.key = None
    restored_key_id = store.restore_recovery(recovery_path)

    assert exported_key_id == restored_key_id == cipher.key_id
    assert stat.S_IMODE(recovery_path.stat().st_mode) == 0o600
    assert store.load_key() == TEST_KEY or len(store.load_key()) == 32


def test_initialize_does_not_replace_a_malformed_existing_key(
    monkeypatch,
) -> None:
    runner = FakeSecurityRunner()
    runner.key = "not-a-valid-key"
    monkeypatch.setattr("app.sensitive_data.platform.system", lambda: "Darwin")

    with pytest.raises(SensitiveDataError, match="key is invalid"):
        MacOSKeychainStore(runner=runner).initialize()

    assert runner.key == "not-a-valid-key"


def test_initialize_does_not_treat_keychain_access_failure_as_missing(
    monkeypatch,
) -> None:
    runner = FakeSecurityRunner()
    runner.find_error = 128
    monkeypatch.setattr("app.sensitive_data.platform.system", lambda: "Darwin")

    with pytest.raises(SensitiveDataError, match="Unable to read"):
        MacOSKeychainStore(runner=runner).initialize()

    assert runner.key is None


def test_recovery_rejects_world_readable_and_tampered_packages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = FakeSecurityRunner()
    runner.key = base64.urlsafe_b64encode(TEST_KEY).decode("ascii")
    monkeypatch.setattr("app.sensitive_data.platform.system", lambda: "Darwin")
    store = MacOSKeychainStore(runner=runner)
    recovery_path = tmp_path / "recovery.json"
    store.export_recovery(recovery_path)

    recovery_path.chmod(0o644)
    with pytest.raises(SensitiveDataError, match="owner-only"):
        store.load_recovery_key(recovery_path)
    recovery_path.chmod(0o600)
    payload = json.loads(recovery_path.read_text(encoding="utf-8"))
    payload["key_id"] = "0" * 16
    recovery_path.write_text(json.dumps(payload), encoding="utf-8")
    recovery_path.chmod(0o600)
    with pytest.raises(SensitiveDataError, match="checksum"):
        store.load_recovery_key(recovery_path)


def make_storage(tmp_path: Path) -> JobStorage:
    return JobStorage(
        tmp_path / "jobs.json",
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
    )


def save_contact(storage: JobStorage, value: str = "private@example.com") -> dict:
    return storage.save_sensitive_value(
        scope="application_profile",
        field_name="email",
        value=value,
        category=SensitiveCategory.CONTACT,
        reuse_policy=SensitiveReusePolicy.CONFIRM_FIRST,
        approved_by="Matthew",
    )


def test_storage_persists_ciphertext_and_returns_metadata_only(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    saved = save_contact(storage)
    raw_database = (tmp_path / "jobs.json").read_text(encoding="utf-8")

    assert "private@example.com" not in raw_database
    assert "encrypted_value" not in saved
    assert storage.list_sensitive_values() == [saved]
    assert storage.read_sensitive_value(saved["secret_id"]) == (
        "private@example.com"
    )
    assert storage.schema_version() == CURRENT_SCHEMA_VERSION == 7


@pytest.mark.parametrize(
    "category",
    [
        SensitiveCategory.DEMOGRAPHIC,
        SensitiveCategory.DISABILITY,
        SensitiveCategory.VETERAN,
        SensitiveCategory.BACKGROUND,
        SensitiveCategory.SIGNATURE,
        SensitiveCategory.LEGAL_ATTESTATION,
    ],
)
def test_high_sensitivity_categories_require_retention_confirmation(
    tmp_path: Path,
    category: SensitiveCategory,
) -> None:
    storage = make_storage(tmp_path)

    with pytest.raises(ValueError, match="retention confirmation"):
        storage.save_sensitive_value(
            scope="answer_bank",
            field_name="response",
            value="private response",
            category=category,
            reuse_policy=SensitiveReusePolicy.NEVER_REUSE,
            approved_by="Matthew",
        )

    saved = storage.save_sensitive_value(
        scope="answer_bank",
        field_name="response",
        value="private response",
        category=category,
        reuse_policy=SensitiveReusePolicy.NEVER_REUSE,
        approved_by="Matthew",
        retention_confirmed=True,
    )
    assert saved["retention_confirmed"] is True


def test_sensitive_values_cannot_use_automatic_reuse(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)

    with pytest.raises(ValueError, match="not a valid SensitiveReusePolicy"):
        storage.save_sensitive_value(
            scope="application_profile",
            field_name="email",
            value="private@example.com",
            category=SensitiveCategory.CONTACT,
            reuse_policy="automatic",
            approved_by="Matthew",
        )


def test_backup_contains_only_ciphertext_and_delete_can_purge_snapshots(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    saved = save_contact(storage)
    storage.create_backup(now=datetime(2026, 8, 13, tzinfo=UTC))
    backup_path = tmp_path / "backups" / "jobs-daily-2026-08-13.json"

    assert backup_path.is_file()
    assert "private@example.com" not in backup_path.read_text(encoding="utf-8")
    result = storage.delete_sensitive_value(
        saved["secret_id"],
        purge_backups=True,
    )

    assert result == {
        "deleted": True,
        "backups_purged": 1,
        "backup_purge_recommended": False,
    }
    assert not list((tmp_path / "backups").iterdir())
    assert storage.list_sensitive_values() == []


def test_delete_without_purge_reports_backup_recommendation(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    saved = save_contact(storage)

    result = storage.delete_sensitive_value(saved["secret_id"])

    assert result["deleted"] is True
    assert result["backup_purge_recommended"] is True


def test_backup_purge_requires_confirmation_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    with pytest.raises(ValueError, match="explicit confirmation"):
        storage.purge_database_backups()

    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    (backup_directory / "jobs-daily-2026-08-13.json").symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        storage.purge_database_backups(confirm=True)
    assert outside.read_text(encoding="utf-8") == "outside"


def save_application(storage: JobStorage) -> dict:
    job = storage.save_job(
        JobPosting(
            title="Junior Software Engineer",
            company="Example",
            location="Rochester, MN",
            url="https://example.com/job",
            source="test",
        )
    )
    return storage.create_application(job["id"])


def test_sensitive_audit_event_contains_metadata_but_no_raw_value(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    application = save_application(storage)
    storage.add_application_event(
        application["id"],
        message="Application approved",
        status=ApplicationStatus.QUEUED,
        approval_kind="start",
        approved_by="Matthew",
    )
    storage.add_application_event(
        application["id"],
        message="Application started",
        status=ApplicationStatus.IN_PROGRESS,
    )

    updated = storage.add_sensitive_application_event(
        application["id"],
        question="Are you legally authorized to work in the United States?",
        category=SensitiveCategory.WORK_AUTHORIZATION,
        status=ApplicationStatus.WAITING_FOR_INPUT,
    )
    event = updated["events"][-1]
    database_text = (tmp_path / "jobs.json").read_text(encoding="utf-8")

    assert event["sensitive_category"] == "work_authorization"
    assert event["approval_outcome"] == "pending"
    assert event["value_redacted"] == REDACTED_VALUE
    assert "private response" not in database_text


def test_deleting_application_removes_redacted_audit_and_scoped_secrets(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    application = save_application(storage)
    saved = storage.save_sensitive_value(
        scope="application",
        scope_id=str(application["id"]),
        field_name="work_authorization",
        value="private response",
        category=SensitiveCategory.WORK_AUTHORIZATION,
        reuse_policy=SensitiveReusePolicy.CONFIRM_FIRST,
        approved_by="Matthew",
    )

    result = storage.delete_application(application["id"])

    assert result["deleted"] is True
    assert result["sensitive_values_deleted"] == 1
    assert storage.get_application(application["id"]) is None
    with pytest.raises(ValueError, match="does not exist"):
        storage.read_sensitive_value(saved["secret_id"])


def test_recovery_drill_decrypts_snapshot_without_printing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = make_storage(tmp_path)
    save_contact(storage)
    runner = FakeSecurityRunner()
    runner.key = base64.urlsafe_b64encode(TEST_KEY).decode("ascii")
    monkeypatch.setattr("app.sensitive_data.platform.system", lambda: "Darwin")
    recovery_path = tmp_path / "recovery.json"
    MacOSKeychainStore(runner=runner).export_recovery(recovery_path)

    key_id, record_count = verify_recovery(
        recovery_path,
        tmp_path / "jobs.json",
    )

    assert key_id == SensitiveValueCipher(TEST_KEY).key_id
    assert record_count == 1


def test_redaction_recurses_without_mutating_safe_metadata() -> None:
    original = {
        "question": "What is your email?",
        "answer": "private@example.com",
        "nested": {
            "access_token": "abc",
            "client_secret": "def",
            "status": "pending",
        },
    }

    redacted = redact_sensitive_mapping(original)

    assert redacted == {
        "question": "What is your email?",
        "answer": REDACTED_VALUE,
        "nested": {
            "access_token": REDACTED_VALUE,
            "client_secret": REDACTED_VALUE,
            "status": "pending",
        },
    }
    assert original["answer"] == "private@example.com"


def test_sensitive_data_cli_runs_as_a_direct_script() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/manage_sensitive_data.py", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "initialize-key" in result.stdout
    assert "purge-backups" in result.stdout


def test_schema_validation_rejects_plaintext_or_unknown_sensitive_fields(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    save_contact(storage)
    storage.db.close()
    payload = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    record = next(iter(payload["sensitive_values"].values()))
    record["plaintext"] = "private@example.com"
    (tmp_path / "jobs.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Sensitive-value table is invalid") as error:
        JobStorage(tmp_path / "jobs.json")
    assert "private@example.com" not in str(error.value)
