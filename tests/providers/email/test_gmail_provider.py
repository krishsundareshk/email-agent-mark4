"""
Unit tests for GmailProvider — email fetching and processing.

All tests use mocked Gmail API responses.
No real Gmail API calls are made during any test.
"""

import base64
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

from googleapiclient.errors import HttpError

from app.providers.email.gmail_provider import GmailProvider
from app.providers.email.base import EmailFetchError


# ─────────────────────────────────────────────────────────────────────────────
# Helpers & Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def b64(text: str) -> str:
    """Encode a string the way Gmail does — base64url without padding."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_gmail_message(
    msg_id: str = "msg_001",
    subject: str = "Test Subject",
    from_header: str = "John Doe <john@example.com>",
    to_header: str = "me@example.com",
    body_text: str = "This is the email body.",
    mime_type: str = "text/plain",
    has_attachment: bool = False,
) -> dict:
    """
    Build a mock Gmail API message response.
    Supports simple (text/plain, text/html) and multipart structures.
    Optionally adds a PDF attachment part.
    """
    attachment_parts = []
    if has_attachment:
        attachment_parts.append({
            "mimeType": "application/pdf",
            "filename": "report.pdf",
            "body": {}
        })

    base_headers = [
        {"name": "From", "value": from_header},
        {"name": "To", "value": to_header},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 24 Jun 2026 10:00:00 +0000"},
    ]

    if mime_type == "multipart/alternative":
        payload = {
            "mimeType": "multipart/alternative",
            "headers": base_headers,
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": b64(body_text)}
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": b64(f"<p>{body_text}</p>")}
                },
            ] + attachment_parts,
        }
    else:
        payload = {
            "mimeType": mime_type,
            "headers": base_headers,
            "body": {"data": b64(body_text)},
            "parts": attachment_parts,
        }

    return {
        "id": msg_id,
        "internalDate": "1750758000000",
        "payload": payload,
    }


def make_http_error(status: int, reason: str = "") -> HttpError:
    """Build a mock HttpError with a given HTTP status code."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.reason = reason
    return HttpError(
        resp=mock_resp,
        content=b'{"error": {"message": "' + reason.encode() + b'"}}'
    )


@pytest.fixture
def provider():
    """
    Pytest fixture — returns a GmailProvider with a mocked Gmail service.
    Patches environment variables so no real paths are needed.
    The fixture is function-scoped (default) — fresh provider per test.
    """
    with patch.dict("os.environ", {
        "GOOGLE_CREDENTIALS_PATH": "google_credentials.json",
        "GOOGLE_TOKEN_PATH": "google_token.json",
    }):
        p = GmailProvider()
        p._service = MagicMock()
        yield p


@pytest.fixture
def batch_window():
    """Standard batch window used across tests."""
    return (
        datetime(2026, 6, 24, tzinfo=timezone.utc),
        datetime(2026, 6, 25, tzinfo=timezone.utc),
    )


def setup_list_response(provider, message_ids: list[str], next_page_token=None):
    """
    Helper — configure the mock service to return a given list of message IDs.
    Optionally sets a nextPageToken to simulate pagination.
    """
    response = {"messages": [{"id": mid} for mid in message_ids]}
    if next_page_token:
        response["nextPageToken"] = next_page_token
    provider._service.users.return_value.messages.return_value \
        .list.return_value.execute.return_value = response


