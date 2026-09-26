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
The deployed Stage 1–3 backend uses one Python 3.12 process: FastAPI, one paho-mqtt client, and
PyMySQL with MariaDB 11.4. Stage 4 completes the product backend before React/Vite frontend work.
This is an additive expansion of the proven recorder/history path, not a replacement for it.
Pompa Next has no source, API, schema, or data compatibility requirement with legacy. Historical data begins when Pompa Next starts recording.

## 2. Module boundaries

The intended backend boundaries are conceptual, not a mandate for speculative abstractions:

| Boundary | Responsibility |
|---|---|
| Configuration | Parse environment settings and reject invalid startup configuration. |
| Core metric catalog | Define the 21 canonical metric keys, sources, units, kind, sentinels, valid range, and recording flag. |
| MQTT adapter | Connect, subscribe, reconnect, and forward message facts to ingest. |
| Ingest | Parse values, track per-source epochs/freshness, select sources, and maintain live state. |
| Minute accumulator | Turn timestamped source and metric changes into canonical `MinuteRow` values. |
| Recorder | Close minutes, buffer writes, flush storage, roll up closed hours, and purge safely. |
| Storage | Own DDL and parameterized MariaDB queries; contain no domain calculations. |
| Aggregation | Own `Stats`, buckets, derived series, energy, COP, coverage, and read-path composition. |
| Activity | Interpret canonical minutes into activity, compressor runs and defrosts (Stage 4C, §25.3); pure. |
| Control | Validate and encode semantic commands, publish them once, observe readback (Stage 4E, §25.5). The pure domain never imports `Recorder`. The runtime reads the existing live-readings observation and never changes recorder, queue, history or storage state. |
| API | Validate requests and serialize domain results; contain no independent mathematics. |

Dependencies flow inward toward the catalog and domain functions. Circular dependencies are not allowed. Live and history share the proven canonical metric definitions, not a generic storage abstraction. Stage 4A adds a reference-backed capability layer around them (§25).
Backend code owns all domain semantics. Frontend code selects views and renders returned facts. It must not calculate energy, COP, state, alignment, buckets, or coverage.

## 3. Metric catalog

The current core has exactly one catalog entry per canonical metric. Its 21 entries remain the
authority for Stage 1–3 behavior during Stage 4A. An entry contains:

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
The current `sample_1m` schema is the fixed canonical core. Stage 4A does not change it. A future
core metric change may still need an explicit nullable column, but user-selectable additional
history must not require one schema migration per selected capability. Stage 4B decides its
physical storage and participation representation (§25); no optional-history DDL is frozen here.

## 4. MQTT ingest and source freshness

The client subscribes to `{MQTT_TOPIC_PREFIX}/#` and uses a client ID distinct from legacy during parallel operation.

For each message, ingest:

1. Records connection and LWT facts.
2. Ignores topics absent from the current core catalog for canonical metric/minute processing.
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
Flags remain measurable facts. For a 0/1 mean flag, `sum` is the number of minutes in state 1. Operational activity classification is not part of the core history engine; it is the separate Stage 4C activity domain (§25.3.1), which reuses this derivation for its energy ingredients.

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
**Activity durability.** Hourly flags preserve duration but not order. Since Stage 4C-B, purge also
proves the hour's durable activity segments (§25.3.2), so activity order, compressor runs,
defrosts and their energy ingredients survive raw purge. The reset-aware interpretation of
`operations_counter` still has only the raw-minute window; the counter must not be assumed to equal
observed compressor starts. The default raw retention remains 365 days during early Stage 4.

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

Stages 1–3 implemented and froze the current default contract: history with
`auto|1m|5m|1h|1d|total`, derived energy and COP series, live, metrics, status and health. Stage 4A
may add opt-in capability/readings forms while preserving default response semantics (§25).
Responses are limited to 3000 buckets and name both the requested and the resolved bucket. History series are the recorded metrics plus `cop_co`, `cop_dhw` and `cop_total`.

History accepts `from`, `to`, `bucket`, and a series list. Response buckets contain start, end, expected minutes, recorded minutes, and coverage percent. Series arrays align exactly with bucket arrays and use `null` for absent values.

Mean metrics expose average, minimum, maximum, and minutes. Last metrics expose last, minimum, maximum, and minutes. Power metrics additionally expose kWh. COP exposes the ratio, paired minutes, input kWh, and output kWh.

The current period summary is `bucket=total`; current daily history is `bucket=1d`. Stage 4D
freezes one `/api/v1/report` projection resource for activity/event facts that numeric history
cannot express (§25.4). It reuses existing energy, COP and coverage algebra. Stage 4E freezes the
only command resources, `GET /api/v1/controls` and `POST /api/v1/controls/{key}` (§25.5.12).

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

All known SET identities enter the reference-backed capability model in Stage 4A as knowledge
only. Actual MQTT command publishing begins in Stage 4E through an isolated path that publishes
only backend-validated semantic commands. Stage 4E-A decided that there is no persistent command
history (§25.5.10).

Recorder, storage, history, and aggregation have no dependency on control. Command results return through ordinary observed TOP topics. The recorder itself never publishes MQTT commands.

Command results are factual and keep validation, publish, readback and physical execution apart
(§25.5.8). A missing or late readback does not stop unrelated reads, history or controls. There is
no automatic retry, rollback, global failure state, speculative rejection or recovery workflow.

## 21. Testing strategy

Tests use an injected clock and do not sleep. Expected results are either hand-calculated constants or equality between independent raw and rollup paths.

Required ingest cases include real zero versus sentinels, invalid payloads, XTOP/TOP priority, time-weighted means, partial-minute unknowns, freshness expiry, full-minute life, LWT/disconnect transitions, reconnect epochs, all retained-message cases, process start boundaries, isolated missing topics, and reference-data parsing.

Required aggregation cases include associativity, ordered `last`, trailing `NULL`, raw/rollup equivalence, energy from incomplete minutes, paired COP rather than averaged COP, zero denominators, empty buckets, and both Europe/Warsaw DST transitions.

Required storage/recorder cases include idempotent upsert and rollup, fail-closed purge, buffered database outages, overflow counters, delayed minutes entering reprocessed hours, exact partial edges, and HTTP 422 for purged raw — proven to survive a backward clock step and a raised or disabled retention, and proven not to fire for a range that was never recorded. The three write outcomes are covered separately: an ambiguous failure retried unchanged, and a definite refusal that drops only its own rows.

A slice test covers fake MQTT messages through minute closure, storage, and `GET /api/v1/history`. MariaDB runtime smoke runs beside legacy with separate client identity, database, and port. Browser/dev-server validation is used only when a frontend task requires it.

## 22. Complexity budget

The completed Stage 1–3 core has 21 metric definitions, two history tables, one aggregation
implementation and one history endpoint. It has no stored 5-minute tier, watermark table,
user-configurable recording policy, data-dependent resolver, frontend domain math or status
threshold system. Stage 4 adds only mechanisms justified by product needs, in their owning
checkpoints (§25). The effective capability catalog derives identities from references rather
than maintaining a second manual list of ~203 full entries.

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
13. Raw purge deletes an hour only after proving its durable Stage 4C activity segments equal
    the segments of its raw minutes (§25.3.2), so activity facts survive it. Reset-aware counter
    analysis is still guaranteed only while raw 1-minute data remains.
14. Backend owns domain truth; frontend only renders backend facts.
15. Recorder/history remain independent of the later isolated control path.
16. Legacy history is not migrated or backfilled, and legacy compatibility is not a requirement.

## 24. Operating trust boundary

Pompa Next runs in a private Proxmox LXC. The host, the container's kernel environment and its root
administrator are trusted. The system does not attempt to remain semantically correct under
deliberate administrator tampering with `CLOCK_REALTIME`, process memory, application files or
MariaDB contents — there is no defence against a `date -s` run specifically to break the recorder,
and none is required.

Ordinary operational failures remain fully supported and are not excused by the paragraph above:
process/container restart, a host restart, MQTT/database/network outages, and a real backward
`CLOCK_REALTIME` step from a normal NTP correction while the process keeps running (e.g. after a
container freeze/resume or a slow boot before the clock is disciplined) all remain in scope and must
degrade safely.

Freshness is measured only between two `CLOCK_REALTIME` readings taken on the same side of a
correction. Four rules implement that, and nothing else is needed:

1. Event *sequencing* keeps its own clamp: `Recorder._advance` never lets the accumulator's cursor
   regress, so a step produces a gap and never a duplicate primary key. The cursor is an ordering
   device and is never an operand of a duration.
2. A source's freshness anchor is the *raw*, unclamped receipt timestamp, and the observation `now`
   in `/api/v1/live` and `/api/v1/status` is the raw clock too. Clamping either to the cursor would
   pair a reading from one side of the correction with a reading from the other.
3. A backward step larger than `CLOCK_STEP_BACK_SECONDS` between two consecutive readings is
   detected, and every confirmed timestamp taken before it is discarded — the sources need new
   non-retained evidence, exactly as after a disconnect. `seen_live`, the connection epoch and
   retained provenance are untouched. The very next ordinary message re-establishes the source, so
   the cost is at most one publication interval of `mode="none"`, and a message that itself carries
   the step forward re-establishes its own source in the same call.
4. A detected backward step also invalidates any partially integrated open minute. Pre-correction
   integration is never combined with post-correction evidence in one `MinuteRow`; the affected
   minute is a gap.

A step therefore never makes a source look confirmed alive for longer than `STALE_AFTER_SECONDS` of
real elapsed time — in `/api/v1/live`, `/api/v1/status` or `sample_1m`. A step too small to move two
consecutive readings backwards is not detected and does not need to be: it can add at most its own
size to a freshness budget. A *forward* step needs no detection at all, because it only ages
evidence, which the ordinary freshness window already handles.

Time the process did not validly observe is a gap. It is never backfilled, interpolated or
reconstructed, regardless of cause — a container pause, a restart, or a restored older snapshot that
removes later persisted history all leave an ordinary, visible gap, exactly as invariant 3 already
requires. The recorder has no freeze detector and needs none, but the gap is not instantaneous: a
pause or freeze is indistinguishable from a silent source, so the last confirmed value still counts
as known for up to `STALE_AFTER_SECONDS` into the paused interval, and the visible gap begins after
that window, not at the first paused second.

## 25. Stage 4 product-backend expansion

**Current/frozen now:** Stages 1–3 and 4A–4D are complete and merged; Stage 4E is current. CT109
runs the validated Stage 4D runtime head `3af4d8fc0e17bafe17cc669f694bb2d3652a9ac4`.
Sections 3–24 specify the canonical 21-metric ingest, minute recorder, storage, aggregation and
default API. Stage 4 adds product capability around them; Stage 5 is frontend and Stage 6 is
cutover. Broad capability must use a lightweight implementation: one definition of each domain
fact, catalog metadata
instead of repeated code, pure derivations where practical, and domain resources rather than
frontend-page endpoints. Ordinary heat-pump changes may be `unknown` or `transition`; they are
data, not infrastructure incidents.

The intended product is a functional successor to useful legacy capabilities. Targeted legacy
inspection supplies product evidence under `docs/CONTEXT.md`, never implementation authority or a
compatibility requirement.

### 25.1 Stage 4A — reference-backed capabilities and full readable live state

**Frozen direction:** Parse the tracked `docs/reference/heishamon/MQTT-Topics.md` for TOP0–TOP143
(144), OPT0–OPT6 (7), and SET1–SET46 (46). Include the observed XTOP0–XTOP5 (6) from
`docs/reference/heishamon/realne_dane.md`. The documented reference establishes identities,
topics and descriptions; observed XTOP names are evidence, and topic paths not already verified by
the canonical core require validation. A strict deterministic parser produces baseline entries;
the existing `Metric`/`Source` definitions supply the 21 canonical metric semantics; small curated
verified overrides add product labels, types, enums, sentinels or SET/readback relations where
needed. The effective catalog must check identity/topic conflicts and cover every tracked entry.
Unknown metadata stays unknown. There is no manually maintained 203-entry catalog.

All six XTOP paths now have evidence: XTOP0/2/3/5 from canonical Stage 1 sources, and XTOP1/4
from exact received topics in owner-supplied pre-deployment CT109 `mqtt.uncatalogued_topics`.
Those last two are physical readings only; they add no canonical source or history series. Final
CT109 validation of deployed head `eae56fe46f4940aa4940ab2f08f87436cb54a117` confirmed all six
XTOP readings, unchanged default API and database schema, identical saved historical results, and
the expected unrecorded partial minute across restart.

The parser consumes a deliberately stable subset of the checked-in Markdown format. Tests must
reject missing, duplicated or malformed identity rows and protect the grammar against formatting
drift that would silently change behavior. The runtime image must package the tracked reference
from its authoritative source; Stage 4A checkpoint A decides the build wiring. Parsing may be
cached after startup. The current core catalog and its parsing, source priority, XTOP/TOP
fallback, sentinels, valid ranges, `mean`/`last` kinds and recording flags do not change.

`docs/reference/heishamon/MQTT-Topics.md` and `docs/reference/heishamon/realne_dane.md` remain
factual device evidence, but since Stage 4A they are also packaged runtime inputs: the backend
image ships them and parses them at startup to build the effective capability catalog. They are
not harmless documentation — an edit changes runtime behavior — and strict capability tests guard
their grammar and their content relationship (identity coverage, topic/name conflicts).

The public identity of an effective capability is `identity` (e.g. `XTOP0`, `TOP16`); there is no
second capability key. Its relationship to the canonical core, when any, is `canonical_metric` and
`source_priority` alone — a physical capability identity and a canonical logical series key are two
distinct namespaces and must not be conflated into one field.

All meaningful readable TOP/OPT/XTOP identities may exist in an in-memory live store. Minimum
facts are identity, value, receipt time, provenance/mode and availability. Absent optional-PCB
topics are simply absent readings; the reference states they may not appear with a real optional
PCB installed. Full live coverage does **not** add full process-lifetime gap statistics or other
diagnostic machinery for every physical topic. Keep detailed diagnostics for current canonical
sources and add others only for a concrete requirement. Stage 4A makes no database, history,
event, report, SET-publish or frontend change.

**Checkpoint D additive API contract:** Default `/api/v1/metrics` and `/api/v1/live` retain their
Stage 3 shapes. `?include=capabilities` adds the 203-entry effective catalog to `/metrics`, and
`?include=readings` adds 157 physical reading slots to `/live` in one locked observation. Exact
fields and absent-reading semantics are specified in `docs/API.md`. No page-specific response is
introduced.

Stage 4A uses one feature branch and one DRAFT PR with checkpoint commits: A reference-backed
foundation; B additional typed normalization; C full in-memory readable state; D additive API;
E tests, docs and CT109 runtime validation. They are implementation checkpoints within one stage.

### 25.2 Stage 4B — optional history

**Frozen direction:** Full live availability does not imply persistence. Preserve the 21-column
canonical `sample_1m` path and existing `rollup_1h` algebra. Users must later be able to select
additional history-suitable metrics without a SQL migration per selection. Optional writes remain
minute-based. `Selected but unknown` and `not selected` must remain distinguishable through raw
and long-term aggregation, including after purge; retain both selected and known minute counts.
Text or static identity topics do not become historical merely because they are live.

**Accepted Stage 4A→4B safety notes:** Selection must key off physical `identity` plus expected
topic evidence, never a capability field derived from canonical source priority — Stage 4A exposes
no such field. If a selected identity's expected topic later changes, historical storage must not
silently continue under the changed meaning. Generic physical `kind` (`number`/`text`) is payload
syntax, not a historical semantic type; a generic physical value carries no canonical sentinel
semantics automatically — e.g. a physical `TOP15 = -200` is a valid observed numeric payload but
must not automatically become a valid optional-history power value. Stage 4B must define history
eligibility, semantic type, sentinels and aggregation policy explicitly, per selection. Stage 4A's
`available` is a live-surface fact only; it does not by itself define minute-history validity.

**Settled Stage 4B direction:** core-wide minutes plus separate dynamic optional history with
persisted policy selection and selected/known counts in hourly rollups. The initial eligible set
remains all 15 explicit `HistoryProfile` definitions (§25.2.8). All 15 were selected together in
accepted runtime validation; no lower max-selection cap is justified for this set. Keep the shared
`STALE_AFTER_SECONDS=600` and `RETENTION_1M_DAYS=365`. Revisit the list or retention only with
new semantic or durability evidence; Stage 4C owns durable events. Sentinel-only or numeric
zero readings on one device model do not automatically define global eligibility or support.
A numeric known zero is a transport/history fact, not proof that a measurement is physically
useful on every device model.

#### 25.2.1 Checkpoint A — architecture and contract freeze (DONE)

Checkpoint A froze the architecture below and proved its two riskiest claims against real
MariaDB — the `optional_sample_1m` JSON candidate's semantic round trip and the policy-head
concurrency invariant — before any optional runtime recording exists. It changed no canonical
Stage 1–4A behavior, added no production Stage 4B table, and made no CT109 change. The subsections
that follow are the frozen target; later checkpoints implement them incrementally.

**HistoryProfile is not canonical `Metric`.** Optional physical history has its own immutable
semantic definition, conceptually:

```text
HistoryProfile
--------------
identity           expected_topic      profile_version     label
unit                kind (mean|last)   semantic_type       sentinels
min_value           max_value          energy
```

