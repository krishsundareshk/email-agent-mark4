"""
reasoning_engine — Stage 1 (specs v3 §3): Observe → Reason → tentative Act
→ Reflect → Finalize.

Merges what used to be two separate LLM calls (classify_email +
detect_meeting's is_meeting gate) into one triage pass, with NO Priority
output at all (pipeline_changes §2/§4). Subject + body are used
in-memory only and never persisted (specs v3 §6) — only what's built
into ClassifiedEmail crosses into the DB.

Owns tool-calling mid-loop: get_sender_memory, get_thread_memory,
update_sender_memory, update_thread_memory, apply_label — Finalize
invokes all of them, not the orchestrator.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.providers.email.base import EmailObject, EmailProvider
from app.agents.models.enums import RelationshipLabel, Department, ConfidenceTier
from app.agents.models.classified_email import ClassifiedEmail, EvidenceVector
from app.agents.tools.llm_client import LLMClient
from app.agents.tools.base import clean_and_parse_json, safe_enum, safe_bool, truncate_at_word_boundary
from app.agents.tools.link_extractor import extract_candidate_links
from app.agents.tools.confidence_engine import compute_confidence_tier
from app.agents.tools.apply_label_tool import apply_label, ApplyLabelToolResult
from app.agents.memory.sender_memory_store import (
    get_sender_memory, get_combined_distribution, update_sender_memory,
)
from app.agents.memory.thread_memory_store import get_thread_memory, update_thread_memory
from app.agents.embedding.embedding_adapter import EmbeddingAdapter

logger = logging.getLogger(__name__)

BODY_CHAR_LIMIT = 6000
CANDIDATE_RELATIONSHIP_LABELS = [
    RelationshipLabel.INTERNAL.value,
    RelationshipLabel.CLIENT.value,
    RelationshipLabel.VENDOR.value,
    RelationshipLabel.AUTOMATED_SYSTEM.value,
    RelationshipLabel.UNKNOWN_EXTERNAL.value,
    RelationshipLabel.SUSPICIOUS.value,
]

REASON_SYSTEM_PROMPT = """You are the reasoning engine of an enterprise inbox agent.

Classify one email along two independent dimensions, using the context
provided (sender memory, thread memory, combined label distribution,
Stage-0 signals, candidate meeting links). Respond with ONLY a raw JSON
object, no markdown, no commentary:

{
  "relationship": "<Internal|Client|Vendor|Automated-System|Unknown-External|Suspicious>",
  "department": "<HR|Finance|IT|Legal|Operations|General>",
  "is_meeting": true or false,
  "self_reported_certainty": <float 0.0-1.0>,
  "rationale": "<one paragraph — evidence for/against each candidate label; never stored, for reflection only>"
}

Guidance:
- Internal: sender's domain matches the organization's own domain(s).
- Client: an external party the org serves.
- Vendor: an external party providing services/invoices to the org.
- Automated-System: non-marketing automated mail — receipts, CI/CD, monitoring, calendar-system notices.
- Unknown-External: external, no established relationship, content doesn't clarify.
- Suspicious: phishing/spoofing/social-engineering signals present — always flag this regardless of other signals if present.
- is_meeting: true only if the email contains an extractable scheduling invite (specific date/time), never for Promotional-style content.
- Weigh the combined label distribution and sender memory as a prior, but do not blindly follow it — override it when the email's own content clearly disagrees.
"""

REFLECT_SYSTEM_PROMPT = """You are reflecting on a tentative email classification before it is finalized.

You will be given the tentative decision, the reasoning behind it, and
the sender's combined label distribution / memory. Check the tentative
decision against that memory specifically, and flag contradictions.
Respond with ONLY a raw JSON object:

