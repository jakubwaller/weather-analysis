# Long-term history: Home Assistant statistics + Open-Meteo archive

Date: 2026-07-14

## Problem

`backfill` reaches back ~10 days for inside data and 92 days for outside. Both limits are
self-imposed, and both hide data that already exists.

Home Assistant keeps two separate stores. `states` holds every reading and is purged after
`purge_keep_days` (default 10, and the deployment has no `recorder:` block, so the default
applies). `statistics` holds hourly mean/min/max per sensor and is **never purged by default**.
`fetch_history` calls `/api/history/period/...`, which reads `states` only — so eight months of
`statistics` were never visible to it.

Measured on the deployment's recorder database, per temperature sensor:

| Store | Coverage | Rows |
|---|---|---|
| `states` | 2026-07-04 → now (~10 days) | 218–6,388 |
| `statistics` | 2025-11-19 → now (~8 months) | ~4,178 |

The statistics are not continuous. Two contiguous runs, split by an outage:

| Run | Span | Duration |
|---|---|---|
| 1 | 2025-11-19 → 2026-04-06 | 137.7 days |
| 2 | 2026-06-08 → 2026-07-14 | 36.3 days |

~174 days of real hourly data, covering a full heating season. That period is what makes the
analysis worth doing: inside−outside is currently +1.9 °C, but on 2025-11-19 outside was 2.8 °C
while the living room ran ~24 °C in January — a ~21 °C delta, and a lag signal with something in
it.

Outside data is capped at 92 days by `past_days`, so without a matching change the inside history
would have nothing to compare against.

Separately, the Bosch sensors emit occasional `0.0` readings — 5 hours across the 8 months
(2026-06-18 15:00 and 2026-07-14 16:00), each lasting seconds. They are stored faithfully today,
because `_parse_state` filters only `unavailable`/`unknown`, and they draw as spikes to zero. A
room dropping sharply is real (an opened window); dropping to 0.0 and back within seconds is not.

## Goals

- Backfill inside data from HA statistics across its full retention.
- Backfill outside data far enough to match, removing the 92-day cap.
- Preserve the hourly min/max HA computed; once `states` is purged, a room's true daily swing is
  otherwise unrecoverable.
- Surface inside daily min/mean/max in the dashboard, mirroring the outside chart.
- Do not let missing data render as a continuous line.
- Drop sensor glitches (the Bosch `0.0` readings) without touching real temperature swings.

## Non-goals

- Changing the recorder's retention. Statistics already persist; `states` retention is HA's call.
- Backfilling anything before 2025-11-19. No statistics exist earlier.
- Reconciling the Apr 6 → Jun 8 outage. There is no data; the charts should show a gap, not
  invent one.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Statistics transport | WebSocket `recorder/statistics_during_period` | The only API core HA exposes for statistics; no REST equivalent exists. Verified against HA 2025.11.2. |
| WebSocket library | `websocket-client` (sync) | Every existing collector is sync `requests`. `websockets`/`aiohttp` would drag asyncio into a codebase with none. |
| Source value | `home_assistant_stats` | Hourly means and instantaneous readings are different measurements. A distinct `source` keeps provenance and prevents collisions under `UNIQUE(ts, source, sensor, metric)`. |
| Stored fields | mean + min + max | HA already computed them; hourly means cannot reconstruct a daily swing. |
| Outside history | Archive endpoint replaces `past_days` | Measured: the archive returns identical variable names and no observable lag (336/336 non-null hours through the current day). Splicing two endpoints would add branching for no measured benefit. |
| Gap rendering | Preserve NaN in `prepare_series` | Plotly breaks lines at NaN. `.dropna()` removes exactly the markers that would show the outage. |
| Outlier filtering | Per-area plausibility range | Measured: inside data has an empty band from 0.0 to 10.1 °C. A range separates glitch from signal with 5 °C of headroom either side, and — unlike a despike — needs no neighbouring reading, so it works during live collection. |
| Range scope | Per **area**, never global | Outside legitimately reads 2.8 °C in November and below zero all winter. A global 5 °C floor would delete the entire heating season, i.e. the reason for this work. |

Rejected: reading `/config/home-assistant_v2.db` directly. It needs no dependency, but couples to
HA's internal schema, requires co-location, and contradicts the app's remote-HA design
(`home_assistant.url` exists precisely so HA can live elsewhere).

## Design

### New collector: `weather_analysis/collectors/home_assistant_stats.py`

