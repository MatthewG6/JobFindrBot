import base64
import binascii
from datetime import UTC, datetime
from email.message import Message
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.email_ingestion import ingest_job_alert_email
from app.models import JobAlertEmail
from app.storage import JobStorage


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS_PATH = PROJECT_ROOT / "credentials" / "credentials.json"
DEFAULT_TOKEN_PATH = PROJECT_ROOT / "credentials" / "token.json"
DEFAULT_GMAIL_DB_PATH = PROJECT_ROOT / "data" / "jobs.json"
JOB_ALERT_LABELS = ("Jobbot-LinkedIn", "Jobbot-Indeed")
DEFAULT_GMAIL_QUERY = "-in:spam -in:trash newer_than:7d"
AttachmentLoader = Callable[[str], str]
BASE64URL_VALUE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")


class GmailConfigurationError(RuntimeError):
    pass


class GmailMessageError(ValueError):
    pass


def decode_base64url(value: str, charset: str = "utf-8") -> str:
    if not isinstance(value, str) or BASE64URL_VALUE.fullmatch(value) is None:
        raise GmailMessageError("Gmail MIME body is not valid base64url")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError, TypeError) as error:
        raise GmailMessageError("Gmail MIME body is not valid base64url") from error
    try:
        return decoded.decode(charset, errors="replace")
    except LookupError as error:
        raise GmailMessageError(
            f"Gmail MIME body uses unknown charset {charset}"
        ) from error


def iter_mime_parts(part: dict) -> Iterator[dict]:
    if part_is_attachment(part):
        return
    yield part
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            yield from iter_mime_parts(child)


def part_header_values(part: dict, name: str) -> list[str]:
    return [
        str(header.get("value") or "").strip()
        for header in part.get("headers") or []
        if str(header.get("name") or "").lower() == name.lower()
        and str(header.get("value") or "").strip()
    ]


def part_is_attachment(part: dict) -> bool:
    if str(part.get("filename") or "").strip():
        return True
    dispositions = part_header_values(part, "Content-Disposition")
    return any(
        disposition.lower().startswith("attachment")
        for disposition in dispositions
    )


def part_charset(part: dict) -> str:
    content_types = part_header_values(part, "Content-Type")
    if not content_types:
        return "utf-8"
    message = Message()
    message["Content-Type"] = content_types[0]
    return message.get_content_charset() or "utf-8"


def mime_body_text(
    payload: dict,
    mime_type: str,
    attachment_loader: AttachmentLoader | None = None,
) -> str:
    for part in iter_mime_parts(payload):
        if part.get("mimeType") != mime_type or part_is_attachment(part):
            continue

        body = part.get("body") or {}
        data = body.get("data")
        if not data and body.get("attachmentId") and attachment_loader is not None:
            data = attachment_loader(body["attachmentId"])
        if data:
            text = decode_base64url(data, part_charset(part))
            if text.strip():
                return text

    return ""


def header_values(payload: dict, name: str) -> list[str]:
    return [
        str(header.get("value") or "").strip()
        for header in payload.get("headers") or []
        if str(header.get("name") or "").lower() == name.lower()
        and str(header.get("value") or "").strip()
    ]


def required_header(payload: dict, name: str) -> str:
    values = header_values(payload, name)
    if not values:
        raise GmailMessageError(f"Gmail message is missing {name} header")
    return values[0]


def google_authentication_results(payload: dict) -> str:
    values = header_values(payload, "Authentication-Results")
    if len(values) != 1 or not values[0].lower().startswith("mx.google.com;"):
        raise GmailMessageError(
            "Gmail message must have one Google Authentication-Results header"
        )
    return values[0]


