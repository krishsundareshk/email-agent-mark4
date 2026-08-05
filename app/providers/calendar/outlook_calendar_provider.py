import logging
from datetime import timedelta, timezone
from typing import Optional

import httpx

from app.providers.calendar.base import (
    CalendarProvider,
    MeetingCard,
    CalendarEventResult,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class OutlookCalendarProvider(CalendarProvider):
    """
    Outlook / Teams Calendar implementation of CalendarProvider via
    Microsoft Graph (specs v3 §9.2 — the second and final CalendarProvider).

    Same stateless-token model as OutlookProvider (app/providers/email/
    outlook_provider.py) — token lifecycle belongs to the auth layer.

    CRITICAL: add_event() is NEVER called autonomously by the agent. It
    is only invoked via POST /api/meetings/{id}/confirm, unchanged in
    principle from the very first design pass (specs v3 §7, §10).
    """

    def __init__(self, access_token: Optional[str] = None):
        self._access_token = access_token
        self._client: Optional[httpx.Client] = None

    def authenticate(self) -> bool:
        if not self._access_token:
            logger.error("OutlookCalendarProvider: no access token supplied.")
            return False
        self._client = httpx.Client(
            base_url=GRAPH_BASE_URL,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )
        try:
            resp = self._client.get("/me/calendar")
            resp.raise_for_status()
            logger.info("Outlook Calendar authentication successful.")
            return True
        except Exception as e:
            logger.error(f"Outlook Calendar authentication failed: {e}")
            return False

    def add_event(self, meeting_card: MeetingCard) -> CalendarEventResult:
        if self._client is None:
            return CalendarEventResult(
                success=False,
                error_message="Calendar service not initialized. Call authenticate() first.",
            )

        try:
            event_body = self._build_event_body(meeting_card)
            resp = self._client.post("/me/events", json=event_body)
            resp.raise_for_status()
            created = resp.json()

            logger.info(
                f"Outlook calendar event created: '{meeting_card.meeting_title}' "
                f"— event_id: {created.get('id')}"
            )

            return CalendarEventResult(
                success=True,
                event_id=created.get("id"),
                event_link=created.get("webLink"),
            )
        except httpx.HTTPError as e:
            error_message = f"Microsoft Graph calendar error: {e}"
            logger.error(
                f"Failed to create Outlook calendar event for "
                f"'{meeting_card.meeting_title}': {error_message}"
            )
            return CalendarEventResult(success=False, error_message=error_message)
        except Exception as e:
            error_message = f"Unexpected error creating Outlook calendar event: {e}"
            logger.error(error_message)
            return CalendarEventResult(success=False, error_message=error_message)

    def _build_event_body(self, meeting_card: MeetingCard) -> dict:
        start_dt = meeting_card.meeting_datetime
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(minutes=meeting_card.duration_minutes)

        event_body = {
            "subject": meeting_card.meeting_title,
            "body": {
                "contentType": "text",
                "content": meeting_card.meeting_summary or "",
            },
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
        }

        if meeting_card.location_or_link:
            event_body["location"] = {"displayName": meeting_card.location_or_link}

        if meeting_card.attendees:
            event_body["attendees"] = [
                {
                    "emailAddress": {"address": email},
                    "type": "required",
                }
                for email in meeting_card.attendees
            ]

        return event_body
