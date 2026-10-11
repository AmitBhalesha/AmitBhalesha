"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const MAX_GAP_S = 300;
// Set by demo-shim.js on the static demo page: no sign-in, a role switcher instead.
const DEMO = Boolean(window.BEATLINE_DEMO);

const state = {
  config: null,
  user: null,
  shifts: [],
  shift: null,
  report: null,
  trails: {},
  beats: [],
  zones: [],
  selectedOfficer: null,
  selectedBeat: null,
  heat: "",
  t: 0,
  timer: null,
};

let map, renderers, groups, tileLayer, fmtTime, fmtDay, fmtDate, fmtDayTime, refreshTimer;

/* ---------- helpers ---------- */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function isDark() {
  const forced = document.documentElement.dataset.theme;
  if (forced) return forced === "dark";
  return matchMedia("(prefers-color-scheme: dark)").matches;
}

function hhmm(ts) {
  return fmtTime.format(new Date(ts * 1000));
}

function duration(minutes) {
  if (minutes == null) return "—";
  const m = Math.round(minutes);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function seconds(s) {
  if (s == null) return "—";
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Signed out");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

// No GPS in this window (public data only): compare calls with the baseline instead of patrol time.
function callsOnly() {
  return state.report?.mode === "calls_only";
}

function demandLabel(d) {
  if (!d) return "the baseline";
  if (d.source === "crime") return `reported crimes, ${fmtDate.format(new Date(d.start * 1000))} to ${fmtDate.format(new Date((d.end - 1) * 1000))}`;
  return "calls in the 28 days before this shift";
}

function officerLabel(officerId) {
  const o = state.report?.officers.find((x) => x.officer_id === officerId);
  return o ? o.call_sign : officerId;
}

/* ---------- auth ---------- */

function showLogin() {
  stopPlayback();
  clearInterval(refreshTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-agency").textContent = state.config?.agency_name || "";
  $("#demo-hint").hidden = !state.config?.demo;
  $("#login-form [name=username]").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    const { user } = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    e.target.reset();
    await start(user);
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

async function demoLogin(username) {
  const { user } = await api("/api/login", { method: "POST", body: JSON.stringify({ username, password: "" }) });
  stopPlayback();
  state.selectedOfficer = null;
  await start(user);
}

$("#view-as-select").addEventListener("change", (e) => demoLogin(e.target.value));

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* already gone */ }
  state.user = null;
  showLogin();
});

/* ---------- boot ---------- */

async function boot() {
  state.config = await api("/api/config");
  const tz = state.config.timezone;
  fmtTime = new Intl.DateTimeFormat(undefined, { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  fmtDay = new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
  fmtDate = new Intl.DateTimeFormat(undefined, { timeZone: tz, month: "short", day: "numeric", year: "numeric" });
  fmtDayTime = new Intl.DateTimeFormat(undefined, {
    timeZone: tz, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  if (DEMO) return demoLogin("sergeant");
  try {
    await start(await api("/api/me"));
  } catch {
    showLogin();
  }
}

async function start(user) {
  state.user = user;
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#agency").textContent = state.config.agency_name;
  const badge = $("#demo-badge");
  badge.textContent = state.config.demo ? "Synthetic demo data" : state.config.data_note || "";
  badge.hidden = !badge.textContent;
  $("#user-name").textContent = user.display_name;
  $("#user-role").textContent = user.role;
  $("#view-as").hidden = !DEMO;
  $("#logout").hidden = DEMO;
  if (DEMO) $("#view-as-select").value = user.username;

  const auditOnly = user.role === "auditor";
  $("#app").classList.toggle("audit-only", auditOnly);
  $$("#tabs [data-tab]").forEach((b) => {
    const tab = b.dataset.tab;
    b.hidden = auditOnly ? tab !== "audit" : tab === "audit" && user.role !== "admin";
  });
  const note = $("#scope-note");
  note.hidden = user.role !== "officer";
  note.textContent = "You are viewing your own location history. Beat coverage and hot spot figures are city-wide totals with no per-officer detail.";

  if (auditOnly) {
    selectTab("audit");
    return;
  }
  selectTab("units");
  initMap();
  [state.beats, state.zones, state.shifts] = await Promise.all([api("/api/beats"), api("/api/zones"), api("/api/shifts")]);
  renderShiftOptions();
  if (state.shifts.length) {
    await loadShift(0);
  } else {
    $("#tab-units").innerHTML = `<p class="empty">No data yet. Start the Houston poller (<code>python -m beatline houston poll</code>),
      import AVL pings and assignments, or run <code>python -m beatline seed-demo</code>.</p>`;
  }
}

/* ---------- shift picker ---------- */

function renderShiftOptions() {
  $("#shift-select").innerHTML = state.shifts
    .map((s, i) => {
      const what = s.units ? plural(s.units, "unit") : plural(s.calls ?? 0, "call");
      return `<option value="${i}">${esc(fmtDay.format(new Date(s.start * 1000)))} · ${esc(s.name)} ${hhmm(s.start)}–${hhmm(s.end)} · ${what}${s.in_progress ? " · live" : ""}</option>`;
    })
    .join("");
}

$("#shift-select").addEventListener("change", (e) => loadShift(Number(e.target.value)));
$("#prev-shift").addEventListener("click", () => {
  const i = Number($("#shift-select").value);
  if (i < state.shifts.length - 1) loadShift(i + 1);
});
$("#next-shift").addEventListener("click", () => {
  const i = Number($("#shift-select").value);
  if (i > 0) loadShift(i - 1);
});

async function loadShift(index) {
  stopPlayback();
  state.shift = state.shifts[index];
  $("#shift-select").value = String(index);
  const { start, end } = state.shift;
  $("#panel").classList.add("loading");
  try {
    const q = `start=${start}&end=${end}`;
    const [report, trails] = await Promise.all([api(`/api/report?${q}`), api(`/api/trails?${q}`)]);
    state.report = report;
    state.trails = trails;
    if (state.selectedOfficer && !trails[state.selectedOfficer]) state.selectedOfficer = null;
    state.t = start + Math.round((end - start) / 2);
    renderAll();
    if (state.heat) await loadHeat();
  } finally {
    $("#panel").classList.remove("loading");
  }
  clearInterval(refreshTimer);
  if (state.shift.in_progress) refreshTimer = setInterval(refreshLive, 60_000);
}

// While the current shift is live, re-read it every minute (the poller adds calls every five).
async function refreshLive() {
  if (document.hidden || !state.shift?.in_progress) return;
  const shifts = await api("/api/shifts");
  if (shifts[0] && shifts[0].start !== state.shift.start) {
    state.shifts = shifts;
    renderShiftOptions();
    return loadShift(0);  // the shift rolled over: follow the new live one
  }
  state.shifts = shifts;
  renderShiftOptions();
  state.shift = shifts[0];
  $("#shift-select").value = "0";
  const { start, end } = state.shift;
  state.report = await api(`/api/report?start=${start}&end=${end}`);
  renderAll();
}

function applyMode() {
  const co = callsOnly();
  const hasZones = state.zones.length > 0;
  $("#tabs [data-tab=units]").hidden = co;
  $("#tabs [data-tab=zones]").hidden = co && !hasZones;
  $("#playback").hidden = co;
  $('#layers [data-layer="trails"]').closest("label").hidden = co;
  $('#layers [data-layer="zones"]').closest("label").hidden = !hasZones;
  $('#layers [name=heat][value=patrol]').closest("label").hidden = co;
  $("#heat-demand-label").textContent = state.report.demand?.source === "crime" ? "Reported crime (baseline)" : "Call demand (28 days)";
  if (co && state.heat === "patrol") {
    state.heat = "";
    $('#layers [name=heat][value=""]').checked = true;
  }
  const current = $('#tabs [aria-selected="true"]');
  if (!current || current.hidden) selectTab("beats");
}

function renderAll() {
  applyMode();
  renderTiles();
  renderUnits();
  renderBeats();
  renderZones();
  renderCalls();
  drawBeats();
  drawTrails();
  drawCalls();
  drawZones();
  updateScrub();
}

/* ---------- tiles ---------- */

function callsOnlyTiles() {
  const r = state.report;
  const s = r.summary;
  const hours = Math.max(0.25, (Math.min(r.window.end, Date.now() / 1000) - r.window.start) / 3600);
  const busiest = [...r.beats].sort((a, b) => b.calls - a.calls)[0];
  const live = state.shift.in_progress;
  return [
    { label: live ? "Calls so far" : "Calls this shift", value: s.calls,
      sub: `${(s.calls / hours).toFixed(1)} per hour${live ? ` · updated ${hhmm(Date.now() / 1000)}` : ""}` },
    { label: "Busiest beat", value: busiest?.calls ? busiest.beat_id : "—",
      sub: busiest?.calls ? `${plural(busiest.calls, "call")}, ${busiest.expected_calls} expected` : "no calls yet" },
    { label: "Beats busier than usual", value: s.beats_busier, sub: "vs. each beat's usual share" },
    { label: "Calls not on the map", value: s.unmapped_calls, sub: "address could not be located" },
  ];
}

function renderTiles() {
  const s = state.report.summary;
  const p1 = s.median_gps_response_s["1"];
  const tiles = callsOnly() ? callsOnlyTiles() : [
    { label: "Units tracked", value: s.units, sub: `${s.unit_hours} unit-hours of GPS` },
    { label: "City coverage", value: `${s.city_covered_pct}%`, sub: "of beat-time had a unit present" },
    { label: "Beats with 2h+ gap", value: s.beats_with_long_gap, sub: `${plural(s.beats_under_covered, "beat")} under-covered` },
    { label: "P1 response, GPS", value: p1 != null ? seconds(p1) : "—", sub: `median of ${plural(s.calls, "call")} this shift` },
  ];
  $("#tiles").innerHTML = tiles
    .map((t) => `<div class="tile"><div class="label">${esc(t.label)}</div><div class="value">${esc(t.value)}</div><div class="sub">${esc(t.sub)}</div></div>`)
    .join("");
}

/* ---------- tabs ---------- */

function selectTab(name) {
  $$("#tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab-body").forEach((el) => (el.hidden = el.id !== `tab-${name}`));
  if (name === "audit") loadAudit();
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) selectTab(b.dataset.tab);
});

function rowKeys(container, handler) {
  container.addEventListener("click", (e) => {
    const row = e.target.closest("[data-key]");
    if (row) handler(row.dataset.key);
  });
  container.addEventListener("keydown", (e) => {
    const row = e.target.closest("[data-key]");
    if (row && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      handler(row.dataset.key);
    }
  });
}

function renderUnits() {
  const rows = state.report.officers;
  if (!rows.length) {
    $("#tab-units").innerHTML = `<p class="empty">No units tracked in this shift.</p>`;
    return;
  }
  $("#tab-units").innerHTML = `
    <p class="tab-intro">Select a unit to highlight its trail. <em>In beat</em> is the share of tracked time spent inside the assigned beat.</p>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Unit</th><th>Beat</th><th title="Share of tracked time inside the assigned beat">In beat</th>
        <th class="num">Miles</th><th class="num">Calls</th>
        <th class="num" title="Includes time on scene at calls and breaks">Stationary</th>
      </tr></thead>
      <tbody>${rows.map((o) => `
        <tr data-key="${esc(o.officer_id)}" tabindex="0" class="${o.officer_id === state.selectedOfficer ? "selected" : ""}">
          <td><div class="unit-cs">${esc(o.call_sign)}</div><div class="unit-name">${esc(o.name)}</div></td>
          <td>${o.assigned_beat ? esc(o.assigned_beat) : '<span class="muted">Floater</span>'}</td>
          <td>${o.pct_in_assigned == null ? '<span class="muted">—</span>' : `
            <div class="pct-cell"><span>${Math.round(o.pct_in_assigned)}%</span>
            <div class="bar"><span style="width:${Math.min(100, o.pct_in_assigned)}%"></span></div></div>`}</td>
          <td class="num">${o.miles}</td>
          <td class="num">${o.calls_handled}</td>
          <td class="num">${duration(o.stationary_minutes)}</td>
        </tr>`).join("")}
      </tbody>
    </table></div>`;
}
rowKeys($("#tab-units"), (id) => selectOfficer(state.selectedOfficer === id ? null : id, true));

const STATUS_TEXT = {
  uncovered: "No patrol",
  under: "Under-covered",
  balanced: "Balanced",
  over: "Over-covered",
  low_demand: "Low demand",
};
const CALL_STATUS = { busier: "▲ Busier than usual", quieter: "▼ Quieter than usual", typical: "Typical" };

function callStatusLabel(b) {
  return `<span class="status ${b.call_status}"><span class="dot" aria-hidden="true"></span>${CALL_STATUS[b.call_status]}</span>`;
}

function renderBeatsCallsOnly() {
  const r = state.report;
  const rows = [...r.beats].sort((a, b) => b.calls - a.calls || b.demand_share - a.demand_share);
  const mapped = rows.reduce((n, b) => n + b.calls, 0);
  $("#tab-beats").innerHTML = `
    <p class="tab-intro">Calls this shift compared with each beat's usual share of demand, taken from ${esc(demandLabel(r.demand))}.
      <em>Expected</em> is that share of this shift's ${plural(mapped, "mapped call")}. A beat is flagged only when the difference is too large to be chance.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Beat</th><th class="num">Calls</th><th class="num">Expected</th><th>Compared with usual</th></tr></thead>
      <tbody>${rows.map((b) => `
        <tr data-key="${esc(b.beat_id)}" tabindex="0" class="${b.beat_id === state.selectedBeat ? "selected" : ""}">
          <td><strong>${esc(b.name)}</strong></td>
          <td class="num">${b.calls}</td>
          <td class="num">${b.expected_calls}</td>
          <td>${callStatusLabel(b)}</td>
        </tr>`).join("")}
      </tbody>
    </table></div>`;
}

function statusLabel(b) {
  const idx = b.coverage_index == null ? "" : ` ${b.coverage_index}×`;
  return `<span class="status ${b.status}"><span class="dot" aria-hidden="true"></span>${STATUS_TEXT[b.status]}${idx}</span>`;
}

function renderBeats() {
  if (callsOnly()) return renderBeatsCallsOnly();
  const th = state.config.thresholds;
  $("#tab-beats").innerHTML = `
    <p class="tab-intro">Coverage index = share of this shift's patrol time ÷ share of ${esc(demandLabel(state.report.demand))}.
      Below ${th.under_covered}× means the beat got far less patrol than its call volume suggests; above ${th.over_covered}× means far more.</p>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Beat</th><th>Coverage</th>
        <th class="num" title="Share of the shift with at least one unit inside the beat">Covered</th>
        <th class="num" title="Longest stretch with no unit inside the beat">Longest gap</th>
        <th class="num">Calls</th>
      </tr></thead>
      <tbody>${state.report.beats.map((b) => `
        <tr data-key="${esc(b.beat_id)}" tabindex="0" class="${b.beat_id === state.selectedBeat ? "selected" : ""}">
          <td><strong>${esc(b.name)}</strong><div class="unit-name">${b.assigned_units.length ? `${b.assigned_units.length} assigned` : "Unassigned"}</div></td>
          <td>${statusLabel(b)}</td>
          <td class="num">${Math.round(b.covered_pct)}%</td>
          <td class="num" title="${hhmm(b.longest_gap_start)}–${hhmm(b.longest_gap_end)}">
            ${b.long_gap ? `<span class="flag">⚠ ${duration(b.longest_gap_minutes)}</span>` : duration(b.longest_gap_minutes)}</td>
          <td class="num">${b.calls}</td>
        </tr>`).join("")}
      </tbody>
    </table></div>`;
}
rowKeys($("#tab-beats"), (id) => selectBeat(state.selectedBeat === id ? null : id, true));

function renderZones() {
  const zones = state.report.zones;
  if (!zones.length) {
    $("#tab-zones").innerHTML = `<p class="empty">No directed-patrol hot spots configured.</p>`;
    return;
  }
  $("#tab-zones").innerHTML = `
    <p class="tab-intro">Directed-patrol hot spots and how many unit-minutes each received this shift against its target.</p>
    ${zones.map((z) => {
      const scale = Math.max(z.target_minutes, z.minutes) || 1;
      const met = z.minutes >= z.target_minutes;
      return `
        <div class="zone" data-key="${esc(z.zone_id)}" tabindex="0">
          <h3><span>${esc(z.name)}</span>
            <span class="flag ${met ? "ok" : ""}">${met ? "✓ Target met" : `✕ ${Math.round(z.pct_of_target)}% of target`}</span></h3>
          <p>${esc(z.reason || "")}</p>
          <div class="bar" aria-hidden="true"><span style="width:${(100 * z.minutes) / scale}%"></span>
            <i class="target" style="left:calc(${(100 * z.target_minutes) / scale}% - 1px)"></i></div>
          <div class="meta"><span>${z.visits} visits · ${z.units} units</span>
            <span>${duration(z.minutes)} of ${duration(z.target_minutes)} target</span></div>
        </div>`;
    }).join("")}`;
}
rowKeys($("#tab-zones"), (id) => {
  const z = state.zones.find((x) => x.id === id);
  if (z) map.flyTo([z.lat, z.lon], 16, { duration: 0.6 });
});

function unmappedList() {
  const rows = state.report.unmapped_calls || [];
  if (!rows.length) return "";
  return `
    <h3 class="subhead">Not on the map (${rows.length})</h3>
    <p class="tab-intro">The geocoder could not place these addresses, often because the address is an intersection or has no house number. They are not included in the call counts above.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Call</th><th>Address as published</th></tr></thead>
      <tbody>${[...rows].reverse().map((c) => `
        <tr><td>${hhmm(c.ts)}</td><td>${esc(c.call_type)}</td><td class="unit-name">${esc(c.address)}</td></tr>`).join("")}
      </tbody>
    </table></div>`;
}

function isActive(c) {
  return Boolean(state.shift?.in_progress && c.cleared_ts && c.cleared_ts >= Date.now() / 1000 - 600);
}

function renderCalls() {
  const calls = state.report.calls;
  if (!calls.length) {
    $("#tab-calls").innerHTML = `<p class="empty">No calls in this window.</p>${callsOnly() ? unmappedList() : ""}`;
    return;
  }
  if (callsOnly()) {
    const fromHec = calls.some((c) => c.source === "hec");
    $("#tab-calls").innerHTML = `
      <p class="tab-intro">${fromHec ? "Police calls from the city's Active Incidents list, captured every 5 minutes. Addresses are as published; priority is not published." : "Calls for service in this window."}
        <em>Listed until</em> is the last time the call was still open.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Call</th><th>Address</th><th>Beat</th><th class="num">Listed until</th></tr></thead>
        <tbody>${[...calls].reverse().map((c) => `
          <tr data-key="${esc(c.id)}" tabindex="0">
            <td>${hhmm(c.ts)}</td>
            <td>${esc(c.call_type)}${c.priority ? ` <span class="muted">P${c.priority}</span>` : ""}</td>
            <td class="unit-name">${esc(c.address || "—")}</td>
            <td>${esc(c.beat_id || "—")}</td>
            <td class="num">${isActive(c) ? "Open" : c.cleared_ts ? hhmm(c.cleared_ts) : "—"}</td>
          </tr>`).join("")}
        </tbody>
      </table></div>${unmappedList()}`;
    return;
  }
  $("#tab-calls").innerHTML = `
    <p class="tab-intro">Calls for service this shift. <em>On scene</em> is measured from GPS: call created until the assigned unit was within 100 m.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Call</th><th>Beat</th><th>Unit</th><th class="num">On scene</th></tr></thead>
      <tbody>${calls.map((c) => `
        <tr data-key="${esc(c.id)}" tabindex="0">
          <td>${hhmm(c.ts)}</td>
          <td>${esc(c.call_type)}${c.priority ? ` <span class="muted">P${c.priority}</span>` : ""}</td>
          <td>${esc(c.beat_id || "—")}</td>
          <td>${c.officer_id ? esc(officerLabel(c.officer_id)) : '<span class="muted">—</span>'}</td>
          <td class="num">${seconds(c.gps_response_s)}</td>
        </tr>`).join("")}
      </tbody>
    </table></div>`;
}
rowKeys($("#tab-calls"), (id) => {
  const c = state.report.calls.find((x) => x.id === id);
  if (!c) return;
  map.flyTo([c.lat, c.lon], 17, { duration: 0.6 });
  groups.calls.eachLayer((l) => { if (l.options.callId === id) l.openTooltip(); });
});

async function loadAudit() {
  const el = $("#tab-audit");
  el.innerHTML = `<p class="empty">Loading…</p>`;
  try {
    const rows = await api("/api/audit?limit=300");
    const detail = (d) => Object.entries(d)
      .map(([k, v]) => `${esc(k)}=${esc(Array.isArray(v) ? (v.length > 4 ? `${v.slice(0, 4).join(",")}… (${v.length})` : v.join(",")) : v)}`)
      .join(" ");
    el.innerHTML = `
      <p class="tab-intro">Every sign-in and every view of officer location data. This log is append-only.
        <button type="button" class="ghost" id="audit-refresh">Refresh</button></p>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr><td>${esc(fmtDayTime.format(new Date(r.ts * 1000)))}</td><td>${esc(r.username)}</td>
          <td>${esc(r.action)}</td><td class="unit-name">${detail(r.detail)}</td></tr>`).join("")}
        </tbody>
      </table></div>`;
    $("#audit-refresh").addEventListener("click", loadAudit);
  } catch (err) {
    el.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

/* ---------- map ---------- */

function initMap() {
  if (map) {
    setTimeout(() => map.invalidateSize(), 0);
    return;
  }
  map = L.map("map", { zoomControl: true, attributionControl: true });
  map.on("zoomend", updateLabels);
  const pane = (name, z) => {
    map.createPane(name);
    map.getPane(name).style.zIndex = z;
    return L.canvas({ pane: name, padding: 0.3 });
  };
  renderers = { heat: pane("heat", 350), beats: pane("beats", 360), trails: pane("trails", 410), zones: pane("zones", 420), calls: pane("calls", 430) };
  groups = {
    heat: L.layerGroup().addTo(map),
    beats: L.layerGroup().addTo(map),
    labels: L.layerGroup().addTo(map),
    trails: L.layerGroup().addTo(map),
    zones: L.layerGroup().addTo(map),
    calls: L.layerGroup().addTo(map),
    units: L.layerGroup().addTo(map),
  };
  if (innerWidth < 860) $("#layers").open = false;
  setTiles();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    setTiles();
    if (state.report) renderAll();
  });
  const b = state.config.bounds;
  if (b) map.fitBounds(b, { padding: [20, 20] });
  else map.setView([39.5, -98.35], 4);
  setTimeout(() => map.invalidateSize(), 0);
}

function setTiles() {
  if (tileLayer) tileLayer.remove();
  const c = state.config;
  if (!c.tile_url) return;
  tileLayer = L.tileLayer(isDark() ? c.tile_url_dark : c.tile_url, {
    attribution: c.tile_attribution, subdomains: "abcd", maxZoom: 20,
  }).addTo(map);
}

// Beat labels: always for a small city; for a big one (Houston has 100+ beats) only when zoomed in.
function updateLabels() {
  const show = $('#layers [data-layer="beats"]').checked && (state.beats.length <= 30 || map.getZoom() >= 12);
  if (show) groups.labels.addTo(map); else groups.labels.remove();
}

$$("#layers [data-layer]").forEach((box) => box.addEventListener("change", () => {
  const g = groups[box.dataset.layer];
  if (box.checked) g.addTo(map); else g.remove();
  if (box.dataset.layer === "beats") updateLabels();
  if (box.dataset.layer === "trails") (box.checked ? groups.units.addTo(map) : groups.units.remove());
}));
$$("#layers [name=heat]").forEach((r) => r.addEventListener("change", () => {
  state.heat = r.value;
  if (state.report) drawBeats();
  loadHeat();
}));

function beatColor(status) {
  if (status === "under" || status === "uncovered" || status === "busier") return cssVar("--div-under");
  if (status === "over" || status === "quieter") return cssVar("--div-over");
  return cssVar("--div-mid");
}

function beatLatLngs(beat) {
  return (beat.rings || [beat.ring]).map((ring) => [ring.map(([lon, lat]) => [lat, lon])]);
}

function drawBeats() {
  groups.beats.clearLayers();
  groups.labels.clearLayers();
  const co = callsOnly();
  const byId = Object.fromEntries(state.report.beats.map((b) => [b.beat_id, b]));
  const fill = co
    ? { busier: 0.32, quieter: 0.16, typical: 0.06 }
    : { uncovered: 0.34, under: 0.24, over: 0.18, balanced: 0.1, low_demand: 0.1 };
  for (const beat of state.beats) {
    const r = byId[beat.id];
    const status = r && (co ? r.call_status : r.status);
    const color = beatColor(status);
    const selected = beat.id === state.selectedBeat;
    const poly = L.polygon(beatLatLngs(beat), {
      renderer: renderers.beats, color: selected ? cssVar("--text") : color, weight: selected ? 3 : co ? 1 : 1.5,
      opacity: 0.9, fillColor: color, fillOpacity: state.heat ? 0 : r ? fill[status] : 0.05,
    });
    if (r && co) {
      poly.bindTooltip(`<strong>${esc(r.name)}</strong><br>${CALL_STATUS[r.call_status]}<br>
        ${plural(r.calls, "call")} this shift · ${r.expected_calls} expected<br>
        ${(r.demand_share * 100).toFixed(1)}% of ${esc(demandLabel(state.report.demand))}`, { sticky: true });
    } else if (r) {
      poly.bindTooltip(`<strong>${esc(r.name)}</strong><br>${STATUS_TEXT[r.status]}${r.coverage_index != null ? ` (${r.coverage_index}×)` : ""}<br>
        Unit present ${Math.round(r.covered_pct)}% of shift · longest gap ${duration(r.longest_gap_minutes)}<br>
        ${r.calls} calls this shift · ${Math.round(r.demand_share * 100)}% of baseline demand`, { sticky: true });
    }
    poly.on("click", () => selectBeat(state.selectedBeat === beat.id ? null : beat.id, false));
    poly.addTo(groups.beats);
    const center = L.polygon(beat.ring.map(([lon, lat]) => [lat, lon])).getBounds().getCenter();
    const sub = !r ? "" : co ? plural(r.calls, "call") : `${STATUS_TEXT[r.status]}${r.long_gap ? " · ⚠ gap" : ""}`;
    const title = co ? beat.id : beat.name;
    L.marker(center, {
      interactive: false,
      icon: L.divIcon({ className: "", html: `<span class="beat-label">${esc(title)}<br><small>${esc(sub)}</small></span>`, iconSize: null }),
    }).addTo(groups.labels);
  }
  updateLabels();
}

function splitTrail(points) {
  const parts = [];
  let cur = [];
  for (let i = 0; i < points.length; i++) {
    if (i > 0 && points[i][0] - points[i - 1][0] > MAX_GAP_S) {
      if (cur.length > 1) parts.push(cur);
      cur = [];
    }
    cur.push([points[i][1], points[i][2]]);
  }
  if (cur.length > 1) parts.push(cur);
  return parts;
}

function drawTrails() {
  groups.trails.clearLayers();
  const sel = state.selectedOfficer;
  const muted = cssVar("--trail-muted");
  const accent = cssVar("--series-1");
  const ids = Object.keys(state.trails).sort((a, b) => (a === sel) - (b === sel));
  for (const id of ids) {
    const isSel = id === sel;
    const line = L.polyline(splitTrail(state.trails[id]), {
      renderer: renderers.trails,
      color: isSel ? accent : muted,
      weight: isSel ? 3.5 : 1.5,
      opacity: isSel ? 0.95 : sel ? 0.18 : 0.5,
    });
    line.bindTooltip(officerLabel(id), { sticky: true });
    line.on("click", () => selectOfficer(state.selectedOfficer === id ? null : id, false));
    line.addTo(groups.trails);
  }
}

function drawCalls() {
  groups.calls.clearLayers();
  const color = cssVar("--series-2");
  const co = callsOnly();
  for (const c of state.report.calls) {
    const strong = co ? isActive(c) : c.priority === 1;
    const m = L.circleMarker([c.lat, c.lon], {
      renderer: renderers.calls, callId: c.id, radius: strong ? 7 : 5,
      color, weight: 2, fillColor: color, fillOpacity: strong ? 0.9 : 0.3,
    });
    const when = co && isActive(c) ? `${hhmm(c.ts)}, still open` : `${hhmm(c.ts)}${c.cleared_ts ? `–${hhmm(c.cleared_ts)}` : ""}`;
    const who = c.officer_id
      ? `<br>Unit ${esc(officerLabel(c.officer_id))} · on scene in ${seconds(c.gps_response_s)}`
      : co ? "" : "<br>No unit recorded";
    m.bindTooltip(`<strong>${esc(c.call_type)}</strong>${c.priority ? ` · P${c.priority}` : ""}<br>${when} · ${esc(c.beat_id || "outside beats")}
      ${c.address ? `<br>${esc(c.address)}` : ""}${who}`);
    m.addTo(groups.calls);
  }
}

function drawZones() {
  groups.zones.clearLayers();
  const color = cssVar("--series-7");
  const byId = Object.fromEntries((state.report?.zones || []).map((z) => [z.zone_id, z]));
  for (const z of state.zones) {
    const r = byId[z.id];
    L.circle([z.lat, z.lon], {
      renderer: renderers.zones, radius: z.radius_m, color, weight: 2, dashArray: "6 5", fillColor: color, fillOpacity: 0.06,
    })
      .bindTooltip(`<strong>${esc(z.name)}</strong><br>${esc(z.reason || "")}${r ? `<br>${duration(r.minutes)} of ${duration(r.target_minutes)} target · ${r.visits} visits` : ""}`)
      .addTo(groups.zones);
  }
}

const RAMPS = {
  patrol: ["#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#0d366b"],
  calls: ["#fbe0d3", "#f6b393", "#eb6834", "#b8481d", "#7a2e10"],
};
const BINS = [0.08, 0.2, 0.4, 0.65];

async function loadHeat() {
  groups.heat.clearLayers();
  $("#legend").innerHTML = "";
  if (!state.heat || !state.shift) return;
  const { start, end } = state.shift;
  const data = await api(`/api/heat?layer=${state.heat}&start=${start}&end=${end}`);
  const ramp = RAMPS[data.layer];
  for (const cell of data.cells) {
    const f = data.max ? cell.v / data.max : 0;
    const bin = BINS.findIndex((b) => f < b);
    const color = ramp[bin === -1 ? ramp.length - 1 : bin];
    const crime = data.demand?.source === "crime";
    const unit = data.layer === "patrol" ? `${duration(cell.v)} of unit presence` : crime ? `${cell.v} reported crimes` : `${cell.v} calls in 28 days`;
    L.rectangle(cell.b, { renderer: renderers.heat, stroke: false, fillColor: color, fillOpacity: 0.6 })
      .bindTooltip(unit, { sticky: true })
      .addTo(groups.heat);
  }
  const crimeLegend = data.demand?.source === "crime";
  const maxText = data.layer === "patrol" ? duration(data.max) : `${data.max} ${crimeLegend ? "crimes" : "calls"}`;
  const what = data.layer === "patrol" ? "Unit-minutes" : crimeLegend ? "Reported crimes" : "Calls";
  $("#legend").innerHTML = `
    <div>${what} per ${data.cell_m} m cell</div>
    <div class="ramp">${ramp.map((c) => `<span style="background:${c}"></span>`).join("")}</div>
    <div class="ends"><span>Low</span><span>${esc(maxText)}</span></div>`;
}

/* ---------- selection ---------- */

function selectOfficer(id, fromTable) {
  state.selectedOfficer = id;
  drawTrails();
  renderUnits();
  updateUnits();
  if (id && state.trails[id]?.length) {
    const pts = state.trails[id].map((p) => [p[1], p[2]]);
    map.flyToBounds(L.latLngBounds(pts), { padding: [40, 40], duration: 0.6 });
  }
  if (!fromTable && id) selectTab("units");
}

function selectBeat(id, fromTable) {
  state.selectedBeat = id;
  drawBeats();
  renderBeats();
  const beat = state.beats.find((b) => b.id === id);
  if (beat && fromTable) map.flyToBounds(L.polygon(beatLatLngs(beat)).getBounds(), { padding: [40, 40], duration: 0.6 });
  if (!fromTable && id) selectTab("beats");
}

/* ---------- playback ---------- */

function positionAt(points, t) {
  let lo = 0, hi = points.length - 1;
  if (hi < 0 || t < points[0][0] - MAX_GAP_S || t > points[hi][0] + MAX_GAP_S) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (points[mid][0] <= t) lo = mid; else hi = mid - 1;
  }
  const a = points[lo], b = points[lo + 1];
  if (a[0] > t) return { lat: a[1], lon: a[2], lost: false };
  if (!b || b[0] - a[0] > MAX_GAP_S) return { lat: a[1], lon: a[2], lost: t - a[0] > 60 };
  const f = (t - a[0]) / (b[0] - a[0]);
  return { lat: a[1] + (b[1] - a[1]) * f, lon: a[2] + (b[2] - a[2]) * f, lost: false };
}

function updateUnits() {
  groups.units.clearLayers();
  const sel = state.selectedOfficer;
  for (const [id, points] of Object.entries(state.trails)) {
    const p = positionAt(points, state.t);
    if (!p) continue;
    const cls = ["unit-marker", sel && sel !== id ? "dim" : "", p.lost ? "lost" : ""].join(" ");
    L.marker([p.lat, p.lon], {
      icon: L.divIcon({ className: "", html: `<span class="${cls}">${esc(officerLabel(id))}</span>`, iconSize: null }),
      zIndexOffset: id === sel ? 1000 : 0,
      keyboard: false,
    })
      .on("click", () => selectOfficer(state.selectedOfficer === id ? null : id, false))
      .addTo(groups.units);
  }
}

function updateScrub() {
  const { start, end } = state.shift;
  $("#scrub").value = String(Math.round((1000 * (state.t - start)) / (end - start)));
  $("#clock").textContent = `${hhmm(state.t)}`;
  updateUnits();
}

$("#scrub").addEventListener("input", (e) => {
  const { start, end } = state.shift;
  state.t = Math.round(start + ((end - start) * Number(e.target.value)) / 1000);
  updateScrub();
});

function stopPlayback() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  $("#play").innerHTML = "&#9654;";
  $("#play").setAttribute("aria-label", "Play");
}

$("#play").addEventListener("click", () => {
  if (state.timer) return stopPlayback();
  if (!state.shift) return;
  if (state.t >= state.shift.end) state.t = state.shift.start;
  $("#play").innerHTML = "&#10074;&#10074;";
  $("#play").setAttribute("aria-label", "Pause");
  state.timer = setInterval(() => {
    state.t = Math.min(state.shift.end, state.t + Number($("#speed").value) / 10);
    updateScrub();
    if (state.t >= state.shift.end) stopPlayback();
  }, 100);
});

boot();
