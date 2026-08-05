"""
thread_memory_store — short-term memory, keyed by Gmail/Outlook thread ID
(specs v3 §5.2). Purged after 30 days of inactivity (see
app/jobs/thread_memory_expiry_job.py).
"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.db.models import ThreadMemoryModel
from app.agents.models.thread_memory import ThreadMemory, THREAD_MEMORY_EXPIRY_DAYS

logger = logging.getLogger(__name__)


def _to_pydantic(row: ThreadMemoryModel) -> ThreadMemory:
    return ThreadMemory(
        thread_id=row.thread_id,
        user_id=row.user_id,
        message_count=row.message_count,
        first_label=row.first_label,
        last_label=row.last_label,
        last_seen_at=row.last_seen_at,
    )


def get_thread_memory(thread_id: str, user_id: str, db: Session) -> ThreadMemory:
    """Tool: get_thread_memory(thread_id) — returns short-term memory."""
    if not thread_id:
        return ThreadMemory(thread_id="", user_id=user_id)

    row = db.query(ThreadMemoryModel).filter_by(thread_id=thread_id, user_id=user_id).first()
    if row:
        return _to_pydantic(row)
    return ThreadMemory(thread_id=thread_id, user_id=user_id)


def update_thread_memory(thread_id: str, user_id: str, label: str, db: Session) -> None:
    """
    Tool: update_thread_memory(...) — called after Finalize (specs v3 §3).
    """
    if not thread_id:
        return

    row = db.query(ThreadMemoryModel).filter_by(thread_id=thread_id, user_id=user_id).first()
    now = datetime.now(timezone.utc)

    if row:
        row.message_count += 1
        row.last_label = label
        row.last_seen_at = now
    else:
        row = ThreadMemoryModel(
            thread_id=thread_id,
            user_id=user_id,
            message_count=1,
            first_label=label,
            last_label=label,
            last_seen_at=now,
        )
        db.add(row)

    db.commit()


def expire_stale_threads(db: Session) -> int:
    """
    Delete thread_memory rows inactive for THREAD_MEMORY_EXPIRY_DAYS days.
    Run by the thread-memory expiry scheduled job. Returns count deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=THREAD_MEMORY_EXPIRY_DAYS)
    deleted = (
        db.query(ThreadMemoryModel)
        .filter(ThreadMemoryModel.last_seen_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info(f"Thread-memory expiry: purged {deleted} thread(s) inactive 30+ days.")
    return deleted
