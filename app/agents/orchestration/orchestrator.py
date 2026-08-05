import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import UserModel
from app.providers.email import get_email_provider
from app.agents.models.enums import BatchRunStatus, CalendarProviderEnum, EmailProviderEnum
from app.agents.models.batch_run_log import BatchRunLog
from app.agents.tools.llm_client import LLMClient
from app.agents.tools.stage0_bulk_filter import run_stage0_filter
from app.agents.tools.reasoning_engine import run_reasoning_engine
from app.agents.tools.detect_meeting import detect_meeting
from app.agents.tools.apply_label_tool import apply_label
from app.agents.embedding.embedding_adapter import EmbeddingAdapter
from app.agents.tools.write_to_data_store import (
    save_classified_email,
    save_meeting_card,
    save_batch_run_log,
    mark_email_processed,
    get_processed_email_ids,
    update_batch_run_log,
)
from app.api.meetings import register_meeting

logger = logging.getLogger(__name__)

# Maximum emails processed per user per batch run
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "500"))


def run_batch_for_user(
    user_id: str,
    access_token: Optional[str] = None,
    ollama_model: Optional[str] = None,
) -> BatchRunLog:
    """
    Run the full email processing pipeline for a single user (specs v3 §2-§4).

    Stage 0 (deterministic bulk-mail filter, no LLM) short-circuits
    Promotional emails straight to apply_label. Everything else goes
    through the reasoning_engine (Stage 1 — Observe/Reason/Act/Reflect/
    Finalize, owns memory + apply_label tool-calling), then conditionally
    Stage 2 (detect_meeting) if is_meeting was flagged.

    Stateless — no memory carried across process restarts; all memory
    lives in the DB (sender_memory / thread_memory), not in this function.

    Always returns — never raises. On critical failure, returns a
    BatchRunLog with status=Failed.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    logger.info(f"[run_id={run_id}] Starting batch run for user_id={user_id}")

    db = SessionLocal()

    initial_log = BatchRunLog(
        run_id=run_id, user_id=user_id, started_at=started_at, status=BatchRunStatus.RUNNING,
    )
    save_batch_run_log(initial_log, db)

    try:
        return _run_pipeline(
            run_id=run_id, user_id=user_id, started_at=started_at, db=db,
            access_token=access_token, ollama_model=ollama_model,
        )
    except Exception as e:
        logger.error(f"[run_id={run_id}] Unhandled exception in batch run: {e}")
        _finalize_run(run_id=run_id, db=db, status=BatchRunStatus.FAILED, error_message=str(e))
        return BatchRunLog(
            run_id=run_id, user_id=user_id, started_at=started_at,
            status=BatchRunStatus.FAILED, error_message=str(e),
        )
    finally:
        db.close()


def _run_pipeline(
    run_id: str, user_id: str, started_at: datetime, db: Session,
    access_token: Optional[str] = None, ollama_model: Optional[str] = None,
) -> BatchRunLog:
    """Core pipeline logic — separated so the outer function can catch unhandled exceptions cleanly."""

    # ── Step 1: Load user from DB ───────────────────────────────────────
    user = db.query(UserModel).filter_by(id=user_id).first()
    if not user:
        logger.error(f"[run_id={run_id}] User not found: {user_id}")
        _finalize_run(run_id=run_id, db=db, status=BatchRunStatus.FAILED,
                       error_message=f"User not found: {user_id}")
        return _make_log(run_id, user_id, started_at, BatchRunStatus.FAILED,
                          error_message=f"User not found: {user_id}")

    calendar_provider_enum = CalendarProviderEnum(
        user.calendar_provider if isinstance(user.calendar_provider, str)
        else user.calendar_provider.value
    )
    email_provider_enum = EmailProviderEnum(
        user.email_provider if isinstance(user.email_provider, str)
        else user.email_provider.value
    )
    org_domains = [d for d in (user.org_domains or "").split(",") if d.strip()]

    # ── Step 2: Authenticate email provider (Gmail or Outlook) ─────────
    email_provider = get_email_provider(email_provider_enum, access_token=access_token)
    if not email_provider.authenticate():
        logger.error(f"[run_id={run_id}] {email_provider_enum.value} authentication failed for user {user_id}.")
        msg = f"{email_provider_enum.value.title()} authentication failed. Please sign in again to refresh your session."
        _finalize_run(run_id=run_id, db=db, status=BatchRunStatus.FAILED, error_message=msg)
        return _make_log(run_id, user_id, started_at, BatchRunStatus.FAILED, error_message=msg)

    # ── Step 3: Load processed email IDs from DB (deduplication) ───────
    processed_ids = get_processed_email_ids(user_id, db)
    logger.info(f"[run_id={run_id}] {len(processed_ids)} emails already processed.")

    # ── Step 4: Fetch emails ─────────────────────────────────────────────
    batch_until = datetime.now(timezone.utc)
    from datetime import time
    local_now = datetime.now()
    local_midnight_today = datetime.combine(local_now.date(), time.min).astimezone(timezone.utc)

    if not processed_ids:
        signup_local = user.created_at.astimezone()
        signup_midnight = datetime.combine(signup_local.date(), time.min).astimezone(timezone.utc)
        batch_since = signup_midnight
        logger.info(f"[run_id={run_id}] First sync — fetching since signup day ({signup_local.date()}): {batch_since}")
    else:
        batch_since = local_midnight_today
        logger.info(f"[run_id={run_id}] Fetching since today's local midnight: {batch_since}")

    try:
        from app.providers.email.base import EmailFetchError
        raw_emails = email_provider.fetch_emails(since=batch_since, until=batch_until)
    except EmailFetchError as e:
        logger.error(f"[run_id={run_id}] Email fetch failed: {e}")
        _finalize_run(run_id=run_id, db=db, status=BatchRunStatus.FAILED, error_message=f"Email fetch failed: {e}")
        return _make_log(run_id, user_id, started_at, BatchRunStatus.FAILED, error_message=f"Email fetch failed: {e}")

    new_emails = [e for e in raw_emails if e.email_id not in processed_ids]
    logger.info(f"[run_id={run_id}] Fetched {len(raw_emails)} emails, {len(new_emails)} new.")

    emails_deferred = 0
    emails_fetched = len(new_emails)

    # ── Step 5: Process each email ──────────────────────────────────────
    llm_client = LLMClient(model=ollama_model)
    embedding_adapter = EmbeddingAdapter()

    emails_classified = 0
    emails_failed = 0
    meetings_detected = 0
    stage0_resolved = 0

    for email in new_emails:
        try:
            # ── Stage 0: deterministic bulk-mail filter, no LLM ────────
            stage0_result = run_stage0_filter(email)

            if stage0_result.is_promotional:
                stage0_resolved += 1
                apply_label(
                    email_id=email.email_id, user_id=user_id, label="Promotional",
                    email_provider=email_provider, db=db,
                )
                from app.agents.models.classified_email import ClassifiedEmail
                from app.agents.models.enums import RelationshipLabel, Department, ConfidenceTier
                classified = ClassifiedEmail(
                    email_id=email.email_id, user_id=user_id, thread_id=email.thread_id,
                    relationship=RelationshipLabel.PROMOTIONAL, department=Department.GENERAL,
                    is_meeting=False, confidence_tier=ConfidenceTier.AUTO_APPLIED,
                    self_reported_certainty=1.0,
                    sender_name=email.sender_name, sender_email=email.sender_email,
                )
                saved = save_classified_email(classified, db)
                if not saved:
                    emails_failed += 1
                    continue
                mark_email_processed(email.email_id, user_id, db)
                emails_classified += 1
                continue  # Promotional never reaches Stage 1/2.

            # ── Stage 1: reasoning_engine (Observe/Reason/Act/Reflect/Finalize) ──
            reasoning_result = run_reasoning_engine(
                email=email, user_id=user_id, org_domains=org_domains,
                stage0_signal_summary=stage0_result.signal_summary,
                email_provider=email_provider, db=db,
                llm_client=llm_client, llm_model=ollama_model,
                embedding_adapter=embedding_adapter,
            )
            classified = reasoning_result.classified_email

            # ── Stage 2: detect_meeting, conditionally triggered ────────
            if classified.is_meeting:
                meeting_result = detect_meeting(
                    email=email, user_id=user_id, calendar_provider=calendar_provider_enum,
                    llm_client=llm_client, llm_model=ollama_model,
                )
                if meeting_result.is_meeting and meeting_result.meeting_card:
                    saved_meeting = save_meeting_card(meeting_result.meeting_card, db)
                    if saved_meeting:
                        register_meeting(meeting_result.meeting_card)
                        meetings_detected += 1
                        logger.info(
                            f"[run_id={run_id}] Meeting detected in email {email.email_id}: "
                            f"'{meeting_result.meeting_card.meeting_title}'"
                        )
                    else:
                        logger.warning(f"[run_id={run_id}] Meeting detected but failed to save for {email.email_id}.")
                else:
                    # Stage 1 flagged is_meeting but Stage 2's stricter
                    # extraction couldn't produce a full card — keep the
                    # ClassifiedEmail as-is (is_meeting stays true; no
                    # summary field exists anymore to append a note to).
                    logger.info(
                        f"[run_id={run_id}] Stage 1 flagged meeting for {email.email_id} "
                        f"but Stage 2 extraction was inconclusive."
                    )

            # ── Persist ClassifiedEmail ──────────────────────────────────
            saved = save_classified_email(classified, db)
            if not saved:
                emails_failed += 1
                logger.warning(f"[run_id={run_id}] Failed to save ClassifiedEmail for {email.email_id}.")
                continue

            mark_email_processed(email.email_id, user_id, db)
            emails_classified += 1

        except Exception as e:
            emails_failed += 1
            logger.error(f"[run_id={run_id}] Unexpected error processing email {email.email_id}: {e}. Skipping.")
            continue

    # ── Step 6: Finalize BatchRunLog ────────────────────────────────────
    final_status = BatchRunStatus.SUCCESS if emails_failed == 0 else BatchRunStatus.PARTIAL_FAILURE
    completed_at = datetime.now(timezone.utc)

    _finalize_run(
        run_id=run_id, db=db, status=final_status, emails_fetched=emails_fetched,
        emails_classified=emails_classified, emails_failed=emails_failed,
        emails_deferred=emails_deferred, meetings_detected=meetings_detected,
        stage0_resolved=stage0_resolved, completed_at=completed_at,
    )

    logger.info(
        f"[run_id={run_id}] Batch run complete. status={final_status}, fetched={emails_fetched}, "
        f"classified={emails_classified}, failed={emails_failed}, meetings={meetings_detected}, "
        f"stage0_resolved={stage0_resolved}, deferred={emails_deferred}"
    )

    _signal_dashboard_refresh(user_id, run_id)

    return _make_log(
        run_id=run_id, user_id=user_id, started_at=started_at, status=final_status,
        emails_fetched=emails_fetched, emails_classified=emails_classified,
        emails_failed=emails_failed, emails_deferred=emails_deferred,
        meetings_detected=meetings_detected, stage0_resolved=stage0_resolved,
        completed_at=completed_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Private Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _finalize_run(
    run_id: str, db: Session, status: BatchRunStatus, emails_fetched: int = 0,
    emails_classified: int = 0, emails_failed: int = 0, emails_deferred: int = 0,
    meetings_detected: int = 0, stage0_resolved: int = 0,
    completed_at: Optional[datetime] = None, error_message: Optional[str] = None,
) -> None:
    if completed_at is None:
        completed_at = datetime.now(timezone.utc)

    update_batch_run_log(
        run_id=run_id, db=db, status=status, emails_fetched=emails_fetched,
        emails_classified=emails_classified, emails_failed=emails_failed,
        emails_deferred=emails_deferred, meetings_detected=meetings_detected,
        stage0_resolved=stage0_resolved, completed_at=completed_at, error_message=error_message,
    )


def _make_log(
    run_id: str, user_id: str, started_at: datetime, status: BatchRunStatus,
    emails_fetched: int = 0, emails_classified: int = 0, emails_failed: int = 0,
    emails_deferred: int = 0, meetings_detected: int = 0, stage0_resolved: int = 0,
    completed_at: Optional[datetime] = None, error_message: Optional[str] = None,
) -> BatchRunLog:
    return BatchRunLog(
        run_id=run_id, user_id=user_id, started_at=started_at,
        completed_at=completed_at or datetime.now(timezone.utc), status=status,
        emails_fetched=emails_fetched, emails_classified=emails_classified,
        emails_failed=emails_failed, emails_deferred=emails_deferred,
        meetings_detected=meetings_detected, stage0_resolved=stage0_resolved,
        error_message=error_message,
    )


def _signal_dashboard_refresh(user_id: str, run_id: str) -> None:
    """
    Notify the dashboard that a batch run is complete.
    v1 — the dashboard polls GET /api/batch/latest on load. No-op placeholder
    for a future push/event mechanism.
    """
    logger.info(f"[run_id={run_id}] Dashboard refresh signal sent for user {user_id}.")
