"""Long-term hourly statistics from Home Assistant.

Home Assistant purges `states` after `purge_keep_days` (10 by default) but keeps
hourly mean/min/max in `statistics` indefinitely. The REST history API reads only
`states`, so months of data are reachable only over the WebSocket API — there is
no REST equivalent for statistics.

Stored under its own source: an hourly mean is not an instantaneous reading, and
keeping them distinct preserves provenance under UNIQUE(ts, source, sensor, metric).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import Measurement

SOURCE = "home_assistant_stats"

DEFAULT_UNITS = {"temperature": "°C", "humidity": "%", "pressure": "hPa"}

# statistics field -> metric suffix
FIELDS = {"mean": "", "min": "_min", "max": "_max"}


class StatisticsClient:
    """Thin synchronous wrapper over the Home Assistant WebSocket API."""

    def __init__(self, url: str, token: str, timeout: int = 60):
        from websocket import create_connection

        ws_url = url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws = create_connection(f"{ws_url}/api/websocket", timeout=timeout)
        self._id = 0
        hello = json.loads(self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(
                f"unexpected greeting from Home Assistant: {hello.get('type')}"
            )
        self._ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(self._ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(
                "Home Assistant rejected the token (home_assistant.token / HA_TOKEN): "
                + str(auth.get("message", ""))
            )

    def statistics_during_period(self, **kwargs) -> dict:
        self._id += 1
        self._ws.send(json.dumps({
            "id": self._id, "type": "recorder/statistics_during_period", **kwargs,
        }))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") != self._id:
                continue  # unrelated event on the same socket
            if not resp.get("success"):
                raise RuntimeError(
                    "statistics request failed: "
                    + str((resp.get("error") or {}).get("message", resp))
                )
            return resp["result"]

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def fetch_statistics(config: Config, days: int, client=None) -> list[Measurement]:
    """Hourly mean/min/max for each configured sensor, as far back as `days`."""
    if not config.ha_sensors:
        return []
    own_client = client is None
    if own_client:
        client = StatisticsClient(config.ha_url, config.ha_token)
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        result = client.statistics_during_period(
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=[s.entity_id for s in config.ha_sensors],
            period="hour",
            types=["mean", "min", "max"],
        )
    finally:
        if own_client:
            client.close()

    measurements = []
    for sensor in config.ha_sensors:
        rows = result.get(sensor.entity_id)
        if not rows:
            print(f"  warning: no statistics for {sensor.entity_id}, skipping")
            continue
        unit = DEFAULT_UNITS.get(sensor.metric)
        for row in rows:
            ts = datetime.fromtimestamp(row["start"] / 1000, timezone.utc)
            for field, suffix in FIELDS.items():
                value = row.get(field)
                if value is None:
                    continue
                measurements.append(Measurement(
                    ts=ts, source=SOURCE, sensor=sensor.entity_id, name=sensor.name,
                    area=sensor.area, metric=f"{sensor.metric}{suffix}",
                    value=float(value), unit=unit,
                ))
    return measurements
