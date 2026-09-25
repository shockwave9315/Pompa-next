# API contract

The default contract of Pompa Next was frozen at Stage 3. Stage 4A adds the opt-in capability and
physical-reading forms described below while preserving those default response semantics. Stage 4B
checkpoint B adds one opt-in metrics form and one new DB-backed endpoint pair for the
optional-history policy. Checkpoint C adds internal raw minute recording. Checkpoint D adds
persisted optional-series discovery and explicit optional history selectors; default responses
remain canonical. The owner validated these Stage 4B endpoints on CT109. Stage 4C checkpoint C adds
one range activity resource and one current activity resource; every earlier response is unchanged.
Stage 4D-A freezes the future `GET /api/v1/report` contract below; it is not implemented yet.

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
| `GET /api/v1/optional-history/selection` | Active-vs-pending optional-history selection | no | yes |
| `PUT /api/v1/optional-history/selection` | Replace the desired optional-history selection | no | yes |
| `GET /api/v1/optional-history/series` | Discover persisted optional historical meanings | no | yes |
| `GET /api/v1/activity` | Activity timeline, compressor runs, off intervals, defrosts and summary over `[from, to)` | no | yes |
| `GET /api/v1/activity/live` | Current activity from the in-memory live observation | no | no |
| `GET /api/v1/report` (Stage 4D-C) | Day/week/month/custom Warsaw calendar report | no | yes |

The application currently exposes the first ten product API endpoints above; the report endpoint
is a frozen Stage 4D-C addition. FastAPI may additionally
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

Historical charts and summaries from persisted data.

| Parameter | Required | Meaning |
|---|---|---|
| `from` | yes | Start instant, inclusive. ISO 8601 with an explicit offset, or `YYYY-MM-DD` local midnight. Whole minutes only. |
| `to` | yes | End instant, exclusive. Same forms. Must be later than `from`. |
| `bucket` | no, default `auto` | `auto`, `1m`, `5m`, `1h`, `1d`, `total`. |
| `series` | no, default canonical set | Comma-separated canonical metric/COP keys and exact persisted optional selectors such as `optional:TOP21@1`. No duplicates. |

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
- An explicit `optional:IDENTITY@VERSION` selector resolves to one persisted `optional_series`
  meaning. Mean series return `avg`, `min`, `max`, `selected_minutes`, `known_minutes`; last series
  return `last`, `min`, `max`, `selected_minutes`, `known_minutes`. Persisted `energy=true` adds
  `kwh = Σ known minute-average W / 60000`. Metadata comes from the persisted series row:
  `series_id`, `identity`, `topic`, `profile_version`, `label`, `unit`, `kind`, `semantic_type`,
  `energy`. A selected unknown minute increments only `selected_minutes`; a missing recorded
  minute increments neither count. Different versions are separate selectors and never joined.
  Bare identities, malformed versions, and unknown persisted meanings return 400. No optional
  selector is included by default. Mixed canonical and optional requests share one database
  snapshot, bucket calendar, raw/rollup boundary, and 3000-bucket limit.
- A valid optional bucket may have known values whose binary-float sum cannot be represented.
  Its selected/known counts and min/max/last remain durable. `kind=last` has no historical sum
  and remains queryable. A requested mean `avg` or `energy=true` `kwh` that needs an
  unrepresentable sum returns 422; it is never reported as unknown, zero, infinity, or a 500.
- `cop` is `Σ paired output / Σ paired input` over minutes where all required power channels are
  known. It is `null` when there are no paired minutes or the paired input sum is 0. Instantaneous
  COP values are never averaged.

Missing minutes stay missing: no zero-fill, interpolation, extrapolation or backfill, in any
bucket.

## Error codes

