from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tesla_energy_cost.billing import build_bill, plan_comparison
from tesla_energy_cost.rates import load_rates
from tesla_energy_cost.storage import Interval
from tesla_energy_cost.util import local_midnight, money

TORONTO = ZoneInfo("America/Toronto")


def _day(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    start = local_midnight(datetime(year, month, day).date(), TORONTO)
    return start, start + timedelta(days=1)


def test_single_mid_peak_hour_is_priced_exactly(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)  # Monday
    interval_start = datetime(2026, 11, 2, 12, 0, tzinfo=TORONTO)
    interval = Interval("test", interval_start, interval_start + timedelta(hours=1), 10.0)

    bill = build_bill(
        schedule=schedule,
        intervals=[interval],
        start=start,
        end=end,
        plan="tou",
    )

    ctx = schedule.context(datetime(2026, 11, 2).date())
    electricity = money(10.0 * 0.146)
    delivery = money(10.0 * ctx.delivery_per_kwh)
    regulatory = money(10.0 * ctx.regulatory_per_kwh)
    subtotal = money(electricity + delivery + regulatory)
    hst = money(subtotal * 0.13)
    oer = money(10.0 * (0.146 + ctx.delivery_per_kwh + ctx.regulatory_per_kwh) * 0.157)
    total = money(subtotal + hst - oer)

    assert bill.electricity == electricity
    assert bill.delivery == delivery
    assert bill.regulatory == regulatory
    assert bill.subtotal == subtotal
    assert bill.hst == hst
    assert bill.oer == oer
    assert bill.total == total
    assert bill.amount_due == total
    assert bill.energy_lines[0].period == "mid_peak"
    assert bill.energy_lines[0].kwh == 10.0
    assert bill.coverage == pytest.approx(1 / 24)
    assert any("Meter data covers" in item for item in bill.assumptions)


def test_interval_crossing_tou_boundary_is_split(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)
    interval = Interval(
        "test",
        datetime(2026, 11, 2, 10, 0, tzinfo=TORONTO),
        datetime(2026, 11, 2, 12, 0, tzinfo=TORONTO),
        2.0,
    )
    bill = build_bill(schedule=schedule, intervals=[interval], start=start, end=end, plan="tou")
    by_period = {line.period: line.kwh for line in bill.energy_lines}
    assert by_period == {"on_peak": 1.0, "mid_peak": 1.0}


def test_weekend_is_all_off_peak(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 7)  # Saturday
    interval = Interval(
        "test",
        datetime(2026, 11, 7, 12, 0, tzinfo=TORONTO),
        datetime(2026, 11, 7, 13, 0, tzinfo=TORONTO),
        5.0,
    )
    bill = build_bill(schedule=schedule, intervals=[interval], start=start, end=end, plan="tou")
    assert [line.period for line in bill.energy_lines] == ["off_peak"]
    assert bill.electricity == money(5.0 * 0.091)


def test_ulo_uses_ultra_low_rate_overnight(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 3)  # Tuesday
    interval = Interval(
        "test",
        datetime(2026, 11, 3, 0, 30, tzinfo=TORONTO),
        datetime(2026, 11, 3, 2, 30, tzinfo=TORONTO),
        4.0,
    )
    bill = build_bill(schedule=schedule, intervals=[interval], start=start, end=end, plan="ulo")
    assert [line.period for line in bill.energy_lines] == ["ultra_low_overnight"]
    assert bill.electricity == money(4.0 * 0.037)


def test_tiered_split_uses_household_total(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)
    interval = Interval("test", start, end, 100.0)
    bill = build_bill(
        schedule=schedule,
        intervals=[interval],
        start=start,
        end=end,
        plan="tiered",
        household_kwh=1200,
    )
    assert bill.electricity == money(100.0 * 0.132)
    assert [line.period for line in bill.energy_lines] == ["tier2"]

    bill_partial = build_bill(
        schedule=schedule,
        intervals=[interval],
        start=start,
        end=end,
        plan="tiered",
        household_kwh=1050,
    )
    by_period = {line.period: line.kwh for line in bill_partial.energy_lines}
    assert by_period == {"tier1": 50.0, "tier2": 50.0}


def test_tiered_without_household_total_warns(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)
    bill = build_bill(
        schedule=schedule,
        intervals=[Interval("test", start, end, 10.0)],
        start=start,
        end=end,
        plan="tiered",
    )
    assert bill.electricity == money(10.0 * 0.112)
    assert any("Tiered plan without" in item for item in bill.assumptions)


def test_proportional_method(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)
    bill = build_bill(
        schedule=schedule,
        intervals=[Interval("test", start, end, 80.0)],
        start=start,
        end=end,
        plan="tou",
        method="proportional",
        household_kwh=800,
        household_bill_total=150.0,
    )
    assert bill.proportional is not None
    assert bill.proportional.share == pytest.approx(0.1)
    assert bill.amount_due == money(15.0)
    assert bill.total != bill.amount_due


def test_coarse_reading_can_be_assigned_to_one_period(project):
    schedule = load_rates(project)
    start, end = _day(2026, 11, 2)
    interval = Interval("test", start, end, 30.0, quality="reading")
    bill = build_bill(
        schedule=schedule,
        intervals=[interval],
        start=start,
        end=end,
        plan="tou",
        assume_period="off_peak",
    )
    assert [line.period for line in bill.energy_lines] == ["off_peak"]
    assert bill.electricity == money(30.0 * 0.091)
    assert any("coarse readings" in item for item in bill.assumptions)


def test_plan_comparison(project):
    schedule = load_rates(project)
    rows = plan_comparison(
        schedule,
        off_peak_kwh=100,
        mid_peak_kwh=20,
        on_peak_kwh=10,
        day=datetime(2026, 11, 2).date(),
        household_kwh=800,
    )
    assert [row["plan"] for row in rows] == ["TOU", "ULO", "Tiered"]
    assert all(float(row["total"]) > 0 for row in rows)
