"use strict";
// Radar Care Fleet - cloud operator console.
// Renders only reduced home summaries: activity state, derived gait/vitals,
// alerts, node health, and cloud-ingest posture. Local radar diagnostics stay
// at the in-home Supervisor boundary and are not requested by this dashboard.

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v == null || v === "" ? "-" : Number(v).toFixed(d));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const signed = (v, d = 1) => `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}`;
const metricValue = (v, meta) => `${fmt(v, meta.decimals)}${meta.unit === "%" ? "" : " "}${meta.unit}`;

const TREND_METRICS = {
  gait_speed_mps: { label: "Gait speed", unit: "m/s", decimals: 2, accent: "#0f9488", review: -0.06, watch: -0.03, improve: 0.03 },
  sleep_efficiency_pct: { label: "Sleep efficiency", unit: "%", decimals: 1, accent: "#4f86c6", pp: true, review: -4.0, watch: -2.0, improve: 2.0 },
};
const TREND_METRIC_ORDER = ["gait_speed_mps", "sleep_efficiency_pct"];
const TREND_RANGE_ORDER = ["4w", "12w"];

const TREND_BUCKETS = {
  review: { label: "review", className: "review" },
  watch: { label: "watch", className: "watch" },
  stable: { label: "stable", className: "stable" },
  improving: { label: "improving", className: "improving" },
  unknown: { label: "unknown", className: "unknown" },
};

const state = {
  cells: new Map(),
  homes: new Map(),
  selected: null,
  lastSig: new Map(),
  fallbackTimer: null,
  snapshotTimer: null,
  eventSource: null,
  fallbackTick: 0,
  trendRange: "4w",
  trendMetric: "gait_speed_mps",
  reviewExpanded: false,
};

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function queryUrlParam(name) {
  const raw = new URLSearchParams(location.search).get(name) || "";
  if (!raw) return null;
  try {
    return new URL(raw);
  } catch (_) {
    return null;
  }
}

function backendOverride() {
  return queryUrlParam("backend");
}

function apiBaseOverride() {
  return queryUrlParam("api");
}

function streamOverride() {
  return queryUrlParam("stream");
}

function snapshotOverride() {
  return queryUrlParam("snapshot");
}

function isLocalHost() {
  return ["localhost", "127.0.0.1", "::1"].includes(location.hostname);
}

function shouldPreferHostedSource() {
  if (location.protocol === "file:" || !location.host || backendOverride()) return false;
  if (apiBaseOverride() || streamOverride() || snapshotOverride()) return true;
  return !isLocalHost();
}

function hostedUrl(path) {
  const apiBase = apiBaseOverride();
  if (!apiBase) return path;
  return new URL(path, apiBase).toString();
}

function setConnection(label, ok = false) {
  const pill = $("conn-pill");
  pill.classList.toggle("ok", ok);
  pill.innerHTML = `<i class="dot"></i> ${label}`;
}

function connect() {
  if (!backendOverride() && (location.protocol === "file:" || !location.host)) {
    setConnection("local preview", true);
    startFallback();
    return;
  }
  stopHostedTransports();
  if (shouldPreferHostedSource()) {
    connectHosted();
    return;
  }
  connectWebSocket();
}

function connectWebSocket() {
  const backend = backendOverride();
  const wsProto = backend ? (backend.protocol === "https:" ? "wss" : "ws")
    : (location.protocol === "https:" ? "wss" : "ws");
  const wsHost = backend ? backend.host : location.host;
  const ws = new WebSocket(`${wsProto}://${wsHost}/ws`);
  let opened = false;
  let previewed = false;
  const useLocalPreview = () => {
    if (opened || previewed) return;
    previewed = true;
    clearTimeout(previewTimeout);
    try { ws.close(); } catch (_) {}
    setConnection("local preview", true);
    startFallback();
  };
  const previewTimeout = setTimeout(useLocalPreview, 1200);
  ws.onopen = () => {
    opened = true;
    clearTimeout(previewTimeout);
    setConnection("live cloud", true);
    stopFallback();
  };
  ws.onclose = () => {
    clearTimeout(previewTimeout);
    if (!opened) {
      useLocalPreview();
      return;
    }
    setConnection("reconnecting");
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    try {
      onSnapshot(JSON.parse(ev.data));
    } catch (e) {
      console.error(e);
    }
  };
}

function connectHosted() {
  stopFallback();
  const snapshotUrl = snapshotOverride()?.toString() || hostedUrl("/api/snapshot");
  const streamUrl = streamOverride()?.toString() || hostedUrl("/api/stream");
  if ("EventSource" in window) {
    connectEventStream(streamUrl, snapshotUrl);
    return;
  }
  startSnapshotPolling(snapshotUrl);
}