```
fetch_statistics(config, days, client=None) -> list[Measurement]
```

`source = "home_assistant_stats"`; `sensor`, `name`, `area` come from the configured `Sensor`.
Each hourly row yields up to three measurements, named from the sensor's own metric so a humidity
sensor behaves identically:

| Statistics field | Stored metric | Notes |
|---|---|---|
| `mean` | `<metric>` e.g. `temperature` | merges with live readings into one dashboard series |
| `min` | `<metric>_min` | not surfaced directly |
| `max` | `<metric>_max` | not surfaced directly |

Response handling:

- `start` is epoch **milliseconds** → `datetime.fromtimestamp(start / 1000, timezone.utc)`.
- Rows carry no unit → fall back to `DEFAULT_UNITS`, as `home_assistant.py` already does.
- A `None` field is skipped rather than stored as 0.
- Entities absent from the result are skipped with a warning.

The `client=None` seam mirrors `session=None` in the existing collectors. The real client wraps
`websocket-client` and handles the connect → `auth_required` → `auth` → `auth_ok` handshake before
issuing `recorder/statistics_during_period` with `period: "hour"` and
`types: ["mean", "min", "max"]`. Tests inject a fake.

### Modified: `weather_analysis/collectors/open_meteo.py`

`fetch_history` switches `FORECAST_URL` → `ARCHIVE_URL`
(`https://archive-api.open-meteo.com/v1/archive`) and `past_days`/`forecast_days` →
`start_date`/`end_date` (dates derived from `days`). `min(days, 92)` is removed.

