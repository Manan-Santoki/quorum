"""Time parsing / formatting helpers for the scheduler bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as dtparser


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_when(text: str, tz_name: str) -> datetime:
    """Parse a user-supplied date/time string into an aware UTC datetime.

    Naive inputs are interpreted in the guild timezone. Raises ValueError on
    unparseable input.
    """
    tz = get_tz(tz_name)
    dt = dtparser.parse(text, fuzzy=True)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def parse_duration_minutes(text: str | None, default: int = 60) -> int:
    """Parse a duration like '90', '1h', '1h30m', '45m' into minutes."""
    if not text:
        return default
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    minutes = 0
    num = ""
    for ch in text:
        if ch.isdigit():
            num += ch
        elif ch == "h":
            minutes += int(num or 0) * 60
            num = ""
        elif ch == "m":
            minutes += int(num or 0)
            num = ""
    if num:  # trailing bare number = minutes
        minutes += int(num)
    return minutes or default


def to_discord_ts(dt: datetime, style: str = "F") -> str:
    """Render an aware datetime as a Discord timestamp token (localises per viewer)."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def humanize_offset(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def fmt_local(dt: datetime, tz_name: str) -> str:
    return dt.astimezone(get_tz(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


# Zones shown in the "around the world" line on event embeds (label -> IANA).
_DISPLAY_ZONES = [
    ("UTC", "UTC"),
    ("US-Pacific", "America/Los_Angeles"),
    ("US-Arizona", "America/Phoenix"),
    ("US-Mountain", "America/Denver"),
    ("US-Central", "America/Chicago"),
    ("US-Eastern", "America/New_York"),
    ("UK", "Europe/London"),
    ("C-Europe", "Europe/Paris"),
    ("India", "Asia/Kolkata"),
]


def world_clock(dt_utc: datetime) -> str:
    """Compact multi-timezone rendering, e.g. 'India: Wed 1:30 AM IST · …'."""
    parts = []
    for label, iana in _DISPLAY_ZONES:
        local = dt_utc.astimezone(get_tz(iana))
        parts.append(f"**{label}** {local.strftime('%a %-I:%M %p %Z')}")
    return " · ".join(parts)


__all__ = [
    "get_tz",
    "parse_when",
    "parse_duration_minutes",
    "to_discord_ts",
    "humanize_offset",
    "fmt_local",
    "world_clock",
    "timedelta",
]
