# Pompa Next context

This is the default context for every development session. It contains stable facts and rules, not project chronology.

## Project identity

Pompa Next is a private, read-mostly dashboard for a Panasonic Aquarea heat pump connected through HeishaMon.

Its primary data path is:

```text
HeishaMon MQTT
→ ingest and normalization
→ canonical minute samples
→ MariaDB
→ aggregation
→ API
→ frontend
```

The system records observed facts, exposes their coverage, and avoids manufacturing data for gaps.

## Repository relationship

Current repository: `shockwave9315/Pompa-next`

Legacy/reference repository: `shockwave9315/pompa`

Legacy is frozen reference material only. Pompa Next is a new implementation, not a refactor or continuation of the old source tree.

Knowledge may be extracted when explicitly needed. Architecture and code do not move automatically.

No source, API, schema, database, or data compatibility is required. There is no migration or backfill of legacy historical data.

## Technology direction

Backend:

- Python 3.12
- FastAPI
- paho-mqtt
- PyMySQL
- MariaDB 11.4
- One backend process initially

Frontend, in a later stage:

- React
- Vite

Deployment uses Docker Compose.

## Core architectural rules

MQTT is the only primary ingest path. There is no HTTP bootstrap or fallback ingest.

Data has three distinct states:

- `0` is a real observed zero.
- `NULL` means a metric is unknown for an otherwise recorded minute.
- No row means the minute was not recorded.

There is no zero-fill, interpolation, or backfill.

A canonical `MinuteRow` exists only when the physical source was observed alive for the full minute. No minute beginning before process start may be created.

An MQTT retained message is not history evidence. Each physical source/topic must emit a non-retained message in the current process and connection epoch before it is eligible for history.

Historical source selection uses the highest-priority source that is valid, `seen_live`, and fresh. A fresh lower-priority TOP source can therefore beat a retained or unconfirmed XTOP source.

`STALE_AFTER_SECONDS=600` is the accepted Stage 1 global freshness policy, decided from a real-runtime measurement on CT109 (22.768 h uninterrupted, owner-accepted short of the originally planned ≥24 h target; see `docs/ARCHITECTURE.md` §4).

Canonical history is stored in a wide `sample_1m` table. Long-term history later uses a narrow `rollup_1h` table.

Energy is derived from minute-average power:

```text
kWh = ΣW / 60000
```

Period COP is the ratio `Σout / Σin` over paired valid minutes. Instantaneous COP values are never averaged.

Coverage reports facts and counts only. It has no `ready`, `partial`, or `unavailable` thresholds.

Timestamps are stored in UTC. Calendar presentation uses local time. Daily buckets follow `Europe/Warsaw`, including 23- and 25-hour DST days.

The backend is the domain source of truth. The frontend renders backend facts and models and performs no energy, COP, state, alignment, or coverage math.

Control is a later, isolated MQTT write path. Recorder and history behavior must never depend on control.

## Working process

Prefer larger vertical stages over artificial micro-stages.

A typical implementation stage is:

```text
implementation
→ local tests
→ DRAFT PR
→ external architecture/code review
→ focused fixes
→ bot or adversarial review when useful
→ runtime smoke when relevant
→ merge
```

Avoid endless review loops. Evaluate new review comments for actual correctness before changing architecture.

Do not create WORKLOG-style chronology. Git commits and PR bodies are development history.

Use focused validation for the assigned task. Do not run browsers, development servers, or long-running watchers as generic validation unless requested.

## Runtime map

- Development and forensic host: `CT112 ai-devbox`
- Legacy production: `CT109 /opt/pompa`
- Pompa Next: `CT109 /opt/pompa-next`

Pompa Next runs beside legacy with:

- A separate MQTT `client_id`
- A separate database
- A separate port

Both systems are read-only MQTT consumers during parallel operation.

## Reading map

For every task, read:

- `docs/CONTEXT.md`
- `docs/STATUS.md`

When architecture or domain semantics matter, read the relevant sections of `docs/ARCHITECTURE.md`.

When MQTT or HeishaMon topic facts matter, read only the needed files in `docs/reference/heishamon/`.

Do not read the legacy repository unless the task explicitly requires comparison or reference evidence. Never use legacy history documents as Pompa Next requirements.
