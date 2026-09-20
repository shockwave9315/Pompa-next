# Current status

## Current stage

**Stage 1 — Core Backend**

Implementation complete. Runtime validation complete: a 22.768 h uninterrupted real-runtime
measurement on CT109 (owner-accepted short of the originally planned ≥24 h target). Freshness
decision complete: `STALE_AFTER_SECONDS=600` is the accepted global policy (max observed gap
305.1 s, no gap over 600 s). Ready for merge.

## Goal

Build the first complete vertical slice from live MQTT input to a queryable canonical minute history.

## In scope

- Configuration and the metric catalog for core recorded metrics.
- MQTT ingest and retained/per-source `seen_live` semantics.
- `MinuteAccumulator` and the full-minute source-alive rule.
- Wide `sample_1m` storage in MariaDB.
- Recorder and bounded write buffer.
- `GET /api/v1/history` with the initial 1-minute contract.
- `GET /api/v1/status` and `GET /health`.
- Focused unit, storage, slice, and runtime-smoke tests.
- Runtime smoke beside legacy using separate identity, database, and port.
- Topic-gap, LWT, and retained-message measurements: a 22.768 h uninterrupted run, accepted
  short of the originally planned ≥24 h target.

`/api/v1` is the fresh Pompa Next API namespace. It does not retain legacy `/api/v2` numbering or compatibility.

## Out of scope

- `rollup_1h`, purge, and 5m/1h/1d/total aggregation.
- Energy and COP reports, coverage aggregation, and DST query behavior.
- Frontend, activity/timeline, cycles, and compressor-start statistics.
- 193-capability explorer, control/SET, and user settings.

## Next

**Stage 2 — Historical Engine**
