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