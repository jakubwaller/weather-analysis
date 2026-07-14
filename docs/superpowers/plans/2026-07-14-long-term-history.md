# Long-Term History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfill ~174 days of Home Assistant hourly statistics and matching Open-Meteo archive weather, filtering sensor glitches without touching real temperature swings.

**Architecture:** A new `validate.py` gates every write behind per-area plausibility ranges. A new `home_assistant_stats.py` collector reaches HA's `statistics` table over the WebSocket API (the only interface core HA exposes for it). `open_meteo.fetch_history` swaps the forecast endpoint for the archive endpoint, dropping its 92-day cap. `collect.py` funnels all three sources through one `_store` helper so validation cannot be bypassed. The dashboard stops dropping NaN (which would draw a two-month outage as a straight line) and gains an inside daily-range band.

**Tech Stack:** Python 3.10+, `requests`, `websocket-client` (new), `pandas`, `plotly`, `streamlit`, `pytest`, SQLite.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-14-long-term-history-design.md`. Read it before starting.
- Branch: `long-term-history`. Never commit to `main`. All paths below are repo-relative.
- Python `>=3.10`. Use `from __future__ import annotations` in every new module, matching existing ones.
- New dependency: `websocket-client>=1.7`. Add no others.
- **No AI or assistant attribution anywhere** — not in code, comments, commit messages, or docs. No `Co-Authored-By` trailers.
- No schema migration. The long-format `measurements` table absorbs new sources and metrics by design.
- Existing tests must keep passing: `python -m pytest tests -q` → currently `10 passed`.
- Match existing style: module docstring, sync `requests`, `session=None` / `client=None` injection seam, no type-annotation churn beyond what neighbouring code does.
- Comments state constraints the code cannot show. Never narrate the diff.

**Measured constants — copy verbatim, do not re-derive:**

| Fact | Value |
|---|---|
| Inside plausible range | `5.0` – `40.0` °C |
| Outside plausible range | `-40.0` – `60.0` °C |
| Humidity range | `0.0` – `100.0` % |
| Coldest real inside reading | `10.1` °C (must survive filtering) |
| Glitch value | exactly `0.0`, 5 occurrences |
| Archive URL | `https://archive-api.open-meteo.com/v1/archive` |
| WS command | `recorder/statistics_during_period`, `period: "hour"`, `types: ["mean", "min", "max"]` |
| WS timestamps | epoch **milliseconds** |

---

### Task 1: Plausibility validation

