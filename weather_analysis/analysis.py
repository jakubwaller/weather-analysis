"""Pure data preparation for the dashboard.

Kept out of dashboard/app.py so it can be tested without executing a Streamlit
page; app.py keeps the figure building and page layout.
"""

from __future__ import annotations

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
    """
    mean_src = df[df["metric"] == metric].set_index("ts")["value"]
    daily = mean_src.resample("1D").agg(["min", "mean", "max"])
    for field in ("min", "max"):
        true_src = df[df["metric"] == f"{metric}_{field}"].set_index("ts")["value"]
        if not true_src.empty:
            daily[field] = true_src.resample("1D").agg(field).combine_first(daily[field])
    return daily.dropna(subset=["mean"])
