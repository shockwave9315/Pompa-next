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

## Stage 4A implementation

The Stage 4A code is complete and locally validated. CT109 deployment and runtime validation are
pending owner execution, so Stage 4A is not yet marked DONE.

## Implemented

- Parse the tracked TOP/OPT/SET reference and observed XTOP identities into a deterministic
  baseline; combine it with existing canonical `Metric`/`Source` semantics and small verified
  overrides. Unknown metadata remains unknown.
- Add typed normalization and lightweight in-memory live readings for additional readable topics.
- Expose additive capability/readings forms while preserving default `/metrics` and `/live` shapes.
- Prove reference coverage, unchanged core behavior and packaged-reference availability locally.

Stage 4A uses one feature branch and one DRAFT PR with checkpoint commits: A reference-backed
foundation; B additional typed normalization; C full in-memory readings; D additive API; E final
local review/tests/docs and owner-run CT109 validation. These are commits within the stage, not
separate roadmap stages.

The catalog contains 203 reference-backed identities and 157 readable TOP/OPT/XTOP slots. Generic
physical values use number/text typing, and opt-in `/metrics?include=capabilities` and
`/live?include=readings` expose them. Default Stage 1–3 API and canonical history behavior remain
unchanged. Owner-supplied pre-deployment CT109 `mqtt.uncatalogued_topics` evidence verifies the
exact physical paths for XTOP1 and XTOP4, so all six XTOP topics are now mapped. Deployment of this
head and the remaining CT109 regression checks are still pending owner execution.

## Out of scope

- Database schema, optional history, logging policy, event engine, reports and SET publishing.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–3 history invariants, or the 365-day default
  raw retention.

## Next

After owner CT109 validation and PR #6 review/merge: **Stage 4B — configurable optional history**,
followed by 4C activity/cycles/defrost and durable events, 4D reports, 4E control and final
API/runtime validation, Stage 5 frontend, then Stage 6 cutover. See `docs/ROADMAP.md`.
