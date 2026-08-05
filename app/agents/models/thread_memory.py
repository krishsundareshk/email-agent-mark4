from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_serializer

THREAD_MEMORY_EXPIRY_DAYS = 30


class ThreadMemory(BaseModel):
    """
    Short-term memory, keyed by Gmail/Outlook thread ID (specs v3 §5.2).
    Purged after 30 days of inactivity by the thread-memory expiry job.
    """
    model_config = ConfigDict()
    thread_id: str
    user_id: str
    message_count: int = 0
    first_label: Optional[str] = None
    last_label: Optional[str] = None
    last_seen_at: datetime = datetime.now(timezone.utc)

    @field_serializer("last_seen_at")
    def serialize_last_seen_at(self, value: datetime) -> str:
        return value.isoformat() if value else None
