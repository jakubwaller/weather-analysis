"""Orchestrates collection runs: fetch from all enabled sources, store in SQLite."""

from __future__ import annotations

import time
import traceback

from .config import Config
from .collectors import home_assistant, open_meteo
from .db import connect, insert_measurements


def collect_once(config: Config) -> int:
    """Run one collection cycle. Returns the number of new rows stored."""
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            try:
                rows = open_meteo.fetch_current(config)
                inserted += insert_measurements(conn, rows)
                print(f"open-meteo: {len(rows)} readings")
            except Exception as exc:
                print(f"open-meteo: FAILED ({exc})")
        if config.ha_enabled and config.ha_sensors:
            try:
                rows = home_assistant.fetch_current(config)
                inserted += insert_measurements(conn, rows)
                print(f"home-assistant: {len(rows)} readings")
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
    """Fetch past data from Open-Meteo (hourly, up to 92 days) and from the
    Home Assistant recorder (as far back as its retention allows)."""
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            rows = open_meteo.fetch_history(config, days)
            n = insert_measurements(conn, rows)
            inserted += n
            print(f"open-meteo history: {len(rows)} readings, {n} new")
        if config.ha_enabled and config.ha_sensors:
            try:
                rows = home_assistant.fetch_history(config, days)
                n = insert_measurements(conn, rows)
                inserted += n
                print(f"home-assistant history: {len(rows)} readings, {n} new")
            except Exception as exc:
                print(f"home-assistant history: FAILED ({exc})")
    finally:
        conn.close()
    print(f"stored {inserted} new rows in {config.db_path}")
    return inserted
