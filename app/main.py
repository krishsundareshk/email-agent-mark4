from contextlib import asynccontextmanager
import os
import logging
import httpx
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app.api.meetings import router as meetings_router
from app.api.emails import router as emails_router
from app.api.batch import router as batch_router
from app.api.analytics import router as analytics_router
from app.db.database import init_db, seed_test_user, get_db
from app.db.models import UserModel
from app.api.auth import get_google_access_token, get_current_user_id
from app.scheduler.scheduler import start_scheduler, stop_scheduler

load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_test_user()
    if os.getenv("ENABLE_SCHEDULER", "false").lower() == "true":
        start_scheduler()
    yield
    if os.getenv("ENABLE_SCHEDULER", "false").lower() == "true":
        stop_scheduler()


app = FastAPI(
    title="Email Agentic System",
    description="Automated email classification and summary dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for Vite frontend (wildcard + credentials is invalid in browsers)
_cors_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8501",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in _cors_origins if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meetings_router)
app.include_router(emails_router)
app.include_router(batch_router)
app.include_router(analytics_router)


@app.get("/api/auth/config")
def get_auth_config():
    """Return the Google Client ID configured on the backend."""
    import json
    import glob
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        files = glob.glob("client_secret_*.json")
        if files:
            try:
                with open(files[0], "r") as f:
                    data = json.load(f)
                    client_type = "web" if "web" in data else "installed"
                    client_id = data[client_type]["client_id"]
            except Exception:
                pass
    return {"client_id": client_id or ""}


@app.get("/api/auth/outlook-config")
def get_outlook_auth_config():
    """Return the Microsoft Entra (Azure AD) app client ID configured on the backend."""
    return {
        "client_id": os.getenv("MICROSOFT_CLIENT_ID", ""),
        "tenant_id": os.getenv("MICROSOFT_TENANT_ID", "common"),
    }


@app.get("/health")
def health_check():
    from app.db.database import verify_db_connection
    from app.scheduler.scheduler import get_scheduler_status
    db_ok = verify_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "0.1.0",
        "database": db_ok,
        "scheduler": get_scheduler_status(),
    }


@app.get("/api/models")
def get_available_models():
    """List available models based on configured LLM_PROVIDER."""
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider == "openai":
        return {
            "models": [
                {"name": "gpt-4o-mini"},
                {"name": "gpt-4o"},
                {"name": "gpt-3.5-turbo"}
            ]
        }
    elif provider == "gemini":
        return {
            "models": [
                {"name": "gemini-1.5-flash"},
                {"name": "gemini-2.5-flash"},
                {"name": "gemini-1.5-pro"}
            ]
        }
    else:
        # Default Ollama
        try:
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            resp = httpx.get(f"{ollama_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {
            "models": [
                {"name": "qwen3:8b"},
                {"name": "qwen2.5-coder:7b"}
            ]
        }


