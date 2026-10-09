// Déjà Vu demo UI. Plain JS, no build step: FastAPI serves this file as is.
//
// Everything shown comes from the API. Nothing here invents a number — where
// a figure is derived (a family's average time to resolve, incidents sharing a
// signature), it is computed from /incidents and /services on the page.

"use strict";

const WEIGHTS = { signature: 10, tags: 3, same_service: 4, dependency: 2 };
const PART_LABELS = {
  signature: "identical error",
  tags: "shared tags",
  same_service: "same service",
  dependency: "dependency tie-break",
};

const PRESETS = [
  {
    id: "pool",
    label: "refund-service · pool exhaustion",
    service: "refund-service",
    error: "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
    tags: ["connection-pool", "timeout", "database"],
  },
  {
    id: "cert",
    label: "payment-gateway · bad certificate",
    service: "payment-gateway",
    error: "x509: certificate signed by unknown authority",
    tags: ["tls", "certificate"],
  },
  {
    id: "oom",
    label: "search-api · OOMKilled",
    service: "search-api",
    error: "OOMKilled: container exceeded memory limit",
    tags: ["memory", "oom"],
  },
  {
    id: "novel",
    label: "never seen before",
    service: "refund-service",
    error: "PaymentRefundException: ledger entry 4471902 locked by txn 0x7f3a2b1c",
    tags: ["ledger"],
  },
];

const EXPLAINERS = [
  { id: "template", name: "Template", note: "free · no model" },
  { id: "openai", name: "OpenAI", note: "gpt-4o-mini" },
  { id: "claude", name: "Claude", note: "haiku 4.5" },
];

// The same rules as dejavu/normalize.py, used here only to highlight which
// parts of the raw error the server replaced. The server's normalised
// signature is always what is displayed as the result.
const VOLATILE = [
  /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?/g,
  /\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b/g,
  /\b0x[0-9a-fA-F]+\b/g,
  /\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b/g,
  /\b\d+(?:\.\d+)?\s?(?:ms|s)\b/g,
  /\b\d{6,}\b/g,
];

const cache = { services: null, incidents: null };
const triageState = {
  preset: "pool",
  service: PRESETS[0].service,
  error: PRESETS[0].error,
  tags: PRESETS[0].tags.join(", "),
  explainer: "template",
  result: null,
  expanded: null,
};
let incidentFilter = { q: "", family: "All", severity: "", service: "" };
let highlightedDep = "HikariCP";

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $("#view");

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = body && body.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ")
      : `${res.status} ${res.statusText}`;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return body;
}

async function getServices() {
  if (!cache.services) cache.services = await api("/services");
  return cache.services;
}
async function getIncidents() {
  if (!cache.incidents) cache.incidents = await api("/incidents");
  return cache.incidents;
}

function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts = { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleString("en-GB", opts) + (withTime ? " UTC" : "");
}
function fmtAgo(iso) {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (Number.isNaN(days)) return "";
  if (days < 31) return `${days}d ago`;
  if (days < 365) return `${Math.round(days / 30)} mo ago`;
  return `${(days / 365).toFixed(1)} yr ago`;
}
const sev = (s) => `<span class="sev sev-${esc(s)}">${esc(s)}</span>`;
const icon = (path) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17.5v.01"/>',
  doc: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/>',
  arrow: '<path d="M15 6l-6 6 6 6"/>',
  wand: '<path d="M4 20 14 10M15 4v3M20 9h-3M18.5 5.5l-2 2"/>',
  graph: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.8 7.8 10.6 16M16.2 7.8 13.4 16M8.5 6h7"/>',
  scale: '<path d="M12 4v16M5 8h14M5 8l-3 6a3 3 0 0 0 6 0zM19 8l-3 6a3 3 0 0 0 6 0z"/>',
};

function highlightRaw(raw) {
  // Collect every span the normalisation rules would replace, then mark them.
  const spans = [];
  for (const re of VOLATILE) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(raw))) {
      const start = m.index, end = m.index + m[0].length;
      if (!spans.some((s) => start < s.end && end > s.start)) spans.push({ start, end });
    }
  }
  spans.sort((a, b) => a.start - b.start);
  let out = "", at = 0;
  for (const s of spans) {
    out += esc(raw.slice(at, s.start)) + `<del>${esc(raw.slice(s.start, s.end))}</del>`;
    at = s.end;
  }
  return out + esc(raw.slice(at));
}
function highlightTokens(sig) {
  return esc(sig).replace(/&lt;(TS|UUID|HEX|IP)&gt;/g, "\u0000$1\u0001")
    .replace(/\bN(ms|s)?\b/g, (t) => `<mark>${t}</mark>`)
    .replace(/\u0000(\w+)\u0001/g, (_, t) => `<mark>&lt;${t}&gt;</mark>`);
}

