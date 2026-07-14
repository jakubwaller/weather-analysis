"""Plausibility checks that separate sensor glitches from real readings.

Some sensors briefly report 0.0. Measured across eight months of recorded data,
inside temperatures never fall below 10.1 C, so a floor of 5 C drops every such
glitch while leaving the coldest real reading 5 C of headroom.

Ranges are per area on purpose: outside is legitimately sub-zero all winter, so
a single global floor would delete real cold weather.
"""

from __future__ import annotations

from .config import Config
from .db import Measurement

# (area, metric) -> (low, high), inclusive. A metric with no rule is not filtered.
DEFAULT_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("inside", "temperature"): (5.0, 40.0),
    ("outside", "temperature"): (-40.0, 60.0),
    ("inside", "humidity"): (0.0, 100.0),
    ("outside", "humidity"): (0.0, 100.0),
}

_SUFFIXES = ("_min", "_max")


def base_metric(metric: str) -> str:
    """'temperature_min' -> 'temperature'.

    Hourly statistics carry the glitch almost entirely in min, so a lookup that
    misses the suffixed metrics would filter nothing that matters.
    """
    for suffix in _SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


def _range_for(m: Measurement, config: Config) -> tuple[float, float] | None:
    metric = base_metric(m.metric)
    for sensor in config.ha_sensors:
        if sensor.entity_id == m.sensor and sensor.valid_range:
            return sensor.valid_range
    override = config.validation_ranges.get(metric, {}).get(m.area)
    if override:
        return override
    return DEFAULT_RANGES.get((m.area, metric))


def plausible(m: Measurement, config: Config) -> bool:
    bounds = _range_for(m, config)
    if bounds is None:
        return True
    low, high = bounds
    return low <= m.value <= high


def filter_implausible(
    rows: list[Measurement], config: Config
) -> tuple[list[Measurement], list[Measurement]]:
    """Partition into (kept, dropped), preserving order."""
    kept: list[Measurement] = []
    dropped: list[Measurement] = []
    for m in rows:
        (kept if plausible(m, config) else dropped).append(m)
    return kept, dropped
