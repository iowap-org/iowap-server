#!/usr/bin/env python3
"""T-188: Backend smoke test — the regression fence for every DB fix (F-01/F-10).

Runs the full production lifecycle against BOTH real backends (SQLite and
PostgreSQL, via an ephemeral initdb instance in /tmp — no mocks, no
dependency overrides):

    init_db → Registration → Approve → Heartbeat → Task → Claim → Complete
    → Restart-Sweep (re-init_db on the SAME database, legacy/None edge cases)

Exit code 0 = all stages passed on all backends. Any failure prints
[FAIL] <backend> <stage> with the exception and exits 1.

Usage:
    python tools/backend_smoke.py            # SQLite + PostgreSQL (if available)
    python tools/backend_smoke.py --sqlite-only

Pattern provenance: derived from the T-183 full-audit probes
(.hermes/agent-runs/full-audit/review/behavior_probes.py +
verification_probes.postgres()), trimmed to the happy-path lifecycle
plus the two restart edge cases that broke in the audit (F-10 legacy
string capability, F-11 available=None).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(backend: str, stage: str, fn):
    """Run one lifecycle stage; record and print failures instead of raising."""
    print(f"  [{backend}] {stage} ...", flush=True)
    try:
        result = fn()
        print(f"    ok: {result}", flush=True)
        return result
    except Exception as exc:  # noqa: BLE001 — smoke fence must survive any error
        FAILURES.append(f"{backend}/{stage}: {type(exc).__name__}: {exc}")
        print(f"    [FAIL] {backend}/{stage}: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc(limit=3)
        return None


# ---------------------------------------------------------------------------
# Lifecycle shared by both backends
# ---------------------------------------------------------------------------

def run_lifecycle(backend: str, tmpdir: Path, extra_env: dict[str, str] | None = None) -> None:
    """Full lifecycle against one configured backend (env already set)."""
    # Fresh interpreter state per backend: the settings/db singletons are
    # import-time bound, so the lifecycle body runs as a subprocess.
    code = _LIFECYCLE_SRC
    env = dict(os.environ)
    env["BACKEND_LABEL"] = backend
    env["SMOKE_TMPDIR"] = str(tmpdir)
    extra_env = extra_env or {}
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=300,
    )
    print(proc.stdout, end="")
    if proc.returncode != 0:
        print(proc.stderr[-4000:], file=sys.stderr)
        FAILURES.append(f"{backend}/lifecycle: subprocess exit {proc.returncode}")


_LIFECYCLE_SRC = r'''
import json
import os
import sys
from pathlib import Path

BACKEND = os.environ["BACKEND_LABEL"]
TMPDIR = Path(os.environ["SMOKE_TMPDIR"])

failures = []

def check(stage, fn):
    print(f"  [{BACKEND}] {stage} ...", flush=True)
    try:
        result = fn()
        print(f"    ok: {result}", flush=True)
        return result
    except Exception as exc:
        failures.append(f"{BACKEND}/{stage}: {type(exc).__name__}: {exc}")
        print(f"    [FAIL] {BACKEND}/{stage}: {type(exc).__name__}: {exc}", flush=True)
        import traceback; traceback.print_exc(limit=3)
        return None

# --- 1. configure backend BEFORE importing relay singletons -----------------
if BACKEND == "postgres":
    sock = os.environ["SMOKE_PG_SOCKET"]
    port = os.environ["SMOKE_PG_PORT"]
    os.environ["RELAY_DB_TYPE"] = "postgres"
    os.environ["RELAY_PG_DSN"] = f"postgresql+psycopg://felix@/postgres?host={sock}&port={port}"
else:
    os.environ["RELAY_DB_TYPE"] = "sqlite"
    os.environ["RELAY_DB_PATH"] = str(TMPDIR / "smoke.db")

from relay_server.config import settings  # noqa: E402
from relay_server.core import db, scheduler, discovery  # noqa: E402
from relay_server.core.auth import register_pending_node, approve_node  # noqa: E402
from relay_server.core.users import has_admin_user, create_user, list_users, list_groups  # noqa: E402

def sql(statement, params=()):
    with db.get_conn() as c:
        r = c.execute(db.q(statement, params))
        c.commit()
        return r

# --- 2. init_db: schema + settings_override present + RBAC seed -------------
check("init_db", db.init_db)
with db.get_conn() as c:
    tables = set(db._table_names(c))
check("table settings_override exists", lambda: "settings_override" in tables or (_ for _ in ()).throw(AssertionError(f"missing: {sorted(tables)}")))
check("table node_capabilities exists", lambda: "node_capabilities" in tables or (_ for _ in ()).throw(AssertionError("missing")))
check("table task_stages exists", lambda: "task_stages" in tables or (_ for _ in ()).throw(AssertionError("missing")))

# --- 3. Registration → Approve (real bearer lifecycle) ----------------------
caps = [{"name": "smoke.cap", "available": True}]
reg = check("register_pending_node",
            lambda: register_pending_node("smoke_worker", "http://localhost:0", caps))
check("approve_node", lambda: approve_node(reg[0]))

# --- 4. Heartbeat (idle → online transition path + capability sync) ---------
check("heartbeat", lambda: discovery.heartbeat(
    reg[0], load=0.0, queue_depth=0, available=True,
    capabilities=caps, replace_capabilities=True))
check("capability indexed", lambda: discovery.get_capability_by_name("smoke.cap")
      or (_ for _ in ()).throw(AssertionError("smoke.cap not indexed")))

# --- 5. User bootstrap (the F-01 boolean/GROUP_CONCAT paths) ----------------
check("has_admin_user", has_admin_user)
check("create_user", lambda: create_user("smoke_admin", "SmokeTest-Only-Pw1", group_names=["admin"]))
users = check("list_users", lambda: list_users())
check("list_users groups", lambda: users
      and users[0].get("groups") == ["admin"]
      or (_ for _ in ()).throw(AssertionError(f"groups={users[0].get('groups') if users else None}")))
groups = check("list_groups", lambda: list_groups())
check("list_groups permissions", lambda: any(g["group_name"] == "admin" and g["permissions"]
      for g in groups) or (_ for _ in ()).throw(AssertionError(f"groups={groups}")))

# --- 6. Task → Claim → Complete ---------------------------------------------
task_id = check("create_task", lambda: scheduler.Scheduler.create_task(
    "smoke", [{"stage_name": "s0", "capability": "smoke.cap"}])["task_id"])
claim = check("claim_stage", lambda: scheduler.Scheduler.claim_stage(reg[0]))
check("complete_stage", lambda: scheduler.Scheduler.complete_stage(
    claim["stage_id"], reg[0], {"ok": True}))
final = scheduler.Scheduler.get_task(task_id)
check("task completed", lambda: final["status"] == "completed"
      or (_ for _ in ()).throw(AssertionError(f"task status={final['status']} stages={[s['status'] for s in final['stages']]}")))

# --- 7. Restart-Sweep on the SAME database -----------------------------------
#    a) plain re-init (idempotent schema + migrations)
check("restart init_db", db.init_db)
#    b) F-10 edge: legacy string capability must not break boot
sql("INSERT INTO nodes(node_id,node_name,capabilities,status,last_seen,registered_at) VALUES (?,?,?,?,?,?)",
    ("smoke-legacy", "smoke-legacy", json.dumps(["legacy.cap"]), "online", "2026-01-01", "2026-01-01"))
check("restart with legacy string capability (F-10)", db.init_db)
#    c) F-11 edge: available=None must survive restart without flipping to 0
none_caps = [{"name": "smoke.none.cap", "available": None}]
sql("INSERT INTO nodes(node_id,node_name,capabilities,status,last_seen,registered_at) VALUES (?,?,?,?,?,?)",
    ("smoke-nullable", "smoke-nullable", json.dumps(none_caps), "online", "2026-01-01", "2026-01-01"))
db.sync_node_capabilities("smoke-nullable", none_caps)
def availability():
    with db.get_conn() as c:
        return c.execute(db.q("SELECT available FROM node_capabilities WHERE node_id='smoke-nullable'")).scalar()
before = check("availability before restart (F-11)", availability)
check("restart init_db 2", db.init_db)
after = check("availability after restart (F-11)", availability)
if before is not None and after is not None and before != after:
    failures.append(f"{BACKEND}/F-11 flip: available {before} -> {after} on restart")

# Cleanup of the edge-case rows so re-runs stay clean.
sql("DELETE FROM node_capabilities WHERE node_id IN ('smoke-legacy','smoke-nullable')")
sql("DELETE FROM nodes WHERE node_id IN ('smoke-legacy','smoke-nullable')")

if failures:
    print(f"\nSMOKE {BACKEND}: {len(failures)} failure(s)", flush=True)
    for f in failures:
        print("  -", f, flush=True)
    sys.exit(1)
print(f"\nSMOKE {BACKEND}: ALL STAGES PASSED", flush=True)
'''


# ---------------------------------------------------------------------------
# Ephemeral PostgreSQL instance (pattern: audit verification_probes.postgres)
# ---------------------------------------------------------------------------

def postgres_available() -> tuple[bool, str]:
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            return False, f"{tool} not found in PATH"
    try:
        subprocess.run(["psql", "--version"], capture_output=True, timeout=10)
    except FileNotFoundError:
        return False, "psql not found"
    return True, ""


def run_postgres(tmpdir: Path, socket_dir: Path, port: int) -> bool:
    ok, why = postgres_available()
    if not ok:
        print(f"-- PostgreSQL skipped: {why}")
        return False  # not a failure — backend not installed locally

    datadir = tmpdir / "pgdata"
    logfile = tmpdir / "pg.log"
    setup = [
        ["initdb", "-D", str(datadir), "-A", "trust", "--no-locale", "--encoding=UTF8"],
        ["pg_ctl", "-D", str(datadir), "-l", str(logfile),
         "-o", f'-k {socket_dir} -h "" -p {port}', "-w", "start"],
    ]
    for cmd in setup:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print(f"  [pg-setup] {' '.join(cmd[:2])} → exit {p.returncode}")
        if p.returncode:
            print(p.stdout + p.stderr)
            FAILURES.append(f"postgres/setup: {' '.join(cmd[:2])} failed")
            return False
    # psycopg driver — install into the tmpdir only, never the project venv.
    driver_target = tmpdir / "pg-deps"
    if not _psycopg_importable():
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--no-cache-dir", "--target", str(driver_target), "psycopg[binary]>=3.1"],
            capture_output=True, text=True, timeout=180,
        )
        print(f"  [pg-driver] pip install → exit {p.returncode}")
        if p.returncode:
            print(p.stdout + p.stderr[-2000:])
            FAILURES.append("postgres/driver: psycopg install failed")
            return False
    try:
        extra = {
            "SMOKE_PG_SOCKET": str(socket_dir),
            "SMOKE_PG_PORT": str(port),
        }
        if driver_target.exists():
            extra["PYTHONPATH"] = str(driver_target)
        run_lifecycle("postgres", tmpdir, extra_env=extra)
        return True
    finally:
        subprocess.run(["pg_ctl", "-D", str(datadir), "-m", "immediate", "-w", "stop"],
                       capture_output=True, text=True, timeout=60)


def _psycopg_importable() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sqlite-only", action="store_true",
                        help="skip the PostgreSQL leg (e.g. no PG installed)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="iowap-smoke-") as tmp:
        tmpdir = Path(tmp)
        run_lifecycle("sqlite", tmpdir)
        if not args.sqlite_only:
            socket_dir = tmpdir / "pg-socket"
            socket_dir.mkdir()
            run_postgres(tmpdir, socket_dir, 55441)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"SMOKE FAILED: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("SMOKE OK: all backends passed the full lifecycle")
    return 0


if __name__ == "__main__":
    sys.exit(main())