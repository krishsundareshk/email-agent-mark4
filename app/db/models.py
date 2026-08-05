from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Float, Boolean, Integer, DateTime, Text, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.agents.models.enums import (
    RelationshipLabel, Department, ConfidenceTier, TrustTier,
    MeetingStatus, CalendarProviderEnum, EmailProviderEnum, BatchRunStatus,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


class UserModel(Base):
    """
    Stores per-user configuration.
    email_provider and calendar_provider determine which API adapters the
    orchestrator uses for this user (specs v3 §9.2 — exactly two of each).
    """
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email_provider: Mapped[str] = mapped_column(
        SAEnum(EmailProviderEnum), default=EmailProviderEnum.GMAIL
    )
    calendar_provider: Mapped[str] = mapped_column(
        SAEnum(CalendarProviderEnum),
        default=CalendarProviderEnum.GOOGLE
    )
    org_domains: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default=""
    )  # comma-separated — used for the Internal relationship domain match
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self):
        return f"<User id={self.id} email={self.email}>"


class ProcessedEmailModel(Base):
    """Tracks which email IDs have already been processed (dedup)."""
    __tablename__ = "processed_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"<ProcessedEmail email_id={self.email_id} user_id={self.user_id}>"


class ClassifiedEmailModel(Base):
    """
    Stores classification results for each processed email (specs v3 §1, §6).

    Structured facts only — no summary/subject/priority/body (dropped per
    pipeline_changes §3). Relationship + Department + Meeting flag +
    confidence_tier replace the old priority/intent_type/confidence-float
    /flagged_for_review quartet.
    """
    __tablename__ = "classified_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    relationship: Mapped[str] = mapped_column(SAEnum(RelationshipLabel), nullable=False)
    department: Mapped[str] = mapped_column(SAEnum(Department), nullable=False)
    is_meeting: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_tier: Mapped[str] = mapped_column(
        SAEnum(ConfidenceTier), default=ConfidenceTier.NEEDS_REVIEW
    )
    self_reported_certainty: Mapped[float] = mapped_column(Float, default=0.0)
    reflection_agreement: Mapped[str] = mapped_column(String, default="confirmed")
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    sender_name: Mapped[Optional[str]] = mapped_column(String, nullable=True, default="")
    sender_email: Mapped[Optional[str]] = mapped_column(String, nullable=True, default="")

    def __repr__(self):
        return (
            f"<ClassifiedEmail email_id={self.email_id} "
            f"relationship={self.relationship} tier={self.confidence_tier}>"
        )


class SenderMemoryModel(Base):
    """
    Long-term memory, keyed by sender identity (specs v3 §5.1, §5.3).
    JSON-serialized dict columns (agent_label_counts, human_corrected_counts,
    label_centroids, label_centroid_counts) — SQLite/Postgres both store
    these as TEXT/JSON without issue via the store layer's json.dumps/loads.
    """
    __tablename__ = "sender_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sender_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    total_seen: Mapped[int] = mapped_column(Integer, default=0)
    agent_label_counts: Mapped[str] = mapped_column(Text, default="{}")
    human_corrected_counts: Mapped[str] = mapped_column(Text, default="{}")
    label_centroids: Mapped[str] = mapped_column(Text, default="{}")
    label_centroid_counts: Mapped[str] = mapped_column(Text, default="{}")
    trust_tier: Mapped[str] = mapped_column(SAEnum(TrustTier), default=TrustTier.NEW)
    correction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"<SenderMemory sender_key={self.sender_key} trust_tier={self.trust_tier}>"


class GlobalLabelCentroidModel(Base):
    """Cold-start fallback centroid — one row per (user_id, label) (specs v3 §5.3)."""
    __tablename__ = "global_label_centroids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String, nullable=False, index=True)
    centroid: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[float]
    n: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self):
        return f"<GlobalLabelCentroid user_id={self.user_id} label={self.label} n={self.n}>"


class ThreadMemoryModel(Base):
    """Short-term memory, keyed by thread ID (specs v3 §5.2). 30-day expiry."""
    __tablename__ = "thread_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    first_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    def __repr__(self):
        return f"<ThreadMemory thread_id={self.thread_id} last_seen_at={self.last_seen_at}>"


class LabelCorrectionModel(Base):
    """Audit log for the human-correction feedback loop (specs v3 §5.4)."""
    __tablename__ = "label_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sender_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    previous_label: Mapped[str] = mapped_column(String, nullable=False)
    corrected_label: Mapped[str] = mapped_column(String, nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"<LabelCorrection email_id={self.email_id} {self.previous_label}->{self.corrected_label}>"


class MeetingCardModel(Base):
    """
    Stores detected meeting invitations (specs v3 §6, §7).

    Content fields (meeting_title/meeting_datetime/attendees/location/
    meeting_summary) are only meaningfully populated while status ==
    Pending. The meeting_purge_job nulls content fields on confirm/dismiss
    and stamps resolution/resolved_at/calendar_event_id, leaving a
    content-free audit stub (pipeline_changes §3).
    """
    __tablename__ = "meeting_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    email_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    meeting_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    meeting_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, default=60, nullable=True)
    organizer_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    organizer_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attendees: Mapped[Optional[str]] = mapped_column(Text, default="[]")  # JSON string
    location_or_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    meeting_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        SAEnum(MeetingStatus),
        default=MeetingStatus.PENDING
    )
    calendar_provider: Mapped[str] = mapped_column(
        SAEnum(CalendarProviderEnum),
        nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    resolution: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def __repr__(self):
        return (
            f"<MeetingCard meeting_id={self.meeting_id} "
            f"title={self.meeting_title} status={self.status}>"
        )


class BatchRunLogModel(Base):
    """Stores metadata about each batch pipeline run."""
    __tablename__ = "batch_run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    status: Mapped[str] = mapped_column(
        SAEnum(BatchRunStatus),
        default=BatchRunStatus.RUNNING
    )
    emails_fetched: Mapped[int] = mapped_column(Integer, default=0)
    emails_classified: Mapped[int] = mapped_column(Integer, default=0)
    emails_failed: Mapped[int] = mapped_column(Integer, default=0)
    emails_deferred: Mapped[int] = mapped_column(Integer, default=0)
    meetings_detected: Mapped[int] = mapped_column(Integer, default=0)
    stage0_resolved: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self):
        return (
            f"<BatchRunLog run_id={self.run_id} "
            f"status={self.status} fetched={self.emails_fetched}>"
        )
