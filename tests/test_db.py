from datetime import datetime, timezone

from weather_analysis.db import Measurement, connect, insert_measurements


def make(ts_hour: int, metric: str = "temperature", value: float = 20.0) -> Measurement:
    return Measurement(
        ts=datetime(2026, 7, 1, ts_hour, tzinfo=timezone.utc),
        source="test", sensor="sensor.x", name="X", area="inside",
        metric=metric, value=value, unit="°C",
    )


def test_insert_and_dedupe(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert insert_measurements(conn, [make(1), make(2)]) == 2
    # same (ts, source, sensor, metric) is ignored, new hour is inserted
    assert insert_measurements(conn, [make(1), make(3)]) == 1
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 3
    conn.close()


def test_naive_timestamps_treated_as_utc(tmp_path):
    conn = connect(tmp_path / "t.db")
    naive = Measurement(
        ts=datetime(2026, 7, 1, 12, 0, 0),
        source="test", sensor="s", name="S", area="outside",
        metric="temperature", value=1.0,
    )
    insert_measurements(conn, [naive])
    ts = conn.execute("SELECT ts FROM measurements").fetchone()[0]
    assert ts == "2026-07-01T12:00:00+00:00"
    conn.close()
