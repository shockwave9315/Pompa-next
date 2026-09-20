# API contract

The frontend-facing contract of Pompa Next. It is frozen at Stage 3: Stage 4 builds against this
document and does not need to read backend code.

Domain rules behind it are in [`ARCHITECTURE.md`](ARCHITECTURE.md). This file describes only what
the HTTP surface promises.

## Endpoints

| Endpoint | Purpose | Needs MQTT | Needs MariaDB |
|---|---|---|---|
| `GET /health` | Process liveness | no | no |
| `GET /api/v1/status` | MQTT, recorder, database and source facts | no | no |
| `GET /api/v1/live` | Current in-memory metric state | no | no |
| `GET /api/v1/metrics` | Metric and COP catalog | no | no |
| `GET /api/v1/history` | All historical charts and summaries | no | yes |

There is no other path. `/api/v1` is a fresh namespace, not inherited legacy versioning.

## Shared truths

- Timestamps are ISO 8601 UTC with `Z`. Sub-second precision appears only where a receipt time has
  it.
- A `YYYY-MM-DD` input is a **calendar date in Europe/Warsaw** and means local midnight. Instant
  inputs must carry an explicit offset (`Z`, `+02:00`); naive timestamps are rejected.
- History intervals are exact half-open `[from, to)`. The backend never rounds, widens, narrows or
  truncates a requested range.
- `0` is real measured data. `null` means unknown or not available **at that position** of the
  response; it is never a zero and never a placeholder for a value that exists.
- A live value delivered from the MQTT retained store is labelled `mode="retained"`. Retained data
  never enters history.
- The backend owns domain truth. The frontend must not compute source priority, freshness, energy,
  COP, bucket alignment, aggregation, coverage or state interpretation.
- Status and live report facts. There is no `healthy`, `degraded`, `ready`, `partial`, `usable` or
  quality score anywhere in the API, and none may be invented by a client.

## `GET /health`

Process liveness only. Always `200` while the process answers:

```json
{"status": "ok"}
```

It is not a readiness probe: it says nothing about MQTT or MariaDB. Those are reported by
`/api/v1/status`.

## `GET /api/v1/live`

Current in-memory metric state. No parameters. No database I/O: it stays `200` during a MariaDB
outage. The snapshot *and its `now`* are taken under the recorder lock in one observation, so a
response never mixes state from before and after an MQTT message, and `received_at` never follows
the `now` of the response carrying it.

```json
{
  "now": "2027-01-15T08:10:00Z",
  "mqtt": {"connected": true, "alive": true, "epoch": 1},
  "metrics": {
    "main_outlet_temp": {
      "value": 35.0,
      "mode": "live",
      "source_id": "TOP6",
      "source_topic": "main/Main_Outlet_Temp",
      "received_at": "2027-01-15T08:09:50Z"
    }
  }
}
```

Every canonical metric of the catalog appears exactly once in `metrics`, in catalog order, with
exactly the five fields above.

`mode` is protocol provenance, never a health verdict:

| `mode` | Meaning | `value` | `source_id` / `source_topic` | `received_at` |
|---|---|---|---|---|
| `live` | A confirmed fresh non-retained source of the current connection epoch | the selected value, `0` included | the selected physical source | receipt used for freshness |
| `retained` | No confirmed source exists; this is the latest retained delivery | the cached value | the source that delivered it | when that retained delivery arrived |
| `none` | Nothing current to show | `null` | `null` | `null` |

Live selection, per metric, is exactly historical selection: the first source in catalog priority
order that is `seen_live` in the current epoch, valid and fresh, while MQTT is connected and LWT is
not `Offline`. So a fresh `TOP16` beats a retained or stale `XTOP0`, and later live `XTOP0`
evidence takes priority back. A stale non-retained value is never shown.

Retained fallback applies only when that selection yields nothing and the source's latest delivery
was retained, valid and non-null. It sets no `seen_live`, proves no source life, changes no
freshness, makes MQTT neither alive nor connected, and enters no `MinuteRow`. A later invalid or
sentinel message hides the retained value behind it: the mode becomes `none`, not the older
retained number.

