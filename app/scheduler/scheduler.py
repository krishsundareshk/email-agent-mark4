import os
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

logger = logging.getLogger(__name__)

BATCH_INTERVAL_MINUTES = int(os.getenv("BATCH_INTERVAL_MINUTES", "60"))
RECONCILIATION_INTERVAL_MINUTES = int(os.getenv("RECONCILIATION_INTERVAL_MINUTES", "120"))
THREAD_MEMORY_EXPIRY_INTERVAL_HOURS = int(os.getenv("THREAD_MEMORY_EXPIRY_INTERVAL_HOURS", "24"))
MEETING_PURGE_INTERVAL_HOURS = int(os.getenv("MEETING_PURGE_INTERVAL_HOURS", "24"))

# Single scheduler instance — shared across the app
scheduler = BackgroundScheduler(timezone="UTC")


def _job_listener(event):
    """
    Listen to scheduler events and log outcomes.
    EVENT_JOB_MISSED fires when max_instances=1 prevents overlap.
    """
    if event.exception:
        logger.error(
            f"Scheduler job failed: job_id={event.job_id}, "
            f"error={event.exception}"
        )
    elif hasattr(event, 'code'):
        from apscheduler.events import EVENT_JOB_MISSED
        if event.code == EVENT_JOB_MISSED:
            logger.warning(
                f"Scheduler job skipped — previous run still in progress: "
                f"job_id={event.job_id}. Overlapping run prevented."
            )
        else:
            logger.info(f"Scheduler job executed: job_id={event.job_id}")


def _run_batch_job(user_id: str) -> None:
    """
    Wrapper around run_batch_for_user() for the scheduler.
    Catches all exceptions so a crash never kills the scheduler.
    """
    from app.agents.orchestration.orchestrator import run_batch_for_user
    try:
        logger.info(f"Scheduler triggering batch run for user: {user_id}")
        result = run_batch_for_user(user_id)
        logger.info(
            f"Scheduler batch run complete for user {user_id}: "
            f"status={result.status}, "
            f"classified={result.emails_classified}, "
            f"meetings={result.meetings_detected}, "
            f"stage0_resolved={result.stage0_resolved}"
        )
    except Exception as e:
        logger.error(
            f"Scheduler batch job crashed for user {user_id}: {e}"
        )


def _run_label_reconciliation_job() -> None:
    """
    Wrapper around label_reconciliation_job (pipeline_changes §4/§9).
    Relies on each provider's own stored-credential fallback — see
    app/jobs/label_reconciliation_job.py docstring.
    """
    from app.jobs.label_reconciliation_job import run_label_reconciliation_job
    try:
        summary = run_label_reconciliation_job()
        logger.info(f"Scheduler label reconciliation complete: {summary}")
    except Exception as e:
        logger.error(f"Scheduler label reconciliation job crashed: {e}")


def _run_thread_memory_expiry_job() -> None:
    """Wrapper around thread_memory_expiry_job (specs v3 §5.2 — 30-day expiry)."""
    from app.jobs.thread_memory_expiry_job import run_thread_memory_expiry_job
    try:
        purged = run_thread_memory_expiry_job()
        logger.info(f"Scheduler thread-memory expiry complete: purged={purged}")
    except Exception as e:
        logger.error(f"Scheduler thread-memory expiry job crashed: {e}")


def _run_meeting_purge_job() -> None:
    """Wrapper around meeting_purge_job (specs v3 §6 — content-purge safety net)."""
    from app.jobs.meeting_purge_job import run_meeting_purge_job
    try:
        purged = run_meeting_purge_job()
        logger.info(f"Scheduler meeting purge complete: purged={purged}")
    except Exception as e:
        logger.error(f"Scheduler meeting purge job crashed: {e}")


def _load_active_users() -> list[str]:
    """
    Load all active user IDs from the database.
    Called once at scheduler start to register one job per user.
    TODO: For dynamic user addition, re-query DB on each trigger instead.
    """
    try:
        from app.db.database import SessionLocal
        from app.db.models import UserModel
        db = SessionLocal()
        try:
            users = db.query(UserModel.id).filter_by(is_active=True).all()
            return [u.id for u in users]
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to load active users for scheduler: {e}")
        return []


def start_scheduler() -> None:
    """
    Start the background scheduler.
    Registers one interval job per active user.

    Each job fires immediately on startup (next_run_time=now) so the
    dashboard is populated without waiting for the first interval to elapse.
    Each job has max_instances=1 — overlapping runs are skipped automatically.

    Called from FastAPI lifespan on startup.
    """
    if scheduler.running:
        logger.warning("Scheduler already running — skipping start.")
        return

    # Attach event listener for logging
    scheduler.add_listener(
        _job_listener,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )

    # Register one job per active user
    active_users = _load_active_users()

    if not active_users:
        logger.warning(
            "No active users found in DB. "
            "Scheduler started but no jobs registered. "
            "Seed a user and restart the app."
        )
    else:
        for user_id in active_users:
            scheduler.add_job(
                func=_run_batch_job,
                trigger="interval",
                minutes=BATCH_INTERVAL_MINUTES,
                id=f"batch_{user_id}",
                args=[user_id],
                max_instances=1,                           # prevents overlapping runs
                replace_existing=True,                     # safe to call start_scheduler() again
                misfire_grace_time=60,                     # seconds to still run if trigger was missed
                next_run_time=datetime.now(timezone.utc),  # fire immediately on startup
            )
            logger.info(
                f"Scheduled batch job for user {user_id} "
                f"every {BATCH_INTERVAL_MINUTES} minutes "
                f"(first run: immediate)."
            )

    scheduler.start()
    logger.info(
        f"Scheduler started. "
        f"{len(active_users)} job(s) registered. "
        f"Interval: {BATCH_INTERVAL_MINUTES} minutes."
    )

    # ── New scheduled/background jobs (pipeline_changes §9) ────────────
    scheduler.add_job(
        func=_run_label_reconciliation_job,
        trigger="interval",
        minutes=RECONCILIATION_INTERVAL_MINUTES,
        id="label_reconciliation",
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        func=_run_thread_memory_expiry_job,
        trigger="interval",
        hours=THREAD_MEMORY_EXPIRY_INTERVAL_HOURS,
        id="thread_memory_expiry",
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        func=_run_meeting_purge_job,
        trigger="interval",
        hours=MEETING_PURGE_INTERVAL_HOURS,
        id="meeting_purge",
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(
        f"Background jobs registered: label_reconciliation "
        f"(every {RECONCILIATION_INTERVAL_MINUTES}m), thread_memory_expiry "
        f"(every {THREAD_MEMORY_EXPIRY_INTERVAL_HOURS}h), meeting_purge "
        f"(every {MEETING_PURGE_INTERVAL_HOURS}h)."
    )


def stop_scheduler() -> None:
    """
    Stop the background scheduler gracefully.
    Waits for running jobs to complete before stopping.
    Called from FastAPI lifespan on shutdown.
    """
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped cleanly.")
    else:
        logger.info("Scheduler was not running — nothing to stop.")


def get_scheduler_status() -> dict:
    """
    Return scheduler status for GET /health endpoint (GH-025).
    """
    if not scheduler.running:
        return {"running": False, "jobs": 0}

    jobs = scheduler.get_jobs()
    return {
        "running": True,
        "jobs": len(jobs),
        "interval_minutes": BATCH_INTERVAL_MINUTES,
        "job_ids": [job.id for job in jobs],
    }