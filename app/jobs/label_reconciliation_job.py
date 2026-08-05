"""
label_reconciliation_job — periodic sweep comparing local DB labels
against real Gmail/Outlook labels (pipeline_changes §1, §4, §9).

apply_label's dual-write can partially fail (provider write succeeds,
DB write fails, or vice versa — see app/agents/tools/apply_label_tool.py).
This job is the cheap-insurance follow-up: for each recently-classified
email, re-read the provider's current label and repair whichever side
drifted, favoring the DB's relationship as the source of truth (it's the
Stage 1 decision; if the provider write silently failed, we retry it).
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import ClassifiedEmailModel, UserModel
from app.agents.models.enums import EmailProviderEnum
from app.providers.email import get_email_provider

logger = logging.getLogger(__name__)

RECONCILIATION_LOOKBACK_HOURS = 48


def run_label_reconciliation_job(access_tokens: Optional[dict[str, str]] = None) -> dict:
    """
    access_tokens: optional {user_id: access_token} override for callers
    that already hold a live token (e.g. right after an interactive batch
    run). When omitted, each provider falls back to its own stored
    credentials — the same pattern GmailProvider already uses for
    unattended scheduled runs (GOOGLE_TOKEN_PATH).

    Returns a summary dict: {checked, repaired, still_drifted}.
    """
    access_tokens = access_tokens or {}
    db = SessionLocal()
    checked = repaired = still_drifted = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=RECONCILIATION_LOOKBACK_HOURS)
        users = db.query(UserModel).filter_by(is_active=True).all()

        for user in users:
            token = access_tokens.get(user.id)  # None is fine — see docstring

            provider_enum = EmailProviderEnum(
                user.email_provider if isinstance(user.email_provider, str)
                else user.email_provider.value
            )
            email_provider = get_email_provider(provider_enum, access_token=token)
            if not email_provider.authenticate():
                logger.warning(f"[reconciliation] auth failed for user {user.id}, skipping.")
                continue

            recent = (
                db.query(ClassifiedEmailModel)
                .filter(ClassifiedEmailModel.user_id == user.id)
                .filter(ClassifiedEmailModel.processed_at >= cutoff)
                .all()
            )

            for record in recent:
                checked += 1
                current = email_provider.get_current_label(record.email_id)
                expected = f"Agent/{record.relationship}"

                if current == expected:
                    continue

                # Drift detected — retry the provider write, DB is source of truth.
                result = email_provider.apply_label(record.email_id, record.relationship)
                if result.success:
                    repaired += 1
                    logger.info(
                        f"[reconciliation] repaired drift for {record.email_id} "
                        f"(had={current}, now={expected})"
                    )
                else:
                    still_drifted += 1
                    logger.warning(
                        f"[reconciliation] could not repair {record.email_id}: "
                        f"{result.error_message}"
                    )

        logger.info(
            f"[reconciliation] complete: checked={checked}, repaired={repaired}, "
            f"still_drifted={still_drifted}"
        )
        return {"checked": checked, "repaired": repaired, "still_drifted": still_drifted}
    finally:
        db.close()