`kind` for a `HistoryProfile` is the aggregation semantic, exactly `mean` or `last` as for a
canonical `Metric`, but a `HistoryProfile` is never modeled as a fake `Metric`. Canonical logical
metric keys and physical capability identities (§25.1) remain two distinct namespaces; a
`HistoryProfile` adds a third, its own historical-series identity (§25.2.5), and none of the three
may be conflated. A future checkpoint may extract a small pure primitive shared by canonical
`catalog.parse_value` and `HistoryProfile` parsing — finite-number validation, sentinel matching,
min/max range checking, and the `Outcome` result — once `HistoryProfile` parsing has a concrete
caller to prove the extraction against. Checkpoint A left `catalog.parse_value` and its tests
untouched rather than extracting speculatively: nothing in checkpoint A calls a `HistoryProfile`
parser yet, so there is nothing real to prove the shared primitive against.

**Eligible optional sources are observed continuously; selection controls persistence only.** A
later checkpoint maintains historical state for every history-eligible physical profile
continuously, whether or not it is currently selected, so that a factual earlier receipt still
fresh at a later minute boundary can support a newly selected minute — not backfill, the same
report-by-exception validity window canonical sources already use (§4). Optional source state must
stay outside `Ingest.sources`, because that collection drives canonical expiry segmentation
(`MinuteAccumulator`); optional integration must never perturb it.

**A separate `OptionalAccumulator`, not a change to `MinuteAccumulator`.** Canonical minute
segmentation and arithmetic (§6) are not modified for optional expiries. A later checkpoint adds an
independent optional integration path sharing the same event timestamps, the same minute
boundaries, and the same connection/LWT/clock-discontinuity invalidation facts as the canonical
accumulator, but computing its own, independent optional expiry splits. `minute.py`'s canonical
arithmetic remains byte-for-byte unchanged; checkpoint A did not modify it.

**A persisted policy timeline is the only selection truth**, never inferred from raw optional row
presence, live availability, in-memory state, or recorder polling time. Conceptually:

```text
optional_series              optional_policy_revision      optional_policy_member
----------------              -------------------------      -----------------------
id                             id                              revision_id
identity                       base_revision_id                series_id
expected_topic                 effective_from_minute
profile_version                created_at
created_at

optional_policy_head
--------------------
id = 1   (singleton)
revision_id
```

A series row is one immutable historical meaning: a different `(identity, expected_topic,
profile_version)` tuple is a different series, never a silent repoint of an old one. Series,
revisions and members are immutable after creation; only the singleton head pointer mutates, and
only to point at a later revision in the linear chain.

**`effective_from_minute` is persisted on every revision**, so policy activation depends on
database timeline truth, never on when recorder code happens to notice a revision. A minute is
governed by the latest revision in the linear chain whose `effective_from_minute <= minute ts`.
There is no partial-minute selection: a policy change affects only future complete minutes.

**Policy concurrency uses one database serialization point.** Both a policy-replacement
transaction and a minute-persistence transaction must serialize against the same singleton
`optional_policy_head` row using a locking/current read equivalent to `SELECT revision_id FROM
optional_policy_head WHERE id = 1 FOR UPDATE`. This repository's transactions already begin with
`START TRANSACTION WITH CONSISTENT SNAPSHOT` (`Storage.session`, §21); an ordinary `SELECT` inside
that snapshot is *not* sufficient here; it stays bound to the transaction's own consistent snapshot
even after the row lock is acquired. Only a locking read forces a current read of the latest
committed row, regardless of when the transaction's own snapshot was established. Checkpoint A
proved exactly this against real MariaDB
(`backend/tests/test_stage4b_policy_concurrency.py`): a plain read taken after unblocking still
returned the pre-commit value, while the locking read taken in the same transaction, at the same
point, returned the value the other transaction had just committed.

Required race property: if minute persistence locks and commits first, the concurrent policy PUT
must block on the same row and, once unblocked, must choose an `effective_from_minute` strictly
after the already-committed minute frontier it observes through its own locking read — never
through a plain read it might have cached while waiting. If the policy PUT locks and commits
first, the concurrent minute-persistence transaction must block and, once unblocked, must resolve
every minute it is about to write against the newly committed policy through its own locking read.
No committed minute may ever be retroactively reclassified because a concurrent PUT was invisible
to the transaction that wrote it.

Conceptual sequencing: a policy PUT observes recorder/open-minute safety under the Python
`Recorder` lock, releases it before any database I/O, then begins a transaction, locks the policy
head with a locking read, validates `base_revision`, inspects the current committed canonical
minute frontier with a locking/current read where needed, computes a future minute-aligned
`effective_from_minute`, inserts the immutable revision and its members, updates the head, and
commits. Minute persistence begins a transaction, locks/reads the same head, resolves the timeline
policy applicable to each minute being written, persists accordingly, and commits.

**Selection states are independent of optional raw row presence.** For any minute and physical
series:

| Combination | Meaning |
|---|---|
| recorded + selected + known value stored | `known` |
| recorded + selected + no known value stored | `selected but unknown` |
| recorded + not selected | `not selected` |
| no canonical minute | `not recorded` |

A missing optional row never by itself means "not selected"; the policy timeline is the only fact
that can say that.

#### 25.2.2 The `optional_sample_1m` JSON candidate — MariaDB feasibility proved

The frozen storage candidate for raw optional values:

```sql
CREATE TABLE optional_sample_1m (
  ts          INT UNSIGNED NOT NULL PRIMARY KEY,
  values_json JSON NOT NULL
) ENGINE=InnoDB
```

The JSON document contains only known, selected values for that canonical minute, keyed by
persistent `series_id` as JSON object keys (e.g. `{"17": 21.25, "22": 0.0, "31": 46.8}`). No
`revision_id` is stored in this row: selection truth comes entirely from the policy timeline
(§25.2.1), never from this table. If no selected optional series is known for a canonical minute,
no `optional_sample_1m` row exists for it at all — an empty document is never stored, matching the
"selected but unknown" state above.

This candidate is frozen *because* checkpoint A proved it against real MariaDB (test-only table
`stage4b_json_feasibility`, `backend/tests/test_stage4b_json_feasibility.py`), not merely proposed:
a deterministic encoding (`json.dumps(values, sort_keys=True, separators=(",", ":"),
allow_nan=False)`) round-trips every tested value — `0.0`, `-0.0`, `0.1`, `1.0/3.0`, `21.123456`,
`1e-12`, `1e12`, and a realistic negative telemetry reading — through MariaDB's `JSON` column
(which is a `LONGTEXT` alias, so the server does not reformat a validated document) and back to an
equal Python float; `NaN`/`Infinity` are rejected by `allow_nan=False` before anything reaches the
database; a document that fails `JSON_VALID` is refused by an explicit `CHECK (JSON_VALID(...))`
constraint rather than silently stored; an idempotent upsert replaces the complete document rather
than merging old and new keys; a `DELETE` for a minute leaves no stale value; and a present key
with numeric `0` is distinguishable from an absent key. This is semantic feasibility only — the
test makes no claim about MariaDB's physical on-disk byte layout.

Do not implement fixed `s1..s64` columns; the JSON candidate is proven sufficient. Do not implement
an EAV table; JSON keeps one row per canonical minute, matching the canonical `sample_1m` shape.

**One canonical minute universe:** `optional_sample_1m.ts ⊆ sample_1m.ts`. Optional history is
logically defined only over canonical-recorded minutes; a canonical gap is also an optional gap.
Selection truth, however, does not depend on atomic optional-row presence (§25.2.1).

#### 25.2.3 Write model and failure classes

The existing one protected/waiting minute write state machine (§19) is kept; checkpoint A adds no
second optional write queue. A later checkpoint's minute writes contain the canonical minute plus
optional known values for that minute, intended to commit in one database transaction together
with the canonical and optional storage/rollup corrections. Two failure classes are distinguished:

- **Optional domain/computation failure** (blocked topic drift, sentinel, rejected physical
  payload, profile mismatch, or any failure building optional values before database work): these
  degrade only the affected optional facts to unknown and must never prevent the canonical minute
  from being queued or written.
- **Optional storage/rollup failure**, once inside the database transaction: this fails the whole
  transaction, exactly like a canonical storage failure (§19). A `SAVEPOINT` that committed
  canonical data while leaving stale optional raw/rollup evidence behind is explicitly rejected: a
  late rewrite or re-recorded `ts` could otherwise commit new canonical truth while old optional
  history remained visible as if current. A pre-database optional domain failure leaves the
  optional fact unknown while the canonical minute survives; a database/storage transaction failure
  fails closed and retries using the existing protected-batch semantics (§19).

#### 25.2.4 Long-term optional rollup, roll frontier and purge frontier

Implemented schema:

```sql
optional_rollup_1h
------------------
hour_ts             series_id            selected_minutes
known_minutes       v_sum                v_min
v_max                v_last

PRIMARY KEY (hour_ts, series_id)
```

`selected_minutes > 0`; `0 <= known_minutes <= selected_minutes`; when `known_minutes == 0`,
`v_sum`, `v_min`, `v_max` and `v_last` are `NULL`. When known is positive, min/max/last are finite;
`v_sum` may be finite or NULL. `selected_minutes` comes from canonical recorded
minutes intersected with policy-timeline participation, never from optional raw-row count.
`known_minutes` and the value statistics come only from stored optional known values. Different
series meanings (§25.2.5) are never automatically combined.

One canonical `rolled_until` frontier is kept (§8); every canonical hour rebuild later rebuilds the
optional rollup for the same hour. One raw retention frontier is kept; `RETENTION_1M_DAYS` remains
365 unchanged. A later purge preserves the existing canonical proof unchanged, proves optional
raw/rollup consistency, and deletes canonical and optional raw below the same cutoff in one
transaction. Any doubt deletes nothing (§13); there is no independent optional retention policy.

#### 25.2.5 Historical meaning and versioning

One historical series is exactly `(identity, expected_topic, profile_version)`; different meanings
are never concatenated automatically. The public selector is
`optional:IDENTITY@PROFILE_VERSION`, resolving to exactly one persisted series id. A bare identity
or current-version alias is not accepted.

#### 25.2.6 Default selection

Empty. The existing canonical 21 continue recording exactly as before; nothing additional is
selected by default.

#### 25.2.7 Deferred beyond checkpoint A

