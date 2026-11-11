from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tesla_energy_cost.rateupdate import (
    parse_oeb_historical,
    parse_oeb_holidays,
    parse_toronto_hydro,
    run_update,
    write_auto_rates,
)
from tesla_energy_cost.rates import load_rates
from tesla_energy_cost.periods import holidays


def test_parse_toronto_hydro_fixture(fixtures: Path):
    parsed = parse_toronto_hydro((fixtures / "toronto_hydro_rates.html").read_text())
    assert parsed.commodity is not None
    assert parsed.commodity.effective_from == date(2026, 11, 1)
    assert parsed.commodity.tou == {
        "off_peak": 0.091,
        "mid_peak": 0.146,
        "on_peak": 0.189,
    }
    assert parsed.commodity.ulo == {
        "ultra_low_overnight": 0.037,
        "weekend_off_peak": 0.091,
        "mid_peak": 0.146,
        "on_peak": 0.377,
    }
    assert parsed.commodity.tiered == {"tier1": 0.112, "tier2": 0.132}

    assert parsed.delivery is not None
    assert parsed.delivery.effective_from == date(2026, 1, 1)
    assert parsed.delivery.components == {
        "deferral_variance": 0.00112,
        "capacity_based_recovery": 0.00048,
        "transmission": 0.02241,
    }
    assert parsed.delivery.fixed_per_30_days["service_charge"] == 51.18
    assert parsed.delivery.fixed_per_30_days["smart_metering_entity"] == 0.41

    assert parsed.regulatory is not None
    assert parsed.regulatory.components == {"wholesale_market_service": 0.0053}
    assert parsed.rebate is not None
    assert parsed.rebate.oer == pytest.approx(0.157)
    assert parsed.rebate.effective_from == date(2026, 11, 1)
    assert parsed.hst == pytest.approx(0.13)
    # The suite-metered tables must not leak into Residential Service.
    assert parsed.delivery.fixed_per_30_days["service_charge"] != 41.98


def test_parse_oeb_historical_fixture(fixtures: Path):
    oeb = parse_oeb_historical((fixtures / "oeb_historical.html").read_text())
    assert oeb.tou[date(2026, 11, 1)] == {
        "off_peak": 0.091,
        "mid_peak": 0.146,
        "on_peak": 0.189,
    }
    assert oeb.ulo[date(2026, 11, 1)]["on_peak"] == 0.377
    assert oeb.tiered[date(2026, 11, 1)] == {"tier1": 0.112, "tier2": 0.132}
    assert oeb.thresholds == (600.0, 1000.0)


def test_parse_oeb_holidays_matches_builtin(fixtures: Path):
    official = parse_oeb_holidays((fixtures / "oeb_holidays.html").read_text())
    assert official == {2026: set(holidays(2026))}


def test_run_update_offline_matches_stored_cards(project, fixtures: Path):
    report = run_update(project, offline=fixtures / "toronto_hydro_rates.html")
    assert report.parsed.commodity is not None
    assert not report.has_changes
    assert not report.stale


def test_auto_overlay_wins_over_base(project, fixtures: Path):
    parsed = parse_toronto_hydro((fixtures / "toronto_hydro_rates.html").read_text())
    parsed.commodity.tou["off_peak"] = 0.10
    write_auto_rates(
        project.auto_rates_path,
        commodity=parsed.commodity,
        delivery=parsed.delivery,
        regulatory=parsed.regulatory,
        rebate=parsed.rebate,
        hst=parsed.hst,
        fetched=[],
    )
    schedule = load_rates(project)
    ctx = schedule.context(date(2026, 11, 10))
    assert ctx.commodity.tou["off_peak"] == 0.10
    # Same-date cards are de-duplicated, not duplicated.
    assert sum(1 for card in schedule.commodity if card.effective_from == date(2026, 11, 1)) == 1


def test_auto_overlay_is_idempotent(project, fixtures: Path):
    parsed = parse_toronto_hydro((fixtures / "toronto_hydro_rates.html").read_text())
    for _ in range(3):
        write_auto_rates(
            project.auto_rates_path,
            commodity=parsed.commodity,
            delivery=parsed.delivery,
            regulatory=parsed.regulatory,
            rebate=parsed.rebate,
            hst=parsed.hst,
            fetched=[],
        )
    schedule = load_rates(project)
    assert sum(1 for card in schedule.commodity if card.effective_from == date(2026, 11, 1)) == 1
    assert sum(1 for card in schedule.delivery if card.effective_from == date(2026, 1, 1)) == 1
    assert sum(1 for card in schedule.rebate if card.effective_from == date(2026, 11, 1)) == 1
