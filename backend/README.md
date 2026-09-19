# Pompa Next backend

One Python 3.12 process: a paho-mqtt client, a recorder thread and a FastAPI
server (one uvicorn worker). It records canonical minutes from HeishaMon MQTT
into the MariaDB tables `sample_1m` and `rollup_1h` and serves history,
aggregates and status over `/api/v1`.
Domain rules are in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Module | Responsibility |
|---|---|
| `pompa/config.py` | Environment settings; invalid configuration stops startup. |
| `pompa/catalog.py` | The 21 recorded metrics: topics, priority, unit, kind, sentinels, ranges. |
| `pompa/ingest.py` | Connection epochs, LWT, retained vs live, `seen_live`, freshness, source selection. |
| `pompa/minute.py` | `MinuteAccumulator` → `MinuteRow` (full-minute source life, time-weighted means). |
| `pompa/aggregation.py` | `Stats` algebra, derived series, energy, paired COP, coverage. |
| `pompa/timegrid.py` | UTC/Europe/Warsaw alignment, buckets, `auto`, raw retention floor. |
| `pompa/recorder.py` | Serialises events, closes minutes, write buffer, flush, rollup, purge, status facts. |
| `pompa/storage.py` | `sample_1m`/`rollup_1h` DDL and parameterized PyMySQL queries, one transaction per session. |
| `pompa/history.py` | Bucket composition from rollups and raw minutes. |
| `pompa/mqtt.py` | paho adapter: subscribe `{prefix}/#`, reconnect, forward retain flag and LWT. |
| `pompa/api.py` | `/health`, `/api/v1/status`, `/api/v1/live`, `/api/v1/metrics`, `/api/v1/history`. |
| `pompa/main.py` | Process wiring and shutdown. |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `MQTT_HOST` | required | Use `host.docker.internal` for a broker on the Docker host. |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | empty | |
| `MQTT_CLIENT_ID` | `pompa-next` | Must differ from legacy. |
| `MQTT_TOPIC_PREFIX` | `panasonic_heat_pump` | HeishaMon base topic. |
| `DB_HOST` / `DB_PORT` | required / `3306` | Compose sets `DB_HOST=db`. |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | required / empty / `pompa_next` | |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8001` | |
| `STALE_AFTER_SECONDS` | `600` | Stage 1 bootstrap value, 60–86400. Not an accepted policy. |
| `WRITE_BUFFER_ROWS` | `60` | Bound on never-submitted waiting rows; one submitted batch of at most as many rows is held for retry on top of it. |
| `RETENTION_1M_DAYS` | `365` | `sample_1m` retention, 0–36500; `0` disables purge. `rollup_1h` is kept indefinitely. |
| `LOG_LEVEL` | `INFO` | Logs are UTC. |

## API

The frozen frontend-facing contract is [`docs/API.md`](../docs/API.md); this section is the
operator's summary.

- `GET /health` — process liveness only: `{"status": "ok"}`.
- `GET /api/v1/status` — facts: MQTT connection/epoch/LWT/alive/last live message/parse rejects,
  recorder process start/last closed and written minute, `protected_rows` (submitted batch whose
  write is not yet confirmed; retried until success, never dropped), `waiting_rows` (never
  submitted, at most `WRITE_BUFFER_ROWS`), `dropped_rows` (never-submitted rows dropped on
  waiting-queue overflow), configured retention, last rollup and purge outcomes, database
  availability, oldest/newest stored minute, `rolled_until` and `raw_floor`. No verdicts.
- `GET /api/v1/live` — current in-memory state of every catalog metric: value, `mode`
  (`live` | `retained` | `none`), physical source and receipt time. In-memory only, so it stays
  available during a database outage. A retained value is labelled and never enters history.
- `GET /api/v1/metrics` — catalog-derived metric and COP metadata, presentation timezone, history
  buckets and the 3000-bucket limit. No database or MQTT dependency.
- `GET /api/v1/history?from=…&to=…[&bucket=…][&series=a,b]` — exact `[from, to)`, never rounded.

`from` and `to` are ISO 8601 instants with an explicit offset (`Z`, `+02:00`) or `YYYY-MM-DD`
calendar dates meaning local midnight in Europe/Warsaw; both must be whole minutes.

| `bucket` | Alignment | Read from |
|---|---|---|
| `1m`, `5m` | UTC | `sample_1m` |
| `1h` | UTC | `rollup_1h` for complete rolled hours, `sample_1m` for partial edges and unrolled hours |
| `1d` | Europe/Warsaw calendar day (1380/1500 minutes across DST) | as `1h` |
| `total` | the exact requested range | as `1h` |
| `auto` (default) | ≤36 h → `1m`, ≤10 d → `5m`, ≤120 d → `1h`, longer → `1d`; promoted to `1h` when raw retention cannot serve minutes | — |

Each bucket has `start`, `end`, `expected_minutes` (elapsed minutes of the bucket, never trimmed to
recording start), `recorded_minutes` (0 = no row) and `coverage_percent`. Series arrays align with
the bucket array; a `null` value with `recorded_minutes > 0` means the metric was unknown in
recorded minutes. `series` accepts recorded metric keys plus `cop_co`, `cop_dhw` and `cop_total`;
by default all of them are returned.

| Series | Fields |
|---|---|
| `kind=mean` | `avg`, `min`, `max`, `minutes` |
| `kind=last` | `last`, `min`, `max`, `minutes` |
| power (W) | additionally `kwh` = Σ W / 60000 over the known minutes |
| COP | `cop` = Σ paired out / Σ paired in, `paired_minutes`, `input_kwh`, `output_kwh` |

Errors: 400 malformed parameters, 422 well-formed but unrepresentable (over 3000 buckets, a range
or partial edge hour older than raw retention, instants outside 1970–2100), 503 database
unavailable.

## Rollup, late writes and purge

`rollup_1h` holds one row per UTC hour and series (catalog metrics plus `recorded` and the paired
power series) whenever that series has at least one known minute in the hour. `rolled_until` is
`MAX(hour_ts) + 1 h`; hours are rolled in ascending order, one transaction each, so the rolled
range stays contiguous.

Minutes can arrive after their hour was rolled — a protected batch retried through a long outage, a
minute closed while its hour was being rolled, a clock stepped back across a restart. Instead of a
larger reprocessing window, every minute write rebuilds the rolled hours it touches inside the same
transaction: raw minute and corrected rollup commit together or not at all, a lost acknowledgement
leaves both committed, and retries are idempotent.

Purge runs hourly in bounded steps (at most 24 whole hours per step) and deletes `sample_1m` rows
below `min(floor_hour(now − RETENTION_1M_DAYS), rolled_until − 2 h, floor_hour(oldest pending
minute))`, and only after proving per hour that the rollup accounts for every minute still stored
there. Anything unproven, missing or failing deletes nothing. That same cutoff is the read path's
raw floor, so queries never depend on minutes purge may already have removed.

## Tests

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest
```

