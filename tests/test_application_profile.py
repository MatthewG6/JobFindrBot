from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
import yaml

from app.application_profile import (
    APPLICATION_PROFILE_SCHEMA_VERSION,
    MAX_APPLICATION_DOCUMENT_BYTES,
    ApplicationProfile,
    ApprovedDocument,
    ApprovedDocumentMetadata,
    approve_document,
    configured_application_profile_path,
    document_sha256,
    load_application_profile,
    parse_application_profile,
    validate_application_profile,
    validate_document_file,
    write_application_profile,
)
from app.sensitive_data import (
    SensitiveCategory,
    SensitiveDataError,
    SensitiveReusePolicy,
    SensitiveValueCipher,
)
from app.storage import JobStorage
from scripts import manage_application_profile


TEST_KEY = bytes(range(32))
NOW = datetime(2026, 8, 21, tzinfo=UTC)


def make_storage(tmp_path: Path) -> JobStorage:
    return JobStorage(
        tmp_path / "jobs.json",
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
    )


def save_field(storage: JobStorage, field_name: str, profile_id: str = "primary") -> dict:
    return storage.save_sensitive_value(
        scope="application_profile",
        scope_id=profile_id,
        field_name=field_name,
        value=f"private-{field_name}",
        category=SensitiveCategory.CONTACT,
        reuse_policy=SensitiveReusePolicy.CONFIRM_FIRST,
        approved_by="Owner",
    )


def write_document(root: Path, name: str = "resume.pdf") -> Path:
    path = root / "documents" / name
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(b"approved resume content")
    return path


def approved_document(
    profile_path: Path,
    path: Path,
    *,
    document_id: str = "software_resume",
    default: bool = True,
) -> ApprovedDocument:
    return approve_document(
        "primary",
        ApprovedDocumentMetadata(
            document_id=document_id,
            kind="resume",
            label="Software engineering resume",
            path=path.relative_to(profile_path.parent).as_posix(),
            media_type="application/pdf",
            revision="2026-08-21",
            sha256=document_sha256(path),
            approved_for=["software_engineering"],
            approved_by="Owner",
            approved_at=NOW,
            active=True,
            default=default,
        ),
        SensitiveValueCipher(TEST_KEY),
    )


def complete_profile(
    tmp_path: Path,
) -> tuple[Path, ApplicationProfile, JobStorage]:
    profile_path = tmp_path / "private" / "application_profile.yaml"
    profile_path.parent.mkdir(mode=0o700)
    storage = make_storage(tmp_path)
    for name in ("legal_name", "email", "phone"):
        save_field(storage, name)
    document_path = write_document(profile_path.parent)
    profile = ApplicationProfile(
        schema_version=APPLICATION_PROFILE_SCHEMA_VERSION,
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
        documents=[approved_document(profile_path, document_path)],
        created_at=NOW,
        updated_at=NOW,
    )
    write_application_profile(profile_path, profile)
    return profile_path, profile, storage


def test_complete_profile_validates_without_persisting_plaintext(
    tmp_path: Path,
) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)

    loaded = load_application_profile(
        profile_path,
        storage=storage,
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        require_ready=True,
    )
    summary = validate_application_profile(
        parse_application_profile(profile_path),
        profile_path,
        storage=storage,
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        require_ready=True,
    )
    serialized_profile = profile_path.read_text(encoding="utf-8")
    serialized_database = (tmp_path / "jobs.json").read_text(encoding="utf-8")

    assert summary == {
        "schema_version": 1,
        "profile_id": "primary",
        "field_count": 3,
        "document_count": 1,
        "active_document_count": 1,
        "ready": True,
        "readiness_issues": [],
    }
    assert loaded == profile
    assert "private-legal_name" not in serialized_profile
    assert "private-legal_name" not in serialized_database
    secret_id = storage.list_sensitive_values()[0]["secret_id"]
    assert secret_id not in serialized_profile
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert profile_path.parent.stat().st_mode & 0o777 == 0o700
    assert (profile_path.parent / "documents/resume.pdf").stat().st_mode & 0o777 == 0o600


def test_profile_rejects_plaintext_and_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "profile_id": "primary",
                "profile_name": "Primary",
                "approval_name": "Owner",
                "fields": [
                    {
                        "field_name": "email",
                        "secret_id": "0" * 32,
                        "value": "plaintext@example.com",
                    }
                ],
                "documents": [],
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        parse_application_profile(path)


