"""Central maintenance scheduler (T-050).

Bündelt alle periodischen Watchdog-/Cleanup-Tasks des Servers in einem
einzigen :class:`MaintenanceScheduler`. Anstatt in ``main.py`` mehrere
unabhängige ``asyncio``-Tasks mit jeweils eigenem Sleep-Timer zu pflegen,
gibt es nur noch einen Loop, der alle ``settings.maintenance_interval_seconds``
Sekunden :meth:`MaintenanceScheduler.run_due` aufruft. Jeder registrierte
Task läuft nur, wenn sein individuelles Intervall abgelaufen ist.

Registrierte Tasks (Default-Intervalle siehe :meth:`register_defaults`):

* ``heartbeat_watchdog``        — :func:`relay_server.core.discovery.mark_offline_nodes`
* ``claim_ttl_watchdog``        — :meth:`relay_server.core.scheduler.Scheduler.release_or_fail_claims`
* ``token_cleanup``             — purge expired ``node_tokens`` rows
* ``artifact_cleanup``          — :func:`relay_server.core.artifacts.cleanup_orphaned_artifacts` (T-049)
* ``chunked_upload_cleanup``    — :meth:`ChunkedUploadManager.prune_stale`
* ``orphaned_stage_cleanup``    — :meth:`relay_server.core.scheduler.Scheduler.fail_orphaned_stages` (T-063)
* ``db_vacuum``                 — WAL-Checkpoint + ``VACUUM`` (1x täglich)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn

logger = logging.getLogger("relay.maintenance")


# ---------------------------------------------------------------------------
# Token cleanup (inline — small enough, no separate module needed)
# ---------------------------------------------------------------------------


def _purge_expired_tokens() -> Dict[str, Any]:
    """Delete expired rows from ``node_tokens``. Returns ``{"deleted": n}``."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        deleted = conn.execute(
            "DELETE FROM node_tokens WHERE expires_at < ?", (now,)
        ).rowcount
        conn.commit()
        return {"deleted": deleted}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB vacuum (WAL checkpoint + VACUUM)
# ---------------------------------------------------------------------------


def _db_vacuum() -> Dict[str, Any]:
    """Run a WAL checkpoint and VACUUM to reclaim space.

    Returns ``{"checkpointed": bool, "vacuumed": bool}``. Errors are
    logged but never raised — VACUUM can legitimately fail when another
    connection holds a write lock; the next run will retry.
    """
    checkpointed = False
    vacuumed = False
    conn = get_conn()
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            checkpointed = True
        except sqlite3.OperationalError as exc:
            logger.warning("WAL checkpoint failed: %s", exc)
        try:
            conn.execute("VACUUM")
            vacuumed = True
        except sqlite3.OperationalError as exc:
            logger.warning("VACUUM failed: %s", exc)
        conn.commit()
    finally:
        conn.close()
    return {"checkpointed": checkpointed, "vacuumed": vacuumed}


def _ssn_auto_approve() -> Dict[str, Any]:
    """Auto-approve pending SSN node registrations (T-069).

    The SSN registers as a normal worker node. When ``ssn_auto_approve``
    is enabled we periodically sweep for pending worker nodes and approve
    them so the SSN can transition to ``online`` on its first heartbeat.
    """
    if not settings.ssn_auto_approve:
        return {"approved": 0}
    from relay_server.core.auth import approve_node  # noqa: PLC0415

    approved = 0
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT node_id FROM nodes WHERE status = 'pending' AND role = 'worker'"
        ).fetchall()
        node_ids = [r["node_id"] for r in rows]
    finally:
        conn.close()
    for node_id in node_ids:
        if approve_node(node_id) is not None:
            approved += 1
    return {"approved": approved}


# ---------------------------------------------------------------------------
# MaintenanceScheduler
# ---------------------------------------------------------------------------