Not yet frozen at checkpoint A: the complete eligible physical-profile list, energy output for
optional power, an operational maximum selected-series count, the `HistoryProfile` model itself,
production policy tables, the selection API and its concurrency implementation. Checkpoint B
(§25.2.8) makes these concrete. Still deferred beyond checkpoint B: `OptionalAccumulator`,
optional ingest runtime state, `optional_sample_1m`/`optional_rollup_1h`, optional raw
recording/rollup/purge, optional history query endpoints, and any CT109 optional-history
deployment (§25.2.8's own "deferred" list).

#### 25.2.8 Checkpoint B — policy foundation (DONE)

**`HistoryProfile` implemented** (`pompa/history_profile.py`): an initial, explicitly curated set
of 15 identities — TOP21, TOP50–TOP53, TOP55, TOP63, TOP64, TOP66, TOP90, TOP91, TOP93, TOP142,
XTOP1, XTOP4 — each evidenced from the tracked reference and/or an explicit (non-heuristic) legacy
catalog fact, or an explicitly labelled project design choice; never a topic/description substring,
a payload's generic Stage 4A `kind`, or a physically-plausible guess. `__post_init__` additionally
rejects a non-positive `profile_version`, a canonical-source identity, a non-finite sentinel or
min/max, an inverted `min_value > max_value` range, or `energy=true` with a kind other than `mean`.

The six temperature profiles share the canonical `{-78, -128}` sentinel pair. Evidence is kept
precisely separated by claim: the tracked `MQTT-Topics.md` documents each identity, topic and its
"(°C)" meaning only, **not** a sentinel value; the legacy repository's explicit (non-heuristic)
catalog fact assigns `TEMPERATURE_SENTINELS` to all six; the tracked `realne_dane.md` directly
observes `-128` on two of them in its one checked-in snapshot (`-78` is not directly observed
there). Using the pair for all six is therefore a project decision supported by that evidence and
by Pompa Next's own existing canonical temperature convention, never a claim that the tracked
reference itself documents the sentinel values. Every other non-power profile has an empty
sentinel set and no min/max, because no such evidence exists for it.

`XTOP1`/`XTOP4` v1 is an explicit owner decision, frozen with its evidence kept separated by claim:
their W identity/meaning is tracked/observed Stage 4A evidence; their `{-200}` sentinel is an
explicit, non-heuristic legacy product catalog fact (**not** documented by the tracked reference);
their `min_value = 0.0` is a **project design choice**, not evidence from either source, deliberately
aligned with Pompa Next's own existing canonical power algebra (`catalog._power(..., min_value=0.0)`)
so that an unevidenced negative cooling-power reading fails closed instead of silently becoming
valid historical energy — every other finite negative value (e.g. `-1`) is `REJECTED`, only `-200`
is the documented "unknown" `SENTINEL`. No optional data had ever been persisted for these two
identities, so correcting this v1 definition needed no migration. `energy=true` for both: their W
semantics are evidenced, independent of the still-deferred optional-power kWh question.

Asserted by test, from the canonical catalog itself: no chosen identity ever serves a canonical
metric source. A golden-value test also pins a deterministic semantic fingerprint
(`history_profile.semantic_fingerprint`) of every existing profile; if code ever changes an
existing `(identity, profile_version)`'s semantics, the fingerprint test fails loudly, and the
correct fix is a new `profile_version`, never updating the expected fingerprint. This is
code/domain knowledge only; nothing here is persisted until a profile is selected.

**One `(identity, profile_version)` is one immutable semantic definition; `label` is not
semantic.** The semantic fields are `identity`, `expected_topic`, `profile_version`, `unit`,
`kind`, `semantic_type`, `sentinels`, `min_value`, `max_value` and `energy`
(`history_profile.ProfileSemantics`/`profile_semantics`). `label` is presentation metadata — a
first-seen historical presentation snapshot — and changing it, alone, never requires a new
`profile_version` and never creates a new series; an already-persisted series keeps its own stored
label forever regardless of later code changes. Any intentional change to a semantic field,
`expected_topic` included, requires a new `profile_version`: `expected_topic` remains part of the
persisted series identity `(identity, expected_topic, profile_version)` — a historical
self-description, kept precisely so an old row never needs code to interpret it — but it is *not*
an independent axis a given `profile_version` may vary along. One `identity`/`profile_version`
pair names exactly one topic and one semantic definition; runtime enforces this *before* a series
is ever created or reused (below), not merely by convention.

**The numeric-parsing primitive is extracted** (`pompa.catalog.parse_numeric`): float conversion,
the finite-number check, the sentinel check and the min/max check, in that order. Canonical
`parse_value` is now a thin wrapper over it; a test proves the two are exactly equivalent, so the
extraction changed no canonical behavior. `HistoryProfile` parsing (`parse_history_profile_value`)
reuses the same primitive; checkpoint B adds no caller for it yet (`OptionalAccumulator` is
checkpoint C), so it is proved correct in isolation, ready to be trusted immediately once it has one.

**Production policy tables exist**, exactly as the checkpoint A candidate, with one addition: an
`optional_series` row persists the *complete immutable semantic snapshot* of its meaning — label,
unit, kind, semantic_type, a deterministic JSON sentinel set, min/max and `energy` — not just the
bare `(identity, expected_topic, profile_version)` tuple. This is deliberate: future code must
never be required to retain every historical `HistoryProfile` version forever just to interpret an
old selected series; the persisted row is already self-describing. The persisted historical
identity is `(identity, expected_topic, profile_version)`, enforced as `UNIQUE` — but, as detailed
below, the database schema alone does not enforce the *stronger* rule that one `identity`/
`profile_version` pair may only ever name one `expected_topic`; that is a runtime guard, not a
schema constraint.
`optional_policy_revision.base_revision_id` is additionally `UNIQUE`, giving the linear chain a
real database-level guarantee (one child per base) beyond the application-level head lock.
`optional_series.identity`/`expected_topic` use an explicit binary collation
(`utf8mb4`/`utf8mb4_bin`), independent of the database's default collation: protocol identity/topic
text compares exactly, so a case difference is a different tuple, never a silent alias — `label`
keeps the default collation, since it is display text never compared or looked up by value. A
genesis revision (id 1, no base, `effective_from_minute = 0`, empty selection) and a head already
pointing at it are seeded idempotently by `Storage.ensure_schema()`; no `optional_sample_1m` or
`optional_rollup_1h` table exists yet.

**An existing series is looked up by `(identity, profile_version)`, verified before reuse, never
trusted blind, and never mutated on conflict.** `optional_policy.resolve_series_id` always takes a
locking/current read for *any* row already sharing the current profile's `(identity,
profile_version)` — `Session.lock_series_by_identity_version` — regardless of that row's own
stored `expected_topic`. Checking only the full `(identity, expected_topic, profile_version)`
tuple, as `Session.get_or_create_series`'s own `UNIQUE` constraint does, is not sufficient by
itself: a code change to `expected_topic` without a `profile_version` bump would otherwise look
like a brand-new, non-conflicting tuple and silently create a second, independent series for the
same identity and version — precisely the failure the "one version, one meaning" rule above exists
to prevent. So the identity+version lookup runs first: no existing row means it is safe to create
one (nothing else can be concurrently mutating `optional_series` while this transaction holds the
policy-head lock); an existing row is reused only if its complete stored `ProfileSemantics` —
`expected_topic` included — equals the current code's; any disagreement, on `expected_topic` or
any other semantic field, raises `SeriesDefinitionConflict` and fails the whole PUT transaction
closed — no new revision, no head movement, no row ever mutated or duplicated. More than one row
already sharing an `(identity, profile_version)` is corrupted state and is never silently resolved
by picking one. The idempotent-replay path (§25.2.1 Part 13) is equally unable to turn a persisted
topic/semantic disagreement into a false success: its own coarse key comparison already includes
`expected_topic`, so a topic change without a version bump is refused as a stale base before any
semantic comparison is even reached.

**The policy-head lock generalizes to every read of what it protects.** Checkpoint A proved that a
locking read of the head row observes committed truth even inside this repository's
`START TRANSACTION WITH CONSISTENT SNAPSHOT`, unlike a plain read of that same row. Checkpoint B's
first working implementation initially violated the *next* step of that same principle: after
`lock_policy_head()` correctly returned the current head id, it read that revision's own row and
member list with a **plain** `SELECT` — which stayed bound to the transaction's own pre-commit
snapshot and could report the just-locked revision as not existing at all. A real two-thread test
against production tables caught this. The fix, and the now-general rule: once a transaction has
taken one locking read to establish current truth, every further read needed to interpret *that
same fact* must also be a locking read (`Session.lock_revision`/`lock_revision_members`), not only
the singleton row that started the chain. A plain read stays correct only for read-only reporting
that never mixes with a locking read in the same transaction (`read_selection`/`GET`).

**`effective_from_minute` is computed** as the later of: a new read-only `Recorder.safe_future_minute`
fact (lock held only in-process, no database I/O, released before any transaction begins); the next
whole minute after a second clock read taken after the head lock is acquired; the latest committed
canonical minute (from `Session.lock_latest_minute_ts`, a current read of the newest `sample_1m`
row, not `MAX(ts)`) plus one minute; and the current head revision's own `effective_from_minute`.
The database transaction never overlaps the `Recorder` lock.

**Drift/blocking uses exactly five reasons** (`history_profile.drift_reason`): `profile_missing`,
`profile_version_changed`, `topic_changed`, `capability_topic_changed`, and
`profile_definition_changed` — same `(identity, expected_topic, profile_version)`, but the
persisted `ProfileSemantics` disagrees with current code's for that tuple (a code-definition error,
never a fact about the device). A capability currently missing from the effective catalog entirely
surfaces as `capability_topic_changed` (its topic lookup returns `None`, which can never equal a
real `expected_topic`); a sixth, dedicated reason is not worth the vocabulary unless it later needs
to be told apart from an ordinary topic change. It compares one persisted series meaning against
*current* code and *current* Stage 4A capability topics; a blocked member is never mutated or
deleted, and blocking one member never blocks canonical operation or any other member.

**Ambiguous PUT retries are idempotent** (§25.2.1's required property, now implemented) *and*
semantic-safe: a retry supplying the same `base_revision` whose current head's own
`base_revision_id` and resolved `(identity, expected_topic, profile_version)` set match is answered
with the already-created revision only after also verifying every one of those existing members'
stored `ProfileSemantics` still equals the current code's — coarse identity agreement is not
sufficient by itself, since it cannot by construction distinguish an ordinary replay from a replay
racing a code change that altered semantics without a version bump. That specific case raises
`SeriesDefinitionConflict` (never a false idempotent success) exactly like a fresh PUT would. A
genuinely different concurrent request against a since-moved head is refused (`StaleBaseRevision`).

**Deferred beyond checkpoint B**: `OptionalAccumulator` and optional ingest runtime state,
`optional_sample_1m`/`optional_rollup_1h`, optional raw recording/rollup/purge, optional history
query endpoints, and any CT109 optional-history deployment.

#### 25.2.9 Constraints recorded now for checkpoint C

Documentation only; no runtime path below is implemented yet.

**Lock order.** A future canonical-plus-optional persist transaction must lock
`optional_policy_head` **before** touching or inserting into `sample_1m`, preserving exactly the
same lock order a policy PUT already uses (policy head, then the canonical minute frontier/sample).
Reversing this order for only one of the two transaction kinds would create a deadlock inversion
that does not exist today.

**Membership resolved at persist time, not at minute-open time.** The future `OptionalAccumulator`
observes and computes values for every history-eligible profile continuously, regardless of current
selection (§25.2.1). Policy resolution decides only which of those already-computed per-minute
facts are *selected for persistence* — never whether they are known. Knowledge/value validity is
entirely the optional accumulator's and its source evidence's concern (freshness, sentinels, full-
minute life — the same kind of facts §6 already establishes for canonical sources), computed
independently of selection. Which minutes get *persisted* for a given series must be decided when
that minute is persisted, by re-resolving the timeline policy applicable to it under the same
policy-head locking/current-read transaction, never by capturing selection membership once when the
minute opens. A policy PUT may commit while a minute is already open; only a persist-time
resolution can be correct for that minute without retroactively reclassifying anything already
committed (§25.2.1's required race property).

#### 25.2.10 Checkpoint C — optional raw recording (DONE)

`Ingest.optional_sources` holds one history-specific source state for every current
`HistoryProfile`, separate from canonical `Ingest.sources`. It tracks `seen_live`, the latest
non-retained numeric value or unknown, and its receive time. Every profile is observed from process
start independent of policy selection. Retained deliveries still update the Stage 4A physical live
reading, but never establish optional history. A non-retained sentinel or rejected payload replaces
the previous optional value with unknown. Reconnect, disconnect, Offline LWT (retained or not), and
detected backward clock steps invalidate confirmed optional values. Fresh optional values require
connection, non-Offline LWT, current-epoch non-retained evidence, a known value and the same
half-open 600-second source-life window as canonical history. Optional expiry queries never enter
canonical `Ingest.next_expiry_after`.

`OptionalAccumulator` has its own cursor, UTC minute boundary, expiry walk, per-profile mean sum,
whole-minute validity and final-segment value, and open-minute poison state. It receives the same
pre-event recorder timestamps as `MinuteAccumulator` and computes all profiles without policy or
database access. A mean is known only for a wholly valid minute and is rounded to six decimals;
`last` uses the final segment even if an earlier segment was unknown. Its optional expiry splits
cannot change canonical segmentation or floating-point arithmetic. On a backward clock correction,
the open optional minute is poisoned if it already integrated pre-correction time, and its source
evidence is invalidated; the following non-retained event may establish fresh evidence. A finite
source payload can overflow the time-weighted segment or sum: any non-finite optional mean
arithmetic makes that profile unknown for the minute, permanently for that minute. The close path
checks the derived mean again before emitting it. Persistence also omits any unexpectedly
non-finite optional fact. This optional domain invalidity never prevents canonical persistence.

The recorder pairs only an optional minute whose `ts` equals a canonical `MinuteRow.ts`; it
discards optional-only results and supplies an empty optional fact for a canonical minute with no
optional result. One immutable `RecordedMinute` pair enters the existing single waiting/protected
queue. Overflow drops the whole never-submitted pair; ambiguous failure protects and retries the
same pair; `RebuildRefused` removes whole permanently unwritable pairs. Existing row counters
remain counts of canonical minutes.

`optional_sample_1m(ts INT UNSIGNED PRIMARY KEY, values_json JSON NOT NULL)` is created
idempotently. Its restrictive foreign key to `sample_1m(ts)` enforces
`optional_sample_1m.ts ⊆ sample_1m.ts` without cascade deletion. Each JSON object contains only
selected, unblocked, known values under string-encoded persistent `series_id` keys. Deterministic
encoding uses sorted keys, compact separators and `allow_nan=False`; an empty document is deleted,
and an upsert replaces the complete prior document. A selected unknown value has no key (or no
optional row if no other value is known); real zero is stored as `0.0`.

For each write batch, `persist` locks `optional_policy_head` first, then loads the immutable
revision/member chain through current/locking reads, resolving each minute by the latest descendant
effective at its own `ts` (including same-boundary ties). It checks each selected series snapshot
against the current profile and capability topic; any drift is selected-but-unknown for that
minute. The transaction checks for purged rolled hours before any upsert, writes canonical raw,
then optional raw, and corrects touched canonical rolled hours atomically. Optional storage failure
rolls back both; ambiguous retries repeat whole-document replacement from the same protected pair.
Policy membership is never captured at minute-open, close or queue time.

During Checkpoint C, before D's shared proof, the canonical purge
locks the policy head first, reads the exact candidate canonical minute timestamps and their
persisted policy memberships with current/locking reads, and fails closed if **any** candidate
minute was under a non-empty selection. This protects selected-known optional raw and
selected-but-unknown evidence, including a selected minute with no `optional_sample_1m` row. The
optional-row check remains as a second guard. A refused operation deletes neither canonical nor
optional raw; canonical-only old ranges still purge as before. Canonical hourly rollup continues
normally. Until D, canonical raw remains for every recorded minute intersecting a non-empty
optional selection so D can prove both `selected_minutes` and `known_minutes`. D replaces this
conservative guard with `optional_rollup_1h` and shared proof/deletion. No optional history query,
kWh output or optional raw public API existed in checkpoint C.

#### 25.2.11 Checkpoint D — durable optional history (DONE)

`optional_rollup_1h` has primary key `(hour_ts, series_id)` and a restrictive FK to
`optional_series(id)`. Each row has positive `selected_minutes`, `known_minutes` between zero and
selected, and nullable `v_sum`, `v_min`, `v_max`, `v_last`. All four statistics are NULL when
known is zero. With positive known count, min/max/last stay finite; sum may be NULL.
`OptionalStats` keeps these optional facts separate from canonical `Stats` and combines them in
the same chronological binary-float association. Persisted `kind=last` never computes a sum:
NULL means not applicable. For `kind=mean`, NULL with positive known count means the aggregate
sum exceeded binary DOUBLE representability. Once lost, a later opposite-signed value cannot
recover it. This is valid history, not corruption, and does not stop rolling, late persistence or
purge. For each canonical recorded minute, selected ids come from the
persisted policy timeline; known ids and values come from its optional raw JSON. Natural canonical
gaps contribute nothing. Raw keys that are invalid, non-finite, or unselected for their minute
fail closed as inconsistent stored evidence. Read-only queries validate all loaded raw JSON keys
and values but aggregate only requested series; a valid unrequested huge series cannot affect
another answer. Current profiles, capability drift and selection
availability never reinterpret old raw or rolled facts.

There is still one `rolled_until`, derived from canonical `rollup_1h`. Rolling a stored UTC hour
locks `optional_policy_head` first, reads the exact canonical minutes, persisted policy and
optional raw through current/locking reads, and atomically replaces the complete canonical and
optional hour rollups. Late writes below `rolled_until` repeat that joint rebuild in the raw
write transaction; a purged hour remains unwritable. Replacing the complete optional hour removes
stale series rows after a same-ts rewrite. A failed optional rollup write rolls back canonical
rollup work too.

Purge retains the canonical completeness proof and additionally compares the exact optional fold
of candidate canonical minutes, persisted policy and optional raw against every stored optional
rollup row for those hours. Missing, extra or mismatched rows refuse the entire purge. After all
proofs pass, it deletes optional raw before canonical raw under their restrictive FK, in one
transaction. The old C selection-based refusal is gone: selected-but-unknown survives as an
optional rollup row with positive selected count, zero known count and NULL statistics. Policy,
series metadata and both rollups remain indefinitely. There is no optional retention frontier.

Public history uses exact `optional:IDENTITY@VERSION` selectors, each resolving to one persisted
`optional_series` row. Versions never concatenate; old or currently blocked meanings remain
discoverable and queryable. The existing history engine reads canonical and optional facts in one
consistent snapshot and uses the same exact ranges, UTC hour pieces, Warsaw daily edges,
auto-bucket promotion, `MAX_BUCKETS`, and canonical `rolled_until`. Minute buckets use raw;
hourly or larger buckets use optional rollup for complete rolled hours and raw for partial or
unrolled hours. Per-bucket selected and known counts remain distinct. Persisted `energy=true`
adds kWh from known minute-average W only (`Σ W / 60000`); no optional COP is inferred. Default
history requests remain canonical-only. A requested mean bucket with positive known count and
NULL sum cannot supply `avg`, and a persisted energy series in that state cannot supply `kwh`;
either request returns 422 Unrepresentable. A last series remains fully queryable from
last/min/max/counts without a sum. Rollup reads validate the structural NULL pattern and reject
a non-NULL sum for persisted `kind=last`; they never consult current profiles.

### 25.3 Stage 4C — one activity interpretation and durable events

**Frozen direction:** One backend interpretation serves friendly operational state, CO/CWU
activity, compressor runtime and observed starts, cycles, short-cycling facts, individual defrosts,
timeline and later reports. Start/end, duration, intervals, min/max/average duration and duration
distribution are factual outputs, not good/bad/fault verdicts. Missing rows remain gaps; unknown
remains unknown. Do not smooth across gaps or add per-second machinery without real evidence.
Keep the device operations counter distinct from observed compressor starts until verified.

Useful event facts must survive raw purge; raw-only recomputation cannot meet durability. **Owner
decision:** durable per-hour activity segments. Checkpoint B materializes each UTC hour's ordered
segments through the existing `persist()` → `rebuild_hour()` path, together with the rule version.
Events, compressor runs, starts, intervals, continuation and range projections are derived on read
by stitching segments; no cross-hour state machine is persisted. The segment table and its purge
proof are frozen in §25.3.2; the read model, evidence loading and API resources in §25.3.3.

#### 25.3.1 Checkpoint A — activity domain truth (DONE)

`pompa/activity.py` is pure and storage-free. It reads only canonical `MinuteRow` values
(`ACTIVITY_COLUMNS`); a column that was not read is an error, never an unknown metric. The
minute/segment interpretation is versioned: `ACTIVITY_RULE_VERSION = 1`. 4C-B stores it as historical
meaning, so its scope is exactly what a stored segment means:

- minute classification
- the persisted `Activity`/`Compressor` strings
- the activity columns and the >100 W threshold
- segment grouping (activity, compressor state and exact defrost fraction; a UTC hour or missing
  minute always splits)
- the segment fields
- `ENERGY_SERIES` with its order and ingredient content

Changing any of them needs a new version and a new golden, never an edit. The version-1 golden
(`tests/test_activity.py`) is a literal fingerprint independent of the implementation's tables,
and a self-test proves it fails on each kind of drift. Read-time projections, boundaries and
summaries are not versioned by it: a summary names the interpretation it read as
`segment_rule_version`.

**Classification of one recorded minute.** Compressor state comes from `compressor_freq` alone:
`NULL` → unknown, exactly `0` → off, `> 0` → on. It is not `NULL`-as-off. Activity precedence:

1. `defrosting_state` `NULL` → `unknown` (a defrost cannot be excluded).
2. `defrosting_state > 0` → `defrost`, over valve, power and compressor evidence.
3. Compressor unknown → `unknown`.
4. Compressor off → `off` if `heatpump_state == 0`, `unknown` if it is `NULL`, otherwise `idle`.
   Valve position and power tails never create CO/CWU activity with a stopped compressor.
5. Compressor on, known valve → `0` `co`, `1` `dhw`, fractional `transition`. A fractional value is
   the time-integrated minute mean, never a reconstructed intra-minute switch order.
6. Compressor on, valve `NULL` → the legacy-proven `> 100 W` power evidence. A side (CO or DHW)
   is active if any of its consumption/production channels exceeds 100 W, and inactive only if
   both are known and at most 100 W. CO only → `co`, DHW only → `dhw`, both → `transition`; any
   other combination, including an unknown side, → `unknown`.

`operations_counter` and `operating_mode` never classify. The device counter is not observed start
truth.

**Segments.** An `ActivitySegment` is consecutive recorded minutes within one UTC hour with the same
activity, compressor state and exact `defrosting_state` value. A fractional defrost boundary minute
is therefore never merged with full minutes, and every segment can be clipped at any minute
exactly. Each segment carries the canonical energy ingredients: the `fold_minutes` `Stats` of the
four power channels and six paired series (`ENERGY_SERIES`) over exactly its minutes. Energy
(`ΣW/60000`, `minutes = n`) and period COP (`Σ paired out / Σ paired in`, `paired_minutes`) come
from the existing `energy_kwh`/`cop`; there is no second formula and no COP averaging.
Chronological `combine` of segment ingredients reproduces the history fold's minute counts and
pairing exactly. Its sums are equal up to binary floating-point association, and exactly equal
whenever the sums are representable. A piece clipped out of a segment carries no ingredients; a
sub-segment energy edge needs raw minutes, as in §15. Building hours separately and concatenating
their segments gives the same result as building a whole range.

**Timeline and gaps.** A `Timeline` places segments on an evidence window with explicit `Gap`s for
settled historical minutes without a row. A gap is missing evidence, never `unknown`. Minutes at or after
`closed_until`, the first minute whose historical outcome is unsettled for this process, are
neither gaps nor unknown. It is no later than `floor_minute(now)` and stops at the accumulator's
open minute or the oldest waiting/unacknowledged row. No row is manufactured.

**Spans.** Activity events, observed compressor runs ("cycles" = observed compressor runs),
compressor-off intervals and individual defrosts are maximal consecutive recorded minutes in one
state. They are never bridged across a gap or an unknown minute, and nothing is smoothed. `CO → idle
→ CO`, `CO → gap → CO`, `CO → unknown → CO` and `CO → defrost → CO` all remain separate pieces. A
compressor run may span CO, DHW, transition and defrost activity. Each edge carries a `Boundary`
describing the adjacent minute:

| Boundary | Adjacent minute |
|---|---|
| `observed` | recorded, consecutive, in a different known state (for a run: compressor off) |
| `unknown` | recorded, consecutive, state unknown |
| `gap` | settled without a row |
| `open` | not yet settled as historical evidence (right edge only): a current run or event has no fabricated end |
| `outside_evidence` | outside the examined window |

`start_observed`/`end_observed` are true only for `observed`. An observed start therefore needs
the immediately preceding consecutive minute recorded with the compressor off. Missing → on and
unknown → on are not starts, and nothing searches backwards across a gap. A short stop inside
minute means cannot produce a zero minute and is not claimed. An off interval is exact only when
both of its edges are observed runs.

Defrost spans (`defrosts`) judge their edges by the defrost signal itself. An adjacent known
`defrosting_state == 0` is `observed`, even when that minute's overall activity is `unknown` (for
example an unknown compressor or valve). Only an adjacent `defrosting_state` `NULL` is `unknown`.
Gap, open and outside-evidence edges are unchanged. `activity_events` reports activity changes,
so its defrost events keep activity-based edges, and compressor-run edges stay compressor-based.

**Resolution limits.** Activity minutes are per-minute classifications, not exact per-state
seconds. A fractional `heatpump_state` is a fully known state that changed within the minute; with
the compressor off such a minute is `idle`. Only defrost keeps an exact time-integrated fraction
and duration. Compressor run duration and compressor minutes are observed minute-resolution
evidence, not exact physical runtime.

**Defrost duration.** `observed_defrost_seconds = Σ defrosting_state × 60` over valid defrost
minutes. It is exact relative to Next's time-integrated observations, not a physical transition
second. Example: `0.583333, 1, 1, 0.166667` → 165 s. Separate defrosts stay separate.

**Range projection.** Chronology is UTC; local-day ranges come from the existing Europe/Warsaw
helpers, so 23 h and 25 h days need no special case. A projected span has
`starts_before_range`/`ends_after_range` true only when recorded minutes of the same span exist
outside the range. A query edge alone is never evidence, and a span ending exactly at midnight does
not continue. A summary counts a start in the range holding the run's first minute and a stop in
the range holding its last minute, because a zero minute proves the compressor off for that whole
minute. Starts and stops are therefore additive across adjacent ranges. Complete runs and exact
off intervals are attributed by their first minute. `compressor_runs_overlapping` and
`defrosts_overlapping` count every span intersecting the range. A span crossing midnight counts
once in each day and once in the combined two-day range, so overlap counts are **not** additive
across adjacent ranges. Summaries report minutes, counts and durations only, with no verdicts.

**Evidence window.** `Timeline.start`/`end` are the evidence actually examined, and a summary
carries them as `evidence_start`/`evidence_end` with `closed_until`. A span edge at the window
limit is `outside_evidence`. The same `[start, end)` can therefore prove different
boundary-sensitive facts from a narrower or wider window. Observed starts and stops are fixed once
the window contains the minute before `start` and the minute at `end` (`[start − 1 min, end +
1 min)`). Complete-run durations and exact off intervals may need evidence arbitrarily far beyond
the range, because a span can continue. Minute counts, gaps and defrost seconds depend only on
`[start, end)`. How much evidence an API loads is a checkpoint C policy decision.

#### 25.3.2 Checkpoint B — durable hourly activity segments (DONE)

**Table.** One additive table holds one row per `ActivitySegment`:

```sql
CREATE TABLE activity_segment_1h (
  start_ts         INT UNSIGNED      NOT NULL PRIMARY KEY,
  minutes          TINYINT UNSIGNED  NOT NULL,
  rule_version     SMALLINT UNSIGNED NOT NULL,
  activity         VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  compressor       VARCHAR(8)  CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  defrost_fraction DOUBLE            NULL,
  energy_json      JSON              NOT NULL CHECK (JSON_VALID(energy_json)),
  CHECK (start_ts MOD 60 = 0 AND minutes BETWEEN 1 AND 60 AND start_ts MOD 3600 + minutes * 60 <= 3600),
  CHECK (defrost_fraction IS NULL OR (defrost_fraction >= 0 AND defrost_fraction <= 1))
) ENGINE=InnoDB;
```

- **Hour and order.** A segment never crosses a UTC hour, so its hour is `floor_hour(start_ts)` and
  `start_ts` alone is the unique, ordered key. A separate hour/sequence column would only duplicate
  that fact.
- **Energy.** `energy_json` holds the segment's `ENERGY_SERIES` `Stats` as deterministic JSON
  (`{series: [n, sum, min, max, last]}`, sorted keys, shortest round-trip float text). A series
  with no known minute is absent, exactly as in `fold_minutes`. MariaDB JSON is stored as text, so
  every DOUBLE round-trips bit for bit. One row per segment carries its ten optional series
  without a child table or fifty columns.
- **No foreign key.** There is no FK to `sample_1m`: segments must outlive raw purge. An hour with
  no raw minutes has no rows; no gap row, no empty-hour record and no cross-hour or open-event
  state is stored. Within a materialized span, missing minutes are the holes between segments,
  read back as `Gap`s.
- **Validation.** `pompa.activity` encodes and fail-closed decodes every row. It checks:
  - the rule version (only 1 exists) and known `Activity`/`Compressor` strings;
  - activity/compressor/defrost-fraction combinations version 1 can produce;
  - alignment inside one UTC hour, and ascending non-overlapping rows;
  - energy structure: known series; `1 ≤ n ≤ minutes`; finite, non-negative values that are
    never `-0.0` (the application never writes one), with `min ≤ last ≤ max`;
  - canonical pairing invariants: paired input and output together with equal `n`; a pair never
    exceeds its channels; the total pair never exceeds the CO or DHW pair.

  Nothing malformed is coerced. The database CHECKs are a second structural guard.

**Materialization.**
- **Forward roll and repair.** `rebuild_hour(H)` reads the locked canonical minutes of `H` once and,
  in the caller's transaction, replaces `rollup_1h(H)`, `optional_rollup_1h(H)` and the complete
  segment set of `H` (`build_segments` of those minutes). It deletes the whole hour and inserts
  the deterministic new set, so a repeated rebuild or a lost-acknowledgement retry is idempotent.
  A failure anywhere rolls back all three with the raw write. `roll_next_hour` and late-write
  repair in `persist()` therefore materialize activity with no new pipeline and no new watermark;
  `rolled_until` remains the one roll frontier.
- **Purged hours.** A late write into an already-purged hour is still refused
  (`first_purged_hour`). Segments are a read representation, never a rebuild source.

**Upgrade backfill.** Hours rolled before this checkpoint have rollups but no segments, and
forward rolling never revisits them. `backfill_activity_step` derives them from the tables:
rolled hours (a `rollup_1h` row) at or above an in-memory scan hint that store raw minutes but no
segment row.
- **Scope and bounds.** Each step rebuilds at most 24 such hours through `rebuild_hour`. It
  examines at most seven days of raw (a bounded, primary-key range anti-join) and starts from a
  current read of the next raw minute, so long empty stretches cost nothing.
- **Order.** The recorder runs one step per maintenance pass after rolling. It purges only once
  a pass has found nothing left: roll, then backfill, then purge.
- **No watermark.** The scan hint and the completion flag are process memory, recomputed after
  every restart by one scan. No new watermark exists, because no Stage 4C-B path can create an
  unmaterialized rolled raw hour.
- **Anomalies stay visible.** A raw hour below `rolled_until` without a rollup row is a canonical
  anomaly, not backfill work, and the purge proof keeps reporting it.
- **History purged before 4C-B.** Hours whose raw was purged before this checkpoint have a rollup
  but no raw and no segments. They are never backfilled, and nothing is inferred from hourly
  averages or `operations_counter`. `first_purged_hour` keeps its single meaning.

**Activity evidence per UTC hour.** Stored facts give four distinct cases:

| Segments | Raw | Canonical rollup | Activity evidence |
|---|---|---|---|
| present | any | present | durable |
| absent | present | any | derivable from raw (not yet rolled, or awaiting backfill) |
| absent | absent | present | **unavailable**: canonical minutes were recorded, but their activity detail was purged before 4C-B |
| absent | absent | absent | not recorded |

An unavailable hour is not an ordinary gap. Checkpoint C reads keep "activity unavailable"
distinct from "not recorded" (§25.3.3).

**Purge proof.** After the canonical count proof and the optional proof, purge decodes the
candidate hours' segment rows through a locking read. It requires them to equal, hour by hour and
field by field, `build_segments` of the same locked raw minutes. The compared fields are start,
minutes, activity, compressor, exact defrost fraction, rule version and every energy statistic.
Any missing, extra, split, shifted, changed or invalid row raises `PurgeRefused` and deletes
nothing. After deletion, canonical and optional rollups and all segments remain. Decoded segments
rebuild the same `Timeline`, activity events, compressor runs with boundaries, defrosts and their
seconds, gaps, and per-run energy and paired-COP ingredients as the raw minutes did.

- **Canonical form.** Semantic equality is not enough. Each stored row must also equal
  `segment_record` of its expected segment exactly, including the `energy_json` text. JSON that
  Python decodes to the same values can still mean something else to another reader. Examples
  are a duplicate key, other whitespace, key order or number spelling, and `-0.0`. On MariaDB,
  `JSON_EXTRACT` returns a duplicate key's first occurrence, while Python keeps the last.
- **Why byte equality is safe.** MariaDB stores and returns the `utf8mb4_bin` text byte for byte,
  and a DOUBLE collapses `-0.0` to `0.0`. Exact record equality is therefore reliable, and
  application-written rows always pass it.

**Locking.** Every writer (persist, roll, backfill, purge, policy PUT) takes the policy-head lock
first, so they serialize. After it come the rolled frontier, canonical raw and the derived tables.
A transaction's snapshot predates that lock, so any read that decides a write must be current.
`persist()` therefore evaluates `first_purged_hour` with current reads (`locking=True`, the same
fact). A real MariaDB race test showed that the earlier snapshot read let a late minute commit a
partial rebuild over an hour purged while it waited. Backfill discovery may stay a snapshot read
because its range starts at a current read of the oldest raw minute and purge deletes only a
prefix.

#### 25.3.3 Checkpoint C — activity read model and resources (DONE)

`pompa/activity_history.py` answers `GET /api/v1/activity` and `GET /api/v1/activity/live` (contract
in `docs/API.md`). It is read-only: no backfill, repair or rewrite ever happens on an API thread.

**Source per UTC hour.** One `Storage.session()`, one consistent snapshot, chooses exactly one
source for each examined hour:
1. **Durable segments**, when present. These are never recomputed from raw, even while raw
   exists, because they are what the purge proof guarantees and what survives. A durable hour is
   accepted only when all of the following hold:
   - every row decodes under a supported rule version;
   - every row equals `segment_record` of its decoded segment, the exact canonical persisted
     form, so a duplicate JSON key, other whitespace, key order or number spelling is refused;
   - the hour's segment minutes sum to exactly its `rollup_1h` `recorded` count. `rebuild_hour`
     writes both from the same locked minutes, so a missing, extra or forged segment is
     corruption, never a gap.

   Otherwise the read raises `ActivityRecordInvalid` (HTTP 500), even when the corrupt hour was
   loaded only as widened evidence. There is no fallback to raw and no repair. An hour with
   **no** durable rows is not corruption: it continues to rule 2, 3 or 4.
2. Otherwise, **raw minutes** through `build_segments`: unrolled hours and rolled hours awaiting
   backfill.
3. Otherwise, a `rollup_1h` row makes the hour **unavailable**: canonical minutes existed, but their
   activity was purged before 4C-B. It becomes an `Unavailable` timeline item.
4. Otherwise nothing was recorded: ordinary gaps.

Each hour has one source, so mixed durable/raw ranges never duplicate a minute. The activity API
reads the recorder's settled frontier under its lock before opening the storage snapshot. The
frontier is the minimum of `floor_minute(now)`, the accumulator's open minute and every waiting or
unacknowledged row minute. All minutes below it have an acknowledged write or can no longer yield
a row from this process, including permanently dropped/refused rows and settled no-row minutes.
At and after `closed_until`, even a temporally closed minute awaiting persistence is `open`, never
a gap. A committed row still protected pending acknowledgement is conservatively `open`; after
acknowledgement, the subsequently opened DB snapshot can safely expose it. Restart needs no
persistent frontier: earlier DB history is settled and an actual restart hole remains a gap.

**Read-time boundary.** `Unavailable` and `Boundary.UNAVAILABLE` are read-time only and outside
`ACTIVITY_RULE_VERSION`. `unavailable` is never `gap` (not recorded), `unknown` (a recorded,
unclassifiable minute) or `open` (not yet settled). A request whose settled part intersects an
unavailable hour is refused with `ActivityUnavailable` (HTTP 422), naming the first such hour;
nothing partial is returned. Unavailable evidence met only while widening outside the range just
ends a span with an `unavailable` boundary.

**Evidence loading.**
- **Initial window.** Loading starts at `[floor_hour(from − 1 min), ceil_hour(to + 1 min))`, capped
  at `ceil_hour(closed_until)`. That already fixes observed starts and stops at both edges.
- **Widening.** Widening follows only spans that intersect the request. While any such span
  (activity event, compressor run, off interval or defrost) still ends at the window's edge as
  `outside_evidence`, the loader adds whole hours on that side. The added chunks grow
  exponentially: 1 h, 2 h, 4 h, and so on, up to 7 days. The final window may therefore extend
  beyond the decisive boundary by up to the size of the last chunk.
- **Stop conditions.** Widening stops at an observed change, `unknown`, a true gap (an unrecorded
  neighbouring hour), `unavailable`, `open`, or the start of all history.
- **Cost.** A run or event that intersects the range is therefore returned whole, however long,
  across UTC hours, midnight and DST, and never as two cycles. Each loaded chunk costs:
  - one segment range read;
  - one rollup range read of the `recorded` series;
  - one raw range read per contiguous run of hours without segments. Alternating durable and
    non-durable hours can therefore need several raw reads.

  All of these are indexed primary-key range reads. A small range is not a history scan.
- **Range limit.** A request is at most 31 days and one hour (the longest local month); longer is
  a `422`, never truncated.

**Projection.** The response reuses the Stage 4C-A spans and `summarize`; there is no second
formula.
- **Spans.** Every span object gives its full observed `start`/`end`/`minutes` and, separately,
  its `overlap_*` with the request, plus `starts_before_range`/`ends_after_range`, both boundaries
  and `start_observed`/`end_observed`.
- **Energy.** Energy and paired COP come from `energy_kwh`/`cop` over the full span's segment
  `Stats`. They are never prorated to the overlap and are `null` when a piece carries no
  ingredients.
- **Defrosts** report full and overlap integrated seconds.
- **Timeline** items are positional `activity` (a maximal event, with its span), `gap`, or
  `open` (`[max(from, closed_until), to)`).
- **Additivity.** `*_overlapping` counts stay non-additive. Starts, stops and minute counts add up
  over any partition of a range, and each span keeps identical full-span facts in every range
  that contains it.

**Live.** `/api/v1/activity/live` classifies one `Recorder.live` observation, the same lock and
freshness/provenance rules as `/api/v1/live`, with the version-1 `classify`. Only `mode="live"`
values are evidence. Retained, stale, absent and disconnected inputs are `NULL`, and the
classifier's own `NULL` rules decide. It exposes every input's value, mode, receipt time and use.
It never reads storage, stays available during a database outage and is never persisted. It is
the current moment, not the last closed history minute.

### 25.4 Stage 4D — report projections

**4D-A contract freeze; pure domain in 4D-B, API in 4D-C.** One backend-owned report model composes canonical
history, `rollup_1h` and Stage 4C durable activity for day, week, month and custom local-date
periods. The frontend renders these facts; it does not derive energy, COP, activity, event
attribution, coverage or bucket alignment. `/history` and `/activity` retain arbitrary instant
ranges. There is no service or formula per frontend panel.

**Storage and durability.** Reports use existing `rollup_1h` and `activity_segment_1h`, both kept
indefinitely. There is no `daily_rollup`, 5m report table, report cache, materialization or
watermark. A longest Warsaw month has 745 UTC hours; the selected facts remain exactly
representable after raw purge. Do not add infrastructure for hypothetical year reports. A strict
energy total restricted to minutes where both constituent channels were simultaneously known is
*not* representable from these durable tables: two different minute histories can yield identical
per-channel and paired hourly/segment `Stats` but different simultaneous-known subsets for the
ordinary consumption or production pair. Stage 4D therefore uses observed-channel energy and
does not persist another series to manufacture that strict total.

**Calendar and resource.** `GET /api/v1/report` accepts `period=day|week|month` with an anchor
`date=YYYY-MM-DD`, or `period=custom&from=YYYY-MM-DD&to=YYYY-MM-DD` (`to` exclusive, at most 31
local days). Day is `[midnight(D), midnight(D+1))` in `Europe/Warsaw` (23/24/25 h). Week is the
ISO Monday–Sunday local week (167/168/169 h). Month is the local calendar month (possible UTC
hours include 672, 696, 720, 743, 744 and 745). The anchor may fall anywhere inside its period.
All boundaries are whole UTC hours, including on DST changes. Time-of-day inputs and minute-level
report edges are rejected; custom is not an instant-range API. The response emits every calendar
bucket: `1h` for day and `1d` for week, month and custom. A custom range may be empty of data but
cannot have zero calendar days. No `/daily-report`, `/period-report`, `/statistics-report`,
`/report-plan` or `/report-matrix` resource is introduced.
Calendar resolution does not depend on the installation date or whether stored evidence exists.
A settled period with no recorded rows is a normal report: recorded minutes are zero, settled
minutes are gaps, and unmeasured energy, COP and temperatures remain `null`. Only an actual
calendar/time conversion failure or a Warsaw boundary that cannot align to the frozen whole UTC
hour grid is technically unrepresentable; no report-specific year range is imposed.

**One interpretation.** The Stage 4C versioned classes `off`, `idle`, `co`, `dhw`, `transition`,
`defrost`, `unknown` and compressor states `off`, `on`, `unknown` are the only activity truth.
Reports reuse the stored/reconstructed Stage 4C segments and span builders; they never classify
independently from power thresholds, compressor heuristics, valve hints, operating mode or legacy
state strings. No smoothing, blip merging, dominant activity or mixed heuristic is applied.
The owner-selected heating set is `H* = {co, dhw, transition}`. It defines useful heat and the
headline heating COP; `defrost`, `idle`, `off` and `unknown` stay separately visible.

**Energy knownness and shared serializer.** For a settled range or bucket `R`, let `rec(R)` be
recorded canonical minutes, excluding no-row gaps. Every one of
`co_power_consumption`, `co_power_production`, `dhw_power_consumption`,
`dhw_power_production` serializes as `channels[ch] = {kwh, minutes, unknown_minutes}`:

```text
kwh             = Σ known minute-average W / 60000; null if minutes = 0
minutes         = number of recorded minutes with this channel known
unknown_minutes = rec(R) - minutes
```

Known `0 W` increments `minutes` and contributes zero kWh. `NULL` in a recorded minute increments
`unknown_minutes`; a no-row minute belongs to coverage gaps, not channel unknownness. Aggregate
`consumption` uses the two consumption channels and `production` the two production channels:

```text
{observed_kwh, unknown_channel_minutes}
observed_kwh            = sum of actually observed constituent-channel energy
unknown_channel_minutes = Σ(rec(R) - constituent channel.minutes), in 0..2*rec(R)
```

`observed_kwh` is `null` only when neither constituent channel has a known minute. Zero
`unknown_channel_minutes` means both channels are complete over every recorded minute; a positive
count leaves observed energy true but incomplete as a total. Do not call it `total_kwh` or assign a
subjective completeness status. `pair_co_in/out`, `pair_dhw_in/out` and `pair_total_in/out` are
**COP evidence only**, never ordinary energy-knownness evidence. In particular, `pair_total`
requires all four channels; an unknown production channel must not erase valid observed
consumption energy.

**COP.** Every COP is `{cop, paired_minutes, input_kwh, output_kwh}` and uses `Σ paired output /
Σ paired input`: `energy.cop.co` pairs both CO channels, `.dhw` both DHW channels, and `.total`
all four power channels. Known zeros are valid paired evidence. `cop` is `null` for no paired
minutes or zero paired input; kWh ingredients are `null` when there are no paired minutes. Never
average instantaneous or daily COP, or divide independently aggregated unpaired energy. The
ordinary energy serializer and COP serializer are shared by top-level, heating and every class.

**Activity energy.** `activity.heating.activities = ["co", "dhw", "transition"]` in that order.
`activity.heating` contains `minutes`, `channels`, `consumption`, `production` and `cop` using only
H* segments. Its production `observed_kwh` is useful heat: the observed CO + DHW production
inside H*, accompanied by the same `unknown_channel_minutes`. Its headline heating COP is
`activity.heating.cop.total`, calculated from the strict four-channel paired subset inside H*,
never `production.observed_kwh / consumption.observed_kwh`. Every Stage 4C class has
`activity.classes[A] = {minutes, channels, consumption, production, cop}` using the identical
serializer. Across all seven classes, minute and known/unknown counts partition the top-level
facts exactly, including `Σ class.minutes = recorded_minutes`, `Σ class channel.minutes =
channel.minutes`, `Σ class channel.unknown_minutes = channel.unknown_minutes`, and `Σ class
unknown_channel_minutes = top-level unknown_channel_minutes` for each aggregate. kWh comparisons
allow floating-point tolerance for changed summation association.

**Technical facts.** Only `outside_temp = {avg, min, max, minutes}` and `compressor_freq =
{avg, max, minutes, active_avg}` are included. `active_avg` is the known compressor-frequency
`Stats.sum / activity.compressor_minutes.on`, or `null` if there are no on minutes. This identity
is valid only when every recorded on minute has known frequency `>0`, every recorded off minute
has known frequency `0`, and unknown-frequency minutes are exactly compressor-unknown minutes.
The report verifies `compressor_freq.minutes = compressor_minutes.on + compressor_minutes.off`
per composed range/bucket; an inconsistency fails 500 rather than producing `active_avg`.
No `compressor_freq_p95`, `active_p95`, percentile approximation, histogram, raw-only report
field or non-durable active minimum is exposed. Detailed temperatures and pump measurements remain
in `/history`.

**Coverage and current period.** For every calendar bucket `[a,b)`, take one observation with
`C = closed_until` from the recorder and `F = floor_minute(now)`, with `C ≤ F`. Counts are lengths
in minutes of intersections:

```text
settled_minutes   = |[a,b) ∩ (-∞,C)|
unsettled_minutes = |[a,b) ∩ [C,F)|
future_minutes    = |[a,b) ∩ [F,+∞)|
calendar_minutes  = settled_minutes + unsettled_minutes + future_minutes
recorded_minutes  = canonical rows in the settled part only
gap_minutes       = settled_minutes - recorded_minutes
coverage_percent  = round(100 * recorded_minutes / settled_minutes, 1), or null if settled=0
effective_to      = min(report_to, max(report_from, C))
```

The clamp applies only to the settled extraction endpoint, never to the actual Stage 4C
`closed_until` frontier: `report_from <= effective_to <= report_to`. If `C >= report_to`, a
fully historical report ends at `report_to`. If `report_from < C < report_to`, a partially settled
report ends at `C`. If `C <= report_from`, including a fully future report, `effective_to` equals
`report_from` and the settled extraction range is empty. If the whole period is after
`F`, it has zero settled and unsettled minutes, all calendar minutes future, and null coverage.
The coverage intersections above are unchanged. The current partial minute is future. A
waiting/protected unacknowledged tail is unsettled, never a gap. Future buckets contain no
fabricated data. Counts and coverage are facts, with no
`ready`, `partial`, `full`, `good`, `bad` or 95% verdict. As with existing history, clock-step
anomalies are surfaced rather than silently clamped.

**Spans and events.** Reuse the Stage 4C full spans and boundaries. `observed_starts` and
`observed_stops` keep their strict Stage 4C observed-boundary requirements, attributed to the
run's first and last minutes respectively. Complete runs and exact off intervals retain
first-minute attribution. `defrost_events` counts maximal observed defrost spans, and
`activity_events[A]` counts maximal observed Stage 4C activity spans of class `A`; each belongs
to the bucket holding its first observed minute. A span with an `observed`, `gap`, `unknown` or
`outside_evidence` left boundary counts once, but only `observed` proves its start. These generic
counts do not claim a physical/domain start. A span crossing adjacent buckets is counted only in
the bucket with that first observed minute. No separate observed-defrost-start field is needed.
`observed_defrost_seconds` is clipped to the bucket and adds exactly. `complete_runs` and
`exact_off_intervals` each report `count`, `total_minutes`, `min_minutes`, `max_minutes`,
`mean_minutes`; the report carries no `minutes[]` list. The overall
`compressor_runs_overlapping` and `defrosts_overlapping` are non-additive and never emitted per
bucket. A run crossing day/week/month boundaries is attributed once.
For any span intersecting the requested settled range, `outside_evidence` on the right is an
internal inconsistency. `outside_evidence` on the left is likewise refused because a Stage 4C
Timeline does not prove that any absolute evidence floor was reached. The pure composer fails
closed if a caller supplies an insufficiently widened timeline; this does not reinterpret Stage
4C boundaries or create a report calendar floor.

**Domain shape.** The response is `{period, observation, segment_rule_version, evidence,
totals, buckets}`. `period` names kind, resolved local `from_date`/exclusive `to_date`, UTC
`from`/`to`, timezone and bucket size; `observation` names `now`, `closed_until`, `effective_to`;
`evidence` gives the examined UTC window, including Stage 4C widening. Both `totals` and each
bucket carry the same FACTS: `coverage`, `energy`, `activity`, `events`, `technical`. Buckets also
carry `start`, `end`. Totals alone carry the overall overlap span counts. See `docs/API.md` for
exact field names and HTTP errors.

**Read consistency and current edge (4D-C).** In order: call `Recorder.settled_before(clock)`;
release the recorder lock; open one `Storage.session()` and `START TRANSACTION WITH CONSISTENT
SNAPSHOT`; extract history; extract activity and widen evidence; close the session; compose the
report purely. No DB I/O under the recorder lock. Do not call the public `/history` and
`/activity` independently. Refactor `history.py` toward a caller-session-aware canonical partial
extractor that owns raw/rollup resolution, purged-raw semantics, series loading and folding to
caller bucket edges; the existing route wraps the same logic. Refactor `activity_history.py`
toward a caller-session-aware timeline loader with a report-local `raw_edge` option; the existing
route keeps its old behavior and bytes. The report-local loader must cover the full settled
report interval even when `period.start < 0`; it must not inherit `/activity`'s Unix-0 left
clamp. A pre-Unix-0 interval with no stored evidence is ordinary gap evidence, not an error or
activity-unavailable history. This requires no new stored rows and does not change `/activity`.
Exact Python names are implementation choices. If `C`
is not hour-aligned, Stage 4D may source `floor_hour(C)` from raw minutes strictly below `C`,
even when earlier hours use durable segments. This edge rule does not alter `/activity` and reuses
its classification, `build_segments` and energy fold. A purged current edge that cannot be
represented is 422. Within the one snapshot, compare history recorded counts with activity
recorded-minute counts **per hour** in the requested settled portion; mismatch is corruption and
fails 500. A request intersecting Stage 4C activity-unavailable history fails 422 as a whole,
never returns numeric energy with `activity=null`; `/history` remains independently available.
Invalid durable activity fails 500, and DB outage 503.

**Performance and compatibility.** A custom request is at most 31 local days (≤745 UTC hours).
The selected canonical rollup series need about 9.7k rows at that bound. Typical activity is
about 0.9k segments/month; pathological minute flapping can reach about 44.7k. Read indexed
ranges, compute spans once and attribute them to buckets in one pass; never call whole-timeline
`summarize()` for every bucket. No daily table, cache or materialization. Stage 4D changes none of
the public `/history`, `/activity`, `/activity/live`, `/live`, `/metrics`, `/status` response
contracts. Golden byte-identical `/history` and `/activity` tests are a 4D-C merge gate.

**Checkpoint gates.** 4D-A freezes this architecture and the API contract for owner review,
without production code. 4D-B adds a pure report domain (`pompa/report.py` or equivalent):
calendar/bucket resolution, H*, coverage, shared energy/COP/class serializers and one-pass span
attribution, with no SQL or HTTP. Hand-calculated fixtures and an independent brute-force
reference verify knownness, partitions, DST and one-pass equivalence. 4D-C first makes the
session-aware extraction refactor independently reviewable as behavior-preserving, then adds one
snapshot, report-local edge, consistency guard and `/api/v1/report`; gate on byte-identical old
endpoints, report API tests, MariaDB green and a real snapshot race. 4D-D checks raw/rollup,
raw/durable and post-purge equality, DST, F2 current-tail behavior, races and bounded query
count; CT109 smoke and whole-PR adversarial review precede the owner's merge decision. That CT109
deployment first delivers the already-reviewed F2 correction.

The later test matrix includes independent per-channel knownness; known zero vs `NULL` vs no row;
observed aggregates; paired COP; H* useful heat; class partition identities; event first-minute
attribution; runs and defrosts crossing calendar boundaries; Warsaw 23/25 h days, 167/169 h
weeks, 743/745 h months, ISO week at year boundary and February; raw vs rollup and raw vs durable
activity; post-purge equality; activity-unavailable 422, corrupt activity 500 and per-hour
consistency 500; waiting/protected unsettled tails and future bucket partition; a real MariaDB
one-snapshot race, bounded query count and old endpoint byte compatibility.

**Deliberate product exclusions.** Targeted legacy report inspection supplied product evidence,
not implementation authority. Stage 4D drops legacy P95, dominant activity, report readiness
verdicts and 95% thresholds, smoothing/blip merging, UTC or rolling 24h/7d/30d report calendars,
average instantaneous COP, unpaired COP ratios, daily rollup and 5m report layers, report
planner/matrix, technical production composite, `active_day_cop` and differing legacy
`total_day_cop` semantics. Stage 4D excludes SET/control (4E), frontend (5), cooling/heater,
cost/tariff and external SDM analytics, year/season reports and legacy migration/backfill.

### 25.5 Stage 4E — isolated control and final backend API

**4E-A contract freeze (research and documentation only).** Stage 4E is the last backend stage
before the Stage 5 frontend. It adds one isolated command path. Frontend code sends semantic
commands and renders backend facts. It never sees MQTT topics, SET numbers, raw HeishaMon payloads
or `SetCurves` JSON, and it performs no device validation. Items marked **(O)** are open owner
decisions, not frozen facts. Each names the checkpoint that must resolve it. Checkpoint 4E-B
resolved every 4E-B item from evidence (§25.5.14). Only the readback window `W` remains open,
for 4E-D.

**Owner decisions accepted in the 4E-A review (frozen):**

- **Transport.** Publishing uses the existing shared paho MQTT client and connection, with QoS 0
  and `retain=false`, and only while connected. One validated request makes at most one publish.
  There is no automatic retry, replay or reconciliation (§25.5.4, §25.5.9).
- **Persistence.** NO COMMAND PERSISTENCE (§25.5.10).
- **Completeness.** The backend exposes the full evidenced HeishaMon control capability,
  independent of Home Assistant's disabled-by-default registry flag (§25.5.3, §25.5.5).
- **Service marker.** Service and high-impact is factual metadata only, not a backend safety
  policy (§25.5.5).
- **Curves.** The four semantic curve controls are the accepted curve model (§25.5.11).
- **HA retained commands** are an operational issue owned by an external writer (§25.5.9):
  - Pompa Next never publishes retained commands.
  - Pompa Next does not fight, reconcile or automatically clear HA's retained commands.
  - CT109 validation audits retained `commands/#` topics before any write.
  - Any retained-topic cleanup is an explicit owner-approved operational action.

