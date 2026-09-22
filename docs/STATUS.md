# Current status

## Current stage

**Stage 4B — checkpoint A DONE.** Configurable optional history. Checkpoint A is architecture/
contract freeze plus MariaDB feasibility proof only; no optional runtime recording exists yet.
Frontend work starts after Stage 4.

Stage 4A — DONE and merged to `main` (PR #6, merge commit
`a34aaead89fae3359769cf2737aab42fd274d145`).

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

### Implemented in Stage 4A

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

## Stage 4B

**Checkpoint A — architecture/contract freeze and feasibility proof — DONE.** Not yet the optional
recorder implementation. Branch `stage-4b-optional-history`, draft PR, not merged.

### Implemented in checkpoint A

- Froze the Stage 4B architecture in `docs/ARCHITECTURE.md` §25.2: `HistoryProfile` as a distinct
  namespace from canonical `Metric` and physical capability identity; continuous observation versus
  selection-gated persistence; a separate future `OptionalAccumulator` leaving `MinuteAccumulator`
  unchanged; a persisted, immutable policy timeline (`optional_series` /
  `optional_policy_revision` / `optional_policy_member` / `optional_policy_head`) as the only
  selection truth, with database-timeline-resolved `effective_from_minute`; the `recorded` /
  `selected` / `known` state algebra; the `optional_sample_1m` JSON write model and its two
  pre-DB-vs-in-transaction failure classes; the `optional_rollup_1h` candidate and shared
  roll/purge frontiers; and default-empty selection.
- Proved, against real MariaDB (test-only tables, no production Stage 4B schema): the
  `optional_sample_1m` JSON candidate's semantic round trip, invalid-JSON rejection,
  NaN/Infinity rejection before persistence, idempotent whole-document upsert, clean delete, and
  missing-key-versus-zero distinguishability
  (`backend/tests/test_stage4b_json_feasibility.py`); and the policy-head concurrency invariant —
  a locking/current read on the singleton head row serializes a policy-replacement transaction
  against a minute-persistence transaction even under this repository's
  `START TRANSACTION WITH CONSISTENT SNAPSHOT`, in both commit orderings, with an explicit control
  proving an ordinary snapshot read would give the wrong answer
  (`backend/tests/test_stage4b_policy_concurrency.py`).
- Left `catalog.parse_value` and canonical parsing untouched; no shared numeric-parsing primitive
  was extracted, since checkpoint A has no `HistoryProfile` parser caller to prove it against yet
  (`docs/ARCHITECTURE.md` §25.2.1).
- Verified no Stage 1–4A behavior changed: full backend test suite, including the new feasibility
  tests, against real MariaDB.

### Deferred beyond checkpoint A

See `docs/ARCHITECTURE.md` §25.2.7: the complete eligible physical-profile list, energy for
optional power, an operational maximum selected-series count, `OptionalAccumulator`
implementation, optional ingest runtime state, the production selection API, production Stage 4B
tables, optional raw recording/rollup/purge/query, and any CT109 optional-history deployment.

## Out of scope

- Optional-history runtime recording, logging-selection API, event engine, reports and SET
  publishing.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–4A history invariants, or the 365-day
  default raw retention.

## Next

Checkpoint B of Stage 4B: the optional recorder implementation building on the checkpoint A
architecture freeze. See `docs/ROADMAP.md` for later stages.