`mqtt.connected`, `mqtt.alive` and `mqtt.epoch` are the same facts `/api/v1/status` reports.
Per-source diagnostics belong to `/api/v1/status`, not here.

Live COP is not part of the contract. COP is defined on canonical minutes and is served by
`/api/v1/history`, including `bucket=1m`.

## `GET /api/v1/metrics`

The frontend-safe catalog. No parameters, no database I/O, `200` during MariaDB and MQTT outages.
It is derived from the one metric catalog, so it cannot drift from `/live` and `/history`.

```json
{
  "timezone": "Europe/Warsaw",
  "history": {"buckets": ["auto", "1m", "5m", "1h", "1d", "total"], "max_buckets": 3000},
  "metrics": [
    {
      "key": "main_outlet_temp",
      "label": "Temperatura zasilania",
      "unit": "°C",
      "group": "temperature",
      "kind": "mean",
      "history_fields": ["avg", "min", "max", "minutes"],
      "energy": false
    }
  ],
  "cop": [
    {
      "key": "cop_co",
      "label": "COP CO",
      "unit": null,
      "kind": "cop",
      "history_fields": ["cop", "paired_minutes", "input_kwh", "output_kwh"]
    }
  ]
}
```

- `metrics` lists every canonical metric in catalog order — the same keys, in the same order, as
  `/api/v1/live`.
- `history_fields` are exactly the value fields `/api/v1/history` returns for that series:
  `kind=mean` → `avg, min, max, minutes`; `kind=last` → `last, min, max, minutes`; a power metric
  adds `kwh`.
- `energy` is `true` exactly for the power metrics that expose `kwh`.
- `cop` lists `cop_co`, `cop_dhw` and `cop_total` with the labels and fields `/api/v1/history` uses.
- `unit` is `null` where a metric has no unit (flags, modes, counters, COP).
- `timezone` is the presentation timezone for calendar days; `history.buckets` and
  `history.max_buckets` are the values `/api/v1/history` actually accepts and enforces.

MQTT topics, source identity and control capabilities are deliberately absent. Topic diagnostics
live in `/api/v1/status`; there is no control/SET surface in this API.

## `GET /api/v1/status`

Facts about MQTT, the recorder, the database and every physical source. No parameters. Stays `200`
while the process is alive, including when MariaDB is unavailable — that failure is reported inside
`database`.

`now`, the MQTT and recorder facts, every source entry and the `alive` verdict are one locked
observation, so no timestamp in them is later than `now`. The `database` object is read afterwards
and is not part of that observation; `raw_floor` is still computed from the same `now`.

Top-level keys: `now`, `mqtt`, `recorder`, `database`, `sources`.

`mqtt`: `connected`, `epoch`, `connects`, `disconnects`, `connected_at`, `disconnected_at`,
`lwt` (`state`, `retained`, `received_at`, `messages`), `alive`, `alive_since`,
`last_live_message_at`, `stale_after_seconds`, `parse_rejects`, `uncatalogued_topics`.

`recorder`: `process_start`, `last_closed_minute`, `last_row_minute`, `last_written_minute`,
`rows_closed`, `rows_written`, `protected_rows` (submitted to storage, write not yet confirmed;
retried unchanged, never dropped), `waiting_rows` (closed, never submitted), `waiting_capacity`,
`flush_in_progress`, `dropped_rows`, `schema_ready`, `db_last_ok_at`, `db_last_error`,
`db_last_error_at`, `retention_1m_days`, `rollup` (`last_rolled_hour`, `last_rolled_at`, `error`,
`error_at`) and `purge` (`last_run_at`, `last_cutoff`, `last_deleted_rows`, `deleted_rows`,
`error`, `error_at`).

`database`: `available`, `error`, `oldest_minute`, `newest_minute`, `rolled_until`, `raw_floor`.
When `available` is `false`, `error` carries the failure text and the other four are `null`.

`sources`: one entry per physical MQTT source with `id`, `topic`, `metric`, `seen_live`,
`historical_value`, `epoch_last_live_at`, `last_value`, `last_outcome`, `last_retained`,
`last_received_at` and the process-lifetime measurement counters (`first_live_at`, `latest_live_at`,
`live_messages`, `retained_messages`, `sentinel_messages`, `rejected_messages`, `gap_count`,
`gap_sum_seconds`, `max_live_gap_seconds`, `mean_live_gap_seconds`).

