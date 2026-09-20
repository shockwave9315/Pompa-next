# Current status

## Current stage

**Stage 2 — Historical Engine**.

### Stage 1

DONE. Merged to `main` (merge commit `ba70fefa94b3000f518e2273283bf69a4abec05f`). Freshness policy
accepted: `STALE_AFTER_SECONDS=600`.

Runtime measurement: 22.768 h uninterrupted on CT109, accepted short of the originally planned
≥24 h target (max observed gap 305.059 s, no gap over 600 s).

### Stage 2

Implementation complete. Independent adversarial review complete, and its finding applied: reads,
writes and the write queue now share one database fact for purged raw evidence (see
`docs/ARCHITECTURE.md` §8). Not merged, not deployed to CT109. Ready for final review.

## Goal

Turn canonical minutes into exact aggregates: one aggregation algebra, hourly rollups, mixed
raw/rollup reads, energy, paired COP, coverage facts, calendar days and safe retention.

## In scope

- `Stats` algebra, derived minute series, energy and paired-minute COP.
- `rollup_1h`, built from `sample_1m` in timestamp order, rebuilt idempotently per hour.
- Late-write correctness: a minute write and the rebuild of every rolled hour it touches commit in
  one transaction.
- Mixed `rollup_1h`/`sample_1m` reads for `1h`, `1d` and `total`; `sample_1m` only for `1m` and `5m`.
- `auto`, the 3000-bucket limit and exact `[from, to)` edges, with 422 only for provably purged raw.
- Europe/Warsaw calendar days with 1380- and 1500-minute DST days, validated at startup.
- Coverage facts, `RETENTION_1M_DAYS` and the fail-closed hourly purge, whose per-hour proof is
  what makes a surviving rollup row evidence that raw was deleted.
- `GET /api/v1/history` extended in place; factual rollup, purge and retention status.

## Out of scope

- Frontend, live and metrics endpoints, timeline/activity, cycles and compressor starts.
- Control/SET, user settings, and any legacy compatibility or migration.
- Changing the accepted Stage 1 freshness policy or its runtime measurement result.

## Next

**Stage 3 — Complete Backend API**
