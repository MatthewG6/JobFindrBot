import base64
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from app.gmail_client import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_GMAIL_QUERY,
    DEFAULT_GMAIL_DB_PATH,
    DEFAULT_TOKEN_PATH,
    GMAIL_READONLY_SCOPE,
    GmailConfigurationError,
    GmailJobAlertClient,
    GmailMessageError,
    decode_base64url,
    gmail_message_to_job_alert_email,
    load_gmail_credentials,
    mime_body_text,
    write_token_file,
)
from app.storage import JobStorage


LINKEDIN_BODY = Path("tests/fixtures/linkedin_job_alert.txt").read_text(
    encoding="utf-8"
)
INDEED_BODY = Path("tests/fixtures/indeed_job_alert.txt").read_text(
    encoding="utf-8"
)


def encode_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def gmail_message(
    message_id: str,
    source: str = "linkedin",
    body: str | None = None,
    include_authentication: bool = True,
    attachment_id: str | None = None,
) -> dict:
    if source == "linkedin":
        sender = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
        authentication = (
            "mx.google.com; dmarc=pass header.from=linkedin.com"
        )
        label_id = "LABEL_LINKEDIN"
        text = body if body is not None else LINKEDIN_BODY
    else:
        sender = "Indeed <donotreply@jobalert.indeed.com>"
        authentication = (
            "mx.google.com; dmarc=pass header.from=jobalert.indeed.com"
        )
        label_id = "LABEL_INDEED"
        text = body if body is not None else INDEED_BODY

    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": "Sanitized job alert"},
    ]
    if include_authentication:
        headers.append(
            {"name": "Authentication-Results", "value": authentication}
        )

    text_part = {
        "mimeType": "text/plain",
        "body": (
            {"attachmentId": attachment_id}
            if attachment_id
            else {"data": encode_body(text)}
        ),
    }
    return {
        "id": message_id,
        "internalDate": "1785778200000",
        "labelIds": [label_id, "INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                text_part,
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_body("<p>Sanitized alert</p>")},
                },
            ],
        },
    }


class FakeRequest:
    def __init__(self, response: dict) -> None:
        self.response = response

    def execute(self) -> dict:
        return self.response


class FakeLabels:
    def __init__(self, labels: list[dict]) -> None:
        self.labels = labels

    def list(self, **kwargs) -> FakeRequest:
        assert kwargs == {"userId": "me"}
        return FakeRequest({"labels": self.labels})


class FakeAttachments:
    def __init__(self, attachments: dict[tuple[str, str], str]) -> None:
        self._attachments = attachments

    def get(self, **kwargs) -> FakeRequest:
        key = (kwargs["messageId"], kwargs["id"])
        return FakeRequest({"data": self._attachments.get(key)})


