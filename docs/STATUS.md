# Current status

## Current stage

**Stage 4A — DONE.** Unified HeishaMon capability foundation and full readable live surface.
PR #6 remains draft for owner review before merge. Frontend work starts after Stage 4.

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

## Stage 4A

DONE. The owner validated two deployed heads on CT109. The original implementation head
`eae56fe46f4940aa4940ab2f08f87436cb54a117`: health 200, MQTT connected/alive with zero parse
rejects, unchanged default API, and 203 capabilities with 157 readable physical slots, with all six
XTOP readings, including real zeros on XTOP1 and XTOP4, present through the opt-in live API. The
adversarial-review correction head `5b3776c4d23a1aa98a0a567c9798486f211c78ee`, which removed the
capability `key` field and added the explicit physical `available` contract, was separately
runtime-validated: 203 capabilities with no `key` field; 157 readable slots (150 with a receipt
timestamp, matching 144 TOP + 6 XTOP; the 7 absent OPT slots read `mode=none`/`available=false`);
representative fresh TOP/XTOP readings, including the TOP15 sentinel `-200` and text payloads,
read `available=true`; unchanged default `/live` (21 metrics) and `/metrics` (21 metrics, 3 COP
entries) shapes; an identical immutable history query result and MariaDB schema before and after;
and the expected unrecorded restart gap, with no backfill. The final head
`0008226f3fa10c2aaece093f21a02f269fa24ae0` adds only parser-side rejection of malformed reference
input; it does not change valid parse output or runtime/API/history semantics, so it required no
further CT109 deployment.

## Implemented

- Parse the tracked TOP/OPT/SET reference and observed XTOP identities into a deterministic
  baseline; combine it with existing canonical `Metric`/`Source` semantics and small verified
  overrides. Unknown metadata remains unknown.
- Add typed normalization and lightweight in-memory live readings for additional readable topics.
- Expose additive capability/readings forms while preserving default `/metrics` and `/live` shapes.
- Prove reference coverage, unchanged core behavior and packaged-reference availability locally.

The catalog contains 203 reference-backed identities and 157 readable TOP/OPT/XTOP slots. Generic
physical values use number/text typing, and opt-in `/metrics?include=capabilities` and
`/live?include=readings` expose them. Default Stage 1–3 API and canonical history behavior remain
unchanged. All 157 topics are mapped; XTOP1 and XTOP4 paths were verified by actual CT109 messages.
The owner's immutable historical-range response matched byte for byte before and after deployment,
MariaDB schemas were identical, canonical minutes continued advancing, and the restart left its
expected unrecorded partial minute. No optional capability was persisted.

## Out of scope

- Database schema, optional history, logging policy, event engine, reports and SET publishing.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–3 history invariants, or the 365-day default
  raw retention.

## Next

After PR #6 review/merge: **Stage 4B — configurable optional history**. See `docs/ROADMAP.md` for
later stages.
