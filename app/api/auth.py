import logging
from typing import Optional
from fastapi import Header, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

def get_google_access_token(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(security),
    x_google_token: Optional[str] = Header(None, alias="X-Google-Token")
) -> Optional[str]:
    """
    Extract the Google access token from HTTP headers.
    Checks 'Authorization: Bearer <token>' first, falls back to 'X-Google-Token'.
    """
    if authorization:
        return authorization.credentials
    return x_google_token

def get_provider_access_token(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(security),
    x_google_token: Optional[str] = Header(None, alias="X-Google-Token"),
    x_outlook_token: Optional[str] = Header(None, alias="X-Outlook-Token"),
) -> Optional[str]:
    """
    Provider-agnostic access token extraction (specs v3 §9.2 — two email
    providers, two calendar providers). Checks 'Authorization: Bearer' first,
    then whichever provider-specific header is present. Which provider the
    token belongs to is resolved separately, from UserModel.email_provider /
    calendar_provider — this dependency only extracts the raw token string.
    """
    if authorization:
        return authorization.credentials
    return x_google_token or x_outlook_token


def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id")
) -> str:
    """
    Extract the user ID from HTTP headers.
    If the user ID is an email address, resolve it to the database ID for consistency.
    Falls back to 'user_local_dev' for backward compatibility.
    """
    resolved_id = x_user_id or "user_local_dev"
    if resolved_id and "@" in resolved_id:
        from app.db.database import SessionLocal
        from app.db.models import UserModel
        db = SessionLocal()
        try:
            user = db.query(UserModel).filter_by(email=resolved_id).first()
            if user:
                return user.id
        except Exception as e:
            logger.error(f"Error resolving user ID by email: {e}")
        finally:
            db.close()
            
    return resolved_id