class FakeMessages:
    def __init__(
        self,
        messages: dict[str, dict],
        ids_by_label: dict[str, list[str]],
        attachments: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._messages = messages
        self._ids_by_label = ids_by_label
        self._attachments = FakeAttachments(attachments or {})
        self.get_calls: list[str] = []
        self.list_calls: list[dict] = []

    def list(self, **kwargs) -> FakeRequest:
        self.list_calls.append(kwargs)
        ids = self._ids_by_label.get(kwargs["labelIds"][0], [])
        page_number = int(kwargs.get("pageToken", "0"))
        start = page_number * 2
        page = ids[start : start + 2]
        response = {"messages": [{"id": message_id} for message_id in page]}
        if start + 2 < len(ids):
            response["nextPageToken"] = str(page_number + 1)
        return FakeRequest(response)

    def get(self, **kwargs) -> FakeRequest:
        assert kwargs["userId"] == "me"
        assert kwargs["format"] == "full"
        self.get_calls.append(kwargs["id"])
        return FakeRequest(self._messages[kwargs["id"]])

    def attachments(self) -> FakeAttachments:
        return self._attachments


class FakeUsers:
    def __init__(
        self,
        labels: list[dict],
        messages: FakeMessages,
    ) -> None:
        self._labels = FakeLabels(labels)
        self._messages = messages

    def labels(self) -> FakeLabels:
        return self._labels

    def messages(self) -> FakeMessages:
        return self._messages


class FakeService:
    def __init__(
        self,
        messages: dict[str, dict],
        ids_by_label: dict[str, list[str]],
        labels: list[dict] | None = None,
        attachments: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.messages_resource = FakeMessages(
            messages,
            ids_by_label,
            attachments,
        )
        self._users = FakeUsers(
            labels
            or [
                {"id": "LABEL_LINKEDIN", "name": "Jobbot-LinkedIn"},
                {"id": "LABEL_INDEED", "name": "Jobbot-Indeed"},
                {"id": "INBOX", "name": "INBOX"},
            ],
            self.messages_resource,
        )

    def users(self) -> FakeUsers:
        return self._users


class FakeCredentials:
    def __init__(
        self,
        valid: bool = True,
        expired: bool = False,
        refresh_token: str | None = None,
        scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,),
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.scopes = scopes
        self.refreshed = False

    def refresh(self, request) -> None:
        self.valid = True
        self.expired = False
        self.refreshed = True

    def to_json(self) -> str:
        return json.dumps({"token": "sanitized-token"})


def test_decode_base64url_handles_missing_padding() -> None:
    encoded = encode_body("Job alert text")

    assert decode_base64url(encoded) == "Job alert text"


@pytest.mark.parametrize("invalid_prefix", ["!!!!", "++++", "////"])
def test_decode_base64url_rejects_invalid_alphabet_characters(
    invalid_prefix: str,
) -> None:
    with pytest.raises(GmailMessageError, match="not valid base64url"):
        decode_base64url(invalid_prefix + encode_body("Job alert text"))


def test_mime_body_text_reads_nested_text_part() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": encode_body("Nested alert")},
                    }
                ],
            }
        ],
    }

    assert mime_body_text(payload, "text/plain") == "Nested alert"


def test_mime_body_text_skips_attachments_and_honors_charset() -> None:
    attachment = {
        "mimeType": "text/plain",
        "filename": "notes.txt",
        "headers": [
            {"name": "Content-Disposition", "value": "attachment"},
        ],
        "body": {"data": encode_body("Unrelated attachment")},
    }
    latin_text = "Développeur"
    latin_data = base64.urlsafe_b64encode(
        latin_text.encode("iso-8859-1")
    ).decode().rstrip("=")
    body = {
        "mimeType": "text/plain",
        "headers": [
            {
                "name": "Content-Type",
                "value": "text/plain; charset=iso-8859-1",
            }
        ],
        "body": {"data": latin_data},
    }

    assert mime_body_text(
        {"mimeType": "multipart/mixed", "parts": [attachment, body]},
        "text/plain",
    ) == latin_text


def test_mime_body_text_prunes_attached_message_subtree() -> None:
    attached_message = {
        "mimeType": "message/rfc822",
        "filename": "forwarded.eml",
        "headers": [
            {"name": "Content-Disposition", "value": "attachment"},
        ],
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": encode_body("ATTACHED MESSAGE")},
            }
        ],
    }
    inline_body = {
        "mimeType": "text/plain",
        "body": {"data": encode_body("Inline job alert")},
    }

    assert mime_body_text(
        {
            "mimeType": "multipart/mixed",
            "parts": [attached_message, inline_body],
        },
        "text/plain",
    ) == "Inline job alert"


def test_gmail_message_conversion_preserves_security_provenance() -> None:
    message = gmail_message("gmail-linkedin-1")

    alert = gmail_message_to_job_alert_email(
        message,
        {"Jobbot-LinkedIn", "INBOX"},
    )

    assert alert.message_id == "gmail-linkedin-1"
    assert alert.gmail_labels == {"Jobbot-LinkedIn", "INBOX"}
    assert alert.authentication_results.startswith("mx.google.com;")
    assert alert.text_body == LINKEDIN_BODY
    assert alert.received_at == datetime.fromtimestamp(
        1785778200,
        UTC,
    )


