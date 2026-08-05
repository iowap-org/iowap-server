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
from datetime import datetime, timezone
from typing import Any

from relay_server.core.db import get_conn, q

# In-Process-Counter: name -> {(label_dict) -> count}
_counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = defaultdict(dict)

# Für /ready: Zeitstempel des letzten erfolgreichen Heartbeat-Loops.
_last_maintenance_run: float = 0.0
_last_maintenance_ok: bool = True

# Histogramm-Buckets (Sekunden) für Latenz-Metriken (T-115).
_LATENCY_BUCKETS = [0.5, 1.0, 5.0, 30.0, 120.0, 600.0, 3600.0]

# Latenz-Fenster für Throughput-Metriken (T-115): letzte N Sekunden.
_RATE_WINDOW_SECONDS = 300.0


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
        rows = conn.execute(q(query)).fetchall()
        return {r[col]: r["cnt"] for r in rows}
    finally:
        conn.close()


def _iso_to_seconds(ts: str) -> float | None:
    """Parse an ISO-8601 timestamp string into epoch seconds, or None.

    Timestamps in the DB are stored as ISO-8601 TEXT strings. Some are naive
    (no tz) and some carry a trailing ``Z``. ``datetime.fromisoformat`` in
    Python 3.11+ handles ``Z`` and ``+00:00``; naive ones are assumed UTC.
    Returns None if the string is missing or unparseable (caller skips it).
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _build_histogram(values: list[float], buckets: list[float]) -> dict:
    """Build a Prometheus-histogram dict from a list of observation seconds.

    Returns ``{"buckets": {le_str: cum_count, ...}, "sum": float, "count": n}``.
    Each observation increments every bucket whose upper bound ``>=`` its
    value, so bucket counts are already cumulative. ``+Inf`` is implied by
    ``count``. Bucket keys are the ``le`` threshold as strings.
    """
    hist = {"buckets": {str(b): 0 for b in buckets}, "sum": 0.0, "count": 0}
    for v in values:
        hist["sum"] += v
        hist["count"] += 1
        for b in buckets:
            if v <= b:
                hist["buckets"][str(b)] += 1
    return hist


def _compute_latency_histograms() -> dict[str, Any]:
    """Compute stage/claim/task latency histograms from terminal DB rows.

    Only completed stages/tasks count. Durations are computed in Python
    (ISO timestamps) and bucketed by ``_LATENCY_BUCKETS``.
    """
    conn = get_conn()
    try:
        stage_rows = conn.execute(q(
            "SELECT created_at, claimed_at, completed_at, retry_count "
            "FROM task_stages WHERE status = 'completed'"
        )).fetchall()
        task_rows = conn.execute(q(
            "SELECT created_at, completed_at FROM tasks WHERE status = 'completed'"
        )).fetchall()
    finally:
        conn.close()

    stage_durs: list[float] = []
    claim_durs: list[float] = []
    retried = 0
    for r in stage_rows:
        created = _iso_to_seconds(r["created_at"])
        completed = _iso_to_seconds(r["completed_at"])
        claimed = _iso_to_seconds(r["claimed_at"])
        if created is not None and completed is not None:
            stage_durs.append(max(0.0, completed - created))
        if claimed is not None and completed is not None:
            claim_durs.append(max(0.0, completed - claimed))
        if (r["retry_count"] or 0) > 0:
            retried += 1

    task_durs: list[float] = []
    for r in task_rows:
        created = _iso_to_seconds(r["created_at"])
        completed = _iso_to_seconds(r["completed_at"])
        if created is not None and completed is not None:
            task_durs.append(max(0.0, completed - created))

    return {
        "relay_stage_duration_seconds": _build_histogram(stage_durs, _LATENCY_BUCKETS),
        "relay_claim_duration_seconds": _build_histogram(claim_durs, _LATENCY_BUCKETS),
        "relay_task_duration_seconds": _build_histogram(task_durs, _LATENCY_BUCKETS),
        "_stages_total": len(stage_rows),
        "_stages_retried": retried,
    }


def _compute_node_gauges() -> list[dict]:
    """Per-node load/queue_depth/online gauges."""
    conn = get_conn()
    try:
        rows = conn.execute(q(
            "SELECT node_id, node_name, load, queue_depth, status "
            "FROM nodes ORDER BY node_name"
        )).fetchall()
    finally:
        conn.close()
    return [
        {
            "node_id": r["node_id"],
            "node_name": r["node_name"],
            "load": float(r["load"] or 0.0),
            "queue_depth": int(r["queue_depth"] or 0),
            "online": 1 if r["status"] == "online" else 0,
        }
        for r in rows
    ]


def _compute_task_rate() -> dict[str, int]:
    """Tasks created/completed within the last rate window."""
    conn = get_conn()
    now = time.time()
    window_start = datetime.fromtimestamp(now - _RATE_WINDOW_SECONDS, tz=timezone.utc).isoformat()
    try:
        created = conn.execute(q(
            "SELECT COUNT(*) AS c FROM tasks WHERE created_at >= ?",
            (window_start,),
        )).fetchone()
        completed = conn.execute(q(
            "SELECT COUNT(*) AS c FROM tasks WHERE completed_at >= ?",
            (window_start,),
        )).fetchone()
    finally:
        conn.close()
    return {
        "created": int(created["c"] or 0) if created else 0,
        "completed": int(completed["c"] or 0) if completed else 0,
    }


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
    """Sammle alle Metriken — Counter + DB-Gauges + Latenz + Node-Last. Reiner Read, kein Write."""
    latency = _compute_latency_histograms()
    node_gauges = _compute_node_gauges()
    task_rate = _compute_task_rate()
    total_stages = latency.pop("_stages_total", 0)
    retried_stages = latency.pop("_stages_retried", 0)
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
        # T-115: Latenz-Histogramme, Retry-Rate, Node-Last, Throughput
        "latency": latency,
        "retry": {
            "retried": retried_stages,
            "total": total_stages,
            "ratio": round(retried_stages / total_stages, 4) if total_stages else 0.0,
        },
        "nodes": node_gauges,
        "task_rate": task_rate,
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

    # T-115: Retry-Rate
    retry = data["retry"]
    out.append(f"relay_stages_retried_total {retry['retried']}")
    out.append(f"relay_stages_total {retry['total']}")
    out.append(f"relay_stages_retry_ratio {retry['ratio']}")

    # T-115: Latenz-Histogramme (_bucket/_sum/_count)
    for hname, hist in data["latency"].items():
        out.append(f"# HELP {hname} latency histogram")
        out.append(f"# TYPE {hname} histogram")
        for le, cum in hist["buckets"].items():
            out.append(f'{hname}_bucket{{le="{le}"}} {cum}')
        out.append(f"{hname}_sum {hist['sum']}")
        out.append(f"{hname}_count {hist['count']}")

    # T-115: Node-Gauges pro Node
    for n in data["nodes"]:
        nl = f'node_id="{n["node_id"]}",node_name="{n["node_name"]}"'
        out.append(f"relay_node_load{{{nl}}} {n['load']}")
        out.append(f"relay_node_queue_depth{{{nl}}} {n['queue_depth']}")
        out.append(f"relay_node_online{{{nl}}} {n['online']}")

    # T-115: Task-Rate (letzte 5 Min)
    out.append(f"relay_tasks_created_5m {data['task_rate']['created']}")
    out.append(f"relay_tasks_completed_5m {data['task_rate']['completed']}")

    return "\n".join(out) + "\n"