import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import (
    ClassifiedEmailModel, MeetingCardModel, SenderMemoryModel, LabelCorrectionModel,
)
from app.api.auth import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _enum_value(v):
    return v.value if hasattr(v, "value") else v


@router.get("/volume-trend")
def volume_trend(
    days: int = 30,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Emails processed per day, last `days` days (specs v3 §8)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = (
        db.query(ClassifiedEmailModel.processed_at)
        .filter(ClassifiedEmailModel.user_id == user_id)
        .filter(ClassifiedEmailModel.processed_at >= cutoff)
        .all()
    )
    counts: dict[str, int] = defaultdict(int)
    for (processed_at,) in records:
        if processed_at:
            counts[processed_at.date().isoformat()] += 1
    return {"days": sorted(counts.keys()), "series": [counts[d] for d in sorted(counts.keys())]}


@router.get("/relationship-distribution")
def relationship_distribution(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Share of Internal / Client / Vendor / Promotional / Automated / Unknown / Suspicious."""
    records = db.query(ClassifiedEmailModel.relationship).filter_by(user_id=user_id).all()
    counts = Counter(_enum_value(r[0]) for r in records)
    total = sum(counts.values()) or 1
    return {
        "counts": dict(counts),
        "percentages": {k: round(v / total * 100, 1) for k, v in counts.items()},
    }


@router.get("/meeting-funnel")
def meeting_funnel(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """detected → confirmed (Added) → dismissed → pending, plus upcoming count."""
    records = db.query(MeetingCardModel).filter_by(user_id=user_id).all()
    detected = len(records)
    confirmed = sum(1 for r in records if _enum_value(r.status) == "Added")
    dismissed = sum(1 for r in records if _enum_value(r.status) == "Dismissed")
    pending = sum(1 for r in records if _enum_value(r.status) == "Pending")
    now = datetime.now(timezone.utc)
    upcoming = sum(
        1 for r in records
        if _enum_value(r.status) == "Pending" and r.meeting_datetime and r.meeting_datetime > now
    )
    return {
        "detected": detected, "confirmed": confirmed, "dismissed": dismissed,
        "pending": pending, "upcoming": upcoming,
    }


@router.get("/needs-review-queue")
def needs_review_queue(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Size of the needs-review queue + average time-to-resolution, approximated
    as the average gap between an email's processed_at and its (if any)
    subsequent human correction timestamp (specs v3 §8).
    """
    needs_review = (
        db.query(ClassifiedEmailModel)
        .filter_by(user_id=user_id, confidence_tier="needs-review")
        .all()
    )
    size = len(needs_review)

    corrections = db.query(LabelCorrectionModel).filter_by(user_id=user_id).all()
    correction_by_email = {c.email_id: c.corrected_at for c in corrections}

    resolution_seconds = []
    for record in needs_review:
        corrected_at = correction_by_email.get(record.email_id)
        if corrected_at and record.processed_at:
            resolution_seconds.append((corrected_at - record.processed_at).total_seconds())

    avg_resolution_hours = (
        round((sum(resolution_seconds) / len(resolution_seconds)) / 3600, 1)
        if resolution_seconds else None
    )
    return {"queue_size": size, "avg_time_to_resolution_hours": avg_resolution_hours}


@router.get("/top-senders")
def top_senders(
    limit: int = 10,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Top senders by volume — identity + count only, no content (specs v3 §8)."""
    records = (
        db.query(ClassifiedEmailModel.sender_email, ClassifiedEmailModel.sender_name)
        .filter_by(user_id=user_id)
        .all()
    )
    counts = Counter(r[0] for r in records if r[0])
    names = {r[0]: r[1] for r in records if r[0]}
    top = counts.most_common(limit)
    return [{"sender_email": email, "sender_name": names.get(email, ""), "count": count} for email, count in top]


@router.get("/trust-tier-breakdown")
def trust_tier_breakdown(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """New / Familiar / Trusted breakdown across all known senders."""
    records = db.query(SenderMemoryModel.trust_tier).filter_by(user_id=user_id).all()
    counts = Counter(_enum_value(r[0]) for r in records)
    return {"New": counts.get("New", 0), "Familiar": counts.get("Familiar", 0), "Trusted": counts.get("Trusted", 0)}


@router.get("/promotional-noise-ratio")
def promotional_noise_ratio(
    days: int = 30,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Promotional share of total volume, trended by day (specs v3 §8)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = (
        db.query(ClassifiedEmailModel.processed_at, ClassifiedEmailModel.relationship)
        .filter(ClassifiedEmailModel.user_id == user_id)
        .filter(ClassifiedEmailModel.processed_at >= cutoff)
        .all()
    )
    total_by_day: dict[str, int] = defaultdict(int)
    promo_by_day: dict[str, int] = defaultdict(int)
    for processed_at, relationship in records:
        if not processed_at:
            continue
        day = processed_at.date().isoformat()
        total_by_day[day] += 1
        if _enum_value(relationship) == "Promotional":
            promo_by_day[day] += 1

    days_sorted = sorted(total_by_day.keys())
    return {
        "days": days_sorted,
        "ratio": [
            round(promo_by_day[d] / total_by_day[d], 3) if total_by_day[d] else 0.0
            for d in days_sorted
        ],
    }


@router.get("/label-accuracy-over-time")
def label_accuracy_over_time(
    weeks: int = 12,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Correction rate per week — trending down as trust builds (specs v3 §8).
    The single most visible answer to "is the agent actually improving".
    """
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    classified = (
        db.query(ClassifiedEmailModel.processed_at)
        .filter(ClassifiedEmailModel.user_id == user_id)
        .filter(ClassifiedEmailModel.processed_at >= cutoff)
        .all()
    )
    corrections = (
        db.query(LabelCorrectionModel.corrected_at)
        .filter(LabelCorrectionModel.user_id == user_id)
        .filter(LabelCorrectionModel.corrected_at >= cutoff)
        .all()
    )

    def _week_key(dt: datetime) -> str:
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    total_by_week: dict[str, int] = defaultdict(int)
    corrections_by_week: dict[str, int] = defaultdict(int)
    for (processed_at,) in classified:
        if processed_at:
            total_by_week[_week_key(processed_at)] += 1
    for (corrected_at,) in corrections:
        if corrected_at:
            corrections_by_week[_week_key(corrected_at)] += 1

    weeks_sorted = sorted(set(total_by_week) | set(corrections_by_week))
    return {
        "weeks": weeks_sorted,
        "correction_rate": [
            round(corrections_by_week[w] / total_by_week[w], 3) if total_by_week.get(w) else 0.0
            for w in weeks_sorted
        ],
    }


@router.get("/reasoning-agreement-rate")
def reasoning_agreement_rate(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """% of emails where Reflect confirmed the tentative Stage 1 decision."""
    records = db.query(ClassifiedEmailModel.reflection_agreement).filter_by(user_id=user_id).all()
    counts = Counter(r[0] for r in records)
    total = sum(counts.values()) or 1
    return {
        "confirmed_rate": round(counts.get("confirmed", 0) / total, 3),
        "revised_rate": round(counts.get("revised", 0) / total, 3),
        "reversed_rate": round(counts.get("reversed", 0) / total, 3),
        "total": total,
    }


@router.get("/suspicious-count")
def suspicious_count(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Kept visually prominent per specs v3 §8."""
    count = db.query(ClassifiedEmailModel).filter_by(user_id=user_id, relationship="Suspicious").count()
    return {"suspicious_count": count}