def test_gmail_message_conversion_loads_text_attachment() -> None:
    message = gmail_message(
        "gmail-linkedin-attachment",
        attachment_id="text-part-1",
    )

    alert = gmail_message_to_job_alert_email(
        message,
        {"Jobbot-LinkedIn"},
        attachment_loader=lambda attachment_id: (
            encode_body(LINKEDIN_BODY)
            if attachment_id == "text-part-1"
            else ""
        ),
    )

    assert alert.text_body == LINKEDIN_BODY


def test_gmail_message_requires_google_authentication_header() -> None:
    message = gmail_message(
        "gmail-untrusted-1",
        include_authentication=False,
    )

    with pytest.raises(GmailMessageError):
        gmail_message_to_job_alert_email(message, {"Jobbot-LinkedIn"})


def test_gmail_message_rejects_ambiguous_authentication_headers() -> None:
    message = gmail_message("gmail-ambiguous-auth")
    message["payload"]["headers"].append(
        {
            "name": "Authentication-Results",
            "value": "mx.google.com; dmarc=pass header.from=linkedin.com",
        }
    )

    with pytest.raises(GmailMessageError, match="must have one"):
        gmail_message_to_job_alert_email(message, {"Jobbot-LinkedIn"})


def test_scan_reads_only_required_labels_and_ingests_jobs(
    tmp_path: Path,
) -> None:
    service = FakeService(
        messages={
            "gmail-linkedin-1": gmail_message("gmail-linkedin-1"),
            "gmail-indeed-1": gmail_message("gmail-indeed-1", "indeed"),
        },
        ids_by_label={
            "LABEL_LINKEDIN": ["gmail-linkedin-1"],
            "LABEL_INDEED": ["gmail-indeed-1"],
        },
    )
    client = GmailJobAlertClient(service)
    storage = JobStorage(tmp_path / "jobs.json")

    first = client.scan(storage)
    second = client.scan(storage)

    assert first == {
        "messages_found": 2,
        "messages_fetched": 2,
        "messages_processed": 2,
        "messages_skipped": 0,
        "jobs_parsed": 5,
        "jobs_created": 4,
        "errors": [],
    }
    assert second["messages_fetched"] == 0
    assert second["messages_skipped"] == 2
    assert len(storage.list_jobs()) == 4
    assert service.messages_resource.get_calls == [
        "gmail-linkedin-1",
        "gmail-indeed-1",
    ]
    assert all(
        call["q"] == DEFAULT_GMAIL_QUERY
        and call["includeSpamTrash"] is False
        and len(call["labelIds"]) == 1
        for call in service.messages_resource.list_calls
    )


def test_scan_query_has_seven_day_catch_up_window() -> None:
    assert DEFAULT_GMAIL_QUERY == "-in:spam -in:trash newer_than:7d"


def test_scan_paginates_deduplicates_and_isolates_bad_message(
    tmp_path: Path,
) -> None:
    messages = {
        "good": gmail_message("good"),
        "bad": gmail_message("bad", include_authentication=False),
        "duplicate": gmail_message("duplicate"),
    }
    service = FakeService(
        messages=messages,
        ids_by_label={
            "LABEL_LINKEDIN": ["good", "bad", "duplicate"],
            "LABEL_INDEED": ["duplicate"],
        },
    )
    client = GmailJobAlertClient(service)
    storage = JobStorage(tmp_path / "jobs.json")

    summary = client.scan(storage)

    assert summary["messages_found"] == 3
    assert summary["messages_processed"] == 2
    assert summary["jobs_created"] == 2
    assert summary["errors"] == [
        {"message_id": "bad", "error_type": "GmailMessageError"}
    ]
    assert len(service.messages_resource.list_calls) == 3


def test_scan_rejects_missing_required_label(tmp_path: Path) -> None:
    service = FakeService(
        messages={},
        ids_by_label={},
        labels=[
            {"id": "LABEL_LINKEDIN", "name": "Jobbot-LinkedIn"},
        ],
    )

    with pytest.raises(GmailConfigurationError, match="Jobbot-Indeed"):
        GmailJobAlertClient(service).scan(JobStorage(tmp_path / "jobs.json"))


