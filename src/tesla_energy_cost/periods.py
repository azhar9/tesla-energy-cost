"""Ontario TOU / ULO price periods, statutory holidays, and interval splitting.

All timestamps are handled in UTC. Price periods are defined in local
wall-clock time (America/Toronto) and converted, so DST transition days are
handled correctly (a 23-hour or 25-hour day).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc

TOU_PERIODS = ("off_peak", "mid_peak", "on_peak")
ULO_PERIODS = ("ultra_low_overnight", "weekend_off_peak", "mid_peak", "on_peak")
PERIOD_LABELS = {
    "off_peak": "Off-peak",
    "mid_peak": "Mid-peak",
    "on_peak": "On-peak",
    "ultra_low_overnight": "Ultra-low overnight",
    "weekend_off_peak": "Weekend/holiday off-peak",
    "tier1": "Tier 1",
    "tier2": "Tier 2",
    "tiered": "Tiered",
}


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday of a month (weekday: Monday=0)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def victoria_day(year: int) -> date:
    """Monday immediately preceding May 25."""
    d = date(year, 5, 24)
    return d - timedelta(days=d.weekday())


@lru_cache(maxsize=32)
def _base_holidays(year: int) -> frozenset[date]:
    """Ontario holidays on which TOU off-peak / ULO low prices apply all day.

    Includes the Civic Holiday, which the OEB treats as a holiday for TOU/ULO
    pricing even though it is not a statutory holiday under the ESA.
    """
    return frozenset(
        {
            date(year, 1, 1),                 # New Year's Day
            _nth_weekday(year, 2, 0, 3),      # Family Day (3rd Monday)
            easter_sunday(year) - timedelta(days=2),  # Good Friday
            victoria_day(year),               # Victoria Day
            date(year, 7, 1),                 # Canada Day
            _nth_weekday(year, 8, 0, 1),      # Civic Holiday (1st Monday)
            _nth_weekday(year, 9, 0, 1),      # Labour Day (1st Monday)
            _nth_weekday(year, 10, 0, 2),     # Thanksgiving (2nd Monday)
            date(year, 12, 25),               # Christmas Day
            date(year, 12, 26),               # Boxing Day
        }
    )


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset[date]:
    """Holiday dates with the OEB weekend-observation rule applied.

    "If a holiday falls on a weekend, the next weekday (that is not also a
    holiday) will have the holiday prices in effect all day."
    """
    base = _base_holidays(year)
    observed: set[date] = set()
    for d in sorted(base):
        if d.weekday() < 5 and d not in observed:
            observed.add(d)
            continue
        candidate = d
        while True:
            candidate += timedelta(days=1)
            if (
                candidate.weekday() < 5
                and candidate not in base
                and candidate not in observed
            ):
                observed.add(candidate)
                break
    return frozenset(observed)


def is_holiday(day: date) -> bool:
    return day in holidays(day.year)


def is_off_peak_day(day: date) -> bool:
    return day.weekday() >= 5 or is_holiday(day)


def period_for(local_dt: datetime, plan: str) -> str:
    """Price period for a local wall-clock time."""
    if plan == "tiered":
        return "tiered"

    hour = local_dt.hour
    off_day = is_off_peak_day(local_dt.date())

    if plan == "ulo":
        if hour >= 23 or hour < 7:
            return "ultra_low_overnight"
        if off_day:
            return "weekend_off_peak"
        if 7 <= hour < 16:
            return "mid_peak"
        if 16 <= hour < 21:
            return "on_peak"
        return "mid_peak"  # 21:00-23:00

    if plan == "tou":
        if off_day:
            return "off_peak"
        if 7 <= hour < 11:
            return "on_peak"
        if 11 <= hour < 17:
            return "mid_peak"
        if 17 <= hour < 19:
            return "on_peak"
        return "off_peak"

    raise ValueError(f"unknown plan: {plan!r}")


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    period: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


# Boundaries cover TOU (7/11/17/19), ULO (7/16/21/23) and midnight.
_BOUNDARIES = (0, 7, 11, 16, 17, 19, 21, 23, 24)


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def build_segments(start: datetime, end: datetime, plan: str, tz: ZoneInfo) -> list[Segment]:
    """Split [start, end) into contiguous price-period segments."""
    if start >= end:
        return []
    if plan == "tiered":
        return [Segment(start, end, "tiered")]

    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    day = local_start.date() - timedelta(days=1)
    last_day = local_end.date() + timedelta(days=1)

    raw: list[Segment] = []
    while day <= last_day:
        midnight = _local_midnight(day, tz)
        for i in range(len(_BOUNDARIES) - 1):
            s_local = midnight + timedelta(hours=_BOUNDARIES[i])
            e_local = midnight + timedelta(hours=_BOUNDARIES[i + 1])
            s = s_local.astimezone(UTC)
            e = e_local.astimezone(UTC)
            if e <= s:
                continue
            raw.append(Segment(s, e, period_for(s_local, plan)))
        day += timedelta(days=1)

    raw.sort(key=lambda seg: seg.start)

    clipped: list[Segment] = []
    for seg in raw:
        s = max(seg.start, start)
        e = min(seg.end, end)
        if e <= s:
            continue
        if clipped and clipped[-1].period == seg.period and clipped[-1].end == s:
            clipped[-1] = Segment(clipped[-1].start, e, seg.period)
        else:
            clipped.append(Segment(s, e, seg.period))
    return clipped


def split_energy(
    interval_start: datetime,
    interval_end: datetime,
    kwh: float,
    segments: list[Segment],
) -> dict[str, float]:
    """Allocate interval energy across price periods.

    Assumes constant power inside the interval. For a 1-minute poller the
    error at a period boundary is at most one poll's worth of energy; for
    coarse data the caller should supply ``assume_period``.
    """
    total_seconds = (interval_end - interval_start).total_seconds()
    if total_seconds <= 0 or kwh <= 0:
        return {}
    if not segments:
        return {}

    starts = [seg.start for seg in segments]
    i = max(0, bisect_right(starts, interval_start) - 1)
    out: dict[str, float] = {}

    while i < len(segments):
        seg = segments[i]
        if seg.start >= interval_end:
            break
        overlap = (
            min(interval_end, seg.end) - max(interval_start, seg.start)
        ).total_seconds()
        if overlap > 0:
            out[seg.period] = out.get(seg.period, 0.0) + kwh * overlap / total_seconds
        i += 1

    return out


def parts_by_segment(
    interval_start: datetime,
    interval_end: datetime,
    kwh: float,
    segments: list[Segment],
) -> list[tuple[Segment, float]]:
    """Like split_energy, but keeps the segment (and therefore the date)."""
    total_seconds = (interval_end - interval_start).total_seconds()
    if total_seconds <= 0 or kwh <= 0 or not segments:
        return []

    starts = [seg.start for seg in segments]
    i = max(0, bisect_right(starts, interval_start) - 1)
    out: list[tuple[Segment, float]] = []

    while i < len(segments):
        seg = segments[i]
        if seg.start >= interval_end:
            break
        overlap = (
            min(interval_end, seg.end) - max(interval_start, seg.start)
        ).total_seconds()
        if overlap > 0:
            out.append((seg, kwh * overlap / total_seconds))
        i += 1

    return out


def merge_ranges(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent [start, end) ranges."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def coverage_seconds(
    ranges: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float:
    clipped = [(max(s, start), min(e, end)) for s, e in ranges if e > start and s < end]
    clipped = [(s, e) for s, e in clipped if e > s]
    return sum((e - s).total_seconds() for s, e in merge_ranges(clipped))


def gaps(
    ranges: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    merged = merge_ranges(
        [(max(s, start), min(e, end)) for s, e in ranges if e > start and s < end]
    )
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    for s, e in merged:
        if s > cursor:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        out.append((cursor, end))
    return out
