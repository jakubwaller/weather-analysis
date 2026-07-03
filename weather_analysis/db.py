"""SQLite storage for measurements.

Everything is stored in one long-format table so new sources, sensors and
metrics never need schema changes. The UNIQUE constraint makes inserts
idempotent, which keeps backfills and overlapping collection runs safe.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,   -- UTC, ISO 8601
    source  TEXT NOT NULL,   -- 'open_meteo' | 'home_assistant' | 'demo'
    sensor  TEXT NOT NULL,   -- entity id, or 'open_meteo' for the weather API
    name    TEXT NOT NULL,   -- friendly label shown in the dashboard
    area    TEXT NOT NULL,   -- 'inside' | 'outside'
    metric  TEXT NOT NULL,   -- temperature, humidity, pressure, ...
    value   REAL NOT NULL,
    unit    TEXT,
    UNIQUE (ts, source, sensor, metric)
);
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements (ts);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_ts ON measurements (metric, ts);
"""


@dataclass
class Measurement:
    ts: datetime
    source: str
    sensor: str
    name: str
    area: str
    metric: str
    value: float
    unit: str | None = None


def to_utc_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def insert_measurements(conn: sqlite3.Connection, measurements: list[Measurement]) -> int:
    """Insert measurements, ignoring duplicates. Returns the number inserted."""
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO measurements (ts, source, sensor, name, area, metric, value, unit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (to_utc_iso(m.ts), m.source, m.sensor, m.name, m.area, m.metric, m.value, m.unit)
            for m in measurements
        ],
    )
    conn.commit()
    return conn.total_changes - before
