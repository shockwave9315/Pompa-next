# Current status

## Current stage

**Stage 4C — operational state, activity, cycles, defrost and durable events — DONE.**
Checkpoints A (domain truth), B (durable hourly activity segments), C (activity read model and
API resources) and D (closure and CT109 runtime validation) are complete. Branch
`stage-4c-activity-cycles` remains in DRAFT PR #8. The owner's whole-PR adversarial review is
complete; its F2 correction awaits targeted final review. Frontend work starts after Stage 4.

The owner-approved F2 merge hardening makes `/activity` hold the unacknowledged recorder tail
`open` until its historical outcome settles, preventing transient false gaps before a tick or
during a write backlog.

Stage 4B — DONE and merged to `main` (PR #7, merge commit
`dfd225d2a8fe35563cd1f684efb81cf91b532a2f`).

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

**Checkpoints A (architecture/contract), B (policy foundation), C (raw recording), D
(durable optional history), and E (runtime validation) — DONE.** Merged to `main` (PR #7).

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
- A follow-up review found that the first correction's conflict check was still scoped to the full
  `(identity, expected_topic, profile_version)` tuple, so a code change to `expected_topic` alone,
  without a `profile_version` bump, could still create a second, independent series for the same
  identity/version instead of colliding. Closed: one `identity`/`profile_version` now names exactly
  one `expected_topic`, checked by a locking read *before* any series is created or reused
  (`Session.lock_series_by_identity_version`), never by the narrower per-tuple `UNIQUE` constraint
  alone. The golden fingerprint map is now keyed by `(identity, profile_version)` and append-only.
- Full backend suite verified against real MariaDB; every MariaDB-gated test skips cleanly without
  `POMPA_TEST_DB_HOST`.
- Checkpoint C continuously tracks separate optional source evidence for every current
  `HistoryProfile`, integrates it in an independent `OptionalAccumulator`, and queues one immutable
  `RecordedMinute` pair per canonical recorded minute. Optional source expiry does not segment
  canonical accumulation.
- One head-first locking transaction resolves current policy membership at persist time, writes
  canonical and selected-known optional raw together, and retains the same protected retry,
  refusal, and bounded waiting semantics. `optional_sample_1m` stores deterministic complete JSON
  keyed by stable series id; a restrictive FK enforces its canonical subset relation. Blocked or
  unknown selected members have no JSON key; zero remains a known value.
- Checkpoint C's transitional purge guard preserved selected-known and selected-but-unknown
  evidence until D supplied durable optional rollups.
- Checkpoint C adversarial corrections: optional derived non-finite arithmetic becomes unknown
  before persistence, with a second finite check at the selected-known JSON boundary; the fake
  enforces production JSON serialization. Production `persist` accepts only explicit
  `RecordedMinute` pairs. Real MariaDB tests cover the former poison pair, policy-aware purge,
  persist/PUT serialization, and a lost-ack protected retry across a later PUT.
- Checkpoint D stores `optional_rollup_1h` with separate selected/known counts and known-value
  statistics. Canonical and optional rollups share one `rolled_until`, are rebuilt together for
  new and late hours, and commit atomically. Shared purge proves exact optional raw/policy/rollup
  agreement before deleting optional raw and canonical raw together. Selected-but-unknown counts
  survive purge in rollup rows.
- `GET /api/v1/optional-history/series` discovers persisted meanings. Explicit
  `optional:IDENTITY@VERSION` selectors extend the existing `/history` engine, including mixed
  canonical/optional requests, version isolation, raw/rollup equivalence, Warsaw DST, and
  persisted `energy=true` kWh. Default API shapes remain canonical and unchanged.
- Checkpoint D adversarial correction: optional `last` history never sums values; optional
  mean/energy sum overflow stores `v_sum=NULL` with known counts and finite min/max/last intact.
  Rollup, late persistence and shared purge proceed. A requested unrepresentable avg/kWh returns
  422, while valid unrequested optional series cannot poison another query. Loaded raw JSON still
  receives full consistency validation. Canonical `Stats` and minute arithmetic are unchanged.

### Checkpoint E — accepted CT109 evidence

- The owner's independent read-only MQTT collector ran **22.57 h** (81,253.2 s) without a
  disconnect. All 15 profiles published; no non-retained gap exceeded 600 s. TOP52 and TOP55
  each published 269 sentinel values on this K-series unit; their selected-but-unknown minutes
  remained selected. Retained startup deliveries were excluded from history evidence.
- All 15 profiles were selected simultaneously. Optional raw reached **1,436** canonical minutes;
  the first 61-row integrity check found 793 series keys, 366 numeric zeros and no orphan minute.
  The natural rollup contained **24 hours × 15 series = 360 rows**, including TOP52/TOP55 with
  `known_minutes=0`. API and database counts/values matched. A Force-DHW smoke showed real
  profile changes and preserved valid zero and unknown facts; TOP93 remains unit `duty`.
- Final recorder status: 1,439 minutes closed and written; protected, waiting, dropped and
  refused rows all zero; database, rollup and purge errors absent; MQTT connected and alive with
  zero parse rejects and clock steps. Canonical `rolled_until` advanced to
  `2026-09-24T16:00:00Z`.
- Exact table counts included 7,408 canonical raw, 3,472 canonical rollup, 1,436 optional raw
  and 360 optional rollup rows. Optional raw averaged 13 JSON keys and 122.688 bytes (max 154
  bytes). Database allocated 2,375,680 bytes, including 81,920 index bytes; measured query
  paths used existing indexes, so none were added. Compressed logical backup grew from 106,793
  to 164,473 bytes while canonical history also grew; that difference is not solely Stage 4B.
- The external owner-run helper's global `umask 077` made newly checked-out Python source
  unreadable to the non-root Docker user on the first deployment attempt. The owner restored
  readable source modes and rebuilt/restarted the backend. Recovery passed post-deployment
  checks without any product-code or deployed-SHA change; the helper is corrected on CT112.

Owner decisions: keep all current 15 profiles globally eligible, including TOP52/TOP55/TOP63/
TOP66. Their sentinel/zero observations on one K-series unit do not establish global support
rules. No selection cap below 15 is justified. Keep shared `STALE_AFTER_SECONDS=600` and
`RETENTION_1M_DAYS=365`; future list growth or retention changes need new evidence.

## Stage 4C

Owner-accepted direction: durable per-hour activity segments (option B), materialized by the
existing `persist()` → `rebuild_hour()` path in checkpoint B; events, compressor runs, starts,
intervals, continuation and range projections are derived on read. Legacy smoothing, NULL-as-off
compressor behavior and its asymmetric midnight continuation are deliberately not preserved.

### Checkpoint A — domain truth (DONE)

- Pure `pompa/activity.py` (no storage) implements `docs/ARCHITECTURE.md` §25.3.1:
  per-minute classification, hour-local `ActivitySegment`s, explicit `Gap`s, activity events,
  observed compressor runs, off intervals, individual defrosts, evidence-based range projection
  and a factual summary, under `ACTIVITY_RULE_VERSION = 1`.
- Review hardening kept the owner-approved classifier:
  - defrost `NULL`, heat-pump state `NULL` with the compressor off, and an unresolved power side
    all stay `unknown`
  - a fractional heat-pump state with the compressor off is `idle`
- Hardening changes:
  - an independent literal version-1 golden, with a drift self-test
  - defrost edges proven by the defrost signal itself
  - explicit non-additive `*_overlapping` counts
  - `segment_rule_version` naming
  - explicit evidence-window fields and contract
  - a real-ingest fractional TOP0 test
- `backend/tests/test_activity.py` covers the classification matrix and golden version-1
  meaning, no smoothing around former legacy thresholds, start/end evidence against
  off/unknown/gap/window/open neighbours, cross-hour runs built from separately built hours,
  midnight continuation/stop/gap/unknown/restart/defrost, Warsaw 23 h and 25 h days, fractional
  defrost from the real ingest/accumulator (165 s) with every minute clip exact, an unobservable
  short stop across a minute boundary, the power tail after a CO run, energy/paired-COP
  ingredients against the canonical history fold, and a minute-by-minute brute-force reference.

### Checkpoint B — durable hourly activity segments (DONE)

- The additive `activity_segment_1h` table (one row per hour-local segment, rule version, exact
  defrost fraction, deterministic energy `Stats` JSON; no FK to raw) is created by ordinary
  `ensure_schema()`. See `docs/ARCHITECTURE.md` §25.3.2.
- `rebuild_hour()` replaces canonical rollup, optional rollup and activity segments of one hour
  in one transaction, so forward roll and late-write repair materialize activity atomically.
- A bounded backfill materializes hours rolled before this checkpoint, with no new watermark.
  Purge waits for it and proves every candidate hour's segments field by field against the
  locked raw minutes before deleting anything. Hours purged before 4C-B are never fabricated.
- A real MariaDB race test found that `persist()` judged "already purged" from a snapshot older
  than its policy-head lock. A late minute racing a purge could then rebuild a purged hour from
  itself alone. `persist()` now evaluates `first_purged_hour` with current reads. Production had
  no concurrent path, because persist and purge share the recorder thread.
- Final hardening:
  - Purge also requires every stored row to be exactly the canonical `segment_record`, including
    the `energy_json` text. A duplicate JSON key had let MariaDB and Python read one row
    differently while the decoded-values proof passed.
  - Hours whose raw was purged before 4C-B are documented and tested as "activity unavailable",
    distinct from never-recorded hours. Checkpoint C must keep that distinction on reads.
- `backend/tests/test_activity_durable.py` covers:
  - schema, round trip and fail-closed decoding
  - rebuild, forward roll, late writes, lost acknowledgement and rollback
  - backfill, including pre-4C purged history as "unavailable" rather than "not recorded"
  - every purge-proof corruption class, including non-canonical but equivalent JSON
  - post-purge equivalence of timeline, runs, defrosts, gaps, energy and paired COP
  - cross-hour and Warsaw-midnight stitching
  - four MariaDB concurrency races

### Checkpoint C — activity read model and resources (DONE)

- `GET /api/v1/activity` (exact `[from, to)`, at most 31 days and one hour) and
  `GET /api/v1/activity/live`. The contract is in `docs/API.md`; semantics are in
  `docs/ARCHITECTURE.md` §25.3.3.
- One snapshot and one source per UTC hour: durable segments, else raw, else "unavailable" (a
  rollup without raw or segments), else not recorded. A range intersecting unavailable history is
  a `422`. Corrupt durable rows are a `500`, never answered from raw. Reads never write.
- The backend widens evidence until every span intersecting the range reaches a decisive
  boundary, so runs, events and defrosts are returned whole with separate overlap facts. The
  read-time `unavailable` boundary stays distinct from gap, unknown and open.
- Full-span energy and paired COP for runs and events come from the existing algebra.
  `*_overlapping` counts stay non-additive.
- Live activity classifies only `mode="live"` inputs from the `/api/v1/live` observation. It does
  not read the database.
- `backend/tests/test_activity_api.py` covers:
  - source equivalence: raw, mixed, durable, awaiting backfill, after purge
  - unavailable inside, before and after the range
  - open and unclosed minutes
  - evidence widening: a 50-hour run, minimal windows
  - the cycle and defrost matrices
  - Warsaw midnight and DST days through the API
  - a literal response contract, and 400/422/500/503
  - live classification, including retained, stale and disconnected inputs and a DB outage
  - a partition property: additive facts add up and spans stay identical
- The existing frozen-path tests now include the two additive paths. Every earlier response is
  unchanged.
- Final hardening from the independent review:
  - A durable hour is read only when every row is valid and in exact canonical persisted form,
    and its segment minutes equal the hour's recorded canonical minutes. Otherwise the request
    returns 500, with no raw fallback.
  - Before this, a deleted or forged durable segment turned into a fake gap or a fake minute.
    A duplicate-key `energy_json` row also passed.
  - Record validation also refuses `-0.0`, which the application never writes and a re-encoding
    check alone cannot detect.
  - Documentation of widening overshoot and raw read counts is corrected.

### Checkpoint D — accepted CT109 runtime validation (DONE)

- The owner upgraded CT109 from Stage 4B head `730e0470387f2614efff70bda43dfb978767a807`
  to Stage 4C head `7d6757028ff56065631263ac765565d30887817d`. The backend image's
  activity, activity-history, storage, recorder and API source hashes matched that checkout.
  Health, MQTT and MariaDB recovered; parse rejects, clock steps, recorder queues, rollup/purge
  errors and the backend log error scan were all zero or clear.
- Deployment added only `activity_segment_1h`; all existing table definitions matched before and
  after. Normal bounded backfill reduced 116 rolled/raw hours still missing activity at the first
  poll to zero. Logs show six batches of 24, 24, 24, 24, 24 and 20 hours: 140 materialized hours.
  The final audit found 140 rolled/raw and durable hours, 168 durable segments, no minute-count
  mismatch, missing eligible hour or orphan, and only rule version 1. One raw hour without a
  rollup was the current unrolled hour. No activity-unavailable hour existed in this retained
  history, so CT109 did not exercise that 422 path; local tests cover it.
- An independent fold of real raw minutes for 2026-09-24 08:00–09:00 UTC produced exactly the
  stored durable segment record (60 raw and 60 recorded minutes). `/activity/live` returned
  `unknown` activity and compressor with retained classifier inputs; the literal classifier
  agreed, correctly refusing to treat retained values as current evidence.
- The recent 11-minute activity query accounted for 10 recorded and one gap minute. A real
  38-minute observed compressor run crossed 14:00 UTC as one run with a 20-minute query overlap
  and full-span energy/COP fields. The 24-hour query accounted for 1,439 recorded minutes and one
  gap, two observed starts/stops, two complete 38-minute runs and one exact 774-minute off
  interval. No defrost occurred in this window; defrost semantics remain covered by local tests.
- The closed 2026-09-19 12:00–13:00 UTC `/history` response matched byte for byte before and
  after deployment (SHA256
  `12868d5f376a47eb3afadecf7985086c880e18962107dd4041a1f6a92ac91487`); default
  health/live/metrics/status shapes were unchanged. Deployment left one expected unrecorded
  08:06 UTC partial minute, preserved earlier rows and resumed recording at 08:07, with no
  fabricated or duplicate minute.
- Final local validation at the closure head: Stage 4C tests 307 passed / 94 skipped without
  MariaDB and 401 passed with MariaDB 11.4; affected suites 450 passed / 197 skipped without
  MariaDB; full backend 913 passed / 277 skipped without MariaDB and 1,190 passed with MariaDB.
  The backend Docker image built, and `git diff --check` passed.

## Out of scope

- Stage 4D reports, SET publishing and frontend.
- Frontend and legacy compatibility or historical migration.
- Changing the 21 canonical metric semantics, Stage 1–4A history invariants, or the 365-day
  default raw retention.

## Next

Stage 4D — reports and product analytics projections, as defined in `docs/ROADMAP.md`.
