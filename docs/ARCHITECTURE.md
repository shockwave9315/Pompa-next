# Pompa Next architecture

This document is the architectural source of truth for Pompa Next. It specifies the target system without requiring knowledge of the legacy implementation.

## 1. System overview

Pompa Next is a private, read-mostly monitoring application for a Panasonic Aquarea heat pump connected through HeishaMon.

```text
HeishaMon → MQTT broker → ingest → canonical minute recorder
                                    ↓
                               MariaDB
                                    ↓
                         aggregation and API
                                    ↓
                            React frontend
```

MQTT is the only primary ingest path. There is no HTTP bootstrap, HTTP fallback, legacy database import, or historical backfill.
The first deployment uses one Python 3.12 backend process. It serves FastAPI, owns one paho-mqtt client, records minutes through PyMySQL into MariaDB 11.4, and later serves a React/Vite frontend through Docker Compose deployment.
Pompa Next has no source, API, schema, or data compatibility requirement with legacy. Historical data begins when Pompa Next starts recording.

## 2. Module boundaries

The intended backend boundaries are conceptual, not a mandate for speculative abstractions:

| Boundary | Responsibility |
|---|---|
| Configuration | Parse environment settings and reject invalid startup configuration. |
| Metric catalog | Define metric identity, sources, units, kind, sentinels, valid range, and recording flag. |
| MQTT adapter | Connect, subscribe, reconnect, and forward message facts to ingest. |
| Ingest | Parse values, track per-source epochs/freshness, select sources, and maintain live state. |
| Minute accumulator | Turn timestamped source and metric changes into canonical `MinuteRow` values. |
| Recorder | Close minutes, buffer writes, flush storage, roll up closed hours, and purge safely. |
| Storage | Own DDL and parameterized MariaDB queries; contain no domain calculations. |
| Aggregation | Own `Stats`, buckets, derived series, energy, COP, coverage, and read-path composition. |
| API | Validate requests and serialize domain results; contain no independent mathematics. |

Dependencies flow inward toward the catalog and domain functions. Circular dependencies are not allowed. Live and history share parsing and catalog definitions, not a generic storage abstraction.
Backend code owns all domain semantics. Frontend code selects views and renders returned facts. It must not calculate energy, COP, state, alignment, buckets, or coverage.

## 3. Metric catalog

There is exactly one catalog entry per canonical metric. An entry contains:

- Stable `key`, Polish presentation label, unit, and group.
- `kind`: `mean` or `last`.
- Ordered physical MQTT sources.
- Per-source or per-metric sentinel values and valid range.
- A `record` flag controlling inclusion in `sample_1m`.

`mean` is used for continuous values and numeric flags: temperatures, flow, power, compressor frequency, pump speed, heat-pump state, defrost state, and three-way-valve state. Its minute value is time-weighted.
`last` is used for enums and counters such as operating mode, operations counter, and operating hours. Its minute value is the valid value at the end of the minute.
Core recorded metrics include inlet, outlet, target, tank, and outside temperatures; water pressure and flow; pump, compressor, and fan data; four CO/DHW input/output power channels; heat-pump, defrost, and valve flags; operating mode; operations counter; and operating hours.

Power source priority is XTOP first with TOP fallback:

- CO consumption: XTOP0, then TOP16.
- CO production: XTOP3, then TOP15.
- DHW consumption: XTOP2, then TOP41.
- DHW production: XTOP5, then TOP40.

Sentinels are catalog data, never global guesses. Known examples include `-78` and `-128` for temperatures and `-200` for TOP power. A payload of `0` is always a real zero when it passes the catalog's validity rules.
Adding a recorded metric may add a nullable `sample_1m` column. This explicit schema change is preferable to duplicate catalogs or an EAV history model.

## 4. MQTT ingest and source freshness

The client subscribes to `{MQTT_TOPIC_PREFIX}/#` and uses a client ID distinct from legacy during parallel operation.

For each message, ingest:

1. Records connection and LWT facts.
2. Ignores topics absent from the catalog.
3. Parses numeric payloads.
4. Maps sentinels, non-numeric payloads, and out-of-range values to unknown and increments `parse_rejects` where applicable.
5. Updates per-physical-source state.
6. Resolves canonical values separately for live display and historical accumulation.

`Offline` LWT and MQTT disconnect immediately end source-alive continuity and invalidate values. LWT `Online` is useful evidence but does not make retained metric payloads historical evidence.

Source life at time `t` is:

