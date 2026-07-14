"""Orchestrates collection runs: fetch from all enabled sources, store in SQLite."""

from __future__ import annotations

import sqlite3
import time
import traceback

from .config import Config
from .collectors import home_assistant, home_assistant_stats, open_meteo
from .db import Measurement, connect, insert_measurements
from .validate import filter_implausible


def _store(conn: sqlite3.Connection, rows: list[Measurement],
           config: Config, label: str) -> int:
    """Validate, insert, report.

    Every source goes through here, so a collector added later cannot bypass
    validation by forgetting to call it.
    """
    kept, dropped = filter_implausible(rows, config)
    inserted = insert_measurements(conn, kept)
    note = f", {len(dropped)} implausible dropped" if dropped else ""
    print(f"{label}: {len(rows)} readings, {inserted} new{note}")
    return inserted


def collect_once(config: Config) -> int:
    """Run one collection cycle. Returns the number of new rows stored."""
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            try:
                inserted += _store(conn, open_meteo.fetch_current(config), config,
                                   "open-meteo")
            except Exception as exc:
                print(f"open-meteo: FAILED ({exc})")
        if config.ha_enabled and config.ha_sensors:
            try:
                inserted += _store(conn, home_assistant.fetch_current(config), config,
                                   "home-assistant")
            except Exception as exc:
                print(f"home-assistant: FAILED ({exc})")
    finally:
        conn.close()
    print(f"stored {inserted} new rows in {config.db_path}")
    return inserted


def collect_loop(config: Config) -> None:
    """Collect forever, every `collection.interval_minutes`. Ctrl-C to stop."""
    interval = max(60, config.interval_minutes * 60)
    print(f"collecting every {interval // 60} min, Ctrl-C to stop")
    while True:
        try:
            collect_once(config)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


def backfill(config: Config, days: int) -> int:
    """Fetch past data from every source.

    Open-Meteo's archive has no practical limit. The Home Assistant recorder
    keeps full-detail history only as long as its retention allows, while its
    long-term statistics are hourly but reach back much further; the two overlap
    on purpose, at different resolutions.
    """
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            try:
                inserted += _store(conn, open_meteo.fetch_history(config, days), config,
                                   "open-meteo history")
            except Exception as exc:
                print(f"open-meteo history: FAILED ({exc})")
        if config.ha_enabled and config.ha_sensors:
            try:
                inserted += _store(conn, home_assistant.fetch_history(config, days),
                                   config, "home-assistant history")
            except Exception as exc:
                print(f"home-assistant history: FAILED ({exc})")
            try:
                inserted += _store(conn, home_assistant_stats.fetch_statistics(config, days),
                                   config, "home-assistant statistics")
            except Exception as exc:
                print(f"home-assistant statistics: FAILED ({exc})")
    finally:
        conn.close()
    print(f"stored {inserted} new rows in {config.db_path}")
    return inserted
