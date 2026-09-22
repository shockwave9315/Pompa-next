# API contract

The default contract of Pompa Next was frozen at Stage 3. Stage 4A adds the opt-in capability and
physical-reading forms described below while preserving those default response semantics.

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

The application contract exposes the five product API endpoints above; FastAPI may additionally
expose its standard documentation/OpenAPI routes (`/docs`, `/redoc`, `/openapi.json`). `/api/v1` is
a fresh namespace, not inherited legacy versioning.

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

Current in-memory metric state. With no `include` parameter, the Stage 3 response is unchanged.
No database I/O: it stays `200` during a MariaDB
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

`mode="retained"` means the last retained delivery is still factually available as cached state —
it is not confirmation that the source or device is currently alive. This survives LWT `Offline`,
an MQTT disconnect, and a reconnect into a new connection epoch: none of those events clear a
source's retained cache, because it was never evidence of life in the first place. After any of
them, `/api/v1/live` may keep showing `mode="retained"` with its original value and `received_at`,
while `mqtt.alive` is `false` and no historical evidence is created. A client must not read a
retained value as a current/live confirmation.

`mqtt.connected`, `mqtt.alive` and `mqtt.epoch` are the same facts `/api/v1/status` reports.
Per-source diagnostics belong to `/api/v1/status`, not here.

Live COP is not part of the contract. COP is defined on canonical minutes and is served by
`/api/v1/history`, including `bucket=1m`.

## `GET /api/v1/metrics`

The frontend-safe catalog. With no `include` parameter, the Stage 3 response is unchanged.
No database I/O, `200` during MariaDB and MQTT outages.
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

MQTT topics, source identity and control capabilities are absent from the default form. Topic
diagnostics live in `/api/v1/status`; there is no control/SET surface in this API.

## `GET /api/v1/status`

Facts about MQTT, the recorder, the database and every physical source. No parameters. Stays `200`
while the process is alive, including when MariaDB is unavailable — that failure is reported inside
`database`.

`now`, the MQTT facts, the recorder facts, every source entry and the `alive` verdict are one locked
observation: `now` is read inside the recorder lock, so no MQTT message can be applied between
sampling it and serialising the state it describes. `now` is the raw `CLOCK_REALTIME` reading — the
same clock `alive` and every freshness verdict are measured against, and the same one
`Recorder._purge` uses, so `database.purge_cutoff` describes the cutoff purge will actually apply.

Two consequences are worth stating plainly rather than hiding behind a clamp. While the process is
running normally, no timestamp in the observation is later than `now`. Immediately after a real
backward `CLOCK_REALTIME` step (`ARCHITECTURE.md` §24), stamps that were *recorded before the
correction* can be later than `now` — `sources[].last_received_at`, `mqtt.last_live_message_at`,
`recorder.last_closed_minute`, a retained `received_at` in `/api/v1/live`. That is reported
truthfully: those events really were observed at those readings, and rewriting them to fit the
corrected clock would be fabrication. No *confirmed* freshness stamp is ever among them, because a
detected step discards confirmed evidence outright and `mqtt.clock_steps` counts that it happened.

This atomicity is scoped to that one locked read; it is not a claim that every field in the response
is one byte-atomic multi-field transaction — some recorder maintenance facts (rollup/purge outcomes)
are updated by the recorder thread between ticks and are factual snapshots of their own last run,
not part of the `now`-locked observation.

Top-level keys: `now`, `mqtt`, `recorder`, `database`, `sources`.

`mqtt`: `connected`, `epoch`, `connects`, `disconnects`, `connected_at`, `disconnected_at`,
`lwt` (`state`, `retained`, `received_at`, `messages`), `alive`, `alive_since`,
`last_live_message_at`, `stale_after_seconds`, `parse_rejects`, `uncatalogued_topics`,
`clock_steps` and `last_clock_step_at` (detected backward `CLOCK_REALTIME` steps; each one
discarded the confirmed source evidence recorded before it — see `ARCHITECTURE.md` §24).

`recorder`: `process_start`, `last_closed_minute`, `last_row_minute`, `last_written_minute`,
`rows_closed`, `rows_written`, `protected_rows`, `waiting_rows`, `waiting_capacity`,
`flush_in_progress`, `dropped_rows`, `refused_rows`, `last_refusal`, `schema_ready`,
`db_last_ok_at`, `db_last_error`, `db_last_error_at`, `retention_1m_days`, `rollup`
(`last_rolled_hour`, `last_rolled_at`, `error`, `error_at`) and `purge` (`last_run_at`,
`last_cutoff`, `last_deleted_rows`, `deleted_rows`, `error`, `error_at`).

Three counters distinguish why a row never reached the database, and none of them overlap:

- `dropped_rows` — waiting-queue overflow: a closed minute was never even submitted to storage,
  because the FIFO waiting queue was already at `waiting_capacity`.
