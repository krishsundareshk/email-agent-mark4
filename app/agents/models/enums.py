from enum import Enum


class RelationshipLabel(str, Enum):
    """
    The primary, mutually-exclusive classification dimension (specs v3 §1.1).
    Every email gets exactly one. Replaces the old IntentType enum.

    Promotional        → decided deterministically by Stage 0, never by the LLM.
    Internal/Client/Vendor/Automated-System/Unknown-External/Suspicious
                        → decided by Stage 1 (the reasoning engine).
    """
    PROMOTIONAL = "Promotional"
    INTERNAL = "Internal"
    CLIENT = "Client"
    VENDOR = "Vendor"
    AUTOMATED_SYSTEM = "Automated-System"
    UNKNOWN_EXTERNAL = "Unknown-External"
    SUSPICIOUS = "Suspicious"


class Department(str, Enum):
    """
    Optional secondary metadata (specs v3 §1.3). Unchanged from the prior design.
    """
    HR = "HR"
    FINANCE = "Finance"
    IT = "IT"
    LEGAL = "Legal"
    OPERATIONS = "Operations"
    GENERAL = "General"


class ConfidenceTier(str, Enum):
    """
    Outcome of the Stage 3 confidence rubric (specs v3 §4).
    Replaces the old float classification_confidence + flagged_for_review pair.

    auto-applied → all signals agreed, label applied with no review needed
    needs-review → applied, but flagged for a human to double check
    unclassified → held back; low corroboration/certainty, high ambiguity
    """
    AUTO_APPLIED = "auto-applied"
    NEEDS_REVIEW = "needs-review"
    UNCLASSIFIED = "unclassified"


class TrustTier(str, Enum):
    """
    Sender trust tier derived from long-term memory (specs v3 §5.1).

    New      → fewer than 5 emails seen from this sender
    Familiar → 5-20 emails seen
    Trusted  → 20+ emails seen, with a low correction rate
    """
    NEW = "New"
    FAMILIAR = "Familiar"
    TRUSTED = "Trusted"


class MeetingStatus(str, Enum):
    """
    Lifecycle state of a MeetingCard.

    Pending   → surfaced on dashboard, awaiting user decision
    Added     → user confirmed, event written to calendar
    Dismissed → user dismissed, no calendar action taken
    """
    PENDING = "Pending"
    ADDED = "Added"
    DISMISSED = "Dismissed"


class EmailProviderEnum(str, Enum):
    """
    Supported email providers (specs v3 §9.2) — exactly two, by design.
    """
    GMAIL = "gmail"
    OUTLOOK = "outlook"


class CalendarProviderEnum(str, Enum):
    """
    Supported calendar providers (specs v3 §9.2) — exactly two, by design.

    google  → Google Calendar API
    outlook → Microsoft Graph Calendar API
    """
    GOOGLE = "google"
    OUTLOOK = "outlook"


class BatchRunStatus(str, Enum):
    """
    Overall status of a batch pipeline run.

    Running        → batch is currently in progress
    Success        → all emails processed without errors
    PartialFailure → some emails failed but run completed
    Failed         → run aborted due to a critical error (e.g. API auth failure)
    """
    RUNNING = "Running"
    SUCCESS = "Success"
    PARTIAL_FAILURE = "PartialFailure"
    FAILED = "Failed"
