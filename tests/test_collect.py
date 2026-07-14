from datetime import datetime, timezone

from weather_analysis.collect import _store
from weather_analysis.config import Config, Sensor
from weather_analysis.db import Measurement, connect


def config(**overrides) -> Config:
    base = dict(
        latitude=53.6, longitude=9.9,
        ha_sensors=[Sensor("sensor.living_room_temperature", "Living room", "inside")],
    )
    base.update(overrides)
    return Config(**base)


def m(value, ts_hour):
    return Measurement(
        ts=datetime(2026, 7, 14, ts_hour, tzinfo=timezone.utc),
        source="home_assistant", sensor="sensor.living_room_temperature",
        name="Living room", area="inside", metric="temperature",
        value=value, unit="°C",
    )


def test_store_drops_implausible_and_reports(tmp_path, capsys):
    conn = connect(tmp_path / "t.db")
    inserted = _store(conn, [m(22.0, 1), m(0.0, 2), m(21.5, 3)], config(), "home-assistant")

    assert inserted == 2
    assert "1 implausible dropped" in capsys.readouterr().out

    stored = conn.execute("select value from measurements order by ts").fetchall()
    assert [r[0] for r in stored] == [22.0, 21.5]


def test_store_says_nothing_about_drops_when_there_are_none(tmp_path, capsys):
    conn = connect(tmp_path / "t.db")
    _store(conn, [m(22.0, 1)], config(), "home-assistant")
    assert "implausible" not in capsys.readouterr().out


def test_store_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    rows = [m(22.0, 1)]
    assert _store(conn, rows, config(), "x") == 1
    assert _store(conn, rows, config(), "x") == 0
