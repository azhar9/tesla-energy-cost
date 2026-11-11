"""Effective-dated Toronto Hydro rate cards and marginal price maths."""

from __future__ import annotations

import bisect
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .config import Config, ConfigError


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ConfigError(f"expected an ISO date, got {value!r}")


def _card_at(cards: list[Any], day: date, label: str) -> Any:
    if not cards:
        raise ConfigError(f"no {label} rate cards configured")
    starts = [c.effective_from for c in cards]
    idx = bisect.bisect_right(starts, day) - 1
    if idx < 0:
        raise ConfigError(
            f"no {label} rate card on or before {day.isoformat()}; "
            f"earliest is {starts[0].isoformat()}"
        )
    return cards[idx]


@dataclass(frozen=True)
class CommodityCard:
    effective_from: date
    tou: dict[str, float]
    ulo: dict[str, float]
    tiered: dict[str, float]
    tier_threshold_summer_kwh: float
    tier_threshold_winter_kwh: float
    source: str = ""

    def rate(self, plan: str, period: str) -> float:
        table = {"tou": self.tou, "ulo": self.ulo, "tiered": self.tiered}[plan]
        try:
            return table[period]
        except KeyError as exc:
            raise ConfigError(
                f"rate card {self.effective_from} has no {plan}/{period} price"
            ) from exc

    def tier_threshold(self, month: int) -> float:
        # Summer season is May 1 - Oct 31, winter is Nov 1 - Apr 30.
        return (
            self.tier_threshold_summer_kwh
            if 5 <= month <= 10
            else self.tier_threshold_winter_kwh
        )


@dataclass(frozen=True)
class DeliveryCard:
    effective_from: date
    components: dict[str, float]
    adjustment: float = 0.0
    fixed_per_30_days: dict[str, float] = field(default_factory=dict)
    source: str = ""

    @property
    def volumetric(self) -> float:
        return sum(self.components.values())

    @property
    def fixed_total(self) -> float:
        return sum(self.fixed_per_30_days.values())


@dataclass(frozen=True)
class RegulatoryCard:
    effective_from: date
    components: dict[str, float]
    source: str = ""

    @property
    def volumetric(self) -> float:
        return sum(self.components.values())


@dataclass(frozen=True)
class RebateCard:
    effective_from: date
    oer: float
    source: str = ""


@dataclass(frozen=True)
class LossFactorCard:
    effective_from: date
    value: float
    source: str = ""


@dataclass(frozen=True)
class PriceContext:
    """Everything needed to price one interval on one day."""

    day: date
    commodity: CommodityCard
    delivery: DeliveryCard
    regulatory: RegulatoryCard
    rebate: RebateCard
    loss_factor: float
    hst: float

    @property
    def delivery_per_kwh(self) -> float:
        """Volumetric delivery dollars per *metered* kWh."""
        return self.delivery.volumetric * self.loss_factor + self.delivery.adjustment

    @property
    def regulatory_per_kwh(self) -> float:
        """Volumetric regulatory dollars per *metered* kWh."""
        return self.regulatory.volumetric * self.loss_factor

    @property
    def non_commodity_per_kwh(self) -> float:
        return self.delivery_per_kwh + self.regulatory_per_kwh

    def all_in_per_kwh(self, commodity_rate: float) -> float:
        subtotal = commodity_rate + self.non_commodity_per_kwh
        return subtotal + subtotal * self.hst - subtotal * self.rebate.oer


class RateSchedule:
    def __init__(
        self,
        *,
        currency: str,
        timezone: str,
        hst: float,
        commodity: list[CommodityCard],
        delivery: list[DeliveryCard],
        regulatory: list[RegulatoryCard],
        rebate: list[RebateCard],
        loss_factor: list[LossFactorCard],
        source_path: Path,
    ):
        self.currency = currency
        self.timezone = timezone
        self.hst = hst
        self.commodity = sorted(commodity, key=lambda c: c.effective_from)
        self.delivery = sorted(delivery, key=lambda c: c.effective_from)
        self.regulatory = sorted(regulatory, key=lambda c: c.effective_from)
        self.rebate = sorted(rebate, key=lambda c: c.effective_from)
        self.loss_factor = sorted(loss_factor, key=lambda c: c.effective_from)
        self.source_path = source_path

    def context(self, day: date) -> PriceContext:
        return PriceContext(
            day=day,
            commodity=_card_at(self.commodity, day, "commodity"),
            delivery=_card_at(self.delivery, day, "delivery"),
            regulatory=_card_at(self.regulatory, day, "regulatory"),
            rebate=_card_at(self.rebate, day, "rebate"),
            loss_factor=_card_at(self.loss_factor, day, "loss factor").value,
            hst=self.hst,
        )

    def all_effective_dates(self) -> list[date]:
        dates: set[date] = set()
        for group in (self.commodity, self.delivery, self.regulatory, self.rebate, self.loss_factor):
            dates.update(c.effective_from for c in group)
        return sorted(dates)

    def validate(self, today: date | None = None) -> list[str]:
        """Return human-readable warnings (not errors)."""
        today = today or date.today()
        warnings: list[str] = []
        for name, cards in (
            ("commodity", self.commodity),
            ("delivery", self.delivery),
            ("regulatory", self.regulatory),
            ("rebate", self.rebate),
            ("loss factor", self.loss_factor),
        ):
            latest = cards[-1].effective_from
            if latest > today:
                warnings.append(
                    f"{name}: newest card starts {latest}, in the future"
                )
            elif (today - latest).days > 400:
                warnings.append(
                    f"{name}: newest card is from {latest}; check for a rate update"
                )
        return warnings


