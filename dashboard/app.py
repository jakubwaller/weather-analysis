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
    contiguous_blocks,
    daily_frame,
    prepare_series,
    resample_rule,
    sensor_labels,
)
from weather_analysis.db import to_utc_iso

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
    margin=dict(l=8, r=8, t=8, b=8),
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
def load_data(db_path: str, start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
    """Load only the selected window; the full table is seconds of Pi time.

    ts is compared as text: every row is written through to_utc_iso, so the
    strings share one format and lexicographic order is chronological order —
    which also lets SQLite use idx_measurements_ts.
    """
    query = "SELECT ts, source, sensor, name, area, metric, value, unit FROM measurements"
    clauses, params = [], []
    if start_iso:
        clauses.append("ts >= ?")
        params.append(start_iso)
    if end_iso:
        clauses.append("ts <= ?")
        params.append(end_iso)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


@st.cache_data(ttl=60)
def earliest_iso(db_path: str) -> str | None:
    """MIN over the text column is chronological for the same reason load_data
    can compare it; None doubles as the empty-database signal."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT MIN(ts) FROM measurements").fetchone()[0]


@st.cache_data(ttl=60)
def temperature_labels(db_path: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT name, area FROM measurements WHERE metric = 'temperature'"
        ).fetchall()
    return list(sensor_labels(pd.DataFrame(rows, columns=["name", "area"])).unique())


@st.cache_data(ttl=60)
def window_csv(db_path: str, start_iso: str | None, end_iso: str | None,
               _window: pd.DataFrame) -> bytes:
    """Keyed on the query bounds rather than the frame: hashing the frame every
    rerun would cost about as much as the serialisation this cache avoids."""
    return _window.to_csv(index=False).encode()


# ------------------------------------------------------------------ charts --


def show_chart(title: str, fig: go.Figure) -> None:
    """Render the title as page text above the figure. An in-figure title
    collides with the legend on narrow screens: plotly grows the top margin
    to fit the title or the wrapped legend rows, not both."""
    st.markdown(f"**{title}**")
    st.plotly_chart(fig, width="stretch")


def line_chart(series: pd.DataFrame, colors: dict[str, str],
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
    fig.update_layout(**LAYOUT, showlegend=len(fig.data) > 1)
    return fig


def delta_chart(delta: pd.DataFrame) -> go.Figure:
    """Inside − outside difference as a diverging bar around a zero baseline:
    warm (red) when inside is warmer, cool (blue) when outside is warmer."""
    colors = ["#e34948" if v >= 0 else "#2a78d6" for v in delta["value"]]
    fig = go.Figure(go.Bar(
        x=delta["ts"], y=delta["value"], marker_color=colors, marker_line_width=0,
        hovertemplate="%{y:+.1f} °C<extra>inside − outside</extra>",
    ))
    fig.add_hline(y=0, line_color=BASELINE, line_width=1)
    fig.update_layout(**LAYOUT, bargap=0, showlegend=False)
    return fig


def _fill(color: str, alpha: float = 0.18) -> str:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def daily_range_chart(daily: pd.DataFrame, color: str = "#2a78d6") -> go.Figure:
    fig = go.Figure()
    # one band per contiguous run: a single filled trace would bridge the gaps
    for first, block in enumerate(contiguous_blocks(daily)):
        fig.add_trace(go.Scatter(
            x=block.index, y=block["max"], name="daily max", mode="lines",
            line=dict(width=0), showlegend=False, hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=block.index, y=block["min"], name="min–max range", mode="lines",
            line=dict(width=0), fill="tonexty", fillcolor=_fill(color),
            showlegend=first == 0, legendgroup="range",
            hovertemplate="min %{y:.1f} °C<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=block.index, y=block["mean"], name="daily mean", mode="lines",
            line=dict(color=color, width=2),
            showlegend=first == 0, legendgroup="mean",
            hovertemplate="mean %{y:.1f} °C<extra></extra>",
        ))
    fig.update_layout(**LAYOUT)
    return fig


def heatmap_chart(outside: pd.DataFrame) -> go.Figure:
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
    fig.update_layout(**layout, yaxis_title="hour of day (UTC)")
    return fig


def scatter_chart(pair: pd.DataFrame, inside_label: str) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=pair["outside"], y=pair["inside"], mode="markers",
        marker=dict(color="#2a78d6", size=8, opacity=0.45,
                    line=dict(color=SURFACE, width=1)),
        hovertemplate="outside %{x:.1f} °C · inside %{y:.1f} °C<extra></extra>",
    ))
    layout = {**LAYOUT, "hovermode": "closest"}
    fig.update_layout(**layout,
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

earliest = earliest_iso(str(DB_PATH))
if earliest is None:
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
# floored to the minute: a start that shifts every rerun would never hit the
# load_data cache, and the minute rollover is the refresh cadence anyway
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
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
    start, end = datetime.fromisoformat(earliest), now
else:
    start, end = now - RANGES[choice], now

if RANGES[choice] is None:
    start_iso = end_iso = None
else:
    start_iso, end_iso = to_utc_iso(start), to_utc_iso(end)
window = load_data(str(DB_PATH), start_iso, end_iso)

# Fixed color per sensor across the WHOLE database, so filters never repaint
# surviving series. Outside API first, then sensors in first-seen order.
ordered_labels = sorted(
    temperature_labels(str(DB_PATH)),
    key=lambda l: (0 if l.startswith("Outside (Open-Meteo)") else 1, l),
)
COLOR_BY_LABEL = {label: CATEGORICAL[i % len(CATEGORICAL)]
                  for i, label in enumerate(ordered_labels[: len(CATEGORICAL)])}
ordered_labels = ordered_labels[: len(CATEGORICAL)]  # 8-series ceiling

selected = st.sidebar.multiselect("Temperature sensors", ordered_labels,
                                  default=ordered_labels)

# humidity has its own chart in Trends, so it is not offered here
extra_metrics = sorted(
    m for m in window["metric"].unique()
    if m not in ("temperature", "humidity") and m in METRIC_LABELS
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


def om_first(names: pd.Series) -> pd.Series:
    """Sort key: Open-Meteo before the local sensors, like the charts."""
    return names.map(lambda n: (0 if n.startswith("Outside (Open-Meteo)") else 1, n))


latest_by_inside = latest[latest["area"] == "inside"].sort_values("name")
latest_by_outside = latest[latest["area"] == "outside"].sort_values("name", key=om_first)
hum_latest = (
    window[window["metric"] == "humidity"].sort_values("ts")
    .groupby(["name", "area"]).tail(1)
)
hum_by_outside = hum_latest[hum_latest["area"] == "outside"].sort_values("name", key=om_first)

# Rows of 4: one long row squeezes tiles until labels and values truncate
# on narrow viewports (the dashboard is used at high zoom).
PER_ROW = 4


def tile_section(title: str, tiles: list[tuple[str, str]]) -> None:
    if not tiles:
        return
    st.caption(title)
    for i in range(0, len(tiles), PER_ROW):
        for col, (label, value) in zip(st.columns(PER_ROW), tiles[i:i + PER_ROW]):
            col.metric(label, value)


inside_tiles = [(row["name"], f"{row['value']:.1f} °C")
                for _, row in latest_by_inside.iterrows()]
if pd.notna(latest_inside):
    inside_tiles.append(("Average", f"{latest_inside:.1f} °C"))

def outside_label(name: str) -> str:
    """The section header already says outside, so drop it from the sensor
    name: 'Outside (Open-Meteo)' → 'Open-Meteo', 'Zigbee outside' → 'Zigbee'.
    Long labels truncate in the tile at the zoom level this page is used at."""
    if name.startswith("Outside (") and name.endswith(")"):
        return name[len("Outside ("):-1]
    return name.replace(" outside", "").replace("outside ", "").strip() or name


outside_tiles = [(outside_label(row["name"]), f"{row['value']:.1f} °C")
                 for _, row in latest_by_outside.iterrows()]
outside_tiles += [(f"{outside_label(row['name'])} humidity", f"{row['value']:.0f} %")
                  for _, row in hum_by_outside.iterrows()]

summary_tiles = []
if pd.notna(latest_inside) and pd.notna(latest_outside):
    summary_tiles.append(("Inside − outside now",
                          f"{latest_inside - latest_outside:+.1f} °C"))
if not outside_all.empty:
    summary_tiles.append(("Outside min / max in range",
                          f"{outside_all['value'].min():.1f}–{outside_all['value'].max():.1f} °C"))

tile_section("Inside now", inside_tiles)
tile_section("Outside now", outside_tiles)
tile_section("Summary", summary_tiles)

# --- charts ------------------------------------------------------------------
tab_trends, tab_compare, tab_patterns, tab_table = st.tabs(
    ["Trends", "Inside vs outside", "Patterns", "Data table"]
)

with tab_trends:
    if temps.empty:
        st.info("No temperature sensors selected.")
    else:
        show_chart("Temperature over time",
                   line_chart(temps, COLOR_BY_LABEL, "°C", ordered_labels))
    hums = prepare_series(window, "humidity", rule)
    if not hums.empty:
        # Same sensors as the temperature chart, so reuse their colors; a
        # humidity-only sensor takes the first hue not claimed by any of them.
        hum_order = sorted(
            hums["label"].unique(),
            key=lambda l: (0 if l.startswith("Outside (Open-Meteo)") else 1, l),
        )
        free = [c for c in CATEGORICAL if c not in COLOR_BY_LABEL.values()] or CATEGORICAL
        hum_colors = COLOR_BY_LABEL | {
            l: free[i % len(free)]
            for i, l in enumerate(l for l in hum_order if l not in COLOR_BY_LABEL)
        }
        show_chart("Humidity over time",
                   line_chart(hums, hum_colors, "%", hum_order))
    if selected_metric:
        other = prepare_series(window, selected_metric, rule)
        unit = METRIC_LABELS[selected_metric].split("(")[-1].rstrip(")")
        show_chart(
            METRIC_LABELS[selected_metric] + " over time",
            line_chart(other, COLOR_BY_LABEL | {
                l: CATEGORICAL[0] for l in other["label"].unique()
                if l not in COLOR_BY_LABEL
            }, unit, sorted(other["label"].unique())),
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
        show_chart("Inside − outside temperature (hourly)",
                   delta_chart(delta.rename("value").rename_axis("ts").reset_index()))
        st.caption("Above zero (red): inside warmer than outside · "
                   "below zero (blue): outside warmer.")
        left, right = st.columns(2)
        with left:
            show_chart("Inside vs outside temperature (hourly means)",
                       scatter_chart(pair, "Inside (avg)"))
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
        show_chart("Outside daily min / mean / max",
                   daily_range_chart(daily_frame(outside_all, "temperature")))

    # not temp_now: the true hourly min/max live under their own metrics
    inside_all = window[window["area"] == "inside"]
    rooms = sorted(inside_all["name"].unique())
    if rooms:
        room = st.selectbox("Room", rooms)
        room_daily = daily_frame(inside_all[inside_all["name"] == room], "temperature")
        if not room_daily.empty:
            show_chart(f"{room} daily min / mean / max",
                       daily_range_chart(room_daily,
                                         color=COLOR_BY_LABEL.get(f"{room} · inside", "#2a78d6")))
            st.caption(
                "Backfilled days use the true hourly min/max Home Assistant recorded; "
                "recent days derive them from the collected readings."
            )

    if not outside_all.empty:
        show_chart("Outside temperature by hour and day",
                   heatmap_chart(outside_all))

with tab_table:
    show = window.sort_values("ts", ascending=False).copy()
    show["ts"] = show["ts"].dt.strftime("%Y-%m-%d %H:%M UTC")
    st.dataframe(
        show[["ts", "name", "area", "metric", "value", "unit", "source"]],
        width="stretch", hide_index=True, height=480,
    )
    st.download_button(
        "Download CSV",
        window_csv(str(DB_PATH), start_iso, end_iso, window),
        file_name="weather-data.csv",
        mime="text/csv",
    )
