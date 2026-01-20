// Alertify LGU Dashboard (Live)
// - pulls disaster posts from /api/posts?only_disaster=1
// - listens to /api/stream (SSE) for real-time updates (new + updates)
// - renders the notifier list + map markers
// - citizen feed never sees urgency/confidence (backend enforces this)

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

async function getJSON(url, opts = {}) {
  const res = await fetch(url, {
    ...opts,
    headers: { Accept: "application/json", ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function relativeTime(iso) {
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const sec = Math.max(0, Math.floor(diff / 1000));
    if (sec < 10) return "Just now";
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} mins ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr} hrs ago`;
    const day = Math.floor(hr / 24);
    return `${day} days ago`;
  } catch {
    return "";
  }
}

function pillClass(urg) {
  const u = String(urg || "").toUpperCase();
  if (u === "CRITICAL") return "pill-critical";
  if (u === "HIGH") return "pill-high";
  return "pill-moderate";
}

function alertTitleFor(urg) {
  const u = String(urg || "").toUpperCase();
  if (u === "CRITICAL") return "AMBER ALERT";
  if (u === "HIGH") return "HIGH PRIORITY";
  return "MONITORING";
}

function normalizeTypeKey(disasterType) {
  const t = String(disasterType || "").toLowerCase();
  if (t.includes("flood") || t.includes("baha")) return "FLOOD";
  if (t.includes("fire") || t.includes("sunog")) return "FIRE";
  if (t.includes("landslide") || t.includes("guho")) return "LANDSLIDE";
  if (t.includes("power") || t.includes("brownout") || t.includes("kuryente")) return "POWER";
  if (t.includes("typhoon") || t.includes("storm") || t.includes("bagyo") || t.includes("wind")) return "WIND";
  if (t.includes("earthquake") || t.includes("lindol")) return "QUAKE";
  if (t.includes("volcano") || t.includes("taal") || t.includes("ash")) return "VOLCANO";
  if (t.includes("medical") || t.includes("injur") || t.includes("ambul")) return "MEDICAL";
  return "OTHER";
}

function formatLocation(p) {
  const loc = (p.location_text || "").trim();
  if (!loc) return "Lipa City (unspecified)";
  const low = loc.toLowerCase();
  if (low.includes("lipa city")) return esc(loc);
  if (low.startsWith("brgy") || low.startsWith("barangay")) return `${esc(loc)}, Lipa City`;
  return `Brgy. ${esc(loc)}, Lipa City`;
}

function statusBadge(status) {
  const s = String(status || "NEW").toUpperCase();
  if (s === "VALIDATED") return "VALIDATED";
  if (s === "ACK") return "ACK";
  if (s === "RESOLVED") return "RESOLVED";
  return "NEW";
}

function cardHTML(p) {
  const loc = formatLocation(p);
  const time = relativeTime(p.created_at);
  const urg = String(p.urgency || "MODERATE").toUpperCase();
  const dtype = String(p.disaster_type || "Unknown");
  const conf = Number.isFinite(Number(p.confidence)) ? `${p.confidence}%` : "--%";
  const st = statusBadge(p.status);

  return `
  <article class="alert-card" data-alert-id="AL-${esc(p.id)}">
    <div class="alert-inner">
      <div class="alert-header">
        <div>
          <div class="alert-title">${esc(alertTitleFor(urg))}</div>
          <div class="alert-type">${esc(dtype)} • ${esc(conf)} • ${esc(st)}</div>
        </div>
        <div class="alert-pill ${pillClass(urg)}">${esc(urg)}</div>
      </div>
      <div class="alert-body">${esc(p.content || "")}</div>
      <div class="alert-meta">
        <span><span class="meta-label">Location</span> ${loc}</span>
        <span><span class="meta-label">Time</span> ${esc(time)}</span>
      </div>
      <div class="alert-footer">
        <span class="hotline">Alertify NLP Engine • Auto-classified</span>
        <button class="btn-ghost" type="button" data-action="ack">OK</button>
      </div>
    </div>
  </article>`;
}

function computeKPIs(posts) {
  const total = posts.length;
  const critical = posts.filter((p) => String(p.urgency || "").toUpperCase() === "CRITICAL").length;
  const high = posts.filter((p) => String(p.urgency || "").toUpperCase() === "HIGH").length;
  const monitor = Math.max(0, total - critical - high);
  const barangays = new Set(posts.map((p) => String(p.location_text || "").toLowerCase()).filter(Boolean));
  const estAffected = (critical * 35) + (high * 18) + (monitor * 6);
  return { total, critical, high, monitor, barangayCount: barangays.size, estAffected };
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(value);
}

// --- Toasts (for... live demo feel) ---
let _toastHost = null;
function ensureToastHost() {
  if (_toastHost) return _toastHost;
  _toastHost = document.createElement("div");
  _toastHost.id = "toast-host";
  _toastHost.style.position = "fixed";
  _toastHost.style.right = "14px";
  _toastHost.style.bottom = "14px";
  _toastHost.style.zIndex = "9999";
  _toastHost.style.display = "flex";
  _toastHost.style.flexDirection = "column";
  _toastHost.style.gap = "8px";
  document.body.appendChild(_toastHost);
  return _toastHost;
}

function toast(msg, variant = "info") {
  const host = ensureToastHost();
  const el = document.createElement("div");
  el.style.fontFamily = "Inter, system-ui";
  el.style.fontSize = "12px";
  el.style.padding = "10px 12px";
  el.style.borderRadius = "12px";
  el.style.border = "1px solid rgba(148,163,184,0.6)";
  el.style.background = "rgba(0,0,0,0.78)";
  el.style.color = "#f9fafb";
  el.style.maxWidth = "320px";
  el.style.boxShadow = "0 10px 30px rgba(0,0,0,0.6)";
  if (variant === "critical") {
    el.style.borderColor = "rgba(255,23,68,0.9)";
  } else if (variant === "high") {
    el.style.borderColor = "rgba(255,176,32,0.9)";
  }
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    el.style.transition = "all 200ms ease";
    setTimeout(() => el.remove(), 250);
  }, 3500);
}

// Soft "beep" without bundling a file
let _soundEnabled = true;
function beep() {
  if (!_soundEnabled) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "square";
    o.frequency.value = 880;
    g.gain.value = 0.03;
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    setTimeout(() => {
      o.stop();
      ctx.close();
    }, 120);
  } catch {
    // ignore
  }
}

// --- Ticker ---
let _tickerTimer = null;
let _tickerMessages = [];

function startTicker(dynamicMessages) {
  const el = document.getElementById("ticker-text");
  if (!el) return;
  _tickerMessages = Array.isArray(dynamicMessages) ? dynamicMessages : [];
  let i = 0;

  function tick() {
    if (!_tickerMessages.length) {
      el.textContent = "Waiting for live reports from community feed…";
      return;
    }
    el.textContent = _tickerMessages[i % _tickerMessages.length];
    i++;
  }

  tick();
  if (_tickerTimer) clearInterval(_tickerTimer);
  _tickerTimer = setInterval(tick, 3500);
}

// --- Mini Chart (visual-only) ---
function renderMiniChart(posts) {
  const el = document.getElementById("mini-chart-bars");
  if (!el) return;

  const bars = new Array(8).fill(0);
  const now = Date.now();
  posts.forEach((p) => {
    const t = new Date(p.created_at).getTime();
    if (!Number.isFinite(t)) return;
    const ageH = (now - t) / (1000 * 60 * 60);
    if (ageH < 0 || ageH > 24) return;
    const idx = Math.min(7, Math.max(0, Math.floor((24 - ageH) / 3)));
    bars[idx] += 1;
  });

  const max = Math.max(1, ...bars);
  el.innerHTML = bars
    .map((v) => {
      const h = 14 + Math.round((v / max) * 46);
      return `<div class="mini-bar" style="height:${h}px" title="${v} incidents"></div>`;
    })
    .join("");
}

// --- Map ---
let _map = null;
let _markerLayer = null;
let _markerById = new Map();

function initMap() {
  const mapContainer = document.getElementById("map");
  if (!mapContainer || typeof L === "undefined") return;
  if (_map) return;

  const LIPA_CENTER = [13.941, 121.163];
  _map = L.map("map").setView(LIPA_CENTER, 13);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(_map);

  // If markercluster plugin is loaded, use it.
  if (L.markerClusterGroup) {
    _markerLayer = L.markerClusterGroup({
      spiderfyDistanceMultiplier: 1.6,
      showCoverageOnHover: false,
      maxClusterRadius: 46,
    });
    _map.addLayer(_markerLayer);
  } else {
    _markerLayer = L.layerGroup().addTo(_map);
  }
}

function markerColor(urgency) {
  const u = String(urgency || "").toUpperCase();
  if (u === "CRITICAL") return "#ff1744";
  if (u === "HIGH") return "#ffb020";
  return "#37d299";
}

function updateMapMarkers(posts) {
  if (!_map || !_markerLayer) return;
  _markerLayer.clearLayers();
  _markerById = new Map();

  posts.forEach((p) => {
    if (p.lat == null || p.lon == null) return;
    const color = markerColor(p.urgency);
    const popup = `
      <div style="font-family: Inter, system-ui; font-size:13px; max-width:240px;">
        <strong>${esc(p.disaster_type)}</strong><br/>
        <span style="font-size:11px;">Urgency: ${esc(String(p.urgency).toUpperCase())} • Confidence: ${esc(p.confidence)}%</span><br/>
        <span style="font-size:11px;">${esc(p.location_text || "Lipa City")}</span><br/>
        <span style="font-size:11px; opacity:0.85;">${esc(p.content || "")}</span>
      </div>`;

    const m = L.circleMarker([p.lat, p.lon], {
      radius: 9,
      color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.55,
    });
    m.bindPopup(popup);
    _markerLayer.addLayer(m);
    _markerById.set(String(p.id), m);
  });
}

// --- Filters ---
let _urgencyFilter = "ALL";
let _typeFilter = "ALL";

function wireFilterPills() {
  document.querySelectorAll(".filter-pill[data-urgency-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-pill[data-urgency-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      _urgencyFilter = String(btn.getAttribute("data-urgency-filter") || "ALL").toUpperCase();
      window.__ALERTIFY_RENDER && window.__ALERTIFY_RENDER();
    });
  });

  document.querySelectorAll(".filter-pill[data-type-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-pill[data-type-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      _typeFilter = String(btn.getAttribute("data-type-filter") || "ALL").toUpperCase();
      window.__ALERTIFY_RENDER && window.__ALERTIFY_RENDER();
    });
  });
}

function applyFilters(posts) {
  const searchEl = document.getElementById("alert-search");
  const q = (searchEl?.value || "").toLowerCase().trim();

  return posts.filter((p) => {
    const urg = String(p.urgency || "").toUpperCase();
    const typeKey = normalizeTypeKey(p.disaster_type);

    if (_urgencyFilter !== "ALL" && urg !== _urgencyFilter) return false;
    if (_typeFilter !== "ALL" && typeKey !== _typeFilter) return false;

    if (!q) return true;
    const blob = `${p.author || ""} ${p.content || ""} ${p.disaster_type || ""} ${p.location_text || ""} ${p.urgency || ""} ${p.category || ""}`.toLowerCase();
    return blob.includes(q);
  });
}

// --- Incident detail panel ---
function setIncidentDetail(p) {
  if (!p) return;
  setText("detail-code", `[AL-${p.id}]`);
  setText("detail-urgency", `${String(p.urgency || "MODERATE").toUpperCase()} • ${p.disaster_type || "Unknown"}`);
  setText("detail-location", formatLocation(p));
  setText("detail-category", p.category || "Situational Report");
  setText("detail-source", p.source || "Community Feed");
  setText("detail-confidence", `${p.confidence || "--"}%`);
}

async function ackPost(id) {
  try {
    await getJSON(`/api/posts/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "ACK" }),
      }
    );
  } catch {
    // ignore
  }
}