function connectEventStream(streamUrl, snapshotUrl) {
  setConnection("connecting");
  let received = false;
  const es = new EventSource(streamUrl);
  state.eventSource = es;
  const timeout = setTimeout(() => {
    if (!received) {
      try { es.close(); } catch (_) {}
      startSnapshotPolling(snapshotUrl);
    }
  }, 2500);

  const handleSnapshot = (ev) => {
    received = true;
    clearTimeout(timeout);
    try {
      onSnapshot(JSON.parse(ev.data));
      setConnection("live cloud", true);
    } catch (e) {
      console.error(e);
    }
  };

  es.addEventListener("snapshot", handleSnapshot);
  es.onmessage = handleSnapshot;
  es.onerror = () => {
    clearTimeout(timeout);
    try { es.close(); } catch (_) {}
    state.eventSource = null;
    if (!received) {
      startSnapshotPolling(snapshotUrl);
      return;
    }
    setConnection("reconnecting");
    setTimeout(connectHosted, 1800);
  };
}

function startSnapshotPolling(snapshotUrl) {
  stopHostedTransports();
  setConnection("connecting");
  let hasData = false;
  let failures = 0;
  const poll = async () => {
    try {
      const response = await fetch(snapshotUrl, { cache: "no-store", headers: { accept: "application/json" } });
      if (!response.ok) throw new Error(`snapshot HTTP ${response.status}`);
      onSnapshot(await response.json());
      hasData = true;
      failures = 0;
      setConnection("live cloud", true);
    } catch (e) {
      console.error(e);
      failures += 1;
      setConnection(hasData ? "reconnecting" : "cloud unavailable");
      if (!hasData && failures >= 3) {
        $("clock").textContent = "--:--:--";
      }
    }
  };
  poll();
  state.snapshotTimer = setInterval(poll, 3000);
}

function onSnapshot(s) {
  $("clock").textContent = s.clock ? `${s.clock} UTC` : "--:--:--";
  state.homes.clear();
  for (const home of s.homes || []) state.homes.set(home.site_id, home);
  ensureSelection(s.homes || []);
  renderKpis(s.rollup);
  renderFleet(s.homes || []);
  renderReviewList(s.homes || []);
  renderEvents(s.events || []);
  renderCloudPanel();
  renderCloudPosture(s.rollup);
}

function ensureSelection(homes) {
  if (state.selected && state.homes.has(state.selected)) return;
  // deep link: /?home=home-0005 pins the drill-down to one home
  const pinned = new URLSearchParams(location.search).get("home");
  if (pinned && state.homes.has(pinned)) {
    state.selected = pinned;
    return;
  }
  const priority = [...homes].sort((a, b) => (b.worst_severity ?? -1) - (a.worst_severity ?? -1));
  state.selected = priority[0]?.site_id || null;
}

function renderKpis(r) {
  if (!r) return;
  $("kpi-homes").textContent = r.homes_total;
  $("kpi-walking").textContent = r.by_state.walking || 0;
  $("kpi-stationary").textContent = r.by_state.stationary || 0;
  $("kpi-absent").textContent = r.by_state.absent || 0;
  const crit = r.critical_alerts || 0;
  const warn = r.warning_alerts || Math.max(0, (r.active_alerts || 0) - crit);
  $("kpi-alerts").innerHTML = `${r.active_alerts} <small>${crit} crit · ${warn} warn</small>`;
  const mix = r.gait_trend_mix || {};
  const review = r.gait_review_homes ?? mix.review ?? 0;
  $("kpi-review").innerHTML = `${review} <small>${mix.watch || 0} watch</small>`;
  $("kpi-nodes").innerHTML = `${r.nodes_online} / ${r.nodes_total}`;
  $("kpi-rate").innerHTML = `${fmt(r.msgs_per_s, 0)} <small>msg/s</small>`;
  $("kpi-latency").innerHTML = r.ingest_latency_p95_ms == null
    ? "-" : `${fmt(r.ingest_latency_p95_ms, 1)} <small>ms</small>`;
  $("fleet-count").textContent = `- ${r.homes_total} homes · ${r.nodes_total} nodes`;
}

function cellClass(h) {
  if (h.worst_severity === 2) return "c-crit";
  if (h.worst_severity === 1) return "c-warn";
  if (h.state === "walking") return "c-walk";
  if (h.state === "stationary") return "c-stat";
  return "c-absent";
}

function cellLabel(h) {
  const critical = h.critical_count || 0;
  const warning = h.warning_count || 0;
  const alert = critical ? `${critical} critical alert${critical === 1 ? "" : "s"}`
    : warning ? `${warning} warning${warning === 1 ? "" : "s"}` : "nominal";
  const review = h.gait_trend_bucket === "review" ? " · gait review" : "";
  return `${h.site_id} · ${h.state} · ${h.n_nodes_online}/${h.n_nodes} nodes reporting · ${alert}${review}`;
}

function reviewFlag(h) {
  return h.gait_trend_bucket === "review" ? " rv" : "";
}