function showBanner(message) {
  const el = $("#banner");
  if (!message) { el.hidden = true; return; }
  el.textContent = message;
  el.hidden = false;
}

// ---------------------------------------------------------------------------
// header status
// ---------------------------------------------------------------------------

async function refreshStatus() {
  const pill = $("#health-pill");
  try {
    const h = await api("/health");
    if (h.status === "ok") {
      pill.className = "pill pill-ok";
      pill.innerHTML = `<span class="dot"></span>FalkorDB · ${h.incidents} incidents`;
      showBanner(null);
    } else {
      pill.className = "pill pill-warn";
      pill.innerHTML = `<span class="dot"></span>Degraded`;
      showBanner(`The graph isn't ready: ${h.detail || (h.incidents === 0 ? "no incidents loaded. Run scripts/load_incidents.py." : h.graph)}`);
    }
  } catch (e) {
    pill.className = "pill pill-bad";
    pill.innerHTML = `<span class="dot"></span>API down`;
    showBanner(`Can't reach the Déjà Vu API: ${e.message}`);
  }
  try {
    const b = await api("/budget");
    const bp = $("#budget-pill");
    if (b.spent_usd == null) {
      bp.className = "pill pill-bad";
      bp.textContent = "Spend ledger unreadable";
    } else {
      const low = b.remaining_usd < b.limit_usd * 0.2;
      bp.className = `pill ${low ? "pill-warn" : "pill-muted"}`;
      bp.textContent = `Model spend $${b.spent_usd.toFixed(2)} / $${b.limit_usd.toFixed(2)}`;
    }
    bp.title = "Hard cap enforced in dejavu/budget.py. The template explainer costs nothing.";
    bp.hidden = false;
  } catch { /* budget is informational */ }
}

// ---------------------------------------------------------------------------
// triage
// ---------------------------------------------------------------------------

async function renderTriage() {
  let services = [];
  try { services = await getServices(); } catch { /* banner already explains */ }
  const names = services.length ? services.map((s) => s.service) : [triageState.service];
  if (!names.includes(triageState.service)) names.push(triageState.service);

  view().innerHTML = `
    <div class="page-head">
      <div>
        <h1>Have we seen this before?</h1>
        <p>Paste the alert. Déjà Vu finds past incidents that match, shows why each one matched, and lists other services exposed through a shared dependency.</p>
      </div>
    </div>
    <div class="grid-2">
      <div class="stack">
        <form class="card" id="triage-form" novalidate>
          <p class="eyebrow">Try a demo alert</p>
          <div class="presets">
            ${PRESETS.map((p) => `<button type="button" class="preset ${triageState.preset === p.id ? "active" : ""}" data-preset="${p.id}">${esc(p.label)}</button>`).join("")}
          </div>
          <div class="form-grid">
            <div class="two">
              <div class="field">
                <label for="f-service">Alerting service</label>
                <select id="f-service" class="input">
                  ${names.map((n) => `<option ${n === triageState.service ? "selected" : ""}>${esc(n)}</option>`).join("")}
                </select>
              </div>
              <div class="field">
                <label for="f-tags">Tags <span class="faint" style="text-transform:none">comma separated</span></label>
                <input id="f-tags" class="input" value="${esc(triageState.tags)}" placeholder="connection-pool, timeout">
              </div>
            </div>
            <div class="field">
              <label for="f-error">Error from the alert</label>
              <textarea id="f-error" class="input" rows="3" placeholder="HikariPool-1 - Connection is not available, request timed out after 30000ms.">${esc(triageState.error)}</textarea>
            </div>
            <div class="form-foot">
              <div class="field">
                <label>Briefing written by</label>
                <div class="seg" role="radiogroup">
                  ${EXPLAINERS.map((x) => `<button type="button" role="radio" aria-checked="${triageState.explainer === x.id}" class="${triageState.explainer === x.id ? "on" : ""}" data-explainer="${x.id}">${x.name}<small>${x.note}</small></button>`).join("")}
                </div>
              </div>
              <button class="btn btn-primary" id="run" type="submit">${icon(ICONS.wand)}Triage alert</button>
            </div>
            <p class="err" id="form-err" hidden></p>
          </div>
        </form>
        <div id="results" class="stack"></div>
      </div>
      <aside class="stack sticky-col" id="side"></aside>
    </div>`;

  const form = $("#triage-form");
  form.addEventListener("click", (e) => {
    const p = e.target.closest("[data-preset]");
    if (p) {
      const preset = PRESETS.find((x) => x.id === p.dataset.preset);
      Object.assign(triageState, {
        preset: preset.id, service: preset.service, error: preset.error, tags: preset.tags.join(", "),
      });
      $("#f-service").value = preset.service;
      $("#f-error").value = preset.error;
      $("#f-tags").value = triageState.tags;
      form.querySelectorAll(".preset").forEach((b) => b.classList.toggle("active", b === p));
      runTriage();
    }
    const x = e.target.closest("[data-explainer]");
    if (x) {
      triageState.explainer = x.dataset.explainer;
      form.querySelectorAll("[data-explainer]").forEach((b) => {
        b.classList.toggle("on", b === x);
        b.setAttribute("aria-checked", b === x);
      });
    }
  });
  form.addEventListener("input", (e) => {
    if (e.target.id === "f-error") { triageState.error = e.target.value; $("#form-err").hidden = true; }
    if (e.target.id === "f-tags") triageState.tags = e.target.value;
    if (["f-error", "f-tags", "f-service"].includes(e.target.id)) {
      triageState.preset = null;
      form.querySelectorAll(".preset").forEach((b) => b.classList.remove("active"));
    }
  });
  $("#f-service").addEventListener("change", (e) => { triageState.service = e.target.value; });
  form.addEventListener("submit", (e) => { e.preventDefault(); runTriage(); });

  renderSideIdle();
  if (triageState.result) renderResults(triageState.result);
  else runTriage();
}