- `protected_rows` — a submitted batch whose write outcome is ambiguous (the database was
  unreachable or its acknowledgement was lost). It may already be committed; it is retried
  unchanged, idempotently, until a write returns success. Never dropped, never counted anywhere.
- `refused_rows` — a submitted row that was definitely, permanently unwritable: it would have
  landed in an already-rolled hour whose raw evidence was already purged, so rebuilding that hour
  from it would replace a complete rollup with a partial one. The write is refused before anything
  is upserted, so nothing is lost from storage, and retrying could never succeed. `last_refusal`
  carries the most recent such event: `at`, `hours` (the refused hour(s)), `rows` (how many), and
  `reason`.

`database`: `available`, `error`, `oldest_minute`, `newest_minute`, `rolled_until`, `purge_cutoff`.
When `available` is `false`, `error` carries the failure text and the other four are `null`.
`purge_cutoff` is the prospective purge policy — what purge may delete next, following the wall
clock and `RETENTION_1M_DAYS`, and it can move backwards with either. It is never evidence of what
raw data was already deleted; that fact is per-hour (§8/§13 of `ARCHITECTURE.md`) and surfaces
through `422` and `auto` promotion in `/api/v1/history`, not through this field.

`sources`: one entry per physical MQTT source with `id`, `topic`, `metric`, `seen_live`,
`historical_value`, `epoch_last_live_at`, `last_value`, `last_outcome`, `last_retained`,
`last_received_at` and the process-lifetime measurement counters (`first_live_at`, `latest_live_at`,
`live_messages`, `retained_messages`, `sentinel_messages`, `rejected_messages`, `gap_count`,
`gap_sum_seconds`, `max_live_gap_seconds`, `mean_live_gap_seconds`).

`stale_after_seconds` is `600`, the accepted Stage 1 global freshness policy, decided from a
22.768 h uninterrupted real-runtime measurement on CT109 (max observed gap 305.059 s, no gap over
600 s; see `ARCHITECTURE.md` §4). The source gap counters remain in the response as the ongoing
evidence trail, not because the policy is undecided.

## `GET /api/v1/history`

All historical charts and summaries. The only endpoint that reads persisted data.

| Parameter | Required | Meaning |
|---|---|---|
| `from` | yes | Start instant, inclusive. ISO 8601 with an explicit offset, or `YYYY-MM-DD` local midnight. Whole minutes only. |
| `to` | yes | End instant, exclusive. Same forms. Must be later than `from`. |
| `bucket` | no, default `auto` | `auto`, `1m`, `5m`, `1h`, `1d`, `total`. |
| `series` | no, default all | Comma-separated metric keys plus `cop_co`, `cop_dhw`, `cop_total`. No duplicates. |

`auto` chooses its base bucket from range length alone: ≤36 h → `1m`, ≤10 days → `5m`, ≤120 days →
`1h`, longer → `1d`. If that chosen `1m`/`5m` bucket would require the raw minutes of an hour that
was factually purged, `auto` promotes to `1h` instead — a whole rolled hour still answers from
`rollup_1h`. The exact partial edge of a promoted request can still be `422` (see below). `1d` is a
Europe/Warsaw calendar day, so DST days are 1380 or 1500 minutes. A response never exceeds 3000
buckets.

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
  Immediately after a real backward `CLOCK_REALTIME` step (`ARCHITECTURE.md` §24), a range that
  reaches up to the current, now-corrected instant can transiently report `coverage_percent` above
  `100` — rows persisted before the correction carry their original timestamps and can briefly look
  like they belong to a range that has not fully elapsed yet by the corrected clock. This is not
  clamped: doing so would hide what is actually stored. It resolves on its own as real time passes.
- For an ordinary metric, `minutes` is `0` where a bucket has no known value, and its other fields
  (`avg`/`last`, `min`, `max`, `kwh`) are `null` there. For a COP series, `paired_minutes` is `0`
  where a bucket has no paired minutes, and `cop`, `input_kwh`, `output_kwh` are `null` there.
  A `null` value alongside `recorded_minutes > 0` means the metric was unknown in the recorded
  minutes, not that the minutes are missing.
- `kwh` is `Σ W / 60000` over known minutes only, never extrapolated over missing ones.
- `cop` is `Σ paired output / Σ paired input` over minutes where all required power channels are
  known. It is `null` when there are no paired minutes or the paired input sum is 0. Instantaneous
  COP values are never averaged.

Missing minutes stay missing: no zero-fill, interpolation, extrapolation or backfill, in any
bucket.

## Error codes

