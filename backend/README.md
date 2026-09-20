# Pompa Next backend

One Python 3.12 process: a paho-mqtt client, a recorder thread and a FastAPI
server (one uvicorn worker). It records canonical minutes from HeishaMon MQTT
into the MariaDB table `sample_1m` and serves them over `/api/v1`.
Domain rules are in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Module | Responsibility |
|---|---|
| `pompa/config.py` | Environment settings; invalid configuration stops startup. |
| `pompa/catalog.py` | The 21 recorded metrics: topics, priority, unit, kind, sentinels, ranges. |
| `pompa/ingest.py` | Connection epochs, LWT, retained vs live, `seen_live`, freshness, source selection. |
| `pompa/minute.py` | `MinuteAccumulator` → `MinuteRow` (full-minute source life, time-weighted means). |
| `pompa/recorder.py` | Serialises events, closes minutes, bounded write buffer, flush, status facts. |
| `pompa/storage.py` | `sample_1m` DDL and parameterized PyMySQL queries. |
| `pompa/history.py` | 1-minute history response from stored rows. |
| `pompa/mqtt.py` | paho adapter: subscribe `{prefix}/#`, reconnect, forward retain flag and LWT. |
| `pompa/api.py` | `/health`, `/api/v1/status`, `/api/v1/history`. |
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
| `STALE_AFTER_SECONDS` | `600` | Accepted Stage 1 global freshness policy, configurable in 60–86400. |
| `WRITE_BUFFER_ROWS` | `60` | Bound on never-submitted waiting rows; one submitted batch of at most as many rows is held for retry on top of it. |
| `LOG_LEVEL` | `INFO` | Logs are UTC. |

## API (Stage 1)

- `GET /health` — process liveness only: `{"status": "ok"}`.
- `GET /api/v1/status` — facts: MQTT connection/epoch/LWT/alive/last live message/parse rejects,
  recorder process start/last closed and written minute, `protected_rows` (submitted batch whose
  write is not yet confirmed; retried until success, never dropped), `waiting_rows` (never
  submitted, at most `WRITE_BUFFER_ROWS`), `dropped_rows` (never-submitted rows dropped on
  waiting-queue overflow), database availability and
  oldest/newest stored minute, and per-source measurement counters. No verdicts.
- `GET /api/v1/history?from=…&to=…&bucket=1m[&series=a,b]` — exact `[from, to)`; both instants are
  ISO 8601 with an explicit offset and minute-aligned; at most 48 hours. Each bucket has `start`,
  `end`, `expected_minutes`, `recorded_minutes` (0 = no row) and `coverage_percent`. Series arrays
  align with buckets; a `null` value with `recorded_minutes = 1` is a stored `NULL`.
  Errors: 400 bad parameters, 422 bucket other than `1m` or range over 48 h, 503 database
  unavailable.

## Tests

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest
```

Storage and part of the slice test run against real MariaDB when `POMPA_TEST_DB_HOST` is set
(they drop and recreate `sample_1m` in that database, so use a throwaway one):

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
git checkout stage-1-core-backend
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
