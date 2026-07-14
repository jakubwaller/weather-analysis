from datetime import datetime, timezone

from weather_analysis.config import Config, Sensor
from weather_analysis.db import Measurement
from weather_analysis.validate import base_metric, filter_implausible, plausible


def config(**overrides) -> Config:
    base = dict(
        latitude=53.6, longitude=9.9,
        ha_sensors=[Sensor("sensor.living_room_temperature", "Living room", "inside")],
    )
    base.update(overrides)
    return Config(**base)


def m(value, area="inside", metric="temperature", sensor="sensor.living_room_temperature"):
    return Measurement(
        ts=datetime(2026, 1, 21, 9, tzinfo=timezone.utc),
        source="home_assistant", sensor=sensor, name="Living room",
        area=area, metric=metric, value=value, unit="°C",
    )


def test_base_metric_strips_min_max_suffix():
    assert base_metric("temperature") == "temperature"
    assert base_metric("temperature_min") == "temperature"
    assert base_metric("temperature_max") == "temperature"
    assert base_metric("humidity_min") == "humidity"


def test_inside_glitch_dropped_but_real_cold_morning_kept():
    assert not plausible(m(0.0), config())
    # the real 21 Jan open-window morning
    assert plausible(m(10.1), config())


def test_outside_subzero_is_kept():
    # a global 5C floor would erase the heating season
    assert plausible(m(2.8, area="outside"), config())
    assert plausible(m(-15.0, area="outside"), config())
    assert not plausible(m(-99.0, area="outside"), config())


def test_min_suffix_resolves_to_temperature_range():
    # the glitch arrives almost entirely via min
    assert not plausible(m(0.0, metric="temperature_min"), config())
    assert plausible(m(10.1, metric="temperature_min"), config())


def test_metric_without_rule_passes_through():
    assert plausible(m(1013.0, area="outside", metric="pressure"), config())
    assert plausible(m(0.0, area="outside", metric="cloud_cover"), config())


def test_per_sensor_valid_range_overrides_area_default():
    cellar = Sensor("sensor.cellar_temperature", "Cellar", "inside",
                    valid_range=(0.0, 25.0))
    cfg = config(ha_sensors=[cellar])
    assert plausible(m(3.0, sensor="sensor.cellar_temperature"), cfg)
    assert not plausible(m(30.0, sensor="sensor.cellar_temperature"), cfg)


def test_global_validation_block_overrides_default():
    cfg = config(validation_ranges={"temperature": {"inside": (15.0, 30.0)}})
    assert not plausible(m(10.1), cfg)
    assert plausible(m(20.0), cfg)


def test_filter_implausible_partitions_and_keeps_order():
    rows = [m(22.0), m(0.0), m(21.5)]
    kept, dropped = filter_implausible(rows, config())
    assert [r.value for r in kept] == [22.0, 21.5]
    assert [r.value for r in dropped] == [0.0]