| Status | When |
|---|---|
| `400` | Malformed parameters: missing `from`/`to`, unparseable or naive timestamps, non-minute alignment, `from >= to`, unknown bucket, unknown or duplicate series, invalid or repeated `include`. |
| `422` | Well-formed but unrepresentable: more than 3000 buckets, a range or partial edge hour whose raw minutes were provably purged, instants outside 1970–2100. |
| `503` | `/api/v1/history` only: the database is unavailable. |

The body is `{"detail": "…"}`. `422` for purged raw means the backend knows the minutes existed and
were physically deleted — it is never a consequence of a range simply being old. A range that was
never recorded at all — no raw minutes and no rollup row for it, including one predating the
recorder — is answered normally with `recorded_minutes = 0`, not `422`. A whole rolled hour whose
raw was purged is still served, as `1h`/`1d`/`total`, from its surviving `rollup_1h` row; only an
exact partial-hour edge into a purged hour is unrepresentable. `422` never results in a rounded or
truncated range — the request is refused instead.

## Subsystem independence

| Condition | `/health` | `/api/v1/live` | `/api/v1/metrics` | `/api/v1/status` | `/api/v1/history` |
|---|---|---|---|---|---|
| MariaDB unavailable | 200 | 200 | 200 | 200, `database.available=false` | 503 |
| MQTT disconnected | 200 | 200, no confirmed metrics, retained only where factual | 200 | 200, `mqtt.connected=false`, `mqtt.alive=false` | 200 from persisted data |

One subsystem's failure is never turned into process failure or into a global verdict.

## Stage 4A opt-in capability and physical-reading forms

`GET /api/v1/metrics?include=capabilities` returns the default `/metrics` body plus exactly one
top-level `capabilities` array. It contains all 203 effective identities in order: TOP0–TOP143,
OPT0–OPT6, SET1–SET46, XTOP0–XTOP5. Each entry has exactly:

```json
{
  "identity": "TOP9",
  "key": "top_9",
  "family": "TOP",
  "name": "DHW_Target_Temp",
  "topic": "main/DHW_Target_Temp",
  "description": "DHW target temperature (°C)",
  "provenance": "documented",
  "readable": true,
  "canonical_metric": null,
  "source_priority": null
}
```

`readable` is `true` for TOP/OPT/XTOP and `false` for SET. A canonical physical source has its
metric key and zero-based priority; other entries have `null` for both. TOP/OPT/SET topics come
from the documented reference. XTOP0/2/3/5 topics come from verified canonical sources; the exact
received paths `extra/Cool_Power_Consumption_Extra` (XTOP1) and
`extra/Cool_Power_Production_Extra` (XTOP4) were supplied from CT109 `mqtt.uncatalogued_topics`.
No type, history, safety or control policy is implied by these fields.

`GET /api/v1/live?include=readings` returns the default `/live` body plus exactly one top-level
`readings` object. It contains all 157 readable identities in order: TOP0–TOP143, OPT0–OPT6,
XTOP0–XTOP5. SET identities are absent. Each identity maps to exactly:

```json
{
  "topic": "main/DHW_Target_Temp",
  "value": 48,
  "kind": "number",
  "raw": "48",
  "mode": "live",
  "received_at": "2027-01-15T08:00:01Z"
}
```

`raw` preserves the decoded MQTT payload. `value` is a finite decimal number or outer-trimmed
text, and `kind` is `number` or `text`. Physical values retain sentinels such as `-200` as numeric
facts; canonical metrics continue to apply their own sentinel and range rules independently.
The entire opt-in live response, including `now`, MQTT facts, canonical metrics and physical
readings, is one recorder-locked observation. Neither opt-in form reads MariaDB; both work before
MQTT connects.

For an identity without a current reading, the entry is still present: `topic` is its known path
or `null`, and `value`, `kind`, `raw` and `received_at` are `null`, with `mode: "none"`. All 157
readable identities currently have known topics; an unpublished OPT or XTOP reading still has
`mode: "none"` rather than a synthesized value.
For a payload, `mode` is `retained` if the MQTT delivery was retained and `live` otherwise.
Physical `mode="live"` means the latest non-retained receipt in the current valid connection
lifecycle. It does **not** claim freshness under the canonical 600-second source rule; generic
physical readings have no per-capability freshness policy. A disconnect, LWT `Offline`, new
connection, or detected backward clock step clears these physical readings until new deliveries
arrive. This differs from the canonical `/live.metrics` retained fallback described above.

Each endpoint accepts only its one documented `include` value or no `include`; unknown,
comma-separated, empty, and repeated `include` values return `400`. The five endpoint paths remain
unchanged. `/status` keeps its Stage 3 shape and `uncatalogued_topics` name, which can still list
known non-core capability topics. `/history` still accepts only canonical series keys.

Optional history, activity/events, reports and commands belong to later Stage 4 checkpoints.
This document lists no endpoint or response for them until implemented and contract-tested. The
frontend starts only after the complete product-backend contract is documented.