function renderFleet(homes) {
  const grid = $("fleet-grid");
  if (homes.length !== state.cells.size) {
    const ordered = [...homes].sort((a, b) => a.site_id.localeCompare(b.site_id));
    const frag = document.createDocumentFragment();
    state.cells.clear();
    for (const h of ordered) {
      const cell = document.createElement("button");
      cell.className = "cell " + cellClass(h) + reviewFlag(h);
      cell.title = cellLabel(h);
      cell.setAttribute("aria-label", cellLabel(h));
      cell.dataset.site = h.site_id;
      cell.addEventListener("click", () => {
        state.selected = h.site_id;
        renderFleet([...state.homes.values()]);
        renderCloudPanel();
      });
      state.cells.set(h.site_id, cell);
      frag.appendChild(cell);
    }
    clear(grid);
    grid.appendChild(frag);
  }

  for (const h of homes) {
    const cell = state.cells.get(h.site_id);
    if (!cell) continue;
    const sig = cellClass(h) + reviewFlag(h);
    const nextClass = "cell " + sig + (h.site_id === state.selected ? " sel" : "");
    if (state.lastSig.get(h.site_id) !== sig || cell.className !== nextClass) {
      cell.className = nextClass;
      state.lastSig.set(h.site_id, sig);
    }
    const label = cellLabel(h);
    cell.title = label;
    cell.setAttribute("aria-label", label);
  }
}

function renderReviewList(homes) {
  const ul = $("review-list");
  clear(ul);
  const flagged = homes
    .filter((h) => h.gait_trend_bucket === "review" || h.gait_trend_bucket === "watch")
    .sort((a, b) => {
      const rank = (h) => (h.gait_trend_bucket === "review" ? 0 : 1);
      if (rank(a) !== rank(b)) return rank(a) - rank(b);
      return (a.gait?.speed_change_pct_12w ?? 0) - (b.gait?.speed_change_pct_12w ?? 0);
    });
  $("review-sub").textContent = flagged.length
    ? `${flagged.length} of ${homes.length} homes flagged on 12-week gait trend`
    : "12-week gait decline";
  if (!flagged.length) {
    ul.innerHTML = '<li class="review-empty">No homes currently flagged for gait review.</li>';
    return;
  }
  const shown = state.reviewExpanded ? flagged : flagged.slice(0, 8);
  for (const h of shown) {
    const li = document.createElement("li");
    const row = document.createElement("button");
    row.type = "button";
    row.className = "review-row" + (h.gait_trend_bucket === "watch" ? " watch" : "");
    row.dataset.site = h.site_id;
    const delta12 = gaitSeriesChangePct(h, "12w") ?? h.gait?.speed_change_pct_12w;
    const sym = h.gait?.step_time_symmetry;
    row.innerHTML = `<span class="rv-site">${h.site_id}</span>
      <span class="trend-badge ${h.gait_trend_bucket}">${h.gait_trend_bucket}</span>
      <span class="rv-delta">${delta12 == null ? "-" : signed(delta12, 1) + "% gait speed 12w"}</span>
      <span class="rv-meta">${sym == null ? "" : "symmetry " + fmt(sym * 100, 1) + "%"}</span>`;
    li.appendChild(row);
    ul.appendChild(li);
  }
  if (flagged.length > 8) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "review-toggle";
    btn.dataset.action = "toggle-review";
    btn.textContent = state.reviewExpanded
      ? "show fewer"
      : `+${flagged.length - shown.length} more flagged homes`;
    li.appendChild(btn);
    ul.appendChild(li);
  }
}

// One persistent listener: rows are re-rendered on every snapshot, so binding
// per-row handlers would race against refresh while the pointer is moving.
function setupReviewListControls() {
  $("review-list").addEventListener("click", (ev) => {
    const toggle = ev.target.closest('[data-action="toggle-review"]');
    if (toggle) {
      state.reviewExpanded = !state.reviewExpanded;
      renderReviewList([...state.homes.values()]);
      return;
    }
    const row = ev.target.closest(".review-row");
    if (row?.dataset.site && state.homes.has(row.dataset.site)) {
      state.selected = row.dataset.site;
      renderFleet([...state.homes.values()]);
      renderCloudPanel();
    }
  });
}

function renderEvents(events) {
  const ul = $("events");
  clear(ul);
  if (!events.length) {
    ul.innerHTML = '<li class="ev-empty">No alert transitions in this window.</li>';
    return;
  }
  for (const e of events.slice(0, 18)) {
    const li = document.createElement("li");
    const gait = String(e.type || "").startsWith("gait_") ? " gait" : "";
    li.className = "ev sev-" + (e.severity || "info") + gait;
    li.innerHTML = `<span class="ev-time">${e.time}</span>
      <span class="ev-type">${String(e.type || "").replaceAll("_", " ")}</span>
      <span class="ev-site">${e.site_id}${e.zone ? " · " + e.zone : ""}</span>`;
    ul.appendChild(li);
  }
}

function pillClass(home) {
  const alertClass = home.worst_severity === 2 ? " alert-critical"
    : home.worst_severity === 1 ? " alert-warning" : "";
  return "status-pill state-" + home.state + alertClass;
}

function renderCloudPanel() {
  const home = state.selected ? state.homes.get(state.selected) : null;
  if (!home) {
    $("home-title").textContent = "No home selected";
    $("home-state").textContent = "-";
    $("home-state").className = "status-pill";
    $("home-empty").hidden = false;
    $("home-body").hidden = true;
    return;
  }

  $("home-title").textContent = home.site_id;
  $("home-state").textContent = home.state;
  $("home-state").className = pillClass(home);
  $("home-empty").hidden = true;
  $("home-body").hidden = false;

  renderSummaryRows(home);
  renderDerivedRows(home);
  renderTrendRows(home);
  renderNodeRows(home.nodes || []);
  renderPacketRows(home);
}

