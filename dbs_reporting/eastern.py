"""Eastern Time (New York) with a 12-hour clock, for everything David shows.

ConnectWise sends times in UTC. The daylight-saving rule is worked out here rather than taken from
a time-zone database, because Python on Windows doesn't come with one."""

import re
from datetime import date, datetime, timedelta, timezone

# A ConnectWise timestamp, e.g. 2026-09-30T15:13:00Z
_UTC_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z\b")


def _sunday(year: int, month: int, nth: int) -> int:
    """Day of the month of the nth Sunday."""
    first = date(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + 7 * (nth - 1)


def to_eastern(dt: datetime) -> datetime:
    """An aware datetime in Eastern Time (EDT from the second Sunday of March to the first of November)."""
    dt = dt.astimezone(timezone.utc)
    starts = datetime(dt.year, 3, _sunday(dt.year, 3, 2), 7, tzinfo=timezone.utc)   # 2 AM EST
    ends = datetime(dt.year, 11, _sunday(dt.year, 11, 1), 6, tzinfo=timezone.utc)   # 2 AM EDT
    return dt.astimezone(timezone(timedelta(hours=-4 if starts <= dt < ends else -5)))


def now() -> datetime:
    return to_eastern(datetime.now(timezone.utc))


def clock(dt: datetime) -> str:
    """3:13 PM"""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def stamp(dt: datetime) -> str:
    """2026-09-30 11:13 AM ET"""
    local = to_eastern(dt)
    return f"{local:%Y-%m-%d} {clock(local)} ET"


def localize(text: str) -> str:
    """Rewrite every UTC timestamp in a tool result as Eastern Time. Midnight exactly is how
    ConnectWise stores plain dates (project start and end dates), so those stay plain dates."""
    def swap(m: re.Match) -> str:
        if m.group(2, 3, 4) == ("00", "00", "00"):
            return m.group(1)
        return stamp(datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}+00:00"))
    return _UTC_STAMP.sub(swap, text)
