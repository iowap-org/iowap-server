"""Observability — Metrik-Sammlung + Prometheus-Format.

Bündelt In-Process-Counter (Auth-Failures, Fehler) und DB-abgeleitete
Gauges (Tasks/Stages/Nodes). Kein externer Metrics-Server nötig; der
Relay exponiert /metrics selbst und das eingebaute Dashboard rendert die
Zahlen. Prometheus-Format wird hier erzeugt, damit ein späterer
Prometheus-Server die Werte ohne Code-Änderung scrapen kann.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from relay_server.core.db import get_conn

# In-Process-Counter: name -> {(label_dict) -> count}
_counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = defaultdict(dict)

# Für /ready: Zeitstempel des letzten erfolgreichen Heartbeat-Loops.
_last_maintenance_run: float = 0.0
_last_maintenance_ok: bool = True


def reset() -> None:
    """Leere alle In-Process-Counter (Test-Helfer + Server-Neustart)."""
    _counters.clear()


def inc(name: str, labels: dict[str, str] | None = None) -> None:
    """Erhöhe einen Zähler um 1. ``labels`` sind optionale Key/Value-Tags."""
    key = tuple(sorted((labels or {}).items()))
    _counters[name][key] = _counters[name].get(key, 0) + 1


def get_counter(name: str, labels: dict[str, str] | None = None) -> int:
    """Lies einen Zählerstand (0 wenn nie inkrementiert)."""
    key = tuple(sorted((labels or {}).items()))
    return _counters[name].get(key, 0)


def mark_maintenance_run(ok: bool) -> None:
    """Merke den letzten Maintenance-Sweep (für /ready)."""
    global _last_maintenance_run, _last_maintenance_ok
    _last_maintenance_run = time.monotonic()
    _last_maintenance_ok = ok


def maintenance_age_seconds() -> float | None:
    """Sekunden seit letztem Maintenance-Loop (None wenn nie gelaufen)."""
    if _last_maintenance_run == 0.0:
        return None
    return time.monotonic() - _last_maintenance_run


def _count_groups(query: str, col: str) -> dict[str, int]:
    conn = get_conn()
    try:
        rows = conn.execute(query).fetchall()
        return {r[col]: r["cnt"] for r in rows}
    finally:
        conn.close()


def _labels_to_key(labels: tuple[tuple[str, str], ...]) -> str:
    """Serialisiere ein Label-Tuple in einen stabilen JSON-freundlichen String.

    Prometheus nutzt ``{endpoint="/auth/login"}``; für die JSON-API und das
    Dashboard derselbe String als Dict-Key (JSON kann keine Tuple-Keys
    serialisieren).
    """
    if not labels:
        return "{}"
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"


def collect_metrics() -> dict[str, Any]:
    """Sammle alle Metriken — Counter + DB-Gauges. Reiner Read, kein Write."""
    return {
        "generated_at": time.time(),
        "counters": {name: {_labels_to_key(labels): c
                           for labels, c in series.items()}
                     for name, series in _counters.items()},
        "tasks_by_status": _count_groups(
            "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status", "status"),
        "stages_by_status": _count_groups(
            "SELECT status, COUNT(*) AS cnt FROM task_stages GROUP BY status", "status"),
        "nodes_total": _count_groups(
            "SELECT 'nodes' AS status, COUNT(*) AS cnt FROM nodes", "status").get("nodes", 0),
        "nodes_online": _count_groups(
            "SELECT CASE WHEN status='online' THEN 'online' ELSE 'other' END AS status, "
            "COUNT(*) AS cnt FROM nodes GROUP BY status", "status").get("online", 0),
        "queue_depth": _count_groups(
            "SELECT 'q' AS status, COALESCE(SUM(queue_depth),0) AS cnt FROM nodes",
            "status").get("q", 0),
    }


def _render_counter_series(name: str, series: dict) -> list[str]:
    """Rendere eine Counter-Serie (Series-Keys sind Label-Tuples).

    ``series`` ist das rohe In-Process-Format
    ``{label_tuple: count}`` aus :data:`_counters`. Das Prometheus-Format
    nutzt ``{endpoint="/x"}`` als inline-Label-Syntax.
    """
    lines = [f"# HELP {name} {name}", f"# TYPE {name} counter"]
    for labels, value in series.items():
        if labels:
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{lbl}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return lines


def render_prometheus() -> str:
    """Rendere alle Metriken im Prometheus-Expositionstextformat."""
    data = collect_metrics()
    out: list[str] = []
    # Counter direkt aus dem In-Process-Store rendern (Label-Tuples
    # bleiben erhalten), nicht aus der JSON-freundlichen ``collect_metrics``
    # Repräsentation.
    for name, series in _counters.items():
        out.extend(_render_counter_series(name, series))
    for label, count in sorted(data["tasks_by_status"].items()):
        out.append(f'relay_tasks{{status="{label}"}} {count}')
    for label, count in sorted(data["stages_by_status"].items()):
        out.append(f'relay_stages{{status="{label}"}} {count}')
    out.append(f"relay_nodes_total {data['nodes_total']}")
    out.append(f"relay_nodes_online {data['nodes_online']}")
    out.append(f"relay_queue_depth {data['queue_depth']}")
    return "\n".join(out) + "\n"