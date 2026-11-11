from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tesla_energy_cost.storage import (
    Interval,
    add_reading,
    connect,
    derive_intervals_from_readings,
    intervals_between,
    last_sample,
    list_sources,
    record_counter_sample,
    record_power_sample,
)

UTC = timezone.utc


def test_counter_samples_produce_exact_deltas(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert record_counter_sample(conn, source="plug", ts=start, counter_wh=1000) is None
    interval = record_counter_sample(
        conn, source="plug", ts=start + timedelta(minutes=10), counter_wh=2500
    )
    assert interval is not None
    assert interval.kwh == pytest.approx(1.5)
    assert interval.quality == "measured"
    assert last_sample(conn, "plug")[1] == 2500


def test_counter_reset_does_not_invent_energy(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    record_counter_sample(conn, source="plug", ts=start, counter_wh=5000)
    assert (
        record_counter_sample(
            conn, source="plug", ts=start + timedelta(minutes=10), counter_wh=100
        )
        is None
    )
    assert intervals_between(conn, "plug", start, start + timedelta(days=1)) == []


def test_large_poll_gap_is_tagged_gap(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    record_counter_sample(conn, source="plug", ts=start, counter_wh=0)
    interval = record_counter_sample(
        conn, source="plug", ts=start + timedelta(hours=5), counter_wh=5000
    )
    assert interval is not None
    assert interval.quality == "gap"
    assert interval.kwh == pytest.approx(5.0)


def test_power_samples_are_integrated_trapezoidally(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    record_power_sample(conn, source="tesla", ts=start, power_w=0)
    interval = record_power_sample(
        conn, source="tesla", ts=start + timedelta(minutes=10), power_w=1000
    )
    assert interval is not None
    assert interval.kwh == pytest.approx(1000 * 600 / 3_600_000 / 2)
    assert interval.quality == "measured"


def test_power_gap_is_not_integrated(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    record_power_sample(conn, source="tesla", ts=start, power_w=1000)
    interval = record_power_sample(
        conn,
        source="tesla",
        ts=start + timedelta(hours=2),
        power_w=1000,
        max_gap_seconds=600,
    )
    assert interval is None
    assert intervals_between(conn, "tesla", start, start + timedelta(days=1)) == []


def test_counter_gap_is_treated_as_coarse_for_billing(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    record_counter_sample(conn, source="plug", ts=start, counter_wh=0)
    interval = record_counter_sample(
        conn, source="plug", ts=start + timedelta(hours=5), counter_wh=5000
    )
    assert interval is not None
    assert interval.quality == "gap"
    assert interval.is_coarse


def test_readings_derive_intervals(tmp_path):
    conn = connect(tmp_path / "energy.db")
    add_reading(conn, source="manual", ts=datetime(2026, 10, 1, tzinfo=UTC), total_kwh=1000)
    add_reading(conn, source="manual", ts=datetime(2026, 11, 1, tzinfo=UTC), total_kwh=1280)
    created = derive_intervals_from_readings(conn, "manual")
    assert len(created) == 1
    assert created[0].kwh == 280
    assert created[0].quality == "reading"


def test_readings_must_increase(tmp_path):
    conn = connect(tmp_path / "energy.db")
    add_reading(conn, source="manual", ts=datetime(2026, 10, 1, tzinfo=UTC), total_kwh=1000)
    add_reading(conn, source="manual", ts=datetime(2026, 11, 1, tzinfo=UTC), total_kwh=900)
    with pytest.raises(ValueError, match="not greater"):
        derive_intervals_from_readings(conn, "manual")


def test_list_sources_summarizes(tmp_path):
    conn = connect(tmp_path / "energy.db")
    start = datetime(2026, 11, 2, tzinfo=UTC)
    record_counter_sample(conn, source="plug", ts=start, counter_wh=0)
    record_counter_sample(conn, source="plug", ts=start + timedelta(hours=1), counter_wh=2000)
    rows = list_sources(conn)
    assert rows[0][0] == "plug"
    assert rows[0][1] == 1
    assert rows[0][2] == pytest.approx(2.0)