| Status | When |
|---|---|
| `400` | Malformed parameters: missing `from`/`to`, unparseable or naive timestamps, non-minute alignment, `from >= to`, unknown bucket, unknown or duplicate series, invalid or repeated `include`. |
| `422` | Well-formed but unrepresentable: more than 3000 history buckets, a history range or partial edge hour whose raw minutes were provably purged, an optional mean/energy bucket whose known-value sum cannot fit in binary DOUBLE, instants outside 1970–2100, an activity range longer than 31 days and one hour, or activity-unavailable history. Stage 4D report-specific cases are below. |
| `500` | `/api/v1/activity`: stored durable activity is inconsistent. A row may be invalid or not in its exact canonical persisted form, or an hour's durable segment minutes may differ from its recorded canonical minutes. This applies to widened evidence too. It is never answered from raw instead. Stage 4D report consistency cases are below. |
| `503` | The database is unavailable for a DB-backed resource (`/history`, optional-history selection/series, `/activity`, or the Stage 4D report once implemented). |

The body is `{"detail": "…"}`. `422` for purged raw means the backend knows the minutes existed and
were physically deleted — it is never a consequence of a range simply being old. A range that was
never recorded at all — no raw minutes and no rollup row for it, including one predating the
recorder — is answered normally with `recorded_minutes = 0`, not `422`. A whole rolled hour whose
raw was purged is still served, as `1h`/`1d`/`total`, from its surviving `rollup_1h` row; only an
exact partial-hour edge into a purged hour is unrepresentable. `422` never results in a rounded or
truncated range — the request is refused instead.

## Subsystem independence

| Condition | `/health` | `/api/v1/live` | `/api/v1/metrics` | `/api/v1/status` | `/api/v1/history` | `/api/v1/optional-history/selection` | `/api/v1/activity` | `/api/v1/activity/live` |
|---|---|---|---|---|---|---|---|---|
| MariaDB unavailable | 200 | 200 | 200 | 200, `database.available=false` | 503 | 503 | 503 | 200, unchanged result |
| MQTT disconnected | 200 | 200, no confirmed metrics, retained only where factual | 200 | 200, `mqtt.connected=false`, `mqtt.alive=false` | 200 from persisted data | 200 (selection needs no MQTT) | 200 from persisted data | 200, `activity="unknown"` |

One subsystem's failure is never turned into process failure or into a global verdict.

## Stage 4A opt-in capability and physical-reading forms

`GET /api/v1/metrics?include=capabilities` returns the default `/metrics` body plus exactly one
top-level `capabilities` array. It contains all 203 effective identities in order: TOP0–TOP143,
OPT0–OPT6, SET1–SET46, XTOP0–XTOP5. Each entry has exactly:

```json
{
  "identity": "TOP9",
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

`identity` (e.g. `TOP9`, `XTOP0`) is the one stable physical-capability handle; there is no separate
capability `key`. A capability's relationship to the canonical core, when any, is expressed only by
`canonical_metric` (the canonical metric's own key) and zero-based `source_priority`; other entries
have `null` for both. A physical capability identity and a canonical logical series key are distinct
namespaces and are never conflated into one field.

`readable` is `true` for TOP/OPT/XTOP and `false` for SET. TOP/OPT/SET topics come
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
  "available": true,
  "received_at": "2027-01-15T08:00:01Z"
}
```

`raw` preserves the decoded MQTT payload. `value` is a finite decimal number or outer-trimmed
text, and `kind` is `number` or `text`. Physical values retain sentinels such as `-200` as numeric
facts; canonical metrics continue to apply their own sentinel and range rules independently.
The entire opt-in live response, including `now`, MQTT facts, canonical metrics and physical
readings, is one recorder-locked observation: `available` is derived from the same `now` already
sampled for the response, never a second clock read or a second recorder lock. Neither opt-in form
reads MariaDB; both work before MQTT connects.

For an identity without a current reading, the entry is still present: `topic` is its known path
or `null`, and `value`, `kind`, `raw` and `received_at` are `null`, with `mode: "none"` and
`available: false`. All 157 readable identities currently have known topics; an unpublished OPT or
XTOP reading still has `mode: "none"` rather than a synthesized value.

