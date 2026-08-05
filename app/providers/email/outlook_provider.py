import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.providers.email.base import (
    EmailProvider,
    EmailObject,
    EmailFetchError,
    ApplyLabelResult,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
CATEGORY_NAMESPACE_PREFIX = "Agent"

# Outlook/Microsoft Graph OAuth2 scopes needed to read mail and write
# categories. Kept as narrow as the equivalent Gmail scope change
# (pipeline_changes §5) — no Mail.Send, no Mail.ReadWrite beyond categories.
SCOPES = ["Mail.Read", "Mail.ReadWrite", "Calendars.ReadWrite", "offline_access"]


class OutlookProvider(EmailProvider):
    """
    Outlook / Microsoft 365 implementation of EmailProvider, via Microsoft
    Graph (specs v3 §9.2 — the second and final EmailProvider, by design).

    Authentication is stateless: this class expects a valid Graph access
    token (delegated auth, acquired by the frontend/backend's own MSAL
    auth-code flow — analogous to how GmailProvider accepts a Google
    access token). No local token caching / browser consent flow is
    implemented here, mirroring the "stateless, restart-safe" principle
    in specs v3 §10 — token lifecycle belongs to the auth layer, not the
    provider.

    Outlook doesn't have Gmail-style labels; the closest first-class
    equivalent is message *categories* (a string list on each message),
    which is what apply_label() writes to, namespaced "Agent/<label>".
    """

    def __init__(self, access_token: Optional[str] = None):
        self._access_token = access_token
        self._authenticated = False
        self._client: Optional[httpx.Client] = None

    def authenticate(self) -> bool:
        if not self._access_token:
            logger.error("OutlookProvider: no access token supplied.")
            return False
        self._client = httpx.Client(
            base_url=GRAPH_BASE_URL,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )
        try:
            resp = self._client.get("/me")
            resp.raise_for_status()
            self._authenticated = True
            logger.info("Outlook (Microsoft Graph) authentication successful.")
            return True
        except Exception as e:
            logger.error(f"Outlook authentication failed: {e}")
            return False

    def fetch_emails(self, since: datetime, until: datetime) -> list[EmailObject]:
        if not self._authenticated or self._client is None:
            raise RuntimeError("Call authenticate() before fetch_emails().")

        since_iso = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        until_iso = until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        filter_query = (
            f"receivedDateTime ge {since_iso} and receivedDateTime le {until_iso}"
        )
        select_fields = (
            "id,conversationId,subject,bodyPreview,body,from,toRecipients,"
            "receivedDateTime,hasAttachments,internetMessageHeaders"
        )

        emails: list[EmailObject] = []
        url = "/me/messages"
        params = {
            "$filter": filter_query,
            "$select": select_fields,
            "$top": 50,
            "$headers": "true",
        }

        try:
            while url:
                resp = self._client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                for msg in data.get("value", []):
                    email_obj = self._map_message(msg)
                    if email_obj:
                        emails.append(email_obj)
                url = data.get("@odata.nextLink")
                params = None  # nextLink already includes query params
            return emails
        except httpx.HTTPError as e:
            raise EmailFetchError(f"Microsoft Graph fetch failed: {e}", original_error=e)

    def _map_message(self, msg: dict) -> Optional[EmailObject]:
        try:
            sender = (msg.get("from") or {}).get("emailAddress", {})
            recipients = [
                r.get("emailAddress", {}).get("address", "")
                for r in msg.get("toRecipients", [])
                if r.get("emailAddress")
            ]
            body = msg.get("body", {}) or {}
            body_text = body.get("content", "") if body.get("contentType") == "text" else (
                msg.get("bodyPreview", "")
            )

            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("internetMessageHeaders", []) or []
            }

            received = msg.get("receivedDateTime")
            timestamp = (
                datetime.fromisoformat(received.replace("Z", "+00:00"))
                if received
                else datetime.now(timezone.utc)
            )

            return EmailObject(
                email_id=msg["id"],
                thread_id=msg.get("conversationId"),
                sender_name=sender.get("name") or sender.get("address", ""),
                sender_email=sender.get("address", ""),
                recipients=recipients,
                subject=msg.get("subject") or "(No Subject)",
                body_text=body_text,
                timestamp=timestamp,
                has_attachments=bool(msg.get("hasAttachments")),
                list_unsubscribe=headers.get("list-unsubscribe"),
                list_unsubscribe_post=headers.get("list-unsubscribe-post"),
                precedence=headers.get("precedence"),
            )
        except Exception as e:
            logger.warning(f"Failed to map Outlook message: {e}")
            return None

    def mark_processed(self, email_id: str) -> None:
        # Purely local tracking — handled by the DB-backed ProcessedEmailModel
        # in write_to_data_store.py, same as GmailProvider. No provider-side
        # state change (no read/unread mutation).
        logger.debug(f"Marked Outlook email as processed: {email_id}")

    def apply_label(self, email_id: str, label: str) -> ApplyLabelResult:
        """
        Outlook has no label concept identical to Gmail's; the nearest
        first-class equivalent is message *categories* — a string list on
        the message resource (pipeline_changes §5's "a real design decision
        to make explicitly"). We namespace it "Agent/<label>" for parity
        with Gmail's namespaced labels (specs v3 §10).
        """
        if not self._authenticated or self._client is None:
            return ApplyLabelResult(
                success=False,
                error_message="Outlook provider not authenticated — call authenticate() first.",
            )

        provider_label = f"{CATEGORY_NAMESPACE_PREFIX}/{label}"
        try:
            get_resp = self._client.get(
                f"/me/messages/{email_id}", params={"$select": "categories"}
            )
            get_resp.raise_for_status()
            existing_categories = get_resp.json().get("categories", [])

            if provider_label not in existing_categories:
                existing_categories.append(provider_label)

            patch_resp = self._client.patch(
                f"/me/messages/{email_id}",
                json={"categories": existing_categories},
            )
            patch_resp.raise_for_status()
            logger.info(f"Applied Outlook category '{provider_label}' to message {email_id}")
            return ApplyLabelResult(success=True, provider_label=provider_label)
        except httpx.HTTPError as e:
            logger.error(f"Outlook apply_label failed for {email_id}: {e}")
            return ApplyLabelResult(success=False, error_message=str(e))
        except Exception as e:
            logger.error(f"Unexpected error in Outlook apply_label for {email_id}: {e}")
            return ApplyLabelResult(success=False, error_message=str(e))

    def get_current_label(self, email_id: str) -> Optional[str]:
        """Reconciliation-job support — reads the message's current Agent/ category."""
        if not self._authenticated or self._client is None:
            return None
        try:
            resp = self._client.get(f"/me/messages/{email_id}", params={"$select": "categories"})
            resp.raise_for_status()
            for cat in resp.json().get("categories", []):
                if cat.startswith(f"{CATEGORY_NAMESPACE_PREFIX}/"):
                    return cat
            return None
        except Exception as e:
            logger.warning(f"get_current_label failed for {email_id}: {e}")
            return None