function wireAlertCardClicks(view, allPosts) {
  const cards = document.querySelectorAll(".alert-card[data-alert-id]");
  cards.forEach((card) => {
    const okBtn = card.querySelector("button[data-action='ack']");
    if (okBtn) {
      okBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        okBtn.classList.add("acknowledged");
        okBtn.textContent = "ACK";
        const id = String(card.getAttribute("data-alert-id") || "").replace(/^AL-/, "");
        await ackPost(id);
      });
    }

    card.addEventListener("click", () => {
      cards.forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");

      const id = String(card.getAttribute("data-alert-id") || "").replace(/^AL-/, "");
      const p = allPosts.find((x) => String(x.id) === id);
      if (p) {
        setIncidentDetail(p);
        const m = _markerById.get(String(p.id));
        if (m && _map) {
          // circleMarker has getLatLng
          const ll = m.getLatLng ? m.getLatLng() : null;
          if (ll) {
            _map.setView(ll, 14, { animate: true });
            m.openPopup && m.openPopup();
          }
        }
      }
    });
  });
}

// --- Alert mode selector (visual only) ---
function wireModeSelect() {
  const modeEl = document.getElementById("alert-mode-select");
  if (!modeEl) return;
  modeEl.addEventListener("change", () => {
    const v = String(modeEl.value || "NORMAL").toUpperCase();
    document.body.classList.remove("mode-normal", "mode-heightened", "mode-red");
    if (v === "RED_ALERT") document.body.classList.add("mode-red");
    else if (v === "HEIGHTENED") document.body.classList.add("mode-heightened");
    else document.body.classList.add("mode-normal");
  });
}

