"""
apply_label — first-class tool, not a silent orchestrator side-effect
(specs v3 §5.5).

Dual-write: applies the label in the real inbox (Gmail/Outlook API) AND
records it in the local DB, so the dashboard and the actual inbox never
disagree by design. If one write fails, this is logged loudly — the
label_reconciliation_job (app/jobs/label_reconciliation_job.py) is the
cheap-insurance follow-up for exactly that failure mode.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.providers.email.base import EmailProvider
from app.db.models import ClassifiedEmailModel

logger = logging.getLogger(__name__)


@dataclass
class ApplyLabelToolResult:
    provider_success: bool
    db_success: bool
    provider_label: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def fully_consistent(self) -> bool:
        return self.provider_success and self.db_success


def apply_label(
    email_id: str,
    user_id: str,
    label: str,
    email_provider: EmailProvider,
    db: Session,
) -> ApplyLabelToolResult:
    """
    Dual-write `label` for `email_id`:
      1. Provider write — email_provider.apply_label(email_id, label)
      2. DB write — update the stored relationship on the ClassifiedEmailModel
         row, if one already exists (the reasoning_engine typically calls
         this tool right before persisting the row for the first time, in
         which case the DB half is a no-op here and save_classified_email
         does the DB write instead).
    """
    provider_result = email_provider.apply_label(email_id, label)

    db_success = True
    try:
        record = db.query(ClassifiedEmailModel).filter_by(
            email_id=email_id, user_id=user_id
        ).first()
        if record is not None:
            record.relationship = label
            db.commit()
    except Exception as e:
        db.rollback()
        db_success = False
        logger.error(f"apply_label DB write failed for {email_id}: {e}")

    if not provider_result.success:
        logger.warning(
            f"apply_label: provider write failed for {email_id} "
            f"(label={label}): {provider_result.error_message}. "
            f"DB and inbox may now be out of sync until reconciliation runs."
        )

    return ApplyLabelToolResult(
        provider_success=provider_result.success,
        db_success=db_success,
        provider_label=provider_result.provider_label,
        error_message=provider_result.error_message,
    )
