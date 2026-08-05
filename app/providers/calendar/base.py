from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_serializer
from app.agents.models.enums import MeetingStatus, CalendarProviderEnum


class MeetingCard(BaseModel):
    """
    Represents a detected meeting invitation.

    Serves two purposes:
    1. Display — rendered on the dashboard for the employee to review
    2. Write   — passed to CalendarProvider.add_event() to create the calendar event

    Created by the detect_meeting tool during a batch run.
    Status transitions: Pending → Added (user confirms) or Dismissed (user dismisses)
    """
    model_config = ConfigDict()
    meeting_id: str
    email_id: str
    user_id: str
    meeting_title: str
    meeting_datetime: datetime
    duration_minutes: int = 60
    organizer_name: Optional[str] = None
    organizer_email: Optional[str] = None
    attendees: list[str] = []
    location_or_link: Optional[str] = None
    meeting_summary: Optional[str] = None
    status: MeetingStatus = MeetingStatus.PENDING
    calendar_provider: CalendarProviderEnum
    created_at: datetime = datetime.now(timezone.utc)

    # Content-purge lifecycle (specs v3 §6 / pipeline_changes §3): content
    # fields (title/datetime/attendees/location/summary) are only ever
    # populated while status == Pending. On confirm/dismiss, the
    # meeting_purge_job nulls them out and stamps the fields below,
    # leaving a content-free audit stub.
    resolution: Optional[str] = None          # "added" | "dismissed"
    resolved_at: Optional[datetime] = None
    calendar_event_id: Optional[str] = None

    @field_serializer("meeting_datetime", "created_at", "resolved_at")
    def serialize_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class CalendarEventResult(BaseModel):
    """
    Result returned by CalendarProvider.add_event().

    Always returned — never raises an exception.
    Failures are expressed as success=False with an error_message.
    This allows the orchestrator to handle calendar failures gracefully
    without crashing the pipeline.

    On success: success=True, event_id and event_link are populated
    On failure: success=False, error_message describes what went wrong
    """
    success: bool
    event_id: Optional[str] = None
    event_link: Optional[str] = None
    error_message: Optional[str] = None


class CalendarProvider(ABC):
    """
    Abstract base class for all calendar providers.

    Any provider (Google Calendar, Outlook Calendar) must implement
    both methods below. The pipeline never imports GoogleCalendarProvider
    or OutlookCalendarProvider directly — it always works through this interface.

    CRITICAL: add_event() must NEVER be called autonomously by the agent.
    It is only invoked after explicit user confirmation from the dashboard.
    This is enforced at the API endpoint level (GH-014).

    To add a new calendar provider:
    1. Create a new file under app/providers/calendar/
    2. Subclass CalendarProvider
    3. Implement authenticate() and add_event()
    4. No other file in the codebase needs to change
    """

    @abstractmethod
    def authenticate(self) -> bool:
        """
        Authenticate with the calendar provider.
        Returns True if authentication succeeded, False otherwise.
        Must be called before add_event().
        """
        pass

    @abstractmethod
    def add_event(self, meeting_card: MeetingCard) -> CalendarEventResult:
        """
        Add a meeting event to the user's calendar.

        ONLY called after explicit user confirmation from the dashboard.
        Never called autonomously by the batch pipeline.

        Args:
            meeting_card: Full meeting details including title, datetime,
                         attendees, and location. Contains everything needed
                         to create the calendar event.

        Returns:
            CalendarEventResult with success status, event ID, and link.
            Never raises — all failures are returned as CalendarEventResult(success=False).
        """
        pass