function renderSideIdle() {
  $("#side").innerHTML = scoringKey();
}

function scoringKey() {
  return `
    <div class="card">
      <div class="card-h"><h3>${icon(ICONS.scale)}How scores work</h3></div>
      <div class="weights">
        <div><span>identical error signature</span><b>+${WEIGHTS.signature}</b></div>
        <div><span>same service</span><b>+${WEIGHTS.same_service}</b></div>
        <div><span>each shared tag</span><b>+${WEIGHTS.tags}</b></div>
        <div class="dim"><span>shares a dependency</span><b>+${WEIGHTS.dependency}</b></div>
      </div>
      <p class="foot-note">Scores are points, not probabilities. To be returned at all, a past incident needs the identical error, the same service, or at least two shared tags. A shared dependency only breaks ties.</p>
    </div>`;
}

async function runTriage() {
  const err = $("#form-err");
  const error = $("#f-error").value.trim();
  if (!error) {
    err.textContent = "Paste the error message from the alert first.";
    err.hidden = false;
    $("#f-error").focus();
    return;
  }
  triageState.service = $("#f-service").value;
  const tags = $("#f-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  const btn = $("#run");
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  $("#results").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
  try {
    const body = await api("/triage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service: triageState.service, error, tags, explainer: triageState.explainer }),
    });
    body._raw = error;
    body._explainer = triageState.explainer;
    triageState.result = body;
    triageState.expanded = body.matches[0]?.id ?? null;
    renderResults(body);
    refreshStatus();
  } catch (e) {
    $("#results").innerHTML = `<div class="card"><p class="err">${esc(e.message)}</p>${
      e.status === 400 || e.status === 429 ? `<p class="foot-note">Switch the briefing to Template to triage without a model.</p>` : ""}</div>`;
  } finally {
    btn.removeAttribute("aria-busy");
    btn.disabled = false;
  }
}

function renderResults(r) {
  const expl = EXPLAINERS.find((x) => x.id === r._explainer) || EXPLAINERS[0];
  const paragraphs = r.summary.split(/\n\s*\n/).map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");

  $("#results").innerHTML = `
    <div class="card">
      <div class="card-h"><h3>Normalised signature</h3><span class="faint mono">same rules run on stored incidents</span></div>
      <div class="norm">
        <div class="norm-row"><p class="eyebrow">Raw</p><div class="norm-text">${highlightRaw(r._raw || "")}</div></div>
        <div class="norm-row"><p class="eyebrow">Matched as</p><div class="norm-text">${highlightTokens(r.normalised_signature)}</div></div>
      </div>
    </div>

    <div class="card briefing">
      <div class="card-h"><h3>${icon(ICONS.doc)}Briefing</h3><span class="chip">${esc(expl.name)} · ${esc(expl.note)}</span></div>
      ${paragraphs}
    </div>

    <div class="section-title">
      <h2>Seen before</h2>
      <span class="faint mono">${r.matches.length} match${r.matches.length === 1 ? "" : "es"} · ranked by evidence</span>
    </div>
    ${r.matches.length ? r.matches.map(matchCard).join("") : `
      <div class="card empty">
        <h3>No past incident resembles this</h3>
        <p>Nothing shares the error, the service, or two tags. That's a real answer: treat it as new, and write it up afterwards so the next person gets a match.</p>
      </div>`}`;

  $("#results").querySelectorAll(".match-top").forEach((el) => {
    const toggle = () => {
      const card = el.closest(".match");
      card.classList.toggle("collapsed");
      el.setAttribute("aria-expanded", !card.classList.contains("collapsed"));
    };
    el.addEventListener("click", (e) => { if (!e.target.closest("a")) toggle(); });
    el.addEventListener("keydown", (e) => {
      if (e.target === el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); }
    });
  });

  $("#side").innerHTML = riskPanel(r) + scoringKey();
}

