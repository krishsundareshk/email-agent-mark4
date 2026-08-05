"""
confidence_engine — Stage 3, the multi-signal decision rubric (specs v3 §4).

Combines five signals into a ConfidenceTier. Not a single float threshold —
that was the old classification_confidence/flagged_for_review design; this
replaces it entirely.
"""
import logging

from app.agents.models.classified_email import EvidenceVector
from app.agents.models.enums import ConfidenceTier, RelationshipLabel

logger = logging.getLogger(__name__)

HIGH_CERTAINTY_THRESHOLD = 0.75
LOW_CERTAINTY_THRESHOLD = 0.4
MEMORY_CONTRADICTION_THRESHOLD = 0.35   # below this, memory disagrees with the tentative call
LOW_CORROBORATION_THRESHOLD = 0.4
NARROW_AMBIGUITY_MARGIN = 0.55          # below this, two labels were close contenders


def compute_confidence_tier(
    evidence: EvidenceVector,
    relationship: RelationshipLabel,
) -> ConfidenceTier:
    """
    specs v3 §4 decision rubric:

    | Condition                                                        | Outcome        |
    |-------------------------------------------------------------------|----------------|
    | All signals agree, high self-reported certainty                  | auto-applied   |
    | Reflection revised, OR memory contradiction, OR narrow ambiguity  | needs-review   |
    | Low corroboration + low certainty + high ambiguity                | unclassified   |
    | Suspicious signals present                                        | always flagged |
    """
    # Suspicious is always surfaced, never silently auto-resolved.
    if relationship == RelationshipLabel.SUSPICIOUS:
        logger.info("confidence_engine: Suspicious relationship — forcing needs-review.")
        return ConfidenceTier.NEEDS_REVIEW

    reflection_revised = evidence.reflection_agreement in ("revised", "reversed")
    memory_contradiction = evidence.memory_consistency < MEMORY_CONTRADICTION_THRESHOLD
    narrow_ambiguity = evidence.ambiguity_margin < NARROW_AMBIGUITY_MARGIN

    low_corroboration = evidence.structural_corroboration < LOW_CORROBORATION_THRESHOLD
    low_certainty = evidence.self_reported_certainty < LOW_CERTAINTY_THRESHOLD
    high_ambiguity = evidence.ambiguity_margin < (NARROW_AMBIGUITY_MARGIN - 0.15)

    if low_corroboration and low_certainty and high_ambiguity:
        return ConfidenceTier.UNCLASSIFIED

    if reflection_revised or memory_contradiction or narrow_ambiguity:
        return ConfidenceTier.NEEDS_REVIEW

    all_signals_agree = (
        evidence.reflection_agreement == "confirmed"
        and not memory_contradiction
        and not low_corroboration
        and not narrow_ambiguity
    )
    if all_signals_agree and evidence.self_reported_certainty >= HIGH_CERTAINTY_THRESHOLD:
        return ConfidenceTier.AUTO_APPLIED

    # Default — signals didn't clearly clear the auto-apply bar but also
    # didn't hit the unclassified floor; safest middle ground is review.
    return ConfidenceTier.NEEDS_REVIEW
