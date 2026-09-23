# Current status

## Current stage

**Stage 4B — checkpoint B DONE.** Configurable optional history. Checkpoint B adds the
`HistoryProfile` domain model and the production immutable policy timeline/selection API; no
optional value is recorded, aggregated or queryable yet — there is still no `OptionalAccumulator`
and no `optional_sample_1m` table. Frontend work starts after Stage 4.

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

**Checkpoint A (architecture/contract freeze) and checkpoint B (policy foundation) — DONE.** Not
yet the optional recorder implementation: no `OptionalAccumulator`, no `optional_sample_1m` table,
no optional value is ever read, written or computed. Branch `stage-4b-optional-history`, draft
PR #7, not merged.

### Implemented

- Checkpoint A froze the Stage 4B architecture (`docs/ARCHITECTURE.md` §25.2.1–§25.2.7) and proved
  its two riskiest claims against real MariaDB: the `optional_sample_1m` JSON candidate's semantic
  round trip and the policy-head locking-read concurrency invariant, in test-only tables
  (`backend/tests/test_stage4b_json_feasibility.py`, `test_stage4b_policy_concurrency.py`, kept and
  still run).
- Checkpoint B (`docs/ARCHITECTURE.md` §25.2.8) makes the foundation concrete: the `HistoryProfile`
  domain model with an initial, explicitly evidenced set of 15 identities; the shared numeric-
  parsing primitive (`pompa.catalog.parse_numeric`, proved equivalent to unchanged canonical
  `parse_value`); production policy tables with an idempotent empty genesis and a self-describing
  immutable series snapshot; `effective_from_minute` resolution; drift/blocking; idempotent
  ambiguous-PUT-retry handling; and `GET /api/v1/metrics?include=history_profiles` plus
  `GET`/`PUT /api/v1/optional-history/selection` (see `docs/API.md`).
- A real two-thread test against the production tables caught a genuine bug in the first
  implementation — a plain, snapshot-bound read of the just-locked head revision's own row,
  instead of a further locking read — fixed by generalizing the checkpoint A locking-read
  principle to every read needed to interpret the same fact (`docs/ARCHITECTURE.md` §25.2.8).
- An adversarial review of checkpoint B found and fixed two correctness gaps, still checkpoint B:
  a persisted `optional_series` row is now always verified (locking read, full semantic comparison)
  before reuse, failing closed with `SeriesDefinitionConflict`/`409` — including on an ambiguous
  PUT retry — instead of silently trusting a recovered row id; and `XTOP1`/`XTOP4` v1 now carry the
  `{-200}` sentinel and `min_value=0.0` (owner decision) instead of accepting every finite number,
  including negative power. `label` is now explicitly non-semantic (never requires a version bump),
  `optional_series.identity`/`expected_topic` use explicit binary collation, and a golden semantic-
  fingerprint test guards every existing profile against an unversioned semantic change.
- Full backend suite (689 tests) verified against real MariaDB; every MariaDB-gated test skips
  cleanly without `POMPA_TEST_DB_HOST`.

### Deferred

See `docs/ARCHITECTURE.md` §25.2.8: the complete eligible profile list, optional-power energy
beyond `XTOP1`/`XTOP4`, and a max-selection-count remain open questions. Remaining checkpoints:

- **Checkpoint C:** continuous optional source state; `OptionalAccumulator`; pairing with canonical
  minutes; `optional_sample_1m` JSON persistence; one protected/waiting write path; atomic
  canonical + optional raw persistence.
- **Checkpoint D:** `optional_rollup_1h`; late-hour rebuild; purge proof/integration; optional
  history query through the existing history algebra/API.
- **Checkpoint E:** full tests/docs; owner CT109 runtime validation; publication-gap evidence;
  storage/table/index/backup measurements.

## Out of scope

- Optional-history runtime recording, logging-selection API, event engine, reports and SET
  publishing.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–4A history invariants, or the 365-day
  default raw retention.

## Next

Checkpoint C of Stage 4B: `OptionalAccumulator` and continuous optional source state, building on
the checkpoint A/B foundation. See `docs/ROADMAP.md` for later stages.