#### 25.5.1 Evidence base

The research used these pinned sources:

- **Tracked references:** `docs/reference/heishamon/MQTT-Topics.md` (SET1–SET46) and the owner
  device snapshot `realne_dane.md`.
- **Upstream HeishaMon:** `heishamon/HeishaMon` (the historical `Egyras/HeishaMon` URL redirects
  to the same repository), release v4.2.2, commit
  `0de4f3c02598f542e7859a772e627e5c1ebc2ce3`. Files: `HeishaMon/commands.h`/`commands.cpp`
  (command tables and encoders), `HeishaMon/HeishaMon.ino` (MQTT subscription, callback and serial
  queue), `MQTT-Topics.md` and `OptionalPCB.md`.
- **Home Assistant integration:** `kamaradclimber/heishamon-homeassistant` (domain `aquarea`,
  shown as "HeishaMon") at `e206e023af5453e4fefbbf3545e9c60d5ca79fd5`, v2.10.1. The owner's HA
  entity names match this integration. The installed version on the owner's system is not
  recorded.
- **paho-mqtt 2.1.0 source:** the version pinned in `backend/requirements.txt`.
- **Legacy:** targeted product inspection of `shockwave9315/pompa` at
  `d646615d8ac536684de644797be005e549b785db` (§25.5.6).
