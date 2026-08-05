from app.agents.models.enums import CalendarProviderEnum
from app.providers.calendar.base import CalendarProvider
from app.providers.calendar.google_calendar_provider import GoogleCalendarProvider
from app.providers.calendar.outlook_calendar_provider import OutlookCalendarProvider


def get_calendar_provider(provider: CalendarProviderEnum, access_token: str = None) -> CalendarProvider:
    """
    Factory — resolves the configured CalendarProviderEnum to a concrete
    CalendarProvider implementation (specs v3 §9.2: exactly two).
    """
    if provider == CalendarProviderEnum.OUTLOOK:
        return OutlookCalendarProvider(access_token=access_token)
    return GoogleCalendarProvider(access_token=access_token)
