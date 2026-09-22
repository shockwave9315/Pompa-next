# Current status

## Current stage

**Stage 4A — Unified HeishaMon capability foundation and full readable live surface.** Frontend
work is postponed until Stage 4 — Complete Product Backend is finished.

### Stage 0

DONE. Clean bootstrap and tracked project/HeishaMon references.

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

Add a reference-backed effective HeishaMon capability catalog and full readable in-memory live
surface without changing the 21 canonical metrics or Stage 1–3 recorder/history behavior. The
next implementation work is **Stage 4A checkpoint A: reference-backed capability foundation**.

## In scope

- Parse the tracked TOP/OPT/SET reference and observed XTOP identities into a deterministic
  baseline; combine it with existing canonical `Metric`/`Source` semantics and small verified
  overrides. Unknown metadata remains unknown.
- Add typed normalization and lightweight in-memory live readings for additional readable topics.
- Expose additive capability/readings forms while preserving default `/metrics` and `/live` shapes.
- Prove reference coverage, unchanged core behavior and packaged-reference availability; validate
  the resulting live surface on CT109.

Stage 4A uses one feature branch and one DRAFT PR with checkpoint commits: A reference-backed
foundation; B additional typed normalization; C full in-memory readings; D additive API; E
tests, docs and CT109 validation. These are commits within the stage, not separate roadmap stages.

## Out of scope

- Database schema, optional history, logging policy, event engine, reports and SET publishing.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–3 history invariants, or the 365-day default
  raw retention.

## Next

**Stage 4B — configurable optional history**, followed by 4C activity/cycles/defrost and durable
events, 4D reports, 4E control and final API/runtime validation, Stage 5 frontend, then Stage 6
cutover. See `docs/ROADMAP.md`.