`stale_after_seconds` is still the bootstrap value 600 and not an accepted freshness policy; the
source gap counters exist to decide it.

## `GET /api/v1/history`

All historical charts and summaries. The only endpoint that reads persisted data.

| Parameter | Required | Meaning |
|---|---|---|
| `from` | yes | Start instant, inclusive. ISO 8601 with an explicit offset, or `YYYY-MM-DD` local midnight. Whole minutes only. |
| `to` | yes | End instant, exclusive. Same forms. Must be later than `from`. |
| `bucket` | no, default `auto` | `auto`, `1m`, `5m`, `1h`, `1d`, `total`. |
| `series` | no, default all | Comma-separated metric keys plus `cop_co`, `cop_dhw`, `cop_total`. No duplicates. |

`auto` resolves from range length only: ≤36 h → `1m`, ≤10 days → `5m`, ≤120 days → `1h`, longer →
`1d`, promoted to `1h` when raw retention cannot serve minutes. `1d` is a Europe/Warsaw calendar
day, so DST days are 1380 or 1500 minutes. A response never exceeds 3000 buckets.

```json
{
  "from": "2027-01-15T08:00:00Z",
  "to": "2027-01-15T08:03:00Z",
  "bucket": "1m",
  "requested_bucket": "auto",
  "buckets": [
    {"start": "2027-01-15T08:00:00Z", "end": "2027-01-15T08:01:00Z",
     "expected_minutes": 1, "recorded_minutes": 1, "coverage_percent": 100.0}
  ],
  "series": {
    "main_outlet_temp": {"label": "Temperatura zasilania", "unit": "°C", "kind": "mean",
                         "avg": [35.0], "min": [35.0], "max": [35.0], "minutes": [1]},
    "cop_co": {"label": "COP CO", "unit": null, "kind": "cop",
               "cop": [4.0], "paired_minutes": [1], "input_kwh": [0.015], "output_kwh": [0.06]}
  }
}
```

- `bucket` is the resolved bucket, `requested_bucket` is what was asked for.
- Every series array has exactly one entry per bucket, in bucket order. `series` keys appear in the
  order requested.
- `expected_minutes` counts elapsed minutes of the bucket and is never trimmed to recorder start.
  `recorded_minutes` counts stored rows. `coverage_percent` is `null` when `expected_minutes` is 0.
- `minutes` is `0` where a bucket has no known value; every other series field is `null` there.
  A `null` value with `recorded_minutes > 0` means the metric was unknown in the recorded minutes.
- `kwh` is `Σ W / 60000` over known minutes only, never extrapolated over missing ones.
- `cop` is `Σ paired output / Σ paired input` over minutes where all required power channels are
  known. It is `null` when there are no paired minutes or the paired input sum is 0. Instantaneous
  COP values are never averaged.

Missing minutes stay missing: no zero-fill, interpolation, extrapolation or backfill, in any
bucket.

## Error codes

| Status | When |
|---|---|
| `400` | Malformed parameters: missing `from`/`to`, unparseable or naive timestamps, non-minute alignment, `from >= to`, unknown bucket, unknown or duplicate series. |
| `422` | Well-formed but unrepresentable: more than 3000 buckets, a range or partial edge hour older than raw retention, instants outside 1970–2100. |
| `503` | `/api/v1/history` only: the database is unavailable. |

The body is `{"detail": "…"}`. `422` never results in a rounded or truncated range — the request is
refused instead.

## Subsystem independence

| Condition | `/health` | `/api/v1/live` | `/api/v1/metrics` | `/api/v1/status` | `/api/v1/history` |
|---|---|---|---|---|---|
| MariaDB unavailable | 200 | 200 | 200 | 200, `database.available=false` | 503 |
| MQTT disconnected | 200 | 200, no confirmed metrics, retained only where factual | 200 | 200, `mqtt.connected=false`, `mqtt.alive=false` | 200 from persisted data |

One subsystem's failure is never turned into process failure or into a global verdict.
