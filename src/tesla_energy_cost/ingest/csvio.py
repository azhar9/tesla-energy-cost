"""CSV import for smart-plug exports and hand-built data.

Canonical schemas (header names are normalised, so "Energy (kWh)" works):

  intervals:  start, end, kwh
  energy:     timestamp, kwh          (timestamp is the END of each interval)
  power:      timestamp, power_w      (samples, integrated trapezoidally)
  counter:    timestamp, total_wh     (cumulative meter counter)
  daily:      date, kwh               (coarse; one value per day)

Use ``--format auto`` to detect. For interval energy the first row has no
preceding timestamp and is skipped unless ``--interval-minutes`` is given.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..storage import add_interval, add_sample
from ..util import InputError, local_midnight, parse_datetime

ALIASES = {
    "start": {"start", "start_time", "starttime", "from", "begin", "interval_start", "session_start"},
    "end": {"end", "end_time", "endtime", "to", "finish", "interval_end", "session_end"},
    "timestamp": {"timestamp", "time", "datetime", "date_time", "ts", "read_date", "date"},
    "date": {"date", "day"},
    "kwh": {"kwh", "energy_kwh", "energy", "usage_kwh", "consumption_kwh", "charge_energy_added", "value_kwh"},
    "wh": {"wh", "energy_wh", "total_wh", "counter_wh", "reading_wh", "aenergy", "total_work"},
    "power": {"power", "power_w", "watts", "w", "active_power", "apower", "watt"},
}


def _norm(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name


def _find(header: list[str], kind: str) -> str | None:
    normed = {_norm(h): h for h in header}
    for candidate in ALIASES[kind]:
        if candidate in normed:
            return normed[candidate]
    return None


def detect_format(header: list[str]) -> str:
    normed = {_norm(name) for name in header}
    has_start = _find(header, "start") is not None
    has_end = _find(header, "end") is not None
    has_kwh = _find(header, "kwh") is not None
    has_wh = _find(header, "wh") is not None
    has_power = _find(header, "power") is not None
    has_ts = _find(header, "timestamp") is not None
    has_date = _find(header, "date") is not None

    if has_start and has_end and has_kwh:
        return "intervals"
    # A lone "date" column is a daily total, not an energy interval ending at
    # that timestamp. "date" is also accepted as a timestamp elsewhere because
    # many exports use it for a midnight-to-midnight reading.
    if has_date and has_kwh and not (
        normed & {"timestamp", "time", "datetime", "date_time", "ts", "read_date"}
    ):
        return "daily"
    if has_ts and has_power:
        return "power"
    if has_ts and has_wh:
        return "counter"
    if has_ts and has_kwh:
        return "energy"
    if has_date and has_kwh:
        return "daily"
    raise InputError(
        "could not detect CSV format from headers: "
        + ", ".join(header)
        + ". Use --format intervals|energy|power|counter|daily."
    )


@dataclass
class ImportResult:
    rows: int
    intervals: int
    kwh: float
    skipped: int


def import_csv(
    conn: sqlite3.Connection,
    path: Path,
    *,
    source: str,
    tz: ZoneInfo,
    fmt: str = "auto",
    interval_minutes: float | None = None,
    max_gap_minutes: float = 60,
) -> ImportResult:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise InputError(f"{path}: empty CSV")
        header = list(reader.fieldnames)
        chosen = detect_format(header) if fmt == "auto" else fmt
        rows = list(reader)

    if chosen not in {"intervals", "energy", "power", "counter", "daily"}:
        raise InputError(f"unknown CSV format {chosen!r}")

    if chosen == "intervals":
        return _import_intervals(rows, header, conn, source, tz, path)
    if chosen == "energy":
        return _import_energy(rows, header, conn, source, tz, interval_minutes)
    if chosen == "power":
        return _import_power(rows, header, conn, source, tz, max_gap_minutes)
    if chosen == "counter":
        return _import_counter(rows, header, conn, source, tz)
    return _import_daily(rows, header, conn, source, tz)


def _get(row: dict, header: list[str], kind: str) -> str | None:
    col = _find(header, kind)
    return row.get(col) if col else None


def _float(value: str | None, field: str) -> float:
    if value is None or str(value).strip() == "":
        raise InputError(f"missing {field}")
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise InputError(f"bad {field}: {value!r}") from exc


def _import_intervals(rows, header, conn, source, tz, path) -> ImportResult:
    count = 0
    kwh_total = 0.0
    skipped = 0
    for row in rows:
        start_raw = _get(row, header, "start")
        end_raw = _get(row, header, "end")
        kwh_raw = _get(row, header, "kwh")
        if not start_raw or not end_raw or kwh_raw in (None, ""):
            skipped += 1
            continue
        start = parse_datetime(start_raw, tz)
        end = parse_datetime(end_raw, tz)
        kwh = _float(kwh_raw, "kwh")
        if end <= start or kwh <= 0:
            skipped += 1
            continue
        if add_interval(conn, source=source, start=start, end=end, kwh=kwh):
            count += 1
            kwh_total += kwh
        else:
            skipped += 1
    return ImportResult(len(rows), count, kwh_total, skipped)


def _import_energy(rows, header, conn, source, tz, interval_minutes) -> ImportResult:
    count = 0
    kwh_total = 0.0
    skipped = 0
    prev_ts: datetime | None = None
    for row in rows:
        ts_raw = _get(row, header, "timestamp")
        kwh_raw = _get(row, header, "kwh")
        if not ts_raw or kwh_raw in (None, ""):
            skipped += 1
            continue
        ts = parse_datetime(ts_raw, tz)
        kwh = _float(kwh_raw, "kwh")
        if prev_ts is None:
            if interval_minutes:
                start = ts - timedelta(minutes=interval_minutes)
                if kwh > 0 and add_interval(conn, source=source, start=start, end=ts, kwh=kwh, quality="measured"):
                    count += 1
                    kwh_total += kwh
                else:
                    skipped += 1
        else:
            if ts <= prev_ts or kwh <= 0:
                skipped += 1
            elif add_interval(conn, source=source, start=prev_ts, end=ts, kwh=kwh, quality="measured"):
                count += 1
                kwh_total += kwh
            else:
                skipped += 1
        prev_ts = ts
    return ImportResult(len(rows), count, kwh_total, skipped)


def _import_power(rows, header, conn, source, tz, max_gap_minutes) -> ImportResult:
    count = 0
    kwh_total = 0.0
    skipped = 0
    prev: tuple[datetime, float] | None = None
    max_gap = max_gap_minutes * 60
    for row in rows:
        ts_raw = _get(row, header, "timestamp")
        power_raw = _get(row, header, "power")
        if not ts_raw or power_raw in (None, ""):
            skipped += 1
            continue
        ts = parse_datetime(ts_raw, tz)
        power = _float(power_raw, "power")
        add_sample(conn, source=source, ts=ts, power_w=power)
        if prev is not None:
            gap = (ts - prev[0]).total_seconds()
            if 0 < gap <= max_gap:
                kwh = (prev[1] + power) / 2 * gap / 3_600_000.0
                if kwh > 0:
                    if add_interval(conn, source=source, start=prev[0], end=ts, kwh=kwh):
                        count += 1
                        kwh_total += kwh
                    else:
                        skipped += 1
            else:
                skipped += 1
        prev = (ts, power)
    return ImportResult(len(rows), count, kwh_total, skipped)


def _import_counter(rows, header, conn, source, tz) -> ImportResult:
    count = 0
    kwh_total = 0.0
    skipped = 0
    prev: tuple[datetime, float] | None = None
    for row in rows:
        ts_raw = _get(row, header, "timestamp")
        wh_raw = _get(row, header, "wh")
        if not ts_raw or wh_raw in (None, ""):
            skipped += 1
            continue
        ts = parse_datetime(ts_raw, tz)
        total_wh = _float(wh_raw, "total_wh")
        add_sample(conn, source=source, ts=ts, counter_wh=total_wh)
        if prev is not None:
            delta = total_wh - prev[1]
            if ts > prev[0] and delta > 0:
                kwh = delta / 1000.0
                if add_interval(conn, source=source, start=prev[0], end=ts, kwh=kwh):
                    count += 1
                    kwh_total += kwh
                else:
                    skipped += 1
            else:
                skipped += 1
        prev = (ts, total_wh)
    return ImportResult(len(rows), count, kwh_total, skipped)


def _import_daily(rows, header, conn, source, tz) -> ImportResult:
    count = 0
    kwh_total = 0.0
    skipped = 0
    day_col = _find(header, "date")
    kwh_col = _find(header, "kwh")
    for row in rows:
        day_raw = row.get(day_col) if day_col else None
        kwh_raw = row.get(kwh_col) if kwh_col else None
        if not day_raw or kwh_raw in (None, ""):
            skipped += 1
            continue
        start = parse_datetime(day_raw, tz)
        kwh = _float(kwh_raw, "kwh")
        if kwh <= 0:
            skipped += 1
            continue
        local_day = start.astimezone(tz).date()
        end = local_midnight(local_day + timedelta(days=1), tz)
        if add_interval(
            conn,
            source=source,
            start=start,
            end=end,
            kwh=kwh,
            quality="daily",
        ):
            count += 1
            kwh_total += kwh
        else:
            skipped += 1
    return ImportResult(len(rows), count, kwh_total, skipped)
