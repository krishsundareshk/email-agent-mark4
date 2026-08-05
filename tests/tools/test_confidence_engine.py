from app.agents.models.classified_email import EvidenceVector
from app.agents.models.enums import ConfidenceTier, RelationshipLabel
from app.agents.tools.confidence_engine import compute_confidence_tier


def test_suspicious_always_needs_review():
    evidence = EvidenceVector(
        self_reported_certainty=0.99, reflection_agreement="confirmed",
        memory_consistency=0.99, structural_corroboration=0.99, ambiguity_margin=0.99,
    )
    tier = compute_confidence_tier(evidence, RelationshipLabel.SUSPICIOUS)
    assert tier == ConfidenceTier.NEEDS_REVIEW


def test_all_signals_agree_auto_applied():
    evidence = EvidenceVector(
        self_reported_certainty=0.9, reflection_agreement="confirmed",
        memory_consistency=0.8, structural_corroboration=0.8, ambiguity_margin=0.8,
    )
    tier = compute_confidence_tier(evidence, RelationshipLabel.INTERNAL)
    assert tier == ConfidenceTier.AUTO_APPLIED


def test_low_everything_unclassified():
    evidence = EvidenceVector(
        self_reported_certainty=0.1, reflection_agreement="confirmed",
        memory_consistency=0.5, structural_corroboration=0.1, ambiguity_margin=0.1,
    )
    tier = compute_confidence_tier(evidence, RelationshipLabel.UNKNOWN_EXTERNAL)
    assert tier == ConfidenceTier.UNCLASSIFIED


def test_reflection_revised_needs_review():
    evidence = EvidenceVector(
        self_reported_certainty=0.9, reflection_agreement="revised",
        memory_consistency=0.9, structural_corroboration=0.9, ambiguity_margin=0.9,
    )
    tier = compute_confidence_tier(evidence, RelationshipLabel.VENDOR)
    assert tier == ConfidenceTier.NEEDS_REVIEW