def test_profile_fields_are_isolated_by_profile_scope(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    write_application_profile(
        profile_path,
        ApplicationProfile(
            schema_version=1,
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    storage = make_storage(tmp_path)
    for name in ("legal_name", "email", "phone"):
        save_field(storage, name, profile_id="secondary")

    summary = validate_application_profile(
        parse_application_profile(profile_path),
        profile_path,
        storage=storage,
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
    )

    assert summary["field_count"] == 0
    assert summary["ready"] is False


def test_profile_requires_core_fields_and_active_resume_when_ready(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    profile = ApplicationProfile(
        schema_version=1,
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
        created_at=NOW,
        updated_at=NOW,
    )
    write_application_profile(profile_path, profile)

    summary = validate_application_profile(profile, profile_path)

    assert summary["ready"] is False
    assert summary["readiness_issues"] == [
        "missing required encrypted field: email",
        "missing required encrypted field: legal_name",
        "missing required encrypted field: phone",
        "encrypted application fields were not verified",
        "missing an active approved resume",
    ]
    with pytest.raises(ValueError, match="not ready"):
        validate_application_profile(
            profile,
            profile_path,
            storage=JobStorage(tmp_path / "jobs.json"),
            require_ready=True,
        )


def test_document_hash_change_and_symlink_fail_closed(tmp_path: Path) -> None:
    profile_path, profile, _ = complete_profile(tmp_path)
    document = profile.documents[0]
    path = profile_path.parent / document.path
    path.write_bytes(b"changed after approval")

    with pytest.raises(ValueError, match="fingerprint changed"):
        validate_document_file(profile_path, document)

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"approved resume content")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        validate_document_file(profile_path, document)


def test_document_path_and_default_selection_are_constrained(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="relative"):
        ApprovedDocumentMetadata(
            document_id="resume",
            kind="resume",
            label="Resume",
            path="../resume.pdf",
            media_type="application/pdf",
            revision="1",
            sha256="0" * 64,
            approved_for=["general"],
            approved_by="Owner",
            approved_at=NOW,
        )

    profile_path = tmp_path / "profile.yaml"
    first_path = write_document(tmp_path, "first.pdf")
    second_path = write_document(tmp_path, "second.pdf")
    first = approved_document(profile_path, first_path, document_id="first")
    second = approved_document(profile_path, second_path, document_id="second")
    with pytest.raises(ValueError, match="Only one active default"):
        ApplicationProfile(
            schema_version=1,
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
            documents=[first, second],
            created_at=NOW,
            updated_at=NOW,
        )


def test_config_selects_application_profile_relative_to_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "application_profile: private/application_profile.yaml\n",
        encoding="utf-8",
    )

    assert configured_application_profile_path(config) == (
        tmp_path / "private" / "application_profile.yaml"
    )


def test_cli_profile_lifecycle_uses_redacted_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    database_path = tmp_path / "jobs.json"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"resume bytes")

    assert manage_application_profile.main(
        [
            "--profile",
            str(profile_path),
            "--database",
            str(database_path),
            "initialize",
            "--approval-name",
            "Owner",
        ]
    ) == 0
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    monkeypatch.setattr(
        manage_application_profile.getpass,
        "getpass",
        lambda prompt: "private@example.com",
    )
    assert manage_application_profile.main(
        [
            "--profile",
            str(profile_path),
            "--database",
            str(database_path),
            "set-field",
            "email",
            "--category",
            "contact",
        ]
    ) == 0
    assert manage_application_profile.main(
        [
            "--profile",
            str(profile_path),
            "--database",
            str(database_path),
            "add-document",
            str(source),
            "--document-id",
            "general_resume",
            "--kind",
            "resume",
            "--label",
            "General resume",
            "--revision",
            "1",
            "--approved-for",
            "general",
            "--default",
        ]
    ) == 0
    assert manage_application_profile.main(
        [
            "--profile",
            str(profile_path),
            "--database",
            str(database_path),
            "list",
        ]
    ) == 0

    output = capsys.readouterr().out
    profile_text = profile_path.read_text(encoding="utf-8")
    database_text = database_path.read_text(encoding="utf-8")

    assert "private@example.com" not in output
    assert "private@example.com" not in profile_text
    assert "private@example.com" not in database_text
    assert "email" in output
    assert "general_resume" in output

    assert manage_application_profile.main(
        [
            "--profile",
            str(profile_path),
            "--database",
            str(database_path),
            "retire-document",
            "general_resume",
        ]
    ) == 0
    retired = parse_application_profile(profile_path).documents[0]
    assert retired.active is False
    assert retired.default is False


def test_cli_rejects_document_id_before_constructing_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    manage_application_profile.initialize_profile(profile_path, args)
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    malicious = Namespace(
        source=source,
        document_id="../../outside",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=False,
    )

    with pytest.raises(ValueError, match="Document ID"):
        manage_application_profile.add_profile_document(
            profile_path,
            malicious,
            tmp_path / "jobs.json",
        )
    assert not (tmp_path / "outside.pdf").exists()


def test_application_profile_cli_runs_as_direct_script() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/manage_application_profile.py", "--help"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "private application profile" in result.stdout


def test_ready_validation_requires_storage_and_decryptable_references(
    tmp_path: Path,
) -> None:
    profile_path, profile, _ = complete_profile(tmp_path)

    with pytest.raises(ValueError, match="requires encrypted-value storage"):
        validate_application_profile(
            profile,
            profile_path,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
            require_ready=True,
        )

    storage_without_key = JobStorage(tmp_path / "jobs.json")
    with pytest.raises(RuntimeError, match="encryption is not configured"):
        validate_application_profile(
            profile,
            profile_path,
            storage=storage_without_key,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
            require_ready=True,
        )


def test_document_approval_signature_detects_hash_substitution(
    tmp_path: Path,
) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)
    original = profile.documents[0]
    path = profile_path.parent / original.path
    path.write_bytes(b"substituted resume")
    tampered = ApprovedDocument.model_validate(
        {
            **original.model_dump(mode="json"),
            "sha256": document_sha256(path),
        }
    )
    tampered_profile = ApplicationProfile.model_validate(
        {
            **profile.model_dump(mode="json"),
            "documents": [tampered.model_dump(mode="json")],
        }
    )

    with pytest.raises(ValueError, match="metadata is not authentic"):
        validate_application_profile(
            tampered_profile,
            profile_path,
            storage=storage,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        )