def setup_get_response(provider, message: dict):
    """Helper — configure the mock service to return a given message on .get()."""
    provider._service.users.return_value.messages.return_value \
        .get.return_value.execute.return_value = message


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchEmails:
    """Tests for the fetch_emails() method."""

    def test_fetch_returns_email_object_list(self, provider, batch_window):
        """Happy path — correct EmailObject fields mapped from a Gmail response."""
        since, until = batch_window
        message = make_gmail_message(
            msg_id="msg_001",
            subject="Project Update",
            from_header="Jane Smith <jane@example.com>",
            to_header="me@example.com",
            body_text="Here is the project update for this week.",
        )
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        assert len(emails) == 1
        assert emails[0].email_id == "msg_001"
        assert emails[0].subject == "Project Update"
        assert emails[0].sender_name == "Jane Smith"
        assert emails[0].sender_email == "jane@example.com"
        assert emails[0].recipients == ["me@example.com"]

    def test_empty_inbox_returns_empty_list(self, provider, batch_window):
        """Gmail returns no messages in the window — empty list returned cleanly."""
        since, until = batch_window
        provider._service.users.return_value.messages.return_value \
            .list.return_value.execute.return_value = {}

        emails = provider.fetch_emails(since, until)

        assert emails == []
        assert len(emails) == 0

    def test_service_not_initialized_raises_runtime_error(self, batch_window):
        """fetch_emails() before authenticate() raises RuntimeError immediately."""
        since, until = batch_window
        with patch.dict("os.environ", {
            "GOOGLE_CREDENTIALS_PATH": "google_credentials.json",
            "GOOGLE_TOKEN_PATH": "google_token.json",
        }):
            unauthed_provider = GmailProvider()
            # _service is None — authenticate() was never called

        with pytest.raises(RuntimeError, match="Call authenticate()"):
            unauthed_provider.fetch_emails(since, until)


class TestBodyDecoding:
    """Tests for MIME parsing and base64url body decoding."""

    def test_plain_text_body_decoded_correctly(self, provider, batch_window):
        """Simple text/plain email body is decoded from base64url correctly."""
        since, until = batch_window
        message = make_gmail_message(
            body_text="Hello, this is a plain text email.",
            mime_type="text/plain"
        )
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        assert emails[0].body_text == "Hello, this is a plain text email."

    def test_multipart_prefers_plain_text_over_html(self, provider, batch_window):
        """Multipart email returns text/plain body, not text/html."""
        since, until = batch_window
        message = make_gmail_message(
            body_text="Plain text content.",
            mime_type="multipart/alternative"
        )
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        # Should be plain text, not "<p>Plain text content.</p>"
        assert emails[0].body_text == "Plain text content."
        assert "<p>" not in emails[0].body_text

    def test_empty_body_returns_empty_string(self, provider, batch_window):
        """Email with no body data returns empty string — does not crash."""
        since, until = batch_window
        message = make_gmail_message(body_text="", mime_type="text/plain")
        # Manually clear the body data to simulate missing body
        message["payload"]["body"]["data"] = ""
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        assert emails[0].body_text == ""


class TestDeduplication:
    """Tests for already-processed email exclusion."""

    def test_already_processed_email_is_skipped(self, provider, batch_window):
        """Email ID already in _processed_ids is excluded from results."""
        since, until = batch_window
        provider._processed_ids.add("msg_already_done")
        setup_list_response(provider, ["msg_already_done"])

        emails = provider.fetch_emails(since, until)

        assert emails == []
        # Verify .get() was never called — we skipped it entirely
        provider._service.users.return_value.messages.return_value \
            .get.assert_not_called()

    def test_only_unprocessed_emails_returned(self, provider, batch_window):
        """When mix of processed and new IDs, only new ones are fetched."""
        since, until = batch_window
        provider._processed_ids.add("msg_old")

        new_message = make_gmail_message(msg_id="msg_new", subject="New Email")
        setup_list_response(provider, ["msg_old", "msg_new"])
        setup_get_response(provider, new_message)

        emails = provider.fetch_emails(since, until)

        assert len(emails) == 1
        assert emails[0].email_id == "msg_new"


class TestMarkProcessed:
    """Tests for the mark_processed() method."""

    def test_mark_processed_adds_id_to_set(self, provider):
        """mark_processed() correctly adds email ID to _processed_ids."""
        assert "email_xyz" not in provider._processed_ids

        provider.mark_processed("email_xyz")

        assert "email_xyz" in provider._processed_ids

    def test_mark_processed_multiple_ids(self, provider):
        """Multiple calls to mark_processed() accumulate correctly."""
        provider.mark_processed("email_001")
        provider.mark_processed("email_002")
        provider.mark_processed("email_003")

        assert len(provider._processed_ids) == 3
        assert "email_001" in provider._processed_ids
        assert "email_002" in provider._processed_ids
        assert "email_003" in provider._processed_ids

    def test_mark_processed_is_idempotent(self, provider):
        """Marking the same ID twice does not create duplicates."""
        provider.mark_processed("email_dup")
        provider.mark_processed("email_dup")

        assert len(provider._processed_ids) == 1