```text
alive(t) = mqtt_connected
           and lwt != Offline
           and last_live_message_at exists
           and t - last_live_message_at <= STALE_AFTER
```

`last_live_message_at` advances only for non-retained messages. `alive_since` marks the beginning of the current uninterrupted alive interval and resets on disconnect, LWT `Offline`, or detected staleness.
`STALE_AFTER_SECONDS=600` is the accepted Stage 1 global freshness policy, decided from a real-runtime measurement on CT109 (22.768 h uninterrupted, owner-accepted short of the originally planned ≥24 h). No non-retained publication gap exceeded 305.1 s across all relevant TOP/XTOP topics despite materially different mean publication rhythms, so a single global timeout is used; per-metric or per-source freshness was not justified by the evidence. The policy is revisited only if future real-runtime evidence shows normal gaps approaching or exceeding 600 s.
A value remains valid from receipt until its replacement, its source goes offline, or freshness expires. Holding a report-by-exception value inside that bounded interval is protocol semantics, not interpolation.

## 5. Retained and `seen_live` rules

Each physical source/topic tracks `seen_live` for the current process and MQTT connection epoch.

- `seen_live` starts false at process start.
- Every successful MQTT connection begins a new epoch and resets it to false for all sources.
- A retained message may update live state and is labeled `retained: true`.
- A retained message never sets `seen_live`, advances `last_live_message_at`, proves source life, or enters historical accumulation.
- The first non-retained message from that physical source sets `seen_live=true` for the epoch.
- Reconnect requires new non-retained evidence; previously confirmed sources do not remain confirmed.

Historical source selection chooses the first catalog source that is valid, `seen_live`, and fresh. Therefore fresh TOP16 may supply CO consumption while higher-priority XTOP0 is retained or unconfirmed. Once valid live XTOP0 evidence arrives, priority returns to XTOP0.
Live selection uses confirmed fresh sources first. If none exists, it may expose a retained value with explicit retained metadata. That exception never affects history.
No historical row or metric completeness may be established exclusively from retained messages.

## 6. Canonical `MinuteRow`

A minute is the half-open UTC interval `[M, M+60)`, with `M` aligned to 60 seconds.
`MinuteRow` contains `ts=M` and one nullable value for every recorded catalog metric.

A row exists if and only if the source was observed alive for the entire minute:

```text
alive_since != null
and alive_since <= M
and M + 60 - last_live_message_at <= STALE_AFTER
```

An offline or disconnect event anywhere in the minute means no row. A source returning during a minute can first produce a row for the next full minute. Expiry of freshness during a minute also means no row, even if discovered at close time.
The accumulator never creates a minute beginning before `process_start`. Its first advance after startup cannot infer earlier source life. Process downtime is always a gap and is never backfilled.
For a `mean` metric, the value is the time-weighted average of valid value segments only if the metric was valid for the complete minute; otherwise it is `NULL`.
For a `last` metric, the value is the valid value at `M+60`; otherwise it is `NULL`.

The three states are intentionally structural:

| State | Meaning |
|---|---|
| Numeric value, including `0` | The metric was known for the required interval. |
| `NULL` in an existing row | The source minute was recorded but this metric was unknown. |
| No row | The minute was not recorded because full-minute source life was not observed. |

There is no zero-fill, interpolation, extrapolation, or backfill.

## 7. `sample_1m` schema

`sample_1m` is a wide InnoDB table. One row represents one canonical minute; one nullable `DOUBLE` column represents each recorded metric.

```sql
CREATE TABLE sample_1m (
  ts                     INT UNSIGNED NOT NULL PRIMARY KEY,
  main_outlet_temp       DOUBLE NULL,
  main_inlet_temp        DOUBLE NULL,
  main_target_temp       DOUBLE NULL,
  dhw_tank_temp          DOUBLE NULL,
  outside_temp           DOUBLE NULL,
  water_pressure         DOUBLE NULL,
  pump_flow              DOUBLE NULL,
  pump_speed             DOUBLE NULL,
  compressor_freq        DOUBLE NULL,
  compressor_current     DOUBLE NULL,
  fan1_speed             DOUBLE NULL,
  co_power_consumption   DOUBLE NULL,
  co_power_production    DOUBLE NULL,
  dhw_power_consumption  DOUBLE NULL,
  dhw_power_production   DOUBLE NULL,
  heatpump_state         DOUBLE NULL,
  defrosting_state       DOUBLE NULL,
  three_way_valve        DOUBLE NULL,
  operating_mode         DOUBLE NULL,
  operations_counter     DOUBLE NULL,
  operations_hours       DOUBLE NULL
) ENGINE=InnoDB;
```

