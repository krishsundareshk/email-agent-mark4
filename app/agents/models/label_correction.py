from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, field_serializer


class LabelCorrection(BaseModel):
    """
    Audit record for the human-correction feedback loop (specs v3 §5.4).
    Feeds both the sender_memory update and the "label accuracy over time"
    dashboard widget (§8).
    """
    model_config = ConfigDict()
    email_id: str
    sender_key: str
    user_id: str
    previous_label: str
    corrected_label: str
    corrected_at: datetime = datetime.now(timezone.utc)

    @field_serializer("corrected_at")
    def serialize_corrected_at(self, value: datetime) -> str:
        return value.isoformat() if value else None
