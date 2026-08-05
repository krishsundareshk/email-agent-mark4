import os
import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from app.db.models import Base, UserModel

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Engine and Session Factory
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./email_agent.db")

# connect_args only needed for SQLite — allows multiple threads to share
# the same connection (required for FastAPI's async context)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,   # Set True to log all SQL statements for debugging
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def get_db() -> Session:
    """
    FastAPI dependency — yields a DB session and closes it after the request.
    SessionLocal is looked up dynamically so tests can patch db_module.SessionLocal.
    """
    import app.db.database as _self
    db = _self.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Database Initialization
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create all tables if they do not already exist.
    Safe to call multiple times — never drops existing data.
    Called on app startup via FastAPI lifespan event, and used directly by
    tests / a fresh local dev DB.

    NOTE (specs v3 migration note): this is a dev/test convenience only,
    equivalent to `alembic upgrade head` from an empty DB. Any existing
    deployment with the pre-v3 schema (priority/summary/subject columns)
    must go through `alembic upgrade head` — that revision is a genuine
    clean-break cutover (drops the old columns, adds the new tables), not
    a forward migration of the old data. See alembic/versions/ and
    DEPLOYMENT.md for the upgrade procedure. The old sqlite3 ALTER TABLE
    self-migration hack that lived here has been removed for that reason.
    """
    logger.info("Initializing database...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created (or already exist).")


def seed_test_user() -> None:
    """
    Insert a test user record for local development if one doesn't exist.
    Safe to call multiple times — checks before inserting.

    Test user matches the GMAIL_EMAIL from .env.
    """
    test_user_id = "user_local_dev"
    test_email = os.getenv("GMAIL_EMAIL", "test@example.com")

    db = SessionLocal()
    try:
        existing = db.query(UserModel).filter_by(id=test_user_id).first()
        if existing:
            logger.info(f"Test user already exists: {existing.email}")
            return

        test_user = UserModel(
            id=test_user_id,
            email=test_email,
            email_provider=os.getenv("DEFAULT_EMAIL_PROVIDER", "gmail"),
            calendar_provider=os.getenv("DEFAULT_CALENDAR_PROVIDER", "google"),
            org_domains=os.getenv("ORG_DOMAINS", ""),
        )
        db.add(test_user)
        db.commit()
        logger.info(f"Test user seeded: id={test_user_id}, email={test_email}")

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to seed test user: {e}")
    finally:
        db.close()


def verify_db_connection() -> bool:
    """
    Verify the database is reachable.
    Used by GET /health endpoint (GH-025).
    Returns True if connection succeeds, False otherwise.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False