- **Owner product evidence:** HA entities the owner actively uses, and entities that were disabled
  by default, manually enabled and reported working on this K-series installation.

This is research evidence. The runtime never fetches upstream files.

#### 25.5.2 Tracked reference audit

- **Heat-pump commands.** The tracked table has 46 SET rows. Upstream firmware `commands[]` has
  48 heat-pump commands: those 46 plus `SetForceHeater` (upstream SET47, added in v4.2.0 by commit
  `aa42c21`) and `SetReset` (upstream SET48). The `SetReset` encoder has existed since 2021; it was
  documented in v4.2.1. All 46 tracked command names match the firmware dispatch names exactly.
- **Optional PCB commands.** Firmware `optionalCommands[]` has 14 more commands. The tracked
  reference contains none of them. Tracked OPT0–OPT6 are readable heat-pump→PCB outputs, not
  commands.
  - Upstream `OptionalPCB.md` names 13 of the 14 in its set-command table.
  - `SetOptPCBByte9` exists only in the firmware: the table documents datagram byte 09 only as "?".
  - This corrects the 4E-A wording "upstream documents them in `OptionalPCB.md`"; the model is
    unchanged.
- **Other drift.** Everything else is descriptive only: TOP15/16/38–41 warnings, and an upstream
  XTOP0–XTOP5 table matching the XTOP identities Pompa Next already observes.