def test_retirement_refuses_unauthentic_document_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path, profile, _ = complete_profile(tmp_path)
    document = profile.documents[0]
    tampered = ApprovedDocument.model_validate(
        {
            **document.model_dump(mode="json"),
            "label": "Substituted label",
        }
    )
    write_application_profile(
        profile_path,
        ApplicationProfile.model_validate(
            {
                **profile.model_dump(mode="json"),
                "documents": [tampered.model_dump(mode="json")],
            }
        ),
    )
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )

    with pytest.raises(ValueError, match="metadata is not authentic"):
        manage_application_profile.retire_profile_document(
            profile_path,
            document.document_id,
            tmp_path / "jobs.json",
        )


def test_cli_never_echoes_plaintext_from_malformed_profile(
    tmp_path: Path,
    capsys,
) -> None:
    profile_directory = tmp_path / "private"
    profile_directory.mkdir(mode=0o700)
    profile_path = profile_directory / "profile.yaml"
    profile_path.write_text(
        """
schema_version: 1
profile_id: primary
profile_name: Primary
approval_name: Owner
fields:
  - field_name: email
    secret_id: 00000000000000000000000000000000
    value: CANARY_PRIVATE_VALUE
documents: []
created_at: 2026-08-21T00:00:00Z
updated_at: 2026-08-21T00:00:00Z
""".strip(),
        encoding="utf-8",
    )
    profile_path.chmod(0o600)

    result = manage_application_profile.main(
        ["--profile", str(profile_path), "validate"]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "CANARY_PRIVATE_VALUE" not in captured.err
    assert "Application profile is invalid" in captured.err


def test_initialize_rejects_shared_directory_without_changing_mode(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )

    with pytest.raises(ValueError, match="must be owner-only"):
        manage_application_profile.initialize_profile(
            shared / "profile.yaml",
            args,
        )
    assert shared.stat().st_mode & 0o777 == 0o755


def test_invalid_document_metadata_leaves_no_private_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    manage_application_profile.initialize_profile(profile_path, args)
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    invalid = Namespace(
        source=source,
        document_id="resume",
        kind="resume",
        label=" ",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=False,
    )

    with pytest.raises(ValueError):
        manage_application_profile.add_profile_document(
            profile_path,
            invalid,
            tmp_path / "jobs.json",
        )
    assert not (profile_path.parent / "documents/resume.pdf").exists()


def test_document_source_rejects_symlinked_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    manage_application_profile.initialize_profile(profile_path, args)
    real = tmp_path / "real"
    real.mkdir()
    (real / "resume.pdf").write_bytes(b"resume")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    document_args = Namespace(
        source=linked / "resume.pdf",
        document_id="resume",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=False,
    )

    with pytest.raises(ValueError, match="must not contain symlinks"):
        manage_application_profile.add_profile_document(
            profile_path,
            document_args,
            tmp_path / "jobs.json",
        )


def test_duplicate_document_add_preserves_existing_approved_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    manage_application_profile.initialize_profile(profile_path, args)
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"approved resume")
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    document_args = Namespace(
        source=source,
        document_id="resume",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=True,
    )
    manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )
    destination = profile_path.parent / "documents/resume.pdf"
    original = destination.read_bytes()

    with pytest.raises(ValueError, match="already exists"):
        manage_application_profile.add_profile_document(
            profile_path,
            document_args,
            tmp_path / "jobs.json",
        )

    assert destination.read_bytes() == original
    assert (
        parse_application_profile(profile_path).documents[0].document_id
        == "resume"
    )


