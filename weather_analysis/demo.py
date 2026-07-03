"""Seed the database with synthetic data so the dashboard can be tried
before any real collection has run (`weather-analysis demo`)."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Measurement, connect, insert_measurements


def seed_demo_data(db_path: Path | str, days: int = 30, seed: int = 42) -> int:
    rng = random.Random(seed)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    rows: list[Measurement] = []
    ts = start
    while ts <= end:
        hours = (ts - start).total_seconds() / 3600
        day_phase = 2 * math.pi * (ts.hour - 14) / 24  # warmest ~14:00 UTC
        season_drift = 3 * math.sin(2 * math.pi * hours / (24 * days))
        outside = (
            12 + season_drift
            + 6 * math.cos(day_phase)
            + rng.gauss(0, 0.8)
        )
        humidity = min(98, max(25, 70 - 2.5 * (outside - 12) + rng.gauss(0, 5)))
        wind = max(0, rng.gauss(11, 6))
        pressure = 1013 + 6 * math.sin(2 * math.pi * hours / (24 * 6)) + rng.gauss(0, 1)
        precipitation = max(0.0, rng.gauss(-0.6, 0.9)) if humidity > 75 else 0.0

        for metric, value, unit in [
            ("temperature", outside, "°C"),
            ("humidity", humidity, "%"),
            ("pressure", pressure, "hPa"),
            ("wind_speed", wind, "km/h"),
            ("precipitation", precipitation, "mm"),
        ]:
            rows.append(Measurement(ts=ts, source="open_meteo", sensor="open_meteo",
                                    name="Outside (Open-Meteo)", area="outside",
                                    metric=metric, value=round(value, 2), unit=unit))

        # inside temperatures follow outside weakly, damped and lagged
        living = 21.5 + 0.18 * (outside - 12) + 0.9 * math.cos(day_phase - 0.9) + rng.gauss(0, 0.15)
        bedroom = 19.8 + 0.12 * (outside - 12) + 0.5 * math.cos(day_phase - 1.2) + rng.gauss(0, 0.15)
        balcony = outside + 0.8 + rng.gauss(0, 0.3)  # own outside sensor, slightly warmer spot

        for entity, name, area, value in [
            ("sensor.living_room_temperature", "Living room", "inside", living),
            ("sensor.bedroom_temperature", "Bedroom", "inside", bedroom),
            ("sensor.balcony_temperature", "Balcony", "outside", balcony),
        ]:
            rows.append(Measurement(ts=ts, source="home_assistant", sensor=entity,
                                    name=name, area=area, metric="temperature",
                                    value=round(value, 2), unit="°C"))
        ts += timedelta(hours=1)

    conn = connect(db_path)
    try:
        inserted = insert_measurements(conn, rows)
    finally:
        conn.close()
    print(f"seeded {inserted} demo rows into {db_path}")
    return inserted
