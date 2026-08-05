import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.db.models import BatchRunLogModel, UserModel
from app.agents.models.enums import BatchRunStatus
from app.api.auth import get_current_user_id, get_provider_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.get("/latest")
def get_latest_batch(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """Return the most recent BatchRunLog for the current user."""
    record = (
        db.query(BatchRunLogModel)
        .filter_by(user_id=user_id)
        .order_by(BatchRunLogModel.started_at.desc())
        .first()
    )
    if not record:
        return {"found": False}

    return {
        "found": True,
        "run_id": record.run_id,
        "status": record.status.value if hasattr(record.status, "value") else record.status,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "emails_fetched": record.emails_fetched,
        "emails_classified": record.emails_classified,
        "emails_failed": record.emails_failed,
        "emails_deferred": record.emails_deferred,
        "meetings_detected": record.meetings_detected,
        "stage0_resolved": record.stage0_resolved,
        "error_message": record.error_message,
    }


@router.post("/run")
def trigger_batch_run(
    access_token: Optional[str] = Depends(get_provider_access_token),
    user_id: str = Depends(get_current_user_id),
    x_ollama_model: Optional[str] = Header(None, alias="X-Ollama-Model"),
    db: Session = Depends(get_db)
):
    """
    Trigger the email processing batch pipeline manually for the current user.
    Uses the Google access token passed from the React frontend.
    """
    # Ensure user exists in the database
    user = db.query(UserModel).filter_by(id=user_id).first()
    if not user and "@" in user_id:
        user = db.query(UserModel).filter_by(email=user_id).first()
        if user:
            user_id = user.id
    if not user:
        logger.info(f"User {user_id} not found. Creating entry dynamically.")
        user = UserModel(
            id=user_id,
            email=user_id,  # Assumes user_id is email
            email_provider="gmail",
            calendar_provider="google",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    from app.agents.orchestration.orchestrator import run_batch_for_user

    logger.info(f"Triggering batch run for user={user_id} using model={x_ollama_model}")
    
    result = run_batch_for_user(
        user_id=user_id,
        access_token=access_token,
        ollama_model=x_ollama_model
    )
    
    return {
        "success": result.status in (BatchRunStatus.SUCCESS, BatchRunStatus.PARTIAL_FAILURE),
        "status": result.status.value if hasattr(result.status, "value") else result.status,
        "emails_fetched": result.emails_fetched,
        "emails_classified": result.emails_classified,
        "emails_failed": result.emails_failed,
        "meetings_detected": result.meetings_detected,
        "stage0_resolved": result.stage0_resolved,
        "error_message": result.error_message,
    }