# Current status

## Current stage

**Stage 3 — Complete Backend API**, developed on `stage-3-complete-backend-api` as a DRAFT PR
stacked on `stage-2-historical-engine`, which is itself stacked on `stage-1-core-backend`. Nothing
is merged and nothing is deployed.

Stage 1 is not merged: its ≥24 h freshness measurement is still running on CT109 and the freshness
policy is still `STALE_AFTER_SECONDS=600`, unchanged and undecided. Stage 2 remains stacked and
unmerged.

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
- Changing Stage 1 freshness policy, the running CT109 measurement, or Stage 2 aggregation,
  rollup, purge, retention and history semantics.

## Next

**Stage 4 — Frontend**
