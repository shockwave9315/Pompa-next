# Roadmap

Stages are intentionally large vertical outcomes. Micro-stage numbering is added only when a genuinely independent delivery needs it.

## Stage 0 — Clean Bootstrap (DONE)

Establish the concise project context, authoritative architecture, current status, roadmap, and verbatim HeishaMon references. No application code.

## Stage 1 — Core Backend (DONE)

Deliver a running vertical slice:

```text
MQTT → catalog/ingest → canonical minute → sample_1m
     → MariaDB → basic 1m history/status API
```

Run it beside legacy with a separate MQTT client ID, database, and port. Complete a runtime smoke test.

Real-runtime freshness measurement and decision are complete. The originally planned ≥24 h target was stopped short; the owner accepted the 22.768 h uninterrupted run on CT109 because the observed publication-gap envelope was extremely stable across all relevant topics (max 305.1 s, no gap over 600 s). The decided global freshness policy is `STALE_AFTER_SECONDS=600`; per-metric/source freshness was not justified by the evidence.

## Stage 2 — Historical Engine (DONE)

- Implement `Stats`, derived energy and paired-minute COP.
- Add `rollup_1h`, fail-closed purge, and 5m/1h/1d/total query buckets.
- Report coverage facts, implement Europe/Warsaw DST days, and enforce exact range boundaries.
- Prove raw-minute and rollup read paths are algebraically equivalent.

## Stage 3 — Complete Backend API (DONE)

- Add live data and metric-catalog endpoints.
- Complete factual recorder/storage status.
- Freeze the frontend-facing API contract.

## Stage 4 — Complete Product Backend

Expand the completed Stage 1–3 foundation additively. Broad HeishaMon/product capability must
remain lightweight internally: one definition of each fact, reusable domain resources, and no
frontend-page calculation services. For areas present in legacy, inspect the corresponding product
behavior before finalizing scope; recover useful capability without its implementation debt (see
`docs/CONTEXT.md`). Frontend work starts after this stage.

### 4A — Unified HeishaMon capability foundation and full readable live surface (DONE)

Use a strict parser over tracked TOP/OPT/SET references and observed XTOP identities, the existing
21 canonical metric semantics, and small verified overrides. Expose additional readable topics in
memory and through additive API forms. Preserve the default Stage 3 contract and all recorder,
history and database behavior. Use one feature branch and one DRAFT PR with checkpoint commits:
reference foundation, typed normalization, full in-memory readings, additive API, then tests/docs
and CT109 validation. These are implementation checkpoints, not roadmap stages.

### 4B — Configurable optional history and long-term aggregation (DONE)

Keep the 21-metric core history path; decide history eligibility, dynamic optional storage,
selected-versus-unknown semantics, hourly aggregation, and purge interaction from evidence. Measure
actual MariaDB growth on CT109. Keep `RETENTION_1M_DAYS=365` during early Stage 4; choose any later
retention change only after durable events and storage/backup measurements.

### 4C — Operational state, activity, cycles, defrost and durable events (DONE)

Derive one operational interpretation from canonical minutes and expose factual runtime, starts,
cycle durations/intervals and individual defrosts. Preserve missing evidence. The owner chose
durable per-hour activity segments; events and runs are derived on read. Checkpoints A (pure
domain truth), B (durable segments), C (API resources) and D (CT109 validation) are complete.
The owner accepted CT109 schema, backfill, durable integrity, raw-vs-durable, activity API and
unchanged prior-contract evidence. PR #8 is merged to `main` at
`8c3bccbcf6ba305bbf547c59ef3f6167471442e8`.

### 4D — Reports and product analytics projections (DONE)

Compose day, week, month and custom-period outputs from the existing energy, paired COP and
coverage algebra plus 4C activity truth. Use `rollup_1h` and `activity_segment_1h`; add no report
storage. Expose one `GET /api/v1/report` resource for Warsaw calendar periods, with backend-owned
facts rather than formulas per frontend panel. Checkpoints A–D, CT109 validation and whole-PR
review are complete. PR #9 is merged to `main` at `890e4b0b882248130dbdecf0c412b5c1eac852ff`.
See `docs/ARCHITECTURE.md` §25.4.

### 4E — SET/control backend, final API freeze and runtime validation (CURRENT)

Expose the complete evidenced HeishaMon command surface as backend-validated semantic controls:
48 heat-pump commands and 14 Optional PCB commands, as 63 controls (`SetOptPCBByte9` and
`SetHeatCoolMode` have no validated value meaning). Publish through an isolated
QoS 0, non-retained, connected-only path that makes at most one publish per request. Report
factual readback, with no command persistence. Freeze the complete product API and validate the
backend on CT109 before frontend work. Checkpoints:

- A: research and contract freeze (done);
- B: reference refresh and pure control domain (owner accepted/closed);
- C: publish path and `/api/v1/controls` (owner accepted/closed at
  `fa4d49e233927210260595143a0de01f12f05399`);
- D: CT109 validation, API freeze, whole-stage review and the owner merge (CURRENT).
  D-A's tracked validation contract is implemented and awaits independent review/owner acceptance;
  D-B execution runbook and CT109 validation have not started. W remains provisional at 15 s.

The phase/gate/evidence contract is [`STAGE_4E_D_VALIDATION.md`](STAGE_4E_D_VALIDATION.md).
Stage 4 is not complete and the complete backend API is not finally frozen yet.

See `docs/ARCHITECTURE.md` §25.5.

## Stage 5 — Frontend

Build Teraz, Historia, Statystyki, Cykle/activity, Status, capabilities/settings and Sterowanie
from the completed backend API. Inspect corresponding legacy views, workflows and mocks for useful
product behavior; mocks do not prove backend completeness. Preserve no legacy API, schema or
frontend compatibility. The frontend renders backend domain facts.

## Stage 6 — Cutover

Deploy Pompa Next as the primary application. Stop legacy only after owner approval.