**Files:**
- Create: `weather_analysis/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `Measurement` from `weather_analysis.db`, `Config`/`Sensor` from `weather_analysis.config`.
- Produces:
  - `plausible(m: Measurement, config: Config) -> bool`
  - `filter_implausible(rows: list[Measurement], config: Config) -> tuple[list[Measurement], list[Measurement]]` returning `(kept, dropped)`
  - `DEFAULT_RANGES: dict[tuple[str, str], tuple[float, float]]`
  - `base_metric(metric: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_validate.py`:

```python
from datetime import datetime, timezone

from weather_analysis.config import Config, Sensor
from weather_analysis.db import Measurement
from weather_analysis.validate import base_metric, filter_implausible, plausible


def config(**overrides) -> Config:
    base = dict(
        latitude=53.6, longitude=9.9,
        ha_sensors=[Sensor("sensor.living_room_temperature", "Living room", "inside")],
    )
    base.update(overrides)
    return Config(**base)


def m(value, area="inside", metric="temperature", sensor="sensor.living_room_temperature"):
    return Measurement(
        ts=datetime(2026, 1, 21, 9, tzinfo=timezone.utc),
        source="home_assistant", sensor=sensor, name="Living room",
        area=area, metric=metric, value=value, unit="°C",
    )


def test_base_metric_strips_min_max_suffix():
    assert base_metric("temperature") == "temperature"
    assert base_metric("temperature_min") == "temperature"
    assert base_metric("temperature_max") == "temperature"
    assert base_metric("humidity_min") == "humidity"


def test_inside_glitch_dropped_but_real_cold_morning_kept():
    assert not plausible(m(0.0), config())
    # the real 21 Jan open-window morning
    assert plausible(m(10.1), config())


def test_outside_subzero_is_kept():
    # a global 5C floor would erase the heating season
    assert plausible(m(2.8, area="outside"), config())
    assert plausible(m(-15.0, area="outside"), config())
    assert not plausible(m(-99.0, area="outside"), config())


def test_min_suffix_resolves_to_temperature_range():
    # the glitch arrives almost entirely via min
    assert not plausible(m(0.0, metric="temperature_min"), config())
    assert plausible(m(10.1, metric="temperature_min"), config())


def test_metric_without_rule_passes_through():
    assert plausible(m(1013.0, area="outside", metric="pressure"), config())
    assert plausible(m(0.0, area="outside", metric="cloud_cover"), config())


def test_per_sensor_valid_range_overrides_area_default():
    cellar = Sensor("sensor.cellar_temperature", "Cellar", "inside")
    cellar.valid_range = (0.0, 25.0)
    cfg = config(ha_sensors=[cellar])
    assert plausible(m(3.0, sensor="sensor.cellar_temperature"), cfg)
    assert not plausible(m(30.0, sensor="sensor.cellar_temperature"), cfg)


def test_filter_implausible_partitions_and_keeps_order():
    rows = [m(22.0), m(0.0), m(21.5)]
    kept, dropped = filter_implausible(rows, config())
    assert [r.value for r in kept] == [22.0, 21.5]
    assert [r.value for r in dropped] == [0.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_validate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather_analysis.validate'`

- [ ] **Step 3: Write minimal implementation**

Create `weather_analysis/validate.py`:

```python
"""Plausibility checks that separate sensor glitches from real readings.

The Bosch sensors occasionally report 0.0 for a few seconds. Measured across
eight months, inside temperatures never fall below 10.1 C, so a floor of 5 C
drops every glitch while leaving the coldest real reading 5 C of headroom.

Ranges are per area because outside is legitimately sub-zero all winter: one
global floor would erase the heating season.
"""

from __future__ import annotations

from .config import Config
from .db import Measurement

# (area, metric) -> (low, high), inclusive. A metric with no rule is not filtered.
DEFAULT_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("inside", "temperature"): (5.0, 40.0),
    ("outside", "temperature"): (-40.0, 60.0),
    ("inside", "humidity"): (0.0, 100.0),
    ("outside", "humidity"): (0.0, 100.0),
}

_SUFFIXES = ("_min", "_max")


def base_metric(metric: str) -> str:
    """'temperature_min' -> 'temperature'. The glitch arrives via min, so a
    lookup that misses the suffixed metrics filters nothing that matters."""
    for suffix in _SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


def _range_for(m: Measurement, config: Config) -> tuple[float, float] | None:
    metric = base_metric(m.metric)
    for sensor in config.ha_sensors:
        if sensor.entity_id == m.sensor and getattr(sensor, "valid_range", None):
            return sensor.valid_range
    override = (config.validation_ranges or {}).get(metric, {}).get(m.area)
    if override:
        return override
    return DEFAULT_RANGES.get((m.area, metric))


def plausible(m: Measurement, config: Config) -> bool:
    bounds = _range_for(m, config)
    if bounds is None:
        return True
    low, high = bounds
    return low <= m.value <= high


def filter_implausible(
    rows: list[Measurement], config: Config
) -> tuple[list[Measurement], list[Measurement]]:
    """Partition into (kept, dropped), preserving order."""
    kept, dropped = [], []
    for m in rows:
        (kept if plausible(m, config) else dropped).append(m)
    return kept, dropped
```

- [ ] **Step 4: Add config plumbing**

In `weather_analysis/config.py`, add `valid_range` to `Sensor`:

```python
@dataclass
class Sensor:
    entity_id: str
    name: str
    area: str = "inside"  # 'inside' or 'outside'
    metric: str = "temperature"
    valid_range: tuple[float, float] | None = None
```

Add to `Config`:

```python
    validation_ranges: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
```

In `load_config`, parse the per-sensor override inside the existing sensor loop:

```python
        sensors.append(
            Sensor(
                entity_id=s["entity_id"],
                name=s.get("name") or s["entity_id"],
                area=s.get("area", "inside"),
                metric=s.get("metric", "temperature"),
                valid_range=tuple(s["valid_range"]) if s.get("valid_range") else None,
            )
        )
```

And parse the global block, before the `return Config(...)`:

```python
    validation_ranges = {
        metric: {area: tuple(bounds) for area, bounds in by_area.items()}
        for metric, by_area in (raw.get("validation") or {}).items()
    }
```

Pass `validation_ranges=validation_ranges` into the returned `Config`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_validate.py tests/test_config.py -q`
Expected: PASS — 7 new tests plus the existing config tests.

- [ ] **Step 6: Commit**

```bash
git add weather_analysis/validate.py weather_analysis/config.py tests/test_validate.py
git commit -m "Add per-area plausibility validation

Inside data has an empty band from 0.0 to 10.1 C, so a 5 C floor drops
every Bosch glitch while keeping the coldest real reading. Ranges are per
area: outside is legitimately sub-zero, and one global floor would erase
the heating season."
```

---

### Task 2: Open-Meteo archive endpoint

**Files:**
- Modify: `weather_analysis/collectors/open_meteo.py:17` (add `ARCHIVE_URL`), `:69-100` (`fetch_history`)
- Test: `tests/test_collectors.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `open_meteo.ARCHIVE_URL`; `fetch_history(config, days, session=None)` keeps its signature, loses the 92-day clamp.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_collectors.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_collectors.py::test_open_meteo_history_uses_archive_without_day_cap -q`
Expected: FAIL — `AttributeError: module 'weather_analysis.collectors.open_meteo' has no attribute 'ARCHIVE_URL'`

- [ ] **Step 3: Write minimal implementation**

In `weather_analysis/collectors/open_meteo.py`, add below `FORECAST_URL`:

```python
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# The archive serves the same variable names with no usable lag, so history has
# no 92-day ceiling. fetch_current stays on the forecast endpoint, which is the
# only one with a `current` block.
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
```

Replace the request block in `fetch_history` (keep the parsing below it untouched):

```python
def fetch_history(config: Config, days: int,
                  session: requests.Session | None = None) -> list[Measurement]:
    """Hourly history for the past `days` days, from the Open-Meteo archive."""
    metrics = _selected_metrics(config)
    http = session or requests
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    resp = http.get(
        ARCHIVE_URL,
        params={
            "latitude": config.latitude,
            "longitude": config.longitude,
            "hourly": ",".join(var for var, _ in metrics.values()),
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "timezone": "UTC",
        },
        timeout=60,
    )
```

Update the import at the top of the file:

```python
from datetime import datetime, timedelta, timezone
```

The existing `ts > now` guard stays: the archive also returns the current day's future hours.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_collectors.py -q`
Expected: PASS — the new test plus `test_open_meteo_history_skips_future_and_none`, which must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add weather_analysis/collectors/open_meteo.py tests/test_collectors.py
git commit -m "Fetch outside history from the Open-Meteo archive

The archive returns identical variable names with no measured lag, so
history loses its 92-day cap and needs no splicing between endpoints.
fetch_current stays on the forecast endpoint for its current block."
```

---

### Task 3: Home Assistant statistics collector

**Files:**
- Create: `weather_analysis/collectors/home_assistant_stats.py`
- Modify: `pyproject.toml:9-15` (dependencies)
- Test: `tests/test_collectors.py`

**Interfaces:**
- Consumes: `Config`/`Sensor`, `Measurement`, `DEFAULT_UNITS` pattern from `home_assistant.py`.
- Produces:
  - `home_assistant_stats.SOURCE = "home_assistant_stats"`
  - `fetch_statistics(config: Config, days: int, client=None) -> list[Measurement]`
  - A client seam: any object with `statistics_during_period(start, end, statistic_ids, period, types) -> dict` mapping `statistic_id -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_collectors.py`:

```python
from weather_analysis.collectors import home_assistant_stats


class FakeStatsClient:
    def __init__(self, result):
        self.result = result
        self.last_kwargs = None

    def statistics_during_period(self, **kwargs):
        self.last_kwargs = kwargs
        return self.result

    def close(self):
        pass


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
    # HA sends epoch milliseconds
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_collectors.py -q`
Expected: FAIL — `ImportError: cannot import name 'home_assistant_stats'`

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, add to `dependencies`:

```toml
dependencies = [
    "requests>=2.31",
    "PyYAML>=6.0",
    "pandas>=2.0",
    "plotly>=5.18",
    "streamlit>=1.30",
    "websocket-client>=1.7",
]
```

Install: `pip install -e .`

- [ ] **Step 4: Write minimal implementation**

Create `weather_analysis/collectors/home_assistant_stats.py`:

```python
"""Long-term hourly statistics from Home Assistant.

Home Assistant purges `states` after `purge_keep_days` (10 by default) but keeps
hourly mean/min/max in `statistics` indefinitely. The REST history API reads only
`states`, so months of data are reachable only over the WebSocket API — there is
no REST equivalent for statistics.

Stored under its own source: an hourly mean is not an instantaneous reading, and
keeping them distinct preserves provenance under UNIQUE(ts, source, sensor, metric).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import Measurement

SOURCE = "home_assistant_stats"

DEFAULT_UNITS = {"temperature": "°C", "humidity": "%", "pressure": "hPa"}

# statistics field -> metric suffix
FIELDS = {"mean": "", "min": "_min", "max": "_max"}


class StatisticsClient:
    """Thin sync wrapper over HA's WebSocket API."""

    def __init__(self, url: str, token: str, timeout: int = 60):
        from websocket import create_connection

        ws_url = url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws = create_connection(f"{ws_url}/api/websocket", timeout=timeout)
        self._id = 0
        hello = json.loads(self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting from Home Assistant: {hello.get('type')}")
        self._ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(self._ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(
                "Home Assistant rejected the token "
                "(home_assistant.token / HA_TOKEN): " + str(auth.get("message", ""))
            )

    def statistics_during_period(self, **kwargs) -> dict:
        self._id += 1
        self._ws.send(json.dumps({
            "id": self._id, "type": "recorder/statistics_during_period", **kwargs,
        }))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") != self._id:
                continue  # ignore unrelated events
            if not resp.get("success"):
                raise RuntimeError(
                    "statistics request failed: "
                    + str((resp.get("error") or {}).get("message", resp))
                )
            return resp["result"]

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def fetch_statistics(config: Config, days: int, client=None) -> list[Measurement]:
    if not config.ha_sensors:
        return []
    own_client = client is None
    if own_client:
        client = StatisticsClient(config.ha_url, config.ha_token)
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        result = client.statistics_during_period(
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=[s.entity_id for s in config.ha_sensors],
            period="hour",
            types=["mean", "min", "max"],
        )
    finally:
        if own_client:
            client.close()

    measurements = []
    for sensor in config.ha_sensors:
        rows = result.get(sensor.entity_id)
        if not rows:
            print(f"  warning: no statistics for {sensor.entity_id}, skipping")
            continue
        unit = DEFAULT_UNITS.get(sensor.metric)
        for row in rows:
            ts = datetime.fromtimestamp(row["start"] / 1000, timezone.utc)
            for field, suffix in FIELDS.items():
                value = row.get(field)
                if value is None:
                    continue
                measurements.append(Measurement(
                    ts=ts, source=SOURCE, sensor=sensor.entity_id, name=sensor.name,
                    area=sensor.area, metric=f"{sensor.metric}{suffix}",
                    value=float(value), unit=unit,
                ))
    return measurements
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_collectors.py -q`
Expected: PASS — 5 new tests, existing collector tests unaffected.

- [ ] **Step 6: Commit**

```bash
git add weather_analysis/collectors/home_assistant_stats.py pyproject.toml tests/test_collectors.py
git commit -m "Add Home Assistant statistics collector

States are purged after 10 days while hourly statistics persist
indefinitely, and only the WebSocket API exposes them. Stored under its own
source so hourly means never masquerade as instantaneous readings."
```

---

### Task 4: Route every source through validation

**Files:**
- Modify: `weather_analysis/collect.py` (whole file)
- Test: `tests/test_collect.py` (create)

**Interfaces:**
- Consumes: `filter_implausible` (Task 1), `open_meteo.fetch_history` (Task 2), `home_assistant_stats.fetch_statistics` (Task 3).
- Produces: `_store(conn, rows, config, label) -> int`; `backfill` and `collect_once` keep their signatures.

- [ ] **Step 1: Write the failing test**

Create `tests/test_collect.py`:

```python
from datetime import datetime, timezone

from weather_analysis.collect import _store
from weather_analysis.config import Config, Sensor
from weather_analysis.db import Measurement, connect


def config(**overrides) -> Config:
    base = dict(
        latitude=53.6, longitude=9.9,
        ha_sensors=[Sensor("sensor.living_room_temperature", "Living room", "inside")],
    )
    base.update(overrides)
    return Config(**base)


def m(value, ts_hour):
    return Measurement(
        ts=datetime(2026, 7, 14, ts_hour, tzinfo=timezone.utc),
        source="home_assistant", sensor="sensor.living_room_temperature",
        name="Living room", area="inside", metric="temperature",
        value=value, unit="°C",
    )


def test_store_drops_implausible_and_reports(tmp_path, capsys):
    conn = connect(tmp_path / "t.db")
    inserted = _store(conn, [m(22.0, 1), m(0.0, 2), m(21.5, 3)], config(), "home-assistant")

    assert inserted == 2
    out = capsys.readouterr().out
    assert "1 implausible dropped" in out

    stored = conn.execute("select value from measurements order by ts").fetchall()
    assert [r[0] for r in stored] == [22.0, 21.5]


def test_store_says_nothing_about_drops_when_there_are_none(tmp_path, capsys):
    conn = connect(tmp_path / "t.db")
    _store(conn, [m(22.0, 1)], config(), "home-assistant")
    assert "implausible" not in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_collect.py -q`
Expected: FAIL — `ImportError: cannot import name '_store'`

- [ ] **Step 3: Write minimal implementation**

Rewrite `weather_analysis/collect.py`:

```python
"""Orchestrates collection runs: fetch from all enabled sources, store in SQLite."""

from __future__ import annotations

import time
import traceback
import sqlite3

from .config import Config
from .collectors import home_assistant, home_assistant_stats, open_meteo
from .db import Measurement, connect, insert_measurements
from .validate import filter_implausible


def _store(conn: sqlite3.Connection, rows: list[Measurement],
           config: Config, label: str) -> int:
    """Validate, insert, report. Every source goes through here, so a new
    collector cannot bypass validation by forgetting to call it."""
    kept, dropped = filter_implausible(rows, config)
    inserted = insert_measurements(conn, kept)
    note = f", {len(dropped)} implausible dropped" if dropped else ""
    print(f"{label}: {len(rows)} readings, {inserted} new{note}")
    return inserted


def collect_once(config: Config) -> int:
    """Run one collection cycle. Returns the number of new rows stored."""
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            try:
                inserted += _store(conn, open_meteo.fetch_current(config), config, "open-meteo")
            except Exception as exc:
                print(f"open-meteo: FAILED ({exc})")
        if config.ha_enabled and config.ha_sensors:
            try:
                inserted += _store(conn, home_assistant.fetch_current(config), config,
                                   "home-assistant")
            except Exception as exc:
                print(f"home-assistant: FAILED ({exc})")
    finally:
        conn.close()
    print(f"stored {inserted} new rows in {config.db_path}")
    return inserted


def collect_loop(config: Config) -> None:
    """Collect forever, every `collection.interval_minutes`. Ctrl-C to stop."""
    interval = max(60, config.interval_minutes * 60)
    print(f"collecting every {interval // 60} min, Ctrl-C to stop")
    while True:
        try:
            collect_once(config)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


def backfill(config: Config, days: int) -> int:
    """Fetch past data: Open-Meteo archive (hourly), the Home Assistant recorder
    (10-minute detail, limited by states retention) and Home Assistant long-term
    statistics (hourly, kept indefinitely)."""
    conn = connect(config.db_path)
    inserted = 0
    try:
        if config.open_meteo_enabled:
            try:
                inserted += _store(conn, open_meteo.fetch_history(config, days), config,
                                   "open-meteo history")
            except Exception as exc:
                print(f"open-meteo history: FAILED ({exc})")
        if config.ha_enabled and config.ha_sensors:
            try:
                inserted += _store(conn, home_assistant.fetch_history(config, days), config,
                                   "home-assistant history")
            except Exception as exc:
                print(f"home-assistant history: FAILED ({exc})")
            try:
                inserted += _store(conn, home_assistant_stats.fetch_statistics(config, days),
                                   config, "home-assistant statistics")
            except Exception as exc:
                print(f"home-assistant statistics: FAILED ({exc})")
    finally:
        conn.close()
    print(f"stored {inserted} new rows in {config.db_path}")
    return inserted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests -q`
Expected: PASS — all tests, including the original 10.

- [ ] **Step 5: Commit**

```bash
git add weather_analysis/collect.py tests/test_collect.py
git commit -m "Route every source through validation and add statistics to backfill

One _store helper validates, inserts and reports for all sources, so a new
collector cannot bypass filtering. Backfill now also pulls long-term
statistics, which overlap the states history at a coarser resolution."
```

---

### Task 5: Extract analysis helpers and fix the gap

**Files:**
- Create: `weather_analysis/analysis.py`
- Modify: `dashboard/app.py:79-108` (remove helpers, import them instead)
- Test: `tests/test_analysis.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all importable from `weather_analysis.analysis`:
  - `sensor_labels(df: pd.DataFrame) -> pd.Series`
  - `resample_rule(span: timedelta) -> str | None`
  - `prepare_series(df: pd.DataFrame, metric: str, rule: str | None) -> pd.DataFrame`
  - `daily_frame(df: pd.DataFrame, metric: str) -> pd.DataFrame` with columns `min`/`mean`/`max`, indexed by day

**Why extract:** these are pure pandas functions with no Streamlit dependency, but living inside a
Streamlit script makes them reachable only by executing the page. Moving them makes the gap fix
testable by plain import instead of an `exec` hack, and leaves `app.py` as page composition. The
chart functions stay in `app.py` — they build figures and are exercised in the browser.

- [ ] **Step 1: Write the failing test**

Create `tests/test_analysis.py`:

```python
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
    assert out["label"].dropna().unique().tolist() == ["Living room · inside"] or \
           out["label"].unique().tolist() == ["Living room · inside"]


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_analysis.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather_analysis.analysis'`

- [ ] **Step 3: Create the module**

Create `weather_analysis/analysis.py`. `sensor_labels`, `resample_rule` and `prepare_series` move
verbatim from `dashboard/app.py:79-108` except for the `.dropna()` removal noted below:

```python
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
    derived from the mean series, which is all the live 10-minute period has.
    """
    mean_src = df[df["metric"] == metric].set_index("ts")["value"]
    daily = mean_src.resample("1D").agg(["min", "mean", "max"])
    for field in ("min", "max"):
        true_src = df[df["metric"] == f"{metric}_{field}"].set_index("ts")["value"]
        if not true_src.empty:
            daily[field] = true_src.resample("1D").agg(field).combine_first(daily[field])
    return daily.dropna(subset=["mean"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_analysis.py -q`
Expected: PASS — 6 tests.

- [ ] **Step 5: Import the helpers in `dashboard/app.py`**

Delete `sensor_labels`, `resample_rule` and `prepare_series` from `dashboard/app.py` (lines 79-108,
between `load_data` and the `# --- charts ---` marker) and import them instead. Add below the
existing imports:

```python
from weather_analysis.analysis import (
    daily_frame,
    prepare_series,
    resample_rule,
    sensor_labels,
)
```

Nothing else changes: every call site keeps the same names.

- [ ] **Step 6: Commit**

```bash
git add weather_analysis/analysis.py dashboard/app.py tests/test_analysis.py
git commit -m "Extract dashboard data preparation and break lines across gaps

Dropping NaN buckets removed exactly the markers plotly uses to break a
line, so a two-month outage drew as a straight line through missing data.
The helpers move out of the Streamlit script so this is testable by import
rather than by executing a page."
```

---

### Task 5b: Inside daily range band

**Files:**
- Modify: `dashboard/app.py:143-161` (`daily_range_chart`), Patterns tab (~`:374-382`)

**Interfaces:**
- Consumes: `daily_frame` from Task 5.
- Produces: `daily_range_chart(daily: pd.DataFrame, title: str, color: str = "#2a78d6") -> go.Figure` — now takes a precomputed frame rather than deriving min/max itself.

- [ ] **Step 1: Generalize `daily_range_chart`**

Replace it in `dashboard/app.py`. Only the first line and the `fillcolor`/`line` colour change; the
three traces are otherwise as they were:

```python
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


def _fill(color: str, alpha: float = 0.18) -> str:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
```

- [ ] **Step 2: Wire up the Patterns tab**

Replace the `daily_range_chart(outside_all, ...)` call and add the room picker before the heatmap:

```python
with tabs[2]:
    st.plotly_chart(
        daily_range_chart(daily_frame(outside_all, "temperature"),
                          "Outside daily min / mean / max"),
        use_container_width=True,
    )

    inside_all = window[window["area"] == "inside"]
    rooms = sorted(inside_all["name"].unique())
    if rooms:
        room = st.selectbox("Room", rooms)
        room_df = inside_all[inside_all["name"] == room]
        st.plotly_chart(
            daily_range_chart(daily_frame(room_df, "temperature"),
                              f"{room} daily min / mean / max",
                              color=COLOR_BY_LABEL.get(f"{room} · inside", "#2a78d6")),
            use_container_width=True,
        )
        st.caption(
            "Backfilled days use the true hourly min/max Home Assistant recorded; "
            "recent days derive them from 10-minute readings."
        )

    st.plotly_chart(
        heatmap_chart(outside_all, "Outside temperature by hour and day"),
        use_container_width=True,
    )
```

- [ ] **Step 3: Verify the whole suite still passes**

Run: `python -m pytest tests -q`
Expected: PASS — nothing regressed.

- [ ] **Step 4: Check the page actually renders**

Run: `weather-analysis demo && weather-analysis dashboard`, open the Patterns tab, confirm the room
picker appears and switching rooms redraws the band. Then `git checkout -- data/` or delete the
demo database so synthetic rows never reach the Pi.

- [ ] **Step 5: Commit**

```bash
git add dashboard/app.py
git commit -m "Add an inside daily min/mean/max band per room

Mirrors the outside chart so the band means the same thing in both: one
sensor's diurnal swing. Prefers the true hourly min/max where backfilled
statistics provide them."
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `config.example.yaml`

- [ ] **Step 1: Update the CLI table in `README.md`**

Replace the `backfill` row:

```markdown
| `weather-analysis backfill --days N` | fetch past data (Open-Meteo archive + HA recorder + HA long-term statistics) |
```

Replace the backfill paragraph under "Real setup":

```markdown
2. **Backfill history** so the graphs are interesting from day one:

   ```bash
   weather-analysis backfill --days 240
   ```

   Three sources, at different resolutions. Open-Meteo's archive has no practical
   limit. Home Assistant's recorder keeps full-detail history only as long as its
   `purge_keep_days` (10 by default), but its hourly long-term statistics are kept
   indefinitely — so a long backfill gets 10-minute detail recently and hourly
   mean/min/max as far back as your Home Assistant has been running.

   Readings outside a plausible range for their area are dropped as sensor
   glitches and reported. See `validation` in `config.example.yaml`.
```

- [ ] **Step 2: Document validation in `config.example.yaml`**

Append:

```yaml
validation:
  # Readings outside these ranges are treated as sensor glitches and dropped.
  # Ranges are per area on purpose: outside is legitimately sub-zero in winter,
  # so a single global floor would delete real cold weather.
  temperature:
    inside: [5, 40]
    outside: [-40, 60]
  # A sensor can override its area default:
  #   sensors:
  #     - entity_id: sensor.cellar_temperature
  #       valid_range: [0, 25]
```

- [ ] **Step 3: Commit**

```bash
git add README.md config.example.yaml
git commit -m "Document the statistics backfill and validation ranges"
```

---

### Task 7: Deploy and verify on the Pi

**Files:** none (deployment)

**Interfaces:** Consumes everything above.

- [ ] **Step 1: Merge and pull**

Open a PR from `long-term-history`, merge to `main`, then on the Pi:

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && git pull && docker compose build'
```
Expected: image builds; `websocket-client` installs.

- [ ] **Step 2: Run the tests in the built image**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker run --rm \
  -v /home/ubuntu/weather-analysis/tests:/app/tests:ro weather-analysis \
  bash -c "pip install -q pytest && python -m pytest /app/tests -q"'
```
Expected: all tests pass.

- [ ] **Step 3: Count glitch rows before cleaning**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker compose exec -T dashboard python3 -c "
import sqlite3
c = sqlite3.connect(\"/app/data/weather.db\")
q = \"select count(*) from measurements where area=? and metric like ? and value < 5.0\"
print(\"glitch rows:\", c.execute(q, (\"inside\", \"temperature%\")).fetchone()[0])
"'
```
Expected: `glitch rows: 3` (2026-07-14 16:20–16:23). If it is not 3, stop and investigate before deleting anything.

- [ ] **Step 4: Delete the glitch rows**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker compose exec -T dashboard python3 -c "
import sqlite3
c = sqlite3.connect(\"/app/data/weather.db\")
n = c.execute(\"delete from measurements where area=? and metric like ? and value < 5.0\",
              (\"inside\", \"temperature%\")).rowcount
c.commit()
print(\"deleted:\", n)
print(\"lowest surviving:\", c.execute(
    \"select min(value) from measurements where area=? and metric like ?\",
    (\"inside\", \"temperature%\")).fetchone()[0])
"'
```
Expected: `deleted: 3`, and the lowest surviving value is ~20.2 — far above the 5.0 floor.

- [ ] **Step 5: Backfill the full history**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker compose run --rm collector \
  weather-analysis backfill --days 240'
```
Expected: three source lines. Statistics should report roughly 75k readings (174 days × 24 h × 6 sensors × 3 metrics) and report implausible drops for the 5 known glitch hours (2026-06-18 15:00 and 2026-07-14 16:00) arriving via `temperature_min`.

- [ ] **Step 6: Verify the stored shape**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker compose exec -T dashboard python3 -c "
import sqlite3
c = sqlite3.connect(\"file:/app/data/weather.db?mode=ro\", uri=True)
for r in c.execute(\"\"\"select source, metric, count(*), min(ts), max(ts)
                        from measurements group by source, metric order by 1, 2\"\"\"):
    print(f\"  {r[0]:22} {r[1]:18} n={r[2]:6}  {r[3][:10]} -> {r[4][:10]}\")
print(\"any inside value below 5:\", c.execute(
    \"select count(*) from measurements where area=? and metric like ? and value < 5.0\",
    (\"inside\", \"temperature%\")).fetchone()[0])
"'
```
Expected: `home_assistant_stats` rows for `temperature`, `temperature_min`, `temperature_max` starting 2025-11-19; `open_meteo` back to ~2025-11-17; zero inside values below 5.

- [ ] **Step 7: Restart and check the charts**

```bash
ssh rpi 'cd /home/ubuntu/weather-analysis && docker compose up -d --build'
```

Open `http://192.168.0.67:8502`, select **All data**, and confirm:
- Trends shows a visible **break** across 2026-04-06 → 2026-06-08, not a straight line.
- No spikes to 0.
- Inside − outside reaches roughly +15 to +20 °C in January (it is +1.9 °C in July).
- Patterns has a room picker and its band tracks the chosen room.

- [ ] **Step 8: Commit nothing; report results**

Deployment produces no commits. Report the row counts and the January delta.

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `validate.py`, per-area ranges, suffix resolution, per-measurement drops | 1 |
| Config: `valid_range`, `validation` block | 1 |
| Archive endpoint, no 92-day cap, `fetch_current` untouched | 2 |
| `home_assistant_stats.py`, websocket, ms timestamps, unit default, None skip, missing entity | 3 |
| `websocket-client` dependency | 3 |
| `_store` choke point, backfill calls three sources, drops reported | 4 |
| `prepare_series` NaN gap fix | 5 |
| `daily_frame` with true-min/max preference and live-period fallback | 5 |
| Generalized `daily_range_chart`, room picker | 5b |
| `METRIC_LABELS` untouched so `_min`/`_max` stay out of the UI | 5b (by omission — no change made) |
| README + config docs | 6 |
| One-off DELETE, verified before and after | 7 |
| Backfill 240 days, gap visible, January delta | 7 |

No gaps.

**Placeholder scan:** none — every code step contains complete code, every command its expected output.

**Type consistency:** `filter_implausible` returns `(kept, dropped)` in Task 1 and is unpacked that way in Task 4. `daily_frame(df, metric)` is defined in Task 5 and consumed in Task 5b. `daily_range_chart(daily, title, color)` is defined in Task 5b Step 1 and called with that signature in Step 2. `statistics_during_period(**kwargs)` accepts the keywords Task 3's `fetch_statistics` passes, and `FakeStatsClient` mirrors it. `Sensor.valid_range` is a `tuple[float, float] | None` in Task 1 and read via `getattr` in `_range_for`.

**Revision:** Task 5 originally tested `prepare_series` by `exec`-ing a slice of `dashboard/app.py`.
That was fragile — it split on a comment marker and stripped lines by prefix, and `daily_frame`
would have landed on the wrong side of the split. Extracting the pure helpers into
`weather_analysis/analysis.py` makes them testable by import and is why Task 5/5b are separate: one
extracts and fixes the gap, the other adds the band.

**Known risk carried into execution:** Task 3's `StatisticsClient` is the only code here with no
automated coverage of its real transport — the tests inject a fake. It was verified by hand against
HA 2025.11.2 (auth handshake and an hourly response), so Task 7 Step 5 is where a real regression
would surface. Read that step's output rather than assuming success.
