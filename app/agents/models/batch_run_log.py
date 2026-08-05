from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_serializer
from app.agents.models.enums import BatchRunStatus


class BatchRunLog(BaseModel):
    """
    A record of a single batch pipeline run for one user.

    Written at the start of each run (status=Running) and
     updated at the end (status=Success/PartialFailure/Failed).

    Powers the dashboard's "Last synced X minutes ago" indicator
    and the error banner when a run fails.
    """
    model_config = ConfigDict()
    run_id: str
    user_id: str
    started_at: datetime = datetime.now(timezone.utc)
    completed_at: Optional[datetime] = None
    status: BatchRunStatus = BatchRunStatus.RUNNING
    emails_fetched: int = 0
    emails_classified: int = 0
    emails_failed: int = 0
    emails_deferred: int = 0
    meetings_detected: int = 0
    stage0_resolved: int = 0
    error_message: Optional[str] = None

    @field_serializer("started_at", "completed_at")
    def serialize_datetimes(self, value: datetime) -> str:
        return value.isoformat() if value else None