`mode` is MQTT provenance, never a health verdict: `retained` if the last MQTT delivery was
retained, `live` if the latest non-retained receipt in the current valid connection lifecycle, or
`none` if there is no current reading. A disconnect, LWT `Offline`, new connection, or detected
backward clock step clears these physical readings until new deliveries arrive. This differs from
the canonical `/live.metrics` retained fallback described above.

`available` is the current physical-display fact: whether this reading may be shown as live right
now. It is `true` only when all of the following hold, using the same accepted `stale_after`
boundary (`STALE_AFTER_SECONDS`, default 600) as canonical freshness: a payload exists; the last
delivery was non-retained (`mode == "live"`); MQTT is currently connected; the current LWT is not
`Offline`; `received_at` exists; and `now - received_at <= stale_after`. Otherwise `available` is
`false` — including for `mode: "retained"` and `mode: "none"`. `available=true` is a live-surface
fact only. It does **not** imply Stage 4B optional-history eligibility, selection or persistence for
that identity.

Each endpoint accepts only its one documented `include` value or no `include`; unknown,
comma-separated, empty, and repeated `include` values return `400`. The five Stage 1–4A endpoint
paths remain unchanged. `/status` keeps its Stage 3 shape and `uncatalogued_topics` name, which can
still list known non-core capability topics. Default `/history` remains canonical; explicit
Stage 4B selectors are documented below.

Report implementation and commands belong to later Stage 4 checkpoints. The frozen future report
contract is below. The frontend starts only after the complete product-backend contract is
documented.

## Stage 4B optional-history selection

Checkpoint A froze the architecture (`ARCHITECTURE.md` §25.2.1); checkpoint B implements the
`HistoryProfile` domain model, the production immutable policy timeline, and this API. Checkpoint C
records selected-known optional values internally in `optional_sample_1m` for canonical minutes.
Checkpoint D adds durable hourly counts and explicit historical queries. Default `/live`,
`/metrics`, `/status`, and `/history` response shapes remain unchanged, and raw optional JSON is
not exposed publicly.

### `GET /api/v1/optional-history/series`

DB-backed and independent of MQTT. Returns `{"series": [...]}` in stable `series_id` order,
including disabled, old-version, and currently blocked meanings. Each entry has `selector`
(`optional:IDENTITY@VERSION`), `series_id`, `identity`, `topic`, `profile_version`, `label`,
`unit`, `kind`, `semantic_type`, and `energy` from the persisted `optional_series` row. Empty
before the first selection; disabling selection does not remove historical metadata. Returns
503 when MariaDB is unavailable.

### `GET /api/v1/metrics?include=history_profiles`

Returns the default `/metrics` body (unchanged) plus one top-level `history_profiles` array, one
entry per code-side `HistoryProfile` (`docs/ARCHITECTURE.md` §25.2.1):

```json
{
  "identity": "TOP21",
  "topic": "main/Outside_Pipe_Temp",
  "profile_version": 1,
  "label": "Temperatura rury zewnętrznej",
  "unit": "°C",
  "kind": "mean",
  "semantic_type": "measurement",
  "energy": false,
  "selectable": true,
  "blocked_reason": null
}
```

`selectable`/`blocked_reason` here describe the **current code-side profile and current Stage 4A
capability only** — this form never reads the database, so it never compares against a persisted
`optional_series` row and never reports `profile_definition_changed`. A stored-policy conflict for
an already-selected series is database state, reported only by the selection GET/PUT surface below;
a profile can read `selectable: true` here while a `PUT` for it still fails with `409` if its
persisted definition has drifted from current code. `selectable` is `true` exactly when
`blocked_reason` is `null`; a reason here is one of `profile_missing`, `profile_version_changed`,
`topic_changed` or `capability_topic_changed` (`ARCHITECTURE.md` §25.2.8). Sentinels and min/max
stay backend-internal and are not exposed here. No database I/O: this form has the same
MQTT/MariaDB independence as the default `/metrics` response. `include` still accepts exactly one
of `capabilities` or `history_profiles`, never both.

