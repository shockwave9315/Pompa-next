# Pompa Next

Pompa Next is a private, read-mostly dashboard for a Panasonic Aquarea heat pump connected through HeishaMon. It is a clean implementation focused on trustworthy minute history, aggregation, and presentation.

```text
HeishaMon MQTT
  → ingest and normalization
  → canonical minute samples
  → MariaDB
  → aggregation and API
  → React frontend
```

Stage 1, 2 and 3 are complete. Stage 3 — Complete Backend API is merged to `main` and has been
deployed and runtime-validated on CT109. The backend records canonical minutes from MQTT into
MariaDB, rolls them up hourly, and serves live state, a metric/COP catalog, factual status and
1m/5m/1h/1d/total history with energy, COP and coverage facts over a frozen `/api/v1`; see
[backend/README.md](backend/README.md). Current work is **Stage 4A — Unified HeishaMon capability
foundation and full readable live surface**, the first part of Stage 4 — Complete Product Backend.
Frontend work follows in Stage 5; see [the roadmap](docs/ROADMAP.md).

Planned stack: Python 3.12, FastAPI, paho-mqtt, PyMySQL, MariaDB 11.4, React, Vite, and Docker Compose.

Start with:

- [Project context](docs/CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Current status](docs/STATUS.md)
- [Roadmap](docs/ROADMAP.md)

The former project remains available as [legacy reference material](https://github.com/shockwave9315/pompa).

Pompa Next does **not** preserve legacy source, API, database, or historical-data compatibility.