class TestErrorHandling:
    """Tests for API error handling."""

    def test_rate_limit_raises_email_fetch_error(self, provider, batch_window):
        """HTTP 429 raises EmailFetchError with rate limit message."""
        since, until = batch_window
        provider._service.users.return_value.messages.return_value \
            .list.return_value.execute.side_effect = make_http_error(429, "Too Many Requests")

        with pytest.raises(EmailFetchError, match="rate limit"):
            provider.fetch_emails(since, until)

    def test_other_api_error_raises_email_fetch_error(self, provider, batch_window):
        """Non-429 HttpError also raises EmailFetchError."""
        since, until = batch_window
        provider._service.users.return_value.messages.return_value \
            .list.return_value.execute.side_effect = make_http_error(503, "Service Unavailable")

        with pytest.raises(EmailFetchError):
            provider.fetch_emails(since, until)

    def test_single_email_failure_does_not_abort_batch(self, provider, batch_window):
        """
        If one email fails to parse, the rest of the batch continues.
        The failing email is skipped — not the entire run.
        """
        since, until = batch_window
        setup_list_response(provider, ["msg_bad", "msg_good"])

        good_message = make_gmail_message(msg_id="msg_good", subject="Good Email")

        # First call (.get for msg_bad) raises an error
        # Second call (.get for msg_good) returns a valid message
        provider._service.users.return_value.messages.return_value \
            .get.return_value.execute.side_effect = [
                Exception("Unexpected parse error"),
                good_message
            ]

        emails = provider.fetch_emails(since, until)

        # Only the good email is returned — bad one skipped
        assert len(emails) == 1
        assert emails[0].email_id == "msg_good"


class TestPagination:
    """Tests for Gmail API pagination handling."""

    def test_pagination_fetches_all_pages(self, provider, batch_window):
        """
        When Gmail returns nextPageToken, all pages are fetched.
        Verifies messages.list is called multiple times.
        """
        since, until = batch_window

        # Page 1 — has a nextPageToken
        page_1_response = {
            "messages": [{"id": "msg_page1"}],
            "nextPageToken": "token_abc"
        }
        # Page 2 — no nextPageToken, last page
        page_2_response = {
            "messages": [{"id": "msg_page2"}]
        }

        provider._service.users.return_value.messages.return_value \
            .list.return_value.execute.side_effect = [
                page_1_response,
                page_2_response
            ]

        msg_page1 = make_gmail_message(msg_id="msg_page1", subject="Page 1 Email")
        msg_page2 = make_gmail_message(msg_id="msg_page2", subject="Page 2 Email")

        provider._service.users.return_value.messages.return_value \
            .get.return_value.execute.side_effect = [msg_page1, msg_page2]

        emails = provider.fetch_emails(since, until)

        assert len(emails) == 2
        assert emails[0].email_id == "msg_page1"
        assert emails[1].email_id == "msg_page2"


class TestAttachmentDetection:
    """Tests for attachment detection."""

    def test_email_with_attachment_sets_flag(self, provider, batch_window):
        """Email with a named attachment part sets has_attachments=True."""
        since, until = batch_window
        message = make_gmail_message(
            mime_type="multipart/alternative",
            has_attachment=True
        )
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        assert emails[0].has_attachments is True

    def test_email_without_attachment_flag_is_false(self, provider, batch_window):
        """Email with no attachments has has_attachments=False (default)."""
        since, until = batch_window
        message = make_gmail_message(mime_type="text/plain", has_attachment=False)
        setup_list_response(provider, ["msg_001"])
        setup_get_response(provider, message)

        emails = provider.fetch_emails(since, until)

        assert emails[0].has_attachments is False