`ts` is a UTC Unix second and must satisfy `ts % 60 = 0`. Epoch integers avoid database timezone ambiguity. `DOUBLE` avoids dialect-specific decimal conversion. Writes upsert by `ts` and all SQL values are parameterized.
The wide form preserves the row/`NULL` distinction and permits paired COP channels to be evaluated within one row.

## 8. `rollup_1h` schema

Long history uses one narrow row per UTC hour and series:

```sql
CREATE TABLE rollup_1h (
  hour_ts  INT UNSIGNED      NOT NULL,
  series   VARCHAR(40)       NOT NULL,
  n        SMALLINT UNSIGNED NOT NULL,
  v_sum    DOUBLE            NOT NULL,
  v_min    DOUBLE            NOT NULL,
  v_max    DOUBLE            NOT NULL,
  v_last   DOUBLE            NOT NULL,
  PRIMARY KEY (hour_ts, series)
) ENGINE=InnoDB;
```

A row exists only when the series has at least one non-`NULL` minute in that hour. `v_last` is retained for every series and preserves end-of-bucket semantics for `kind=last`.
There is no 5-minute table and no watermark table. `rolled_until` is derived as `MAX(hour_ts) + 3600` over the contiguous rolled range.
Each closed hour is rebuilt idempotently in one transaction: delete its rollup rows, fold its minutes in timestamp order, then insert the result. Hours are rolled in ascending order starting at `rolled_until`; processing stops at the first failed hour, preserving a contiguous range.

A minute can still arrive after its hour was rolled: a protected batch retried through an outage longer than the rolled hour, a minute closed while its hour was being rolled, or a clock stepped back across a restart. No time window makes that safe, so minute persistence is the repair point: one transaction upserts the batch and rebuilds every touched hour below `rolled_until`. Raw minute and corrected rollup therefore commit together or not at all, a lost acknowledgement leaves both committed, and the idempotent retry reproduces the same state. Hours at or above `rolled_until` need no repair because they are not rolled yet.

A rolled hour whose raw evidence has already been purged is never rebuilt from newly arriving partial raw data: the write is refused, before anything is upserted (a wall clock stepped back across a restart by more than raw retention).

### Purged raw evidence

One database fact decides, everywhere, whether raw minutes were physically deleted:

```text
purged(H) = a rollup_1h row exists for UTC hour H
            and no sample_1m row exists in [H, H+1h)
```

It is exact, not heuristic. A `rollup_1h` row proves the hour once held raw minutes, because an hour with none is never given one. Purge deletes only whole hours and only after proving that hour's rollup complete (§13), so a deleted hour always leaves its rollup row behind as evidence. An hour with neither raw minutes nor a rollup row is therefore not evidence of purge at all — it was simply never recorded, and stays truthfully empty.

This fact is the single source of truth for late-write refusal, raw representability, `auto` promotion, and the recorder's refusal semantics. No watermark, scalar, or third table records it; `rollup_1h` already does.
The purge cutoff (§13) is the opposite kind of value: it is the prospective policy of what purge *may* delete next, it follows the wall clock and the configured retention, and it may move backwards when either does. It never decides representability.

## 9. Aggregation algebra

All stored metrics and derived series use:

```text
Stats = (n, sum, min, max, last)
Stats.of(v) = (1, v, v, v, v)
combine(a, b) = (
  a.n + b.n,
  a.sum + b.sum,
  min(a.min, b.min),
  max(a.max, b.max),
  b.last
)
avg = sum / n
```

`combine` is associative but not commutative because `last` is time-sensitive. Every fold reads and combines minutes or hours in ascending time order. `NULL` contributes nothing and does not increment `n`.
The same fold builds 5-minute, hourly, local-day, and total buckets. Rollup is an exact acceleration of minute folding, not a separate calculation. Within raw retention, raw-minute and mixed rollup read paths must return equivalent results.

## 10. Derived series

A single pure derivation function expands each `MinuteRow` before folding:

- `recorded = 1` for each existing row.
- `pair_co_in` and `pair_co_out` exist only when both CO power channels are known in the minute.
- `pair_dhw_in` and `pair_dhw_out` exist only when both DHW power channels are known.
- `pair_total_in` and `pair_total_out` exist only when all four power channels are known; their values are the respective CO+DHW sums.

