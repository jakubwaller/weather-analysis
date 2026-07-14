"""Pure data preparation for the dashboard.

Kept out of dashboard/app.py so it can be tested without executing a Streamlit
page; app.py keeps the figure building and page layout.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pandas as pd


def sensor_labels(df: pd.DataFrame) -> pd.Series:
    """Stable display label per sensor: 'Living room · inside'."""
    return df["name"] + " · " + df["area"]


def resample_rule(span: timedelta) -> str | None:
    """Downsample long ranges so lines stay readable."""
    if span <= timedelta(days=3):
        return None
    if span <= timedelta(days=14):
        return "30min"
    if span <= timedelta(days=45):
        return "1h"
    return "3h"


def prepare_series(df: pd.DataFrame, metric: str, rule: str | None) -> pd.DataFrame:
    """One row per (sensor label, timestamp) with the mean value in each bucket.

    Empty buckets stay NaN. Plotly breaks a line at NaN, which is what draws a
    real gap rather than a straight line across missing data.
    """
    sub = df[df["metric"] == metric].copy()
    sub["label"] = sensor_labels(sub)
    if rule:
        sub = (
            sub.set_index("ts")
            .groupby("label")["value"]
            .resample(rule)
            .mean()
            .reset_index()
        )
    return sub


def daily_frame(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Daily min/mean/max for one sensor's readings.

    Prefers the true hourly min/max Home Assistant recorded; falls back to values
    derived from the mean series, which is all the live period has — statistics
    exist only for backfilled hours.

    Days with no readings stay NaN, so the band and its line break across a gap
    instead of spanning it.
    """
    mean_src = df[df["metric"] == metric].set_index("ts")["value"]
    daily = mean_src.resample("1D").agg(["min", "mean", "max"])
    for field in ("min", "max"):
        true_src = df[df["metric"] == f"{metric}_{field}"].set_index("ts")["value"]
        if not true_src.empty:
            daily[field] = true_src.resample("1D").agg(field).combine_first(daily[field])
    return daily


def contiguous_blocks(daily: pd.DataFrame) -> Iterator[pd.DataFrame]:
    """Split a daily frame into runs of consecutive days that have data.

    A filled band has to be drawn one block at a time: plotly builds the fill
    polygon from a trace's non-null points, so a single trace spanning a gap
    bridges it even though the line itself breaks at NaN.
    """
    if daily.empty:
        return
    has_data = daily["mean"].notna()
    for _, block in daily.groupby((has_data != has_data.shift()).cumsum()):
        if block["mean"].notna().all():
            yield block