def _parse_commodity(raw: dict[str, Any]) -> CommodityCard:
    return CommodityCard(
        effective_from=_as_date(raw["effective_from"]),
        tou={k: float(v) for k, v in raw.get("tou", {}).items()},
        ulo={k: float(v) for k, v in raw.get("ulo", {}).items()},
        tiered={k: float(v) for k, v in raw.get("tiered", {}).items()},
        tier_threshold_summer_kwh=float(
            raw.get("commodity_tier_threshold_summer_kwh", 600)
        ),
        tier_threshold_winter_kwh=float(
            raw.get("commodity_tier_threshold_winter_kwh", 1000)
        ),
        source=str(raw.get("source", "")),
    )


def _dedupe(cards: list[Any]) -> list[Any]:
    """Keep the last card for each effective date.

    ``rates.auto.toml`` is loaded after ``rates.toml``, so an automatically
    updated card with the same effective date overrides the hand-written one.
    """
    by_date: dict[date, Any] = {}
    for card in cards:
        by_date[card.effective_from] = card
    return sorted(by_date.values(), key=lambda c: c.effective_from)


def load_rates(config: Config) -> RateSchedule:
    paths = [config.rates_path, config.local_rates_path, config.auto_rates_path]
    merged: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for key, value in data.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged[key] = value

    if not merged:
        raise ConfigError(
            f"no rate data found at {config.rates_path}. Run `tec init`."
        )

    commodity = [_parse_commodity(c) for c in merged.get("commodity", [])]
    delivery = [
        DeliveryCard(
            effective_from=_as_date(c["effective_from"]),
            components={k: float(v) for k, v in c.get("components", {}).items()},
            adjustment=float(c.get("delivery_adjustment", 0.0)),
            fixed_per_30_days={
                k: float(v) for k, v in c.get("fixed_per_30_days", {}).items()
            },
            source=str(c.get("source", "")),
        )
        for c in merged.get("delivery", [])
    ]
    regulatory = [
        RegulatoryCard(
            effective_from=_as_date(c["effective_from"]),
            components={k: float(v) for k, v in c.get("components", {}).items()},
            source=str(c.get("source", "")),
        )
        for c in merged.get("regulatory", [])
    ]
    rebate = [
        RebateCard(
            effective_from=_as_date(c["effective_from"]),
            oer=float(c["oer"]),
            source=str(c.get("source", "")),
        )
        for c in merged.get("rebate", [])
    ]
    loss = [
        LossFactorCard(
            effective_from=_as_date(c["effective_from"]),
            value=float(c["value"]),
            source=str(c.get("source", "")),
        )
        for c in merged.get("loss_factor", [])
    ]

    schedule = RateSchedule(
        currency=str(merged.get("currency", "CAD")),
        timezone=str(merged.get("timezone", "America/Toronto")),
        hst=float(merged.get("hst", 0.13)),
        commodity=commodity,
        delivery=delivery,
        regulatory=regulatory,
        rebate=rebate,
        loss_factor=loss,
        source_path=config.rates_path,
    )

    schedule.commodity = _dedupe(schedule.commodity)
    schedule.delivery = _dedupe(schedule.delivery)
    schedule.regulatory = _dedupe(schedule.regulatory)
    schedule.rebate = _dedupe(schedule.rebate)
    schedule.loss_factor = _dedupe(schedule.loss_factor)

    # Apply optional bill calibration overrides.
    for override in merged.get("delivery_override", []):
        day = _as_date(override["effective_from"])
        target = next((d for d in schedule.delivery if d.effective_from == day), None)
        if target is None:
            continue
        updated = DeliveryCard(
            effective_from=target.effective_from,
            components=target.components,
            adjustment=float(override.get("delivery_adjustment", target.adjustment)),
            fixed_per_30_days=target.fixed_per_30_days,
            source=target.source + " (calibrated)",
        )
        schedule.delivery[schedule.delivery.index(target)] = updated

    if not schedule.commodity:
        raise ConfigError("rates.toml has no [[commodity]] cards")
    return schedule


def effective_multiplier(context: PriceContext) -> float:
    """Total-bill multiplier on the pre-tax subtotal.

    HST is charged on the full subtotal and the OER is a credit on the same
    subtotal, so the net multiplier is (1 + HST - OER).
    """
    return 1.0 + context.hst - context.rebate.oer


def iter_contexts(schedule: RateSchedule, start: date, end: date) -> Iterable[PriceContext]:
    for day in (start, end):
        yield schedule.context(day)
