import json
import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import (
    ClassifiedEmailModel,
    MeetingCardModel,
    BatchRunLogModel,
    ProcessedEmailModel,
)
from app.agents.models.classified_email import ClassifiedEmail
from app.providers.calendar.base import MeetingCard
from app.agents.models.batch_run_log import BatchRunLog
from app.agents.models.enums import MeetingStatus, BatchRunStatus

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Write Functions
# ─────────────────────────────────────────────────────────────────────────────

def save_classified_email(
    classified_email: ClassifiedEmail,
    db: Session,
) -> bool:
    """
    Persist a ClassifiedEmail Pydantic model to the classified_emails table.

    No summary/subject/priority (pipeline_changes §3) — structured facts
    only. relationship + department + is_meeting + confidence_tier is the
    full classification surface now.
    """
    try:
        orm_record = ClassifiedEmailModel(
            email_id=classified_email.email_id,
            user_id=classified_email.user_id,
            thread_id=classified_email.thread_id,
            relationship=classified_email.relationship,
            department=classified_email.department,
            is_meeting=classified_email.is_meeting,
            confidence_tier=classified_email.confidence_tier,
            self_reported_certainty=classified_email.self_reported_certainty,
            reflection_agreement=classified_email.reflection_agreement,
            processed_at=classified_email.processed_at,
            sender_name=classified_email.sender_name,
            sender_email=classified_email.sender_email,
        )
        db.add(orm_record)
        db.commit()
        logger.debug(
            f"Saved ClassifiedEmail: email_id={classified_email.email_id}, "
            f"relationship={classified_email.relationship}, "
            f"tier={classified_email.confidence_tier}"
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to save ClassifiedEmail for email_id="
            f"{classified_email.email_id}: {e}"
        )
        return False


def save_meeting_card(
    meeting_card: MeetingCard,
    db: Session,
) -> bool:
    """
    Persist a MeetingCard Pydantic model to the meeting_cards table.

    Content fields are populated while status==Pending. See
    app/jobs/meeting_purge_job.py for the post-resolution purge.
    """
    try:
        orm_record = MeetingCardModel(
            meeting_id=meeting_card.meeting_id,
            email_id=meeting_card.email_id,
            user_id=meeting_card.user_id,
            meeting_title=meeting_card.meeting_title,
            meeting_datetime=meeting_card.meeting_datetime,
            duration_minutes=meeting_card.duration_minutes,
            organizer_name=meeting_card.organizer_name,
            organizer_email=meeting_card.organizer_email,
            attendees=json.dumps(meeting_card.attendees),  # list → JSON string
            location_or_link=meeting_card.location_or_link,
            meeting_summary=meeting_card.meeting_summary,
            status=meeting_card.status,
            calendar_provider=meeting_card.calendar_provider,
            created_at=meeting_card.created_at,
        )
        db.add(orm_record)
        db.commit()
        logger.info(
            f"Saved MeetingCard: meeting_id={meeting_card.meeting_id}, "
            f"title={meeting_card.meeting_title}"
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to save MeetingCard for meeting_id="
            f"{meeting_card.meeting_id}: {e}"
        )
        return False


def save_batch_run_log(
    batch_run_log: BatchRunLog,
    db: Session,
) -> bool:
    """Persist a BatchRunLog Pydantic model to the batch_run_logs table (Running)."""
    try:
        orm_record = BatchRunLogModel(
            run_id=batch_run_log.run_id,
            user_id=batch_run_log.user_id,
            started_at=batch_run_log.started_at,
            completed_at=batch_run_log.completed_at,
            status=batch_run_log.status,
            emails_fetched=batch_run_log.emails_fetched,
            emails_classified=batch_run_log.emails_classified,
            emails_failed=batch_run_log.emails_failed,
            emails_deferred=batch_run_log.emails_deferred,
            meetings_detected=batch_run_log.meetings_detected,
            stage0_resolved=getattr(batch_run_log, "stage0_resolved", 0),
            error_message=batch_run_log.error_message,
        )
        db.add(orm_record)
        db.commit()
        logger.info(
            f"Saved BatchRunLog: run_id={batch_run_log.run_id}, "
            f"status={batch_run_log.status}"
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to save BatchRunLog for run_id="
            f"{batch_run_log.run_id}: {e}"
        )
        return False


