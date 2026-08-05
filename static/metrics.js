/* metrics.js — Built-in metrics dashboard (T-109).
 *
 * Fetches /relay/v2/dashboard/api/metrics and renders number cards plus
 * minimal horizontal bar charts for tasks/stages grouped by status.
 * No external dependencies; CSP allows script-src 'self' only.
 */

(function () {
  "use strict";

  var API_URL = "/relay/v2/dashboard/api/metrics";

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    return String(n);
  }

  function setStatus(text, ok) {
    var pill = document.getElementById("statusPill");
    if (!pill) return;
    pill.innerHTML = '<span class="dot"></span> ' + text;
    var dot = pill.querySelector(".dot");
    if (dot) {
      dot.style.background = ok === false ? "var(--bad)" : (ok === "warn" ? "var(--warn)" : "var(--ok)");
    }
  }

  function renderCards(data) {
    var host = document.getElementById("metricCards");
    if (!host) return;
    var cards = [
      { label: "Nodes total", value: data.nodes_total },
      { label: "Nodes online", value: data.nodes_online, tone: data.nodes_online > 0 ? "ok" : "" },
      { label: "Queue depth", value: data.queue_depth },
      { label: "Tasks (total)", value: sumValues(data.tasks_by_status) },
      { label: "Stages (total)", value: sumValues(data.stages_by_status) },
    ];
    host.innerHTML = cards.map(function (c) {
      return '<div class="metric-card">'
        + '<div class="label">' + c.label + '</div>'
        + '<div class="value ' + (c.tone || "") + '">' + fmt(c.value) + '</div>'
        + '</div>';
    }).join("");
  }

  function sumValues(obj) {
    if (!obj) return 0;
    return Object.keys(obj).reduce(function (acc, k) { return acc + (obj[k] || 0); }, 0);
  }

  function renderBars(hostId, groups) {
    var host = document.getElementById(hostId);
    if (!host) return;
    var keys = Object.keys(groups || {}).sort();
    if (keys.length === 0) {
      host.innerHTML = '<div class="empty-hint">No data.</div>';
      return;
    }
    var max = 1;
    keys.forEach(function (k) { if (groups[k] > max) max = groups[k]; });
    host.innerHTML = keys.map(function (k) {
      var v = groups[k];
      var pct = Math.round((v / max) * 100);
      return '<div class="bar-row">'
        + '<div class="bar-label">' + k + '</div>'
        + '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div>'
        + '<div class="bar-value">' + v + '</div>'
        + '</div>';
    }).join("");
  }

  function renderCounters(counters) {
    var host = document.getElementById("counterList");
    if (!host) return;
    var names = Object.keys(counters || {}).sort();
    if (names.length === 0) {
      host.innerHTML = '<div class="empty-hint">No counters recorded.</div>';
      return;
    }
    var lines = [];
    names.forEach(function (name) {
      var series = counters[name];
      Object.keys(series).forEach(function (labelKey) {
        var v = series[labelKey];
        var lbl = labelKey && labelKey !== "{}" ? " " + labelKey : "";
        lines.push('<div class="counter-line"><span class="ck">' + name + lbl + '</span> ' + v + '</div>');
      });
    });
    host.innerHTML = lines.join("");
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 1) return Math.round(seconds * 1000) + "ms";
    if (seconds < 60) return Math.round(seconds) + "s";
    var m = Math.floor(seconds / 60);
    var s = Math.round(seconds % 60);
    return m + "m " + s + "s";
  }

  function renderLatency(latency) {
    var host = document.getElementById("latencyCards");
    if (!host) return;
    // latency: { relay_stage_duration_seconds: {buckets, sum, count}, ... }
    var names = ["relay_stage_duration_seconds", "relay_claim_duration_seconds", "relay_task_duration_seconds"];
    var cards = [];
    names.forEach(function (name) {
      var h = latency && latency[name];
      if (!h) return;
      // Approximate p50 from buckets: find the bucket where cumulative count >= half.
      var half = h.count / 2;
      var p50 = null;
      var ordered = Object.keys(h.buckets).map(Number).sort(function (a, b) { return a - b; });
      for (var i = 0; i < ordered.length; i++) {
        if (h.buckets[String(ordered[i])] >= half) { p50 = ordered[i]; break; }
      }
      var label = name.replace("relay_", "").replace("_seconds", "").replace(/_/g, " ");
      cards.push({
        label: label,
        value: fmtDuration(p50),
        sub: h.count + " obs",
      });
    });
    if (cards.length === 0) {
      host.innerHTML = '<div class="empty-hint">No completed tasks yet.</div>';
      return;
    }
    host.innerHTML = cards.map(function (c) {
      return '<div class="metric-card"><div class="label">' + c.label + '</div>'
        + '<div class="value">' + c.value + '</div>'
        + '<div class="empty-hint">' + c.sub + '</div></div>';
    }).join("");
  }

  function renderRetry(retry) {
    var host = document.getElementById("retryBars");
    if (!host) return;
    if (!retry || !retry.total) {
      host.innerHTML = '<div class="empty-hint">No completed stages yet.</div>';
      return;
    }
    var pct = Math.round((retry.retried / retry.total) * 100);
    host.innerHTML = '<div class="bar-row">'
      + '<div class="bar-label">Retried</div>'
      + '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div>'
      + '<div class="bar-value">' + retry.retried + ' / ' + retry.total + ' (' + pct + '%)</div>'
      + '</div>';
  }

  function renderNodes(nodes) {
    var host = document.getElementById("nodeList");
    if (!host) return;
    if (!nodes || nodes.length === 0) {
      host.innerHTML = '<div class="empty-hint">No nodes registered.</div>';
      return;
    }
    var rows = nodes.map(function (n) {
      var color = n.online ? "var(--ok)" : "var(--muted)";
      return '<div class="bar-row">'
        + '<div class="bar-label" style="color:' + color + '">' + n.node_name + '</div>'
        + '<div class="bar-track"><div class="bar-fill" style="width:' + Math.min(100, n.load) + '%;background:var(--ok)"></div></div>'
        + '<div class="bar-value">' + n.load + '% · q' + n.queue_depth + '</div>'
        + '</div>';
    }).join("");
    host.innerHTML = rows;
  }

  function updateLastUpdated(ts) {
    var el = document.getElementById("lastUpdated");
    if (!el) return;
    if (!ts) { el.textContent = ""; return; }
    var d = new Date(ts * 1000);
    el.textContent = "Updated " + d.toLocaleTimeString();
  }

  function load() {
    setStatus("loading…", "warn");
    fetch(API_URL, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        renderCards(data);
        renderLatency(data.latency);
        renderRetry(data.retry);
        renderNodes(data.nodes);
        renderBars("tasksBars", data.tasks_by_status);
        renderBars("stagesBars", data.stages_by_status);
        renderCounters(data.counters);
        updateLastUpdated(data.generated_at);
        setStatus("ok", true);
      })
      .catch(function (err) {
        setStatus("error: " + err.message, false);
      });
  }

  document.addEventListener("DOMContentLoaded", load);
  var btn = document.getElementById("btnRefresh");
  if (btn) btn.addEventListener("click", load);
  // Auto-refresh every 15s.
  setInterval(load, 15000);
})();