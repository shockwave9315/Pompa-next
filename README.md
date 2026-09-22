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

Stages 1–3 are complete. Stage 4A — Unified HeishaMon capability foundation and full readable live
surface — is complete and runtime-validated on CT109; PR #6 is pending owner review and merge. The
backend records canonical minutes from MQTT into MariaDB, rolls them up hourly, and serves live
state, capability and metric catalogs, factual status, and 1m/5m/1h/1d/total history; see
[backend/README.md](backend/README.md). **Stage 4B — configurable optional history** is next after
merge. Frontend work remains Stage 5; see [the roadmap](docs/ROADMAP.md).

Planned stack: Python 3.12, FastAPI, paho-mqtt, PyMySQL, MariaDB 11.4, React, Vite, and Docker Compose.

Start with:

- [Project context](docs/CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Current status](docs/STATUS.md)
- [Roadmap](docs/ROADMAP.md)

The former project remains available as [legacy reference material](https://github.com/shockwave9315/pompa).
Pompa Next aims to recover and improve its useful product capabilities through a clean implementation.

Pompa Next does **not** preserve legacy source, API, database, or historical-data compatibility.