function matchCard(m) {
  const parts = Object.entries(m.breakdown).filter(([, v]) => v > 0);
  const total = m.score || 1;
  const open = m.id === triageState.expanded;
  const evidence = [
    m.signature_match ? `<span class="chip chip-ev">${icon(ICONS.check)}Identical error</span>` : "",
    m.same_service ? `<span class="chip chip-ev">${icon(ICONS.check)}Same service</span>` : "",
    ...m.shared_tags.map((t) => `<span class="chip chip-ev">#${esc(t)}</span>`),
  ].join("");
  const context = m.also_shares.map((d) => `<span class="chip chip-cx">also uses ${esc(d)}</span>`).join("");

  return `
    <article class="card match ${open ? "" : "collapsed"}">
      <div class="match-top" role="button" tabindex="0" aria-expanded="${open}">
        <div class="match-main">
          <div class="match-meta">
            ${sev(m.severity)}
            <a class="mono" href="#/incidents/${esc(m.id)}">${esc(m.id)}</a>
            <span class="mono">${esc(m.service)}</span>
            <span>${fmtAgo(m.occurred_at)}</span>
          </div>
          <div class="match-title">${esc(m.title)}</div>
          <div class="bar" aria-hidden="true">${parts.map(([k, v]) => `<span class="b-${k}" style="width:${(v / total) * 100}%"></span>`).join("")}</div>
          <div class="math">${parts.map(([k, v]) => `<span><i class="b-${k}"></i>${PART_LABELS[k]} <em>+${v}</em></span>`).join("")}</div>
        </div>
        <div class="score"><b>${m.score}</b><span>points</span></div>
      </div>
      <div class="match-body">
        <div>
          <p class="eyebrow">Why it matched</p>
          <div class="chips">${evidence}</div>
        </div>
        ${context ? `<div><p class="eyebrow">Context, not a reason</p><div class="chips">${context}</div></div>` : ""}
        ${m.resolution ? `
          <div class="fix">
            <p class="eyebrow">Fix that worked</p>
            <p>${esc(m.resolution)}</p>
            <div class="by">fixed by ${esc(m.fixed_by || "unknown")} · <a href="#/incidents/${esc(m.id)}">open ${esc(m.id)}</a></div>
          </div>` : ""}
      </div>
    </article>`;
}

function riskPanel(r) {
  if (!r.at_risk.length) {
    return `
      <div class="card">
        <div class="card-h"><h2>${icon(ICONS.graph)}Also at risk</h2></div>
        <p class="muted" style="margin:0">No other service shares a dependency with <span class="mono">${esc(r.service)}</span>.</p>
      </div>`;
  }
  const deps = [...new Set(r.at_risk.flatMap((p) => p.via))];
  return `
    <div class="card risk">
      <div class="card-h"><h2>${icon(ICONS.alert)}Also at risk</h2><span class="pill pill-warn">${r.at_risk.length} peer${r.at_risk.length === 1 ? "" : "s"}</span></div>
      <p class="muted" style="margin:0 0 6px;font-size:13px">Services that share a dependency with <span class="mono">${esc(r.service)}</span>. Found by walking the graph, not by reading incident text.</p>
      ${riskGraph(r.service, deps, r.at_risk)}
      ${r.at_risk.map((p) => `
        <div class="peer">
          <div><div class="peer-name">${esc(p.service)}</div><div class="peer-sub">via ${p.via.map(esc).join(", ")}</div></div>
          <div class="peer-count ${p.past_incidents ? "" : "zero"}">${p.past_incidents
            ? `${p.past_incidents} past incident${p.past_incidents === 1 ? "" : "s"}<br><span class="faint">with these tags</span>`
            : "exposed<br><span class=\"faint\">never hit</span>"}</div>
        </div>`).join("")}
    </div>`;
}

