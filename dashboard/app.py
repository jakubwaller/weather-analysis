"""Streamlit dashboard: analyse collected weather + Home Assistant data.

Run with `weather-analysis dashboard` (or `streamlit run dashboard/app.py`).
The database path comes from the WEATHER_DB environment variable, falling
back to data/weather.db.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from weather_analysis.analysis import (
    daily_frame,
    prepare_series,
    resample_rule,
    sensor_labels,
)

DB_PATH = Path(os.environ.get("WEATHER_DB", "data/weather.db"))

# ---------------------------------------------------------------- palette ---
# Validated categorical palette (light mode) — hues are assigned to sensors in
# a fixed order and never cycled; color follows the sensor, not its rank.
CATEGORICAL = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7",
               "#e34948", "#e87ba4", "#eb6834"]
SEQUENTIAL_BLUES = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
                    "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
                    "#184f95", "#104281", "#0d366b"]
DIVERGING = [(0.0, "#2a78d6"), (0.5, "#f0efec"), (1.0, "#e34948")]  # blue-gray-red
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SURFACE = "#fcfcfb"

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

LAYOUT = dict(
    font=dict(family=FONT, color=INK_SECONDARY, size=13),
    paper_bgcolor=SURFACE,
    plot_bgcolor=SURFACE,
    margin=dict(l=8, r=8, t=48, b=8),
    title=dict(font=dict(size=15, color=INK), x=0, xanchor="left"),
    xaxis=dict(gridcolor=GRID, linecolor=BASELINE, zeroline=False,
               tickfont=dict(color=INK_MUTED)),
    yaxis=dict(gridcolor=GRID, linecolor=BASELINE, zeroline=False,
               tickfont=dict(color=INK_MUTED)),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0,
                font=dict(color=INK_SECONDARY)),
    hoverlabel=dict(bgcolor="#ffffff", font=dict(family=FONT, color=INK)),
    hovermode="x unified",
)

METRIC_LABELS = {
    "temperature": "Temperature (°C)",
    "apparent_temperature": "Apparent temperature (°C)",
    "humidity": "Humidity (%)",
    "pressure": "Pressure (hPa)",
    "wind_speed": "Wind speed (km/h)",
    "precipitation": "Precipitation (mm)",
    "cloud_cover": "Cloud cover (%)",
}

# ------------------------------------------------------------------- data ---


@st.cache_data(ttl=60)
def load_data(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT ts, source, sensor, name, area, metric, value, unit FROM measurements",
            conn,
        )
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


# ------------------------------------------------------------------ charts --


def line_chart(series: pd.DataFrame, colors: dict[str, str], title: str,
               unit: str, order: list[str]) -> go.Figure:
    fig = go.Figure()
    for label in order:
        part = series[series["label"] == label]
        if part.empty:
            continue
        fig.add_trace(go.Scatter(
            x=part["ts"], y=part["value"], name=label,
            mode="lines", line=dict(color=colors[label], width=2),
            hovertemplate="%{y:.1f} " + unit + "<extra>" + label + "</extra>",
        ))
    fig.update_layout(**LAYOUT, title_text=title, showlegend=len(fig.data) > 1)
    return fig


def delta_chart(delta: pd.DataFrame, title: str) -> go.Figure:
    """Inside − outside difference as a diverging bar around a zero baseline:
    warm (red) when inside is warmer, cool (blue) when outside is warmer."""
    colors = ["#e34948" if v >= 0 else "#2a78d6" for v in delta["value"]]
    fig = go.Figure(go.Bar(
        x=delta["ts"], y=delta["value"], marker_color=colors, marker_line_width=0,
        hovertemplate="%{y:+.1f} °C<extra>inside − outside</extra>",
    ))
    fig.add_hline(y=0, line_color=BASELINE, line_width=1)
    fig.update_layout(**LAYOUT, title_text=title, bargap=0, showlegend=False)
    return fig


def _fill(color: str, alpha: float = 0.18) -> str:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def daily_range_chart(daily: pd.DataFrame, title: str, color: str = "#2a78d6") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=daily.index, y=daily["max"], name="daily max", mode="lines",
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=daily.index, y=daily["min"], name="min–max range", mode="lines",
        line=dict(width=0), fill="tonexty", fillcolor=_fill(color),
        hovertemplate="min %{y:.1f} °C<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=daily.index, y=daily["mean"], name="daily mean", mode="lines",
        line=dict(color=color, width=2),
        hovertemplate="mean %{y:.1f} °C<extra></extra>",
    ))
    fig.update_layout(**LAYOUT, title_text=title)
    return fig


def heatmap_chart(outside: pd.DataFrame, title: str) -> go.Figure:
    sub = outside.copy()
    sub["day"] = sub["ts"].dt.strftime("%b %d")
    sub["day_key"] = sub["ts"].dt.floor("D")
    sub["hour"] = sub["ts"].dt.hour
    grid = sub.pivot_table(index="hour", columns="day_key", values="value", aggfunc="mean")
    fig = go.Figure(go.Heatmap(
        z=grid.values,
        x=[d.strftime("%b %d") for d in grid.columns],
        y=grid.index,
        colorscale=[[i / (len(SEQUENTIAL_BLUES) - 1), c] for i, c in enumerate(SEQUENTIAL_BLUES)],
        xgap=2, ygap=2,
        colorbar=dict(title="°C", outlinewidth=0, tickfont=dict(color=INK_MUTED)),
        hovertemplate="%{x} %{y}:00 · %{z:.1f} °C<extra></extra>",
    ))
    layout = {**LAYOUT, "hovermode": "closest"}
    fig.update_layout(**layout, title_text=title,
                      yaxis_title="hour of day (UTC)")
    return fig


def scatter_chart(pair: pd.DataFrame, inside_label: str, title: str) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=pair["outside"], y=pair["inside"], mode="markers",
        marker=dict(color="#2a78d6", size=8, opacity=0.45,
                    line=dict(color=SURFACE, width=1)),
        hovertemplate="outside %{x:.1f} °C · inside %{y:.1f} °C<extra></extra>",
    ))
    layout = {**LAYOUT, "hovermode": "closest"}
    fig.update_layout(**layout, title_text=title,
                      xaxis_title="Outside (°C)", yaxis_title=f"{inside_label} (°C)")
    return fig


# --------------------------------------------------------------------- app --

st.set_page_config(page_title="Weather analysis", page_icon="🌤️", layout="wide")

if not DB_PATH.exists():
    st.title("Weather analysis")
    st.warning(
        f"No database found at `{DB_PATH}`.\n\n"
        "Collect some data first:\n\n"
        "- `weather-analysis collect` — one collection run\n"
        "- `weather-analysis backfill --days 30` — fetch past data\n"
        "- `weather-analysis demo` — synthetic data to try the dashboard"
    )
    st.stop()

df = load_data(str(DB_PATH))
if df.empty:
    st.title("Weather analysis")
    st.warning("The database is empty — run `weather-analysis collect` or `weather-analysis demo` first.")
    st.stop()

# --- sidebar filters ---------------------------------------------------------
st.sidebar.header("Filters")

RANGES = {
    "Last 24 hours": timedelta(days=1),
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "Last 90 days": timedelta(days=90),
    "All data": None,
    "Custom": "custom",
}
choice = st.sidebar.radio("Time range", list(RANGES), index=1)
now = datetime.now(timezone.utc)
if RANGES[choice] == "custom":
    default_start = (now - timedelta(days=7)).date()
    picked = st.sidebar.date_input(
        "Custom range", value=(default_start, now.date()), max_value=now.date(),
    )
    start_date = picked[0] if isinstance(picked, tuple) else picked
    end_date = picked[1] if isinstance(picked, tuple) and len(picked) > 1 else start_date
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)
elif RANGES[choice] is None:
    start, end = df["ts"].min().to_pydatetime(), now
else:
    start, end = now - RANGES[choice], now

window = df[(df["ts"] >= start) & (df["ts"] <= end)]

# Fixed color per sensor across the WHOLE database, so filters never repaint
# surviving series. Outside API first, then sensors in first-seen order.
all_labels = sensor_labels(df[df["metric"] == "temperature"])
ordered_labels = sorted(
    all_labels.unique(),
    key=lambda l: (0 if l.startswith("Outside (Open-Meteo)") else 1, l),
)
COLOR_BY_LABEL = {label: CATEGORICAL[i % len(CATEGORICAL)]
                  for i, label in enumerate(ordered_labels[: len(CATEGORICAL)])}
ordered_labels = ordered_labels[: len(CATEGORICAL)]  # 8-series ceiling

selected = st.sidebar.multiselect("Temperature sensors", ordered_labels,
                                  default=ordered_labels)

extra_metrics = sorted(
    m for m in window["metric"].unique() if m != "temperature" and m in METRIC_LABELS
)
selected_metric = st.sidebar.selectbox(
    "Secondary metric", extra_metrics,
    format_func=lambda m: METRIC_LABELS[m],
) if extra_metrics else None

st.title("Weather analysis")
st.caption(
    f"{start:%d %b %Y %H:%M} – {end:%d %b %Y %H:%M} UTC · "
    f"{len(window):,} readings in range · database `{DB_PATH}`"
)

if window.empty:
    st.info("No data in the selected range.")
    st.stop()

rule = resample_rule(end - start)
temps = prepare_series(window, "temperature", rule)
temps = temps[temps["label"].isin(selected)]

# --- KPI row -----------------------------------------------------------------
temp_now = window[window["metric"] == "temperature"].sort_values("ts")
latest = temp_now.groupby(["name", "area"]).tail(1)
latest_inside = latest[latest["area"] == "inside"]["value"].mean()
latest_outside = latest[latest["area"] == "outside"]["value"].mean()

outside_all = temp_now[temp_now["area"] == "outside"]
cols = st.columns(4)
if pd.notna(latest_inside):
    cols[0].metric("Inside now (avg)", f"{latest_inside:.1f} °C")
if pd.notna(latest_outside):
    cols[1].metric("Outside now", f"{latest_outside:.1f} °C")
if pd.notna(latest_inside) and pd.notna(latest_outside):
    cols[2].metric("Inside − outside", f"{latest_inside - latest_outside:+.1f} °C")
if not outside_all.empty:
    cols[3].metric("Outside min / max in range",
                   f"{outside_all['value'].min():.1f} / {outside_all['value'].max():.1f} °C")

# --- charts ------------------------------------------------------------------
tab_trends, tab_compare, tab_patterns, tab_table = st.tabs(
    ["Trends", "Inside vs outside", "Patterns", "Data table"]
)

with tab_trends:
    if temps.empty:
        st.info("No temperature sensors selected.")
    else:
        st.plotly_chart(
            line_chart(temps, COLOR_BY_LABEL, "Temperature over time", "°C", ordered_labels),
            width="stretch",
        )
    if selected_metric:
        other = prepare_series(window, selected_metric, rule)
        unit = METRIC_LABELS[selected_metric].split("(")[-1].rstrip(")")
        st.plotly_chart(
            line_chart(other, COLOR_BY_LABEL | {
                l: CATEGORICAL[0] for l in other["label"].unique()
                if l not in COLOR_BY_LABEL
            }, METRIC_LABELS[selected_metric] + " over time", unit,
                sorted(other["label"].unique())),
            width="stretch",
        )

with tab_compare:
    inside_hourly = (
        temp_now[temp_now["area"] == "inside"]
        .set_index("ts")["value"].resample("1h").mean()
    )
    outside_hourly = outside_all.set_index("ts")["value"].resample("1h").mean()
    pair = pd.DataFrame({"inside": inside_hourly, "outside": outside_hourly}).dropna()
    if pair.empty:
        st.info("Need both inside and outside temperature data in the selected "
                "range for this comparison — add Home Assistant sensors with "
                "`area: inside` to your config.")
    else:
        delta = pair["inside"] - pair["outside"]
        st.plotly_chart(
            delta_chart(delta.rename("value").rename_axis("ts").reset_index(),
                        "Inside − outside temperature (hourly)"),
            width="stretch",
        )
        st.caption("Above zero (red): inside warmer than outside · "
                   "below zero (blue): outside warmer.")
        left, right = st.columns(2)
        with left:
            st.plotly_chart(
                scatter_chart(pair, "Inside (avg)",
                              "Inside vs outside temperature (hourly means)"),
                width="stretch",
            )
        with right:
            corr = pair["inside"].corr(pair["outside"])
            lag_hours, lag_corr = 0, corr
            for lag in range(1, 13):
                c = pair["inside"].corr(pair["outside"].shift(lag))
                if pd.notna(c) and c > lag_corr:
                    lag_hours, lag_corr = lag, c
            st.markdown("#### How coupled is inside to outside?")
            st.metric("Correlation (same hour)", f"{corr:.2f}")
            st.metric("Best correlation with lag", f"{lag_corr:.2f}",
                      delta=f"outside leads by {lag_hours} h", delta_color="off")
            st.caption(
                "A high lagged correlation means outside temperature changes take "
                "that many hours to show up indoors — a rough measure of your "
                "home's thermal inertia."
            )

with tab_patterns:
    if outside_all.empty:
        st.info("No outside temperature data in the selected range.")
    else:
        st.plotly_chart(
            daily_range_chart(daily_frame(outside_all, "temperature"),
                              "Outside daily min / mean / max"),
            width="stretch",
        )

    # not temp_now: the true hourly min/max live under their own metrics
    inside_all = window[window["area"] == "inside"]
    rooms = sorted(inside_all["name"].unique())
    if rooms:
        room = st.selectbox("Room", rooms)
        room_daily = daily_frame(inside_all[inside_all["name"] == room], "temperature")
        if not room_daily.empty:
            st.plotly_chart(
                daily_range_chart(room_daily, f"{room} daily min / mean / max",
                                  color=COLOR_BY_LABEL.get(f"{room} · inside", "#2a78d6")),
                width="stretch",
            )
            st.caption(
                "Backfilled days use the true hourly min/max Home Assistant recorded; "
                "recent days derive them from the collected readings."
            )

    if not outside_all.empty:
        st.plotly_chart(
            heatmap_chart(outside_all, "Outside temperature by hour and day"),
            width="stretch",
        )

with tab_table:
    show = window.sort_values("ts", ascending=False).copy()
    show["ts"] = show["ts"].dt.strftime("%Y-%m-%d %H:%M UTC")
    st.dataframe(
        show[["ts", "name", "area", "metric", "value", "unit", "source"]],
        width="stretch", hide_index=True, height=480,
    )
    st.download_button(
        "Download CSV",
        window.to_csv(index=False).encode(),
        file_name="weather-data.csv",
        mime="text/csv",
    )
