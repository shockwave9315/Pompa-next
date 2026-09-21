# Current status

## Current stage

**Stage 4 — Frontend**.

### Stage 1

DONE. Merged to `main` (merge commit `ba70fefa94b3000f518e2273283bf69a4abec05f`). Freshness policy
accepted: `STALE_AFTER_SECONDS=600`.

Runtime measurement: 22.768 h uninterrupted on CT109, accepted short of the originally planned
≥24 h target (max observed gap 305.059 s, no gap over 600 s).

### Stage 2

DONE. Merged to `main` (merge commit `9ea192967b7ab24d4b961d22f4280c19069ad4b8`). Independent
adversarial review complete, and its finding applied: reads, writes and the write queue share one
database fact for purged raw evidence — `purged(H)` from `rollup_1h`/`sample_1m` presence, never the
wall clock or retention config (see `docs/ARCHITECTURE.md` §8).

### Stage 3

DONE. Merged to `main` (merge commit `3e1c260388d8144222df407f0b6af741a39d2350`). Deployed and
runtime-validated on CT109 alongside legacy, in a separate Docker Compose project with its own
MariaDB, MQTT client id and port. The frontend-facing `/api/v1` contract is frozen; see
`docs/API.md`.

## Goal

Build the Stage 4 frontend: Teraz, Historia, Statystyki and Status views that consume the frozen
Stage 3 `/api/v1` contract. The frontend is a thin renderer of backend facts and performs no domain
calculations.

## In scope

- Teraz, Historia, Statystyki and Status views.
- Consuming `docs/API.md` as-is.

## Out of scope

- Any backend semantic redesign.
- Control/SET.
- Timeline/activity.
- Cycles or compressor-start statistics.
- The 193-capability explorer.
- Legacy frontend compatibility.
- Frontend domain math: energy, COP, state, alignment and coverage stay backend-owned.

## Next

**Stage 5 — Cutover**