def test_document_add_preserves_preexisting_untracked_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"new resume")
    destination = profile_path.parent / "documents/resume.pdf"
    destination.parent.mkdir(mode=0o700)
    destination.write_bytes(b"preexisting private file")
    destination.chmod(0o600)
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )

    with pytest.raises(ValueError, match="already exists"):
        manage_application_profile.add_profile_document(
            profile_path,
            Namespace(
                source=source,
                document_id="resume",
                kind="resume",
                label="Resume",
                revision="1",
                approved_for=["general"],
                approved_by=None,
                default=True,
            ),
            tmp_path / "jobs.json",
        )

    assert destination.read_bytes() == b"preexisting private file"
    assert not manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()


def test_concurrent_document_additions_preserve_both_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    initialize_args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    manage_application_profile.initialize_profile(profile_path, initialize_args)
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )

    def add(document_id: str) -> dict:
        source = tmp_path / f"{document_id}.pdf"
        source.write_bytes(document_id.encode("ascii"))
        return manage_application_profile.add_profile_document(
            profile_path,
            Namespace(
                source=source,
                document_id=document_id,
                kind="resume",
                label=document_id,
                revision="1",
                approved_for=[document_id],
                approved_by=None,
                default=False,
            ),
            tmp_path / "jobs.json",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(add, ["resume_one", "resume_two"]))

    profile = parse_application_profile(profile_path)
    assert {result["document_id"] for result in results} == {
        "resume_one",
        "resume_two",
    }
    assert {document.document_id for document in profile.documents} == {
        "resume_one",
        "resume_two",
    }


def test_interrupted_document_add_rolls_back_published_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    monkeypatch.setattr(
        manage_application_profile,
        "write_application_profile",
        lambda path, profile: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        manage_application_profile.add_profile_document(
            profile_path,
            Namespace(
                source=source,
                document_id="resume",
                kind="resume",
                label="Resume",
                revision="1",
                approved_for=["general"],
                approved_by=None,
                default=True,
            ),
            tmp_path / "jobs.json",
        )

    assert not (profile_path.parent / "documents/resume.pdf").exists()
    assert not manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()
    assert parse_application_profile(profile_path).documents == []


def test_stale_document_add_transaction_recovers_before_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    destination = profile_path.parent / "documents/resume.pdf"
    destination.parent.mkdir(mode=0o700)
    staging_path = destination.parent / (
        ".resume.pdf.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.staging"
    )
    staging_path.write_bytes(b"orphaned copy")
    staging_path.chmod(0o600)
    os.link(staging_path, destination)
    manage_application_profile._write_document_add_transaction(
        profile_path,
        document_id="resume",
        document_path="documents/resume.pdf",
        staging_path=staging_path.relative_to(profile_path.parent).as_posix(),
    )
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )

    result = manage_application_profile.add_profile_document(
        profile_path,
        Namespace(
            source=source,
            document_id="resume",
            kind="resume",
            label="Resume",
            revision="1",
            approved_for=["general"],
            approved_by=None,
            default=True,
        ),
        tmp_path / "jobs.json",
    )

    assert result["document_id"] == "resume"
    assert destination.read_bytes() == b"resume"
    assert not manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()