def gmail_message_to_job_alert_email(
    message: dict,
    label_names: set[str],
    attachment_loader: AttachmentLoader | None = None,
) -> JobAlertEmail:
    message_id = str(message.get("id") or "").strip()
    payload = message.get("payload")
    if not message_id or not isinstance(payload, dict):
        raise GmailMessageError("Gmail message is missing ID or MIME payload")

    try:
        received_at = datetime.fromtimestamp(
            int(message["internalDate"]) / 1000,
            UTC,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise GmailMessageError("Gmail message has invalid internalDate") from error

    return JobAlertEmail(
        message_id=message_id,
        sender=required_header(payload, "From"),
        subject=required_header(payload, "Subject"),
        received_at=received_at,
        gmail_labels=label_names,
        authentication_results=google_authentication_results(payload),
        text_body=mime_body_text(payload, "text/plain", attachment_loader),
        html_body=mime_body_text(payload, "text/html", attachment_loader),
    )


def secure_private_file(path: Path, description: str) -> None:
    file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise GmailConfigurationError(f"{description} must be a regular file")
    os.chmod(path, 0o600)


def validate_exact_scopes(credentials: Credentials) -> None:
    scopes = set(credentials.scopes or ())
    if scopes != set(GMAIL_SCOPES):
        raise GmailConfigurationError(
            "Gmail token must grant exactly the gmail.readonly scope"
        )


def write_token_file(token_path: Path, credentials: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(token_path.parent, 0o700)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{token_path.name}.",
        suffix=".tmp",
        dir=token_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as token_file:
            file_descriptor = -1
            token_file.write(credentials.to_json())
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(temporary_path, token_path)
        os.chmod(token_path, 0o600)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)


def load_gmail_credentials(
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
    allow_interactive: bool = True,
) -> Credentials:
    if not credentials_path.is_file():
        raise GmailConfigurationError(
            f"OAuth client file not found at {credentials_path}"
        )
    secure_private_file(credentials_path, "OAuth client file")
    os.chmod(credentials_path.parent, 0o700)

    credentials = None
    if token_path.is_file():
        secure_private_file(token_path, "Gmail token file")
        credentials = Credentials.from_authorized_user_file(token_path)
        validate_exact_scopes(credentials)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        write_token_file(token_path, credentials)
    elif not credentials or not credentials.valid:
        if not allow_interactive:
            raise GmailConfigurationError(
                "Gmail authorization is required; no valid local token exists"
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_path,
            GMAIL_SCOPES,
        )
        credentials = flow.run_local_server(port=0)
        validate_exact_scopes(credentials)
        write_token_file(token_path, credentials)

    if not credentials or not credentials.valid:
        raise GmailConfigurationError("Gmail OAuth credentials are not valid")
    return credentials


class GmailJobAlertClient:
    def __init__(
        self,
        service,
        label_names: tuple[str, ...] = JOB_ALERT_LABELS,
        search_query: str = DEFAULT_GMAIL_QUERY,
    ) -> None:
        self.service = service
        self.label_names = label_names
        self.search_query = search_query

    @classmethod
    def from_local_oauth(
        cls,
        credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
        allow_interactive: bool = True,
    ) -> "GmailJobAlertClient":
        credentials = load_gmail_credentials(
            credentials_path=credentials_path,
            token_path=token_path,
            allow_interactive=allow_interactive,
        )
        service = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
        return cls(service)

    def label_map(self) -> dict[str, str]:
        response = (
            self.service.users().labels().list(userId="me").execute()
        )
        return {
            str(label["id"]): str(label["name"])
            for label in response.get("labels", [])
            if label.get("id") and label.get("name")
        }

    def required_label_ids(self, labels: dict[str, str]) -> dict[str, str]:
        ids_by_name = {name: label_id for label_id, name in labels.items()}
        missing = [name for name in self.label_names if name not in ids_by_name]
        if missing:
            raise GmailConfigurationError(
                f"Required Gmail labels not found: {', '.join(missing)}"
            )
        return {name: ids_by_name[name] for name in self.label_names}

    def iter_message_ids(self, label_id: str) -> Iterator[str]:
        page_token = None
        while True:
            request_args = {
                "userId": "me",
                "labelIds": [label_id],
                "q": self.search_query,
                "includeSpamTrash": False,
                "maxResults": 100,
            }
            if page_token:
                request_args["pageToken"] = page_token
            response = (
                self.service.users()
                .messages()
                .list(**request_args)
                .execute()
            )
            for item in response.get("messages", []):
                message_id = str(item.get("id") or "").strip()
                if message_id:
                    yield message_id
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def get_message(self, message_id: str) -> dict:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def get_attachment_data(
        self,
        message_id: str,
        attachment_id: str,
    ) -> str:
        response = (
            self.service.users()
            .messages()
            .attachments()
            .get(
                userId="me",
                messageId=message_id,
                id=attachment_id,
            )
            .execute()
        )
        data = response.get("data")
        if not data:
            raise GmailMessageError("Gmail text attachment has no data")
        return str(data)

    def scan(self, storage: JobStorage) -> dict:
        labels = self.label_map()
        required_labels = self.required_label_ids(labels)
        message_ids: list[str] = []
        seen_ids: set[str] = set()
        for label_id in required_labels.values():
            for message_id in self.iter_message_ids(label_id):
                if message_id not in seen_ids:
                    seen_ids.add(message_id)
                    message_ids.append(message_id)

        summary = {
            "messages_found": len(message_ids),
            "messages_fetched": 0,
            "messages_processed": 0,
            "messages_skipped": 0,
            "jobs_parsed": 0,
            "jobs_created": 0,
            "errors": [],
        }

        for message_id in message_ids:
            if storage.get_processed_email(message_id) is not None:
                summary["messages_skipped"] += 1
                continue

            try:
                message = self.get_message(message_id)
                summary["messages_fetched"] += 1
                message_labels = {
                    labels[label_id]
                    for label_id in message.get("labelIds", [])
                    if label_id in labels
                }
                alert_email = gmail_message_to_job_alert_email(
                    message,
                    message_labels,
                    attachment_loader=lambda attachment_id: (
                        self.get_attachment_data(message_id, attachment_id)
                    ),
                )
                result = ingest_job_alert_email(alert_email, storage)
                if result["processed"]:
                    summary["messages_processed"] += 1
                    summary["jobs_parsed"] += result["parsed_count"]
                    summary["jobs_created"] += result["created_count"]
                else:
                    summary["messages_skipped"] += 1
            except Exception as error:
                summary["errors"].append(
                    {
                        "message_id": message_id,
                        "error_type": type(error).__name__,
                    }
                )

        return summary