function renderSummaryRows(home) {
  dl("summary-dl", [
    ["state", `<span class="state-text state-${home.state}">${home.state}</span>`],
    ["zone", home.zone || "none"],
    ["track confidence", fmt(home.track_confidence, 2)],
    ["node detections", `${home.n_detecting || 0} / ${home.n_nodes || 0}`],
    ["last summary", timeOnly(home.last_seen)],
  ]);
}

function gaitChangeSpan(pct) {
  if (pct == null) return "-";
  const cls = pct <= -5 ? "bad" : pct <= -2.5 ? "warn" : pct >= 2.5 ? "good" : "";
  return `<span class="${cls}">${signed(pct, 1)}%</span>`;
}

// One source of truth for gait-speed change: derive from the plotted series
// when it exists, so this row always matches the trend panel above the chart.
function gaitSeriesChangePct(home, range) {
  const series = trendSeries(home, "gait_speed_mps", range);
  if (series.length < 2) return null;
  const first = Number(series[0]);
  if (!first) return null;
  return ((Number(series[series.length - 1]) - first) / first) * 100;
}

function respChangeSpan(bpm) {
  if (bpm == null) return "-";
  const cls = bpm >= 1.5 ? "bad" : bpm >= 0.8 ? "warn" : bpm <= -0.8 ? "good" : "";
  return `<span class="${cls}">${signed(bpm, 1)} br/min</span>`;
}

function renderDerivedRows(home) {
  const rows = [];
  if (home.gait) {
    const sym = home.gait.step_time_symmetry;
    rows.push([`gait ${home.gait.window_d || 7}d`, `${fmt(home.gait.walking_speed_mps_avg, 2)} m/s · ${fmt(home.gait.cadence_spm_avg, 0)} spm`]);
    rows.push(["walking coverage", `${home.gait.walking_minutes || 0} min · ${home.gait.walking_bouts || 0} bouts`]);
    rows.push(["symmetry", `<span class="${home.gait.impaired ? "bad" : "good"}">${fmt(sym * 100, 1)}%</span>`]);
    rows.push(["gait speed 4w", gaitChangeSpan(gaitSeriesChangePct(home, "4w") ?? home.gait.speed_change_pct_4w ?? 0)]);
    const change12w = gaitSeriesChangePct(home, "12w") ?? home.gait.speed_change_pct_12w;
    if (change12w != null) rows.push(["gait speed 12w", gaitChangeSpan(change12w)]);
    rows.push(["gait confidence", fmt(home.gait.confidence, 2)]);
  }
  if (home.vitals) {
    rows.push([`vitals ${home.vitals.window_h || 24}h`, `${fmt(home.vitals.respiration_rate_bpm_avg, 1)} br/min · ${fmt(home.vitals.heart_rate_bpm_avg, 0)} bpm`]);
    if (home.vitals.respiration_change_bpm_4w != null) rows.push(["resp change 4w", respChangeSpan(home.vitals.respiration_change_bpm_4w)]);
    rows.push(["stationary coverage", `${home.vitals.stationary_minutes || 0} min`]);
    rows.push(["vitals confidence", fmt(home.vitals.confidence, 2)]);
  }
  if (home.sleep) {
    rows.push([`sleep ${home.sleep.window_d || 7}d`, `${fmt(home.sleep.sleep_efficiency_pct, 1)}% efficiency · ${fmt(home.sleep.total_sleep_h_avg, 1)} h`]);
    rows.push(["REM / deep", `${fmt(home.sleep.rem_pct, 1)}% · ${fmt(home.sleep.deep_pct, 1)}%`]);
    rows.push(["sleep confidence", fmt(home.sleep.confidence, 2)]);
  }
  dl("derived-dl", rows);
}

function trendSeries(home, metric, range) {
  return home.trends?.ranges?.[range]?.[metric] || [];
}

function metricHasTrend(home, metric) {
  return TREND_RANGE_ORDER.some((range) => trendSeries(home, metric, range).length >= 2);
}

function availableRanges(home, metric) {
  return TREND_RANGE_ORDER.filter((range) => trendSeries(home, metric, range).length >= 2);
}

function classifyTrend(metric, series) {
  const meta = TREND_METRICS[metric] || TREND_METRICS.gait_speed_mps;
  if (!series.length || series.length < 2) return { bucket: "unknown", delta: 0 };
  const first = Number(series[0]);
  const last = Number(series[series.length - 1]);
  const delta = last - first;
  if (meta.neutral) {
    return { bucket: Math.abs(delta) >= meta.watch ? "watch" : "stable", delta };
  }
  if (delta <= meta.review) return { bucket: "review", delta };
  if (delta <= meta.watch) return { bucket: "watch", delta };
  if (delta >= meta.improve) return { bucket: "improving", delta };
  return { bucket: "stable", delta };
}

function trendChangeClass(metric, bucket, delta) {
  if (bucket === "review" || bucket === "watch") return "bad";
  if (bucket === "improving") return "good";
  if (TREND_METRICS[metric]?.neutral || delta === 0) return "";
  return delta > 0 ? "good" : "";
}

