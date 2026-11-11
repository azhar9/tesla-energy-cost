from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesla_energy_cost.ingest.csvio import detect_format, import_csv
from tesla_energy_cost.storage import connect, intervals_between

UTC = timezone.utc


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_detect_formats():
    assert detect_format(["start", "end", "kwh"]) == "intervals"
    assert detect_format(["timestamp", "kwh"]) == "energy"
    assert detect_format(["timestamp", "power_w"]) == "power"
    assert detect_format(["timestamp", "total_wh"]) == "counter"
    assert detect_format(["date", "kwh"]) == "daily"


def test_import_intervals(tmp_path, project):
    csv_path = _write(
        tmp_path / "intervals.csv",
        "start,end,kwh\n"
        "2026-11-02 23:00,2026-11-03 00:00,1.44\n"
        "2026-11-03 00:00,2026-11-03 01:00,1.44\n",
    )
    conn = connect(project.storage_path)
    result = import_csv(
        conn, csv_path, source="plug", tz=project.timezone, fmt="auto"
    )
    assert result.rows == 2
    assert result.intervals == 2
    assert result.kwh == pytest.approx(2.88)
    rows = intervals_between(
        conn,
        "plug",
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert len(rows) == 2
    assert rows[0].quality == "measured"


def test_import_energy_timestamps_are_interval_ends(tmp_path, project):
    csv_path = _write(
        tmp_path / "energy.csv",
        "timestamp,energy_kwh\n"
        "2026-11-02 01:00,0.5\n"
        "2026-11-02 02:00,0.7\n",
    )
    conn = connect(project.storage_path)
    result = import_csv(
        conn,
        csv_path,
        source="plug",
        tz=project.timezone,
        fmt="auto",
        interval_minutes=60,
    )
    assert result.intervals == 2
    assert result.kwh == pytest.approx(1.2)
    rows = intervals_between(
        conn,
        "plug",
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 3, tzinfo=UTC),
    )
    assert rows[0].start == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert rows[0].end == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


def test_import_power_integrates(tmp_path, project):
    csv_path = _write(
        tmp_path / "power.csv",
        "timestamp,power_w\n"
        "2026-11-02 00:00,0\n"
        "2026-11-02 01:00,1440\n",
    )
    conn = connect(project.storage_path)
    result = import_csv(conn, csv_path, source="plug", tz=project.timezone)
    assert result.intervals == 1
    assert result.kwh == pytest.approx(0.72)


def test_import_counter(tmp_path, project):
    csv_path = _write(
        tmp_path / "counter.csv",
        "timestamp,total_wh\n"
        "2026-11-02 00:00,1000\n"
        "2026-11-02 01:00,2440\n",
    )
    conn = connect(project.storage_path)
    result = import_csv(conn, csv_path, source="plug", tz=project.timezone)
    assert result.intervals == 1
    assert result.kwh == pytest.approx(1.44)


def test_import_daily_uses_local_midnight_across_dst(tmp_path, project):
    spring = _write(tmp_path / "spring.csv", "date,kwh\n2026-03-08,9.6\n")
    conn = connect(project.storage_path)
    result = import_csv(conn, spring, source="spring", tz=project.timezone)
    assert result.intervals == 1
    rows = intervals_between(
        conn,
        "spring",
        datetime(2026, 3, 8, tzinfo=UTC),
        datetime(2026, 3, 10, tzinfo=UTC),
    )
    assert rows[0].seconds == 23 * 3600


def test_import_daily(tmp_path, project):
    csv_path = _write(tmp_path / "daily.csv", "date,kwh\n2026-11-02,9.6\n")
    conn = connect(project.storage_path)
    result = import_csv(conn, csv_path, source="plug", tz=project.timezone)
    assert result.intervals == 1
    assert result.kwh == pytest.approx(9.6)
    rows = intervals_between(
        conn,
        "plug",
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert rows[0].quality == "daily"
    assert rows[0].seconds == pytest.approx(24 * 3600, abs=1)