class MaintenanceScheduler:
    """Registry + dispatcher für periodische Maintenance-Tasks.

    Singleton-Charakter wird durch den Caller (``main.py``) sichergestellt,
    der eine Instanz pro Prozess erzeugt. Die Klasse selbst ist zustandslos
    gegenüber der Außenwelt — alle registrierten Funktionen sind reine
    DB-Operationen.
    """

    def __init__(self) -> None:
        # name -> {"func", "interval", "last_run"}
        self._tasks: Dict[str, Dict[str, Any]] = {}

    # -- registration --------------------------------------------------

    def register(
        self, name: str, func: Callable[[], Dict[str, Any]], interval_seconds: int
    ) -> None:
        """Register oder ersetze einen Maintenance-Task.

        ``func`` muss eine Null-arg-Callable sein, die ein dict zurückgibt
        (leeres dict = nichts zu tun / no-op). Ein bereits existierender
        Task gleichen Namens wird überschrieben; ``last_run`` wird auf 0
        zurückgesetzt, damit der Task beim nächsten ``run_due`` sofort läuft.
        """
        self._tasks[name] = {
            "func": func,
            "interval": int(interval_seconds),
            "last_run": 0.0,
        }

    def unregister(self, name: str) -> None:
        """Entfernt einen registrierten Task (no-op falls nicht vorhanden)."""
        self._tasks.pop(name, None)

    # -- execution -----------------------------------------------------

    def run_due(self) -> Dict[str, Dict[str, Any]]:
        """Führt alle Tasks aus, deren Intervall abgelaufen ist.

        Returns ``{name: result}`` nur für Tasks die tatsächlich liefen
        (also fällig waren). Fehler in einem Task beeinflussen nicht die
        Ausführung der anderen — sie werden geloggt und als
        ``{"error": str(exc)}`` ins Ergebnis eingetragen.
        """
        now = time.monotonic()
        results: Dict[str, Dict[str, Any]] = {}
        for name, entry in list(self._tasks.items()):
            if now - entry["last_run"] < entry["interval"]:
                continue
            try:
                result = entry["func"]() or {}
            except Exception as exc:  # noqa: BLE001 — maintenance darf nie raise
                logger.exception("Maintenance task '%s' failed: %s", name, exc)
                result = {"error": str(exc)}
            entry["last_run"] = now
            results[name] = result
        return results

    def run_all(self) -> Dict[str, Dict[str, Any]]:
        """Führt **alle** registrierten Tasks sofort aus (ignoriert Intervall).

        Wird beim graceful Shutdown verwendet, um ein letztes Cleanup
        durchzuführen. Die ``last_run``-Zeit wird dabei aktualisiert.
        """
        results: Dict[str, Dict[str, Any]] = {}
        for name, entry in list(self._tasks.items()):
            try:
                result = entry["func"]() or {}
            except Exception as exc:  # noqa: BLE001
                logger.exception("Maintenance task '%s' failed: %s", name, exc)
                result = {"error": str(exc)}
            entry["last_run"] = time.monotonic()
            results[name] = result
        return results

    # -- introspection -------------------------------------------------

    def status(self) -> List[Dict[str, Any]]:
        """Gibt eine sortierte Status-Liste aller registrierten Tasks zurück.

        Jeder Eintrag: ``{name, interval, last_run, next_run}``.
        ``last_run``/``next_run`` sind ``time.monotonic()``-Sekunden
        (relativ zum Prozess) — geeignet für Diagnose/Logging, nicht für
        Persistenz.
        """
        now = time.monotonic()
        out: List[Dict[str, Any]] = []
        for name, entry in sorted(self._tasks.items()):
            last = float(entry["last_run"])
            interval = int(entry["interval"])
            next_run = last + interval if last else 0.0
            out.append(
                {
                    "name": name,
                    "interval": interval,
                    "last_run": last,
                    "next_run": next_run,
                    "due": (last == 0.0) or (now - last >= interval),
                }
            )
        return out

    # -- default registry ----------------------------------------------

    def register_defaults(self) -> None:
        """Registriert die Standard-Task-Menge des Servers.

        Intervalle werden aus :mod:`relay_server.config.settings` gelesen,
        sodass sie via YAML/Env übersteuerbar sind. Importe sind bewusst
        lazy (inline), damit ``maintenance`` importiert werden kann, bevor
        ``init_db`` gelaufen ist — und damit Test-Code gezielt einzelne
        Tasks durch Mocks ersetzen kann.
        """
        from relay_server.core.discovery import mark_offline_nodes
        from relay_server.core.scheduler import Scheduler
        from relay_server.core.artifacts import cleanup_orphaned_artifacts
        from relay_server.core.chunked_upload import chunked_manager

        # Heartbeat / offline watchdog — kurzes Intervall, da direkt vom
        # heartbeat_timeout_multiplier abgeleitet.
        self.register(
            "heartbeat_watchdog",
            lambda: {"offline": mark_offline_nodes()},
            settings.heartbeat_interval_seconds,
        )

        # Claim-TTL Watchdog — längeres Intervall als heartbeat, da claims
        # eine Minute TTL haben.
        self.register(
            "claim_ttl_watchdog",
            Scheduler.release_or_fail_claims,
            settings.claim_ttl_seconds,
        )

        # Token cleanup — einmal pro Stunde.
        self.register("token_cleanup", _purge_expired_tokens, 3600)

        # Artifact cleanup (T-049) — einmal pro Stunde, nur Artifakte
        # älter als artifact_cleanup_max_age_days.
        self.register(
            "artifact_cleanup",
            lambda: cleanup_orphaned_artifacts(
                max_age_days=settings.artifact_cleanup_max_age_days
            ),
            3600,
        )

        # Chunked-upload session cleanup — einmal pro Stunde.
        self.register(
            "chunked_upload_cleanup",
            lambda: {"pruned": chunked_manager.prune_stale()},
            3600,
        )

        # Orphaned-stage watchdog (T-063) — mittleres Intervall.
        self.register(
            "orphaned_stage_cleanup",
            Scheduler.fail_orphaned_stages,
            settings.orphaned_stage_interval_seconds,
        )

        # DB VACUUM — einmal pro Tag.
        self.register("db_vacuum", _db_vacuum, settings.db_vacuum_interval_seconds)

        # SSN auto-approve (T-069) — only registered when ssn_enabled and
        # ssn_auto_approve are both on. Approves pending SSN registrations
        # so the SSN can come online without a manual admin action.
        if settings.ssn_enabled and settings.ssn_auto_approve:
            self.register(
                "ssn_auto_approve",
                _ssn_auto_approve,
                settings.maintenance_interval_seconds,
            )