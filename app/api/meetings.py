import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.providers.calendar.base import MeetingCard, CalendarEventResult, CalendarProvider
from app.providers.calendar import get_calendar_provider as _resolve_calendar_provider
from app.db.database import get_db
from app.db.models import MeetingCardModel, UserModel
from app.agents.models.enums import MeetingStatus, CalendarProviderEnum
from app.agents.tools.write_to_data_store import update_meeting_status, resolve_and_purge_meeting
from datetime import datetime, timezone
from app.api.auth import get_current_user_id, get_provider_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache (optional — DB is source of truth)
# Populated during batch runs and on-demand when loading from DB.
# ─────────────────────────────────────────────────────────────────────────────

_meeting_store: dict[str, MeetingCard] = {}


def get_meeting_store() -> dict[str, MeetingCard]:
    """Returns the in-memory meeting cache."""
    return _meeting_store


def register_meeting(meeting_card: MeetingCard) -> None:
    """Cache a MeetingCard in memory after batch detection or DB load."""
    _meeting_store[meeting_card.meeting_id] = meeting_card
    logger.info(f"Meeting registered: {meeting_card.meeting_id}")


def _meeting_card_from_db(record: MeetingCardModel) -> MeetingCard:
    """Convert a MeetingCardModel ORM row to a MeetingCard Pydantic model."""
    status = record.status
    if not isinstance(status, MeetingStatus):
        status = MeetingStatus(status)

    calendar_provider = record.calendar_provider
    if not isinstance(calendar_provider, CalendarProviderEnum):
        calendar_provider = CalendarProviderEnum(calendar_provider)

    return MeetingCard(
        meeting_id=record.meeting_id,
        email_id=record.email_id,
        user_id=record.user_id,
        meeting_title=record.meeting_title,
        meeting_datetime=record.meeting_datetime,
        duration_minutes=record.duration_minutes,
        organizer_name=record.organizer_name,
        organizer_email=record.organizer_email,
        attendees=json.loads(record.attendees) if record.attendees else [],
        location_or_link=record.location_or_link,
        meeting_summary=record.meeting_summary,
        status=status,
        calendar_provider=calendar_provider,
        created_at=record.created_at,
    )


def _get_meeting_card(
    meeting_id: str,
    store: dict[str, MeetingCard],
    db: Session,
) -> Optional[MeetingCard]:
    """
    Resolve a MeetingCard from the in-memory cache or database.
    GET /pending reads from DB, so confirm/dismiss must also work after restart.
    """
    if meeting_id in store:
        return store[meeting_id]

    record = db.query(MeetingCardModel).filter_by(meeting_id=meeting_id).first()
    if not record:
        return None

    meeting_card = _meeting_card_from_db(record)
    store[meeting_id] = meeting_card
    return meeting_card


# ─────────────────────────────────────────────────────────────────────────────
# Calendar provider dependency
# ─────────────────────────────────────────────────────────────────────────────

def get_calendar_provider(
    user_id: str = Depends(get_current_user_id),
    access_token: Optional[str] = Depends(get_provider_access_token),
    db: Session = Depends(get_db),
) -> CalendarProvider:
    """
    FastAPI dependency — provides an authenticated CalendarProvider,
    resolved to whichever of the two supported providers (Google Calendar
    or Outlook/Teams Calendar) this user is configured for (specs v3 §9.2).
    """
    user = db.query(UserModel).filter_by(id=user_id).first()
    provider_enum = CalendarProviderEnum(
        user.calendar_provider if user and isinstance(user.calendar_provider, str)
        else (user.calendar_provider.value if user else CalendarProviderEnum.GOOGLE.value)
    )
    provider = _resolve_calendar_provider(provider_enum, access_token=access_token)
    authenticated = provider.authenticate()
    if not authenticated:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Calendar service unavailable ({provider_enum.value}). "
                f"OAuth authentication failed. Check token or sign in again."
            )
        )
    return provider


# ─────────────────────────────────────────────────────────────────────────────
# Response Models
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmResponse(BaseModel):
    """Response returned by POST /api/meetings/{meeting_id}/confirm."""
    success: bool
    meeting_id: str
    event_id: Optional[str] = None
    event_link: Optional[str] = None
    error_message: Optional[str] = None


class DismissResponse(BaseModel):
    """Response returned by POST /api/meetings/{meeting_id}/dismiss."""
    success: bool
    meeting_id: str
    error_message: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# GET Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pending")
