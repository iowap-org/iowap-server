#!/usr/bin/env bash
# Install iowap-server on Linux (native systemd or Docker image).
# Usage: curl -fsSL https://raw.githubusercontent.com/iowap-org/iowap-server/main/install.sh | bash
set -euo pipefail

REPO_URL="${IOWAP_REPO_URL:-https://github.com/iowap-org/iowap-server.git}"
INSTALL_DIR="${IOWAP_INSTALL_DIR:-$HOME/iowap-server}"
PYTHON_MIN="${IOWAP_PYTHON_MIN:-3.11}"
NONINTERACTIVE="${IOWAP_NONINTERACTIVE:-0}"
MODE="${IOWAP_MODE:-}"          # update|override|uninstall|dupdate|duninstall
DB_IN="${IOWAP_DB:-}"
LAB_IN="${IOWAP_LAB:-}"
DEPLOY_IN="${IOWAP_DEPLOY:-}"   # native|docker
UNIT_NAME="iowap-server"
RELAY_HOME_DIR="$HOME/.relay"
IMAGE="${IOWAP_IMAGE:-ghcr.io/iowap-org/iowap-server:latest}"
CONTAINER_NAME="${IOWAP_CONTAINER:-iowap-server}"
VOLUME_NAME="${IOWAP_VOLUME:-relay-data}"
ENVFILE="$RELAY_HOME_DIR/docker.env"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing: $1"; }
ask() {
  local var="$1" prompt="$2" def="${3:-}" reply
  if [ "$NONINTERACTIVE" = "1" ]; then
    reply="$def"
  else
    read -r -p "$prompt " reply </dev/tty || reply=""
    reply="${reply:-$def}"
  fi
  printf -v "$var" '%s' "$reply"
}
ask_secret() {
  local var="$1" prompt="$2" def="${3:-}" reply
  if [ "$NONINTERACTIVE" = "1" ]; then
    reply="$def"
  else
    read -r -s -p "$prompt " reply </dev/tty || reply=""
    printf '\n'
    reply="${reply:-$def}"
  fi
  printf -v "$var" '%s' "$reply"
}

case "$(uname -s)" in
  Linux) ;;
  *) die "this installer supports Linux only." ;;
esac

if [ "$(id -u)" = "0" ]; then
  SUDO=""
elif sudo -n true 2>/dev/null; then
  SUDO="sudo"
elif [ "$NONINTERACTIVE" != "1" ]; then
  # No passwordless sudo: in interactive runs allow REAL sudo - it prompts
  # for the password on the tty exactly when a root step needs it.
  SUDO="sudo"
  log "Note: no passwordless sudo - sudo password prompts will appear where root is needed."
else
  SUDO=""
fi

# Docker availability: binary anywhere sensible (incl. QNAP Container-Station
# qpkg path, where the binary is NOT on the regular PATH) + working daemon.
DOCKER_BIN=""
for CAND in docker /usr/local/bin/docker /usr/bin/docker /usr/sbin/docker \
            /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker; do
  if [ -x "$CAND" ]; then DOCKER_BIN="$CAND"; break; fi
