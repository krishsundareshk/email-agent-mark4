import logging
from datetime import timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ClassifiedEmailModel, UserModel
from app.api.auth import get_current_user_id, get_provider_access_token
from app.agents.models.enums import RelationshipLabel, EmailProviderEnum
from app.agents.embedding.embedding_adapter import EmbeddingAdapter
from app.agents.memory.sender_memory_store import apply_human_correction
from app.agents.tools.apply_label_tool import apply_label
from app.providers.email import get_email_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/emails", tags=["emails"])


def _open_in_inbox_url(provider: str, email_id: str) -> str:
    """
    Build a direct deep link into the real inbox (pipeline_changes §6).
    Gmail: message ID works directly in the #all/ search-result view.
    Outlook: OWA's /id/ route expects the message ID URL-encoded.
    """
    if provider == EmailProviderEnum.OUTLOOK.value:
        return f"https://outlook.office.com/mail/inbox/id/{quote(email_id, safe='')}"
    return f"https://mail.google.com/mail/u/0/#all/{email_id}"


@router.get("/")
def get_emails(
    relationship: Optional[str] = None,
    department: Optional[str] = None,
    confidence_tier: Optional[str] = None,
    is_meeting: Optional[bool] = None,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Return classified emails for the current user, newest first.
    Filters by Relationship (not Priority — removed entirely), Department,
    confidence_tier, and is_meeting.
    """
    query = db.query(ClassifiedEmailModel).filter_by(user_id=user_id)

    if relationship:
        query = query.filter(ClassifiedEmailModel.relationship == relationship)
    if department:
        query = query.filter(ClassifiedEmailModel.department == department)
    if confidence_tier:
        query = query.filter(ClassifiedEmailModel.confidence_tier == confidence_tier)
    if is_meeting is not None:
        query = query.filter(ClassifiedEmailModel.is_meeting == is_meeting)

    records = query.order_by(ClassifiedEmailModel.processed_at.desc()).all()

    user = db.query(UserModel).filter_by(id=user_id).first()
    provider_value = (
        user.email_provider.value if user and hasattr(user.email_provider, "value")
        else (user.email_provider if user else EmailProviderEnum.GMAIL.value)
    )

    return [
        {
            "email_id": r.email_id,
            "thread_id": r.thread_id,
            "relationship": r.relationship.value if hasattr(r.relationship, "value") else r.relationship,
            "department": r.department.value if hasattr(r.department, "value") else r.department,
            "is_meeting": r.is_meeting,
            "confidence_tier": r.confidence_tier.value if hasattr(r.confidence_tier, "value") else r.confidence_tier,
            "self_reported_certainty": r.self_reported_certainty,
            "processed_at": r.processed_at.replace(tzinfo=timezone.utc).isoformat() if r.processed_at else None,
            "sender_name": r.sender_name or "",
            "sender_email": r.sender_email or "",
            "open_in_inbox_url": _open_in_inbox_url(provider_value, r.email_id),
        }
        for r in records
    ]


class CorrectLabelRequest(BaseModel):
    corrected_label: RelationshipLabel


@router.post("/{email_id}/correct-label")
def correct_label(
    email_id: str,
    body: CorrectLabelRequest,
    user_id: str = Depends(get_current_user_id),
    access_token: Optional[str] = Depends(get_provider_access_token),
    db: Session = Depends(get_db),
):
    """
    Human-correction feedback loop (specs v3 §5.4, pipeline_changes §6):
    1. Update the stored label on the ClassifiedEmail row.
    2. Append to label_corrections (audit log, feeds the accuracy widget).
    3. Update sender_memory (human_corrected_counts + centroid).
    4. Re-invoke apply_label to fix the real inbox label.
    """
    record = db.query(ClassifiedEmailModel).filter_by(email_id=email_id, user_id=user_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Email not found for this user.")

    previous_label = record.relationship.value if hasattr(record.relationship, "value") else record.relationship
    corrected_label = body.corrected_label.value

    if previous_label == corrected_label:
        return {"success": True, "message": "No change — corrected label matches existing label."}

    user = db.query(UserModel).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    provider_enum = EmailProviderEnum(
        user.email_provider if isinstance(user.email_provider, str) else user.email_provider.value
    )

    # Step 1 — update the stored row.
    record.relationship = corrected_label
    db.commit()

    # Step 2 + 3 — audit log + sender_memory update (with a fresh
    # embedding of the current combined_text if available; email content
    # isn't persisted, so we embed the sender identity + label pair as a
    # lightweight fallback signal when body text isn't available here).
    embedding_adapter = EmbeddingAdapter()
    correction_embedding = embedding_adapter.embed(f"{record.sender_email} {corrected_label}")

    apply_human_correction(
        email_id=email_id,
        sender_email=record.sender_email or "",
        user_id=user_id,
        previous_label=previous_label,
        corrected_label=corrected_label,
        email_embedding=correction_embedding,
        db=db,
    )

    # Step 4 — re-invoke apply_label to fix the real inbox label.
    email_provider = get_email_provider(provider_enum, access_token=access_token)
    provider_write_ok = False
    if email_provider.authenticate():
        result = apply_label(
            email_id=email_id, user_id=user_id, label=corrected_label,
            email_provider=email_provider, db=db,
        )
        provider_write_ok = result.provider_success
    else:
        logger.warning(f"correct-label: could not authenticate provider for user {user_id} to fix inbox label.")

    return {
        "success": True,
        "previous_label": previous_label,
        "corrected_label": corrected_label,
        "inbox_updated": provider_write_ok,
    }