def test_write_token_file_uses_owner_only_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / "credentials" / "token.json"

    write_token_file(token_path, FakeCredentials())

    assert json.loads(token_path.read_text()) == {"token": "sanitized-token"}
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert token_path.parent.stat().st_mode & 0o777 == 0o700


def test_token_temporary_file_is_private_before_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_path = tmp_path / "credentials" / "token.json"
    observed: dict = {}

    def fail_replace(source, destination) -> None:
        observed["mode"] = Path(source).stat().st_mode & 0o777
        raise OSError("simulated interruption")

    monkeypatch.setattr("app.gmail_client.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        write_token_file(token_path, FakeCredentials())

    assert observed["mode"] == 0o600
    assert list(token_path.parent.glob("*.tmp")) == []


def test_noninteractive_credentials_require_existing_token(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")

    with pytest.raises(GmailConfigurationError, match="authorization is required"):
        load_gmail_credentials(
            credentials_path=credentials_path,
            token_path=tmp_path / "token.json",
            allow_interactive=False,
        )


def test_interactive_credentials_create_owner_only_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")
    token_path = tmp_path / "private" / "token.json"
    fake_credentials = FakeCredentials()
    calls: dict = {}

    class FakeFlow:
        def run_local_server(self, port: int):
            calls["port"] = port
            return fake_credentials

    def fake_from_client_secrets_file(path, scopes):
        calls["path"] = path
        calls["scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr(
        "app.gmail_client.InstalledAppFlow.from_client_secrets_file",
        fake_from_client_secrets_file,
    )

    result = load_gmail_credentials(
        credentials_path=credentials_path,
        token_path=token_path,
    )

    assert result is fake_credentials
    assert calls == {
        "path": credentials_path,
        "scopes": (GMAIL_READONLY_SCOPE,),
        "port": 0,
    }
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_expired_credentials_are_refreshed_and_rewritten(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")
    fake_credentials = FakeCredentials(
        valid=False,
        expired=True,
        refresh_token="sanitized-refresh-token",
    )
    monkeypatch.setattr(
        "app.gmail_client.Credentials.from_authorized_user_file",
        lambda path: fake_credentials,
    )

    result = load_gmail_credentials(credentials_path, token_path)

    assert result is fake_credentials
    assert fake_credentials.refreshed is True
    assert json.loads(token_path.read_text()) == {"token": "sanitized-token"}


def test_existing_token_permissions_are_repaired(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")
    token_path.chmod(0o644)
    fake_credentials = FakeCredentials()
    monkeypatch.setattr(
        "app.gmail_client.Credentials.from_authorized_user_file",
        lambda path: fake_credentials,
    )

    load_gmail_credentials(credentials_path, token_path)

    assert token_path.stat().st_mode & 0o777 == 0o600
    assert credentials_path.stat().st_mode & 0o777 == 0o600


def test_existing_token_with_broader_scope_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")
    broader_credentials = FakeCredentials(
        scopes=("https://mail.google.com/",),
    )
    monkeypatch.setattr(
        "app.gmail_client.Credentials.from_authorized_user_file",
        lambda path: broader_credentials,
    )

    with pytest.raises(GmailConfigurationError, match="exactly"):
        load_gmail_credentials(credentials_path, token_path)


def test_adapter_uses_only_readonly_gmail_scope() -> None:
    assert GMAIL_READONLY_SCOPE == (
        "https://www.googleapis.com/auth/gmail.readonly"
    )


def test_default_scheduler_paths_are_project_absolute_paths() -> None:
    assert DEFAULT_CREDENTIALS_PATH.is_absolute()
    assert DEFAULT_TOKEN_PATH.is_absolute()
    assert DEFAULT_GMAIL_DB_PATH.is_absolute()
    assert DEFAULT_CREDENTIALS_PATH.name == "credentials.json"
    assert DEFAULT_GMAIL_DB_PATH.name == "jobs.json"
