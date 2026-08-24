# iowap-server

**IOWAP Relay Server — API, Scheduler, Auth, Dashboard, Metrics**

The relay server is the central coordinator in the IOWAP ecosystem. Nodes register their capabilities via heartbeat, and the relay matches tasks to capable nodes. It is a **dumb scheduler** — it knows nothing about the work itself, only who can do what.

## Features

- **Capability Registry** — nodes heartbeat their capabilities, relay maintains a live registry
- **Task Scheduler** — match tasks to capable nodes, claim/release lifecycle with retry
- **Auth & Security** — token-based node auth, CSRF-protected dashboard, TLS support
- **Dashboard** — web UI for node overview, task monitoring, capability browsing, metrics
- **SSE Events** — real-time event stream for connected nodes
- **Status System** — node/task/stage status registry with valid transitions
- **Event Bus** — publish/subscribe internal events for monitoring & extensibility
- **PostgreSQL + SQLite** — production (Postgres) and dev (SQLite) backends
- **Docker** — `docker compose up` for one-command deployment
- **Observability** — `/metrics` (Prometheus), `/ready` health checks, structured JSON logs

## Quick Start

```bash
# Clone
git clone https://github.com/iowap-org/iowap-server.git
cd iowap-server

# Run (SQLite dev mode)
cp .env.example .env
docker compose up -d

# Or with PostgreSQL
docker compose --profile postgres up -d
```

Server starts on `http://localhost:8788`.

## Configuration

| Env | Default | Description |
|-----|---------|-------------|
| `RELAY_PORT` | 8788 | Server listen port |
| `RELAY_DB_TYPE` | sqlite | Database backend (sqlite / postgres) |
| `RELAY_MASTER_SEED` | — | Deterministic master seed for token generation |
| `TLS_CERTFILE` | — | TLS cert path (enables HTTPS, disables mDNS) |
| `POSTGRES_*` | — | PostgreSQL connection (when `RELAY_DB_TYPE=postgres`) |

See `docs/server/setup.md` for full reference.

## Architecture

```
┌──────────┐   heartbeat + capabilities    ┌───────────┐
│  Nodes   │ ◄─────────────────────────►   │   Relay   │
│ (any)    │   claim + complete tasks      │   Server  │
└──────────┘                               └─────┬─────┘
                                                  │
                                          ┌───────┴───────┐
                                          │   Dashboard   │
                                          │  (HTMX/HTML)  │
                                          └───────────────┘
```

## Docs

Full documentation in [iowap-org/iowap-docs](https://github.com/iowap-org/iowap-docs):

- `docs/getting-started.md` — first steps
- `docs/server/setup.md` — full setup guide
- `docs/server/docker.md` — Docker deployment
- `docs/reference/api.md` — API reference
- `docs/server/dashboard.md` — dashboard usage
- `docs/server/admin.md` — administration

## License

AGPL-3.0