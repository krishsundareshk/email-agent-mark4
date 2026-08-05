from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_serializer

class EmailObject(BaseModel):
    model_config = ConfigDict()

    email_id: str
    thread_id: Optional[str] = None
    sender_name: str
    sender_email: str
    recipients: list[str]
    subject: str
    body_text: str
    timestamp: datetime
    has_attachments: bool = False
    # Stage 0 (specs v3 §2) needs these RFC bulk-mail headers, never a
    # hand-maintained keyword/sender blocklist.
    list_unsubscribe: Optional[str] = None
    list_unsubscribe_post: Optional[str] = None
    precedence: Optional[str] = None

    @field_serializer("timestamp")
    def serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat()


class EmailProvider(ABC):
    """
    Abstract base class for all email providers.
    
    Any provider (Gmail, Outlook, etc.) must implement all three methods below.
    The pipeline never imports GmailProvider or OutlookProvider directly —
    it always works through this interface.
    
    To add a new provider:
    1. Create a new file under app/providers/email/
    2. Subclass EmailProvider
    3. Implement all three abstract methods
    4. No other file in the codebase needs to change
    """

    @abstractmethod
    def authenticate(self) -> bool:
        """
        Authenticate with the email provider.
        Returns True if authentication succeeded, False otherwise.
        Must be called before fetch_emails().
        """
        pass

    @abstractmethod
    def fetch_emails(
        self,
        since: datetime,
        until: datetime
    ) -> list[EmailObject]:
        """
        Fetch all emails within the given time window.
        
        Args:
            since: Start of the batch window (inclusive)
            until: End of the batch window (inclusive)
        
        Returns:
            List of EmailObject instances, one per email.
            Returns an empty list if no emails found in the window.
        
        Raises:
            EmailFetchError: If the API call fails for any reason.
        """
        pass

    @abstractmethod
    def mark_processed(self, email_id: str) -> None:
        """
        Record that this email has been processed in the current system.
        Does NOT modify the email in the provider (no read/unread changes).
        This is purely a local tracking mechanism to prevent reprocessing.
        
        Args:
            email_id: The provider-assigned unique ID of the email.
        """
        pass

    @abstractmethod
    def apply_label(self, email_id: str, label: str) -> "ApplyLabelResult":
        """
        Apply a namespaced label to a message in the real inbox
        (specs v3 §5.5, §10 — "Agent/Internal", "Agent/Promotional", etc).

        Gmail's label API and Outlook's category/folder model aren't
        identical — each concrete provider decides explicitly how it
        represents "however this provider represents a label" (pipeline
        changes §5). This is one half of apply_label's dual-write; the
        other half (the local DB write) is handled by the apply_label
        tool in app/agents/tools/apply_label_tool.py, never here.

        Never raises — failures are returned as ApplyLabelResult(success=False)
        so the reconciliation job (label_reconciliation_job) can detect and
        repair drift between the provider and the local DB.
        """
        pass


    @abstractmethod
    def get_current_label(self, email_id: str) -> Optional[str]:
        """
        Return the current Agent/-namespaced label applied to this message
        in the real inbox, or None if no agent label is present.

        Used exclusively by label_reconciliation_job to detect drift
        between the provider's real state and the local DB (specs v3 §5.5).
        Never raises — returns None on any lookup failure.
        """
        pass


class ApplyLabelResult(BaseModel):
    """Result of a single-provider label write. Never raises; see apply_label()."""
    model_config = ConfigDict()
    success: bool
    provider_label: Optional[str] = None
    error_message: Optional[str] = None


class EmailFetchError(Exception):
    """
    Raised when an email provider fails to fetch emails.
    Wraps the underlying provider error with context.
    
    Usage:
        raise EmailFetchError("Gmail API rate limit exceeded", original_error=e)
    """
    def __init__(self, message: str, original_error: Exception = None):
        super().__init__(message)
        self.original_error = original_error