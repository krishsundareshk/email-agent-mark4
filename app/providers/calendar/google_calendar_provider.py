import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.providers.calendar.base import (
    CalendarProvider,
    MeetingCard,
    CalendarEventResult,
)

logger = logging.getLogger(__name__)

# Same scopes as GmailProvider — token file already has these granted
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


class GoogleCalendarProvider(CalendarProvider):
    """
    Google Calendar implementation of CalendarProvider.

    Reuses the same OAuth2 token file as GmailProvider.
    Since both Gmail and Calendar scopes were requested together
    in GH-005, no second consent screen is needed.

    CRITICAL: add_event() is NEVER called autonomously by the agent.
    It is only invoked via POST /api/meetings/{id}/confirm after
    explicit user confirmation on the dashboard (GH-014).

    Authentication flow:
    - Loads token from GOOGLE_TOKEN_PATH
    - Refreshes silently if expired
    - Falls back to consent flow if token missing (first run only)
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

    def authenticate(self) -> bool:
        """
        Authenticate with Google Calendar using OAuth2.

        Attempts in order:
        1. If access_token is provided, use it directly (stateless)
        2. Loads and reuses the token from GmailProvider's consent flow.
        3. If the token is expired, refreshes it silently.
        4. If no token exists, runs the browser consent flow.

        Returns:
            True if authentication succeeded.
            False on any failure.
        """
        try:
            if self._access_token:
                logger.info("GoogleCalendarProvider: Authenticating using request access token.")
                self._credentials = Credentials(token=self._access_token)
            else:
                self._credentials = self._load_or_refresh_credentials()

            if not self._credentials or not self._credentials.valid:
                logger.error(
                    "Google Calendar authentication failed — "
                    "credentials not valid."
                )
                return False

            self._service = build(
                serviceName="calendar",
                version="v3",
                credentials=self._credentials,
            )

            # Verify credentials by performing a lightweight API call.
            # This triggers refresh check early if the access token has expired.
            self._service.calendarList().list(maxResults=1).execute()

            logger.info("Google Calendar authentication successful.")
            return True

        except Exception as e:
            logger.error(
                f"Google Calendar authentication failed: {e}"
            )
            return False

    def _load_or_refresh_credentials(self) -> Optional[Credentials]:
        """
        Load token from disk, refresh if expired, run consent flow if missing.
        Saves updated credentials back to disk after any change.
        """
        credentials = None

        # Load cached token — shared with GmailProvider
        if os.path.exists(self._token_path):
            logger.info(
                f"Loading cached token from {self._token_path}"
            )
            credentials = Credentials.from_authorized_user_file(
                self._token_path, SCOPES
            )

        # Refresh silently if expired
        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Calendar token expired — refreshing silently.")
            try:
                credentials.refresh(Request())
                logger.info("Calendar token refreshed successfully.")
            except Exception as e:
                logger.warning(
                    f"Token refresh failed: {e}. Re-running consent flow."
                )
                credentials = None

        # Run consent flow if no valid credentials
        if not credentials or not credentials.valid:
            logger.info(
                "No valid credentials — starting OAuth2 consent flow."
            )
            if not os.path.exists(self._credentials_path):
                raise FileNotFoundError(
                    f"Credentials file not found at '{self._credentials_path}'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                self._credentials_path, SCOPES
            )
            credentials = flow.run_local_server(port=0)
            logger.info("OAuth2 consent flow completed.")

        # Save updated credentials
        try:
            with open(self._token_path, "w") as f:
                f.write(credentials.to_json())
        except Exception as e:
            logger.warning(f"Failed to save credentials: {e}")

        return credentials

    def add_event(self, meeting_card: MeetingCard) -> CalendarEventResult:
        """
        Add a meeting event to the user's Google Calendar.

        ONLY called after explicit user confirmation from the dashboard.
        Never called autonomously by the batch pipeline.

        Maps MeetingCard fields to Google Calendar event schema.
        End time computed as meeting_datetime + duration_minutes.

        Args:
            meeting_card: Full meeting details to write to calendar.

        Returns:
            CalendarEventResult(success=True, event_id, event_link) on success.
            CalendarEventResult(success=False, error_message) on any failure.
            Never raises.
        """
        if self._service is None:
            return CalendarEventResult(
                success=False,
                error_message=(
                    "Calendar service not initialized. "
                    "Call authenticate() before add_event()."
                )
            )

        try:
            event_body = self._build_event_body(meeting_card)

            created_event = self._service.events().insert(
                calendarId="primary",
                body=event_body,
            ).execute()

            event_id = created_event.get("id")
            event_link = created_event.get("htmlLink")

            logger.info(
                f"Calendar event created: '{meeting_card.meeting_title}' "
                f"— event_id: {event_id}"
            )

            return CalendarEventResult(
                success=True,
                event_id=event_id,
                event_link=event_link,
            )

        except HttpError as e:
            error_message = (
                f"Google Calendar API error {e.resp.status}: {e.error_details}"
            )
            logger.error(
                f"Failed to create calendar event for "
                f"'{meeting_card.meeting_title}': {error_message}"
            )
            return CalendarEventResult(
                success=False,
                error_message=error_message,
            )

        except Exception as e:
            error_message = f"Unexpected error creating calendar event: {e}"
            logger.error(error_message)
            return CalendarEventResult(
                success=False,
                error_message=error_message,
            )

    def _build_event_body(self, meeting_card: MeetingCard) -> dict:
        """
        Map MeetingCard fields to Google Calendar API event schema.

        Start time: meeting_card.meeting_datetime
        End time:   meeting_datetime + duration_minutes
        """
        start_dt = meeting_card.meeting_datetime

        # Ensure timezone-aware
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)

        end_dt = start_dt + timedelta(minutes=meeting_card.duration_minutes)

        event_body = {
            "summary": meeting_card.meeting_title,
            "description": meeting_card.meeting_summary,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "UTC",
            },
        }

        # Optional fields — only add if present
        if meeting_card.location_or_link:
            event_body["location"] = meeting_card.location_or_link

        if meeting_card.attendees:
            event_body["attendees"] = [
                {"email": email}
                for email in meeting_card.attendees
            ]

        if meeting_card.organizer_email:
            event_body["organizer"] = {
                "displayName": meeting_card.organizer_name or "",
                "email": meeting_card.organizer_email,
            }

        return event_body