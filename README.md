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

Stages 1–3 and Stages 4A–4D are complete and merged. Stage 4D (PR #9) is owner-validated on
CT109. **Stage 4E — SET/control backend, final API freeze and runtime validation** is current. The
backend records
canonical and selected optional minutes from MQTT into MariaDB, maintains hourly rollups and
durable activity segments, and serves live state, catalogs, status, history, activity and report
resources;
see [backend/README.md](backend/README.md). Frontend work
remains Stage 5; see [the roadmap](docs/ROADMAP.md).

Planned stack: Python 3.12, FastAPI, paho-mqtt, PyMySQL, MariaDB 11.4, React, Vite, and Docker Compose.

Start with:

- [Project context](docs/CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Current status](docs/STATUS.md)
- [Roadmap](docs/ROADMAP.md)

The former project remains available as [legacy reference material](https://github.com/shockwave9315/pompa).
Pompa Next aims to recover and improve its useful product capabilities through a clean implementation.

Pompa Next does **not** preserve legacy source, API, database, or historical-data compatibility.
