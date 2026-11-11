from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tesla_energy_cost.rates import load_rates
from tesla_energy_cost.util import money

TORONTO = ZoneInfo("America/Toronto")


def test_current_cards_are_loaded(project):
    schedule = load_rates(project)
    ctx = schedule.context(date(2026, 11, 10))
    assert ctx.commodity.effective_from == date(2026, 11, 1)
    assert ctx.commodity.tou == {"off_peak": 0.091, "mid_peak": 0.146, "on_peak": 0.189}
    assert ctx.commodity.ulo["ultra_low_overnight"] == 0.037
    assert ctx.commodity.tiered == {"tier1": 0.112, "tier2": 0.132}
    assert ctx.delivery.effective_from == date(2026, 1, 1)
    assert ctx.delivery.components == {
        "deferral_variance": 0.00112,
        "capacity_based_recovery": 0.00048,
        "transmission": 0.02241,
    }
    assert ctx.regulatory.components == {"wholesale_market_service": 0.0053}
    assert ctx.rebate.oer == 0.157
    assert ctx.loss_factor == 1.0295


def test_old_card_is_used_for_old_dates(project):
    schedule = load_rates(project)
    ctx = schedule.context(date(2025, 12, 15))
    assert ctx.commodity.effective_from == date(2025, 11, 1)
    assert ctx.commodity.tou["off_peak"] == 0.098
    assert ctx.rebate.oer == 0.235
    # Delivery changed on 2026-01-01, so 2025 still has the 2025 card.
    assert ctx.delivery.effective_from == date(2025, 1, 1)


def test_marginal_rate_math(project):
    schedule = load_rates(project)
    ctx = schedule.context(date(2026, 11, 10))
    expected_delivery = (0.00112 + 0.00048 + 0.02241) * 1.0295
    expected_regulatory = 0.0053 * 1.0295
    assert ctx.delivery_per_kwh == pytest.approx(expected_delivery)
    assert ctx.regulatory_per_kwh == pytest.approx(expected_regulatory)

    subtotal = 0.091 + expected_delivery + expected_regulatory
    expected = subtotal + subtotal * 0.13 - subtotal * 0.157
    assert ctx.all_in_per_kwh(0.091) == pytest.approx(expected)
    assert ctx.all_in_per_kwh(0.091) == pytest.approx(0.1179, abs=0.00005)

    # The common mistake is compounding the rebate and HST.
    wrong = subtotal * (1 - 0.157) * 1.13
    assert not (abs(wrong - ctx.all_in_per_kwh(0.091)) < 1e-9)


def test_oer_and_hst_are_both_based_on_pre_rebate_subtotal():
    # This mirrors the calculation in billing.build_bill, isolated here so a
    # future refactor cannot silently change the order of operations.
    subtotal = 100.00
    hst = money(subtotal * 0.13)
    oer = money(subtotal * 0.157)
    total = money(subtotal + hst - oer)
    assert hst == 13.00
    assert oer == 15.70
    assert total == 97.30


def test_tier_threshold_follows_season(project):
    schedule = load_rates(project)
    may = schedule.context(date(2026, 5, 1)).commodity
    november = schedule.context(date(2026, 11, 1)).commodity
    assert may.tier_threshold(5) == 600
    assert may.tier_threshold(10) == 600
    assert november.tier_threshold(11) == 1000
    assert november.tier_threshold(4) == 1000
