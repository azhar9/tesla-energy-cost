"""Turn metered intervals into a landlord-ready electricity statement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from .periods import (
    PERIOD_LABELS,
    Segment,
    build_segments,
    coverage_seconds,
    gaps as find_gaps,
    merge_ranges,
    parts_by_segment,
)
from .rates import PriceContext, RateSchedule
from .storage import Interval
from .util import fmt_money, money


class BillingError(RuntimeError):
    pass


PERIOD_ORDER = {
    "tou": ["off_peak", "mid_peak", "on_peak"],
    "ulo": ["ultra_low_overnight", "weekend_off_peak", "mid_peak", "on_peak"],
    "tiered": ["tier1", "tier2"],
}

VALID_PERIODS = {
    "tou": {"off_peak", "mid_peak", "on_peak"},
    "ulo": {"ultra_low_overnight", "weekend_off_peak", "mid_peak", "on_peak"},
    "tiered": {"tier1", "tier2"},
}


@dataclass
class EnergyLine:
    period: str
    rate: float
    kwh: float
    amount: float
    effective_from: str = ""

    @property
    def label(self) -> str:
        return PERIOD_LABELS.get(self.period, self.period)


@dataclass
class ProportionalShare:
    share: float
    household_kwh: float
    household_bill_total: float
    amount: float


@dataclass
class Bill:
    start: datetime
    end: datetime
    plan: str
    method: str
    kwh: float
    energy_lines: list[EnergyLine]
    electricity: float
    delivery: float
    regulatory: float
    subtotal: float
    hst: float
    oer: float
    total: float
    delivery_per_kwh: float
    regulatory_per_kwh: float
    loss_factor: float
    hst_rate: float
    oer_rate: float
    coverage: float
    gaps: list[tuple[datetime, datetime]]
    interval_count: int
    coarse_kwh: float
    context: PriceContext
    proportional: ProportionalShare | None = None
    assumptions: list[str] = field(default_factory=list)

    @property
    def effective_rate(self) -> float:
        return self.total / self.kwh if self.kwh else 0.0

    @property
    def amount_due(self) -> float:
        if self.method == "proportional" and self.proportional:
            return self.proportional.amount
        return self.total

    @property
    def commodity_rate_avg(self) -> float:
        return self.electricity / self.kwh if self.kwh else 0.0


def _clip_kwh(interval: Interval, start: datetime, end: datetime) -> float:
    if interval.end <= start or interval.start >= end:
        return 0.0
    total = interval.seconds
    if total <= 0:
        return 0.0
    overlap = (min(interval.end, end) - max(interval.start, start)).total_seconds()
    return interval.kwh * overlap / total if overlap > 0 else 0.0


def build_bill(
    *,
    schedule: RateSchedule,
    intervals: Iterable[Interval],
    start: datetime,
    end: datetime,
    plan: str,
    method: str = "marginal",
    assume_period: str | None = None,
    household_kwh: float | None = None,
    household_bill_total: float | None = None,
) -> Bill:
    intervals = [i for i in intervals if i.end > start and i.start < end and i.kwh > 0]
    if not intervals:
        raise BillingError(
            "no meter data in this period. Import a CSV, add a reading, or run "
            "the poller first."
        )

    if plan not in PERIOD_ORDER:
        raise BillingError(f"unknown plan {plan!r}")
    if assume_period and assume_period not in VALID_PERIODS[plan]:
        raise BillingError(
            f"--assume-period {assume_period!r} is not valid for plan {plan!r}; "
            f"choose one of {sorted(VALID_PERIODS[plan])}"
        )

    from zoneinfo import ZoneInfo

    timezone = ZoneInfo(schedule.timezone)

    total_kwh = sum(_clip_kwh(i, start, end) for i in intervals)
    if total_kwh <= 0:
        raise BillingError("meter data exists but contains zero energy in this period")

    contexts = [schedule.context(d) for d in _dates_touched(start, end, timezone)]
    context = contexts[-1]

    raw_coverage = merge_ranges([(i.start, i.end) for i in intervals])
    covered = coverage_seconds(raw_coverage, start, end)
    total_seconds = (end - start).total_seconds()
    coverage = covered / total_seconds if total_seconds else 0.0
    gap_list = [g for g in find_gaps(raw_coverage, start, end) if (g[1] - g[0]).total_seconds() >= 300]
    coarse_kwh = sum(i.kwh for i in intervals if i.is_coarse)

    assumptions: list[str] = []
    if coverage < 0.98:
        assumptions.append(
            f"Meter data covers {coverage:.1%} of the period; missing time is billed at $0."
        )
    if coarse_kwh > 0 and assume_period:
        assumptions.append(
            f"{coarse_kwh:,.2f} kWh came from coarse readings or cumulative-counter "
            f"poll gaps and was assigned to {PERIOD_LABELS.get(assume_period, assume_period)} "
            "because charging is confined to that period."
        )
    elif coarse_kwh > 0:
        assumptions.append(
            f"{coarse_kwh:,.2f} kWh came from coarse readings or cumulative-counter "
            "poll gaps; its time-of-day split is an even spread, not measured."
        )

    if plan == "tiered":
        energy_lines, electricity, oer_raw = _tiered_energy(
            intervals,
            start,
            end,
            schedule,
            timezone,
            total_kwh,
            household_kwh,
            assumptions,
        )
        delivery = money(total_kwh * context.delivery_per_kwh)
        regulatory = money(total_kwh * context.regulatory_per_kwh)
    else:
        energy_lines, electricity, delivery, regulatory, oer_raw = _time_based_energy(
            intervals,
            start,
            end,
            schedule,
            timezone,
            plan,
            assume_period,
            assumptions,
        )

    subtotal = money(electricity + delivery + regulatory)
    hst = money(subtotal * schedule.hst)
    oer = money(oer_raw)
    total = money(subtotal + hst - oer)

    proportional = None
    if method == "proportional":
        if household_kwh is None or household_bill_total is None:
            raise BillingError(
                "proportional billing needs --household-kwh and "
                "--household-bill-total (or set them in config/local.toml)"
            )
        if household_kwh <= 0:
            raise BillingError("--household-kwh must be positive")
        share = total_kwh / household_kwh
        proportional = ProportionalShare(
            share=share,
            household_kwh=household_kwh,
            household_bill_total=household_bill_total,
            amount=money(share * household_bill_total),
        )
        assumptions.append(
            f"Proportional method: {total_kwh:,.2f} kWh / {household_kwh:,.2f} kWh "
            f"= {share:.2%} of a {fmt_money(household_bill_total)} bill."
        )

    return Bill(
        start=start,
        end=end,
        plan=plan,
        method=method,
        kwh=total_kwh,
        energy_lines=energy_lines,
        electricity=electricity,
        delivery=delivery,
        regulatory=regulatory,
        subtotal=subtotal,
        hst=hst,
        oer=oer,
        total=total,
        delivery_per_kwh=context.delivery_per_kwh,
        regulatory_per_kwh=context.regulatory_per_kwh,
        loss_factor=context.loss_factor,
        hst_rate=schedule.hst,
        oer_rate=context.rebate.oer,
        coverage=coverage,
        gaps=gap_list,
        interval_count=len(intervals),
        coarse_kwh=coarse_kwh,
        context=context,
        proportional=proportional,
        assumptions=assumptions,
    )


def _dates_touched(start: datetime, end: datetime, tz) -> list:
    """Local dates in [start, end], capped to avoid huge loops."""
    first = start.astimezone(tz).date()
    last = (end - timedelta(seconds=1)).astimezone(tz).date()
    days = []
    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    return days or [first]


def _time_based_energy(
    intervals: list[Interval],
    start: datetime,
    end: datetime,
    schedule: RateSchedule,
    tz,
    plan: str,
    assume_period: str | None,
    assumptions: list[str],
):
    segments = build_segments(start, end, plan, tz)
    # Aggregate identical (period, rate, card date) rows.
    buckets: dict[tuple[str, float, str], float] = {}
    electricity_raw = 0.0
    delivery_raw = 0.0
    regulatory_raw = 0.0
    oer_raw = 0.0

    for interval in intervals:
        clipped = _clip_kwh(interval, start, end)
        if clipped <= 0:
            continue
        if assume_period and interval.is_coarse:
            local_mid = (max(interval.start, start) + (min(interval.end, end) - max(interval.start, start)) / 2)
            ctx = schedule.context(local_mid.astimezone(tz).date())
            rate = ctx.commodity.rate(plan, assume_period)
            kwh = clipped
            buckets[(assume_period, rate, ctx.commodity.effective_from.isoformat())] = (
                buckets.get((assume_period, rate, ctx.commodity.effective_from.isoformat()), 0.0)
                + kwh
            )
            electricity_raw += kwh * rate
            delivery_raw += kwh * ctx.delivery_per_kwh
            regulatory_raw += kwh * ctx.regulatory_per_kwh
            oer_raw += kwh * (rate + ctx.delivery_per_kwh + ctx.regulatory_per_kwh) * ctx.rebate.oer
            continue

        for segment, kwh in parts_by_segment(interval.start, interval.end, clipped, segments):
            ctx = schedule.context(segment.start.astimezone(tz).date())
            rate = ctx.commodity.rate(plan, segment.period)
            key = (segment.period, rate, ctx.commodity.effective_from.isoformat())
            buckets[key] = buckets.get(key, 0.0) + kwh
            electricity_raw += kwh * rate
            delivery_raw += kwh * ctx.delivery_per_kwh
            regulatory_raw += kwh * ctx.regulatory_per_kwh
            oer_raw += kwh * (rate + ctx.delivery_per_kwh + ctx.regulatory_per_kwh) * ctx.rebate.oer

    lines = [
        EnergyLine(
            period=period,
            rate=rate,
            kwh=kwh,
            amount=money(kwh * rate),
            effective_from=effective,
        )
        for (period, rate, effective), kwh in buckets.items()
    ]
    lines.sort(
        key=lambda line: (
            PERIOD_ORDER[plan].index(line.period) if line.period in PERIOD_ORDER[plan] else 99,
            line.rate,
        )
    )
    electricity = money(sum(line.amount for line in lines))
    return (
        lines,
        electricity,
        money(delivery_raw),
        money(regulatory_raw),
        oer_raw,
    )


def _tiered_energy(
    intervals: list[Interval],
    start: datetime,
    end: datetime,
    schedule: RateSchedule,
    tz,
    total_kwh: float,
    household_kwh: float | None,
    assumptions: list[str],
):
    ctx = schedule.context(start.astimezone(tz).date())
    threshold = ctx.commodity.tier_threshold(start.astimezone(tz).month)

    if household_kwh is None:
        tier2_kwh = 0.0
        assumptions.append(
            "Tiered plan without a household monthly total: all EV kWh priced at "
            "Tier 1. Add --household-kwh for an exact tier split."
        )
    else:
        if household_kwh < total_kwh:
            raise BillingError(
                "--household-kwh cannot be smaller than the EV's kWh in the period"
            )
        tier2_kwh = max(0.0, min(total_kwh, household_kwh - threshold))
    tier1_kwh = total_kwh - tier2_kwh

    rate1 = ctx.commodity.rate("tiered", "tier1")
    rate2 = ctx.commodity.rate("tiered", "tier2")

    lines = []
    if tier1_kwh > 0:
        lines.append(
            EnergyLine("tier1", rate1, tier1_kwh, money(tier1_kwh * rate1), ctx.commodity.effective_from.isoformat())
        )
    if tier2_kwh > 0:
        lines.append(
            EnergyLine("tier2", rate2, tier2_kwh, money(tier2_kwh * rate2), ctx.commodity.effective_from.isoformat())
        )

    electricity = money(sum(line.amount for line in lines))
    subtotal_raw = electricity + total_kwh * (ctx.delivery_per_kwh + ctx.regulatory_per_kwh)
    oer_raw = subtotal_raw * ctx.rebate.oer
    assumptions.append(
        f"Tier threshold for this season: {threshold:,.0f} kWh/month."
    )
    return lines, electricity, oer_raw


def plan_comparison(
    schedule: RateSchedule,
    *,
    off_peak_kwh: float,
    mid_peak_kwh: float,
    on_peak_kwh: float,
    day,
    household_kwh: float | None = None,
    hst: float | None = None,
) -> list[dict[str, float | str]]:
    """Compare TOU / ULO / Tiered for a given monthly kWh split.

    Assumes the ULO pattern: existing off-peak kWh is charged at the
    ultra-low overnight rate (i.e. the household can move that load to
    11pm-7am), mid-peak stays mid-peak, on-peak stays on-peak.
    """
    ctx = schedule.context(day)
    total = off_peak_kwh + mid_peak_kwh + on_peak_kwh
    metric = hst if hst is not None else ctx.hst

    def all_in(commodity: float) -> float:
        subtotal = commodity + total * (ctx.delivery_per_kwh + ctx.regulatory_per_kwh)
        return subtotal + subtotal * metric - subtotal * ctx.rebate.oer

    tou = (
        off_peak_kwh * ctx.commodity.rate("tou", "off_peak")
        + mid_peak_kwh * ctx.commodity.rate("tou", "mid_peak")
        + on_peak_kwh * ctx.commodity.rate("tou", "on_peak")
    )
    ulo = (
        off_peak_kwh * ctx.commodity.rate("ulo", "ultra_low_overnight")
        + mid_peak_kwh * ctx.commodity.rate("ulo", "mid_peak")
        + on_peak_kwh * ctx.commodity.rate("ulo", "on_peak")
    )

    rows: list[dict[str, float | str]] = []
    for name, commodity in (("TOU", tou), ("ULO", ulo)):
        rows.append(
            {
                "plan": name,
                "commodity": money(commodity),
                "total": money(all_in(commodity)),
                "effective_cents": (all_in(commodity) / total * 100) if total else 0.0,
            }
        )

    # Tiered needs the household total to know how much sits in tier 2.
    threshold = ctx.commodity.tier_threshold(day.month)
    if household_kwh and household_kwh >= total:
        tier2 = max(0.0, min(total, household_kwh - threshold))
        tier1 = total - tier2
    else:
        tier2 = 0.0
        tier1 = total
    tiered = tier1 * ctx.commodity.rate("tiered", "tier1") + tier2 * ctx.commodity.rate(
        "tiered", "tier2"
    )
    rows.append(
        {
            "plan": "Tiered",
            "commodity": money(tiered),
            "total": money(all_in(tiered)),
            "effective_cents": (all_in(tiered) / total * 100) if total else 0.0,
        }
    )
    return rows
