import os
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.providers.email.base import EmailProvider, EmailObject, EmailFetchError, ApplyLabelResult

logger = logging.getLogger(__name__)

SCOPES = [
    # Widened from gmail.readonly to gmail.modify so apply_label (specs v3
    # §5.5) can write labels. The code's actual capability stays narrowly
    # scoped to label writes only — no message deletion, no send/reply —
    # regardless of the broader permission gmail.modify technically grants.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

# Namespace all agent-applied labels under "Agent/..." (specs v3 §10).
LABEL_NAMESPACE_PREFIX = "Agent"


class GmailProvider(EmailProvider):
    """
    Gmail implementation of EmailProvider.

    Uses OAuth2 with the installed app flow for local development.
    Credentials are loaded from GOOGLE_CREDENTIALS_PATH (.env).
    Token is cached to GOOGLE_TOKEN_PATH (.env) after first login.

    Authentication flow:
    - First run  : opens browser consent screen, saves token to disk
    - Later runs : loads cached token, refreshes silently if expired
    - Never      : stores credentials in code or environment variables directly
    """

    def __init__(self, access_token: Optional[str] = None):
        self._credentials: Optional[Credentials] = None
        self._service = None
        self._access_token = access_token

        self._credentials_path = os.getenv(
            "GOOGLE_CREDENTIALS_PATH",
            "google_credentials.json"
        )
        self._token_path = os.getenv(
            "GOOGLE_TOKEN_PATH",
            "google_token.json"
        )

        if not self._access_token and not self._credentials_path:
            raise ValueError(
                "GOOGLE_CREDENTIALS_PATH is not set in .env. "
                "Download credentials.json from Google Cloud Console and set the path."
            )

        # In-memory set of processed email IDs for this session.
        # Prevents reprocessing within the same run.
        # TODO GH-015: Replace with DB query via get_processed_email_ids(user_id)
        self._processed_ids: set[str] = set()

        # Cache of label name -> Gmail label ID, avoids a labels.list()
        # round-trip on every apply_label() call.
        self._label_id_cache: dict[str, str] = {}
        self._label_name_by_id: dict[str, str] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Authentication (implemented in GH-005)
    # ──────────────────────────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        Authenticate with Gmail using OAuth2.

        Attempts in order:
        1. If access_token is provided, use it directly (stateless)
        2. Load valid cached token from GOOGLE_TOKEN_PATH
        3. Refresh expired token silently using the refresh token
        4. Run browser consent flow if no usable token exists

        Returns:
            True if authentication succeeded and service is ready.
            False if authentication failed for any reason.
        """
        try:
            if self._access_token:
                logger.info("GmailProvider: Authenticating using request access token.")
                self._credentials = Credentials(token=self._access_token)
            else:
                self._credentials = self._load_or_refresh_credentials()

            if not self._credentials or not self._credentials.valid:
                logger.error(
                    "Authentication failed — credentials not valid after all attempts."
                )
                return False

            self._service = build(
                serviceName="gmail",
                version="v1",
                credentials=self._credentials,
            )

            # Verify credentials by performing a lightweight API call.
            # This triggers refresh check early if the access token has expired.
            self._service.users().getProfile(userId="me").execute()

            logger.info("Gmail authentication successful.")
            return True

        except Exception as e:
            logger.error(f"Gmail authentication failed with unexpected error: {e}")
            return False

    def _load_or_refresh_credentials(self) -> Optional[Credentials]:
        """
        Internal method — handles the full credential lifecycle.

        Step 1: Try to load existing token from disk
        Step 2: If expired but refresh token exists, refresh silently
        Step 3: If no token or refresh fails, run browser consent flow
        Step 4: Save updated credentials back to disk
        """
        credentials = None

        if os.path.exists(self._token_path):
            logger.info(f"Loading cached token from {self._token_path}")
            credentials = Credentials.from_authorized_user_file(
                self._token_path,
                SCOPES
            )

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Access token expired — refreshing silently.")
            try:
                credentials.refresh(Request())
                logger.info("Token refreshed successfully.")
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}. Will re-run consent flow.")
                credentials = None

        if not credentials or not credentials.valid:
            logger.info("No valid credentials found — starting OAuth2 consent flow.")

            if not os.path.exists(self._credentials_path):
                raise FileNotFoundError(
                    f"Credentials file not found at '{self._credentials_path}'. "
                    f"Download it from Google Cloud Console → APIs & Services → Credentials."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                self._credentials_path,
                SCOPES
            )
            credentials = flow.run_local_server(port=0)
            logger.info("OAuth2 consent flow completed successfully.")

        self._save_credentials(credentials)
        return credentials

    def _save_credentials(self, credentials: Credentials) -> None:
        """Save credentials to disk for future runs."""
        try:
            with open(self._token_path, "w") as token_file:
                token_file.write(credentials.to_json())
            logger.info(f"Credentials saved to {self._token_path}")
        except Exception as e:
            logger.warning(f"Failed to save credentials to disk: {e}")

    @property
    def service(self):
        """
        Expose the Gmail API service object.
        Raises RuntimeError if authenticate() has not been called.
        """
        if self._service is None:
            raise RuntimeError(
                "Gmail service is not initialized. "
                "Call authenticate() before accessing service."
            )
        return self._service

    # ──────────────────────────────────────────────────────────────────────────
    # Email Fetching (implemented in GH-006)
    # ──────────────────────────────────────────────────────────────────────────

    def fetch_emails(
        self,
        since: datetime,
        until: datetime
    ) -> list[EmailObject]:
        """
        Fetch all emails within the given time window.

        Uses Gmail's search query syntax to filter by date range.
        Skips emails already in _processed_ids (deduplication).
        Fetches full message payload and maps to EmailObject.

        Args:
            since: Start of the batch window (inclusive)
            until: End of the batch window (exclusive)

        Returns:
            List of EmailObject instances. Empty list if none found.

        Raises:
            EmailFetchError: If the Gmail API call fails.
        """
        if self._service is None:
            raise RuntimeError(
                "Call authenticate() before fetch_emails()."
            )

        try:
            # Build Gmail search query for the time window
            # Gmail date format: YYYY/MM/DD
            query = self._build_date_query(since, until)
            logger.info(f"Fetching emails with query: {query}")

            # Step 1 — Get list of message IDs matching the query
            message_ids = self._list_message_ids(query)
            logger.info(f"Found {len(message_ids)} emails in window.")

            if not message_ids:
                return []

            # Step 2 — Fetch full message for each ID and map to EmailObject
            emails = []
            for msg_id in message_ids:

                # Skip already processed emails
                if msg_id in self._processed_ids:
                    logger.debug(f"Skipping already processed email: {msg_id}")
                    continue

                try:
                    email_obj = self._fetch_and_map_message(msg_id)
                    if email_obj:
                        emails.append(email_obj)
                except Exception as e:
                    # Single email failure does not abort the entire fetch
                    logger.warning(f"Failed to fetch email {msg_id}: {e}")
                    continue

            logger.info(f"Successfully fetched {len(emails)} emails.")
            return emails

        except HttpError as e:
            if e.resp.status == 429:
                raise EmailFetchError(
                    "Gmail API rate limit exceeded. Will retry on next batch run.",
                    original_error=e
                )
            raise EmailFetchError(
                f"Gmail API error: {e.resp.status} — {e.error_details}",
                original_error=e
            )
        except EmailFetchError:
            raise
        except Exception as e:
            raise EmailFetchError(
                f"Unexpected error during email fetch: {e}",
                original_error=e
            )

    def _build_date_query(self, since: datetime, until: datetime) -> str:
        since_epoch = int(since.timestamp())
        until_epoch = int(until.timestamp())
        return f"in:inbox after:{since_epoch} before:{until_epoch}"

    def _list_message_ids(self, query: str) -> list[str]:
        """
        Retrieve all message IDs matching the query.
        Handles Gmail API pagination automatically.

        Returns:
            List of message ID strings.
        """
        message_ids = []
        page_token = None

        while True:
            params = {
                "userId": "me",
                "q": query,
                "maxResults": 100,  # Max allowed per page by Gmail API
            }
            if page_token:
                params["pageToken"] = page_token

            response = self._service.users().messages().list(**params).execute()

            messages = response.get("messages", [])
            message_ids.extend([msg["id"] for msg in messages])

            # Check if there are more pages
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids

    def _fetch_and_map_message(self, message_id: str) -> Optional[EmailObject]:
        """
        Fetch a single message by ID and map it to EmailObject.

        Fetches with format=full to get headers and body.
        Parses MIME structure to extract plain text body.

        Returns:
            EmailObject on success, None if message cannot be parsed.
        """
        raw_message = self._service.users().messages().get(
            userId="me",
            id=message_id,
            format="full"
        ).execute()

        payload = raw_message.get("payload", {})
        headers = payload.get("headers", [])

        # Parse headers into a dict for easy access
        header_map = {h["name"].lower(): h["value"] for h in headers}

        # Extract and parse From header
        from_raw = header_map.get("from", "")
        sender_name, sender_email = parseaddr(from_raw)
        if not sender_name:
            sender_name = sender_email  # fallback if no display name

        # Extract To header — may be comma-separated list
        to_raw = header_map.get("to", "")
        recipients = [
            addr.strip()
            for addr in to_raw.split(",")
            if addr.strip()
        ]

        # Extract Subject
        subject = header_map.get("subject", "(No Subject)")

        # Extract and parse Date header
        date_raw = header_map.get("date", "")
        timestamp = self._parse_email_date(date_raw, raw_message)

        # Extract body text — prefer plain text over HTML
        body_text = self._extract_body(payload)

        # Check for attachments
        has_attachments = self._has_attachments(payload)

        return EmailObject(
            email_id=message_id,
            thread_id=raw_message.get("threadId"),
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            timestamp=timestamp,
            has_attachments=has_attachments,
            # RFC-standard bulk-mail signals for Stage 0 (specs v3 §2) —
            # never a hand-maintained keyword/sender blocklist.
            list_unsubscribe=header_map.get("list-unsubscribe"),
            list_unsubscribe_post=header_map.get("list-unsubscribe-post"),
            precedence=header_map.get("precedence"),
        )

    def _extract_body(self, payload: dict) -> str:
        """
        Extract plain text body from the MIME payload.

        Handles two structures:
        1. Simple email  — body is directly in payload.body.data
        2. Multipart     — body is in payload.parts[], prefer text/plain

        Always prefers text/plain over text/html.
        Falls back to HTML if no plain text found.
        Returns empty string if no body can be extracted.
        """
        mime_type = payload.get("mimeType", "")

        # Simple email — body directly in payload
        if mime_type == "text/plain":
            return self._decode_base64(payload.get("body", {}).get("data", ""))

        # HTML-only email — decode and return as-is
        if mime_type == "text/html":
            return self._decode_base64(payload.get("body", {}).get("data", ""))

        # Multipart email — search parts for text/plain
        if mime_type.startswith("multipart"):
            parts = payload.get("parts", [])
            return self._extract_from_parts(parts)

        return ""

    def _extract_from_parts(self, parts: list) -> str:
        """
        Recursively search MIME parts for plain text content.
        Prefers text/plain. Falls back to text/html.
        Handles nested multipart structures.
        """
        plain_text = ""
        html_text = ""

        for part in parts:
            mime_type = part.get("mimeType", "")

            if mime_type == "text/plain":
                plain_text = self._decode_base64(
                    part.get("body", {}).get("data", "")
                )

            elif mime_type == "text/html" and not plain_text:
                html_text = self._decode_base64(
                    part.get("body", {}).get("data", "")
                )

            elif mime_type.startswith("multipart"):
                # Recurse into nested multipart
                nested = self._extract_from_parts(part.get("parts", []))
                if nested:
                    return nested

        return plain_text or html_text

    def _decode_base64(self, data: str) -> str:
        """
        Decode a base64url-encoded string to plain text.

        Gmail uses base64url encoding (RFC 4648) which replaces:
          + with -
          / with _
        Python's urlsafe_b64decode handles this but requires
        padding to be a multiple of 4 characters.

        Returns empty string if data is empty or decoding fails.
        """
        if not data:
            return ""
        try:
            # Fix padding — base64 requires length to be multiple of 4
            padded = data + "=" * (4 - len(data) % 4)
            decoded_bytes = base64.urlsafe_b64decode(padded)
            return decoded_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Failed to decode base64 body: {e}")
            return ""

    def _parse_email_date(
        self,
        date_raw: str,
        raw_message: dict
    ) -> datetime:
        """
        Parse the email Date header into a timezone-aware datetime.

        Falls back to internalDate (Gmail's server-side timestamp)
        if the Date header is missing or unparseable.

        Gmail's internalDate is Unix timestamp in milliseconds.
        """
        if date_raw:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(date_raw)
            except Exception:
                logger.debug(f"Could not parse Date header: {date_raw}. Using internalDate.")

        # Fallback to Gmail's internalDate (milliseconds since epoch)
        internal_date_ms = int(raw_message.get("internalDate", 0))
        return datetime.fromtimestamp(
            internal_date_ms / 1000,
            tz=timezone.utc
        )

    def _has_attachments(self, payload: dict) -> bool:
        """
        Detect if the email has any attachments.
        Checks if any part has a filename — that indicates an attachment.
        """
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("filename"):
                return True
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # Mark Processed (implemented in GH-006)
    # ──────────────────────────────────────────────────────────────────────────

    def mark_processed(self, email_id: str) -> None:
        """
        Record that this email has been processed.

        Adds the email ID to the in-memory set for this session.
        Does NOT modify the email in Gmail — no read/unread changes.

        TODO GH-015: Also persist to DB via save_processed_email_id(email_id)
        so processed IDs survive across batch runs.
        """
        self._processed_ids.add(email_id)
        logger.debug(f"Marked email as processed: {email_id}")

    # ──────────────────────────────────────────────────────────────────────────
    # apply_label (specs v3 §5.5) — Gmail's half of the dual-write.
    # ──────────────────────────────────────────────────────────────────────────

    def apply_label(self, email_id: str, label: str) -> ApplyLabelResult:
        """
        Apply a namespaced Gmail label (e.g. "Agent/Internal") to a message.

        Gmail represents labels as first-class objects with their own IDs —
        unlike Outlook's category/folder model. Creates the label if it
        doesn't already exist, then attaches it via messages.modify.

        Never raises — always returns ApplyLabelResult so the caller
        (the apply_label tool + reconciliation job) can detect drift
        without a try/except at every call site.
        """
        if self._service is None:
            return ApplyLabelResult(
                success=False,
                error_message="Gmail service not initialized — call authenticate() first.",
            )

        provider_label = f"{LABEL_NAMESPACE_PREFIX}/{label}"

        try:
            label_id = self._get_or_create_label_id(provider_label)
            self._service.users().messages().modify(
                userId="me",
                id=email_id,
                body={"addLabelIds": [label_id]},
            ).execute()
            logger.info(f"Applied Gmail label '{provider_label}' to message {email_id}")
            return ApplyLabelResult(success=True, provider_label=provider_label)
        except HttpError as e:
            logger.error(f"Gmail apply_label failed for {email_id}: {e}")
            return ApplyLabelResult(success=False, error_message=str(e))
        except Exception as e:
            logger.error(f"Unexpected error in Gmail apply_label for {email_id}: {e}")
            return ApplyLabelResult(success=False, error_message=str(e))

    def _get_or_create_label_id(self, label_name: str) -> str:
        """Resolve a Gmail label name to its ID, creating it if missing."""
        if label_name in self._label_id_cache:
            return self._label_id_cache[label_name]

        existing = self._service.users().labels().list(userId="me").execute()
        for lbl in existing.get("labels", []):
            if lbl["name"] == label_name:
                self._label_id_cache[label_name] = lbl["id"]
                return lbl["id"]

        created = self._service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        self._label_id_cache[label_name] = created["id"]
        return created["id"]

    def get_current_label(self, email_id: str) -> Optional[str]:
        """Reconciliation-job support — reads the message's current Agent/ label."""
        if self._service is None:
            return None
        try:
            msg = self._service.users().messages().get(
                userId="me", id=email_id, format="metadata", metadataHeaders=[]
            ).execute()
            label_ids = msg.get("labelIds", [])

            # Build/refresh a reverse id->name map from the (already cached
            # forward) label list if we don't have one yet.
            if not hasattr(self, "_label_name_by_id") or not self._label_name_by_id:
                all_labels = self._service.users().labels().list(userId="me").execute()
                self._label_name_by_id = {
                    lbl["id"]: lbl["name"] for lbl in all_labels.get("labels", [])
                }

            for label_id in label_ids:
                name = self._label_name_by_id.get(label_id, "")
                if name.startswith(f"{LABEL_NAMESPACE_PREFIX}/"):
                    return name
            return None
        except Exception as e:
            logger.warning(f"get_current_label failed for {email_id}: {e}")
            return None