- **Decision.** Do not edit the packaged reference in 4E-A. It is a startup runtime input
  (§25.1). Adding SET47/SET48 changes `/metrics?include=capabilities` from 203 to 205 entries and
  breaks pinned tests, which is production behavior. Checkpoint 4E-B refreshes `MQTT-Topics.md`
  verbatim from the pinned upstream commit and adds a verbatim `OptionalPCB.md`. The parser, tests
  and API counts change in the same reviewable checkpoint.
- **Done in 4E-B.** Both files are verbatim copies at the pinned commit; their git blob ids equal
  upstream's (`docs/reference/heishamon/PROVENANCE.md`). The catalog grew from 203 to 218 entries
  (+SET47, +SET48, +13 PCB). The only change to earlier identities is the six upstream TOP15/16/38–41
  warnings, which a regression test pins.

#### 25.5.3 Home Assistant integration findings

**Why entities are disabled by default.** The integration sets `entity_registry_enabled_default`
to false on the cooling requests (SET6/SET8), SET19, SET21–SET23, SET27, the cooling-curve numbers,
the cooling climates, SET30–SET33, SET43, Demand Control, Smart Grid Mode, Compressor State and the
HeishaMon GPIO relays. Its source comments give the reasons:

- less common setups: cooling, buffer, solar, pool;
- the command comes from the optional PCB;
- SET43 applies only to K/L All-In-One units;
- Compressor State only acts when SET32 is on and a main-PCB DIP switch is set;
- climate entities are shown for heating only.

SET30–SET33 are disabled without a comment.

**Disabled-by-default is not "unsupported".** The flag only sets the initial HA entity-registry
enablement:

- Command topic, payload mapping and state subscription are defined identically either way.
- There is no model or series gate.
- These entities define no availability property. HA availability is therefore independent of
  both registry enablement and device capability.
- Manual enablement instantiates the same entity. The owner confirmed that such entities operate
  on this installation.

Disabled-by-default is UX evidence only and never a negative capability signal.

**HA defects and limits.** These are evidence, not authority:

- **SET37/SET38 writes never reach the heat pump.** HA publishes to
  `SetBivalentAStartTemp`/`SetBivalentAStopTemp` (since v1.15.x, January 2025). The firmware names
  are `SetBivalentAPStartTemp`/`SetBivalentAPStopTemp`, and unknown names are ignored silently. HA
  therefore never delivers these writes. The owner's use of these entities is not evidence that
  the writes worked.
- **Retained commands.** HA publishes SET36, Demand Control, Smart Grid Mode, Compressor State
  and the relays with `retain=true`. HeishaMon subscribes to `commands/#`, so it re-executes a
  retained command on every (re)connect. A later Pompa Next write to SET36 can therefore be
  reverted by HA's retained value.
- **HA PCB state is an echo.** HA's state for the optional-PCB entities is the echo of its own
  retained command topic, not a heat-pump readback.
- **Undocumented values.** The HA operating-mode select can write `7`/`8`; the firmware treats
  these as "no change". HA quiet-mode "Scheduled" writes `4`, which upstream does not document.
- **No write entity.** HA has none for SET14, SET46 (TOP78 is a read-only sensor there),
  `SetHeatCoolMode`, the thermostat inputs, the PCB temperature inputs or PCB byte 9.
- **Composite entities.** HA climate and water-heater entities combine SET1, SET9, SET17 and
  request temperatures, with client-side mode guessing.
- **Automatic retry.** HA re-publishes a command up to three times after about 10 s when the
  state does not match.

Pompa Next adopts neither the composite entities nor the automatic retry.

#### 25.5.4 MQTT write transport (proved)

- **Topic and payload.**
  - Every command, heat-pump or Optional PCB, is published to
    `{MQTT_TOPIC_PREFIX}/commands/{UpstreamName}`. HeishaMon strips the base and `commands/` and
    dispatches by exact name; an unknown name is ignored silently.
  - Payloads are UTF-8 text. Heat-pump commands and PCB enum/byte commands are parsed with Arduino
    `String.toInt()`. PCB temperatures are parsed with `toFloat()`. `SetCurves` is a JSON document.
- **The firmware does not validate.**
  - Out-of-range integers wrap into a byte, and fractions truncate.
  - Most two-state commands treat any value other than `1` as `0`, so `SetHeatpump=2` means off.
  - Unmapped enum values fall back to "no change".
  - Backend validation is therefore the only validation.
- **HeishaMon delivery.**
  - HeishaMon subscribes with PubSubClient's default QoS 0 and a clean session. Broker-to-HeishaMon
    delivery is at most once.
  - A command published while HeishaMon is offline is lost unless it is retained. A retained
    command is re-executed at every reconnect.
  - HeishaMon handles one MQTT callback at a time and drops a re-entrant one. It buffers at most
    ten heat-pump commands and drops overflow with only a log line. It ignores commands in
    listen-only mode.
  - It publishes no acknowledgement topic. Its optional MQTT `log` text is diagnostics, not an
    acknowledgement.
  - After a heat-pump command, changed decoded values are published retained under `main/`.
    HeishaMon polls every `waitTime` (default and minimum 5 s) and republishes all values every
    `updateAllTime` (default 300 s).
- **Optional PCB behavior.**
  - A PCB command sends nothing on its own. It edits an in-memory datagram that HeishaMon sends
    every second while its own Optional PCB emulation setting is on. HeishaMon saves this datagram
    to flash every 5 minutes and restores it at boot. Without that setting, PCB commands are
    ignored.
  - The heat pump must also have Optional PCB = YES in its service settings (readable as TOP110).
    Otherwise the command has no effect.
  - If the heat pump has Optional PCB = YES and the datagram stops for about 40 s, the heat pump
    raises error H74 and goes to standby.
  - The emulated input values are never published back.
- **paho 2.1.0 semantics.**
  - A QoS 0 publish while disconnected returns `MQTT_ERR_NO_CONN` and queues nothing.
  - While connected, `MQTT_ERR_SUCCESS` only means the packet was queued on the client's current
    connection. There is no broker receipt.
  - QoS ≥ 1 messages published while disconnected, or in flight at a disconnect, are kept and
    (re)sent after reconnect, flagged as duplicates, even with a clean session. That could execute
    a command long after the API answered, or twice.
  - A QoS 1 broker acknowledgement would prove only broker receipt, not HeishaMon delivery,
    because HeishaMon subscribes at QoS 0.
- **Frozen transport:**
  - QoS 0, `retain=false`, publish only while the MQTT adapter is connected.
  - One validated request makes exactly one `publish()` call or none.
  - Nothing is queued, retried or replayed after a disconnect, reconnect or restart.
  - Publishing uses the existing shared paho client and connection (one process, one client id).
    `publish()` is thread-safe. Accepted by the owner.
- **Control/recorder boundary (frozen):**
  - The pure control domain and definitions never import or call `Recorder`.
  - The API/runtime layer obtains the existing immutable live-readings observation through the
    already-existing live path, the same observation `/live?include=readings` uses. It passes
    those facts into control validation and readback evaluation.
  - Control never changes recorder state, the waiting or protected queues, history or
    persistence. It never calls storage, history or aggregation code.
  - The recorder never depends on control, and a control failure cannot affect ingest.
  - 4E-A introduces no new live-state abstraction and no refactor.
- **Command echoes.** Pompa Next subscribes to `{prefix}/#`. Its own command publications, and
  HA's (including HA-retained command topics), therefore reach ingest as uncatalogued topics. They
  carry no source life, are not readings and never confirm a command. Stage 4E does not change
  ingest.

#### 25.5.5 Complete command inventory and control definitions

**Counts.** 62 upstream command names total = 48 heat-pump commands + 14 Optional PCB commands.
Pompa Next defines 63 semantic controls = 51 heat-pump controls + 12 Optional PCB controls,
because `SetCurves` becomes four curve controls and two Optional PCB commands have no validated
meaning and are not exposed. `SetOptPCBByte9` is a raw byte that upstream documents only as "?".
`SetHeatCoolMode` is a known catalog command whose bit polarity is undocumented (§25.5.14 O4). The
following are not heat-pump commands and are out of scope:

- `SendRawValue` (raw serial bytes);
- `gpio/…` relays;
- OpenTherm topics;
- the HeishaMon HTTP `/command` interface;
- HeishaMon reboot and settings.

**One definition per control.** A 4E-B definition module is the single owner of:

- the stable semantic `key`;
- the Stage 4A capability `identity`;
- the upstream name (and so the topic);
- the class;
- the value schema, validation and payload encoding;
- the readback identity and its mapping;
- documented prerequisites and restrictions.

Nothing is duplicated in API, MQTT, frontend or test code. Tests derive from these definitions and
check them against the tracked references: every tracked command is defined or explicitly excluded.

**Identities.** Heat-pump controls reference the existing SET identities. Optional PCB commands
have no upstream IDs. 4E-B adds them to the capability catalog with the family `PCB` and the
upstream command name as identity (for example `SetSmartGridMode`). This is resolved in 4E-B.
The name cannot match the `TOP|OPT|SET|XTOP<n>` grammar, is deterministic across restarts, is not
a display string, and is the only representation. No numeric PCB id is invented.

**Keys and values.** Keys and enum ids are language-neutral snake_case identifiers. Units are
symbols (`°C`, `K`, `min`, `%`, `duty`). HA labels and legacy Polish labels are not identities, and
the backend carries no display strings. Numeric heat-pump values are integers only, because the
firmware truncates. PCB temperatures are finite numbers in −78..120 °C, the range firmware
`temp2hex()` converts without clamping. The resulting NTC byte quantizes non-uniformly (about
0.3 °C to 11 °C per step; −78 °C itself encodes as `0xFE`), so no step is claimed.

**Classes.** Five classes, derived from firmware behavior:

- `setting`: an absolute value that persists until changed. Idempotent. It has a state readback
  where one exists; SET14 `pump_service_mode` has none.
- `temporary`: an absolute state request that the heat pump ends by itself. Re-sending while active
  is harmless; re-sending after it ended starts it again.
- `trigger`: only "start" is meaningful, because firmware encodes `0` as "no change". Not
  idempotent: every request triggers again.
- `curve`: a structured `SetCurves` partial update.
- `pcb_input`: a held Optional PCB emulated input with no readback.

A separate `service` fact marks commands that upstream describes as service mode or menu, forced
routines, emergency heater or fault reset. It is not a safety verdict and is not enforced.

Legend for the tables below. **Owner:** `U` = actively used; `U*` = used through an HA entity whose
writes cannot reach the firmware; `T` = disabled in HA, manually enabled, reported working;
`–` = no owner HA-write evidence. The owner may still use a physical setting outside HA.
**HA:** `on`/`off` = entity enabled/disabled by default; `none` = no HA write entity. Readback `=`
means the readable TOP value equals the encoded value.

| Key | Upstream | Class | Value → payload | Readback | Prerequisites / notes | Owner | HA |
|---|---|---|---|---|---|---|---|
| `heat_pump_power` | SET1 `SetHeatpump` | setting | bool → `0`/`1` | TOP0 = | — | U | on |
| `holiday_mode` | SET2 `SetHolidayMode` | setting | bool | TOP19: off `0`; on `1` or `2` | — | U | on |
| `quiet_mode` | SET3 `SetQuietMode` | setting | `off`,`level_1`,`level_2`,`level_3` → `0`–`3` | TOP18 = | HA-only `4` excluded | U | on |
| `powerful_mode` | SET4 `SetPowerfulMode` | temporary | `off`,`min_30`,`min_60`,`min_90` → `0`–`3` | TOP17 = | ends by itself | U | on |
| `zone1_heat_request` | SET5 | setting | int; TOP76=`0`: shift −5..5 K; `1`: direct 20..127 °C (max = protocol bound) | TOP27 = | range needs TOP76; device maximum undocumented; restriction `documented_direct_temperature_water_mode_only` (§25.5.7) | U | on |
| `zone1_cool_request` | SET6 | setting | int; TOP81=`0`: shift −5..5 K; `1`: direct 5..20 °C | TOP28 = | range needs TOP81; direct range from the TOP28/TOP35 text (the SET6/SET8 rows repeat "20 to max"); restriction `documented_direct_temperature_water_mode_only` (§25.5.7) | T | off |
| `zone2_heat_request` | SET7 | setting | as `zone1_heat_request` | TOP34 = | TOP76; restriction `documented_direct_temperature_water_mode_only` (§25.5.7) | U | on |
| `zone2_cool_request` | SET8 | setting | as `zone1_cool_request` | TOP35 = | TOP81; restriction `documented_direct_temperature_water_mode_only` (§25.5.7) | T | off |
| `operation_mode` | SET9 | setting | `heat`,`cool`,`auto`,`dhw`,`heat_dhw`,`cool_dhw`,`auto_dhw` → `0`–`6` | TOP4: `auto`→{2,7}, `auto_dhw`→{6,8}, else = | — | U | on |
| `force_dhw` | SET10 | temporary | bool | TOP2 = | effective only if TOP4 ∈ {3,4,5,6,8}; `false` deasserts the force request and does not stop a running DHW cycle | U | on |
| `dhw_target_temperature` | SET11 | setting | int 40..75 °C | TOP9 = | HA offers 40..65 | U | on |
| `force_defrost` | SET12 | trigger, service | trigger → `1` | effect TOP26=1 | — | U | on |
| `force_sterilization` | SET13 | trigger, service | trigger → `1` | effect TOP69=1 | — | U | on |
| `pump_service_mode` | SET14 `SetPump` | setting, service | bool | none | service mode, max speed | – | none |
| `max_pump_duty` | SET15 | setting, service | int 64..254 `duty` | TOP95 = | service-menu value | U | on |
| `zone1_heat_curve` | SET16 `SetCurves` | curve | §25.5.11 → `zone1.heat` | TOP29–TOP32 | fields −127..127 °C (protocol bound) | U | on |
| `zone1_cool_curve` | SET16 | curve | → `zone1.cool` | TOP72–TOP75 | fields −127..127 °C (protocol bound) | T | off |
| `zone2_heat_curve` | SET16 | curve | → `zone2.heat` | TOP82–TOP85 | fields −127..127 °C (protocol bound) | U | on |
| `zone2_cool_curve` | SET16 | curve | → `zone2.cool` | TOP86–TOP89 | fields −127..127 °C (protocol bound) | T | off |
| `active_zones` | SET17 `SetZones` | setting | `zone1`,`zone2`,`zone1_zone2` → `0`–`2` | TOP94 = | — | U | on |
| `heat_delta` | SET18 `SetFloorHeatDelta` | setting | int 1..15 K | TOP23 = | — | U | on |
| `cool_delta` | SET19 `SetFloorCoolDelta` | setting | int 1..15 K | TOP24 = | — | T | off |
| `dhw_heat_delta` | SET20 | setting | int −12..−2 K | TOP22 = | — | U | on |
| `heater_delay_time` | SET21 | setting | int 0..254 `min` (protocol bound) | TOP96 = | MQTT-Topics "only J-series" vs ProtocolByteDecrypt "J/K/L"; snapshot 20 | T | off |
| `heater_start_delta` | SET22 | setting | int −127..127 K (protocol bound) | TOP97 = | J-only vs J/K/L docs; snapshot −4 | T | off |
| `heater_stop_delta` | SET23 | setting | int −127..127 K (protocol bound) | TOP98 = | J-only vs J/K/L docs; snapshot −1 | T | off |
| `main_schedule` | SET24 | setting | bool | TOP13 = | — | U | on |
| `alt_external_sensor` | SET25 | setting | bool | TOP108 = | — | U | on |
| `external_pad_heater` | SET26 | setting | `disabled`,`type_a`,`type_b` → `0`–`2` | TOP114 = | — | U | on |
| `buffer_delta` | SET27 | setting | int 0..10 K | TOP113 = | — | – | off |
| `buffer_installed` | SET28 `SetBuffer` | setting | bool | TOP99 = | — | U | on |
| `heating_off_outdoor_temperature` | SET29 `SetHeatingOffOutdoorTemp` | setting | int 5..35 °C | TOP77 = | HA name: heating cutoff | U | on |
| `external_control` | SET30 | setting | bool | TOP119 = | — | T | off |
| `external_error_signal` | SET31 `SetExternalError` | setting | bool | TOP121 = | — | T | off |
| `external_compressor_control` | SET32 | setting | bool | TOP122 = | TOP122 doc: optional-PCB setting | T | off |
| `external_heat_cool_control` | SET33 | setting | bool | TOP120 = | TOP120 doc: optional-PCB setting | T | off |
| `bivalent_control` | SET34 | setting | bool | TOP129 = | — | U | on |
| `bivalent_mode` | SET35 | setting | `alternative`,`parallel`,`advanced_parallel` → `0`–`2` | TOP130 = | — | U | on |
| `bivalent_start_temperature` | SET36 | setting | int −15..35 °C | TOP131 = | HA publishes it retained | U | on |
| `bivalent_advanced_start_temperature` | SET37 `SetBivalentAPStartTemp` | setting | int −15..35 °C | TOP134 = | HA topic name wrong | U* | on |
| `bivalent_advanced_stop_temperature` | SET38 `SetBivalentAPStopTemp` | setting | int −15..35 °C | TOP135 = | HA topic name wrong | U* | on |
| `heating_control` | SET39 | setting | `comfort`,`efficiency` → `0`/`1` | TOP139 = | — | U | on |
| `smart_dhw` | SET40 | setting | `variable`,`standard` → `0`/`1` | TOP140 = | — | U | on |
| `quiet_mode_priority` | SET41 | setting | `sound`,`capacity` → `0`/`1` | TOP141 = | — | U | on |
| `pump_flowrate_mode` | SET42 | setting | `delta_t`,`max_duty` → `0`/`1` | TOP106 = | TOP106 "J-series only" vs ProtocolByteDecrypt "J/K/L" | U | on |
| `dhw_sensor_selection` | SET43 | setting | `top`,`center` → `0`/`1` | TOP143 = | doc: K/L All-In-One only | – | off |
| `dhw_heater_allowed` | SET44 `SetDHWHeaterState` | setting | `blocked`,`free` → `0`/`1` | TOP58 = | — | U | on |
| `room_heater_allowed` | SET45 `SetRoomHeaterState` | setting | `blocked`,`free` → `0`/`1` | TOP59 = | — | U | on |
| `heater_on_outdoor_temperature` | SET46 `SetHeaterOnOutdoorTemp` | setting | int −15..20 °C | TOP78 = | TOP78: outdoor threshold below which the backup heater is allowed; HA: TOP78 is read-only | – | none |
| `force_heater` | SET47 `SetForceHeater` | setting, service | bool | TOP68 = | firmware ≥ v4.2.0 | U | on |
| `fault_reset` | SET48 `SetReset` | trigger, service | trigger → `1` | none (TOP44 related) | — | – | on |