async function refreshPosts() {
  const data = await getJSON("/api/posts?only_disaster=1&limit=300");
  return data.posts || [];
}

document.addEventListener("DOMContentLoaded", async () => {
  // Ensure admin
  try {
    const me = await getJSON("/api/me");
    if (me.role !== "admin") {
      window.location.href = "/login.html";
      return;
    }
  } catch {
    window.location.href = "/login.html";
    return;
  }

  initMap();
  wireModeSelect();
  wireFilterPills();

  const scrollEl = document.getElementById("alerts-scroll");
  const searchEl = document.getElementById("alert-search");
  const btnAckAll = document.getElementById("btn-ack-all");

  let allPosts = await refreshPosts();

  async function acknowledgeAllVisible() {
    const view = applyFilters(allPosts);
    await Promise.all(view.slice(0, 50).map((p) => ackPost(p.id)));
    toast("Acknowledged visible incidents", "info");
  }

  btnAckAll?.addEventListener("click", acknowledgeAllVisible);

  function render() {
    const view = applyFilters(allPosts);

    // Render notifier list
    if (scrollEl) {
      if (!view.length) {
        scrollEl.innerHTML = `
          <div style="padding:14px; opacity:.85; color:#9ca3af; font-family:Inter,system-ui;">
            No incidents match current filters. Try selecting <strong style="color:#ff4b6e;">All</strong>.
          </div>`;
      } else {
        scrollEl.innerHTML = view.map(cardHTML).join("");
      }
    }

    // KPIs
    const k = computeKPIs(allPosts);
    setText("kpi-total-alerts", k.total);
    setText("kpi-critical-count", k.critical);
    setText("kpi-high-count", k.high);
    setText("kpi-monitor-count", k.monitor);
    setText("kpi-barangay-count", k.barangayCount);
    setText("kpi-est-affected", k.estAffected);
    setText("alert-count-badge", `${k.total} ACTIVE ALERTS`);

    // Model "last refresh"
    const now = new Date();
    const mm = String(now.getMonth() + 1).padStart(2, "0");
    const dd = String(now.getDate()).padStart(2, "0");
    setText("kpi-model-updated", `Last model refresh: ${now.getFullYear()}-${mm}-${dd} ${now.toLocaleTimeString()}`);

    // Ticker
    const ticker = allPosts.slice(0, 12).map((p) => {
      const loc = p.location_text ? p.location_text : "Lipa City";
      return `[AL-${p.id}] ${loc} • ${p.disaster_type} • ${String(p.urgency).toUpperCase()} • ${p.confidence}%`;
    });
    startTicker(ticker);

    // Map markers follow the *current* filtered view
    updateMapMarkers(view);

    // Mini chart
    renderMiniChart(allPosts);

    // Click interactions
    wireAlertCardClicks(view, allPosts);

    // Ensure detail panel is always populated
    if (view.length) {
      setIncidentDetail(view[0]);
      const firstCard = document.querySelector(".alert-card[data-alert-id]");
      if (firstCard) firstCard.classList.add("selected");
    }
  }

  window.__ALERTIFY_RENDER = render;

  searchEl?.addEventListener("input", render);
  render();

  // Live updates via SSE
  const ev = new EventSource("/api/stream");
  ev.onmessage = (msg) => {
    try {
      const data = JSON.parse(msg.data);

      if (data.type === "new_post" && data.post && data.post.is_disaster === 1) {
        const p = data.post;
        allPosts = [p, ...allPosts.filter((x) => String(x.id) !== String(p.id))].slice(0, 300);
        render();

        const u = String(p.urgency || "").toUpperCase();
        const txt = `[AL-${p.id}] ${p.location_text || "Lipa"} • ${p.disaster_type} • ${u} (${p.confidence}%)`;
        if (u === "CRITICAL") { toast(txt, "critical"); beep(); }
        else if (u === "HIGH") { toast(txt, "high"); }
      }

      if (data.type === "update_post" && data.post && data.post.is_disaster === 1) {
        const p = data.post;
        allPosts = allPosts.map((x) => (String(x.id) === String(p.id) ? p : x));
        render();
      }
    } catch {
      // ignore
    }
  };

  // Keyboard shortcut: toggle sound (S)
  document.addEventListener("keydown", (e) => {
    if (String(e.key || "").toLowerCase() === "s") {
      _soundEnabled = !_soundEnabled;
      toast(`Sound ${_soundEnabled ? "ON" : "OFF"}`, "info");
    }
  });
});