def test_committed_document_add_transaction_is_idempotent_on_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    document_args = Namespace(
        source=source,
        document_id="resume",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=True,
    )
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    first = manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )
    committed = parse_application_profile(profile_path).documents[0]
    committed_metadata = ApprovedDocumentMetadata.model_validate(
        committed.model_dump(
            exclude={"approval_key_id", "approval_signature"}
        )
    )
    manage_application_profile._write_document_add_transaction(
        profile_path,
        document_id="resume",
        document_path="documents/resume.pdf",
        staging_path=(
            "documents/.resume.pdf.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.staging"
        ),
        request_fingerprint=(
            manage_application_profile._document_add_request_fingerprint(
                committed_metadata
            )
        ),
    )

    retried = manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )

    assert retried == first
    assert len(parse_application_profile(profile_path).documents) == 1
    assert not manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()


def test_staging_unlink_failure_retains_journal_for_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    document_args = Namespace(
        source=source,
        document_id="resume",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=True,
    )
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    original_unlink = manage_application_profile._unlink_owner_only_staging
    monkeypatch.setattr(
        manage_application_profile,
        "_unlink_owner_only_staging",
        lambda path: False,
    )

    first = manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )
    transaction = manage_application_profile._read_document_add_transaction(
        profile_path
    )
    staging_path = profile_path.parent / transaction["staging_path"]

    assert staging_path.exists()
    assert manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()

    monkeypatch.setattr(
        manage_application_profile,
        "_unlink_owner_only_staging",
        original_unlink,
    )
    retried = manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )

    assert retried == first
    assert not staging_path.exists()
    assert not manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()


@pytest.mark.parametrize("changed", ["content", "metadata"])
def test_committed_document_retry_rejects_changed_request(
    tmp_path: Path,
    monkeypatch,
    changed: str,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    manage_application_profile.initialize_profile(
        profile_path,
        Namespace(
            profile_id="primary",
            profile_name="Primary",
            approval_name="Owner",
        ),
    )
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"resume")
    document_args = Namespace(
        source=source,
        document_id="resume",
        kind="resume",
        label="Resume",
        revision="1",
        approved_for=["general"],
        approved_by=None,
        default=True,
    )
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    manage_application_profile.add_profile_document(
        profile_path,
        document_args,
        tmp_path / "jobs.json",
    )
    committed = parse_application_profile(profile_path).documents[0]
    committed_metadata = ApprovedDocumentMetadata.model_validate(
        committed.model_dump(
            exclude={"approval_key_id", "approval_signature"}
        )
    )
    manage_application_profile._write_document_add_transaction(
        profile_path,
        document_id="resume",
        document_path="documents/resume.pdf",
        staging_path=(
            "documents/.resume.pdf.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.staging"
        ),
        request_fingerprint=(
            manage_application_profile._document_add_request_fingerprint(
                committed_metadata
            )
        ),
    )
    if changed == "content":
        source.write_bytes(b"different resume")
    else:
        document_args.label = "Different resume"

    with pytest.raises(ValueError, match="does not match"):
        manage_application_profile.add_profile_document(
            profile_path,
            document_args,
            tmp_path / "jobs.json",
        )

    assert manage_application_profile._document_add_transaction_path(
        profile_path
    ).exists()


def test_default_validation_decrypts_every_profile_field(tmp_path: Path) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)
    record = storage.sensitive_values_table.all()[0]
    envelope = dict(record["encrypted_value"])
    envelope["ciphertext"] = "A" * 24
    storage.sensitive_values_table.update(
        {"encrypted_value": envelope},
        doc_ids=[record.doc_id],
    )

    with pytest.raises(SensitiveDataError, match="authenticated"):
        validate_application_profile(
            profile,
            profile_path,
            storage=storage,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        )


def test_profile_validation_rejects_duplicate_secret_ids(tmp_path: Path) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)
    records = storage.sensitive_values_table.all()
    duplicate_id = records[0]["secret_id"]
    for record in records[1:]:
        storage.sensitive_values_table.update(
            {"secret_id": duplicate_id},
            doc_ids=[record.doc_id],
        )

    with pytest.raises(ValueError, match="duplicate secret IDs"):
        validate_application_profile(
            profile,
            profile_path,
            storage=storage,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
            require_ready=True,
        )