done
DOCKER=0
if [ -n "$DOCKER_BIN" ]; then
  if "$DOCKER_BIN" info >/dev/null 2>&1; then
    DOCKER=1
    case "$DOCKER_BIN" in /*)
      export PATH="$(dirname "$DOCKER_BIN"):$PATH" ;;
    esac
  else
    log "Docker binary found ($DOCKER_BIN) but the daemon is not reachable - Docker mode unavailable."
  fi
fi

check_python() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys;exit(0 if sys.version_info[:2] >= (3,11) else 1)" 2>/dev/null || return 1
  else
    return 1
  fi
  return 0
}

ensure_deps() {
  need git
  if ! check_python; then
    log "Python ${PYTHON_MIN}+ not found - attempting install"
    if command -v apt-get >/dev/null 2>&1; then
      ${SUDO:+"${SUDO}"} apt-get update -qq
      ${SUDO:+"${SUDO}"} apt-get install -y python3 python3-venv python3-pip git
    elif command -v dnf >/dev/null 2>&1; then
      ${SUDO:+"${SUDO}"} dnf install -y python3 python3-pip python3-venv git
    elif command -v pacman >/dev/null 2>&1; then
      ${SUDO:+"${SUDO}"} pacman -Sy --noconfirm python python-pip git
    else
      die "no supported package manager; install Python >= ${PYTHON_MIN}, pip, git, venv manually"
    fi
  fi
  python3 -c "import venv" >/dev/null 2>&1 || die "python venv module missing"
  log "✓ Dependencies verified (python >= ${PYTHON_MIN}, git, venv)"
}

# ---------- container helpers ----------
container_exists() {
  [ "$DOCKER" = "1" ] && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER_NAME"
}

docker_wait_healthy() { # $1 = host port (fallback when image has no HEALTHCHECK)
  local port="${1:-8788}" i status
  for i in $(seq 1 30); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER_NAME" 2>/dev/null)"
    case "$status" in
      healthy) return 0 ;;
      "")
        # Image ships no HEALTHCHECK (live: iowap-server image) - probe /health.
        if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then return 0; fi ;;
    esac
    sleep 2
  done
  log "⚠ Container not ready within 60s - check: docker logs $CONTAINER_NAME"
  return 1
}

docker_print_login() {
  local scheme="$1" port="$2"
  local lan_ip
  lan_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  lan_ip="${lan_ip:-127.0.0.1}"
  log ""
  log "Dashboard login: $scheme://$lan_ip:$port/relay/v2/dashboard/login"
  log "  (local: $scheme://127.0.0.1:$port/relay/v2/dashboard/login)"
}

docker_print_seed() {
  log "Master admin seed: $1"
  log "⚠ Store the seed securely - it grants admin access."
  log "Seed login is the bootstrap admin - valid until the first human admin exists"
  log "(recovery afterwards: set enable_master_seed_login=true / RELAY_MASTER_SEED stays valid)."
}

# ---------- Docker question block (fresh or update-override of docker install) ----------
docker_ask_and_run() { # $1 = fresh|update
  local phase="$1" port scheme tls_dir tls_base
  local db lab pg_host pg_port pg_db pg_user pg_pw
  mkdir -p "$RELAY_HOME_DIR"
  if [ "$phase" = "update" ] && [ -f "$ENVFILE" ]; then
    # Recreate from the stored env (pull new image first)
    port="$(grep '^IOWAP_DOCKER_PORT=' "$ENVFILE" | cut -d= -f2-)"
    scheme="$(grep '^IOWAP_DOCKER_SCHEME=' "$ENVFILE" | cut -d= -f2-)"
    tls_dir="$(grep '^IOWAP_DOCKER_TLS_DIR=' "$ENVFILE" | cut -d= -f2-)"
    log "Pulling image..."
    docker pull "$IMAGE" >/dev/null
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker volume create "$VOLUME_NAME" >/dev/null
    TMOUNT=()
    [ -n "$tls_dir" ] && TMOUNT=(-v "$tls_dir:/certs:ro")
    docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      -p "${port}:8788" \
      --env-file "$ENVFILE" \
      ${TMOUNT[@]+"${TMOUNT[@]}"} \
      -v "${VOLUME_NAME}:/app/.relay" \
      "$IMAGE" >/dev/null
    log "Container recreated: $CONTAINER_NAME (image updated, volume/env kept)"
  else
    # ---- Questions ----
    if [ "$phase" = "update" ]; then
      log "⚠ Previous env file ($ENVFILE) missing - regeneration uses a NEW master seed"
      log "  which only applies if the volume/database is EMPTY (a DB with a seed keeps its old login)."
    fi
    if [ -z "$DB_IN" ]; then
      ask DB_IN "Database: [s]qlite or [p]ostgres (remote DSN)? (s/p)" "s"
    fi
    case "$DB_IN" in
      p|P|postgres)
        DB=postgres
        ask PG_HOST "Postgres host:" "localhost"
        ask PG_PORT "Postgres port:" "5432"
        ask PG_DB "Postgres database name:" "relay"
        ask PG_USER "Postgres user:" "relay"
        ask_secret PG_PW "Postgres password:" ""
        [ -n "$PG_PW" ] || die "postgres password required"
        log "Note: for a LOCALLY provisioned postgres use the native installer (systemd)."
        ;;
      *) DB=sqlite ;;
    esac
    if [ -z "$LAB_IN" ]; then
      ask LAB_IN "Environment: [h]omelab (HTTP) or [i]nternet (TLS)? (h/i)" "h"
    fi
    case "$LAB_IN" in
      i|I|internet) LAB=internet ;;
      *) LAB=homelab ;;
    esac
    if [ -n "${IOWAP_PORT:-}" ]; then
      DOCKER_PORT="$IOWAP_PORT"
    else
      ask DOCKER_PORT "Host port for the relay:" "8788"
    fi
    SCHEME=http; [ "$LAB" = "internet" ] && SCHEME=https
    TLS_ENV_ARGS=()
    if [ "$LAB" = "internet" ]; then
      ask TLS_CERT "TLS certificate file (host path):" ""
      ask TLS_KEY "TLS key file (host path):" ""
      [ -f "$TLS_CERT" ] || die "TLS cert not found: $TLS_CERT"
      [ -f "$TLS_KEY" ] || die "TLS key not found: $TLS_KEY"
      tls_dir="$(cd "$(dirname "$TLS_CERT")" && pwd)"
      TLS_MOUNT="-v $tls_dir:/certs:ro"
      TLS_BASE="$(basename "$TLS_CERT")"
      [ "$(basename "$TLS_KEY")" != "$TLS_BASE" ] && die "cert and key must live in the same directory"
      TLS_ENV_ARGS=(-e "RELAY_TLS_CERTFILE=/certs/$TLS_BASE" -e "RELAY_TLS_KEYFILE=/certs/$(basename "$TLS_KEY")")
    fi
    MASTER_SEED="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    log "Writing $ENVFILE..."
    {
      echo "RELAY_MASTER_SEED=$MASTER_SEED"
      echo "RELAY_SESSION_SECRET=$SESSION_SECRET"
      if [ "$LAB" = "homelab" ]; then
        echo "RELAY_SESSION_COOKIE_SECURE=false"
      else
        echo "RELAY_SESSION_COOKIE_SECURE=true"
      fi
      echo "RELAY_DB_TYPE=$DB"
      if [ "$DB" = "postgres" ]; then
        export IOWAP_PG_HOST IOWAP_PG_PORT IOWAP_PG_DB IOWAP_PG_USER IOWAP_PG_PW
        PG_DSN_VALUE="$(python3 - <<'PY'
import os
from urllib.parse import quote
e = os.environ
print(f"postgresql+psycopg://{quote(e['IOWAP_PG_USER'],safe='')}:{quote(e['IOWAP_PG_PW'],safe='')}"
      f"@{e['IOWAP_PG_HOST']}:{e['IOWAP_PG_PORT']}/{quote(e['IOWAP_PG_DB'],safe='')}")
PY
)"
        echo "RELAY_PG_DSN=$PG_DSN_VALUE"
      fi
      # Docker bridge network: mDNS advertisement would carry the container IP.
      echo "RELAY_ENABLE_MDNS=false"
      if [ "$LAB" = "internet" ]; then
        echo "RELAY_TLS_CERTFILE=/certs/$TLS_BASE"
        echo "RELAY_TLS_KEYFILE=/certs/$(basename "$TLS_KEY")"
      fi
      echo "IOWAP_DOCKER_PORT=$DOCKER_PORT"
      echo "IOWAP_DOCKER_SCHEME=$SCHEME"
      echo "IOWAP_DOCKER_TLS_DIR=${tls_dir:-}"
    } >"$ENVFILE"
    port="$DOCKER_PORT"
    scheme="$SCHEME"
    docker volume create "$VOLUME_NAME" >/dev/null
    log "Pulling image..."
    docker pull "$IMAGE" >/dev/null
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      -p "${port}:8788" \
      --env-file "$ENVFILE" \
      ${TLS_MOUNT:-} \
      -v "${VOLUME_NAME}:/app/.relay" \
      "$IMAGE" >/dev/null
    log "Container started: $CONTAINER_NAME (volume: $VOLUME_NAME)"
    if [ "$LAB" = "homelab" ]; then
      local lan_ip
      lan_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
      lan_ip="${lan_ip:-127.0.0.1}"
      log "Note: mDNS stays off under Docker (bridge net would advertise the container IP)."
      log "      Point nodes at explicit RELAY_URL=$scheme://$lan_ip:$port instead."
    fi
  fi
  log "Waiting for health check..."
  if docker_wait_healthy "$port"; then
    log "✓ Server ready (container up, /health ok)"
  fi
  docker_print_login "$scheme" "$port"
  if [ "$phase" = "fresh" ]; then
    log "Seed applied on first start (see $ENVFILE)."
    docker_print_seed "$MASTER_SEED"
  else
    log "Seed/env unchanged (see $ENVFILE)."
  fi
  log ""
  log "Done. Next:"
  log "  - logs: docker logs $CONTAINER_NAME"
  log "  - uninstall: re-run installer (Docker detected -> options)"
}

# ---------- menus / mode detection ----------
installed=0
if [ -d "$INSTALL_DIR/.git" ] && [ -d "$INSTALL_DIR/.venv" ]; then
  installed=1
fi

DOCKER_UNINSTALL=0
if [ -z "$MODE" ]; then
  if [ "$installed" = "1" ]; then
    log "iowap-server already installed (native) in $INSTALL_DIR"
    ask MODE "Choose: [u]pdate / [o]verride / [x]uninstall / [a]bort? (u/o/x/a)" "u"
    case "$MODE" in
      u|U|update) MODE=update ;;
      o|O|override) MODE=override ;;
      x|X|uninstall) MODE=uninstall ;;
      *) MODE=abort ;;
    esac
  elif container_exists; then
    log "Docker container '$CONTAINER_NAME' already running an iowap-server"
    if [ "$NONINTERACTIVE" = "1" ]; then
      log "NONINTERACTIVE with an existing container is ambiguous - aborting (set IOWAP_MODE=dupdate|duninstall or run interactively)."
      exit 0
    fi
    ask MODE "Choose: [u]pdate (pull+recreate) / [x]uninstall / [n]ew native install / [a]bort? (u/x/n/a)" "u"
    case "$MODE" in
      u|U|update) MODE=dupdate ;;
      x|X|uninstall) MODE=duninstall ;;
      n|N|native) MODE=""; DEPLOY_IN="native" ;;
      *) MODE=abort ;;
    esac
  fi
fi

DEPLOY="native"
if [ -z "$MODE" ] || [ "$MODE" = "override" ]; then
  if [ "$DOCKER" = "1" ]; then
    if [ -z "$DEPLOY_IN" ]; then
      ask DEPLOY_IN "Deploy method: [n]ative (systemd, git clone) or [d]ocker (GHCR image)? (n/d)" "n"
    fi
    case "$DEPLOY_IN" in
      d|D|docker) DEPLOY="docker" ;;
      *) DEPLOY="native" ;;
    esac
  fi
  if [ "$MODE" = "override" ] && [ "$DEPLOY" = "docker" ]; then
    MODE=""   # docker override == fresh docker deploy
  fi
fi

case "$MODE" in
  abort) log "Aborted."; exit 0 ;;
  duninstall)
    log "Removing Docker deployment..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    ask DELVOL "Remove volume $VOLUME_NAME (DB + master seed live there)? (y/n)" "n"
    if [[ "$DELVOL" =~ ^[Yy] ]]; then docker volume rm "$VOLUME_NAME" >/dev/null 2>&1 || true; fi
    ask DELENV "Remove $ENVFILE? (y/n)" "n"
    if [[ "$DELENV" =~ ^[Yy] ]]; then rm -f "$ENVFILE"; fi
    log "Docker deployment removed. Node registrations on other machines are NOT removed."
    exit 0
    ;;
  dupdate)
    DOCKER_UNINSTALL=0
    docker_ask_and_run update
    exit 0
    ;;
  uninstall)
    log "Uninstalling (native)..."
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/${UNIT_NAME}.service"
    systemctl --user daemon-reload 2>/dev/null || true
    if [ -n "$SUDO" ]; then
      $SUDO systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
      $SUDO rm -f "/etc/systemd/system/${UNIT_NAME}.service"
      $SUDO systemctl daemon-reload 2>/dev/null || true
    fi
    rm -rf "$INSTALL_DIR"
    ask DELRELAY "Remove $RELAY_HOME_DIR (config, sqlite DB, artifacts)? (y/n)" "n"
    if [[ "$DELRELAY" =~ ^[Yy] ]]; then rm -rf "$RELAY_HOME_DIR"; fi
    if command -v psql >/dev/null 2>&1 && [ -n "$SUDO" ]; then
      ask DELLPG "Drop local postgres database 'iowap' and role? (y/n)" "n"
      if [[ "$DELLPG" =~ ^[Yy] ]]; then
        $SUDO -u postgres dropdb iowap 2>/dev/null || true
        $SUDO -u postgres dropuser iowap 2>/dev/null || true
        log "Local postgres database/role removed."
      fi
    fi
    log "Uninstalled."
    log "Note: a REMOTE postgres DB and node registrations on other machines are NOT removed."
    log "Local git sources removed: $INSTALL_DIR"
    exit 0
    ;;
esac

ensure_deps

# ---------- Docker fresh path (decision happened BEFORE any clone) ----------
if [ "$DEPLOY" = "docker" ]; then
  docker_ask_and_run fresh
  exit 0
fi

# ---------- Native path ----------
if [ "$MODE" = "override" ]; then
  rm -rf "$INSTALL_DIR"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  log "Updating existing checkout: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
  git -C "$INSTALL_DIR" submodule update --init --checkout 2>/dev/null || true
else
  log "Cloning iowap-server into $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
  if ! git -C "$INSTALL_DIR" submodule update --init --checkout 2>/dev/null; then
    log "⚠ docs submodule not fetched - server boots, but docs pages need it"
  fi
fi

cd "$INSTALL_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate

if [ "$MODE" != "update" ]; then
  # ---- Questions (fresh/override only) ----
  if [ -z "$DB_IN" ]; then
    ask DB_IN "Database: [s]qlite or [p]ostgres? (s/p)" "s"
  fi
  case "$DB_IN" in
    p|P|postgres) DB_KIND=postgres ;;
    *) DB_KIND=sqlite ;;
  esac

  if [ -z "$LAB_IN" ]; then
    ask LAB_IN "Environment: [h]omelab (HTTP+mDNS) or [i]nternet (TLS, no mDNS)? (h/i)" "h"
  fi
  case "$LAB_IN" in
    i|I|internet) LAB_KIND=internet ;;
    *) LAB_KIND=homelab ;;
  esac

  if [ "$DB_KIND" = "postgres" ]; then
    pip install -e ".[postgres]" >/dev/null
  else
    pip install -e . >/dev/null
  fi
  log "✓ Python dependencies installed"

  # ---- Postgres provisioning ----
  if [ "$DB_KIND" = "postgres" ]; then
    ask PG_MODE "Postgres: [l]ocal install or [r]emote? (l/r)" "l"
    if [[ "$PG_MODE" =~ ^[Ll] ]]; then
      PG_MODE=local
      if ! command -v psql >/dev/null 2>&1; then
        log "Installing postgresql package..."
        if command -v apt-get >/dev/null 2>&1; then
          ${SUDO:+"${SUDO}"} apt-get install -y postgresql || true
        elif command -v dnf >/dev/null 2>&1; then
          ${SUDO:+"${SUDO}"} dnf install -y postgresql-server || true
          ${SUDO:+"${SUDO}"} postgresql-setup --initdb 2>/dev/null || true
        elif command -v pacman >/dev/null 2>&1; then
          ${SUDO:+"${SUDO}"} pacman -Sy --noconfirm postgresql || true
        fi
      fi
      if [ -n "$SUDO" ]; then
        ${SUDO} systemctl enable --now postgresql 2>/dev/null || true
        PG_PW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')"
        $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='iowap'" 2>/dev/null | grep -q 1 \
          || $SUDO -u postgres psql -c "CREATE USER iowap WITH PASSWORD '$PG_PW';" || true
        $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='iowap'" 2>/dev/null | grep -q 1 \
          || $SUDO -u postgres createdb -O iowap iowap || true
        DB_KIND=postgres_local
      else
        log "No passwordless sudo - create the local DB manually, then re-run or choose remote."
        die "cannot provision local postgres without sudo"
      fi
    else
      PG_MODE=remote
      ask PG_HOST "Postgres host:" "localhost"
      ask PG_PORT "Postgres port:" "5432"
      ask PG_DB "Postgres database name:" "iowap"
      ask PG_USER "Postgres user:" "iowap"
      ask_secret PG_PW "Postgres password:" ""
      [ -n "$PG_PW" ] || die "postgres password required for remote DB"
      DB_KIND=postgres_remote
    fi
  else
    DB_KIND=sqlite
  fi

  if [ -n "${IOWAP_PORT:-}" ]; then
    SERVER_PORT="$IOWAP_PORT"
  else
    ask SERVER_PORT "Server port:" "8788"
  fi
  if [ "$LAB_KIND" = "internet" ]; then
    ask TLS_CERT "TLS certificate file path:" ""
    ask TLS_KEY "TLS key file path:" ""
    [ -f "$TLS_CERT" ] || die "TLS cert not found: $TLS_CERT"
    [ -f "$TLS_KEY" ] || die "TLS key not found: $TLS_KEY"
  fi

  # ---- Write ~/.relay/config.yaml (outside install dir; survives override) ----
  mkdir -p "$RELAY_HOME_DIR"
  log "Writing $RELAY_HOME_DIR/config.yaml..."
  if [ -f "$RELAY_HOME_DIR/config.yaml" ]; then
    cp "$RELAY_HOME_DIR/config.yaml" "$RELAY_HOME_DIR/config.yaml.bak-install-$(date +%Y%m%d%H%M%S)"
  fi
  export IOWAP_DB_KIND="$DB_KIND" IOWAP_LAB="$LAB_KIND" IOWAP_PORT="$SERVER_PORT"
  export IOWAP_PG_MODE="${PG_MODE:-}" IOWAP_PG_HOST="${PG_HOST:-}" IOWAP_PG_PORT="${PG_PORT:-}"
  export IOWAP_PG_DB="${PG_DB:-}" IOWAP_PG_USER="${PG_USER:-}" IOWAP_PG_PW="${PG_PW:-}"
  export IOWAP_TLS_CERT="${TLS_CERT:-}" IOWAP_TLS_KEY="${TLS_KEY:-}"
  CONFIG_OUT="$("$INSTALL_DIR/.venv/bin/python" - <<'PY'
import os, secrets, yaml
from urllib.parse import quote
p = os.path.expanduser("~/.relay/config.yaml")
cfg = {}
if os.path.exists(p):
    cfg = yaml.safe_load(open(p)) or {}
cfg["host"] = "0.0.0.0"
cfg["port"] = int(os.environ["IOWAP_PORT"])
kind, lab = os.environ["IOWAP_DB_KIND"], os.environ["IOWAP_LAB"]
if kind == "sqlite":
    cfg["db_type"], cfg["pg_dsn"] = "sqlite", ""
elif kind == "postgres_local":
    cfg["db_type"] = "postgres"
    cfg["pg_dsn"] = f"postgresql+psycopg://iowap:{quote(os.environ['IOWAP_PG_PW'], safe='')}@127.0.0.1:5432/iowap"
else:
    e = os.environ
    cfg["db_type"] = "postgres"
    cfg["pg_dsn"] = (f"postgresql+psycopg://{quote(e['IOWAP_PG_USER'], safe='')}:{quote(e['IOWAP_PG_PW'], safe='')}"
                     f"@{e['IOWAP_PG_HOST']}:{e['IOWAP_PG_PORT']}/{quote(e['IOWAP_PG_DB'], safe='')}")
if lab == "homelab":
    cfg["enable_mdns"] = True
    cfg["session_cookie_secure"] = False
    cfg.pop("tls_certfile", None); cfg.pop("tls_keyfile", None)
else:
    cfg["enable_mdns"] = False
    cfg["session_cookie_secure"] = True
    cfg["tls_certfile"], cfg["tls_keyfile"] = e["IOWAP_TLS_CERT"], e["IOWAP_TLS_KEY"]
cfg.setdefault("session_secret", secrets.token_urlsafe(32))
seed_pre = "master_seed" in cfg
cfg.setdefault("master_seed", secrets.token_urlsafe(24))
yaml.safe_dump(cfg, open(p, "w"), sort_keys=False)
print("SEED_PREEXISTING" if seed_pre else "SEED_GENERATED")
print(cfg["master_seed"])
PY
)"
  SEED_FLAG="$(printf '%s' "$CONFIG_OUT" | head -n1)"
  SEED_VALUE="$(printf '%s' "$CONFIG_OUT" | tail -n1)"
else
  MODE=update
  pip install -e . >/dev/null
  log "✓ Python dependencies updated"
fi

# ---- systemd unit ----
SYSTEM_UNIT_DIR="/etc/systemd/system"
SERVICE_USER="$(id -un)"
write_unit() { # $1 = system|user
  if [ "$1" = "system" ]; then
    cat <<EOF
[Unit]
Description=IOWAP Relay Server
After=network.target postgresql.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/relay-server server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  else
    cat <<EOF
[Unit]
Description=IOWAP Relay Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=HOME=$HOME
ExecStart=$INSTALL_DIR/.venv/bin/relay-server server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  fi
}
if [ "$(id -u)" = "0" ]; then
  write_unit system | tee "$SYSTEM_UNIT_DIR/${UNIT_NAME}.service" >/dev/null
  systemctl daemon-reload || true
  log "Systemd system unit written: $SYSTEM_UNIT_DIR/${UNIT_NAME}.service (root run)"
else
  USER_UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$USER_UNIT_DIR"
  write_unit user >"$USER_UNIT_DIR/${UNIT_NAME}.service"
  systemctl --user daemon-reload 2>/dev/null || true
  log "Systemd user unit written: $USER_UNIT_DIR/${UNIT_NAME}.service"
fi

ENABLED=0
if [ "$MODE" != "update" ]; then
  ask ENABLE "Enable/start $UNIT_NAME via systemd? (y/n)" "y"
  if [[ "$ENABLE" =~ ^[Yy] ]]; then
    if [ "$(id -u)" = "0" ]; then
      systemctl enable --now "$UNIT_NAME" && ENABLED=1 || log "systemctl enable failed"
    else
      systemctl --user enable --now "$UNIT_NAME" && ENABLED=1 || log "systemctl --user enable failed (no user session?)"
      if [ "$ENABLED" = "1" ] && [ -n "$SUDO" ] && [ "$NONINTERACTIVE" != "1" ]; then
        ask LINGER "Enable lingering (service survives logout/reboot, sudo prompt)? (y/n)" "y"
        if [[ "$LINGER" =~ ^[Yy] ]]; then
          $SUDO loginctl enable-linger "$SERVICE_USER" 2>/dev/null \
            || log "enable-linger failed - without it the service stops at logout."
        fi
      else
        log "Note: without lingering the user service stops at logout. Enable via: sudo loginctl enable-linger $SERVICE_USER"
      fi
    fi
  fi
fi

# ---- Master seed bootstrap ----
# The DB's admin_seeds row wins over any config seed (core/auth.py
# init_master_seed returns None -> init-master exits 1 with "already exists").
# Only print a seed value that init-master actually just applied; after a
# wiped-config re-install the freshly generated config seed would otherwise
# be printed although it can never log in (QA-audit finding 2026-10-17).
if [ "$MODE" != "update" ]; then
  log "Initializing master admin seed..."
  SEED_INIT_TMP="$(mktemp)"
  SEED_INIT_RC=0
  "$INSTALL_DIR/.venv/bin/relay-server" admin init-master 2>"$SEED_INIT_TMP" || SEED_INIT_RC=$?
  SEED_INIT_MSG="$(cat "$SEED_INIT_TMP" 2>/dev/null || true)"
  rm -f "$SEED_INIT_TMP"
  SCHEME="http"; [ "$LAB_KIND" = "internet" ] && SCHEME="https"
  LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  LAN_IP="${LAN_IP:-127.0.0.1}"
  log ""
  log "Dashboard login: $SCHEME://$LAN_IP:$SERVER_PORT/relay/v2/dashboard/login"
  log "  (local: $SCHEME://127.0.0.1:$SERVER_PORT/relay/v2/dashboard/login)"
  if [ "$SEED_INIT_RC" = "0" ]; then
    log "Master admin seed: $SEED_VALUE"
  elif [ "$SEED_INIT_RC" = "1" ] && printf '%s' "$SEED_INIT_MSG" | grep -q "already exists"; then
    if [ "$SEED_FLAG" = "SEED_GENERATED" ]; then
      "$INSTALL_DIR/.venv/bin/python" - <<PY
from relay_server.config import purge_master_seed
purge_master_seed()
PY
      log "Master admin seed: already configured in the database (freshly generated config seed discarded - it would not log in)."
    else
      log "Master admin seed: already configured in the database."
    fi
  else
    log "Seed init could not verify against the database (rc=$SEED_INIT_RC). Config seed kept, verify manually before trusting it:"
    log "Master admin seed: $SEED_VALUE"
  fi
  log "Seed login is the bootstrap admin - valid until the first human admin exists"
  log "(recovery afterwards: set enable_master_seed_login=true in config)."
  log "⚠ Store the seed securely - it grants admin access."
fi

# ---- Health verification ----
if [ "$ENABLED" = "1" ]; then
  log "Verifying /health + /ready..."
  OK=0
  for _ in 1 2 3 4 5 6; do
    if curl -sf "http://127.0.0.1:${SERVER_PORT:-8788}/health" >/dev/null 2>&1; then OK=1; break; fi
    sleep 2
  done
  if [ "$OK" = "1" ]; then
    curl -sf "http://127.0.0.1:${SERVER_PORT:-8788}/ready" >/dev/null 2>&1 \
      && log "✓ Server ready (health + ready ok)" \
      || log "⚠ /health ok but /ready failing - check logs: journalctl -u $UNIT_NAME"
  else
    log "✗ Server did not come up within 12s - check: journalctl -u $UNIT_NAME"
  fi
fi

log ""
log "Done. Next:"
log "  - config: $RELAY_HOME_DIR/config.yaml"
log "  - service: systemctl status $UNIT_NAME"
log "  - register nodes from other machines: node installer (iowap-node install.sh)"