Storage, rollup, purge, history and slice tests run against real MariaDB when `POMPA_TEST_DB_HOST`
is set (they drop and recreate `sample_1m` and `rollup_1h` there, so use a throwaway database):

```sh
docker run -d --name pompa-next-testdb -e MARIADB_ROOT_PASSWORD=testroot \
  -e MARIADB_DATABASE=pompa_next_test -e MARIADB_USER=pompa -e MARIADB_PASSWORD=pompa \
  -p 127.0.0.1:33306:3306 mariadb:11.4
POMPA_TEST_DB_HOST=127.0.0.1 POMPA_TEST_DB_PORT=33306 ../.venv/bin/python -m pytest
```

## Deployment on CT109 beside legacy

Pompa Next uses its own compose project (`pompa-next`), its own MariaDB container and volume,
MQTT client id `pompa-next` and port 8001. Nothing in `/opt/pompa` is touched.

```sh
git clone https://github.com/shockwave9315/Pompa-next.git /opt/pompa-next
cd /opt/pompa-next
git checkout stage-1-core-backend   # Stage 2 is not deployed
cp .env.example .env && chmod 600 .env   # set MQTT_HOST, credentials, DB passwords
docker compose up -d --build
docker compose logs -f backend           # connection epochs, LWT, database state
scripts/smoke.sh                         # health, status, last-hour gaps, per-topic table
```

Update: `git pull && docker compose up -d --build backend`. Stop: `docker compose down`
(keeps the `db-data` volume). Run exactly one backend container.

## 24-hour freshness measurement

The process accumulates the evidence in memory from its start; a restart resets it.

1. Deploy, then note `recorder.process_start` from `scripts/smoke.sh`.
2. Leave it running for at least 24 hours without restarting the backend.
3. Archive the evidence:

   ```sh
   scripts/smoke.sh --json > measurement-$(date -u +%Y%m%dT%H%M%SZ).json
   scripts/smoke.sh > measurement-$(date -u +%Y%m%dT%H%M%SZ).txt
   docker compose logs --no-color backend > measurement-backend-$(date -u +%Y%m%dT%H%M%SZ).log
   ```

What each question is answered by:

| Question | Evidence |
|---|---|
| Publication gap per TOP/XTOP topic | `sources[].gap_count`, `gap_sum_seconds`, `mean_live_gap_seconds`, `max_live_gap_seconds` |
| Maximum observed non-retained gap | largest `max_live_gap_seconds` |
| Retained behavior | `sources[].retained_messages`, `last_retained`; `mqtt.lwt.retained` |
| Reconnect behavior | `mqtt.connects`/`disconnects`/`epoch` and `MQTT connected`/`disconnected` log lines |
| LWT behavior | `LWT … retained=…` log lines, `mqtt.lwt` |
| XTOP topic path | `sources[]` XTOP rows receiving messages, or `uncatalogued_topics` outside `main/` |

A gap sample is the time between two consecutive non-retained messages of the same topic inside
one observation interval. An interval ends at MQTT disconnect/reconnect and at LWT `Offline`; the
first message of a new interval is only a baseline. Retained deliveries never contribute. Message
counts, `first_live_at` and `latest_live_at` cover the process lifetime; `epoch_last_live_at` is the
current connection epoch's freshness evidence used by history.

The measurement only reads ingest facts; it does not change `sample_1m` semantics.
