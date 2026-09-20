# Current status

## Current stage

**Stage 2 — Historical Engine**, developed on `stage-2-historical-engine` as a DRAFT PR stacked on
`stage-1-core-backend`. Stage 1 is not merged: its ≥24 h freshness measurement is still running on
CT109 and the freshness policy is still `STALE_AFTER_SECONDS=600`, unchanged and undecided.

Implementation complete. Runtime validation complete: a 22.768 h uninterrupted real-runtime
measurement on CT109 (owner-accepted short of the originally planned ≥24 h target). Freshness
decision complete: `STALE_AFTER_SECONDS=600` is the accepted global policy (max observed gap
305.1 s, no gap over 600 s). Ready for merge.

## Goal

Turn canonical minutes into exact aggregates: one aggregation algebra, hourly rollups, mixed
raw/rollup reads, energy, paired COP, coverage facts, calendar days and safe retention.

## In scope

<<<<<<< HEAD
- `Stats` algebra, derived minute series, energy and paired-minute COP.
- `rollup_1h`, built from `sample_1m` in timestamp order, rebuilt idempotently per hour.
- Late-write correctness: a minute write and the rebuild of every rolled hour it touches commit in
  one transaction.
- Mixed `rollup_1h`/`sample_1m` reads for `1h`, `1d` and `total`; `sample_1m` only for `1m` and `5m`.
- `auto`, the 3000-bucket limit and exact `[from, to)` edges, including 422 for unrepresentable ones.
- Europe/Warsaw calendar days with 1380- and 1500-minute DST days, validated at startup.
- Coverage facts, `RETENTION_1M_DAYS` and the fail-closed hourly purge.
- `GET /api/v1/history` extended in place; factual rollup, purge and retention status.
=======
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
>>>>>>> origin/main

## Out of scope

- Frontend, live and metrics endpoints, timeline/activity, cycles and compressor starts.
- Control/SET, user settings, and any legacy compatibility or migration.
- Changing Stage 1 freshness policy or the running CT109 measurement.

## Next

**Stage 3 — Complete Backend API**