function riskGraph(service, deps, peers) {
  const W = 320, rowH = 30;
  const H = Math.max(peers.length, deps.length, 1) * rowH + 20;
  const yOf = (i, n) => 10 + (H - 20) * ((i + 0.5) / n);
  const depY = Object.fromEntries(deps.map((d, i) => [d, yOf(i, deps.length)]));
  const svcY = H / 2;
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  return `
    <svg class="risk-graph" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(service)} linked to ${peers.length} peers through ${esc(deps.join(", "))}">
      ${deps.map((d) => `<line x1="96" y1="${svcY}" x2="124" y2="${depY[d]}" stroke="#38bdf8" stroke-width="1.2"/>`).join("")}
      ${peers.map((p, i) => p.via.map((d) => `<line x1="180" y1="${depY[d]}" x2="200" y2="${yOf(i, peers.length)}" stroke="#fbbf24" stroke-width="1.2" stroke-dasharray="3 2"/>`).join("")).join("")}
      <rect x="2" y="${svcY - 12}" width="94" height="24" rx="6" fill="#0b2536" stroke="#38bdf8"/>
      <text x="49" y="${svcY + 4}" text-anchor="middle" fill="#bae6fd" font-family="JetBrains Mono, monospace" font-size="9.5">${esc(trunc(service, 16))}</text>
      ${deps.map((d) => `
        <rect x="124" y="${depY[d] - 11}" width="56" height="22" rx="11" fill="#1a1c3d" stroke="#818cf8"/>
        <text x="152" y="${depY[d] + 3.5}" text-anchor="middle" fill="#c7d2fe" font-family="JetBrains Mono, monospace" font-size="9">${esc(trunc(d, 9))}</text>`).join("")}
      ${peers.map((p, i) => `
        <rect x="200" y="${yOf(i, peers.length) - 11}" width="118" height="22" rx="6" fill="#2b1f07" stroke="#b7791f"/>
        <text x="259" y="${yOf(i, peers.length) + 3.5}" text-anchor="middle" fill="#fde68a" font-family="JetBrains Mono, monospace" font-size="9">${esc(trunc(p.service, 19))}</text>`).join("")}
    </svg>`;
}

// ---------------------------------------------------------------------------
// incidents
// ---------------------------------------------------------------------------

function familyStats(list) {
  const services = new Set(list.map((i) => i.service));
  const ttrs = list.map((i) => i.time_to_resolve_min).filter((n) => n != null);
  return {
    count: list.length,
    services: services.size,
    sev1: list.filter((i) => i.severity === "SEV1").length,
    avgTtr: ttrs.length ? Math.round(ttrs.reduce((a, b) => a + b, 0) / ttrs.length) : null,
  };
}

