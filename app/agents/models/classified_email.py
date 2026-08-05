from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_serializer
from app.agents.models.enums import RelationshipLabel, Department, ConfidenceTier


class EvidenceVector(BaseModel):
    """
    The Stage 1 evidence signals feeding the Stage 3 confidence rubric (specs v3 §4).
    Ephemeral inputs to confidence_engine — only the derived ConfidenceTier
    and self_reported_certainty are persisted on ClassifiedEmail; the rest
    are used in-memory during the pipeline run and never stored verbatim.
    """
    model_config = ConfigDict()
    self_reported_certainty: float = 0.0
    reflection_agreement: str = "confirmed"  # confirmed | revised | reversed
    memory_consistency: float = 0.5          # 0..1 agreement with combined distribution
    structural_corroboration: float = 0.5    # 0..1 agreement of deterministic signals
    ambiguity_margin: float = 0.5            # 0..1, higher = clearer winner


class ClassifiedEmail(BaseModel):
    """
    The primary output of the pipeline for each processed email (specs v3 §1, §3, §4, §6).

    Produced by the reasoning_engine (Stage 1) + confidence_engine (Stage 3).
    Stored in the database (structured facts only — never subject/body/summary,
    per the data retention policy in §6), displayed on the dashboard.

    confidence_tier is a rubric outcome (not a float) — see confidence_engine.py.
    """
    model_config = ConfigDict()
    email_id: str
    user_id: str
    thread_id: Optional[str] = None
    relationship: RelationshipLabel
    department: Department
    is_meeting: bool = False
    confidence_tier: ConfidenceTier = ConfidenceTier.NEEDS_REVIEW
    self_reported_certainty: float = 0.0
    reflection_agreement: str = "confirmed"  # confirmed | revised | reversed — feeds §8's agreement-rate widget
    processed_at: datetime = datetime.now(timezone.utc)
    sender_name: Optional[str] = ""
    sender_email: Optional[str] = ""

    @field_serializer("processed_at")
    def serialize_processed_at(self, value: datetime) -> str:
        return value.isoformat()
