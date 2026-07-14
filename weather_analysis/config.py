"""Configuration loading.

Reads config.yaml (see config.example.yaml), expands ${ENV_VAR} references,
and exposes a typed Config object.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATHS = ("config.yaml", "config.yml")

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


class ConfigError(Exception):
    pass


@dataclass
class Sensor:
    entity_id: str
    name: str
    area: str = "inside"  # 'inside' or 'outside'
    metric: str = "temperature"
    valid_range: tuple[float, float] | None = None  # overrides the area default


@dataclass
class Config:
    latitude: float
    longitude: float
    timezone: str = "UTC"
    db_path: Path = Path("data/weather.db")
    open_meteo_enabled: bool = True
    open_meteo_metrics: list[str] = field(default_factory=list)
    ha_enabled: bool = False
    ha_url: str = ""
    ha_token: str = ""
    ha_sensors: list[Sensor] = field(default_factory=list)
    interval_minutes: int = 10
    # metric -> area -> (low, high); overrides validate.DEFAULT_RANGES
    validation_ranges: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)


def _expand_env(value):
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        path = Path(candidate)
        if path.exists():
            return path
    raise ConfigError(
        "No config.yaml found. Copy config.example.yaml to config.yaml and edit it."
    )


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    with open(path) as f:
        raw = _expand_env(yaml.safe_load(f) or {})

    location = raw.get("location") or {}
    if "latitude" not in location or "longitude" not in location:
        raise ConfigError("config: location.latitude and location.longitude are required")

    om = raw.get("open_meteo") or {}
    ha = raw.get("home_assistant") or {}

    sensors = []
    for s in ha.get("sensors") or []:
        if not s.get("entity_id"):
            raise ConfigError("config: every home_assistant sensor needs an entity_id")
        sensors.append(
            Sensor(
                entity_id=s["entity_id"],
                name=s.get("name") or s["entity_id"],
                area=s.get("area", "inside"),
                metric=s.get("metric", "temperature"),
                valid_range=tuple(s["valid_range"]) if s.get("valid_range") else None,
            )
        )

    ha_enabled = bool(ha.get("enabled", bool(sensors)))
    ha_token = ha.get("token") or os.environ.get("HA_TOKEN", "")
    if ha_enabled and sensors and not ha_token:
        raise ConfigError(
            "config: home_assistant is enabled but no token is set "
            "(set home_assistant.token or the HA_TOKEN environment variable)"
        )

    validation_ranges = {
        metric: {area: tuple(bounds) for area, bounds in (by_area or {}).items()}
        for metric, by_area in (raw.get("validation") or {}).items()
    }

    return Config(
        latitude=float(location["latitude"]),
        longitude=float(location["longitude"]),
        timezone=location.get("timezone", "UTC"),
        db_path=Path((raw.get("database") or {}).get("path", "data/weather.db")),
        open_meteo_enabled=bool(om.get("enabled", True)),
        open_meteo_metrics=om.get("metrics")
        or ["temperature", "humidity", "pressure", "wind_speed", "precipitation"],
        ha_enabled=ha_enabled,
        ha_url=(ha.get("url") or "").rstrip("/"),
        ha_token=ha_token,
        ha_sensors=sensors,
        interval_minutes=int((raw.get("collection") or {}).get("interval_minutes", 10)),
        validation_ranges=validation_ranges,
    )
