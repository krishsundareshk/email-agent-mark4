from app.agents.models.enums import EmailProviderEnum
from app.providers.email.base import EmailProvider
from app.providers.email.gmail_provider import GmailProvider
from app.providers.email.outlook_provider import OutlookProvider


def get_email_provider(provider: EmailProviderEnum, access_token: str = None) -> EmailProvider:
    """
    Factory — resolves the configured EmailProviderEnum to a concrete
    EmailProvider implementation (specs v3 §9.2: exactly two).
    """
    if provider == EmailProviderEnum.OUTLOOK:
        return OutlookProvider(access_token=access_token)
    return GmailProvider(access_token=access_token)