### `GET /api/v1/optional-history/selection`

DB-backed. Resolves the policy timeline for the current minute and reports active-versus-pending
selection:

```json
{
  "active_revision": {"id": 1, "effective_from": "1970-01-01T00:00:00Z"},
  "head_revision": {"id": 2, "effective_from": "2027-01-15T09:01:00Z"},
  "pending": true,
  "active_members": [],
  "head_members": [
    {
      "identity": "TOP21", "topic": "main/Outside_Pipe_Temp", "profile_version": 1,
      "label": "Temperatura rury zewnętrznej", "unit": "°C", "kind": "mean",
      "semantic_type": "measurement", "energy": false, "blocked_reason": null
    }
  ]
}
```

`active_revision` is the revision governing the current minute; `head_revision` is the latest
accepted revision regardless of when it takes effect. `pending` is `true` exactly when they differ.
Each member's `blocked_reason` reflects the *persisted* series snapshot against *current* code and
capabilities — here a fifth reason, `profile_definition_changed`, can also appear: the same
`(identity, expected_topic, profile_version)`, but the persisted definition (unit/kind/sentinels/
min/max/energy) no longer matches current code, a code-definition error rather than a device or
capability fact. Either way, a member can become blocked long after its revision was created
without that revision ever being mutated or deleted. `503` when the database is unavailable.

### `PUT /api/v1/optional-history/selection`

DB-backed. Body:

```json
{"base_revision": 2, "identities": ["TOP21", "XTOP1"]}
```

Replaces the *whole* desired selection; an empty `identities` list is legal. Success:

```json
{"revision": {"id": 3, "effective_from": "2027-01-15T09:05:00Z"}, "idempotent_replay": false}
```

`effective_from` is always a future, minute-aligned instant no earlier than the currently open
minute, the latest committed canonical minute, or the current head's own `effective_from`
(`ARCHITECTURE.md` §25.2.1, Part 8); disabling a series never deletes its history, and no revision
is ever mutated. `idempotent_replay: true` means this exact request (same `base_revision`, same
resolved series set) already succeeded — an ambiguous retry after a lost acknowledgement is
answered with the revision it actually produced, not a second one and not a conflict.