def mark_email_processed(
    email_id: str,
    user_id: str,
    db: Session,
) -> bool:
    """Record that an email has been processed — persists to processed_emails table."""
    try:
        existing = db.query(ProcessedEmailModel).filter_by(
            email_id=email_id,
            user_id=user_id
        ).first()

        if existing:
            logger.debug(
                f"Email already marked as processed: "
                f"email_id={email_id}, user_id={user_id}"
            )
            return True

        orm_record = ProcessedEmailModel(
            email_id=email_id,
            user_id=user_id,
        )
        db.add(orm_record)
        db.commit()
        logger.debug(f"Marked email processed: email_id={email_id}")
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to mark email processed: "
            f"email_id={email_id}, error={e}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Read Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_processed_email_ids(
    user_id: str,
    db: Session,
) -> set[str]:
    """Return all email IDs already processed for a given user."""
    try:
        records = db.query(ProcessedEmailModel.email_id).filter_by(
            user_id=user_id
        ).all()
        ids = {record.email_id for record in records}
        logger.debug(
            f"Retrieved {len(ids)} processed email IDs for user {user_id}"
        )
        return ids

    except Exception as e:
        logger.error(
            f"Failed to get processed email IDs for user {user_id}: {e}"
        )
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# Update Functions
# ─────────────────────────────────────────────────────────────────────────────

def update_meeting_status(
    meeting_id: str,
    status: MeetingStatus,
    db: Session,
) -> bool:
    """Update the status of a MeetingCard (used mid-flight; purge happens separately)."""
    try:
        record = db.query(MeetingCardModel).filter_by(
            meeting_id=meeting_id
        ).first()

        if not record:
            logger.warning(
                f"Cannot update status — meeting not found: {meeting_id}"
            )
            return False

        record.status = status
        db.commit()
        logger.info(
            f"Updated meeting status: meeting_id={meeting_id}, "
            f"new_status={status}"
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to update meeting status for {meeting_id}: {e}"
        )
        return False


def resolve_and_purge_meeting(
    meeting_id: str,
    resolution: str,  # "added" | "dismissed"
    db: Session,
    calendar_event_id: Optional[str] = None,
) -> bool:
    """
    Resolve a MeetingCard (confirm/dismiss) and null out its content fields
    (specs v3 §6 / pipeline_changes §3), leaving a content-free audit stub:
    meeting_id, email_id, user_id, status, resolution, resolved_at,
    calendar_provider, calendar_event_id survive; everything else is purged.

    Called synchronously by POST /api/meetings/{id}/confirm|dismiss — no
    need to wait for the scheduled meeting_purge_job when the resolution
    is already known at request time. meeting_purge_job exists as a
    safety net for any Pending card that ages out without resolution.
    """
    try:
        record = db.query(MeetingCardModel).filter_by(meeting_id=meeting_id).first()
        if not record:
            logger.warning(f"Cannot resolve — meeting not found: {meeting_id}")
            return False

        record.status = MeetingStatus.ADDED if resolution == "added" else MeetingStatus.DISMISSED
        record.resolution = resolution
        record.resolved_at = datetime.now(timezone.utc)
        record.calendar_event_id = calendar_event_id

        # Content purge — this is the part of the row allowed to disappear.
        record.meeting_title = None
        record.meeting_datetime = None
        record.organizer_name = None
        record.organizer_email = None
        record.attendees = "[]"
        record.location_or_link = None
        record.meeting_summary = None

        db.commit()
        logger.info(f"Resolved + purged MeetingCard {meeting_id} -> {resolution}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to resolve/purge meeting {meeting_id}: {e}")
        return False


def update_batch_run_log(
    run_id: str,
    db: Session,
    status: Optional[BatchRunStatus] = None,
    emails_fetched: Optional[int] = None,
    emails_classified: Optional[int] = None,
    emails_failed: Optional[int] = None,
    emails_deferred: Optional[int] = None,
    meetings_detected: Optional[int] = None,
    stage0_resolved: Optional[int] = None,
    completed_at=None,
    error_message: Optional[str] = None,
) -> bool:
    """Update an existing BatchRunLog record at the end of a batch run."""
    try:
        record = db.query(BatchRunLogModel).filter_by(run_id=run_id).first()

        if not record:
            logger.warning(
                f"Cannot update — BatchRunLog not found: run_id={run_id}"
            )
            return False

        if status is not None:
            record.status = status
        if emails_fetched is not None:
            record.emails_fetched = emails_fetched
        if emails_classified is not None:
            record.emails_classified = emails_classified
        if emails_failed is not None:
            record.emails_failed = emails_failed
        if emails_deferred is not None:
            record.emails_deferred = emails_deferred
        if meetings_detected is not None:
            record.meetings_detected = meetings_detected
        if stage0_resolved is not None:
            record.stage0_resolved = stage0_resolved
        if completed_at is not None:
            record.completed_at = completed_at
        if error_message is not None:
            record.error_message = error_message

        db.commit()
        logger.info(
            f"Updated BatchRunLog: run_id={run_id}, status={record.status}"
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update BatchRunLog {run_id}: {e}")
        return False
