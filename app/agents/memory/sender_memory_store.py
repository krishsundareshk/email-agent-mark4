"""
sender_memory_store — long-term memory, keyed by sender identity
(specs v3 §5.1, §5.3).

Owns:
- get_sender_memory / update_sender_memory  (the get/update tool pair the
  reasoning_engine calls mid-loop, specs v3 §5.5)
- the combined-distribution fusion (counts prior x semantic-similarity
  likelihood) that feeds Stage 1's Observe step and Stage 3's
  memory-consistency signal
- the human-correction write path (specs v3 §5.4)

Structured facts and statistical aggregates only — never raw content
(specs v3 §6). label_centroids are the one disclosed exception: lossy
aggregate vectors, not the original text (§5.3's privacy note).
"""
import json
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import SenderMemoryModel, GlobalLabelCentroidModel, LabelCorrectionModel
from app.agents.models.sender_memory import SenderMemory, GlobalLabelCentroid
from app.agents.models.label_correction import LabelCorrection
from app.agents.models.enums import TrustTier

logger = logging.getLogger(__name__)

HUMAN_CORRECTION_WEIGHT = 3.0  # human_corrected_counts count for more than agent_label_counts
GLOBAL_PRIOR_WEIGHT = 0.25     # weight given to the global cold-start centroid when blended


def _sender_key(sender_email: str) -> str:
    """
    Domain-level key by default (specs v3 §5.1 — "domain or address").
    Falls back to the full address if no '@' is present.
    """
    if not sender_email or "@" not in sender_email:
        return (sender_email or "unknown").lower()
    return sender_email.split("@", 1)[1].lower()


def _to_pydantic(row: SenderMemoryModel) -> SenderMemory:
    return SenderMemory(
        sender_key=row.sender_key,
        user_id=row.user_id,
        first_seen_at=row.first_seen_at,
        total_seen=row.total_seen,
        agent_label_counts=json.loads(row.agent_label_counts or "{}"),
        human_corrected_counts=json.loads(row.human_corrected_counts or "{}"),
        label_centroids=json.loads(row.label_centroids or "{}"),
        label_centroid_counts=json.loads(row.label_centroid_counts or "{}"),
        trust_tier=TrustTier(row.trust_tier) if row.trust_tier else TrustTier.NEW,
        correction_count=row.correction_count,
        last_label=row.last_label,
        last_updated=row.last_updated,
    )


def get_sender_memory(sender_email: str, user_id: str, db: Session) -> SenderMemory:
    """
    Tool: get_sender_memory(sender_key) — returns long-term memory.
    Creates and persists a fresh record on first sight (cold start).
    """
    sender_key = _sender_key(sender_email)
    row = (
        db.query(SenderMemoryModel)
        .filter_by(sender_key=sender_key, user_id=user_id)
        .first()
    )
    if row:
        return _to_pydantic(row)

    memory = SenderMemory(sender_key=sender_key, user_id=user_id)
    row = SenderMemoryModel(
        sender_key=sender_key,
        user_id=user_id,
        first_seen_at=memory.first_seen_at,
        total_seen=0,
        agent_label_counts="{}",
        human_corrected_counts="{}",
        label_centroids="{}",
        label_centroid_counts="{}",
        trust_tier=TrustTier.NEW,
        correction_count=0,
        last_updated=memory.last_updated,
    )
    db.add(row)
    db.commit()
    return memory


def get_global_centroid(label: str, user_id: str, db: Session) -> Optional[GlobalLabelCentroid]:
    row = db.query(GlobalLabelCentroidModel).filter_by(user_id=user_id, label=label).first()
    if not row:
        return None
    return GlobalLabelCentroid(
        user_id=row.user_id, label=row.label,
        centroid=json.loads(row.centroid or "[]"), n=row.n,
    )


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def get_combined_distribution(
    sender_memory: SenderMemory,
    candidate_labels: list[str],
    email_embedding: Optional[list[float]],
    user_id: str,
    db: Session,
) -> dict[str, float]:
    """
    Fuse count-based priors with semantic similarity (specs v3 §5.3):
    combined ∝ prior(counts, human-corrections weighted heavier) x
               likelihood(cosine similarity to this label's centroid)

    Falls back to the global per-label centroid (across all senders) when
    the sender has no sender-specific centroid yet — the cold-start case
    for a "New" trust-tier sender.
    """
    # ---- Prior from counts (human corrections weighted 3x agent guesses) ----
    raw_counts = {}
    for label in candidate_labels:
        agent_n = sender_memory.agent_label_counts.get(label, 0)
        human_n = sender_memory.human_corrected_counts.get(label, 0)
        raw_counts[label] = agent_n + HUMAN_CORRECTION_WEIGHT * human_n

    total_count_weight = sum(raw_counts.values())
    if total_count_weight > 0:
        prior = {k: v / total_count_weight for k, v in raw_counts.items()}
    else:
        prior = {k: 1.0 / len(candidate_labels) for k in candidate_labels}  # uniform, cold start

    # ---- Likelihood from cosine similarity to each label's centroid ----
    likelihood = {}
    for label in candidate_labels:
        centroid = sender_memory.label_centroids.get(label)
        if not centroid:
            global_c = get_global_centroid(label, user_id, db)
            centroid = global_c.centroid if global_c and global_c.centroid else None

        if centroid and email_embedding:
            sim = _cosine_similarity(email_embedding, centroid)
            # cosine in [-1, 1] -> [0, 1], then floor so an unfamiliar
            # label never collapses to exactly zero likelihood
            likelihood[label] = max(0.05, (sim + 1) / 2)
        else:
            likelihood[label] = 0.5  # neutral — no signal either way

    # ---- Bayesian-style fusion: prior x likelihood, renormalized ----
    fused = {k: prior[k] * likelihood[k] for k in candidate_labels}
    total = sum(fused.values())
    if total == 0:
        return {k: 1.0 / len(candidate_labels) for k in candidate_labels}
    return {k: v / total for k, v in fused.items()}