The identical derivation runs when building `rollup_1h` and when querying raw minutes.
Flags remain measurable facts. For a 0/1 mean flag, `sum` is the number of minutes in state 1. Operational activity classification is not part of the core history engine.

## 11. Energy and COP

Energy for a power series is calculated over valid minute-average watt values:

```text
energy_kwh = Stats.sum / 60000
```

Every energy value is accompanied by `minutes = Stats.n`. Missing minutes are never filled or extrapolated.
Period COP is calculated only from paired minutes:

```text
COP = Σ paired_out_W / Σ paired_in_W
```

The API supports CO, DHW, and total COP. Total COP requires all four channels in each contributing minute. `paired_minutes` accompanies every COP result and is equal for paired input and output by construction.
COP is `null` when there are no paired minutes or paired input sum is zero. A paired 18 W input and 0 W output minute is valid and lowers period COP. A 0 W/0 W minute is paired but does not make a nonzero denominator.
Instantaneous COP uses the same ratio for one minute. Instantaneous COP values are never averaged to obtain period COP.

## 12. Coverage

Each bucket reports only measurable facts:

```text
expected_minutes = elapsed minutes belonging to the bucket
recorded_minutes = Stats(recorded).n
coverage_percent = round(100 * recorded_minutes / expected_minutes, 1)
```

Coverage is `null` when `expected_minutes=0`. Expected minutes are not trimmed to recorder start; starting halfway through a period truthfully shows partial coverage.
Each ordinary series reports `minutes`. Each COP series reports `paired_minutes`. There are no completeness thresholds or labels such as `ready`, `partial`, `unavailable`, `exact`, or `usable`. The frontend must not invent them.

## 13. Retention and purge

`sample_1m` default retention is 365 days through `RETENTION_1M_DAYS=365`; zero disables purge. `rollup_1h` retention is indefinite. A 5-minute representation is computed from raw minutes and is retained only as long as they are.

Purge runs hourly in bounded batches and is fail-closed:

```text
cutoff = min(
  floor_hour(now - RETENTION_1M_DAYS days),
  rolled_until - 2 hours,
  floor_hour(oldest minute still waiting to be written)
)
delete sample_1m where ts < cutoff
```

If no contiguous rollup exists, `rolled_until` is absent and purge deletes nothing. Purge cannot delete a minute from an unrolled hour, from the two-hour margin below `rolled_until`, or from an hour a pending write can still enter and force a rebuild of.
Each bounded step additionally proves per hour that the rollup accounts for exactly as many minutes as the hour still stores; any mismatch, missing rollup row or error deletes nothing. That proof is what makes a surviving rollup row conclusive evidence of deletion (§8), which is how reads learn what purge removed; the `cutoff` itself is prospective policy and is never used to answer that question.
Activity/event timelines and minute-order cycle reconstruction are guaranteed only while raw `sample_1m` exists. Hourly flags preserve duration but not order.
Compressor-start reconstruction from positive `operations_counter` steps across resets is likewise guaranteed only in the raw 1-minute retention window. A future feature requiring indefinite starts must explicitly add a persisted derived series; the core does not anticipate it.

## 14. Query resolution and read paths

Every query uses an exact half-open interval `[from,to)`.

| Requested bucket | Read source |
|---|---|
| `1m`, `5m` | `sample_1m`; reject if a required hour's raw evidence was purged. |
| `1h`, `1d`, `total` | `rollup_1h` for complete rolled UTC hours, plus `sample_1m` for unrolled data and partial edge hours. |

There is no data-dependent resolver or fallback chain. Missing raw minutes remain missing in every aggregate.

`bucket=auto` is selected from range length alone:

| Range | Bucket |
|---|---|
| Up to 36 hours | `1m` |
| Up to 10 days | `5m` |
| Up to 120 days | `1h` |
| Longer | `1d` |

If an automatically chosen 1m/5m range contains a purged hour, auto promotes it to 1h. Responses are limited to 3000 buckets.

## 15. Partial-hour old-range rule

Non-hour-aligned `from` or `to` edges require `sample_1m` for the affected UTC hour. If that hour is purged (§8), the exact interval cannot be reconstructed from its complete `rollup_1h` row.
The API returns HTTP 422 in that case. It never silently rounds, widens, narrows, or truncates the requested range.
422 means the backend knows the minutes existed and were deleted. Age alone never causes it: a range that was never recorded — no raw minutes and no rollup row — is answered with `recorded_minutes = 0`, because that is exactly what "no row" means (§6). A range predating the recorder is answered the same way.
Local calendar-date ranges remain representable from hourly rollups because Europe/Warsaw day boundaries align to whole UTC hours. Startup validates this assumption for the configured timezone.

