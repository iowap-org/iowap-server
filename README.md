# iowap-server

**IOWAP Relay Server — API, Scheduler, Auth, Dashboard, Metrics**

The relay server is the central coordinator in the IOWAP ecosystem. Nodes register their capabilities via heartbeat, and the relay matches tasks to capable nodes. It is a **purpose-agnostic scheduler** — it knows nothing about the work itself, only who can do what.

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

## Quick Start (Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/iowap-org/iowap-server/main/install.sh | bash
```

The installer asks **native (systemd)** vs. **Docker (GHCR image)**, configures the
database (SQLite default, PostgreSQL local/remote) and environment (homelab HTTP /
internet TLS), creates the master admin seed, and prints the dashboard login URL.

**Docker (manual)** — pre-built image on GHCR:

```bash
docker volume create relay-data
docker run -d --name iowap-server --restart unless-stopped \
  -p 8788:8788 \
  -e RELAY_MASTER_SEED="$(openssl rand -hex 24)" \
  -e RELAY_SESSION_SECRET="$(openssl rand -base64 32)" \
  -e RELAY_SESSION_COOKIE_SECURE=false \
  -v relay-data:/app/.relay \
  ghcr.io/iowap-org/iowap-server:latest
```

Compose files live in [iowap-org/iowap-docker](https://github.com/iowap-org/iowap-docker) (`server/docker-compose.yml`).

**From PyPI**:

```bash
pip install "iowap-server"      # import namespace: relay_server
relay-server --host 0.0.0.0 --port 8788
relay-server admin init-master  # bootstrap step only the installer automates
```

**Manual (source)**:

```bash
git clone --recursive https://github.com/iowap-org/iowap-server.git
cd iowap-server
pip install -e .            # or: pip install -e ".[postgres]"
relay-server --host 0.0.0.0 --port 8788
relay-server admin init-master  # prints the adm_ seed once; retry-safe
```

The manual paths do NOT run the installer's bootstrap, so run
`relay-server admin init-master` once before the dashboard login: it applies
the seed from config/`RELAY_MASTER_SEED` (or generates one) to the database.
Without a seed row the dashboard has no working login. Then:

1. Open the dashboard login and sign in with **Master seed**.
2. Complete `/bootstrap` to create your first human admin.
3. From that point seed login is disabled automatically.

> QA-audit note (2026-10-17): when a master seed row already exists in the DB,
> `admin init-master` exits with "already exists" and the DB row wins over any
> (re)generated config seed — do not log in with a seed printed by a re-run
> installer after its config was wiped. The installer (>= this commit) discards
> such dead seeds and says so instead.

Server starts on `http://localhost:8788` — the dashboard login is under
`/relay/v2/dashboard/login` (master seed acts as the bootstrap admin until the
first human admin exists).

## Configuration

| Env | Default | Description |
|-----|---------|-------------|
| `RELAY_PORT` | 8788 | Server listen port |
| `RELAY_DB_TYPE` | sqlite | Database backend (sqlite / postgres) |
| `RELAY_MASTER_SEED` | — | Deterministic master seed for token generation |
| `TLS_CERTFILE` | — | TLS cert path (enables HTTPS, disables mDNS) |
| `POSTGRES_*` | — | PostgreSQL connection (when `RELAY_DB_TYPE=postgres`) |

See [iowap-docs → server/setup.md](https://github.com/iowap-org/iowap-docs/blob/main/server/setup.md) for full reference.

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

- [getting-started.md](https://github.com/iowap-org/iowap-docs/blob/main/getting-started.md) — first steps
- [server/setup.md](https://github.com/iowap-org/iowap-docs/blob/main/server/setup.md) — full setup guide
- [server/docker.md](https://github.com/iowap-org/iowap-docs/blob/main/server/docker.md) — Docker deployment
- [reference/api.md](https://github.com/iowap-org/iowap-docs/blob/main/reference/api.md) — API reference
- [server/dashboard.md](https://github.com/iowap-org/iowap-docs/blob/main/server/dashboard.md) — dashboard usage
- [server/admin.md](https://github.com/iowap-org/iowap-docs/blob/main/server/admin.md) — administration

## License

MIT — see [LICENSE](LICENSE).
