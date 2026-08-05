"""v3 clean-break: drop priority/summary/subject, add relationship/memory/correction schema

Revision ID: v3_clean_break
Revises:
Create Date: 2026-07-24

This is a deliberate CLEAN-BREAK cutover (pipeline_changes.md migration
note), not a forward migration of old classified_emails data. The old
priority/intent_type/summary/subject/classification_confidence/
flagged_for_review columns are not carried forward — the pipeline
re-processes email history under the new schema on its next run instead
(processed_emails / dedup tracking is also reset by this migration for
the same reason: the old dedup records refer to emails classified under
a taxonomy that no longer exists).

Tables dropped and recreated from scratch under the new shape:
  users, classified_emails, meeting_cards, batch_run_logs, processed_emails
Tables newly created:
  sender_memory, global_label_centroids, thread_memory, label_corrections
"""
from typing import Sequence, Union

from alembic import op

# Import the current ORM metadata so this revision always creates tables
# that exactly match app/db/models.py — a clean-break migration is only
# safe to hand-write once; keeping it metadata-driven avoids the table
# definitions here silently drifting from the ORM models over time.
from app.db.models import Base

revision: str = "v3_clean_break"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TABLES = [
    "users",
    "classified_emails",
    "meeting_cards",
    "batch_run_logs",
    "processed_emails",
]


def upgrade() -> None:
    bind = op.get_bind()

    # Drop the pre-v3 shape of these tables outright (clean break — see
    # module docstring). Safe no-ops if a table doesn't exist yet (fresh DB).
    Base.metadata.drop_all(bind=bind, tables=[
        t for t in Base.metadata.sorted_tables if t.name in _OLD_TABLES
    ])

    # Recreate everything (old tables under the new shape, plus the four
    # brand-new memory/correction tables) from the current ORM metadata.
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    # This is an intentional one-way clean-break cutover — there is no
    # supported downgrade path back to the pre-v3 (Priority-based) schema.
    raise NotImplementedError(
        "v3_clean_break has no downgrade path — restore from a pre-migration "
        "backup instead, per the migration note in pipeline_changes.md."
    )
