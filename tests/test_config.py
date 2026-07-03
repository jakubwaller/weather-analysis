import pytest

from weather_analysis.config import ConfigError, load_config

MINIMAL = """
location:
  latitude: 50.0
  longitude: 14.4
"""

FULL = """
location:
  latitude: 50.0
  longitude: 14.4
  timezone: Europe/Prague
database:
  path: mydata/w.db
home_assistant:
  url: http://ha.local:8123/
  token: ${TEST_HA_TOKEN}
  sensors:
    - entity_id: sensor.living_room_temperature
      name: Living room
      area: inside
    - entity_id: sensor.balcony_temperature
      name: Balcony
      area: outside
      metric: temperature
collection:
  interval_minutes: 5
"""


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_minimal_config(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    assert config.latitude == 50.0
    assert config.open_meteo_enabled
    assert not config.ha_enabled
    assert config.interval_minutes == 10


def test_full_config_with_env_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HA_TOKEN", "secret123")
    config = load_config(write(tmp_path, FULL))
    assert config.ha_enabled
    assert config.ha_url == "http://ha.local:8123"  # trailing slash stripped
    assert config.ha_token == "secret123"
    assert [s.name for s in config.ha_sensors] == ["Living room", "Balcony"]
    assert config.ha_sensors[0].metric == "temperature"
    assert str(config.db_path) == "mydata/w.db"
    assert config.interval_minutes == 5


def test_ha_without_token_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.delenv("TEST_HA_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="token"):
        load_config(write(tmp_path, FULL))


def test_missing_location_fails(tmp_path):
    with pytest.raises(ConfigError, match="latitude"):
        load_config(write(tmp_path, "location: {latitude: 1}"))
