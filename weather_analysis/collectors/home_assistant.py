"""Inside/outside sensor readings from the Home Assistant REST API.

Uses a long-lived access token (profile -> Security -> Long-lived access tokens).

Two entry points:
- fetch_current():  current state of each configured sensor
- fetch_history():  recorded history for the past N days (limited by how long
  your Home Assistant recorder keeps data, 10 days by default)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..config import Config, Sensor
from ..db import Measurement

SOURCE = "home_assistant"

DEFAULT_UNITS = {"temperature": "°C", "humidity": "%", "pressure": "hPa"}


def _headers(config: Config) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ha_token}"}


def _parse_state(state: str) -> float | None:
    if state in ("unavailable", "unknown", "none", ""):
        return None
    try:
        return float(state)
    except ValueError:
        return None


def fetch_current(config: Config, session: requests.Session | None = None) -> list[Measurement]:
    http = session or requests
    measurements = []
    for sensor in config.ha_sensors:
        resp = http.get(
            f"{config.ha_url}/api/states/{sensor.entity_id}",
            headers=_headers(config),
            timeout=30,
        )
        if resp.status_code == 404:
            print(f"  warning: entity {sensor.entity_id} not found in Home Assistant, skipping")
            continue
        resp.raise_for_status()
        data = resp.json()
        value = _parse_state(data.get("state", ""))
        if value is None:
            print(f"  warning: entity {sensor.entity_id} is '{data.get('state')}', skipping")
            continue
        measurements.append(_measurement(sensor, datetime.now(timezone.utc), value,
                                         data.get("attributes", {}).get("unit_of_measurement")))
    return measurements


def fetch_history(config: Config, days: int,
                  session: requests.Session | None = None) -> list[Measurement]:
    if not config.ha_sensors:
        return []
    http = session or requests
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    resp = http.get(
        f"{config.ha_url}/api/history/period/{start.isoformat()}",
        headers=_headers(config),
        params={
            "end_time": end.isoformat(),
            "filter_entity_id": ",".join(s.entity_id for s in config.ha_sensors),
            "minimal_response": "",
            "no_attributes": "",
        },
        timeout=120,
    )
    resp.raise_for_status()

    by_entity = {s.entity_id: s for s in config.ha_sensors}
    measurements = []
    for entity_history in resp.json():
        if not entity_history:
            continue
        # with minimal_response only the first item carries entity_id
        entity_id = entity_history[0].get("entity_id")
        sensor = by_entity.get(entity_id)
        if sensor is None:
            continue
        for item in entity_history:
            value = _parse_state(item.get("state", ""))
            ts_raw = item.get("last_changed") or item.get("last_updated")
            if value is None or not ts_raw:
                continue
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            measurements.append(_measurement(sensor, ts, value, None))
    return measurements


def _measurement(sensor: Sensor, ts: datetime, value: float, unit: str | None) -> Measurement:
    return Measurement(
        ts=ts,
        source=SOURCE,
        sensor=sensor.entity_id,
        name=sensor.name,
        area=sensor.area,
        metric=sensor.metric,
        value=value,
        unit=unit or DEFAULT_UNITS.get(sensor.metric),
    )