def get_pending_meetings(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Return all pending MeetingCards for the current user.
    Queried from the DB — source of truth for dashboard meeting section.
    """
    records = (
        db.query(MeetingCardModel)
        .filter_by(user_id=user_id, status=MeetingStatus.PENDING)
        .order_by(MeetingCardModel.created_at.desc())
        .all()
    )
    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_title": r.meeting_title,
            "meeting_datetime": r.meeting_datetime.isoformat() if r.meeting_datetime else None,
            "duration_minutes": r.duration_minutes,
            "organizer_name": r.organizer_name,
            "organizer_email": r.organizer_email,
            "attendees": json.loads(r.attendees) if r.attendees else [],
            "location_or_link": r.location_or_link,
            "meeting_summary": r.meeting_summary,
            "status": r.status.value if hasattr(r.status, "value") else r.status,
        }
        for r in records
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/confirm", response_model=ConfirmResponse)
def confirm_meeting(
    meeting_id: str,
    user_id: str = Depends(get_current_user_id),
    store: dict = Depends(get_meeting_store),
    calendar_provider: CalendarProvider = Depends(get_calendar_provider),
    db: Session = Depends(get_db),
) -> ConfirmResponse:
    """
    Add a meeting to the user's Google Calendar.

    ONLY reachable via explicit user action on the dashboard.
    Never called autonomously by the batch pipeline.

    Validates:
    - Meeting exists in the store
    - Meeting status is Pending (idempotency guard)

    On success: updates status to Added in both the in-memory store and
    the DB, then returns event_id and event_link.
    On failure: returns success=False with error_message — does not raise.
    """
    meeting_card = _get_meeting_card(meeting_id, store, db)
    if not meeting_card:
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {meeting_id} not found."
        )

    if meeting_card.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this meeting."
        )

    if meeting_card.status == MeetingStatus.ADDED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Meeting {meeting_id} has already been added to calendar. "
                f"Check your calendar to avoid duplicates."
            )
        )

    if meeting_card.status == MeetingStatus.DISMISSED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Meeting {meeting_id} was previously dismissed. "
                f"Cannot add a dismissed meeting to calendar."
            )
        )

    result: CalendarEventResult = calendar_provider.add_event(meeting_card)

    if result.success:
        # Update in-memory store
        meeting_card.status = MeetingStatus.ADDED
        store[meeting_id] = meeting_card

        # Persist status + resolve + purge content fields (specs v3 §6) —
        # this MeetingCard now becomes a content-free audit stub in the DB.
        db_updated = resolve_and_purge_meeting(
            meeting_id, resolution="added", db=db, calendar_event_id=result.event_id,
        )
        if not db_updated:
            logger.warning(
                f"Meeting {meeting_id} confirmed in calendar but DB status "
                f"update failed — status may revert after restart."
            )

        logger.info(
            f"Meeting {meeting_id} confirmed and added to calendar. "
            f"event_id: {result.event_id}"
        )
        store.pop(meeting_id, None)

        return ConfirmResponse(
            success=True,
            meeting_id=meeting_id,
            event_id=result.event_id,
            event_link=result.event_link,
        )

    else:
        logger.error(
            f"Failed to add meeting {meeting_id} to calendar: "
            f"{result.error_message}"
        )

        return ConfirmResponse(
            success=False,
            meeting_id=meeting_id,
            error_message=result.error_message,
        )


@router.post("/{meeting_id}/dismiss", response_model=DismissResponse)
def dismiss_meeting(
    meeting_id: str,
    user_id: str = Depends(get_current_user_id),
    store: dict = Depends(get_meeting_store),
    db: Session = Depends(get_db),
) -> DismissResponse:
    """
    Dismiss a meeting invitation — no calendar action taken.

    Updates status to Dismissed in both the in-memory store and the DB
    so the dismissal survives a server restart.
    """
    meeting_card = _get_meeting_card(meeting_id, store, db)
    if not meeting_card:
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {meeting_id} not found."
        )

    if meeting_card.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this meeting."
        )

    if meeting_card.status == MeetingStatus.DISMISSED:
        raise HTTPException(
            status_code=409,
            detail=f"Meeting {meeting_id} has already been dismissed."
        )

    if meeting_card.status == MeetingStatus.ADDED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Meeting {meeting_id} has already been added to calendar. "
                f"Cannot dismiss an already-confirmed meeting."
            )
        )

    # Update in-memory store
    meeting_card.status = MeetingStatus.DISMISSED
    store[meeting_id] = meeting_card

    # Persist status + resolve + purge content fields (specs v3 §6)
    db_updated = resolve_and_purge_meeting(meeting_id, resolution="dismissed", db=db)
    if not db_updated:
        logger.warning(
            f"Meeting {meeting_id} dismissed in memory but DB status "
            f"update failed — dismissal may revert after restart."
        )

    logger.info(f"Meeting {meeting_id} dismissed.")
    store.pop(meeting_id, None)

    return DismissResponse(
        success=True,
        meeting_id=meeting_id,
    )