def update_sender_memory(
    sender_email: str,
    user_id: str,
    applied_label: str,
    email_embedding: Optional[list[float]],
    db: Session,
) -> SenderMemory:
    """
    Tool: update_sender_memory(...) — called after Finalize (specs v3 §3).

    Increments agent_label_counts and total_seen, updates the label's
    running-mean centroid (new = old + (incoming - old)/n — only the
    current email's embedding is ever computed; no per-email embedding
    is stored, per §5.3), recomputes trust_tier.
    """
    sender_key = _sender_key(sender_email)
    row = (
        db.query(SenderMemoryModel)
        .filter_by(sender_key=sender_key, user_id=user_id)
        .first()
    )
    if not row:
        get_sender_memory(sender_email, user_id, db)
        row = db.query(SenderMemoryModel).filter_by(sender_key=sender_key, user_id=user_id).first()

    agent_counts = json.loads(row.agent_label_counts or "{}")
    agent_counts[applied_label] = agent_counts.get(applied_label, 0) + 1
    row.agent_label_counts = json.dumps(agent_counts)
    row.total_seen += 1
    row.last_label = applied_label
    row.last_updated = datetime.now(timezone.utc)

    if email_embedding:
        _update_centroid(row, applied_label, email_embedding, "agent")
        _update_global_centroid(user_id, applied_label, email_embedding, db)

    tmp = SenderMemory(
        sender_key=row.sender_key, user_id=row.user_id, total_seen=row.total_seen,
        correction_count=row.correction_count,
    )
    row.trust_tier = tmp.compute_trust_tier()

    db.commit()
    return _to_pydantic(row)


def apply_human_correction(
    email_id: str,
    sender_email: str,
    user_id: str,
    previous_label: str,
    corrected_label: str,
    email_embedding: Optional[list[float]],
    db: Session,
) -> LabelCorrection:
    """
    The human-correction feedback loop (specs v3 §5.4):
    - human_corrected_counts and the corrected label's centroid absorb
      this instance;
    - the previously-assigned label's centroid does NOT (it was wrong).
    - correction_count increments, feeding trust_tier and confidence.
    - a LabelCorrection audit row is written (feeds §8's accuracy widget).
    """
    sender_key = _sender_key(sender_email)
    row = db.query(SenderMemoryModel).filter_by(sender_key=sender_key, user_id=user_id).first()
    if not row:
        get_sender_memory(sender_email, user_id, db)
        row = db.query(SenderMemoryModel).filter_by(sender_key=sender_key, user_id=user_id).first()

    human_counts = json.loads(row.human_corrected_counts or "{}")
    human_counts[corrected_label] = human_counts.get(corrected_label, 0) + 1
    row.human_corrected_counts = json.dumps(human_counts)
    row.correction_count += 1
    row.last_label = corrected_label
    row.last_updated = datetime.now(timezone.utc)

    if email_embedding:
        _update_centroid(row, corrected_label, email_embedding, "human")

    tmp = SenderMemory(
        sender_key=row.sender_key, user_id=row.user_id, total_seen=row.total_seen,
        correction_count=row.correction_count,
    )
    row.trust_tier = tmp.compute_trust_tier()
    db.commit()

    correction = LabelCorrection(
        email_id=email_id, sender_key=sender_key, user_id=user_id,
        previous_label=previous_label, corrected_label=corrected_label,
    )
    db.add(LabelCorrectionModel(
        email_id=correction.email_id, sender_key=correction.sender_key,
        user_id=correction.user_id, previous_label=correction.previous_label,
        corrected_label=correction.corrected_label, corrected_at=correction.corrected_at,
    ))
    db.commit()
    return correction


def _update_centroid(row: SenderMemoryModel, label: str, embedding: list[float], source: str) -> None:
    """Incremental running-mean update of a sender's per-label centroid."""
    centroids = json.loads(row.label_centroids or "{}")
    counts = json.loads(row.label_centroid_counts or "{}")

    n = counts.get(label, 0) + 1
    old = centroids.get(label)
    if old and len(old) == len(embedding):
        new_centroid = [old[i] + (embedding[i] - old[i]) / n for i in range(len(embedding))]
    else:
        new_centroid = list(embedding)

    centroids[label] = new_centroid
    counts[label] = n
    row.label_centroids = json.dumps(centroids)
    row.label_centroid_counts = json.dumps(counts)


def _update_global_centroid(user_id: str, label: str, embedding: list[float], db: Session) -> None:
    """Cold-start fallback centroid, across all senders per (user, label)."""
    row = db.query(GlobalLabelCentroidModel).filter_by(user_id=user_id, label=label).first()
    if not row:
        row = GlobalLabelCentroidModel(user_id=user_id, label=label, centroid=json.dumps(embedding), n=1)
        db.add(row)
        return

    old = json.loads(row.centroid or "[]")
    n = row.n + 1
    if old and len(old) == len(embedding):
        new_centroid = [old[i] + (embedding[i] - old[i]) / n for i in range(len(embedding))]
    else:
        new_centroid = list(embedding)
    row.centroid = json.dumps(new_centroid)
    row.n = n