function renderTrendRows(home) {
  const metrics = TREND_METRIC_ORDER.filter((metricKey) => metricHasTrend(home, metricKey));
  const metric = metrics.includes(state.trendMetric) ? state.trendMetric : metrics[0];
  if (metric && metric !== state.trendMetric) state.trendMetric = metric;

  const ranges = metric ? availableRanges(home, metric) : [];
  const range = ranges.includes(state.trendRange) ? state.trendRange
    : ranges.includes("4w") ? "4w" : ranges[0];
  if (range && range !== state.trendRange) state.trendRange = range;

  updateTrendButtons(home);

  const meta = TREND_METRICS[state.trendMetric] || TREND_METRICS.gait_speed_mps;
  const series = range ? trendSeries(home, state.trendMetric, state.trendRange) : [];
  const labels = range ? (home.trends?.ranges?.[state.trendRange]?.labels || []) : [];
  if (!series.length || series.length < 2) {
    $("trend-chart").innerHTML = "";
    $("trend-stats").innerHTML = '<div class="trend-stat"><span>status</span><b>no trend series</b></div>';
    $("trend-badge").textContent = "unknown";
    $("trend-badge").className = "trend-badge unknown";
    dl("trend-dl", [["metric", meta.label], ["range", state.trendRange], ["latest", "-"], ["change", "-"]]);
    return;
  }

  const first = Number(series[0]);
  const last = Number(series[series.length - 1]);
  const { bucket, delta } = classifyTrend(state.trendMetric, series);
  const bucketMeta = TREND_BUCKETS[bucket] || TREND_BUCKETS.unknown;
  const latest = metricValue(last, meta);
  const change = meta.pp
    ? `${signed(delta, 1)}%`
    : `${signed(delta, meta.decimals)} ${meta.unit} · ${signed(first ? (delta / first) * 100 : 0, 1)}%`;
  const changeClass = trendChangeClass(state.trendMetric, bucket, delta);
  $("trend-badge").textContent = meta.neutral && bucket === "watch" ? "shift" : bucketMeta.label;
  $("trend-badge").className = `trend-badge ${bucketMeta.className}`;
  $("trend-stats").innerHTML = `
    <div class="trend-stat"><span>baseline</span><b>${metricValue(first, meta)}</b></div>
    <div class="trend-stat"><span>latest</span><b>${latest}</b></div>
    <div class="trend-stat"><span>change</span><b class="${changeClass}">${change}</b></div>
  `;
  drawTrend(series, labels, meta);
  dl("trend-dl", [
    ["metric", meta.label],
    ["range", state.trendRange],
    ["coverage", trendCoverage(home, state.trendMetric)],
  ]);
}

function updateTrendButtons(home) {
  const metricRanges = availableRanges(home, state.trendMetric);
  document.querySelectorAll("#range-tabs button").forEach((btn) => {
    const enabled = metricRanges.includes(btn.dataset.range);
    btn.disabled = !enabled;
    btn.classList.toggle("active", btn.dataset.range === state.trendRange);
  });
  document.querySelectorAll("#metric-tabs button").forEach((btn) => {
    const enabled = metricHasTrend(home, btn.dataset.metric);
    btn.disabled = !enabled;
    btn.classList.toggle("active", btn.dataset.metric === state.trendMetric);
  });
}

function trendCoverage(home, metric) {
  if (metric === "gait_speed_mps" || metric === "step_time_symmetry_pct") {
    return home.gait ? `${home.gait.walking_minutes || 0} min walking` : "-";
  }
  if (metric === "respiration_rate_bpm") {
    return home.vitals ? `${home.vitals.stationary_minutes || 0} min stationary` : "-";
  }
  if (metric === "sleep_efficiency_pct") {
    return `${home.sleep?.window_d || 7}d of nightly staging`;
  }
  return "-";
}

