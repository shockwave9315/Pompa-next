# Roadmap

Stages are intentionally large vertical outcomes. Micro-stage numbering is added only when a genuinely independent delivery needs it.

## Stage 0 — Clean Bootstrap

Establish the concise project context, authoritative architecture, current status, roadmap, and verbatim HeishaMon references. No application code.

## Stage 1 — Core Backend

Deliver a running vertical slice:

```text
MQTT → catalog/ingest → canonical minute → sample_1m
     → MariaDB → basic 1m history/status API
```

Run it beside legacy with a separate MQTT client ID, database, and port. Complete a runtime smoke test.

Real-runtime freshness measurement and decision are complete. The originally planned ≥24 h target was stopped short; the owner accepted the 22.768 h uninterrupted run on CT109 because the observed publication-gap envelope was extremely stable across all relevant topics (max 305.1 s, no gap over 600 s). The decided global freshness policy is `STALE_AFTER_SECONDS=600`; per-metric/source freshness was not justified by the evidence.

## Stage 2 — Historical Engine

- Implement `Stats`, derived energy and paired-minute COP.
- Add `rollup_1h`, fail-closed purge, and 5m/1h/1d/total query buckets.
- Report coverage facts, implement Europe/Warsaw DST days, and enforce exact range boundaries.
- Prove raw-minute and rollup read paths are algebraically equivalent.

## Stage 3 — Complete Backend API

- Add live data and metric-catalog endpoints.
- Complete factual recorder/storage status.
- Freeze the frontend-facing API contract.

## Stage 4 — Frontend

- Build Teraz, Historia, Statystyki, and Status views.
- Reuse visual ideas or presentation components only where justified.
- Keep all domain calculations in the backend.
- Require no compatibility with the legacy frontend.

## Stage 5 — Cutover

Deploy Pompa Next as the primary application. Stop legacy only after owner approval.

## Later, independent work

- Activity and event timeline.
- Cycles and compressor-start statistics.
- 193-capability explorer.
- Control/SET in an isolated write path.
- User settings.
