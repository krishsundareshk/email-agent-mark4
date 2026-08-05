"""
meeting_purge_job — safety-net sweep for content purge (specs v3 §6).

POST /api/meetings/{id}/confirm|dismiss already purges content
synchronously (app/agents/tools/write_to_data_store.resolve_and_purge_meeting).
This job exists for the edge case: a Pending card that never gets
resolved by the user. After MEETING_STALE_DAYS, it's auto-dismissed and
purged so meeting content doesn't linger indefinitely in the DB.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from app.db.database import SessionLocal
from app.db.models import MeetingCardModel
from app.agents.tools.write_to_data_store import resolve_and_purge_meeting

logger = logging.getLogger(__name__)

MEETING_STALE_DAYS = int(os.getenv("MEETING_STALE_DAYS", "14"))


def run_meeting_purge_job() -> int:
    db = SessionLocal()
    purged = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=MEETING_STALE_DAYS)
        stale = (
            db.query(MeetingCardModel)
            .filter(MeetingCardModel.status == "Pending")
            .filter(MeetingCardModel.created_at < cutoff)
            .all()
        )
        for card in stale:
            if resolve_and_purge_meeting(card.meeting_id, "dismissed", db):
                purged += 1
        if purged:
            logger.info(f"[meeting_purge_job] auto-dismissed + purged {purged} stale meeting card(s).")
        return purged
    finally:
        db.close()