function drawTrend(series, labels, meta) {
  const svg = $("trend-chart");
  const width = 460;
  const height = 180;
  const pad = { l: 44, r: 24, t: 20, b: 32 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const vals = series.map(Number);
  const rawMin = Math.min(...vals);
  const rawMax = Math.max(...vals);
  let min = rawMin;
  let max = rawMax;
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const spread = max - min;
  min -= spread * 0.16;
  max += spread * 0.16;
  const x = (i) => pad.l + (i / Math.max(vals.length - 1, 1)) * (width - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / (max - min)) * (height - pad.t - pad.b);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const areaPts = `${x(0).toFixed(1)},${(height - pad.b).toFixed(1)} ${pts} ${x(vals.length - 1).toFixed(1)},${(height - pad.b).toFixed(1)}`;
  const startLabel = labels[0] || "";
  const middleLabel = labels[Math.floor(labels.length / 2)] || "";
  const endLabel = labels[labels.length - 1] || "";
  const grid = [rawMax, (rawMax + rawMin) / 2, rawMin];
  const lastY = y(vals[vals.length - 1]);
  const lastLabelY = clamp(lastY - 8, pad.t + 8, height - pad.b - 10);
  svg.innerHTML = `
    <line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height - pad.b}" class="axis"></line>
    <line x1="${pad.l}" y1="${height - pad.b}" x2="${width - pad.r}" y2="${height - pad.b}" class="axis"></line>
    ${grid.map((v) => `<line x1="${pad.l}" y1="${y(v).toFixed(1)}" x2="${width - pad.r}" y2="${y(v).toFixed(1)}" class="gridline"></line>`).join("")}
    <line x1="${pad.l}" y1="${y(vals[0]).toFixed(1)}" x2="${width - pad.r}" y2="${y(vals[0]).toFixed(1)}" class="baseline"></line>
    <polygon points="${areaPts}" class="trend-area" style="fill:${meta.accent}"></polygon>
    <polyline points="${pts}" class="trend-line" style="stroke:${meta.accent}"></polyline>
    ${vals.map((v, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="3" class="trend-dot" style="fill:${meta.accent}"></circle>`).join("")}
    <text x="${pad.l}" y="${height - 8}" class="chart-label">${startLabel}</text>
    <text x="${(width / 2).toFixed(1)}" y="${height - 8}" class="chart-label mid">${middleLabel}</text>
    <text x="${width - pad.r}" y="${height - 8}" class="chart-label end">${endLabel}</text>
    <text x="${pad.l - 7}" y="${y(rawMax).toFixed(1)}" class="chart-label end">${metricValue(rawMax, meta)}</text>
    <text x="${pad.l - 7}" y="${y(rawMin).toFixed(1)}" class="chart-label end">${metricValue(rawMin, meta)}</text>
    <text x="${width - pad.r - 4}" y="${lastLabelY.toFixed(1)}" class="value-label end">${metricValue(vals[vals.length - 1], meta)}</text>
  `;
}

function renderNodeRows(nodes) {
  const strip = $("nodes-strip");
  clear(strip);
  for (const n of [...nodes].sort((a, b) => a.id.localeCompare(b.id))) {
    const d = document.createElement("div");
    d.className = "node-chip " + (n.status === "online" ? "" : "warn");
    d.title = `${n.id}: ${n.status}`;
    d.innerHTML = `<b>${n.id.split("-").pop()}</b>
      <span>${n.status}</span>
      <span class="nm">${fmt(n.temp_c, 0)}°C · ${fmt(n.cpu_pct, 0)}% · ${n.rssi_dbm}dBm</span>`;
    strip.appendChild(d);
  }
}

function renderPacketRows(home) {
  dl("packet-dl", [
    ["schema", "home-summary.v1"],
    ["sequence", home.seq ?? "-"],
    ["summary size", home.summary_bytes ? `${home.summary_bytes} bytes` : "-"],
    ["edge reduction", home.reduction_ratio ? `${home.reduction_ratio}:1` : "-"],
    ["contents", "summary fields"],
  ]);
}

function renderCloudPosture(r) {
  if (!r) return;
  $("cloud-rate").textContent = `${fmt(r.msgs_per_s, 0)} msg/s`;
  $("cloud-queue").textContent = `${r.queue_depth || 0} / ${r.queue_capacity || 0}`;
  $("cloud-clients").textContent = `${r.websocket_clients || 0}`;
  $("cloud-processed").textContent = `${r.summaries_processed || 0}`;
  $("cloud-duplicates").textContent = `${r.duplicates_ignored || 0}`;
  $("cloud-stale").textContent = `${r.stale_ignored || 0}`;
}

function dl(target, rows) {
  const node = $(target);
  clear(node);
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.innerHTML = v;
    node.appendChild(dt);
    node.appendChild(dd);
  }
}

function timeOnly(value) {
  if (!value) return "-";
  const m = String(value).match(/T(\d\d:\d\d:\d\d)/);
  return m ? m[1] : String(value);
}

function stopFallback() {
  if (!state.fallbackTimer) return;
  clearInterval(state.fallbackTimer);
  state.fallbackTimer = null;
}

function stopHostedTransports() {
  if (state.snapshotTimer) {
    clearInterval(state.snapshotTimer);
    state.snapshotTimer = null;
  }
  if (state.eventSource) {
    try { state.eventSource.close(); } catch (_) {}
    state.eventSource = null;
  }
}

function startFallback() {
  if (state.fallbackTimer) return;
  const homes = makeFallbackHomes(100);
  const tick = () => {
    state.fallbackTick += 1;
    updateFallbackHomes(homes, state.fallbackTick);
    onSnapshot(buildFallbackSnapshot(homes));
  };
  tick();
  state.fallbackTimer = setInterval(tick, 1200);
}

function makeFallbackHomes(count) {
  return Array.from({ length: count }, (_, i) => ({
    site_id: `home-${String(i).padStart(4, "0")}`,
    boot_id: "local-preview",
    seq: 0,
    state: i % 12 === 0 ? "walking" : i % 7 === 0 ? "absent" : "stationary",
    zone: ["lounge", "kitchen", "bedroom", "entry"][i % 4],
    track_confidence: 0.78 + ((i % 15) / 100),
    n_nodes: 5,
    n_nodes_online: 5,
    n_detecting: 4,
    nodes: Array.from({ length: 5 }, (_, n) => ({
      id: `home-${String(i).padStart(4, "0")}-n${n + 1}`,
      status: "online",
      temp_c: 42 + ((i + n) % 9),
      cpu_pct: 20 + ((i * 3 + n) % 28),
      rssi_dbm: -45 - ((i + n * 7) % 28),
    })),
    alert_count: 0,
    warning_count: 0,
    critical_count: 0,
    worst_severity: -1,
    gait: null,
    vitals: null,
    sleep: null,
    trends: null,
    summary_bytes: 2400,
    reduction_ratio: 33300,
    last_seen: new Date().toISOString(),
  }));
}

function fallbackTrendProfile(i) {
  if (i % 17 === 0) {
    return {
      bucket: "review",
      gait4w: -4.8 - (i % 3) * 0.7,
      gait12w: -8.4 - (i % 4) * 1.0,
      sleep4w: -1.8 - (i % 3) * 0.4,
      sleep12w: -3.8 - (i % 3) * 0.8,
      rr4w: 0.5,
      rr12w: 1.1,
    };
  }
  if (i % 13 === 0) {
    return {
      bucket: "improving",
      gait4w: 1.3 + (i % 4) * 0.3,
      gait12w: 3.2 + (i % 4) * 0.5,
      sleep4w: 0.8,
      sleep12w: 1.8,
      rr4w: -0.1,
      rr12w: -0.2,
    };
  }
  if (i % 11 === 0) {
    return {
      bucket: "watch",
      gait4w: -2.0 - (i % 3) * 0.4,
      gait12w: -3.6 - (i % 4) * 0.5,
      sleep4w: -0.9,
      sleep12w: -1.8,
      rr4w: 0.3,
      rr12w: 0.8,
    };
  }
  const gait4w = [-0.8, -0.4, 0.0, 0.4, 0.8][i % 5];
  const sleep4w = [-0.6, -0.2, 0.0, 0.4, 0.7][i % 5];
  return {
    bucket: "stable",
    gait4w,
    gait12w: gait4w * 1.7 + [-0.2, 0.0, 0.2][i % 3],
    sleep4w,
    sleep12w: sleep4w * 1.8,
    rr4w: [-0.2, 0.0, 0.1, 0.2][i % 4],
    rr12w: [-0.3, -0.1, 0.1, 0.3][i % 4],
  };
}

function updateFallbackHomes(homes, tick) {
  for (let i = 0; i < homes.length; i += 1) {
    const h = homes[i];
    const trend = fallbackTrendProfile(i);
    h.seq += 1;
    h.last_seen = new Date().toISOString();
    if ((tick + i) % 31 === 0) h.state = "walking";
    else if ((tick + i) % 17 === 0) h.state = "absent";
    else h.state = "stationary";
    const reviewOffset = trend.bucket === "review" ? -0.05 : trend.bucket === "improving" ? 0.03 : 0;
    const symmetry = trend.bucket === "review" ? 0.84 : trend.bucket === "watch" ? 0.89 : trend.bucket === "improving" ? 0.97 : 0.95;
    h.gait = h.state === "walking" ? {
      window_d: 7,
      walking_minutes: 110 + (i % 7) * 11,
      walking_bouts: 10 + (i % 6),
      walking_speed_mps_avg: 0.8 + (i % 5) * 0.08 + reviewOffset,
      cadence_spm_avg: 92 + (i % 12),
      stride_length_m_avg: 0.92 + (i % 5) * 0.04,
      step_time_symmetry: symmetry,
      gait_variability_index: trend.bucket === "review" ? 70 : trend.bucket === "watch" ? 78 : 92,
      speed_change_pct_4w: trend.gait4w,
      speed_change_pct_12w: trend.gait12w,
      impaired: trend.bucket === "review" || trend.bucket === "watch",
      confidence: 0.82,
    } : {
      window_d: 7,
      walking_minutes: 85 + (i % 7) * 9,
      walking_bouts: 8 + (i % 5),
      walking_speed_mps_avg: 0.74 + (i % 6) * 0.07 + reviewOffset,
      cadence_spm_avg: 88 + (i % 14),
      stride_length_m_avg: 0.86 + (i % 5) * 0.04,
      step_time_symmetry: symmetry,
      gait_variability_index: trend.bucket === "review" ? 72 : trend.bucket === "watch" ? 79 : 91,
      speed_change_pct_4w: trend.gait4w,
      speed_change_pct_12w: trend.gait12w,
      impaired: trend.bucket === "review" || trend.bucket === "watch",
      confidence: 0.86,
    };
    h.vitals = {
      window_h: 24,
      stationary_minutes: 620 + (i % 9) * 24,
      respiration_rate_bpm_avg: 14 + (i % 6) * 0.4,
      heart_rate_bpm_avg: 62 + (i % 18),
      respiration_change_bpm_4w: trend.rr4w,
      respiration_change_bpm_12w: trend.rr12w,
      confidence: 0.86,
    };
    h.sleep = {
      window_d: 7,
      epoch_s: 30,
      context_epochs: 32,
      latest_stage: h.zone === "bedroom" ? "light" : "wake",
      sleep_efficiency_pct: 76 + (i % 12),
      sleep_efficiency_change_pp_4w: trend.sleep4w,
      sleep_efficiency_change_pp_12w: trend.sleep12w,
      total_sleep_h_avg: 6.1 + (i % 10) * 0.12,
      waso_min_avg: 24 + (i % 20),
      rem_pct: 17 + (i % 5),
      deep_pct: 10 + (i % 4),
      light_pct: 55,
      wake_pct: 18,
      confidence: 0.79,
    };
    h.trends = buildFallbackTrends(h);
    h.gait_trend_bucket = classifyTrend("gait_speed_mps", trendSeries(h, "gait_speed_mps", "12w")).bucket;
    const warning = (tick + i) % 47 === 0;
    h.nodes[2].status = warning ? "silent" : "online";
    h.n_nodes_online = warning ? 4 : 5;
    h.alert_count = warning ? 1 : 0;
    h.warning_count = warning ? 1 : 0;
    h.critical_count = 0;
    h.worst_severity = warning ? 1 : -1;
  }
}

function rangeSeries(current, change, count, decimals, wiggle = 0) {
  const start = current - change;
  return Array.from({ length: count }, (_, idx) => {
    const frac = idx / Math.max(count - 1, 1);
    const v = start + (current - start) * frac + Math.sin(idx * 1.4) * wiggle;
    return Number(v.toFixed(decimals));
  });
}

function buildFallbackTrends(h) {
  const speed = h.gait.walking_speed_mps_avg;
  const sym = h.gait.step_time_symmetry * 100;
  const sleep = h.sleep.sleep_efficiency_pct;
  const rr = h.vitals.respiration_rate_bpm_avg;
  const specs = {
    "7d": { labels: ["-6d", "-5d", "-4d", "-3d", "-2d", "-1d", "now"], scale: 0.25 },
    "4w": { labels: ["-3w", "-2w", "-1w", "now"], scale: 1.0 },
    "12w": { labels: ["-11w", "-10w", "-9w", "-8w", "-7w", "-6w", "-5w", "-4w", "-3w", "-2w", "-1w", "now"], scale: 3.0 },
  };
  const ranges = {};
  for (const [key, spec] of Object.entries(specs)) {
    const n = spec.labels.length;
    const gaitPct = key === "12w" ? h.gait.speed_change_pct_12w : (h.gait.speed_change_pct_4w || 0) * spec.scale;
    const sleepChange = key === "12w" ? h.sleep.sleep_efficiency_change_pp_12w : (h.sleep.sleep_efficiency_change_pp_4w || 0) * spec.scale;
    const rrChange = key === "12w" ? h.vitals.respiration_change_bpm_12w : (h.vitals.respiration_change_bpm_4w || 0) * spec.scale;
    ranges[key] = {
      labels: spec.labels,
      gait_speed_mps: rangeSeries(speed, speed * gaitPct / 100, n, 2, 0.01),
      step_time_symmetry_pct: rangeSeries(sym, gaitPct * 0.32, n, 1, 0.3),
      sleep_efficiency_pct: rangeSeries(sleep, sleepChange, n, 1, 0.4),
      respiration_rate_bpm: rangeSeries(rr, rrChange, n, 1, 0.2),
    };
  }
  return { ranges };
}

function buildFallbackSnapshot(homes) {
  const by_state = { walking: 0, stationary: 0, absent: 0 };
  const gaitTrendMix = { review: 0, watch: 0, stable: 0, improving: 0, unknown: 0 };
  let nodesOnline = 0;
  let nodesTotal = 0;
  let alerts = 0;
  let warnings = 0;
  const events = [];
  for (const h of homes) {
    by_state[h.state] += 1;
    const bucket = TREND_BUCKETS[h.gait_trend_bucket] ? h.gait_trend_bucket : "unknown";
    gaitTrendMix[bucket] += 1;
    nodesOnline += h.n_nodes_online;
    nodesTotal += h.n_nodes;
    alerts += h.alert_count;
    warnings += h.warning_count;
    if (h.alert_count) {
      events.push({ time: new Date().toISOString().slice(11, 19), site_id: h.site_id, type: "device_silent", severity: "warning" });
    }
  }
  return {
    type: "snapshot",
    clock: new Date().toISOString().slice(11, 19),
    homes,
    events,
    rollup: {
      homes_total: homes.length,
      by_state,
      nodes_online: nodesOnline,
      nodes_total: nodesTotal,
      active_alerts: alerts,
      warning_alerts: warnings,
      critical_alerts: 0,
      gait_trend_mix: gaitTrendMix,
      gait_review_homes: gaitTrendMix.review,
      msgs_per_s: 10,
      ingest_latency_p95_ms: 0.1,
      queue_depth: 0,
      queue_capacity: 8192,
      websocket_clients: 0,
      summaries_processed: state.fallbackTick * homes.length,
      duplicates_ignored: 0,
      stale_ignored: 0,
    },
  };
}

function setupTrendControls() {
  document.querySelectorAll("#range-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      state.trendRange = btn.dataset.range || state.trendRange;
      renderCloudPanel();
    });
  });
  document.querySelectorAll("#metric-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      state.trendMetric = btn.dataset.metric || state.trendMetric;
      renderCloudPanel();
    });
  });
}

setupTrendControls();
setupReviewListControls();
connect();