Unchanged: the `METRICS` map (the archive accepts the same variable names), the `ts > now` guard
(the archive also returns the current day's future hours), and `fetch_current`, which keeps using
the forecast endpoint because the archive has no `current` block.

### New: `weather_analysis/validate.py`

```
plausible(measurement, config) -> bool
filter_implausible(measurements, config) -> tuple[list[Measurement], list[Measurement]]
```

A reading is dropped when its value falls outside the range for its `(area, metric)`. Built-in
defaults, chosen from measured data:

| Area | Metric | Range | Rationale |
|---|---|---|---|
| inside | temperature | 5 – 40 °C | coldest real reading 10.1 °C; glitch 0.0; 5 °C headroom both ways |
| outside | temperature | −40 – 60 °C | outside is legitimately sub-zero; only a physical-impossibility guard |
| any | humidity | 0 – 100 % | definitional |

**No rule means no filtering.** Pressure, wind, precipitation and cloud cover are unfiltered —
they come from Open-Meteo, which is a model and does not emit sensor glitches.

Two rules that are easy to get wrong:

- **Suffix resolution.** `temperature_min` / `temperature_max` must resolve to the `temperature`
  range. The glitch arrives almost exclusively via `min` (it perturbs `mean` by ~0.03 °C because
  it lasts only seconds), so a lookup that misses `temperature_min` filters nothing that matters.
- **Per-measurement, not per-row.** If an hour's `min` is implausible, drop only that measurement
  and keep `mean` and `max`. The hour keeps its usable fields.

Overridable in config, both globally and per sensor (e.g. a cellar that legitimately sits at 8 °C):

```yaml
validation:
  temperature:
    inside: [5, 40]
    outside: [-40, 60]

home_assistant:
  sensors:
    - entity_id: sensor.cellar_temperature
      valid_range: [0, 25]
```

Dropped readings are **counted and printed**, never discarded silently — a filter that hides its
own effects is how real data disappears unnoticed.

### Modified: `weather_analysis/collect.py`

A single `_store(conn, rows, config, label)` helper validates, inserts, and reports, replacing the
repeated fetch/insert/print blocks. Every source funnels through it, so validation cannot be
skipped by adding a collector later:

```
open-meteo history: 5194 readings, 5194 new
home-assistant history: 8880 readings, 8874 new (3 implausible dropped)
```

`backfill` calls three sources, each wrapped so one failure cannot abort the others:

1. `open_meteo.fetch_history` — outside, hourly, full range.
2. `home_assistant.fetch_history` — inside, 10-minute detail, whatever `states` retains.
3. `home_assistant_stats.fetch_statistics` — inside, hourly, full statistics retention.

2 and 3 overlap for ~10 days. That is intentional and harmless: different `source` values, so the
UNIQUE constraint keeps both, and the dashboard resamples hourly anyway.

### Modified: `dashboard/app.py`

**Gap fix.** `prepare_series` stops calling `.dropna()` after `.resample(rule).mean()`. Empty
buckets stay NaN and Plotly (`connectgaps` defaults to False) breaks the line. `groupby("label")
.resample(...)` builds each label's index from that label's own min→max, so NaN appears only
inside a series' real span. At the widest range the rule is `3h`: 1,920 buckets × 7 labels ≈ 13k
rows.

**Inside daily range.** `daily_range_chart` is generalized to accept a precomputed `daily` frame
with `min`/`mean`/`max` columns plus a colour, instead of deriving them from raw values. Callers:

- outside — `resample("1D").agg(["min", "mean", "max"])` on raw values (today's behaviour).
- inside — per selected room: `mean` from `<metric>`, `min` from `<metric>_min`, `max` from
  `<metric>_max`, **falling back to values derived from `<metric>` where `_min`/`_max` are
  absent**. That fallback is what covers the live 10-minute period, which has no statistics rows.

A room selectbox chooses which room to show, so the chart answers the same question as the outside
one: a single sensor's diurnal swing.

**No change needed** to the sensor list or the metric dropdown. The former filters
`metric == "temperature"`; the latter gates on `m in METRIC_LABELS`. Leaving `temperature_min`/
`temperature_max` out of `METRIC_LABELS` keeps them out of the UI automatically.

### Modified: `pyproject.toml`

Add `websocket-client>=1.7` to `dependencies`.

## Error handling

Matches the established pattern: `backfill` wraps each source, prints `FAILED (<exc>)`, and
continues. Specifically:

- Connection refused / bad URL → one clear message; the other two sources still run.
- `auth_invalid` → message naming the token, not a raw traceback.
- `success: false` from the command → report `error.message`.
- Entities with no statistics → warning, skip; other entities still stored.

## Testing

Pure parsing needs no mocks:

- ms → UTC datetime conversion.
- One hourly row → three measurements with the right metrics and `source`.
- Unit defaults to `°C` when the response carries none.
- `None` mean/min/max skipped, not stored as 0.
- Non-temperature sensor yields `humidity` / `humidity_min` / `humidity_max`.

Transport, via a `FakeClient` mirroring `FakeSession`:

- Auth handshake ordering.
- `fetch_statistics` requests `period: "hour"` and all three types.
- Missing entity skipped with a warning.

Open-Meteo:

- `fetch_history` hits `ARCHIVE_URL` with `start_date`/`end_date` and **no** `past_days`.
- `days=365` is not clamped to 92.
- Existing future-hour and `None` tests still pass.

Validation — the cases that protect real data:

- Inside `0.0` dropped; inside `10.1` **kept** (the real 21 Jan open-window morning).
- Outside `2.8` and `-15.0` **kept** — the regression that would otherwise erase the heating
  season.
- `temperature_min` resolves to the `temperature` range, so an implausible `min` is dropped.
- An hour with implausible `min` but valid `mean`/`max` keeps `mean` and `max`.
- Metrics with no rule (pressure, cloud cover) pass through untouched.
- Per-sensor `valid_range` overrides the area default.
- Dropped counts are reported, not silent.

Dashboard regression:

- `prepare_series` preserves NaN across a gap (the Apr–Jun outage in miniature).

## Verification

Beyond tests, on the Pi: `backfill --days 240` reaches back to 2025-11-17, two days before the
statistics begin, so it should store roughly 174 days × 24 h × 6 sensors × 3 metrics ≈ 75k
statistics rows, plus ~240 days × 24 h × 7 metrics ≈ 40k outside rows. The Trends chart at "All
data" must show a visible break across 2026-04-06 → 2026-06-08 rather than a straight line.

Expect the two inside sources to disagree slightly in the overlap: an hourly mean is not an
instantaneous sample. That is correct behaviour, not drift.

The deployed database already holds glitch rows from the first backfill. The filter only guards
new writes, so deployment includes a one-off deletion:

```sql
DELETE FROM measurements
WHERE area = 'inside' AND metric LIKE 'temperature%' AND value < 5.0;
```

Verify it removes exactly the known glitches and nothing else — as of 2026-07-14 that is 3 rows in
`weather.db` (2026-07-14 16:20–16:23, dining room / bathroom / kitchen). HA's statistics hold 5
glitch hours in total; the two from 2026-06-18 15:00 (bedroom, dining room) predate the 10-day
`states` window and will arrive, already filtered, with the statistics backfill. Confirm the count
before and after rather than assuming.
