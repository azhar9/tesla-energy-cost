from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tesla_energy_cost.periods import (
    build_segments,
    coverage_seconds,
    gaps,
    holidays,
    is_holiday,
    merge_ranges,
    parts_by_segment,
    period_for,
)

TORONTO = ZoneInfo("America/Toronto")
UTC = timezone.utc


def test_2026_holidays_match_oeb_schedule():
    assert holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 18),
        date(2026, 7, 1),
        date(2026, 8, 3),
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 12, 25),
        date(2026, 12, 28),
    }


def test_weekend_holiday_is_observed_on_next_weekday():
    # Boxing Day 2026 is a Saturday; the OEB observes Monday Dec 28.
    assert is_holiday(date(2026, 12, 28))
    # Christmas Day itself still has holiday pricing.
    assert is_holiday(date(2026, 12, 25))


def test_two_weekend_holidays_get_two_weekdays():
    # 2021: Christmas Saturday, Boxing Day Sunday -> Mon Dec 27 and Tue Dec 28.
    assert date(2021, 12, 27) in holidays(2021)
    assert date(2021, 12, 28) in holidays(2021)
    # 2022: Christmas Sunday, Boxing Day Monday -> Mon Dec 26 and Tue Dec 27.
    assert date(2022, 12, 26) in holidays(2022)
    assert date(2022, 12, 27) in holidays(2022)


def test_tou_periods():
    monday = datetime(2026, 11, 2, tzinfo=TORONTO)  # Monday
    assert period_for(monday.replace(hour=6, minute=59), "tou") == "off_peak"
    assert period_for(monday.replace(hour=7), "tou") == "on_peak"
    assert period_for(monday.replace(hour=10, minute=59), "tou") == "on_peak"
    assert period_for(monday.replace(hour=11), "tou") == "mid_peak"
    assert period_for(monday.replace(hour=16, minute=59), "tou") == "mid_peak"
    assert period_for(monday.replace(hour=17), "tou") == "on_peak"
    assert period_for(monday.replace(hour=19), "tou") == "off_peak"

    saturday = datetime(2026, 11, 7, 12, tzinfo=TORONTO)
    assert period_for(saturday, "tou") == "off_peak"
    assert period_for(saturday, "ulo") == "weekend_off_peak"


def test_ulo_periods():
    monday = datetime(2026, 11, 2, tzinfo=TORONTO)
    assert period_for(monday.replace(hour=23), "ulo") == "ultra_low_overnight"
    assert period_for(monday.replace(hour=3), "ulo") == "ultra_low_overnight"
    assert period_for(monday.replace(hour=7), "ulo") == "mid_peak"
    assert period_for(monday.replace(hour=16), "ulo") == "on_peak"
    assert period_for(monday.replace(hour=21), "ulo") == "mid_peak"


def test_dst_days_have_23_and_25_hours():
    spring_start = datetime(2026, 3, 8, tzinfo=TORONTO).astimezone(UTC)
    spring_end = datetime(2026, 3, 9, tzinfo=TORONTO).astimezone(UTC)
    fall_start = datetime(2026, 11, 1, tzinfo=TORONTO).astimezone(UTC)
    fall_end = datetime(2026, 11, 2, tzinfo=TORONTO).astimezone(UTC)

    assert (spring_end - spring_start).total_seconds() == 23 * 3600
    assert (fall_end - fall_start).total_seconds() == 25 * 3600

    spring = build_segments(spring_start, spring_end, "tou", TORONTO)
    fall = build_segments(fall_start, fall_end, "tou", TORONTO)
    assert sum(segment.seconds for segment in spring) == 23 * 3600
    assert sum(segment.seconds for segment in fall) == 25 * 3600
    assert spring[0].start == spring_start
    assert fall[-1].end == fall_end


def test_segments_are_contiguous_and_cover_range():
    start = datetime(2026, 11, 2, tzinfo=TORONTO).astimezone(UTC)
    end = datetime(2026, 11, 5, tzinfo=TORONTO).astimezone(UTC)
    segments = build_segments(start, end, "tou", TORONTO)
    assert segments
    for first, second in zip(segments, segments[1:]):
        assert first.end == second.start
    assert segments[0].start == start
    assert segments[-1].end == end


def test_interval_is_split_at_period_boundary():
    start = datetime(2026, 11, 2, 10, 0, tzinfo=TORONTO).astimezone(UTC)
    end = datetime(2026, 11, 2, 12, 0, tzinfo=TORONTO).astimezone(UTC)
    segments = build_segments(start, end, "tou", TORONTO)
    parts = parts_by_segment(start, end, 2.0, segments)
    by_period = {segment.period: kwh for segment, kwh in parts}
    assert by_period["on_peak"] == 1.0
    assert by_period["mid_peak"] == 1.0


def test_coverage_and_gaps():
    start = datetime(2026, 11, 2, tzinfo=UTC)
    end = start + timedelta(hours=10)
    ranges = merge_ranges(
        [
            (start, start + timedelta(hours=2)),
            (start + timedelta(hours=2), start + timedelta(hours=4)),
            (start + timedelta(hours=7), end),
        ]
    )
    assert coverage_seconds(ranges, start, end) == 7 * 3600
    assert gaps(ranges, start, end) == [
        (start + timedelta(hours=4), start + timedelta(hours=7))
    ]
