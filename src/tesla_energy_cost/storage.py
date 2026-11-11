"""SQLite storage for raw samples, billing intervals, and manual readings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .periods import merge_ranges
from .util import iso_utc, parse_stored

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    counter_wh  REAL,
    power_w     REAL,
    meta        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_samples_source_ts
    ON samples(source, ts_utc);

CREATE TABLE IF NOT EXISTS intervals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    end_utc     TEXT NOT NULL,
    kwh         REAL NOT NULL,
    quality     TEXT NOT NULL DEFAULT 'measured',
    meta        TEXT,
    UNIQUE(source, start_utc, end_utc)
);
CREATE INDEX IF NOT EXISTS idx_intervals_source_start
    ON intervals(source, start_utc);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    total_kwh   REAL NOT NULL,
    note        TEXT,
    UNIQUE(source, ts_utc)
);
"""


@dataclass(frozen=True)
class Interval:
    source: str
    start: datetime
    end: datetime
    kwh: float
    quality: str = "measured"
    meta: dict[str, Any] | None = None

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def is_coarse(self) -> bool:
        # A cumulative counter recovers energy exactly across a poll gap, but
        # its time-of-day split is unknown; treat it like a coarse reading.
        return self.quality in {"reading", "daily", "estimate", "gap"}


@dataclass(frozen=True)
class Reading:
    source: str
    ts: datetime
    total_kwh: float
    note: str = ""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def add_sample(
    conn: sqlite3.Connection,
    *,
    source: str,
    ts: datetime,
    counter_wh: float | None = None,
    power_w: float | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Insert one meter sample. Returns False if this timestamp already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO samples(source, ts_utc, counter_wh, power_w, meta) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            source,
            iso_utc(ts),
            counter_wh,
            power_w,
            json.dumps(meta, sort_keys=True) if meta else None,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def last_sample(conn: sqlite3.Connection, source: str) -> tuple[datetime, float | None, float | None] | None:
    row = conn.execute(
        "SELECT ts_utc, counter_wh, power_w FROM samples "
        "WHERE source = ? ORDER BY ts_utc DESC LIMIT 1",
        (source,),
    ).fetchone()
    if not row:
        return None
    return parse_stored(row[0]), row[1], row[2]


def add_interval(
    conn: sqlite3.Connection,
    *,
    source: str,
    start: datetime,
    end: datetime,
    kwh: float,
    quality: str = "measured",
    meta: dict[str, Any] | None = None,
) -> bool:
    if kwh <= 0 or end <= start:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO intervals(source, start_utc, end_utc, kwh, quality, meta) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            source,
            iso_utc(start),
            iso_utc(end),
            round(float(kwh), 6),
            quality,
            json.dumps(meta, sort_keys=True) if meta else None,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def intervals_between(
    conn: sqlite3.Connection,
    source: str,
    start: datetime,
    end: datetime,
) -> list[Interval]:
    rows = conn.execute(
        "SELECT source, start_utc, end_utc, kwh, quality, meta FROM intervals "
        "WHERE source = ? AND end_utc > ? AND start_utc < ? "
        "ORDER BY start_utc",
        (source, iso_utc(start), iso_utc(end)),
    ).fetchall()
    out = []
    for src, s, e, kwh, quality, meta in rows:
        out.append(
            Interval(
                source=src,
                start=parse_stored(s),
                end=parse_stored(e),
                kwh=float(kwh),
                quality=quality,
                meta=json.loads(meta) if meta else None,
            )
        )
    return out


def list_sources(conn: sqlite3.Connection) -> list[tuple[str, int, float, str, str]]:
    rows = conn.execute(
        "SELECT source, COUNT(*), COALESCE(SUM(kwh), 0), MIN(start_utc), MAX(end_utc) "
        "FROM intervals GROUP BY source ORDER BY source"
    ).fetchall()
    return [(r[0], r[1], float(r[2]), r[3], r[4]) for r in rows]


def add_reading(
    conn: sqlite3.Connection,
    *,
    source: str,
    ts: datetime,
    total_kwh: float,
    note: str = "",
) -> bool:
    cur = conn.execute(
        "INSERT OR REPLACE INTO readings(source, ts_utc, total_kwh, note) VALUES (?, ?, ?, ?)",
        (source, iso_utc(ts), float(total_kwh), note),
    )
    conn.commit()
    return cur.rowcount > 0


def list_readings(
    conn: sqlite3.Connection, source: str | None = None
) -> list[Reading]:
    if source:
        rows = conn.execute(
            "SELECT source, ts_utc, total_kwh, note FROM readings "
            "WHERE source = ? ORDER BY ts_utc",
            (source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, ts_utc, total_kwh, note FROM readings ORDER BY ts_utc"
        ).fetchall()
    return [
        Reading(r[0], parse_stored(r[1]), float(r[2]), r[3] or "") for r in rows
    ]


def derive_intervals_from_readings(
    conn: sqlite3.Connection, source: str
) -> list[Interval]:
    """Turn consecutive cumulative readings into billing intervals.

    The total energy is exact (it comes from the meter), but its timing inside
    the interval is unknown. Intervals are tagged quality='reading' so billing
    can optionally assign them to a single price period.
    """
    readings = list_readings(conn, source)
    created: list[Interval] = []
    for first, second in zip(readings, readings[1:]):
        delta = second.total_kwh - first.total_kwh
        if delta <= 0:
            raise ValueError(
                f"reading at {second.ts:%Y-%m-%d %H:%M} is not greater than the "
                f"previous reading ({second.total_kwh} <= {first.total_kwh}); "
                "reset the baseline or remove the bad reading"
            )
        if second.ts <= first.ts:
            raise ValueError("readings are not in chronological order")
        interval = Interval(source, first.ts, second.ts, delta, "reading")
        if add_interval(
            conn,
            source=source,
            start=first.ts,
            end=second.ts,
            kwh=delta,
            quality="reading",
        ):
            created.append(interval)
    return created


def delete_source(conn: sqlite3.Connection, source: str) -> tuple[int, int, int]:
    a = conn.execute("DELETE FROM intervals WHERE source = ?", (source,)).rowcount
    b = conn.execute("DELETE FROM samples WHERE source = ?", (source,)).rowcount
    c = conn.execute("DELETE FROM readings WHERE source = ?", (source,)).rowcount
    conn.commit()
    return a, b, c


def source_ranges(conn: sqlite3.Connection, source: str) -> list[tuple[datetime, datetime]]:
    rows = conn.execute(
        "SELECT start_utc, end_utc FROM intervals WHERE source = ? ORDER BY start_utc",
        (source,),
    ).fetchall()
    return [(parse_stored(r[0]), parse_stored(r[1])) for r in rows]


def summarize_source(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(kwh),0), MIN(start_utc), MAX(end_utc), "
        "COALESCE(SUM(CASE WHEN quality IN ('reading','daily','estimate') THEN kwh ELSE 0 END),0) "
        "FROM intervals WHERE source = ?",
        (source,),
    ).fetchone()
    return {
        "intervals": int(rows[0]),
        "kwh": float(rows[1]),
        "first": rows[2],
        "last": rows[3],
        "coarse_kwh": float(rows[4]),
    }


def record_power_sample(
    conn: sqlite3.Connection,
    *,
    source: str,
    ts: datetime,
    power_w: float,
    max_gap_seconds: float = 600,
    meta: dict[str, Any] | None = None,
) -> Interval | None:
    """Store a power reading and integrate it with the previous one.

    Gaps longer than ``max_gap_seconds`` are not integrated: trapezoidal
    integration over a long outage would invent energy. The missing time shows
    up as a coverage gap instead.
    """
    previous = last_sample(conn, source)
    add_sample(conn, source=source, ts=ts, power_w=power_w, meta=meta)
    if previous is None:
        return None
    prev_ts, prev_counter, prev_power = previous
    if prev_power is None or prev_ts >= ts:
        return None
    gap = (ts - prev_ts).total_seconds()
    if gap > max_gap_seconds:
        return None
    kwh = ((prev_power + power_w) / 2.0) * gap / 3_600_000.0
    if kwh <= 0:
        return None
    interval = Interval(source, prev_ts, ts, kwh, "measured")
    add_interval(
        conn,
        source=source,
        start=prev_ts,
        end=ts,
        kwh=kwh,
        quality="measured",
    )
    return interval


def record_counter_sample(
    conn: sqlite3.Connection,
    *,
    source: str,
    ts: datetime,
    counter_wh: float,
    multiplier: float = 1.0,
    meta: dict[str, Any] | None = None,
) -> Interval | None:
    """Store a cumulative energy counter and emit the delta as an interval.

    Cumulative counters survive poller downtime: the delta still contains all
    energy that passed through the meter, even if its timing is coarse. Those
    intervals are tagged quality='gap' when the poll gap is large.
    """
    previous = last_sample(conn, source)
    add_sample(conn, source=source, ts=ts, counter_wh=counter_wh, meta=meta)
    if previous is None:
        return None
    prev_ts, prev_counter, _ = previous
    if prev_counter is None or prev_ts >= ts:
        return None
    delta_wh = counter_wh - prev_counter
    if delta_wh <= 0:
        # Device reboot / counter reset: re-baseline without inventing energy.
        return None
    gap = (ts - prev_ts).total_seconds()
    quality = "measured" if gap <= 900 else "gap"
    interval = Interval(source, prev_ts, ts, delta_wh * multiplier / 1000.0, quality)
    add_interval(
        conn,
        source=source,
        start=prev_ts,
        end=ts,
        kwh=interval.kwh,
        quality=quality,
    )
    return interval


def merged_ranges(intervals: Iterable[Interval]) -> list[tuple[datetime, datetime]]:
    return merge_ranges([(i.start, i.end) for i in intervals])
