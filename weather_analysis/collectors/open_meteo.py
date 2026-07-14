"""Outside weather from the free Open-Meteo API (https://open-meteo.com, no API key).

Two entry points:
- fetch_current():  the current conditions, for the periodic collector
- fetch_history():  hourly values for the past N days, for backfilling
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..config import Config
from ..db import Measurement

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# The archive serves the same variable names with no usable lag, so history has no
# 92-day ceiling. fetch_current stays on the forecast endpoint: it is the only one
# with a `current` block.
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# our metric name -> (open-meteo variable, unit)
METRICS = {
    "temperature": ("temperature_2m", "°C"),
    "apparent_temperature": ("apparent_temperature", "°C"),
    "humidity": ("relative_humidity_2m", "%"),
    "pressure": ("surface_pressure", "hPa"),
    "wind_speed": ("wind_speed_10m", "km/h"),
    "precipitation": ("precipitation", "mm"),
    "cloud_cover": ("cloud_cover", "%"),
}

SOURCE = "open_meteo"
SENSOR = "open_meteo"
NAME = "Outside (Open-Meteo)"
AREA = "outside"


def _selected_metrics(config: Config) -> dict[str, tuple[str, str]]:
    unknown = [m for m in config.open_meteo_metrics if m not in METRICS]
    if unknown:
        raise ValueError(f"Unknown open_meteo metrics in config: {unknown}. "
                         f"Available: {sorted(METRICS)}")
    return {m: METRICS[m] for m in config.open_meteo_metrics}


def fetch_current(config: Config, session: requests.Session | None = None) -> list[Measurement]:
    metrics = _selected_metrics(config)
    http = session or requests
    resp = http.get(
        FORECAST_URL,
        params={
            "latitude": config.latitude,
            "longitude": config.longitude,
            "current": ",".join(var for var, _ in metrics.values()),
            "timezone": "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    current = resp.json()["current"]
    ts = datetime.fromisoformat(current["time"] + "+00:00")

    return [
        Measurement(ts=ts, source=SOURCE, sensor=SENSOR, name=NAME, area=AREA,
                    metric=metric, value=float(current[var]), unit=unit)
        for metric, (var, unit) in metrics.items()
        if current.get(var) is not None
    ]


def fetch_history(config: Config, days: int,
                  session: requests.Session | None = None) -> list[Measurement]:
    """Hourly history for the past `days` days, from the Open-Meteo archive."""
    metrics = _selected_metrics(config)
    http = session or requests
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    resp = http.get(
        ARCHIVE_URL,
        params={
            "latitude": config.latitude,
            "longitude": config.longitude,
            "hourly": ",".join(var for var, _ in metrics.values()),
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "timezone": "UTC",
        },
        timeout=60,
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    times = [datetime.fromisoformat(t + "+00:00") for t in hourly["time"]]
    now = datetime.now(tz=times[0].tzinfo) if times else None

    measurements = []
    for metric, (var, unit) in metrics.items():
        for ts, value in zip(times, hourly.get(var) or []):
            # the response includes today's remaining forecast hours; skip them
            if value is None or (now is not None and ts > now):
                continue
            measurements.append(
                Measurement(ts=ts, source=SOURCE, sensor=SENSOR, name=NAME, area=AREA,
                            metric=metric, value=float(value), unit=unit)
            )
    return measurements