Optional PCB controls. All are class `pcb_input`, have no readback, and share these
prerequisites: HeishaMon Optional PCB emulation enabled (not observable over MQTT), and the heat
pump's Optional PCB setting TOP110 = `1` (observable).

| Key | Upstream | Value → payload | Notes | Owner | HA |
|---|---|---|---|---|---|
| `pcb_compressor_switch` | `SetCompressorState` | bool | prerequisite TOP122 = `1` (SET32 on); effect also needs a main-PCB DIP switch (HA source) | T | off |
| `pcb_smart_grid_mode` | `SetSmartGridMode` | `normal`,`capacity_1`,`hp_dhw_off`,`capacity_2` → `0`–`3` | — | T | off |
| `pcb_thermostat1_demand` | `SetExternalThermostat1State` | `none`,`cool`,`heat`,`heat_cool` → `0`–`3` | doc: H/J series only | – | none |
| `pcb_thermostat2_demand` | `SetExternalThermostat2State` | as thermostat 1 | — | – | none |
| `pcb_demand_control` | `SetDemandControl` | percent ∈ {5,25,50,75,100} → `43`,`82`,`133`,`184`,`235` | documented table points only; the firmware default byte 0xEB confirms 100 % | T | off |
| `pcb_pool_temperature` | `SetPoolTemp` | number −78..120 °C | — | – | none |
| `pcb_buffer_temperature` | `SetBufferTemp` | number −78..120 °C | doc: H/J series only | – | none |
| `pcb_zone1_room_temperature` | `SetZ1RoomTemp` | number −78..120 °C | doc: H/J series only | – | none |
| `pcb_zone1_water_temperature` | `SetZ1WaterTemp` | number −78..120 °C | — | – | none |
| `pcb_zone2_room_temperature` | `SetZ2RoomTemp` | number −78..120 °C | — | – | none |
| `pcb_zone2_water_temperature` | `SetZ2WaterTemp` | number −78..120 °C | — | – | none |
| `pcb_solar_temperature` | `SetSolarTemp` | number −78..120 °C | — | – | none |

`SetHeatCoolMode` (bit 7 of byte 06, "Heat/Cool SW") stays a known `PCB` catalog capability but
is not a control: no pinned source says which bit value selects heat or cool (§25.5.14 O4).

**Ranges.** They come from the upstream reference, where the tracked and upstream text agree.

**Value questions.** 4E-B resolved all of them from evidence (§25.5.14). Where upstream documents no
range, the control accepts exactly the encodable protocol range and reports `range_basis:
"protocol"`. It never adopts a Home Assistant or integration-example range as protocol truth.

#### 25.5.6 Owner K-series evidence and legacy

**Device.** The owner snapshot identifies the unit only by TOP92 bytes
`E2 D5 0B 08 95 02 D6 0F 68 95`. Neither the upstream nor the HA model table lists them; the
nearest entries are split K-series units. The snapshot shows these settings:

| Reading | Value |
|---|---|
| TOP110 (Optional PCB) | `0` |
| TOP76, TOP81 (heating/cooling mode) | `0` (compensation curve) |
| TOP94 (active zones) | `0` (zone 1) |
| TOP96–TOP98 (heater delay/start/stop, documented J-series only) | 20 / −4 / −1 |
| TOP143 (DHW sensor, documented All-In-One only) | `0` |

**Genuinely unknown until CT109 or the owner resolves it:**

- whether SET21–SET23, SET42 and SET43 change anything on this unit;
- the HeishaMon firmware version (SET47 needs ≥ v4.2.0; it is readable from the retained
  `{prefix}/stats` JSON, which Pompa Next does not parse);
- whether HeishaMon Optional PCB emulation is on;
- the physical effect of every PCB input. With TOP110 = `0` at snapshot time, upstream says they
  have no effect. The owner's "works" in HA is HA's retained echo.

