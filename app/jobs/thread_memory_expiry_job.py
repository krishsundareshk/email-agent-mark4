"""thread_memory_expiry_job — purges thread_memory rows inactive 30+ days (specs v3 §5.2)."""
import logging
from app.db.database import SessionLocal
from app.agents.memory.thread_memory_store import expire_stale_threads

logger = logging.getLogger(__name__)


def run_thread_memory_expiry_job() -> int:
    db = SessionLocal()
    try:
        return expire_stale_threads(db)
    finally:
        db.close()
