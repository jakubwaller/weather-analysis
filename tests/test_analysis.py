from datetime import timedelta

import pandas as pd
import pytest

from weather_analysis.analysis import daily_frame, prepare_series, resample_rule, sensor_labels


def frame(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"ts": ts, "name": name, "area": area, "metric": metric, "value": value}
         for ts, name, area, metric, value in rows]
    )


def test_sensor_labels_combine_name_and_area():
    df = frame([(pd.Timestamp("2026-01-01", tz="UTC"), "Bedroom", "inside", "temperature", 21.0)])
    assert sensor_labels(df).tolist() == ["Bedroom · inside"]


def test_resample_rule_widens_with_span():
    assert resample_rule(timedelta(days=1)) is None
    assert resample_rule(timedelta(days=7)) == "30min"
    assert resample_rule(timedelta(days=30)) == "1h"
    assert resample_rule(timedelta(days=200)) == "3h"


def test_prepare_series_preserves_gap_as_nan():
    # two clusters two months apart, like the Apr-Jun outage
    ts = list(pd.date_range("2026-04-05", periods=4, freq="1h", tz="UTC")) + \
         list(pd.date_range("2026-06-08", periods=4, freq="1h", tz="UTC"))
    df = frame([(t, "Living room", "inside", "temperature", 21.0) for t in ts])

    out = prepare_series(df, "temperature", "3h")

    # NaN rows are what break the plotly line across the gap; dropping them
    # would draw a straight line through two months of missing data
    assert out["value"].isna().any()
    assert out["label"].unique().tolist() == ["Living room · inside"]


def test_prepare_series_without_rule_returns_raw_points():
    ts = pd.date_range("2026-07-14", periods=3, freq="10min", tz="UTC")
    df = frame([(t, "Bedroom", "inside", "temperature", 21.0) for t in ts])
    out = prepare_series(df, "temperature", None)
    assert len(out) == 3
    assert not out["value"].isna().any()


def test_daily_frame_prefers_true_min_max():
    ts = pd.date_range("2026-01-10", periods=2, freq="1h", tz="UTC")
    df = frame([
        (ts[0], "Bedroom", "inside", "temperature", 20.0),
        (ts[1], "Bedroom", "inside", "temperature", 22.0),
        (ts[0], "Bedroom", "inside", "temperature_min", 10.1),
        (ts[1], "Bedroom", "inside", "temperature_max", 24.0),
    ])
    daily = daily_frame(df, "temperature")
    assert daily["mean"].iloc[0] == pytest.approx(21.0)
    assert daily["min"].iloc[0] == pytest.approx(10.1)  # true min, not min of means
    assert daily["max"].iloc[0] == pytest.approx(24.0)


def test_daily_frame_preserves_gap_as_nan():
    # same two-month outage: the daily band must break too, not just the trend line
    ts = list(pd.date_range("2026-04-04", periods=2, freq="1D", tz="UTC")) + \
         list(pd.date_range("2026-06-08", periods=2, freq="1D", tz="UTC"))
    df = frame([(t, "Bedroom", "inside", "temperature", 21.0) for t in ts])

    daily = daily_frame(df, "temperature")

    assert daily["mean"].isna().any(), "gap days dropped: the band would span the outage"
    # the four real days survive
    assert daily["mean"].notna().sum() == 4


def test_daily_frame_derives_min_max_when_statistics_absent():
    # the live 10-minute period has no statistics rows
    ts = pd.date_range("2026-07-14", periods=2, freq="1h", tz="UTC")
    df = frame([
        (ts[0], "Bedroom", "inside", "temperature", 20.0),
        (ts[1], "Bedroom", "inside", "temperature", 22.0),
    ])
    daily = daily_frame(df, "temperature")
    assert daily["min"].iloc[0] == pytest.approx(20.0)
    assert daily["max"].iloc[0] == pytest.approx(22.0)
