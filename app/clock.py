"""Local date/time in the configured timezone (STUDYVAULT_TZ). Everything that asks "what day is it" goes through here."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import settings


def now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz)).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()


def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
