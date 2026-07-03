# weather-analysis

Collect **outside weather** (from the free [Open-Meteo](https://open-meteo.com) API, no API key
needed) and **your Home Assistant sensors** (inside/outside temperature, humidity, …) into one
local SQLite database — then explore them in an interactive dashboard: inside vs outside trends,
temperature deltas, daily ranges, hour-by-day heatmaps, and how strongly (and how slowly) your
indoor temperature follows the weather.

## Quick start

```bash
pip install -e .

# try it immediately with 30 days of synthetic data
weather-analysis demo
weather-analysis dashboard        # opens at http://localhost:8501
```

## Real setup

1. **Configure** — copy the example and edit it:

   ```bash
   cp config.example.yaml config.yaml
   ```

   - `location`: your latitude/longitude for Open-Meteo.
   - `home_assistant.url`: your HA instance, e.g. `http://homeassistant.local:8123`.
   - `home_assistant.token`: a long-lived access token (HA → your profile → Security →
     Long-lived access tokens). You can also set it via the `HA_TOKEN` environment variable
     instead of putting it in the file. `config.yaml` is git-ignored either way.
   - `home_assistant.sensors`: the entities to record, each with a friendly `name`,
     an `area` (`inside` / `outside`) and a `metric` (`temperature`, `humidity`, …).

2. **Backfill history** so the graphs are interesting from day one:

   ```bash
   weather-analysis backfill --days 30
   ```

   Open-Meteo provides up to 92 days of hourly history; Home Assistant as much as your
   recorder retention keeps (10 days by default).

3. **Collect continuously** — either keep a loop running:

   ```bash
   weather-analysis collect --loop
   ```

   or run one-shot collections from cron / a systemd timer:

   ```cron
   */10 * * * * cd /path/to/weather-analysis && weather-analysis collect >> collect.log 2>&1
   ```

4. **Analyse**:

   ```bash
   weather-analysis dashboard
   ```

## What the dashboard shows

- **Trends** — temperature over time for every sensor plus the outside API, and any secondary
  metric you collect (humidity, pressure, wind, …). Long ranges are downsampled automatically.
- **Inside vs outside** — the hourly inside−outside delta around a zero baseline, an
  inside-vs-outside scatter, and the correlation at increasing time lags: a rough measure of
  your home's thermal inertia (how many hours the outside weather needs to reach your couch).
- **Patterns** — outside daily min/mean/max with range band, and an hour-of-day × day heatmap
  that makes diurnal cycles and heat waves obvious.
- **Data table** — the raw readings for any range, with CSV export.

## CLI reference

| Command | What it does |
|---|---|
| `weather-analysis collect` | one collection run (Open-Meteo current + HA sensor states) |
| `weather-analysis collect --loop` | collect forever on `collection.interval_minutes` |
| `weather-analysis backfill --days N` | fetch past data (Open-Meteo hourly + HA recorder history) |
| `weather-analysis demo` | seed synthetic data to try the dashboard without any setup |
| `weather-analysis dashboard` | start the Streamlit dashboard |

All commands accept `-c/--config path/to/config.yaml` (default: `./config.yaml`).

## Storage

Everything lands in one long-format SQLite table (`data/weather.db` by default):

```
ts · source · sensor · name · area · metric · value · unit
```

Inserts are idempotent (`UNIQUE(ts, source, sensor, metric)`), so overlapping backfills and
collection runs are safe. The file is plain SQLite — query it with anything you like.

## Development

```bash
pip install -e .[dev]
pytest
```
