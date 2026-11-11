"""Small shared helpers: time parsing, money rounding, formatting."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

UTC = timezone.utc

_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m/%d/%y %H:%M",
    "%m/%d/%y",
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{4}/\d{2}/\d{2}$|^\d{2}/\d{2}/\d{4}$|^\d{2}/\d{2}/\d{2}$")


class InputError(ValueError):
    """A user-facing input problem (bad timestamp, impossible range, ...)."""


def parse_datetime(value: str, tz: ZoneInfo, *, end_of_day: bool = False) -> datetime:
    """Parse a timestamp into an aware UTC datetime.

    Accepts ISO-8601 (with or without offset/Z), a handful of common CSV
    formats, and Unix epoch seconds/milliseconds. Naive timestamps are
    interpreted in the configured local timezone.

    If ``end_of_day`` is true and ``value`` is a bare date, the result is
    midnight at the *start of the next day*, which makes a bare ``--to`` date
    inclusive.
    """
    if value is None:
        raise InputError("missing timestamp")
    raw = str(value).strip()
    if not raw:
        raise InputError("empty timestamp")

    if re.fullmatch(r"\d{10}(\.\d+)?", raw):
        return datetime.fromtimestamp(float(raw), UTC)
    if re.fullmatch(r"\d{13}", raw):
        return datetime.fromtimestamp(int(raw) / 1000.0, UTC)

    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _NAIVE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        raise InputError(f"cannot parse timestamp: {value!r}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    if end_of_day and _DATE_ONLY.match(raw):
        dt = dt + timedelta(days=1)

    return dt.astimezone(UTC)


def iso_utc(dt: datetime) -> str:
    """Canonical UTC storage format, e.g. 2026-10-01T04:00:00Z."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_stored(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def money(value: float) -> float:
    """Round to cents the way a bill does (half-up, not banker's rounding)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt_kwh(value: float) -> str:
    return f"{value:,.2f}"


def fmt_cents(per_kwh: float) -> str:
    return f"{per_kwh * 100:.2f}\u00a2"


def human_hours(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h"


def previous_calendar_month(tz: ZoneInfo, today: date | None = None) -> tuple[datetime, datetime]:
    """Return [start, end) UTC for the previous calendar month in local time."""
    today = today or datetime.now(tz).date()
    first_this_month = date(today.year, today.month, 1)
    end = first_this_month
    if first_this_month.month == 1:
        start = date(first_this_month.year - 1, 12, 1)
    else:
        start = date(first_this_month.year, first_this_month.month - 1, 1)
    return local_midnight(start, tz), local_midnight(end, tz)


def month_label(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    s = start.astimezone(tz)
    e = (end - timedelta(seconds=1)).astimezone(tz)
    if (s.year, s.month) == (e.year, e.month):
        return s.strftime("%B %Y")
    return f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}"