@app.get("/api/user/profile")
def get_user_profile(
    access_token: str = Depends(get_google_access_token),
    x_outlook_token: str = Header(None, alias="X-Outlook-Token"),
    db: Session = Depends(get_db),
):
    """
    Verify the request's provider access token against the real provider's
    userinfo endpoint (Google or Microsoft Graph — specs v3 §9.2), get or
    create the corresponding local user, and return their profile info.

    Provider is inferred from which token header is present: X-Outlook-Token
    means Microsoft Graph; Authorization/X-Google-Token means Google.
    """
    if x_outlook_token:
        try:
            resp = httpx.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {x_outlook_token}"},
                timeout=5
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail=f"Invalid Outlook access token: {resp.text}")

            info = resp.json()
            email = info.get("mail") or info.get("userPrincipalName")
            if not email:
                raise HTTPException(status_code=400, detail="Email address not returned by Microsoft Graph")
            name = info.get("displayName", email)
            picture = None
            provider = "outlook"
            calendar_provider = "outlook"
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to retrieve Outlook user profile: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch profile: {str(e)}")
    else:
        if not access_token:
            raise HTTPException(status_code=401, detail="Missing access token in headers")
        try:
            resp = httpx.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail=f"Invalid Google access token: {resp.text}")

            info = resp.json()
            email = info.get("email")
            if not email:
                raise HTTPException(status_code=400, detail="Email address not returned by Google")
            name = info.get("name", email)
            picture = info.get("picture")
            provider = "gmail"
            calendar_provider = "google"
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to retrieve Google user profile: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch profile: {str(e)}")

    try:
        # Get or create user in local DB
        user = db.query(UserModel).filter_by(email=email).first()
        if not user:
            logger.info(f"Registering new user in local DB: {email} (provider={provider})")
            user = UserModel(
                id=email,
                email=email,
                email_provider=provider,
                calendar_provider=calendar_provider,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        return {
            "user_id": user.id,
            "email": user.email,
            "name": name,
            "picture": picture,
            "email_provider": user.email_provider.value if hasattr(user.email_provider, "value") else user.email_provider,
            "calendar_provider": user.calendar_provider.value if hasattr(user.calendar_provider, "value") else user.calendar_provider,
            "is_active": user.is_active,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve user profile: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch profile: {str(e)}")


@app.post("/api/user/reset")
def reset_user_data(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Delete all database history (emails, meetings, logs, processed history,
    memory) for the current active user to allow a fresh initial synchronization.
    """
    from app.db.models import (
        ClassifiedEmailModel, MeetingCardModel, BatchRunLogModel, ProcessedEmailModel,
        SenderMemoryModel, ThreadMemoryModel, LabelCorrectionModel, GlobalLabelCentroidModel,
    )
    try:
        logger.info(f"Clearing database history for user_id={user_id}")
        db.query(ClassifiedEmailModel).filter_by(user_id=user_id).delete()
        db.query(MeetingCardModel).filter_by(user_id=user_id).delete()
        db.query(ProcessedEmailModel).filter_by(user_id=user_id).delete()
        db.query(BatchRunLogModel).filter_by(user_id=user_id).delete()
        db.query(SenderMemoryModel).filter_by(user_id=user_id).delete()
        db.query(ThreadMemoryModel).filter_by(user_id=user_id).delete()
        db.query(LabelCorrectionModel).filter_by(user_id=user_id).delete()
        db.query(GlobalLabelCentroidModel).filter_by(user_id=user_id).delete()
        db.commit()
        return {"success": True, "message": "All database records for this account cleared successfully."}
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to clear database records: {e}")
        raise HTTPException(status_code=500, detail=f"Database reset failed: {str(e)}")


class UpdateProvidersRequest(BaseModel):
    email_provider: Optional[str] = None       # "gmail" | "outlook"
    calendar_provider: Optional[str] = None     # "google" | "outlook"
    org_domains: Optional[str] = None           # comma-separated, used for Internal relationship match


@app.patch("/api/user/providers")
def update_user_providers(
    body: UpdateProvidersRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Set which of the two supported email/calendar providers this user uses
    (specs v3 §9.2), and their org domain(s) for the Internal relationship
    domain-match signal (specs v3 §3's structural-corroboration check).
    """
    from app.agents.models.enums import EmailProviderEnum, CalendarProviderEnum

    user = db.query(UserModel).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if body.email_provider is not None:
        try:
            user.email_provider = EmailProviderEnum(body.email_provider).value
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid email_provider: {body.email_provider}")
    if body.calendar_provider is not None:
        try:
            user.calendar_provider = CalendarProviderEnum(body.calendar_provider).value
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid calendar_provider: {body.calendar_provider}")
    if body.org_domains is not None:
        user.org_domains = body.org_domains

    db.commit()
    return {
        "success": True,
        "email_provider": user.email_provider,
        "calendar_provider": user.calendar_provider,
        "org_domains": user.org_domains,
    }