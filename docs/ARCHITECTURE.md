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
**Current Stage 1–3 limitation:** activity/event timelines and minute-order cycle reconstruction
are guaranteed only while raw `sample_1m` exists. Hourly flags preserve duration but not order.
The current reset-aware interpretation of `operations_counter` likewise has only the raw-minute
window; the counter must not be assumed to equal observed compressor starts. Stage 4C will add
durable factual activity/events before any retention reduction, after comparing storage designs
(§25). The default raw retention remains 365 days during early Stage 4.

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

The current period summary is `bucket=total`; current daily history is `bucket=1d`. Stage 4D may
add one report projection resource for activity/event facts that numeric history cannot express.
It must reuse the existing energy, COP and coverage algebra, not add separate report mathematics.

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
only. Actual MQTT SET publishing begins in Stage 4E through an isolated allowlisted path with
validated values. Persistent command history is a Stage 4E decision, not a required table.

Recorder, storage, history, and aggregation have no dependency on control. Command results return through ordinary observed TOP topics. The recorder itself never publishes MQTT commands.

Preferred command results are factual: `requested`, then `publish_failed` or
`published_unconfirmed`, followed by `confirmed` or `confirmation_timeout` only where reliable
readback exists. A timeout does not stop unrelated reads, history or controls. There is no
automatic rollback, global failure state, speculative rejection or complex recovery workflow.

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
13. In the current Stage 1–3 implementation, activity/timeline and reset-aware counter analysis
    are guaranteed only while raw 1-minute data remains. Stage 4C must make useful event facts
    durable before raw purge can remove their evidence.
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

**Current/frozen now:** Stages 1–3 are complete and deployed on CT109. Sections 3–24 specify the
existing canonical 21-metric ingest, minute recorder, storage, aggregation and default API. Stage
4 adds product capability around them; Stage 5 is frontend and Stage 6 is cutover. Broad capability
must use a lightweight implementation: one definition of each domain fact, catalog metadata
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

**Candidate:** core-wide minutes plus separate dynamic optional history. Compare a participation
pseudo-series, an eligible count in a rollup row, compact metadata, or a simpler factual source.
Prove atomic retries and late-hour correction with the existing recorder invariants. Keep
`RETENTION_1M_DAYS=365` during early Stage 4. Choose any later default only after CT109
table/index/bytes-per-day and backup measurements plus Stage 4C durability proof.

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

Frozen candidate:

```sql
optional_rollup_1h
------------------
hour_ts             series_id            selected_minutes
known_minutes       v_sum                v_min
v_max                v_last

PRIMARY KEY (hour_ts, series_id)
```

`selected_minutes > 0`; `0 <= known_minutes <= selected_minutes`; when `known_minutes == 0`,
`v_sum`, `v_min`, `v_max` and `v_last` are `NULL`. `selected_minutes` comes from canonical recorded
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
are never concatenated automatically. A future convenience alias may resolve to the current
meaning, but a historical query must ultimately resolve to exactly one persistent series id. Final
public selector syntax is not frozen by checkpoint A.

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
min/max, and an inverted `min_value > max_value` range.

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

### 25.3 Stage 4C — one activity interpretation and durable events

**Frozen direction:** One backend interpretation serves friendly operational state, CO/CWU
activity, compressor runtime and observed starts, cycles, short-cycling facts, individual defrosts,
timeline and later reports. Start/end, duration, intervals, min/max/average duration and duration
distribution are factual outputs, not good/bad/fault verdicts. Missing rows remain gaps; unknown
remains unknown. Do not smooth across gaps or add per-second machinery without real evidence.
Keep the device operations counter distinct from observed compressor starts until verified.

Useful event facts must survive raw purge. **Deferred to 4C:** compare direct durable events,
hourly segments and any demonstrably simpler correct representation against late writes, database
outages, cross-hour/midnight spans, restart, idempotency and purge. Raw-only recomputation cannot
meet durability. No event table, segment stitching scheme or materialization marker is frozen now.

### 25.4 Stage 4D — report projections

**Frozen direction:** Day, week, month and custom-period reports compose the existing energy,
paired COP and coverage algebra with Stage 4C activity truth. A report may expose a reusable
domain resource for facts numeric history alone cannot express. It adds no independent formula or
backend service per frontend panel.

### 25.5 Stage 4E — isolated control and final backend API

**Frozen direction:** The full known SET identity surface is catalog knowledge before execution;
MQTT writes start only in 4E. Validate explicit requests against allowlisted commands. Keep
publishing and its factual result independent of live reads, recorder, history and analytics.
Reliable readback may confirm a request; its absence or timeout is factual. **Deferred to 4E:**
which commands can be validated for the installed device, final command API shape, and whether
in-memory state plus logs suffice or persistent command history has a real product need. Freeze
the complete product API and validate it on CT109 before Stage 5 frontend work.