## 16. Timezone and DST

Storage and sub-day bucket alignment use UTC. API timestamps are ISO 8601 UTC with `Z`.
Presentation timezone defaults to `Europe/Warsaw`. `1d` means a calendar day in that timezone, converted to UTC boundaries. A spring DST day contains 1380 expected minutes and an autumn DST day contains 1500. Daily buckets must remain contiguous and non-overlapping through both transitions.
Inputs accept ISO timestamps with an explicit offset and local `YYYY-MM-DD` calendar dates. Dates mean local midnight. Naive timestamps without defined timezone semantics are rejected.

## 17. API contract

Pompa Next starts a fresh namespace at `/api/v1`; this is not inherited legacy versioning.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/history` | All historical charts and summaries. |
| `GET /api/v1/live` | Current in-memory source and metric state. |
| `GET /api/v1/metrics` | Frontend-safe metric and COP catalog. |
| `GET /api/v1/status` | Factual MQTT, recorder, and storage status. |
| `GET /health` | Process liveness for deployment. |

Stage 1 implements the 1-minute subset of history plus status and health. Stage 2 extends the same history contract with `auto|1m|5m|1h|1d|total`, derived energy and COP series; live and metrics follow before the frontend contract is frozen.
Responses are limited to 3000 buckets and name both the requested and the resolved bucket. History series are the recorded metrics plus `cop_co`, `cop_dhw` and `cop_total`.

History accepts `from`, `to`, `bucket`, and a series list. Response buckets contain start, end, expected minutes, recorded minutes, and coverage percent. Series arrays align exactly with bucket arrays and use `null` for absent values.

Mean metrics expose average, minimum, maximum, and minutes. Last metrics expose last, minimum, maximum, and minutes. Power metrics additionally expose kWh. COP exposes the ratio, paired minutes, input kWh, and output kWh.

Period summary is `bucket=total`; daily reporting is `bucket=1d`. Separate daily, period, or report calculation endpoints are forbidden because they would duplicate aggregation semantics.

Bad parameters return 400. Unrepresentable retained-history resolution or old partial-hour edges return 422. Database unavailability returns 503 for history while live may remain available.

Status returns facts, not health verdicts: MQTT connection/LWT/alive/last-message and parse rejects; recorder last minute, buffer size, drops, refused rows and the last refusal, configured retention, and the last rollup and purge outcomes; database availability, rolled boundary, prospective purge cutoff, and oldest/newest raw minute.

## 18. Live versus history

Live state is in memory and reports the most recent valid metric value, receive time, and source metadata. It can show an explicitly marked retained value when no confirmed live source is available.

History reads only canonical persisted minutes and hourly rollups. It never reads live memory to complete a minute and never treats retained delivery as evidence.

Live state does not survive restart. Historical state does. The current open minute is not history until it closes successfully.

## 19. Failure handling and write buffer

- paho reconnects with bounded backoff. Disconnect immediately makes the source not alive.
- Invalid payloads become unknown for that source/metric and increment a factual reject counter; callbacks do not crash.
- If MariaDB is unavailable at startup, the process and live path start while schema bootstrap retries on recorder ticks.
- Closed `MinuteRow` values enter an in-memory FIFO waiting queue of at most 60 rows. Every tick retries idempotent upserts.
- A storage write has three distinguishable outcomes. A success commits the batch. An *ambiguous* failure — the database was unreachable or the acknowledgement was lost — may or may not have committed, so the batch stays protected, is retried unchanged, and is never dropped. A *definite refusal* (§8) is raised before anything is upserted, so nothing was written and nothing ever can be: exactly the rows of the refused hours are dropped and counted as `refused_rows`, and the rest of the batch is written immediately. A permanently unwritable row therefore never blocks the queue, the fresh minutes behind it, or rollup and purge.
- The protected batch holds at most 60 rows in addition to the waiting queue.
- On waiting-queue overflow, the oldest never-submitted row is dropped, `dropped_rows` increments, and the resulting gap remains visible.
- Rollup or purge errors are logged and retried on a later tick. Purge safety still derives from contiguous rollup state.
- Clock reversal cannot duplicate primary keys because minute writes upsert by `ts`.
- Deployment enforces one worker. Idempotent writes reduce damage from accidental duplication but are not a multi-writer design.

A protected batch retried through a long outage can be older than any fixed reprocessing window when it is finally written. Correctness comes from the write transaction rebuilding the rolled hours it touches (§8) and from the purge cutoff never dropping below a pending minute's hour (§13), not from the size of a window.

## 20. Control boundary

Control/SET is later, independent work. A future control module may publish only allowlisted command topics, validate ranges, and write a separate audit record.

Recorder, storage, history, and aggregation have no dependency on control. Command results return through ordinary observed TOP topics. The recorder itself never publishes MQTT commands.

The 193-capability explorer is reference knowledge for that later stage, not a core recorder dependency.

## 21. Testing strategy

Tests use an injected clock and do not sleep. Expected results are either hand-calculated constants or equality between independent raw and rollup paths.

Required ingest cases include real zero versus sentinels, invalid payloads, XTOP/TOP priority, time-weighted means, partial-minute unknowns, freshness expiry, full-minute life, LWT/disconnect transitions, reconnect epochs, all retained-message cases, process start boundaries, isolated missing topics, and reference-data parsing.

Required aggregation cases include associativity, ordered `last`, trailing `NULL`, raw/rollup equivalence, energy from incomplete minutes, paired COP rather than averaged COP, zero denominators, empty buckets, and both Europe/Warsaw DST transitions.

Required storage/recorder cases include idempotent upsert and rollup, fail-closed purge, buffered database outages, overflow counters, delayed minutes entering reprocessed hours, exact partial edges, and HTTP 422 for purged raw — proven to survive a backward clock step and a raised or disabled retention, and proven not to fire for a range that was never recorded. The three write outcomes are covered separately: an ambiguous failure retried unchanged, and a definite refusal that drops only its own rows.

A slice test covers fake MQTT messages through minute closure, storage, and `GET /api/v1/history`. MariaDB runtime smoke runs beside legacy with separate client identity, database, and port. Browser/dev-server validation is used only when a frontend task requires it.

## 22. Complexity budget

The target core has one metric catalog, two stored history tables, one aggregation implementation, and one history endpoint. There is no stored 5-minute tier, watermark table, user-configurable recording policy, data-dependent resolver, domain math in API/frontend code, or status-threshold system.

New layers require a concrete present need. Avoid generic repositories, provider hierarchies, speculative interfaces, duplicated view models, and compatibility adapters. Prefer explicit functions and data structures until multiple real implementations justify abstraction.

Substantive stages deliver a complete vertical outcome and use a feature branch plus DRAFT PR. Project history remains in Git and PR bodies, not chronology documents.

## 23. Final invariants

1. A `sample_1m` row exists exactly when source life was observed for the full minute.
2. `0` is data, `NULL` is an unknown metric in a recorded minute, and no row is an unrecorded minute.
3. No path zero-fills, interpolates, extrapolates, backfills, or creates pre-process minutes.
4. Retained messages are never historical evidence; every physical source needs non-retained evidence in the current process/connection epoch.
5. Historical priority uses the first valid, `seen_live`, fresh source, so confirmed TOP may beat unconfirmed XTOP.
6. The 600-second freshness value is the accepted Stage 1 policy, decided from a 22.768 h real-runtime measurement (max observed gap 305.1 s, no gap over 600 s).
7. `rollup_1h` is exactly the time-ordered fold of its raw minutes, including `last`.
8. A rolled hour with a `rollup_1h` row and no `sample_1m` row is the one fact proving its raw evidence was purged; an hour with neither was never recorded. Every path uses it. A persisted minute below `rolled_until` is rebuilt into its rollup in the same transaction, unless its hour is purged — then the write is refused before anything is upserted, its rows are dropped and counted, and the rest of the batch still commits. Purge cannot delete an unrolled minute, the two-hour margin, a pending write's hour, or any hour whose rollup it cannot prove complete.
9. Energy is `ΣW/60000`; period COP is `Σout/Σin` over paired minutes and is never an average of COP values.
10. Coverage is expressed only as counts and percentages, without arbitrary completeness verdicts.
11. All query intervals are exact `[from,to)`; 422 without rounding means a needed hour's raw evidence was provably purged, never merely that the range is old or never recorded.
12. UTC is storage truth; Europe/Warsaw calendar days include correct 23/25-hour DST behavior.
13. Activity/timeline and reset-aware compressor starts are guaranteed only while raw 1-minute data remains.
14. Backend owns domain truth; frontend only renders backend facts.
15. Recorder/history remain independent of the later isolated control path.
16. Legacy history is not migrated or backfilled, and legacy compatibility is not a requirement.