def test_validation_rejects_oversized_document_before_reading(
    tmp_path: Path,
) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)
    path = profile_path.parent / profile.documents[0].path
    with path.open("r+b") as document_file:
        document_file.truncate(MAX_APPLICATION_DOCUMENT_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds the size limit"):
        validate_application_profile(
            profile,
            profile_path,
            storage=storage,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        )


def test_validation_enforces_size_limit_while_streaming(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path, profile, storage = complete_profile(tmp_path)
    path = profile_path.parent / profile.documents[0].path
    with path.open("r+b") as document_file:
        document_file.truncate(MAX_APPLICATION_DOCUMENT_BYTES + 1)
    original_fstat = os.fstat

    def stale_fstat(descriptor: int) -> os.stat_result:
        result = original_fstat(descriptor)
        values = list(result)
        values[6] = 1
        return os.stat_result(values)

    monkeypatch.setattr("app.application_profile.os.fstat", stale_fstat)

    with pytest.raises(ValueError, match="exceeds the size limit"):
        validate_application_profile(
            profile,
            profile_path,
            storage=storage,
            sensitive_cipher=SensitiveValueCipher(TEST_KEY),
        )


def test_document_copy_enforces_size_limit_while_streaming(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "resume.pdf"
    with source.open("wb") as document_file:
        document_file.truncate(MAX_APPLICATION_DOCUMENT_BYTES + 1)
    destination = tmp_path / "private/documents/resume.pdf"
    original_fstat = os.fstat

    def stale_fstat(descriptor: int) -> os.stat_result:
        result = original_fstat(descriptor)
        values = list(result)
        values[6] = 1
        return os.stat_result(values)

    monkeypatch.setattr(
        "scripts.manage_application_profile.os.fstat",
        stale_fstat,
    )

    with pytest.raises(ValueError, match="exceeds the size limit"):
        manage_application_profile._copy_owner_only(source, destination)
    assert not destination.exists()


def test_loader_rejects_profile_symlink_before_parsing(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "target.yaml"
    target.write_text("not: [valid", encoding="utf-8")
    link = private / "profile.yaml"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="must not contain symlinks"):
        parse_application_profile(link)


def test_initialize_generates_distinct_profile_ids(tmp_path: Path) -> None:
    args = Namespace(
        profile_name="Primary",
        approval_name="Owner",
    )
    first_path = tmp_path / "first" / "profile.yaml"
    second_path = tmp_path / "second" / "profile.yaml"

    first = manage_application_profile.initialize_profile(first_path, args)
    second = manage_application_profile.initialize_profile(second_path, args)

    assert first["profile_id"] != second["profile_id"]
    assert first["profile_id"].startswith("profile_")
    assert second["profile_id"].startswith("profile_")


def test_cli_handles_default_config_failure_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        manage_application_profile,
        "configured_application_profile_path",
        lambda: (_ for _ in ()).throw(ValueError("config is invalid")),
    )

    result = manage_application_profile.main(["list"])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.err.strip() == (
        "Application profile operation failed: config is invalid"
    )


def test_field_replacement_keeps_a_stable_encrypted_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "private" / "profile.yaml"
    args = Namespace(
        profile_id="primary",
        profile_name="Primary",
        approval_name="Owner",
    )
    database_path = tmp_path / "jobs.json"
    manage_application_profile.initialize_profile(profile_path, args)
    monkeypatch.setattr(
        manage_application_profile,
        "_load_cipher",
        lambda: SensitiveValueCipher(TEST_KEY),
    )
    values = iter(["first@example.com", "second@example.com"])
    monkeypatch.setattr(
        manage_application_profile.getpass,
        "getpass",
        lambda prompt: next(values),
    )
    field_args = Namespace(
        field_name="email",
        category="contact",
        reuse_policy="confirm_first",
        approved_by=None,
        confirm_retention=False,
    )
    manage_application_profile.set_profile_field(
        profile_path,
        database_path,
        field_args,
    )
    original = JobStorage(database_path).list_sensitive_values()[0]
    result = manage_application_profile.set_profile_field(
        profile_path,
        database_path,
        field_args,
    )

    storage = JobStorage(
        database_path,
        sensitive_cipher=SensitiveValueCipher(TEST_KEY),
    )
    records = storage.list_sensitive_values()
    assert result["replaced"] is True
    assert result["backup_purge_recommended"] is True
    assert [record["secret_id"] for record in records] == [original["secret_id"]]
    assert storage.read_sensitive_value(
        original["secret_id"],
        purpose="owner_review",
    ) == "second@example.com"
