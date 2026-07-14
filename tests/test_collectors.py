from datetime import datetime, timezone

from weather_analysis.collectors import home_assistant, home_assistant_stats, open_meteo
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


def test_open_meteo_history_uses_archive_without_day_cap():
    now = datetime.now(timezone.utc)
    past = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    session = FakeSession({
        "hourly": {"time": [past], "temperature_2m": [20.0], "relative_humidity_2m": [50]},
    })
    open_meteo.fetch_history(config(), days=365, session=session)

    assert session.last_url == open_meteo.ARCHIVE_URL
    assert "past_days" not in session.last_params
    assert "forecast_days" not in session.last_params
    # 365 days must not be clamped to the old 92-day ceiling
    start = datetime.strptime(session.last_params["start_date"], "%Y-%m-%d").date()
    assert (now.date() - start).days == 365
    assert session.last_params["end_date"] == now.date().strftime("%Y-%m-%d")


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


class FakeStatsClient:
    def __init__(self, result):
        self.result = result
        self.last_kwargs = None
        self.closed = False

    def statistics_during_period(self, **kwargs):
        self.last_kwargs = kwargs
        return self.result

    def close(self):
        self.closed = True


def test_stats_row_becomes_mean_min_max_measurements():
    client = FakeStatsClient({
        "sensor.living_room_temperature": [
            {"start": 1768003200000, "mean": 24.02, "min": 23.9, "max": 24.2},
        ],
    })
    rows = home_assistant_stats.fetch_statistics(config(), days=30, client=client)

    assert {(r.metric, r.value) for r in rows} == {
        ("temperature", 24.02), ("temperature_min", 23.9), ("temperature_max", 24.2),
    }
    assert all(r.source == "home_assistant_stats" for r in rows)
    assert all(r.area == "inside" and r.name == "Living room" for r in rows)
    # Home Assistant sends epoch milliseconds
    assert all(r.ts == datetime(2026, 1, 10, 0, tzinfo=timezone.utc) for r in rows)
    # the response carries no unit
    assert all(r.unit == "°C" for r in rows)


def test_stats_requests_hourly_period_and_all_three_types():
    client = FakeStatsClient({"sensor.living_room_temperature": []})
    home_assistant_stats.fetch_statistics(config(), days=30, client=client)

    assert client.last_kwargs["period"] == "hour"
    assert sorted(client.last_kwargs["types"]) == ["max", "mean", "min"]
    assert client.last_kwargs["statistic_ids"] == ["sensor.living_room_temperature"]


def test_stats_skips_none_fields_without_storing_zero():
    client = FakeStatsClient({
        "sensor.living_room_temperature": [
            {"start": 1768003200000, "mean": 21.0, "min": None, "max": None},
        ],
    })
    rows = home_assistant_stats.fetch_statistics(config(), days=30, client=client)
    assert [(r.metric, r.value) for r in rows] == [("temperature", 21.0)]


def test_stats_skips_entity_missing_from_result():
    client = FakeStatsClient({})
    assert home_assistant_stats.fetch_statistics(config(), days=30, client=client) == []


def test_stats_metric_names_follow_the_sensor_metric():
    cfg = config(ha_sensors=[
        Sensor("sensor.living_room_humidity", "Living room", "inside", "humidity"),
    ])
    client = FakeStatsClient({
        "sensor.living_room_humidity": [
            {"start": 1768003200000, "mean": 55.0, "min": 54.0, "max": 56.0},
        ],
    })
    rows = home_assistant_stats.fetch_statistics(cfg, days=30, client=client)
    assert {r.metric for r in rows} == {"humidity", "humidity_min", "humidity_max"}
    assert all(r.unit == "%" for r in rows)


def test_stats_does_not_close_an_injected_client():
    client = FakeStatsClient({"sensor.living_room_temperature": []})
    home_assistant_stats.fetch_statistics(config(), days=30, client=client)
    assert not client.closed