async function renderIncidents() {
  view().innerHTML = `<div class="skeleton"></div>`;
  let incidents;
  try { incidents = await getIncidents(); } catch (e) {
    view().innerHTML = `<div class="card"><p class="err">${esc(e.message)}</p></div>`;
    return;
  }
  const familySize = (f) => incidents.filter((i) => i.family === f).length;
  const families = [...new Set(incidents.map((i) => i.family))]
    .sort((a, b) => familySize(b) - familySize(a) || a.localeCompare(b));
  const services = [...new Set(incidents.map((i) => i.service))].sort();

  view().innerHTML = `
    <div class="page-head">
      <div>
        <h1>Incident memory</h1>
        <p>${incidents.length} past incidents across ${families.length} root-cause families. Families are grouped by tag.</p>
      </div>
    </div>
    <div class="families" id="families"></div>
    <div class="toolbar">
      <input class="input" id="q" type="search" placeholder="Search titles, errors, fixes, tags" value="${esc(incidentFilter.q)}" aria-label="Search incidents">
      <select class="input" id="sev" aria-label="Severity">
        <option value="">All severities</option>
        ${["SEV1", "SEV2", "SEV3"].map((s) => `<option ${incidentFilter.severity === s ? "selected" : ""}>${s}</option>`).join("")}
      </select>
      <select class="input" id="svc" aria-label="Service">
        <option value="">All services</option>
        ${services.map((s) => `<option ${incidentFilter.service === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
      </select>
    </div>
    <div id="list"></div>`;

  const paint = () => {
    const famCards = [["All", incidents], ...families.map((f) => [f, incidents.filter((i) => i.family === f)])];
    $("#families").innerHTML = famCards.map(([name, list]) => {
      const s = familyStats(list);
      return `
        <button type="button" class="card family ${incidentFilter.family === name ? "on" : ""}" data-family="${esc(name)}">
          <div class="card-h" style="margin:0"><h3>${name === "All" ? "All incidents" : esc(name)}</h3><span class="n">${s.count}</span></div>
          <div class="stats">${s.services} service${s.services === 1 ? "" : "s"} · ${s.sev1} SEV1${s.avgTtr != null ? ` · avg ${s.avgTtr} min to resolve` : ""}</div>
        </button>`;
    }).join("");

    const q = incidentFilter.q.toLowerCase();
    const shown = incidents.filter((i) =>
      (incidentFilter.family === "All" || i.family === incidentFilter.family) &&
      (!incidentFilter.severity || i.severity === incidentFilter.severity) &&
      (!incidentFilter.service || i.service === incidentFilter.service) &&
      (!q || [i.id, i.title, i.summary, i.service, i.error_signature, i.resolution, ...i.tags].join(" ").toLowerCase().includes(q)));

    $("#list").innerHTML = shown.length ? `
      <div class="rows">
        <div class="rows-h"><span>${incidentFilter.family === "All" ? "All incidents" : esc(incidentFilter.family)} · ${shown.length}</span><span>newest first</span></div>
        ${shown.map((i) => `
          <a class="row" href="#/incidents/${esc(i.id)}">
            ${sev(i.severity)}
            <div style="min-width:0">
              <div class="row-title"><span class="mono faint">${esc(i.id)}</span> ${esc(i.title)}</div>
              <div class="row-sub"><span class="mono">${esc(i.service)}</span> · ${i.tags.map((t) => "#" + esc(t)).join(" ")}</div>
            </div>
            <div class="row-right">${fmtDate(i.occurred_at)}<br>${i.time_to_resolve_min != null ? `${i.time_to_resolve_min} min` : ""}</div>
          </a>`).join("")}
      </div>` : `<div class="card empty"><h3>No incidents match</h3><p>Clear a filter or try a different search.</p></div>`;
  };

  view().addEventListener("click", (e) => {
    const f = e.target.closest("[data-family]");
    if (f) { incidentFilter.family = f.dataset.family; paint(); }
  });
  $("#q").addEventListener("input", (e) => { incidentFilter.q = e.target.value; paint(); });
  $("#sev").addEventListener("change", (e) => { incidentFilter.severity = e.target.value; paint(); });
  $("#svc").addEventListener("change", (e) => { incidentFilter.service = e.target.value; paint(); });
  paint();
}

async function renderIncident(id) {
  view().innerHTML = `<div class="skeleton"></div>`;
  let inc, all = [], services = [];
  try {
    [inc, all, services] = await Promise.all([api(`/incidents/${encodeURIComponent(id)}`), getIncidents(), getServices()]);
  } catch (e) {
    view().innerHTML = `
      <a class="back" href="#/incidents">${icon(ICONS.arrow)}All incidents</a>
      <div class="card empty"><h3>${e.status === 404 ? `No incident ${esc(id)}` : "Couldn't load this incident"}</h3><p>${esc(e.message)}</p></div>`;
    return;
  }
  const family = all.find((i) => i.id === inc.id)?.family;
  const sameSig = all.filter((i) => i.id !== inc.id && i.error_signature && i.error_signature === inc.error_signature);
  const svc = services.find((s) => s.service === inc.service);
  const peersByDep = (svc?.dependencies || []).map((d) => ({
    dep: d,
    peers: services.filter((s) => s.service !== inc.service && s.dependencies.includes(d)),
  }));

  view().innerHTML = `
    <a class="back" href="#/incidents" id="back">${icon(ICONS.arrow)}Back</a>
    <div class="match-meta">${sev(inc.severity)}<span class="mono">${esc(inc.id)}</span>${family ? `<span class="chip">${esc(family)}</span>` : ""}</div>
    <h1 class="detail-title">${esc(inc.title)}</h1>
    <div class="muted"><a class="mono" href="#/services">${esc(inc.service)}</a> · ${fmtDate(inc.occurred_at, true)}</div>

    <div class="stats-3">
      <div class="stat"><p class="eyebrow">Time to resolve</p><div class="v">${inc.time_to_resolve_min != null ? `${inc.time_to_resolve_min} min` : "—"}</div></div>
      <div class="stat"><p class="eyebrow">Fixed by</p><div class="v">${esc(inc.fixed_by || "—")}</div></div>
      <div class="stat"><p class="eyebrow">Team</p><div class="v">${esc(svc?.team || "—")}</div></div>
    </div>

    <div class="grid-2">
      <div class="stack">
        <div class="card"><div class="card-h"><h2>What happened</h2></div><p style="margin:0;color:#d4dde6">${esc(inc.summary)}</p></div>
        <div class="card">
          <div class="card-h"><h2>Error signature</h2><span class="faint mono">normalised</span></div>
          <div class="code">${inc.error_signature ? highlightTokens(inc.error_signature) : "—"}</div>
        </div>
        <div class="card resolution">
          <div class="card-h"><h2>${icon(ICONS.check)}Resolution</h2></div>
          <p style="margin:0">${esc(inc.resolution || "No resolution recorded.")}</p>
        </div>
        <div><button class="btn" id="retriage">${icon(ICONS.wand)}Triage a new alert like this</button></div>
      </div>
      <aside class="stack">
        <div class="card"><div class="card-h"><h3>Tags</h3></div><div class="chips">${inc.tags.map((t) => `<span class="chip chip-tag">#${esc(t)}</span>`).join("")}</div></div>
        <div class="card">
          <div class="card-h"><h3>Same error elsewhere</h3><span class="faint mono">${sameSig.length}</span></div>
          ${sameSig.length ? sameSig.map((i) => `
            <div class="mini-row"><a class="mono" href="#/incidents/${esc(i.id)}">${esc(i.id)}</a><span class="mono faint">${esc(i.service)}</span></div>`).join("")
            : `<p class="muted" style="margin:0;font-size:13px">No other incident raised this exact error.</p>`}
        </div>
        <div class="card">
          <div class="card-h"><h3>${icon(ICONS.graph)}Shares a dependency with</h3></div>
          ${peersByDep.length ? peersByDep.map(({ dep, peers }) => `
            <div class="mini-row" style="flex-direction:column;gap:6px">
              <span class="chip chip-dep" style="align-self:flex-start">${esc(dep)}</span>
              <span class="mono faint" style="font-size:12px">${peers.length ? peers.map((p) => esc(p.service)).join(", ") : "no other service"}</span>
            </div>`).join("") : `<p class="muted" style="margin:0">No dependencies recorded.</p>`}
        </div>
      </aside>
    </div>`;

  $("#back").addEventListener("click", (e) => {
    if (history.length > 1) { e.preventDefault(); history.back(); }
  });
  $("#retriage").addEventListener("click", () => {
    Object.assign(triageState, {
      preset: null, service: inc.service, error: inc.error_signature || "", tags: inc.tags.join(", "), result: null,
    });
    location.hash = "#/triage";
  });
}

// ---------------------------------------------------------------------------
// services
// ---------------------------------------------------------------------------

async function renderServices() {
  view().innerHTML = `<div class="skeleton"></div>`;
  let services;
  try { services = await getServices(); } catch (e) {
    view().innerHTML = `<div class="card"><p class="err">${esc(e.message)}</p></div>`;
    return;
  }
  const depCount = {};
  services.forEach((s) => s.dependencies.forEach((d) => { depCount[d] = (depCount[d] || 0) + 1; }));
  const shared = Object.keys(depCount).filter((d) => depCount[d] > 1).sort((a, b) => depCount[b] - depCount[a] || a.localeCompare(b));
  if (!depCount[highlightedDep]) highlightedDep = shared[0] || null;

  view().innerHTML = `
    <div class="page-head">
      <div>
        <h1>Services and dependencies</h1>
        <p>The edges behind "who else could hit this?". Two services sharing a library is a fact in the graph that no incident write-up mentions.</p>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="toolbar" style="margin-bottom:6px">
        <span class="eyebrow" style="margin:0">Highlight a shared dependency</span>
        <div class="chips" id="dep-chips">
          ${shared.map((d) => `<button type="button" class="chip chip-btn ${d === highlightedDep ? "on" : ""}" data-dep="${esc(d)}">${esc(d)} · ${depCount[d]}</button>`).join("")}
        </div>
      </div>
      <div class="graph-wrap" id="graph"></div>
      <div class="legend">
        <span><i style="background:#fbbf24"></i>uses the highlighted dependency</span>
        <span><i style="background:transparent;border:1px dashed #38bdf8"></i>no incidents yet, but exposed</span>
        <span>number = past incidents</span>
      </div>
    </div>
    <div class="svc-cards" id="svc-cards"></div>`;

  const paint = () => {
    $("#graph").innerHTML = depGraph(services, highlightedDep);
    $("#dep-chips").querySelectorAll("[data-dep]").forEach((b) => b.classList.toggle("on", b.dataset.dep === highlightedDep));
    $("#svc-cards").innerHTML = services.map((s) => `
      <a class="card svc-card" href="#/incidents" data-svc="${esc(s.service)}" style="${s.dependencies.includes(highlightedDep) ? "border-color:var(--amber-line)" : ""}">
        <div class="card-h" style="margin:0"><span class="name">${esc(s.service)}</span>
          <span class="pill ${s.incidents ? "pill-muted" : "pill-warn"}">${s.incidents ? `${s.incidents} incident${s.incidents === 1 ? "" : "s"}` : "none yet"}</span></div>
        <div class="meta">${esc(s.team)} · ${esc(s.language)}</div>
        <div class="chips">${s.dependencies.map((d) => `<span class="chip ${d === highlightedDep ? "chip-dep" : ""}">${esc(d)}</span>`).join("")}</div>
      </a>`).join("");
  };

  view().addEventListener("click", (e) => {
    const d = e.target.closest("[data-dep]");
    if (d) { highlightedDep = d.dataset.dep; paint(); return; }
    const s = e.target.closest("[data-svc]");
    if (s) incidentFilter = { q: "", family: "All", severity: "", service: s.dataset.svc };
  });
  paint();
}

function depGraph(services, hl) {
  // Two columns: services on the left, dependencies on the right. Services are
  // ordered so that ones sharing a dependency sit together, which keeps the
  // edges from crossing more than they need to.
  const deps = [];
  services.forEach((s) => s.dependencies.forEach((d) => { if (!deps.includes(d)) deps.push(d); }));
  const firstDep = (s) => Math.min(...s.dependencies.map((d) => deps.indexOf(d)), 999);
  const svcs = [...services].sort((a, b) => firstDep(a) - firstDep(b) || a.service.localeCompare(b.service));
  deps.length = 0;
  svcs.forEach((s) => s.dependencies.forEach((d) => { if (!deps.includes(d)) deps.push(d); }));

  const rowS = 34, rowD = 28, top = 16;
  const H = Math.max(svcs.length * rowS, deps.length * rowD) + top * 2;
  const sy = (i) => top + (H - top * 2) * ((i + 0.5) / svcs.length);
  const dy = (i) => top + (H - top * 2) * ((i + 0.5) / deps.length);
  const SX = 178, DX = 262;

  const edges = [], hot = [];
  svcs.forEach((s, i) => s.dependencies.forEach((d) => {
    const line = `<line x1="${SX}" y1="${sy(i)}" x2="${DX}" y2="${dy(deps.indexOf(d))}"`;
    if (d === hl) hot.push(`${line} stroke="#fbbf24" stroke-width="1.8" ${s.incidents ? "" : 'stroke-dasharray="5 3"'}/>`);
    else edges.push(`${line} stroke="#2a3644" stroke-width="1"/>`);
  }));

  return `
    <svg viewBox="0 0 400 ${H}" role="img" aria-label="Services and the dependencies they share">
      ${edges.join("")}${hot.join("")}
      <g font-family="JetBrains Mono, monospace" font-size="11">
        ${svcs.map((s, i) => {
          const on = s.dependencies.includes(hl);
          const fresh = s.incidents === 0;
          const stroke = fresh ? "#38bdf8" : on ? "#b7791f" : "#2a3644";
          return `
            <rect x="8" y="${sy(i) - 13}" width="${SX - 8}" height="26" rx="6" fill="${fresh ? "#0b2536" : "#10161e"}" stroke="${stroke}" ${fresh ? 'stroke-dasharray="4 2"' : ""}/>
            <text x="16" y="${sy(i) + 4}" fill="${on || fresh ? "#e6edf3" : "#8b98a8"}">${esc(s.service)}</text>
            <text x="${SX - 10}" y="${sy(i) + 4}" text-anchor="end" fill="${fresh ? "#7dd3fc" : on ? "#fbbf24" : "#6b7888"}">${s.incidents}</text>`;
        }).join("")}
        ${deps.map((d, i) => {
          const on = d === hl;
          return `
            <rect x="${DX}" y="${dy(i) - 11}" width="132" height="22" rx="11" fill="${on ? "#1a1c3d" : "#10161e"}" stroke="${on ? "#fbbf24" : "#2a3644"}"/>
            <text x="${DX + 66}" y="${dy(i) + 4}" text-anchor="middle" fill="${on ? "#fde68a" : "#8b98a8"}">${esc(d)}</text>`;
        }).join("")}
      </g>
    </svg>`;
}

// ---------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------

async function route() {
  const hash = location.hash || "#/triage";
  const [, section, id] = hash.split("/");
  const current = section || "triage";
  document.querySelectorAll("[data-route]").forEach((a) => a.classList.toggle("active", a.dataset.route === current));

  // Replace the view node so listeners from the previous page don't pile up.
  const fresh = view().cloneNode(false);
  view().replaceWith(fresh);
  window.scrollTo(0, 0);

  if (current === "incidents" && id) await renderIncident(decodeURIComponent(id));
  else if (current === "incidents") await renderIncidents();
  else if (current === "services") await renderServices();
  else await renderTriage();
}

window.addEventListener("hashchange", route);
refreshStatus();
route();
