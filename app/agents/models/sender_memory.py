from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_serializer
from app.agents.models.enums import RelationshipLabel, TrustTier

TRUSTED_MIN_SEEN = 20
FAMILIAR_MIN_SEEN = 5
TRUSTED_MAX_CORRECTION_RATE = 0.15


class SenderMemory(BaseModel):
    """
    Long-term memory, keyed by sender identity (specs v3 §5.1).

    Holds structured facts and statistical aggregates only — never raw
    content, never a rationale string (§6). `label_centroids` are the one
    disclosed exception to "no content" (§5.3): lossy aggregate vectors,
    not the original text.
    """
    model_config = ConfigDict()
    sender_key: str  # domain or address
    user_id: str
    first_seen_at: datetime = datetime.now(timezone.utc)
    total_seen: int = 0
    agent_label_counts: dict[str, int] = {}
    human_corrected_counts: dict[str, int] = {}
    label_centroids: dict[str, list[float]] = {}       # label -> running-mean vector
    label_centroid_counts: dict[str, int] = {}          # label -> n used in the running mean
    trust_tier: TrustTier = TrustTier.NEW
    correction_count: int = 0
    last_label: Optional[str] = None
    last_updated: datetime = datetime.now(timezone.utc)

    @field_serializer("first_seen_at", "last_updated")
    def serialize_datetimes(self, value: datetime) -> str:
        return value.isoformat() if value else None

    def compute_trust_tier(self) -> TrustTier:
        """
        Derive trust_tier from total_seen and correction rate (specs v3 §5.1).
        """
        if self.total_seen < FAMILIAR_MIN_SEEN:
            return TrustTier.NEW
        if self.total_seen < TRUSTED_MIN_SEEN:
            return TrustTier.FAMILIAR
        correction_rate = (
            self.correction_count / self.total_seen if self.total_seen else 1.0
        )
        if correction_rate <= TRUSTED_MAX_CORRECTION_RATE:
            return TrustTier.TRUSTED
        return TrustTier.FAMILIAR


class GlobalLabelCentroid(BaseModel):
    """
    Cold-start fallback centroid — one per (user_id, label), across all
    senders (specs v3 §5.3). Used when a sender has no sender-specific
    centroid yet (the "New" trust tier case).
    """
    model_config = ConfigDict()
    user_id: str
    label: str
    centroid: list[float] = []
    n: int = 0