| Status | When |
|---|---|
| `200` | Applied (or an identical idempotent retry of an already-applied request). |
| `400` | Duplicate identity, unknown identity, or a currently unselectable (blocked) profile. |
| `409` | `base_revision` is stale (the head has moved and this is not that head's own retry), **or** a requested identity already has a persisted series whose stored definition no longer matches current code. |
| `503` | The database is unavailable. |

A `409` for a stale `base_revision` means the caller must `GET` the current selection and decide
again; the backend never guesses which of two concurrent, genuinely different requests should win.
A `409` for a stored-definition conflict means an identity's on-disk historical meaning disagrees
with the running code for the exact same `(identity, expected_topic, profile_version)` — a
deployment/code error, not a race — and it fails the whole request closed: no revision is created,
the head does not move, and the old row is never mutated. An *ambiguous retry* (same
`base_revision`, same resolved identity set) is answered `200`/`idempotent_replay: true` only if
every one of those already-selected series still matches current code; if the stored definition
was altered in the meantime, the retry also fails `409` rather than falsely reporting success.

## Stage 4C activity

Domain rules are in `ARCHITECTURE.md` §25.3–25.3.3. Both resources expose measured facts only.
There is no cycle quality, short-cycling verdict or threshold, and none may be invented by a
client. Every state string is one of `off`, `idle`, `co`, `dhw`, `transition`, `defrost`,
`unknown`. Compressor strings are `off`, `on`, `unknown`.

### `GET /api/v1/activity?from=…&to=…`

The query is an exact, minute-aligned `[from, to)` of at most 31 days and one hour. `from` and `to`
use the same instant/date parsing as `/history`. The backend chooses and loads the evidence around
the range. At the right edge, `open` minutes become historical facts as the recorder settles them.

Top level:

| Field | Meaning |
|---|---|
| `from`, `to`, `now` | The request and the observation instant. |
| `closed_until` | The first minute whose historical outcome this process has not settled. It is at most `floor_minute(now)` and also stops at the accumulator's open minute or the oldest waiting/unacknowledged row. Minutes at or after it are not yet safe to classify as historical gaps. |
| `segment_rule_version` | The persisted minute/segment interpretation read (`1`). |
| `evidence` | `{from, to}`: whole UTC hours actually examined. Widening follows only spans crossing the range, in exponentially growing hour chunks, so it may extend past the decisive boundary by up to the last chunk. |
| `summary` | Range facts; see below. |
| `timeline` | Chronological positional items inside the range. |
| `compressor_runs`, `compressor_off_intervals`, `defrosts` | Every observed span intersecting the range, each with its full observed extent. |

**Summary fields:**
- Minute counts: `closed_minutes` counts the settled part of the request (before `closed_until`),
  with `recorded_minutes` and `gap_minutes`; `activity_minutes` and `compressor_minutes` list every
  recorded state including zeros.
- Start/stop counts: `observed_starts`, `observed_stops`.
- `compressor_runs_overlapping`, `defrosts_overlapping`: these count spans that intersect the
  range. A span crossing a range edge counts in both adjacent ranges, so these are **not
  additive**. Starts, stops and minute counts are additive.
- `complete_runs` (runs with both edges observed, attributed to the range holding their first
  minute) and `exact_off_intervals` (off intervals between two observed runs). Each is
  `{count, minutes[], total_minutes, min_minutes, max_minutes, mean_minutes}`, with `null`
  min/max/mean when the count is 0.
- `observed_defrost_seconds`.

**Span object.** `compressor_runs`, `compressor_off_intervals`, `defrosts` and each timeline
`event` use the same span fields:
- `start`, `end`, `minutes`: the full observed span, never clipped to the query.
- `overlap_start`, `overlap_end`, `overlap_minutes`: the span's part inside `[from, to)`.
- `starts_before_range`, `ends_after_range`: true only when recorded minutes of the same span lie
  outside the range.
- `start_boundary`, `end_boundary`: each is one of

  | Value | Meaning |
  |---|---|
  | `observed` | The adjacent minute proves the change. |
  | `unknown` | The adjacent minute is recorded but unclassifiable. |
  | `gap` | The adjacent minute is settled and was not recorded. |
  | `unavailable` | The adjacent minute's activity detail was purged before durable activity existed. |
  | `open` | The adjacent minute is not yet settled historical evidence; it may be current or awaiting persistence acknowledgement. |
  | `outside_evidence` | Not examined; appears only at the start of all history. |

- `start_observed`, `end_observed`.

**Type-specific span fields:**
- **Runs and timeline events:** `energy` is `{channel: {kwh, minutes}}` for
  `co_power_consumption`, `co_power_production`, `dhw_power_consumption` and
  `dhw_power_production`. `cop` is `{co|dhw|total: {cop, paired_minutes, input_kwh, output_kwh}}`,
  the same COP fields as `/history`. Both cover the **full observed span**, never a prorated query
  piece; both are `null` if the span's evidence cannot supply them.
- **Runs** also have `activity_minutes` (their composition) and `observed_defrost_seconds`.
- **Off intervals** also have `exact` (both edges observed runs).
- **Defrosts** also have `observed_defrost_seconds` (full span) and
  `overlap_observed_defrost_seconds`. These are exact relative to the recorded minute fractions,
  not physical transition seconds.

**Timeline items** are `{type, activity, start, end, minutes, event}`, where
`start`/`end`/`minutes` are positional inside the range:

| `type` | `activity` | `event` | Meaning |
|---|---|---|---|
| `activity` | state string (`unknown` means a recorded but unclassifiable minute) | span object | A maximal same-activity span. |
| `gap` | `null` | `null` | Settled historical minutes with no recorded row. |
| `open` | `null` | `null` | `[max(from, closed_until), to)`: unsettled right-edge tail, never a gap. |

Activity-unavailable history is never a timeline item. If the requested range intersects it, the
request is `422`. The detail names the first unavailable hour and states that canonical minutes
existed. Outside the range, unavailable evidence only appears as an `unavailable` boundary.

### `GET /api/v1/activity/live`

This is the current activity, classified by the same version-1 classifier from one `/api/v1/live`
observation. Only `mode="live"` values count as current evidence. Retained, stale, absent and
disconnected values are unknown inputs; the result is never guessed from them. No database is
read, and no live activity is stored. This resource describes the present moment; it is never the
last closed history minute.

| Field | Meaning |
|---|---|
| `now` | The observation instant. |
| `rule_version` | Classifier version (`1`). |
| `activity`, `compressor` | The classified state. |
| `all_inputs_live` | Every classifier input was a live value. |
| `mqtt` | `{connected, alive, epoch}` from the same observation. |
| `inputs` | For each of `compressor_freq`, `defrosting_state`, `heatpump_state`, `three_way_valve` and the four power channels: `{value, mode, received_at, used}`. `used` is true only for a live value. |

## Stage 4D report (frozen in 4D-A; implemented in 4D-C)

`GET /api/v1/report` is one backend-owned domain resource. This section is the implementation
contract, not a claim that the route is live at checkpoint 4D-A. The endpoint needs MariaDB but
not MQTT. It leaves the public `/history`, `/activity`, `/activity/live`, `/live`, `/metrics` and
`/status` responses unchanged.

| Form | Resolved period |
|---|---|
| `?period=day&date=YYYY-MM-DD` | That Warsaw local day. |
| `?period=week&date=YYYY-MM-DD` | ISO Monday–Sunday Warsaw week containing the anchor date. |
| `?period=month&date=YYYY-MM-DD` | Warsaw calendar month containing the anchor date. |
| `?period=custom&from=YYYY-MM-DD&to=YYYY-MM-DD` | Local `from` inclusive, local `to` exclusive; at most 31 local days. |

Use exactly the parameters of one form, each once. A date is a valid calendar date in the exact
`YYYY-MM-DD` form, never a timestamp or time of day. `to > from` is required. Other period values,
missing/extra/repeated parameters and custom spans over 31 local days are 400. Arbitrary instant
ranges remain `/history` and `/activity`. Calendar midnight is converted through
`Europe/Warsaw`, yielding UTC-hour-aligned boundaries. Day may be 23/24/25 hours, ISO week
167/168/169 hours, and month 672/696/720/743/744/745 hours. Day has one bucket per local hour
(`bucket: "1h"`, so the repeated autumn hour has two distinct UTC buckets); week, month and
custom have one bucket per local day (`bucket: "1d"`). All calendar buckets are emitted.
Supported report calendar dates are `1970-01-01` through `2100-12-31`. A valid date outside that
range, a resolved period beginning before Warsaw midnight `1970-01-01`, or one ending after Warsaw
midnight `2101-01-01` is unrepresentable (422), while malformed date syntax is 400. The exclusive
end of a supported final day or month may be `2101-01-01`.

The response shape is:

```text
report {
  period: {kind, from_date, to_date, from, to, timezone, bucket},
  observation: {now, closed_until, effective_to},
  segment_rule_version,
  evidence: {from, to},
  totals: FACTS + {compressor_runs_overlapping, defrosts_overlapping},
  buckets: [{start, end, FACTS}, ...]
}
FACTS = {coverage, energy, activity, events, technical}
```

`period.kind` is `day|week|month|custom`; `from_date` and `to_date` are the resolved local dates,
the latter exclusive; `from` and `to` are UTC `Z` instants at those midnights; `timezone` is
`Europe/Warsaw`. `observation.now` is one sampled UTC instant, `closed_until` is the Stage 4C
settled frontier, and `effective_to = min(period.to, max(period.from, closed_until))`. Thus
`period.from <= effective_to <= period.to`: it equals `period.to` when the frontier reaches or
passes `period.to`, `closed_until` when `period.from < closed_until < period.to`, and `period.from`
when `closed_until <= period.from` (including a fully future report). The clamp changes only the
settled extraction endpoint; it does not change the Stage 4C frontier or the coverage partition
below. `segment_rule_version` is the Stage 4C rule version read, currently 1.
`evidence.from/to` are the UTC extent actually examined for activity, including span widening;
both are `null` when no settled activity window needs
examination. Bucket `start/end` are UTC `Z` instants, ordered and half-open. A day with the
repeated autumn hour has two buckets with different UTC starts. A future bucket still has the
same FACTS shape and factual zero counts, with unknown measurements as `null`.

**Coverage.** In each FACTS, `coverage` is:

```text
{calendar_minutes, settled_minutes, unsettled_minutes, future_minutes,
 recorded_minutes, gap_minutes, coverage_percent}
```

For bucket `[a,b)`, set `C = closed_until` and `F = floor_minute(now)`:
`settled_minutes = |[a,b) ∩ (-∞,C)|`, `unsettled_minutes = |[a,b) ∩ [C,F)|`, and
`future_minutes = |[a,b) ∩ [F,+∞)|`. Their sum is `calendar_minutes`.
`recorded_minutes` counts canonical rows only in the settled part;
`gap_minutes = settled_minutes - recorded_minutes`.
`coverage_percent = round(100 * recorded_minutes / settled_minutes, 1)`, or `null` when settled
minutes are zero. Totals use the whole period under the same equations. The current partial
minute is future; a waiting or protected unacknowledged tail is unsettled, never a gap. No
readiness/quality status or threshold is returned.
For a period entirely after `floor_minute(now)`, `settled_minutes=0`, `unsettled_minutes=0`,
`future_minutes=calendar_minutes` and `coverage_percent=null`.

**Energy.** Each FACTS has `energy = {channels, consumption, production, cop}`. `channels`
contains exactly `co_power_consumption`, `co_power_production`, `dhw_power_consumption` and
`dhw_power_production`, each with `{kwh, minutes, unknown_minutes}`. For that FACTS' recorded
minutes, `minutes` counts known channel readings, `unknown_minutes = recorded_minutes - minutes`,
and `kwh = Σ known minute-average W / 60000` or `null` when no minute is known. A known zero is
counted as known and has 0 kWh; `NULL` is unknown; a no-row minute is counted only in coverage.

`consumption` and `production` each have `{observed_kwh, unknown_channel_minutes}`. Consumption
uses both CO and DHW consumption channels; production uses both production channels.
`observed_kwh` sums actually observed energy from those channels and is `null` only if neither
has a known minute. `unknown_channel_minutes` is the sum of their two `unknown_minutes` values,
in `0..2*recorded_minutes`. Zero means complete across both channels over recorded minutes.
A positive count does not make observed energy false or complete. There is no `total_kwh` claim.
The `pair_*` series are used for COP only, never for ordinary energy knownness.

`cop` contains exactly `co`, `dhw`, `total`, each
`{cop, paired_minutes, input_kwh, output_kwh}`. CO pairs its two power channels, DHW pairs its
two, and total requires all four in each contributing minute. Ingredients are paired kWh sums;
they are `null` when there is no paired minute. `cop = Σ paired output / Σ paired input`, `null`
with no pairs or a zero denominator. Known zero is valid paired evidence. No average of
instantaneous COP or ratio of unpaired energy totals is permitted.

**Activity.** Each FACTS has:

```text
activity: {
  compressor_minutes: {off, on, unknown},
  classes: {off, idle, co, dhw, transition, defrost, unknown},
  heating: {activities: ["co", "dhw", "transition"], minutes,
            channels, consumption, production, cop}
}
```

Every `classes[A]` value is `{minutes, channels, consumption, production, cop}`, with the same
energy and COP field meanings as `energy`; its unknown-minute denominator is that class's
recorded `minutes`. All seven classes sum to `coverage.recorded_minutes`; all class channel
known/unknown counts and aggregate unknown-channel counts sum to the corresponding top-level
energy counts. `compressor_minutes` similarly partitions recorded minutes. `heating` uses only
the three listed classes. Its `production.observed_kwh` is useful heat; its headline heating COP
is `heating.cop.total`, paired over all four channels within heating segments. Other classes,
including defrost and unknown, remain separately visible. The frontend never reclassifies them.

**Events.** Each FACTS has:

```text
events: {
  observed_starts, observed_stops,
  complete_runs: {count, total_minutes, min_minutes, max_minutes, mean_minutes},
  exact_off_intervals: {count, total_minutes, min_minutes, max_minutes, mean_minutes},
  defrost_events,
  activity_events: {off, idle, co, dhw, transition, defrost, unknown},
  observed_defrost_seconds
}
```

`min_minutes`, `max_minutes` and `mean_minutes` are `null` when the corresponding count is zero.
The report carries no `complete_runs.minutes[]` list; individual runs remain available in
`/activity`. Observed starts are attributed to the bucket holding the run's first minute;
observed stops to its last minute. Complete runs and exact off intervals are attributed
at their first minute. `defrost_events` counts maximal observed Stage 4C defrost spans;
`activity_events[A]` counts maximal observed activity spans of class `A`. Each span is attributed
to the bucket containing its first observed minute, even if its Stage 4C left boundary is `gap`,
`unknown` or `outside_evidence`. An `observed` boundary proves a start; those other boundaries
do not. The generic event counts include all four cases without claiming a physical/domain
start, and a span crossing adjacent buckets counts only once. `observed_starts` and
`observed_stops` retain their strict Stage 4C observed-boundary requirements. No separate
observed-defrost-start fact is included. `observed_defrost_seconds` clips to each bucket. The
non-additive `compressor_runs_overlapping` and `defrosts_overlapping` are top-level `totals`
fields only.

**Technical.** Each FACTS has `technical.outside_temp = {avg, min, max, minutes}` and
`technical.compressor_freq = {avg, max, minutes, active_avg}`. Missing numerical values are
`null`, with `minutes=0`. `active_avg` is the compressor-frequency sum divided by
`activity.compressor_minutes.on`, or `null` when on minutes are zero. This is allowed only under
the Stage 4C identity that on means known frequency `>0`, off means known frequency `0`, and
unknown frequency means compressor unknown. The read model checks that frequency known minutes
equal on + off minutes for the same range/bucket and returns 500 on inconsistency. No P95 or
non-durable active minimum is reported; detailed temperatures and pump facts stay in `/history`.

**Whole-resource refusal.** A settled report range intersecting Stage 4C activity-unavailable
history returns 422. It never returns energy with `activity=null` in the same report; numeric
history remains independently queryable. Corrupt durable activity or a per-hour mismatch between
history and activity recorded-minute counts in one DB snapshot returns 500. No raw fallback hides
corruption. The only report-specific errors are:

| Status | Cause |
|---|---|
| `400` | Malformed period/date/custom parameters, any time component, `to <= from`, or custom span over 31 local days. |
| `422` | A valid calendar date outside the supported 1970–2100 report range, a resolved period extending beyond it, activity-unavailable history, or a pathological purged current edge that cannot be represented. |
| `500` | Invalid/corrupt durable activity, history/activity recorded-count inconsistency, or the technical compressor-frequency identity failing. |
| `503` | Database unavailable. |

Error bodies use `{"detail": "…"}`. The report reads one MariaDB consistent snapshot after
sampling the settled frontier, with no DB I/O under the recorder lock. The unrolled current hour
may be read from raw below that frontier under a report-local edge rule. Existing `/history` and
`/activity` behavior remains byte-identical through the Stage 4D-C extraction refactor.
