# Current status

## Current stage

**Stage 3 — Complete Backend API**, developed on `stage-3-complete-backend-api`. Not deployed to
CT109.

### Stage 1

DONE. Merged to `main` (merge commit `ba70fefa94b3000f518e2273283bf69a4abec05f`). Freshness policy
accepted: `STALE_AFTER_SECONDS=600`.

Runtime measurement: 22.768 h uninterrupted on CT109, accepted short of the originally planned
≥24 h target (max observed gap 305.059 s, no gap over 600 s).

### Stage 2

DONE. Merged to `main` (merge commit `9ea192967b7ab24d4b961d22f4280c19069ad4b8`). Independent
adversarial review complete, and its finding applied: reads, writes and the write queue share one
database fact for purged raw evidence — `purged(H)` from `rollup_1h`/`sample_1m` presence, never the
wall clock or retention config (see `docs/ARCHITECTURE.md` §8). Not deployed to CT109.

### Stage 3

Current stage.

## Goal

Complete the backend contract the Stage 4 frontend needs: canonical live state, a frontend-safe
metric and COP catalog, a final factual status contract, cross-endpoint consistency, and a frozen,
documented and tested `/api/v1`.

## In scope

- One canonical live selection path in ingest: confirmed fresh source, else explicit retained
  fallback, else nothing; `GET /api/v1/live` serialises it under the recorder lock without
  database I/O.
- `GET /api/v1/metrics`: metric and COP metadata, timezone, buckets and `MAX_BUCKETS`, derived from
  the existing catalog and time grid so no second list of metadata exists.
- `GET /api/v1/status` reviewed against the final architecture and proven complete and factual.
- Subsystem independence: MQTT and MariaDB failures stay local to the endpoints they are facts of.
- `docs/API.md` and structural contract tests that freeze the frontend-facing `/api/v1` surface.

## Out of scope

- Frontend, timeline/activity, cycles, compressor starts, the 193-capability explorer.
- Control/SET, user settings, legacy compatibility, legacy aliases and historical migration.
- Live COP: COP is defined on canonical minutes and stays in `/api/v1/history`.
- Changing the accepted Stage 1 freshness policy or its runtime measurement result, or Stage 2
  aggregation, rollup, purge, retention and purged-history semantics.

## Next

**Stage 4 — Frontend**
