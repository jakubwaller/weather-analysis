from datetime import datetime, timezone

from weather_analysis.collectors import home_assistant, open_meteo
from weather_analysis.config import Config, Sensor


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.status_code < 400


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.last_url = None
        self.last_params = None

    def get(self, url, **kwargs):
        self.last_url = url
        self.last_params = kwargs.get("params")
        return FakeResponse(self.payload)


def config(**overrides) -> Config:
    base = dict(
        latitude=50.0, longitude=14.4,
        open_meteo_metrics=["temperature", "humidity"],
        ha_url="http://ha.local:8123", ha_token="t", ha_enabled=True,
        ha_sensors=[Sensor("sensor.living_room_temperature", "Living room", "inside")],
    )
    base.update(overrides)
    return Config(**base)


def test_open_meteo_current():
    session = FakeSession({
        "current": {"time": "2026-07-03T12:00", "temperature_2m": 24.3,
                    "relative_humidity_2m": 55},
    })
    rows = open_meteo.fetch_current(config(), session=session)
    assert {(r.metric, r.value) for r in rows} == {("temperature", 24.3), ("humidity", 55.0)}
    assert all(r.ts == datetime(2026, 7, 3, 12, tzinfo=timezone.utc) for r in rows)
    assert all(r.area == "outside" for r in rows)


def test_open_meteo_history_skips_future_and_none():
    now = datetime.now(timezone.utc)
    past = (now.replace(minute=0, second=0, microsecond=0)).strftime("%Y-%m-%dT%H:%M")
    future = "2999-01-01T00:00"
    session = FakeSession({
        "hourly": {"time": [past, future],
                   "temperature_2m": [20.0, 21.0],
                   "relative_humidity_2m": [None, 50]},
    })
    rows = open_meteo.fetch_history(config(), days=5, session=session)
    assert [(r.metric, r.value) for r in rows] == [("temperature", 20.0)]


def test_ha_current_parses_and_skips_unavailable():
    session = FakeSession({
        "state": "23.4",
        "attributes": {"unit_of_measurement": "°C"},
    })
    rows = home_assistant.fetch_current(config(), session=session)
    assert len(rows) == 1
    assert rows[0].value == 23.4
    assert rows[0].unit == "°C"
    assert rows[0].name == "Living room"

    session = FakeSession({"state": "unavailable", "attributes": {}})
    assert home_assistant.fetch_current(config(), session=session) == []


def test_ha_history_minimal_response():
    session = FakeSession([
        [
            {"entity_id": "sensor.living_room_temperature", "state": "22.0",
             "last_changed": "2026-07-01T10:00:00+00:00"},
            {"state": "22.5", "last_changed": "2026-07-01T11:00:00Z"},
            {"state": "unknown", "last_changed": "2026-07-01T12:00:00Z"},
        ],
    ])
    rows = home_assistant.fetch_history(config(), days=3, session=session)
    assert [r.value for r in rows] == [22.0, 22.5]
    assert rows[1].ts == datetime(2026, 7, 1, 11, tzinfo=timezone.utc)