**Proven inapplicable:** nothing. Undocumented values (HA's `7`/`8` operating modes, quiet `4`)
are not commands. `SetOptPCBByte9` and `SetHeatCoolMode` are excluded because their value
meaning is unknown, not because they are inapplicable.

**Legacy product evidence.** Legacy never published a command. It exposed a read-only "disabled
future" projection of ten control candidates: DHW temperature and delta, heat delta, Z1 heat
request, quiet mode, heating-off outdoor temperature, Force DHW, Force Defrost, operation mode and
heat-pump power.

- **Useful product evidence:**
  - the grouping DHW / heating / operation mode / advanced / service;
  - the idea that a small everyday subset is shown prominently while the complete catalog remains
    available;
  - a confirmation step for high-impact actions, as a frontend concern.
- **Legacy implementation debt, not adopted:**
  - hand-assigned `safe/medium/high` risk verdicts with speculative rationales;
  - a capability-catalog-driven mutation guard;
  - read-only safety flags in the API.
- **Not available from legacy:** command history, readback or acknowledgement experience, and
  device discoveries. Legacy never wrote.

#### 25.5.7 Applicability and executability

`known` means defined in §25.5.5. At request time the API/runtime layer takes the existing live
observation, the same one `/live?include=readings` uses, and passes it to the pure control domain.
The control domain evaluates three observable facts from it (boundary: §25.5.4):

1. **MQTT connected.** Required to publish.
2. **Documented prerequisites with an observable state:**
   - PCB controls need TOP110 = `1`;
   - `force_dhw` needs TOP4 ∈ {3,4,5,6,8}, for both values (owner decision, §25.5.14).

   A live reading of a documented state that proves the prerequisite false makes the control
   non-executable (`409`). An absent, stale or retained reading, the documented unknown value `-1`,
   or any other undocumented value (for example TOP110 = `2`), leaves it `unknown`:
   the request proceeds, and the response reports the fact. `pcb_compressor_switch` also needs
   TOP122 = `1`: external compressor control on, per the HA source comment and TOP122's
   optional-PCB description.
3. **Validation context.** Request temperatures need a live TOP76 (heat) or TOP81 (cool) reading
   to choose the shift or direct range. Without one, the request is refused (`409`), because the
   same number means different things in the two modes.

Documented restrictions whose state cannot be observed reliably are reported as facts and never
enforced: J-series only, All-In-One only, H/J only, firmware version, HeishaMon emulation, and the
water-sensor-mode note on direct request temperatures (below).

**Direct request temperature and zone sensor mode (owner decision, §25.5.14 F8).** Upstream
`MQTT-Topics.md` says that in water sensor mode with direct temperature the request-temperature
commands (SET5–SET8) set the absolute target. In internal/external thermostat or thermistor mode,
the direct temperature is stored in the curve's high target and is changed through `SetCurves`.
Upstream itself adds that newer heat-pump types may behave differently. The four request controls
therefore carry the static restriction `documented_direct_temperature_water_mode_only`. It
concerns the direct-temperature branch, not the command as a whole. It is metadata only: it is
never a prerequisite or validation context, never produces a `409`, and never infers the
installed model. The zone sensor-settings readings (TOP111/TOP112) are not read for it. Their
zone names differ between the documented table and the firmware; the Stage 4A handling of that
discrepancy is unchanged. There
is no fake device, series or firmware detection.

#### 25.5.8 Readback and confirmation

A request's facts are kept separate:

1. **Validation.** `400`/`404`/`409`/`422`, and nothing is published.
2. **Publish attempted.** Only while connected; otherwise `503` and nothing is published.
3. **Client accepted.** paho returns `MQTT_ERR_SUCCESS` (`publish.status = "sent"`). This proves
   neither broker receipt nor HeishaMon delivery. Any other return code means nothing was sent:
   `503`.
4. **Readback.** Only for definitions with a readback. The backend polls the same in-memory
   live-readings observation for up to `W` seconds. Only `mode="live"` readings count, never
   retained ones. Outcomes:
   - `matched`: a reading received at or after the publish time equals the expected mapped value.
     For every field of a curve. For a trigger, the expected effect state.
   - `unchanged_match`: a live reading already matched before the publish and nothing different
     arrived. The match cannot be attributed to this command, because HeishaMon publishes only
     changes.
   - `not_observed`: the window ended without a matching post-publish reading. The last observed
     reading is reported. This is a fact, not a failure verdict.
   - `not_applicable`: the definition has no readback.
5. **Physical execution.** Never claimed. A matching TOP is the heat pump's reported state, not
   proof of physical actuation.

**Readback strength.** A script over the pinned firmware compared each encoder's written protocol
byte with the byte decoded for its readback TOP:

- All 43 `setting`/`temporary` pairs with a readback TOP write and decode the same byte. These are
  true state readbacks. The curve fields have the same property.
- `force_defrost` and `force_sterilization` write byte 8. TOP26/TOP69 decode bytes 111/117.
  These TOPs are resulting machine states, not acknowledgements (readback kind `effect`).

`W` is one backend constant, provisionally 15 s: above two default HeishaMon query intervals plus
command queuing. The final `W` stays **(O)** until 4E-D measures the real latency on CT109. A
configuration setting is added only if that measurement requires one. The request thread waits synchronously. There is no background
task, job or command store. Trigger effects (TOP26/TOP69) may begin after the window.
`not_observed` states only that the effect was not observed within the window.

#### 25.5.9 Retry and idempotency

- **One validated API request → at most one publish.** Proved by the transport (§25.5.4): QoS 0,
  no retain, no paho queue.
- **No backend retry.** The backend never retries, replays or reconciles. Disconnect → `503`,
  never queued. Reconnect → nothing re-published. Backend restart → no pending command exists.
- **Client retries.** `POST` is not idempotent for triggers and re-arms temporaries. Clients must
  not retry a `POST` automatically. After a lost HTTP response, they read `GET /api/v1/controls`.
  Re-issuing a setting is safe by definition; re-issuing a trigger is a new trigger.
- **One in-flight request per key.** A second request for the same key while the first is within
  its window returns `409 command_in_progress`. Other keys proceed. This is one in-memory set, not
  a queue.
- **HA coexists on the same broker.** Its retained commands and its automatic retries can override
  Pompa Next writes. This is an operational issue owned by the external writer, not something
  Pompa Next handles (frozen owner decision). Pompa Next never publishes retained commands, and it
  never fights, reconciles or automatically clears retained topics. CT109 validation audits
  retained `commands/#` topics before any write. Any cleanup is an explicit owner-approved
  operational action.

**External writers and state synchronization (frozen).**

- **Ordinary controls with a TOP readback.** Pompa Next keeps no private desired-state copy.
  - HA, the heat-pump panel or another writer may change a setting.
  - HeishaMon then publishes the corresponding TOP, and the ordinary MQTT live-readings path
    updates `GET /api/v1/controls`.
  - HA coexistence therefore cannot make Pompa Next state stale.
- **Retained HA commands.** A retained HA command can be replayed after a HeishaMon reconnect and
  physically revert a Pompa Next write. When the corresponding TOP changes, Pompa Next observes and
  reports the reverted state. There is no reconciliation.
- **Optional PCB is the explicit exception.** Upstream publishes no readback of the emulated
  inputs. Pompa Next reports no physical state for `pcb_input` controls. It never treats HA's
  retained command echo, or any other `commands/…` topic, as physical state.

#### 25.5.10 Persistence — NO COMMAND PERSISTENCE

This is a frozen owner decision. No table, no command history and no pending-command recovery:

- A pending command cannot survive restart meaningfully, because QoS 0 has already delivered it or
  lost it.
- The current device state is always re-readable from `/api/v1/controls` and `/live`.
- Readback is bound to its own request window.
- Neither legacy nor any owner workflow shows a use for command history.

Each request writes one structured log line: key, requested value, publish result, readback outcome
and elapsed time. That is the operational record.

#### 25.5.11 Curve model

- **Four controls.** `zone{1,2}_{heat,cool}_curve`. The value is a partial object of integer °C
  fields `target_high`, `target_low`, `outside_high` and `outside_low`, with at least one field
  and no unknown fields.
- **Field meaning.** The TOP names give it, not the tracked descriptions: the descriptions of
  TOP31/TOP32 and TOP84/TOP85 are swapped relative to their names. The owner snapshot is
  consistent with the names (`target_high` 32 °C at `outside_low` −15 °C). For heating:
  - `target_high` is the water target at the curve's lowest-outside point (`outside_low`);
  - `target_low` is the water target at the curve's highest-outside point (`outside_high`).
  - Cooling follows its own TOP names.
- **Encoding.** The backend encodes only the given fields, as
  `{"zoneN":{"heat|cool":{"target|outside":{"high|low":v}}}}`. The firmware leaves omitted
  bytes unchanged. Unrestricted raw JSON is never accepted.
- **Invariants.** No cross-field invariant is documented upstream, and none is enforced (resolved
  in 4E-B). An ordering such as `outside_low < outside_high` would be HVAC policy, not protocol
  validation.
- **Readback.** Each field maps to its own TOP. The firmware writes and decodes the same protocol
  byte:

  | Field | zone1 heat | zone1 cool | zone2 heat | zone2 cool |
  |---|---|---|---|---|
  | `target_high` | TOP29 | TOP72 | TOP82 | TOP86 |
  | `target_low` | TOP30 | TOP73 | TOP83 | TOP87 |
  | `outside_high` | TOP31 | TOP74 | TOP84 | TOP88 |
  | `outside_low` | TOP32 | TOP75 | TOP85 | TOP89 |
- **Thermostat and thermistor modes.** Upstream notes that in these modes the direct room
  temperature is stored in the curve's `target_high`, so it is set through the curve control.

#### 25.5.12 Final control API (frozen in 4E-A, implemented in 4E-C)

- `GET /api/v1/controls` returns every control definition with its current readback state,
  prerequisites and executability, from one live observation without database I/O.
- `POST /api/v1/controls/{key}` validates, encodes, publishes at most once and returns the factual
  publish and readback result.

There is no generic MQTT publish, raw payload, per-panel endpoint or command history. The exact
shapes and errors are in `docs/API.md`.

**Complete backend gap audit.** After Stage 4E control, the planned Stage 5 views map to existing
resources:

| Stage 5 view | Resource |
|---|---|
| Teraz | `/live`, `/activity/live` |
| Historia | `/history` |
| Statystyki | `/report` |
| Cykle | `/activity` |
| Status | `/status`, `/health` |
| Capabilities and settings | `/metrics?include=…`, optional-history resources |
| Sterowanie | `/controls` |

Control is the only missing backend area. These are deliberately not added:

- a HeishaMon version or `stats` resource;
- a model-name table for TOP92;
- semantic enums for read-only TOPs without a control;
- command history;
- any convenience or aggregate resource.

A Stage 5 need for any of these requires new evidence. Accepted APIs are unchanged; 4E adds only the
`/controls` resources and, in 4E-B, the additive SET47/SET48/PCB capability identities.

#### 25.5.13 Remaining Stage 4E checkpoints

| Checkpoint | Scope | Likely files | Gate |
|---|---|---|---|
| **4E-B** pure control domain + reference refresh (OWNER ACCEPTED/CLOSED, §25.5.14) | Refresh tracked `MQTT-Topics.md` (SET47/48) and add `OptionalPCB.md` verbatim at the pinned upstream commit. Extend the parser with the `PCB` family. Add the pure definition module: validation, encoding, readback mapping, prerequisite and context evaluation from a readings snapshot. No MQTT, no API. | `docs/reference/heishamon/*`, `pompa/capabilities.py`, new `pompa/control.py`, capability and control tests | Golden encoding table per command against the firmware encoders; boundary, enum and type rejection; readback mapping; curve JSON; definition ↔ reference coverage; updated capability counts. Adversarial review of definitions vs firmware source. No CT109. Owner gate resolves every open value, identity and invariant question (§25.5.2, §25.5.5, §25.5.11) before the executable definitions are frozen. |
| **4E-C** publish path + API (implemented, §25.5.15; awaiting owner review) | Connected-only QoS 0 non-retained `publish` on the MQTT adapter; per-key in-flight guard; bounded readback observation; `GET`/`POST /api/v1/controls`; one log line per request | `pompa/mqtt.py`, `pompa/control.py` (or a small runtime module), `pompa/api.py`, `pompa/main.py`, tests | Fake-client tests: no publish on any refusal; exactly one publish per accepted request; disconnected `503`; no retry after reconnect; every readback outcome with an injected clock; `409` paths; recorder, history and report unaffected; earlier endpoints byte-identical. Adversarial review. No CT109. |
| **4E-D** CT109 validation, API freeze, closeout | Owner deploys; pre-checks; owner-approved write matrix (below); latency measurement fixes `W`; `docs/API.md` final freeze; whole-stage adversarial review; owner merge; Stage 4 DONE; Stage 5 ready | docs, `scripts/smoke.sh` only if needed | CT109 evidence accepted by the owner; review findings resolved; merge decision |

**Later CT109 validation (owner approves every write).** The pre-checks do not publish:

- `/status` MQTT connected and alive;
- retained `commands/#` topics on the broker (HA);
- `{prefix}/stats` firmware version;
- TOP110, TOP4, TOP76 and TOP81 readings;
- the current value of each target TOP.

For every test the expected MQTT result is `200` with `publish.status="sent"`. Wait at most `W`,
extended to 60 s for measurement only.

The rows are chosen for reversibility and TOP readback evidence. The owner's HA usage decides
priority where it exists. SET46 (`heater_on_outdoor_temperature`) has no owner HA-write evidence;
it is included only because TOP78 gives a direct state readback.

*Safe and reversible.* Each is abort-safe: stop on `503`, on a non-`matched` outcome after two
windows, or on any heat-pump error in TOP44.

| Control | Initial (read) | Request | Expected readback | Restore |
|---|---|---|---|---|
| `quiet_mode_priority` | TOP141 | the other value | TOP141 = | original value |
| `heating_control` | TOP139 | the other value | TOP139 = | original value |
| `smart_dhw` | TOP140 | the other value | TOP140 = | original value |
| `heat_delta` | TOP23 | +1 K | TOP23 = | original value |
| `dhw_heat_delta` | TOP22 | +1 K | TOP22 = | original value |
| `heating_off_outdoor_temperature` | TOP77 | +1 °C | TOP77 = | original value |
| `heater_on_outdoor_temperature` | TOP78 | −1 °C | TOP78 = | original value |
| `bivalent_start_temperature` | TOP131, bivalent off | −1 °C | TOP131 = | original value; HA retained replay can revert it |
| `bivalent_advanced_start_temperature` / `_stop_` | TOP134 / TOP135 | −1 °C | TOP134 / TOP135 = | original value; proves the HA name defect is avoided |
| any of the above | — | request equal to the current value | `unchanged_match` | none |

*State-changing but easily restorable.* Each needs a before/after/restore record. Abort if the
activity or error state becomes unexpected.

| Control | Request | Expected readback | Restore |
|---|---|---|---|
| `dhw_target_temperature` | −1 °C | TOP9 = | original value |
| `zone1_heat_curve` | `{"outside_low": +1}` | TOP32 = | original value |
| `zone1_heat_request` | shift +1 K | TOP27 = | original value |
| `quiet_mode` | `level_1` | TOP18 = | `off` or original |
| `powerful_mode` | `min_30` | TOP17 = | `off` |
| `force_dhw` | on | TOP2 = | off |
| `operation_mode` | an owner-chosen mode | TOP4 per mapping | original value |

*Service or disruptive: not exercised without explicit owner approval.* Transport and encoding are
proved by 4E-B/4E-C tests:

- `force_defrost`, `force_sterilization`, `force_heater`, `pump_service_mode`, `max_pump_duty`,
  `fault_reset`, `heat_pump_power`, `holiday_mode`;
- the installer settings: SET25/26/28/30–33/34/35/43/44/45, SET17, the cooling controls and
  SET21–23;
- every PCB input. On this unit they are refused while TOP110 = `0`. If the heat pump's Optional PCB
  setting is enabled without HeishaMon emulation, error H74 follows.

#### 25.5.14 Checkpoint 4E-B — reference refresh and pure control domain

**OWNER ACCEPTED/CLOSED.** There is no MQTT publish, control route, readback wait, in-flight
guard, request logging, schema or storage change.

**Owner decisions on the independent review.**

- **F7 — Force DHW (no behavior change).** The TOP4 prerequisite applies to `force_dhw=true` and
  `false` alike; executability stays per control. Owner K-series evidence: `true` immediately
  starts or prioritizes a DHW run and TOP2 turns on. A later `false` clears the Force DHW request
  state, but a DHW cycle already running is not aborted; the heat pump finishes it under its
  ordinary DHW logic. `force_dhw=false` therefore means "deassert the force request", never
  "stop or cancel DHW". This is one installation's evidence, not a claim about every series. The
  control stays `temporary` with TOP2 state readback.
- **F8 — direct request temperature (informational restriction).** SET5–SET8 carry
  `documented_direct_temperature_water_mode_only`, never enforced (§25.5.7).

**Reference and catalog.**

- `MQTT-Topics.md` and `OptionalPCB.md` are verbatim copies at `heishamon/HeishaMon@0de4f3c`;
  `PROVENANCE.md` records their blob ids. The backend image packages `OptionalPCB.md` too.
- `pompa/capabilities.py` changes:
  - it parses the PCB set-command table (family `PCB`, identity = upstream name, document order);
  - it cross-checks the documented XTOP table against the observed identities and verified topics
    (XTOP provenance stays `observed`);
  - it has one `Capability.readable` definition, which ingest also uses. SET and PCB command topics
    therefore never become physical readings: a `commands/…` echo, including HA's retained PCB
    commands, stays an uncatalogued topic.
- The catalog now holds 218 entries (TOP 144, OPT 7, SET 48, PCB 13, XTOP 6). The 157 readable
  slots are unchanged.

**Control domain (`pompa/control.py`).**

- **Definitions.** 63 definitions (51 heat-pump, 12 Optional PCB). They are verified at load
  against the catalog: identity, family, upstream name, and readable readback, context and
  prerequisite TOPs. Every catalog command is defined or explicitly excluded, and no excluded
  command is defined. The exclusions are `SetHeatCoolMode` (in the catalog) and the firmware-only
  `SetOptPCBByte9`.
- **`prepare(key, value, facts)`** returns the catalog topic, the exact payload, the validated
  value, the expected readback semantic and the evaluated prerequisites. Otherwise it raises
  `unknown_control`, `invalid_request`, `validation_context_unavailable`, `invalid_value` or
  `prerequisite_not_met`, in that order.
- **Input facts** are `ReadingFact(raw, mode, available)` values, the `/live?include=readings`
  entry shape. Only `mode="live"` and `available` facts count as current evidence.
- **Readback.** `current_state` decodes readbacks. Optional PCB controls have no readback, and
  command echoes are never state.

**Resolved 4E-B questions (evidence priority: firmware, upstream docs, protocol, HA, snapshot).**

| # | Question | Resolution |
|---|---|---|
| O1 | Direct heat request maximum | No upstream source gives it: "20 to max". The integration examples conflict (HA YAML 40, openHAB 65). Accepted 20..127 °C: documented minimum, and the maximum is the `value+128` byte bound (−128 would encode byte 0, "no change"). `range_basis: "protocol"`. The device limit can only be seen through readback. |
| O2 | Curve ranges | Upstream documents none; the firmware encodes `value+128`. Every field accepts −127..127 °C, `protocol` basis. HA ranges are recorded as secondary evidence only. |
| O3 | SET21–SET23 ranges | Upstream gives units only. Encoders: delay `value+1` → 0..254 min; start/stop delta `value+128` → −127..127 K; all `protocol` basis. MQTT-Topics says J-series only, but ProtocolByteDecrypt says J/K/L, and the owner K snapshot reports values, so no series restriction is reported. |
| O4 | `SetHeatCoolMode` polarity | Firmware sets bit 7 of PCB byte 06 to `toInt()==1`. OptionalPCB.md says only "1st bit = Heat/Cool" ("Heat/Cool SW"); no pinned source, upstream history or HA entity documents which value selects heat or cool, or even which switch contact state a bit value means. A boolean would publish a raw bit whose physical meaning is a heat/cool choice nobody can state. Not a control: the command stays a known `PCB` capability, excluded with a reason until polarity evidence exists. |
| O5 | Demand Control mapping | The firmware writes the raw `toInt()` byte, and its default datagram byte 14 is 0xEB. The OptionalPCB.md table (2B/52/85/B8/EB = 5/25/50/75/100 %) matches that default. The range text ("234") and HA's linear formula do not. Frozen: only the five documented points, mapped to `43`/`82`/`133`/`184`/`235`. |
| O6 | Curve invariants | Nothing authoritative exists; none is enforced. |
| O7 | PCB identity scheme | Family `PCB`, identity = upstream command name. |
| O8 | Reference refresh timing | Done in 4E-B. Verbatim upstream copies with recorded blob ids are the reference strategy; upstream is never hand-edited here. |

**Protocol-range review (O1–O3).** The independent 4E-B review re-examined each `protocol` range.
Firmware encodability alone does not prove a device operating range, and no pinned source,
firmware comment or upstream history narrows these values. A documented subset does not exist,
an HA or integration-example range would be invented protocol truth, and refusing the controls
would withdraw documented capability (owner decision: completeness, §25.5). The exposed bound is
therefore the narrowest truthful executable model. `range_basis: "protocol"` tells clients it is
not a device limit, and the device's accepted value appears only through readback.

**Other upstream discrepancies found (documentation only; firmware is authoritative).**

- ProtocolByteDecrypt.md labels bytes 80/81/82 as TOP84/TOP83/TOP85 and bytes 67/68 as
  TOP135/TOP136. The firmware decodes TOP83 from byte 80, TOP85 from byte 81, TOP84 from byte 82,
  TOP136 from byte 67 and TOP135 from byte 68. That matches the `SetCurves` and
  `SetBivalentAPStopTemp` encoders.
- ProtocolByteDecrypt documents quiet "scheduled" as byte-7 pattern `0b10001`. HA's quiet `4`
  encodes `0b00101`, so it is not that state and stays excluded.

**Independent evidence.** An untracked probe extracted the command tables, `topicBytes` and
`topicFunctions` from the pinned firmware sources and transcribed every encoder. It then checked:

- that the catalog SET names equal firmware `commands[]` (48);
- that the catalog PCB names plus the exclusion equal `optionalCommands[]` (14);
- for 245 sampled requests: payload → firmware encoder → protocol byte → firmware decoder at the
  readback TOP → readback semantic.

Every sample matched the definition, with no discrepancy. The probe also confirmed:

- every curve field writes only its own byte;
- trigger payloads write the documented byte-8 values;
- the PCB enums land on the documented bits;
- the Demand Control bytes match the table;
- PCB temperature payloads parse back exactly.

`backend/tests/test_control.py` keeps literal firmware-derived expectations: golden payloads,
ranges, enums, triggers, curves, contexts, prerequisites and readback maps.

#### 25.5.15 Checkpoint 4E-C — control runtime and API

**Implemented, awaiting owner review.** No schema, storage, command persistence, retry, replay or
reconciliation was added. The 63 Stage 4E-B definitions are unchanged.

**Runtime shape (the smallest correct one).**

- `pompa/mqtt.py` — `MqttAdapter.publish_command(topic, payload)` is the only write:
  - it accepts only an already prepared relative `commands/…` topic and publishes
    `{prefix}/{topic}` on the existing shared paho client and connection, with QoS 0 and
    `retain=False`;
  - it returns `True` only if `is_connected()` held and paho returned `MQTT_ERR_SUCCESS`;
  - a disconnected client, another return code, or a paho `ValueError`/`OSError` returns
    `False`;
  - it never retries, and there is no generic publish.
- `pompa/recorder.py` gains two generic, control-unaware facilities:
  - `readings_observation(clock)` is the `/live?include=readings` entries (same serializer, same
    lock), plus the exact float receipt instants and a change sequence number;
  - `wait_for_change(seq, timeout)` is a `threading.Condition` on the recorder lock, notified
    after every applied MQTT event. `Condition.wait` releases the lock, so a waiting request
    never delays ingest. Recorder and ingest import nothing from control (a test enforces this).
- `pompa/control_runtime.py` holds everything runtime:
  - `ControlRuntime.controls()` is the GET projection;
  - `ControlRuntime.execute()` handles POST coordination, the per-key in-flight guard and the
    readback observation;
  - the API maps its `ControlRequestError(code)` to status and `{detail, code}`;
  - `pompa/control.py` stays pure and remains the only validation, encoding and readback-mapping
    authority.
- **Wiring.** `main.py` builds `ControlRuntime(recorder, adapter)` and passes it to
  `create_app`. Without an injected runtime, the app has no publisher and POST answers `503`.
- **Limiter.** POST waits run through `anyio.to_thread.run_sync` on a dedicated
  `CapacityLimiter` (63 tokens, one per key), so up to 15 s readback waits never consume the
  default threadpool tokens that serve every synchronous endpoint.

**POST order.** Every refusal publishes nothing:

1. The body must be one JSON object with only `value`. Duplicate keys at any depth and
   `NaN`/`Infinity` are `400 invalid_request`.
2. An unknown key is `404`.
3. The in-flight guard: `409 command_in_progress`.
4. `control.prepare()` on one baseline observation: `400`/`409`/`422`.
5. `publish_command`: a `False` result is `503 mqtt_unavailable`.
6. After an accepted publish, nothing can turn the request into an error or a second publish.

**In-flight guard.**

- It is a set of keys under a `threading.Lock`: the check and the add are one atomic claim.
- A `finally` releases it on success, validation failure, publish failure, timeout or exception.
- Different keys never contend, and there is no global lock.
- It lives in process memory only, so a restart has no pending command.

**Readback evidence.**

- **Qualifying reading.** A reading counts only if it is `mode="live"` and `available` at the
  observation that sees it.
- **Pre- and post-publish.** `publish.at` is the API clock (`time.time`, the same wall clock as
  MQTT receipts) read immediately before the paho call.
  - A qualifying reading received before it is pre-publish state; the latest one wins, from the
    baseline or a later observation.
  - One received at or after it is a post-publish observation.
- **Outcome per requested field** (a curve uses only its requested fields; a scalar has one):
  - `matched`: every field had a post-publish observation equal to `expected`. Equality is
    type-exact, so `True` never equals `1`. The request returns as soon as this holds.
  - `unchanged_match`: every field's latest pre-publish state equalled `expected` and no field
    had a different post-publish observation. This needs the whole window.
  - `not_observed`: anything else.
- **What is ignored.** Retained, stale, disconnected-epoch and `commands/…` topics never
  qualify; command topics are not physical readings at all.
- **Timing.** The window is measured with `time.monotonic`, so a wall-clock step can never
  extend it. A backward step can only make a post-publish receipt look earlier, which
  under-reports a match (`not_observed`); it never manufactures one.
- **Waiting.** The runtime waits for recorder change notifications, not by polling.
- **Missed intermediates.** Only intermediate values that are overwritten between two
  observations can be missed. That can only turn a `matched` into `not_observed`, never
  manufacture a match.
- **No readback.** A control without readback returns `not_applicable` immediately after the
  publish, without waiting.

**Why no replay is possible (paho 2.1.0 source).**

- A QoS 0 `publish()` without a socket returns `MQTT_ERR_NO_CONN` and queues nothing.
- QoS 0 packets never enter `_out_messages`, the store that is resent after reconnect.
- `reconnect()` clears the unsent packet queue.
- An accepted packet that the socket never wrote is therefore dropped, not sent later.

**Response details the 4E-A contract left open** (now in `docs/API.md`):

- a curve's `readback` has `identity: null` and a `fields` map;
- its `state` carries `value` plus per-field reading facts;
- a `not_applicable` readback has `kind: null` and `waited_seconds: 0.0`;
- `mqtt.connected` in GET is the ingest connection fact of the same observation. POST checks
  the adapter's own `is_connected()` at publish time.

**`W`.** `READBACK_WINDOW_SECONDS = 15.0` is a module constant, reported as
`readback_window_seconds`. Tests inject a short window through the `ControlRuntime` constructor.
It is not a setting and stays provisional until 4E-D measures CT109 latency.

**Evidence.**

- `backend/tests/test_control_api.py` uses a counting fake publisher and real recorder entry
  points. It covers:
  - every error class with zero publishes;
  - no retry after a paho refusal;
  - every readback outcome, including external writers, retained/stale/echo readings, curves
    and effects;
  - disconnect after publish and reconnect during the window;
  - same-key, simultaneous and different-key concurrency;
  - guard release on every path;
  - no database access;
  - an unblocked ingest while a request waits;
  - one log line per request;
  - adapter QoS 0, `retain=False` and connected-only behavior.
- Runtime/API/adapter mutations (retry, retained or stale confirmation, guard leak, no guard,
  extra fields or duplicate keys accepted, QoS 1/retain, publish while disconnected, an extra
  publish, always-executable) are all killed by these tests.
- A disposable local Mosquitto probe on CT112 exercised the real adapter end to end: 18/18
  checks, including exactly one QoS 0 non-retained publication, no retained command for a later
  subscriber, a retained TOP not confirming after an unclean reconnect, `503` while
  disconnected, no replay on reconnect, and Optional PCB `not_applicable`.