{
  "agreement": "<confirmed|revised|reversed>",
  "relationship": "<possibly-revised relationship label>",
  "department": "<possibly-revised department>",
  "is_meeting": true or false,
  "memory_consistency": <float 0.0-1.0, how well the (possibly revised) decision matches the combined distribution>,
  "ambiguity_margin": <float 0.0-1.0, how clearly this label won over the next-best contender>
}
"""


@dataclass
class ReasoningResult:
    classified_email: ClassifiedEmail
    evidence: EvidenceVector
    apply_label_result: Optional[ApplyLabelToolResult] = None
    candidate_links: list[str] = field(default_factory=list)


def run_reasoning_engine(
    email: EmailObject,
    user_id: str,
    org_domains: list[str],
    stage0_signal_summary: str,
    email_provider: EmailProvider,
    db: Session,
    llm_client: Optional[LLMClient] = None,
    llm_model: Optional[str] = None,
    embedding_adapter: Optional[EmbeddingAdapter] = None,
) -> ReasoningResult:
    llm_client = llm_client or LLMClient(model=llm_model)
    embedding_adapter = embedding_adapter or EmbeddingAdapter()

    # ── Observe ──────────────────────────────────────────────────────────
    candidate_links = extract_candidate_links(email.body_text)
    sender_memory = get_sender_memory(email.sender_email, user_id, db)
    thread_memory = get_thread_memory(email.thread_id or "", user_id, db)

    combined_text = f"{email.subject}\n\n{email.body_text}"
    email_embedding = embedding_adapter.embed(truncate_at_word_boundary(combined_text, BODY_CHAR_LIMIT))

    combined_distribution = get_combined_distribution(
        sender_memory=sender_memory,
        candidate_labels=CANDIDATE_RELATIONSHIP_LABELS,
        email_embedding=email_embedding,
        user_id=user_id,
        db=db,
    )

    domain_match = any(
        email.sender_email.lower().endswith(f"@{d.strip().lower()}")
        for d in org_domains if d.strip()
    )

    observe_context = _build_observe_context(
        email=email,
        sender_memory=sender_memory,
        thread_memory=thread_memory,
        combined_distribution=combined_distribution,
        stage0_signal_summary=stage0_signal_summary,
        candidate_links=candidate_links,
        domain_match=domain_match,
    )

    # ── Reason + tentative Act ──────────────────────────────────────────
    tentative = _reason_and_act(llm_client, llm_model, email, observe_context)

    # ── Reflect ──────────────────────────────────────────────────────────
    reflection = _reflect(llm_client, llm_model, tentative, observe_context)

    # ── Finalize ─────────────────────────────────────────────────────────
    relationship = safe_enum(
        RelationshipLabel, reflection.get("relationship") or tentative.get("relationship"),
        RelationshipLabel.UNKNOWN_EXTERNAL, "relationship", email.email_id, logger,
    )
    department = safe_enum(
        Department, reflection.get("department") or tentative.get("department"),
        Department.GENERAL, "department", email.email_id, logger,
    )
    is_meeting = safe_bool(
        reflection.get("is_meeting", tentative.get("is_meeting")), default=False
    )

    structural_corroboration = _structural_corroboration_signal(
        relationship, domain_match, stage0_signal_summary, is_meeting, candidate_links
    )

    evidence = EvidenceVector(
        self_reported_certainty=_safe_float(tentative.get("self_reported_certainty"), 0.4),
        reflection_agreement=reflection.get("agreement", "confirmed"),
        memory_consistency=_safe_float(reflection.get("memory_consistency"), 0.5),
        structural_corroboration=structural_corroboration,
        ambiguity_margin=_safe_float(reflection.get("ambiguity_margin"), 0.5),
    )

    confidence_tier = compute_confidence_tier(evidence, relationship)

    classified = ClassifiedEmail(
        email_id=email.email_id,
        user_id=user_id,
        thread_id=email.thread_id,
        relationship=relationship,
        department=department,
        is_meeting=is_meeting,
        confidence_tier=confidence_tier,
        self_reported_certainty=evidence.self_reported_certainty,
        reflection_agreement=evidence.reflection_agreement,
        sender_name=email.sender_name,
        sender_email=email.sender_email,
    )

    # Finalize's tool calls: apply_label + memory updates.
    apply_result = None
    if confidence_tier != ConfidenceTier.UNCLASSIFIED:
        apply_result = apply_label(
            email_id=email.email_id,
            user_id=user_id,
            label=relationship.value,
            email_provider=email_provider,
            db=db,
        )

    update_sender_memory(
        sender_email=email.sender_email,
        user_id=user_id,
        applied_label=relationship.value,
        email_embedding=email_embedding,
        db=db,
    )
    update_thread_memory(
        thread_id=email.thread_id or "", user_id=user_id, label=relationship.value, db=db
    )

    logger.info(
        f"[reasoning_engine] {email.email_id} -> relationship={relationship.value} "
        f"department={department.value} meeting={is_meeting} tier={confidence_tier.value}"
    )

    return ReasoningResult(
        classified_email=classified,
        evidence=evidence,
        apply_label_result=apply_result,
        candidate_links=candidate_links,
    )


# ─────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────

def _build_observe_context(
    email: EmailObject, sender_memory, thread_memory, combined_distribution,
    stage0_signal_summary: str, candidate_links: list[str], domain_match: bool,
) -> str:
    dist_str = ", ".join(f"{k}: {v:.2f}" for k, v in combined_distribution.items())
    return (
        f"Sender: {email.sender_name} <{email.sender_email}> "
        f"(domain matches org: {domain_match})\n"
        f"Sender trust tier: {sender_memory.trust_tier.value}, "
        f"total_seen: {sender_memory.total_seen}, "
        f"correction_count: {sender_memory.correction_count}\n"
        f"Combined label distribution (counts x semantic similarity): {dist_str}\n"
        f"Thread memory: message_count={thread_memory.message_count}, "
        f"last_label={thread_memory.last_label}\n"
        f"Stage 0 signal summary: {stage0_signal_summary}\n"
        f"Candidate meeting links found by regex: {candidate_links}\n"
    )


def _reason_and_act(llm_client: LLMClient, llm_model: Optional[str], email: EmailObject, observe_context: str) -> dict:
    body = truncate_at_word_boundary(email.body_text, BODY_CHAR_LIMIT)
    user_prompt = (
        f"{observe_context}\n"
        f"Subject: {email.subject}\n\n"
        f"Body:\n{body}\n\n"
        f"Return only the JSON object."
    )
    try:
        raw = llm_client.complete(system_prompt=REASON_SYSTEM_PROMPT, user_prompt=user_prompt, model=llm_model)
        parsed = clean_and_parse_json(raw, logger)
        return parsed or {}
    except Exception as e:
        logger.warning(f"reasoning_engine Reason/Act step failed for {email.email_id}: {e}")
        return {}


def _reflect(llm_client: LLMClient, llm_model: Optional[str], tentative: dict, observe_context: str) -> dict:
    user_prompt = (
        f"{observe_context}\n"
        f"Tentative decision: {tentative}\n\n"
        f"Return only the JSON object."
    )
    try:
        raw = llm_client.complete(system_prompt=REFLECT_SYSTEM_PROMPT, user_prompt=user_prompt, model=llm_model)
        parsed = clean_and_parse_json(raw, logger)
        return parsed or {}
    except Exception as e:
        logger.warning(f"reasoning_engine Reflect step failed: {e}")
        return {}


def _structural_corroboration_signal(
    relationship: RelationshipLabel, domain_match: bool, stage0_signal_summary: str,
    is_meeting: bool, candidate_links: list[str],
) -> float:
    """
    Deterministic-signal agreement (specs v3 §4): does domain match agree
    with Internal, does the meeting flag agree with a found link, etc.
    """
    score = 0.5
    if relationship == RelationshipLabel.INTERNAL:
        score = 0.9 if domain_match else 0.2
    elif relationship == RelationshipLabel.PROMOTIONAL:
        score = 0.9 if "conclusive" in stage0_signal_summary else 0.3
    if is_meeting:
        score = min(1.0, score + 0.2) if candidate_links else max(0.0, score - 0.2)
    return score


def _safe_float(value, default: float) -> float:
    try:
        v = float(value)
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return default
