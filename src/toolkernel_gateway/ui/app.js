"use strict";

/* ToolKernel web UI. No build step: plain DOM + fetch + server-sent events. */

// ------------------------------------------------------------------ utilities

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

/** Tagged template: interpolations are escaped unless wrapped with raw(). Arrays are joined. */
function html(strings, ...vals) {
  let out = "";
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) out += piece(vals[i]);
  });
  return raw(out);
}
function piece(v) {
  if (v == null || v === false) return "";
  if (Array.isArray(v)) return v.map(piece).join("");
  if (typeof v === "object" && "__html" in v) return v.__html;
  return esc(v);
}
const raw = (s) => ({ __html: s });

function bytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}
function count(n) {
  n = Math.round(n || 0);
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}
const plural = (n, one, many = one + "s") => `${n.toLocaleString()} ${n === 1 ? one : many}`;
function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
function dayLabel(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}
const shortId = (id) => {
  const s = (id || "").replace(/^(conn-|rest-)/, "");
  return s.length > 12 ? s.slice(0, 8) : s;
};
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { toast("Couldn't copy. Select the text instead."); }
}

// ------------------------------------------------------------------ API

let KEY = (() => { try { return localStorage.getItem("tk-key") || ""; } catch { return ""; } })();

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (KEY) headers.Authorization = `Bearer ${KEY}`;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    const k = prompt("This gateway needs an API key. Paste it here (kept in this browser only):");
    if (k) {
      KEY = k.trim();
      try { localStorage.setItem("tk-key", KEY); } catch {}
      return api(path, opts);
    }
    throw new Error("unauthorized");
  }
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.error || `HTTP ${res.status}`);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

// ------------------------------------------------------------------ router

const main = $("#main");
const routes = [];
const route = (pattern, view, opts = {}) => routes.push({ re: new RegExp(`^${pattern}$`), view, ...opts });
let current = null;

async function render(fromEvent = false) {
  const [path, query] = (location.hash.slice(1) || "/").split("?");
  const params = new URLSearchParams(query || "");
  for (const r of routes) {
    const m = path.match(r.re);
    if (!m) continue;
    current = { ...r, args: m.slice(1).map(decodeURIComponent), params };
    markNav(r.nav);
    try {
      const out = await r.view(...current.args, params);
      if (out !== undefined) {
        main.innerHTML = out.__html;
        if (!fromEvent) main.firstElementChild?.classList.add("fade-in");
      }
      r.after?.(...current.args, params);
    } catch (e) {
      main.innerHTML = html`<h1>Something went wrong</h1><p class="sub">${e.message}</p>
        <p><button onclick="location.reload()">Try again</button></p>`.__html;
    }
    if (!fromEvent) window.scrollTo(0, 0);
    return;
  }
  main.innerHTML = html`<h1>Nothing here</h1><p class="sub">That page doesn't exist. <a href="#/">Go home</a>.</p>`.__html;
}
function markNav(nav) {
  $$("nav a").forEach((a) => (a.dataset.nav === nav ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
}
window.addEventListener("hashchange", () => render());

// ------------------------------------------------------------------ live updates

const refresh = debounce(() => {
  if (!current || !current.live || document.hidden) return;
  // Views with a partial re-render (e.g. Servers, which has a form) keep the rest of the page intact.
  if (current.liveRender) current.liveRender();
  else render(true);
}, 400);

function connectEvents() {
  const pulse = $("#pulse");
  const es = new EventSource("/api/events" + (KEY ? `?key=${encodeURIComponent(KEY)}` : ""));
  es.onopen = () => { pulse.classList.add("live"); $("#pulse-text").textContent = "live"; };
  es.onerror = () => { pulse.classList.remove("live"); $("#pulse-text").textContent = "reconnecting"; };
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.kind === "approval") updateBadge(true);
    if (current?.live?.includes(ev.kind)) refresh();
  };
}

async function updateBadge(notify = false) {
  try {
    const items = await api("/api/approvals");
    const n = items.filter((a) => a.status === "pending").length;
    const b = $("#approval-badge");
    b.hidden = n === 0;
    b.textContent = n;
    if (notify && n > 0 && current?.nav !== "approvals") toast(n === 1 ? "An agent is waiting for your approval" : `${n} requests are waiting for you`);
  } catch {}
}

// ------------------------------------------------------------------ shared pieces

function runTitle(r) {
  const input = r.input || {};
  switch (r.tool) {
    case "execute_code": {
      const line = (r.code || "").split("\n").map((l) => l.trim()).find((l) => l && !l.startsWith("#")) || "(empty)";
      return html`<span class="mono">${line}</span>`;
    }
    case "call_tool": return html`Called <span class="mono">${input.name}</span>`;
    case "search_tools": return html`Searched for “${input.query}”`;
    case "describe_tool": return html`Looked up <span class="mono">${input.name}</span>`;
    case "list_servers": return html`Listed servers`;
    case "read_output": return html`Read saved output <span class="mono">${input.handle}</span>`;
    case "reset_runtime": return html`Reset the runtime${input.wipe ? " and wiped data" : ""}`;
    default: return html`${r.tool}`;
  }
}

function runRow(r, { showSession = true } = {}) {
  const calls = r.tool_calls?.length || 0;
  const meta = [];
  if (showSession) meta.push(html`session ${shortId(r.session_id)}`);
  if (calls) meta.push(plural(calls, "tool call"));
  if (r.duration_ms) meta.push(`${r.duration_ms.toLocaleString()} ms`);
  if (r.restarted) meta.push(html`<span class="chip warn">after restart</span>`);
  const flow = r.bytes_from_tools
    ? html`${bytes(r.bytes_from_tools)}<span class="arrow">→</span><span class="saved">${bytes(r.bytes_returned)}</span>`
    : html`${bytes(r.bytes_returned)}`;
  return html`<a class="item" href="#/runs/${r.id}">
    <span class="status ${r.is_error ? "error" : "ok"}" title="${r.is_error ? "Ended with an error" : "Completed"}"></span>
    <span class="grow"><span class="title">${runTitle(r)}</span>
      <span class="meta">${meta.map((m, i) => html`${i ? " · " : ""}${m}`)}</span></span>
    <span class="right">${flow}<br><span class="faint">${clock(r.ts)}</span></span>
  </a>`;
}

function groupedRuns(runs, opts) {
  if (!runs.length) return html``;
  const groups = [];
  for (const r of runs) {
    const label = dayLabel(r.ts);
    if (!groups.length || groups.at(-1).label !== label) groups.push({ label, items: [] });
    groups.at(-1).items.push(r);
  }
  return html`${groups.map((g) => html`<div class="day">${g.label}</div><div class="list">${g.items.map((r) => runRow(r, opts))}</div>`)}`;
}

function connectSnippet(o) {
  const key = o.connect.has_key ? ' --header "Authorization: Bearer <your key>"' : "";
  return `claude mcp add --transport http toolkernel ${o.connect.mcp_url}${key}`;
}

function codeBlock(text, cls = "") {
  return html`<div class="copy-wrap"><pre class="${cls}">${text}</pre>
    <button class="quiet small copy" data-copy="${text}">Copy</button></div>`;
}

document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-copy]");
  if (c) copy(c.dataset.copy);
});

const ANN = { readOnlyHint: ["read-only", "good"], destructiveHint: ["destructive", "warn"], idempotentHint: ["idempotent", ""], openWorldHint: ["open world", ""] };
const annChips = (a = {}) => Object.entries(ANN).filter(([k]) => a[k]).map(([, [l, c]]) => html` <span class="chip ${c}">${l}</span>`);

// ------------------------------------------------------------------ Home

route("/", async () => {
  const o = await api("/api/overview");
  const cpt = o.chars_per_token || 4;
  const t = o.today, all = o.all;
  const keptToday = Math.max(0, t.bytes_from_tools - t.bytes_returned);
  const keptAll = Math.max(0, all.bytes_from_tools - all.bytes_returned);

  let summary;
  if (!all.runs) {
    summary = html`Nothing has run yet. When an agent connects, you'll see what it did here, and how much it <em>didn't</em> have to read.`;
  } else if (t.runs) {
    summary = html`Today your agents ran <strong>${plural(t.runs, "step")}</strong> across <strong>${plural(t.sessions, "session")}</strong>.
      ${t.bytes_from_tools ? html`Tools sent back <strong>${bytes(t.bytes_from_tools)}</strong>; the models read <strong>${bytes(t.bytes_returned)}</strong>.
      <span class="good">That's about ${count(keptToday / cpt)} tokens they didn't have to carry.</span>` : html`No tool data has moved yet today.`}`;
  } else {
    summary = html`A quiet day so far. In total, ToolKernel has kept <strong>${bytes(keptAll)}</strong> of tool data out of your models' context.`;
  }

  const days = buildDays(o.daily, 14);
  const maxKept = Math.max(1, ...days.map((d) => d.kept));
  const noServers = o.servers.total === 0;

  return html`<div>
    <section class="hero">
      <p class="eyebrow">ToolKernel</p>
      <p class="hero-lede">ToolKernel sits between your AI agent and your MCP servers. The agent writes a little Python
        instead of calling tools one by one, so large results stay in the runtime and only what matters
        reaches the model. <a href="#/guide">See how it works →</a></p>
      <div class="actions-grid">
        <a class="action primary" href="#/servers">
          <span class="action-title">Connect an MCP server</span>
          <span class="action-sub">${o.servers.total ? `${o.servers.ready} of ${o.servers.total} ready · add more` : "Add your first one"}</span></a>
        <a class="action" href="#/settings">
          <span class="action-title">Connect an agent</span>
          <span class="action-sub">Claude Code, Cursor, Desktop or REST</span></a>
        <a class="action" href="#/playground">
          <span class="action-title">Try the playground</span>
          <span class="action-sub">Run code exactly as an agent would</span></a>
        <a class="action" href="#/tools">
          <span class="action-title">Browse tools</span>
          <span class="action-sub">${plural(o.servers.tools, "tool")} as Python functions</span></a>
      </div>
    </section>

    ${o.pending_approvals ? html`<p style="margin-top:32px" class="note"><strong>${o.pending_approvals === 1 ? "An agent is" : `${o.pending_approvals} agents are`} waiting for you.</strong>
      A tool call needs your approval. <a href="#/approvals">Review now</a></p>` : ""}

    <section class="section">
      <div class="section-head"><h2>Right now</h2></div>
      <p class="summary">${summary}</p>
      <div class="stats">
        <a class="stat" href="#/servers"><div class="n">${o.servers.ready}<span class="faint" style="font-size:18px"> / ${o.servers.total}</span></div><div class="l">servers ready →</div></a>
        <a class="stat" href="#/tools"><div class="n">${o.servers.tools}</div><div class="l">tools available →</div></a>
        <a class="stat" href="#/runs"><div class="n">${o.sessions_active}</div><div class="l">${o.sessions_active === 1 ? "runtime" : "runtimes"} running →</div></a>
        <a class="stat" href="#/runs"><div class="n saved">${count(keptAll / cpt)}</div><div class="l">tokens kept out of context →</div></a>
      </div>
    </section>

    ${noServers ? html`<div class="section empty"><h3>Let's connect your first server</h3>
        Add an MCP server and it becomes a Python package your agents can import.
        <p style="margin:14px 0 0"><a class="btn" href="#/servers">Add a server</a></p></div>` : ""}

    <section class="section">
      <div class="section-head"><h2>Kept out of context</h2><span class="muted small">last 14 days</span></div>
      <p class="sub" style="margin:-4px 0 18px">Tool results that never had to pass through the model. They stayed in the runtime as
        variables, files or saved output, and the model got a summary instead. Less in the context means lower cost,
        faster replies, and more room to think. Hover a day for details.</p>
      <div class="chart">
        <div class="chart-max faint small">${bytes(maxKept)}</div>
        <div class="bars" role="list" aria-label="Data kept out of model context per day">
          ${days.map((d, i) => html`<div class="bar-col" role="listitem" tabindex="0"
              data-label="${d.label}" data-kept="${d.kept}" data-from="${d.from}" data-ret="${d.ret}" data-runs="${d.runs}"
              aria-label="${d.label}: ${bytes(d.kept)} kept out of context, ${d.runs} runs">
            <div class="bar ${i === days.length - 1 ? "today" : ""} ${d.kept ? "" : "zero"}" style="height:${d.kept ? Math.max(4, (d.kept / maxKept) * 100) : 0}%"></div>
          </div>`)}
          <div class="chart-tip" id="chart-tip" hidden></div>
        </div>
        <div class="bar-labels"><span>${days[0].label}</span><span>today</span></div>
      </div>
      ${all.runs ? "" : html`<p class="small muted">Nothing yet. This fills in as agents use tools.</p>`}
    </section>

    <section class="section">
      <div class="section-head"><h2>Recently</h2>${o.recent.length ? html`<a href="#/runs">All runs →</a>` : ""}</div>
      ${o.recent.length ? html`<div class="list">${o.recent.map((r) => runRow(r))}</div>` : html`<div class="empty">
        <h3>Connect an agent</h3>
        Point any MCP client at <span class="mono">${o.connect.mcp_url}</span>. For Claude Code:
        <div style="margin-top:12px">${codeBlock(connectSnippet(o))}</div>
        <p class="small" style="margin:12px 0 0">Or try things yourself in the <a href="#/playground">Playground</a>.</p></div>`}
    </section>
  </div>`;
}, { nav: "home", live: ["run", "servers", "session", "approval"], after: homeAfter });

function homeAfter() {
  // Chart tooltip on hover and keyboard focus.
  const tip = $("#chart-tip");
  if (!tip) return;
  const show = (col) => {
    const d = col.dataset;
    const kept = +d.kept, from = +d.from, ret = +d.ret, runs = +d.runs;
    tip.innerHTML = html`<strong>${d.label}</strong>
      ${runs ? html`<div class="tip-big saved">${bytes(kept)} kept out</div>
        <div>Tools returned ${bytes(from)}</div>
        <div>Model read ${bytes(ret)}${from ? ` (${((100 * ret) / from).toFixed(ret / from < 0.01 ? 2 : 1)}%)` : ""}</div>
        <div class="faint">${plural(runs, "run")} · ≈${count(kept / 4)} tokens saved</div>`
        : html`<div class="faint">No activity</div>`}`.__html;
    tip.hidden = false;
    // Sit beside the hovered bar, on whichever side has room, so it never covers the bar or leaves the chart.
    const bars = col.parentElement.getBoundingClientRect(), c = col.getBoundingClientRect();
    const left = c.left - bars.left, gap = 10;
    if (left + c.width / 2 > bars.width / 2) tip.style.left = `${Math.max(0, left - gap - tip.offsetWidth)}px`;
    else tip.style.left = `${Math.min(bars.width - tip.offsetWidth, left + c.width + gap)}px`;
  };
  $$(".bar-col").forEach((col) => {
    col.onmouseenter = () => show(col);
    col.onfocus = () => show(col);
    col.onmouseleave = col.onblur = () => (tip.hidden = true);
  });
}

function buildDays(rows, n) {
  const map = Object.fromEntries(rows.map((r) => [r.day, r]));
  const out = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(); d.setDate(d.getDate() - i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const r = map[key] || {};
    const from = r.bytes_from_tools || 0, ret = r.bytes_returned || 0;
    out.push({ label: i === 0 ? "Today" : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }),
      runs: r.runs || 0, from, ret, kept: Math.max(0, from - ret) });
  }
  return out;
}

// ------------------------------------------------------------------ Runs

route("/runs", async (params) => {
  const session = params.get("session");
  const [runs, sessions] = await Promise.all([
    api(`/api/runs?limit=150${session ? `&session=${encodeURIComponent(session)}` : ""}`),
    api("/api/sessions"),
  ]);
  const s = session ? sessions.find((x) => x.id === session) : null;
  return html`<div>
    <h1>${session ? html`Session <span class="mono" style="font-size:26px">${shortId(session)}</span>` : "Runs"}</h1>
    <p class="sub">${session
      ? html`${s?.client || "Unknown client"} · started ${ago(s?.created)} ${s?.live && s?.runtime ? html`· <span class="saved">runtime running</span>` : html`· runtime stopped`}
          · <a href="#/runs">all runs</a>`
      : "Everything your agents asked the gateway to do, newest first. Open a run to compare what the model saw with what stayed in the sandbox."}</p>
    ${s?.state ? html`<p class="mono small muted" style="margin-top:14px">${s.state}</p>` : ""}
    ${s?.files?.length ? html`<p class="small muted">Saved files: <span class="mono">${s.files.join(", ")}</span></p>` : ""}
    ${!session && sessions.length > 1 ? html`<div class="row-gap" style="margin-top:20px"><span class="small muted">Sessions:</span>
      ${sessions.slice(0, 8).map((x) => html`<a class="chip" href="#/runs?session=${encodeURIComponent(x.id)}" title="${x.client || ""}">${shortId(x.id)}${x.runtime ? " ●" : ""}</a>`)}</div>` : ""}
    <div class="section" style="margin-top:28px">${runs.length ? groupedRuns(runs, { showSession: !session })
      : html`<div class="empty"><h3>No runs yet</h3>When an agent calls the gateway, each step shows up here.</div>`}</div>
  </div>`;
}, { nav: "runs", live: ["run", "session"] });

route("/runs/(\\d+)", async (id) => {
  const r = await api(`/api/runs/${id}`);
  const calls = r.tool_calls || [];
  const pct = r.bytes_from_tools ? (100 * r.bytes_returned) / r.bytes_from_tools : null;
  const label = { execute_code: "Ran code", call_tool: "Direct tool call", search_tools: "Searched tools", describe_tool: "Looked up a tool",
    list_servers: "Listed servers", read_output: "Read saved output", reset_runtime: "Reset the runtime" }[r.tool] || r.tool;
  const showCode = r.tool === "execute_code";
  return html`<div>
    <p class="small"><a href="#/runs">← Runs</a></p>
    <h1 style="margin-top:8px">${label}</h1>
    <p class="sub">${clock(r.ts)} · ${dayLabel(r.ts)} · session <a href="#/runs?session=${encodeURIComponent(r.session_id)}" class="mono">${shortId(r.session_id)}</a>
      ${r.duration_ms ? ` · ${r.duration_ms.toLocaleString()} ms` : ""}</p>
    ${pct !== null ? html`<p class="lede" style="font-size:19px">Tools sent back <strong>${bytes(r.bytes_from_tools)}</strong>.
      The model saw <strong class="saved">${bytes(r.bytes_returned)}</strong>${pct < 100 ? html`, ${pct < 0.1 ? "less than 0.1" : pct.toFixed(pct < 10 ? 1 : 0)}% of it` : ""}.</p>` : ""}
    ${r.restarted ? html`<p class="note" style="margin-top:20px">The runtime had restarted before this run. The model was told what was lost and what was kept.</p>` : ""}

    ${showCode ? html`<div class="section" style="margin-top:32px"><div class="pane-label"><span>Code</span></div>${codeBlock(r.code, "tall")}</div>`
      : Object.keys(r.input || {}).length ? html`<div class="section" style="margin-top:32px"><div class="pane-label"><span>Input</span></div>
        <pre>${JSON.stringify(r.input, null, 2)}</pre></div>` : ""}

    <div class="split">
      <div>
        <div class="pane-label"><span>What the model saw</span><span>${bytes(r.bytes_returned)}</span></div>
        <pre class="tall">${r.output}</pre>
      </div>
      <div>
        <div class="pane-label"><span>What stayed in the sandbox</span><span>${r.bytes_from_tools ? bytes(r.bytes_from_tools) : ""}</span></div>
        ${calls.length ? calls.map((c) => html`<details class="call">
          <summary><span class="status ${c.ok === false ? "error" : "ok"}"></span><span class="mono grow" style="flex:1">${c.tool}</span>
            <span class="small muted">${c.bytes != null ? bytes(c.bytes) : ""}${c.ms != null ? ` · ${c.ms} ms` : ""}</span></summary>
          <div class="body">
            ${c.approval ? html`<p class="small"><span class="chip ${c.approval === "approved" ? "good" : "bad"}">${c.approval}</span></p>` : ""}
            ${c.error ? html`<p class="note bad small">${c.error}</p>` : ""}
            ${c.warning ? html`<p class="note small">${c.warning}</p>` : ""}
            <div class="pane-label" style="margin-top:8px"><span>Arguments</span></div>
            <pre>${typeof c.args === "string" ? c.args : JSON.stringify(c.args, null, 2)}</pre>
            ${c.preview ? html`<div class="pane-label" style="margin-top:12px"><span>Result (start)</span></div><pre>${c.preview}</pre>` : ""}
          </div></details>`) : html`<p class="muted small" style="margin:0 0 12px">No tool calls in this run.</p>`}
        ${r.sandbox && r.spill ? html`<div class="pane-label" style="margin-top:16px"><span>Full output before the guard</span>
          <span class="mono">${r.spill}</span></div><pre class="tall">${r.sandbox}</pre>` : ""}
      </div>
    </div>
    ${r.state ? html`<div class="section" style="margin-top:28px"><div class="pane-label"><span>Variables in memory afterwards</span></div>
      <p class="mono small" style="margin:0">${r.state.replace(/^\[state\]\s*/, "")}</p></div>` : ""}
  </div>`;
}, { nav: "runs" });

// ------------------------------------------------------------------ Servers

const serversState = { openLog: null };

async function serversList() {
  const servers = await api("/api/servers");
  if (!servers.length) return html`<div class="empty"><h3>No servers yet</h3>Add one below. It will be ready to use within a few seconds.</div>`;
  const pending = await Promise.all(servers.map((s) => (serversState.openLog === s.name ? api(`/api/servers/${encodeURIComponent(s.name)}/log`) : null)));
  return html`<div class="list">${servers.map((s, i) => {
    const what = s.status === "ready" ? html`${plural(s.tools, "tool")} · ${s.transport}${s.connected_at ? ` · connected ${ago(s.connected_at)}` : ""}`
      : s.status === "error" ? html`<span style="color:var(--rose)">Couldn't connect. ${s.error || ""}</span> <span class="faint">Retrying on its own.</span>`
      : s.status === "disabled" ? html`Turned off` : html`Connecting…`;
    const n = encodeURIComponent(s.name);
    return html`<div class="item" style="flex-wrap:wrap">
      <span class="status ${s.status}" title="${s.status}"></span>
      <span class="grow"><span class="title"><strong style="font-weight:600">${s.name}</strong>
        ${s.py && s.py !== s.name ? html` <span class="mono faint small">servers.${s.py}</span>` : ""}
        ${s.warnings?.length ? html` <span class="chip warn" title="${s.warnings.join("\n")}">${plural(s.warnings.length, "note")}</span>` : ""}</span>
        <span class="meta">${what}</span></span>
      <span class="row-gap">
        ${s.status === "ready" ? html`<a class="btn small" href="#/tools?server=${encodeURIComponent(s.py || s.name)}">Tools</a>` : ""}
        <button class="quiet small" data-act="log" data-name="${s.name}">${serversState.openLog === s.name ? "Hide log" : "Log"}</button>
        ${s.status !== "disabled" ? html`<button class="quiet small" data-act="reconnect" data-name="${s.name}">Reconnect</button>` : ""}
        <button class="quiet small" data-act="${s.status === "disabled" ? "enable" : "disable"}" data-name="${s.name}">${s.status === "disabled" ? "Turn on" : "Turn off"}</button>
        <button class="quiet small danger" data-act="remove" data-name="${s.name}">Remove</button>
      </span>
      ${pending[i] !== null ? html`<pre class="tall" style="width:100%;margin-top:10px">${pending[i] || "Nothing logged yet."}</pre>` : ""}
    </div>`;
  })}</div>`;
}

route("/servers", async () => {
  const settings = await api("/api/settings");
  return html`<div>
    <h1>Servers</h1>
    <p class="sub">Each MCP server becomes a Python package your agents can import. Edits to
      <span class="mono">${settings.mcp_json.split("/").slice(-1)[0]}</span> are picked up automatically.</p>
    <div class="section" style="margin-top:28px" id="servers-list">${await serversList()}</div>

    <div class="section">
      <h2>Add a server</h2>
      <p class="sub">Paste a server's config, or a whole <span class="mono">mcpServers</span> block from Claude Desktop or Claude Code.
        Keep keys out of here: write <span class="mono">\${secret:name}</span> or <span class="mono">\${env:NAME}</span> instead.</p>
      <form id="add-form" style="margin-top:18px">
        <div class="field"><label for="srv-name">Name</label><input id="srv-name" placeholder="github" autocomplete="off"></div>
        <div class="field"><label for="srv-config">Config</label>
          <textarea id="srv-config" rows="6" placeholder='{ "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": { "GITHUB_TOKEN": "\${secret:github_token}" } }'></textarea></div>
        <div class="row-gap"><button class="primary" type="submit">Add server</button><span class="small muted" id="add-msg"></span></div>
      </form>
    </div>
  </div>`;
}, {
  nav: "servers", live: ["servers"],
  after() {
    const list = $("#servers-list");
    current.liveRender = async () => { list.innerHTML = (await serversList()).__html; };
    list.onclick = async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const { act, name } = b.dataset;
      const n = encodeURIComponent(name);
      if (act === "log") { serversState.openLog = serversState.openLog === name ? null : name; }
      else if (act === "remove") {
        if (!confirm(`Remove ${name}? Agents will lose access to its tools. You can add it again later.`)) return;
        await api(`/api/servers/${n}`, { method: "DELETE" }); toast(`${name} removed`);
      } else { await post(`/api/servers/${n}/${act}`); toast(act === "reconnect" ? `Reconnecting ${name}…` : `${name} turned ${act === "enable" ? "on" : "off"}`); }
      list.innerHTML = (await serversList()).__html;
    };
    $("#add-form").onsubmit = async (e) => {
      e.preventDefault();
      const msg = $("#add-msg");
      let cfg;
      try { cfg = JSON.parse($("#srv-config").value || "{}"); }
      catch (err) { msg.textContent = "That config isn't valid JSON yet. " + err.message; return; }
      const name = $("#srv-name").value.trim();
      const body = cfg.mcpServers ? cfg : name ? { name, config: cfg } : null;
      if (!body) { msg.textContent = "Give the server a name."; $("#srv-name").focus(); return; }
      try {
        await post("/api/servers", body);
        msg.textContent = "";
        $("#srv-name").value = ""; $("#srv-config").value = "";
        toast("Added. Connecting now…");
        list.innerHTML = (await serversList()).__html;
      } catch (err) { msg.textContent = err.message; }
    };
  },
});

// ------------------------------------------------------------------ Tools

route("/tools", async (params) => {
  const q = params.get("q") || "";
  const only = params.get("server");
  const data = await api("/api/tools");
  const total = data.reduce((n, s) => n + s.tools.length, 0);
  return html`<div>
    <h1>Tools</h1>
    <p class="sub">${plural(total, "tool")} from ${plural(data.length, "server")}, exactly as your agents see them: typed Python functions.</p>
    <div class="search" style="margin-top:24px"><input id="tool-q" type="search" placeholder="Search by what a tool does…" value="${q}" aria-label="Search tools"></div>
    <div id="tool-results" class="section" style="margin-top:24px">${await toolResults(q, only, data)}</div>
  </div>`;
}, {
  nav: "tools", live: ["servers"],
  after() {
    const input = $("#tool-q");
    const out = $("#tool-results");
    input.oninput = debounce(async () => {
      out.innerHTML = (await toolResults(input.value.trim())).__html;
      history.replaceState(null, "", `#/tools${input.value.trim() ? `?q=${encodeURIComponent(input.value.trim())}` : ""}`);
    }, 180);
    if (!input.value) input.focus({ preventScroll: true });
  },
});

async function toolResults(q, only, data) {
  if (q) {
    const hits = await api(`/api/tools?q=${encodeURIComponent(q)}`);
    if (!hits.length) return html`<p class="muted">Nothing matches “${q}”. Try different words.</p>`;
    return html`<div class="list">${hits.map((t) => toolRow(t.server, t))}</div>`;
  }
  data = data || (await api("/api/tools"));
  if (only) data = data.filter((s) => s.server === only);
  if (!data.length) return html`<div class="empty"><h3>No tools yet</h3>Tools appear once a server is connected. <a href="#/servers">Add a server</a>.</div>`;
  return html`${data.map((s) => html`<div class="section" style="margin-top:0;margin-bottom:36px">
    <div class="section-head"><h2>${s.server}</h2><span class="muted small mono">from servers.${s.server} import …</span></div>
    ${s.description && s.description !== s.server ? html`<p class="sub" style="margin:-6px 0 14px">${s.description}</p>` : ""}
    <div class="list">${s.tools.map((t) => toolRow(s.server, t))}</div></div>`)}
    ${only ? html`<p><a href="#/tools">Show all servers</a></p>` : ""}`;
}

function toolRow(server, t) {
  return html`<a class="item" href="#/tools/${encodeURIComponent(server)}/${encodeURIComponent(t.tool)}">
    <span class="grow"><span class="title mono">${server}.${t.signature}</span>
      <span class="meta">${t.summary || "No description."}${annChips(t.annotations)}</span></span></a>`;
}

route("/tools/([^/]+)/([^/]+)", async (server, tool) => {
  const d = await api(`/api/tools/${encodeURIComponent(server)}/${encodeURIComponent(tool)}`);
  const m = d.meta;
  const req = Object.entries(m.params).filter(([, p]) => p.required).map(([k]) => `${k}=...`).join(", ");
  const usage = `from servers.${server} import ${tool}\n\nresult = ${tool}(${req})`;
  const tabs = [["python", "Python"], ["input", "Input schema"], ...(d.output_schema ? [["output", "Output schema"]] : [])];
  return html`<div>
    <p class="small"><a href="#/tools">← Tools</a></p>
    <h1 style="margin-top:8px" class="mono-title"><span style="font-family:var(--mono);font-size:26px">${server}.${tool}</span></h1>
    <p class="sub">${m.summary || "No description."}${annChips(m.annotations)}</p>
    <div class="section" style="margin-top:28px"><div class="pane-label"><span>Use it</span></div>${codeBlock(usage)}</div>
    <div class="tabs" role="tablist">${tabs.map(([k, l], i) => html`<button role="tab" data-tab="${k}" aria-selected="${i === 0}">${l}</button>`)}</div>
    <div data-panel="python"><pre class="tall">${d.source}</pre>
      ${Object.values(d.types || {}).filter(Boolean).length ? html`<div class="pane-label" style="margin-top:16px"><span>Types</span></div>
        <pre>${Object.values(d.types).filter(Boolean).join("\n\n")}</pre>` : ""}</div>
    <div data-panel="input" hidden><pre class="tall">${JSON.stringify(d.input_schema, null, 2)}</pre></div>
    ${d.output_schema ? html`<div data-panel="output" hidden><pre class="tall">${JSON.stringify(d.output_schema, null, 2)}</pre></div>` : ""}
  </div>`;
}, {
  nav: "tools",
  after() {
    $$(".tabs button").forEach((b) => (b.onclick = () => {
      $$(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
      $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== b.dataset.tab));
    }));
  },
});

// ------------------------------------------------------------------ Playground

const play = { cells: [], code: null };
const SAMPLE = `# This runtime is yours. Variables persist between runs.
print(open("README.md").read()[:800])`;

route("/playground", async () => html`<div>
    <h1>Playground</h1>
    <p class="sub">Run code in a runtime of your own. The output below is exactly what an agent would see, guard and all.</p>
    <div class="section" style="margin-top:24px">
      <textarea id="code" class="editor" spellcheck="false" aria-label="Python code" rows="9">${play.code ?? SAMPLE}</textarea>
      <div class="row-gap" style="margin-top:10px">
        <button class="primary" id="run">Run</button><span class="small faint"><kbd>⌘</kbd> <kbd>↵</kbd></span>
        <span style="flex:1"></span>
        <button class="quiet" id="reset" title="Clear variables; files in saved/ are kept">Reset runtime</button>
      </div>
    </div>
    <div id="cells" class="section" style="margin-top:28px">${cellsHtml()}</div>
  </div>`, {
  nav: "playground",
  after() {
    const ta = $("#code");
    const run = async () => {
      const code = ta.value;
      if (!code.trim()) return;
      const btn = $("#run");
      btn.disabled = true; btn.textContent = "Running…";
      try {
        const r = await post("/api/playground", { code });
        play.cells.unshift({ code, text: r.text, error: r.is_error, ts: Date.now() / 1000 });
      } catch (e) {
        play.cells.unshift({ code, text: e.message, error: true, ts: Date.now() / 1000 });
      }
      play.cells = play.cells.slice(0, 12);
      if (!$("#cells")) { toast("Your playground run finished"); return; }  // you navigated away meanwhile
      $("#cells").innerHTML = cellsHtml().__html;
      btn.disabled = false; btn.textContent = "Run";
    };
    ta.oninput = () => (play.code = ta.value);
    ta.onkeydown = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); }
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        const { selectionStart: s, selectionEnd: en } = ta;
        ta.setRangeText("    ", s, en, "end");
        play.code = ta.value;
      }
    };
    $("#run").onclick = run;
    $("#reset").onclick = async () => {
      const r = await post("/api/playground", { tool: "reset_runtime", arguments: {} });
      play.cells.unshift({ code: "# reset_runtime()", text: r.text, error: false, ts: Date.now() / 1000 });
      $("#cells").innerHTML = cellsHtml().__html;
    };
  },
});

function cellsHtml() {
  if (!play.cells.length) return html`<p class="muted small">Output appears here. Try <span class="mono">from servers.&lt;name&gt; import &lt;tool&gt;</span>; the Tools page lists what's available.</p>`;
  return html`${play.cells.map((c, i) => html`<div style="margin-bottom:22px" class="${i === 0 ? "fade-in" : ""}">
    <div class="pane-label"><span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.code.split("\n").find((l) => l.trim() && !l.trim().startsWith("#")) || c.code.split("\n")[0]}</span>
      <span>${clock(c.ts)}</span></div>
    <pre class="tall" style="${c.error ? "border-color:color-mix(in srgb, var(--rose) 35%, var(--line))" : ""}">${c.text}</pre></div>`)}`;
}

// ------------------------------------------------------------------ Approvals

route("/approvals", async () => {
  const items = await api("/api/approvals");
  const pending = items.filter((a) => a.status === "pending");
  const past = items.filter((a) => a.status !== "pending");
  return html`<div>
    <h1>Approvals</h1>
    <p class="sub">Some tool calls wait for a person before they run. Your policies decide which ones.</p>
    <div class="section" style="margin-top:28px">
      ${pending.length ? pending.map(askCard) : html`<div class="empty"><h3>Nothing is waiting for you</h3>
        To ask before risky calls, add a rule to <span class="mono">toolkernel.yaml</span>:
        <pre style="margin-top:12px">policies:
  rules:
    - match: "*"
      when: { annotation: destructiveHint }
      action: approve</pre></div>`}
    </div>
    ${past.length ? html`<div class="section"><h2 style="margin-bottom:14px">Earlier</h2><div class="list">${past.map((a) => html`<div class="item">
      <span class="status ${a.status === "approved" ? "ok" : "error"}"></span>
      <span class="grow"><span class="title"><span class="mono">${a.server}.${a.tool}</span></span>
        <span class="meta">${{ approved: "Allowed", declined: "Declined", expired: "Expired, no one answered" }[a.status] || a.status}
          · session ${shortId(a.session_id)}</span></span>
      <span class="right">${ago(a.decided || a.created)}</span></div>`)}</div></div>` : ""}
  </div>`;
}, {
  nav: "approvals", live: ["approval"],
  after() {
    main.onclick = async (e) => {
      const b = e.target.closest("button[data-decide]");
      if (!b) return;
      b.disabled = true;
      const approve = b.dataset.decide === "yes";
      try {
        await post(`/api/approvals/${b.dataset.id}`, { approve });
        toast(approve ? "Allowed. The agent carries on." : "Declined. The agent will be told.");
      } catch { toast("That request already ended."); }
      updateBadge();
      render(true);
    };
  },
});

function askCard(a) {
  const verb = a.tool.replace(/[_-]+/g, " ");
  const args = Object.entries(a.arguments || {});
  return html`<div class="ask fade-in">
    <h3>An agent wants to <strong style="font-weight:600">${verb}</strong> on <strong style="font-weight:600">${a.server}</strong>.</h3>
    <p class="small muted" style="margin:4px 0 14px">${a.client || "An agent"} · session ${shortId(a.session_id)} · asked ${ago(a.created)}${annChips(a.annotations)}</p>
    ${args.length ? html`<dl class="kv">${args.map(([k, v]) => html`<dt>${k}</dt><dd>${typeof v === "string" ? v : JSON.stringify(v)}</dd>`)}</dl>`
      : html`<p class="muted small">No arguments.</p>`}
    <div class="actions">
      <button class="primary" data-decide="yes" data-id="${a.id}">Allow once</button>
      <button data-decide="no" data-id="${a.id}">Decline</button>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ Guide

const EX = {
  fetch: `from datetime import datetime, timedelta, timezone
from servers.github import list_issues

issues = list_issues(owner="acme", repo="api", state="open", labels=["bug"], per_page=100)
cutoff = datetime(2026, 11, 11, tzinfo=timezone.utc) - timedelta(days=30)
stale = [i for i in issues
         if datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00")) < cutoff]
len(issues), len(stale)`,
  post: `from servers.slack import post_message

lines = [f"• #{i['number']} {i['title']}" for i in stale]
post_message(channel="#eng", text="Stale bugs (>30 days):\\n" + "\\n".join(lines))
print("posted", len(lines))`,
};

function step(n, title, body, call, reply, note) {
  return html`<div class="step">
    <div class="step-n">${n}</div>
    <div class="step-body">
      <h3>${title}</h3>
      ${body ? html`<p>${body}</p>` : ""}
      <div class="exchange">
        <div><div class="pane-label"><span>The agent calls</span></div><pre>${call}</pre></div>
        <div><div class="pane-label"><span>It gets back</span></div><pre class="reply">${reply}</pre></div>
      </div>
      ${note ? html`<p class="small muted step-note">${note}</p>` : ""}
    </div>
  </div>`;
}

route("/guide", async () => html`<div class="guide">
  <aside class="toc" aria-label="On this page">
    <p class="eyebrow">On this page</p>
    <a href="#/guide" data-jump="idea">The idea</a>
    <a href="#/guide" data-jump="shape">How it fits together</a>
    <a href="#/guide" data-jump="example">A worked example</a>
    <a href="#/guide" data-jump="mistakes">When things go wrong</a>
    <a href="#/guide" data-jump="tools">What the agent sees</a>
    <a href="#/guide" data-jump="helpers">Runtime helpers</a>
    <a href="#/guide" data-jump="safety">Safety</a>
    <a href="#/guide" data-jump="start">Get started</a>
  </aside>

  <article class="prose">
    <p class="eyebrow">Guide</p>
    <h1>How ToolKernel works</h1>
    <p class="hero-lede" style="margin-top:14px">Your agent gets a Python notebook that can call every tool you've connected.
      It does the heavy lifting in code and reads only the answer.</p>

    <section id="idea">
      <h2>The idea</h2>
      <p>When an agent uses MCP servers directly, two things fill up its context window:</p>
      <ul>
        <li><strong>Tool definitions.</strong> Every tool's schema is loaded up front. Ten servers can mean tens of thousands of tokens before the first question.</li>
        <li><strong>Intermediate results.</strong> To get data from one tool to the next, it has to pass through the model. A 2 MB export gets read in full, even if the agent only needed a count.</li>
      </ul>
      <p>ToolKernel changes the shape of the conversation. The agent sees a handful of small tools. One of them,
        <code>execute_code</code>, runs Python in a runtime where every connected server is an importable package.
        The agent fetches, filters and joins data in code, and only the printed result comes back.</p>
    </section>

    <section id="shape">
      <h2>How it fits together</h2>
      <div class="flow" role="img" aria-label="Agent connects to ToolKernel, which runs a Python runtime per session and connects to your MCP servers">
        <div class="flow-box"><strong>Your agent</strong><span>Claude Code, Cursor, your own code</span></div>
        <div class="flow-arrow" aria-hidden="true"><span>7 small tools</span><i></i></div>
        <div class="flow-box main"><strong>ToolKernel</strong>
          <span>Python runtime per session</span><span>Output guard</span><span>Policies &amp; approvals</span></div>
        <div class="flow-arrow" aria-hidden="true"><span>MCP</span><i></i></div>
        <div class="flow-box"><strong>Your MCP servers</strong><span>GitHub, Slack, CRM, databases…</span></div>
      </div>
      <ol>
        <li><strong>You connect servers</strong> on the <a href="#/servers">Servers</a> page (or in <code>mcp.json</code>).
          Each one becomes a typed Python package: <code>servers.github</code>, <code>servers.slack</code>…</li>
        <li><strong>You connect an agent</strong> to one URL. It never sees the individual tools until it looks for them.</li>
        <li><strong>The agent writes code.</strong> It runs in a persistent runtime: variables stay in memory between calls, like a notebook.</li>
        <li><strong>The output guard</strong> keeps replies small. Big results are summarised; anything cut is saved and can be paged.</li>
      </ol>
    </section>

    <section id="example">
      <h2>A worked example</h2>
      <p>Someone asks their agent:</p>
      <blockquote>Which open bugs in acme/api haven't been updated in 30 days? Post a summary to #eng.</blockquote>
      <p>Here is the whole exchange, using the demo servers that come with ToolKernel. Every reply below is real output;
        the one long reply is shortened where marked.</p>

      ${step(1, "Find the right tool",
        "The agent doesn't know what's available, so it searches. It gets one line per tool, not a full schema.",
        `search_tools("github issues")`,
        `github.list_issues(owner, repo, state?, labels?, per_page?): List issues in a GitHub repository.
github.create_issue(owner, repo, title, body?, labels?): Create a new issue.
github.get_issue(owner, repo, number): Get one issue by number, including its full body.
github.search_code(query, limit?): Search code across repositories.
Use: from servers.<server> import <tool>. Full signature: describe_tool("server.tool").`)}

      ${step(2, "Fetch and filter in code",
        "The agent writes a few lines of Python. The issues come back into the runtime, get filtered there, and only two numbers go to the model.",
        `execute_code("""\n${EX.fetch}\n""")`,
        `[result]
(24, 13)
[state] stale: list[13] · cutoff: datetime · issues: list[24]
[meta] exec 99ms · tool calls 1 (github.list_issues ×1) · data via tools 15.0 KB · returned 183 B`,
        html`15 KB came back from GitHub; the model read 183 bytes. The <code>[state]</code> line reminds it that
          <code>issues</code> and <code>stale</code> are still in memory.`)}

      ${step(3, "A careless print is caught",
        "Suppose the agent prints everything. ToolKernel trims the reply to its budget, keeps the start and the end, and saves the rest.",
        `execute_code("print(issues)")`,
        `[stdout]
[{'number': 2, 'title': 'Fix login endpoint', 'state': 'open', 'labels': ['bug', 'feature'], …
  (more of the start, shortened here)
… [11,115 chars omitted · full output: read_output("o2f61b5")] …
  (more of the end, shortened here)
… {'number': 129, 'title': 'Investigate billing timeout', 'state': 'open', … }]

[state] stale: list[13] · cutoff: datetime · issues: list[24]
[meta] exec 2ms · tool calls 0 · returned 3.8 KB`,
        html`The reply stops at the 4,000-character budget. If the agent really needs the middle,
          <code>read_output("o2f61b5", offset=…)</code> pages through it.`)}

      ${step(4, "Act, with a person in the loop",
        html`The agent reuses <code>stale</code> from memory; nothing is fetched again. Posting to Slack is marked destructive,
          so the call waits until someone clicks <strong>Allow once</strong> on the <a href="#/approvals">Approvals</a> page.`,
        `execute_code("""\n${EX.post}\n""")`,
        `[stdout]
posted 13

[state] lines: list[13] · stale: list[13] · cutoff: datetime · issues: list[24]
[meta] exec 70ms · tool calls 1 (slack.post_message ×1) · data via tools 66 B · returned 201 B`,
        "Here the approval came quickly; while it waits, the code simply pauses.")}

      <div class="note calm" style="margin-top:8px"><strong>The result:</strong> the three useful replies (steps 1, 2 and 4) add up to under 1 KB.
        Even with the careless print, the whole task used about 4.6 KB of context. With direct tool calls, every issue
        with its full body passes through the model, and in a real repository that's often hundreds of kilobytes.</div>
      <p class="row-gap" style="margin-top:18px">
        <button class="primary" data-try="fetch">Run step 2 in the playground</button>
        <a class="btn" href="#/runs">See your own runs</a>
      </p>
    </section>

    <section id="mistakes">
      <h2>When things go wrong</h2>
      <p><strong>A typo is caught before anything runs.</strong> Imports and argument names are checked against the real tools:</p>
      <pre>[error]
Static check failed (code was not run):
- servers.github has no tool 'list_isues'. Did you mean: list_issues, get_issue, create_issue?</pre>
      <p><strong>A restart is never silent.</strong> If a runtime is stopped (idle too long, out of memory, reset), the next reply says so,
        and lists what survived:</p>
      <pre>[runtime] Restarted since your last call (reason: idle timeout (30 min without use)). All variables were cleared.
Files in saved/ are kept: stale.json · stash keys: n</pre>
      <p>That's why the agent is told to <code>save(x, "name")</code> anything expensive to collect: <code>load("name")</code> brings it back.</p>
      <p><strong>Long-running code is interrupted, not killed.</strong> After the time limit the code stops, and variables set before that point are kept.</p>
    </section>

    <section id="tools">
      <h2>What the agent sees</h2>
      <p>Seven tools, about 1,000 tokens in total, however many servers you connect:</p>
      <table class="table">
        <tr><td><code>execute_code</code></td><td>Run Python in the session's runtime. The main tool.</td></tr>
        <tr><td><code>search_tools</code></td><td>Find tools by keyword, as names, one-line summaries, or full signatures.</td></tr>
        <tr><td><code>describe_tool</code></td><td>The full Python signature and docstring for one tool.</td></tr>
        <tr><td><code>list_servers</code></td><td>Connected servers and how many tools each has.</td></tr>
        <tr><td><code>call_tool</code></td><td>One direct call without code, for simple lookups. Big results stay in the runtime as <code>_last</code>.</td></tr>
        <tr><td><code>read_output</code></td><td>Page through output that was trimmed.</td></tr>
        <tr><td><code>reset_runtime</code></td><td>Start fresh. Files in <code>saved/</code> are kept unless asked otherwise.</td></tr>
      </table>
      <p>It also gets short instructions on how to use them. You can read them, word for word, in <a href="#/settings">Settings</a>.</p>
    </section>

    <section id="helpers">
      <h2>Runtime helpers</h2>
      <table class="table">
        <tr><td><code>preview(x)</code></td><td>The shape of a value (type, size, keys, a few samples) without dumping it.</td></tr>
        <tr><td><code>save(x, name)</code> · <code>load(name)</code></td><td>Keep data in <code>saved/</code>, which survives restarts.</td></tr>
        <tr><td><code>stash</code></td><td>A small dictionary kept by the gateway, for notes that must outlive the runtime.</td></tr>
        <tr><td><code>parallel(fn, kwargs_list)</code></td><td>Call a tool many times at once, results in order.</td></tr>
        <tr><td><code>search(query)</code></td><td><code>search_tools</code>, from inside code.</td></tr>
      </table>
      <p>pandas and numpy are installed. A big DataFrame as the last line shows a preview, not ten thousand rows.</p>
    </section>

    <section id="safety">
      <h2>Safety</h2>
      <ul>
        <li><strong>Credentials stay in the gateway.</strong> Runtimes never see API keys; they ask the gateway to make each call.</li>
        <li><strong>Policies decide what's allowed.</strong> Deny tools by name, rate-limit them, or require a person's approval for anything marked destructive.</li>
        <li><strong>Isolation is a switch.</strong> By default runtimes are local processes, which is fine for your own machine.
          With <code>--driver docker</code> each session gets a locked-down container that can call tools but can't reach the internet.</li>
      </ul>
    </section>

    <section id="start">
      <h2>Get started</h2>
      <div class="actions-grid two">
        <a class="action primary" href="#/servers"><span class="action-title">Connect an MCP server</span><span class="action-sub">Paste a config from Claude Desktop or Claude Code</span></a>
        <a class="action" href="#/settings"><span class="action-title">Connect an agent</span><span class="action-sub">One command for Claude Code</span></a>
        <a class="action" href="#/playground"><span class="action-title">Try the playground</span><span class="action-sub">Run code exactly as an agent would</span></a>
        <a class="action" href="#/tools"><span class="action-title">Browse tools</span><span class="action-sub">Every tool as a Python function</span></a>
      </div>
    </section>
  </article>
</div>`, {
  nav: "guide",
  after() {
    $$("[data-jump]").forEach((a) => (a.onclick = (e) => {
      e.preventDefault();
      document.getElementById(a.dataset.jump)?.scrollIntoView({ block: "start" });
    }));
    $$("[data-try]").forEach((b) => (b.onclick = () => { play.code = EX[b.dataset.try]; location.hash = "#/playground"; }));
    // Highlight the section in view.
    const links = $$(".toc a");
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) links.forEach((l) => l.toggleAttribute("aria-current", l.dataset.jump === en.target.id));
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    $$(".prose section").forEach((sec) => obs.observe(sec));
  },
});

// ------------------------------------------------------------------ Benchmarks

const fmtTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : Math.round(n).toLocaleString());
const CONFIG_HINT = {
  direct: "Every tool given to the model directly, as most MCP clients do today.",
  toolkernel: "ToolKernel with a persistent runtime (the default).",
  stateless: "ToolKernel, but a fresh runtime for every call, like most other code-mode tools.",
  guard_off: "ToolKernel with the output guard switched off, to show what the guard is worth.",
};

route("/bench", async () => {
  const d = await api("/api/bench");
  return html`<div>
    <h1>Benchmarks</h1>
    <p class="sub">Run the same tasks with and without ToolKernel, and see what changes: correctness, tokens, cost and time.
      Tasks use real tools: a <strong>files</strong> server over Excel, CSV and JSON data, and a <strong>web</strong> server for search and pages.</p>

    <div class="section" style="margin-top:28px">
      <h2 style="margin-bottom:12px">Run one</h2>
      <p class="sub" style="margin:0 0 14px">Benchmarks run from the terminal, so nothing spends money by accident. Start with the free check.</p>
      <div class="field"><label>Free plumbing check: no model calls, proves everything is wired up</label>${codeBlock(d.commands.check)}</div>
      <div class="field"><label>The real thing: needs ANTHROPIC_API_KEY, asks before spending, stops at the cap</label>${codeBlock(d.commands.run)}</div>
      <div class="field"><label>Compare a frontier model with a small one</label>${codeBlock(d.commands.models)}</div>
      ${d.has_brave_key ? "" : html`<p class="small muted">The web search task is skipped until <span class="mono">BRAVE_API_KEY</span> is set in the gateway's environment. The web page task works without it.</p>`}
    </div>

    <div class="section">
      <h2 style="margin-bottom:14px">Runs</h2>
      ${d.runs.length ? html`<div class="list">${d.runs.map((r) => {
        const best = {};
        r.headline.forEach((h) => { best[h.config] = h; });
        const dir = best.direct, tk = best.toolkernel;
        const ratio = dir && tk && tk.mean_input_tokens ? dir.mean_input_tokens / tk.mean_input_tokens : null;
        return html`<a class="item" href="#/bench/${r.run_id}">
          <span class="status ${r.meta.status === "done" ? "ok" : r.meta.status === "running" ? "connecting" : "error"}"></span>
          <span class="grow"><span class="title">${r.meta.models.join(", ")} · ${Object.keys(r.meta.configs).length} configurations · ${r.meta.tasks.length} tasks
            ${r.simulated ? html` <span class="chip">plumbing check</span>` : ""}</span>
            <span class="meta">${new Date(r.meta.started * 1000).toLocaleString()} · ${r.rows} task runs · ${r.meta.status}
              ${ratio && !r.simulated ? html` · <span class="saved">direct used ${ratio.toFixed(1)}× the tokens</span>` : ""}</span></span>
          <span class="right">${r.simulated ? "free" : `$${r.total_cost_usd.toFixed(2)}`}</span></a>`;
      })}</div>` : html`<div class="empty"><h3>No runs yet</h3>Start with the free plumbing check above. It takes about a minute.</div>`}
    </div>
  </div>`;
}, { nav: "bench" });

route("/bench/([^/]+)", async (runId) => {
  const s = await api(`/api/bench/${encodeURIComponent(runId)}`);
  const m = s.meta;
  const models = [...new Set(s.summary.map((x) => x.model))];
  const cols = s.summary.map((x) => `${x.model}|${x.config}`);
  const tfile = (task, col) => {
    const [model, config] = col.split("|");
    return s.transcripts.find((n) => n.startsWith(`${model}--${config}--${task}--`));
  };
  return html`<div>
    <p class="small"><a href="#/bench">← Benchmarks</a></p>
    <h1 style="margin-top:8px">Run ${m.run_id}</h1>
    <p class="sub">${new Date(m.started * 1000).toLocaleString()} · effort <span class="mono">${m.effort}</span> · runtime <span class="mono">${m.driver}</span>
      · ${m.tasks.length} tasks × ${m.repeats} · ${m.status} · ${s.simulated ? "no model calls" : `estimated $${s.total_cost_usd.toFixed(2)}`}</p>
    ${s.simulated ? html`<p class="note" style="margin-top:18px"><strong>Plumbing check, not a benchmark.</strong> A scripted stand-in played the agent:
      it answered from the ground truth and token counts are estimated from text length. Ignore the scores; it shows the tools,
      gateway and harness all work. The input-token gap still shows how much raw data each setup pushes at the model.</p>` : ""}

    ${models.map((model) => {
      const rows = s.summary.filter((x) => x.model === model);
      const maxIn = Math.max(1, ...rows.map((x) => x.mean_input_tokens));
      const direct = rows.find((x) => x.config === "direct");
      return html`<div class="section">
        <div class="section-head"><h2>${model}</h2></div>
        <div class="bench-table">
          <div class="bt-row bt-head"><span>Setup</span><span>Correct</span><span>Input tokens per task</span><span>Peak context</span><span>Calls</span><span>Time</span><span>Cost</span></div>
          ${rows.map((x) => html`<div class="bt-row">
            <span><strong>${x.config}</strong><br><span class="small muted">${CONFIG_HINT[x.config] || x.label}</span></span>
            <span data-l="Correct" class="${x.success_rate === 1 ? "saved" : x.success_rate < 0.5 ? "bad-text" : ""}"><strong>${x.successes}/${x.runs}</strong></span>
            <span data-l="Input tokens per task"><span class="hbar"><i style="width:${Math.max(1.5, (100 * x.mean_input_tokens) / maxIn)}%" class="${x.config === "direct" ? "muted-bar" : ""}"></i></span>
              <span class="small">${fmtTok(x.mean_input_tokens)}${direct && x !== direct && x.mean_input_tokens ? html` <span class="saved">· ${(direct.mean_input_tokens / x.mean_input_tokens).toFixed(1)}× less</span>` : ""}</span></span>
            <span class="small" data-l="Peak context">${fmtTok(x.median_peak_context_tokens)}</span>
            <span class="small" data-l="Calls per task">${x.mean_api_calls.toFixed(1)} model · ${x.mean_tool_calls.toFixed(1)} tool${x.total_repeated_fetches ? html`<br><span class="muted">${x.total_repeated_fetches} refetched</span>` : ""}</span>
            <span class="small" data-l="Time">${x.mean_wall_s.toFixed(1)} s</span>
            <span class="small" data-l="Cost">$${x.total_cost_usd.toFixed(2)}</span>
          </div>`)}
        </div>
      </div>`;
    })}

    <div class="section">
      <h2 style="margin-bottom:6px">Task by task</h2>
      <p class="sub" style="margin:0 0 14px">Click a cell to read the full conversation: every model call, tool call and result.</p>
      <div style="overflow-x:auto"><table class="table matrix">
        <tr><td></td>${cols.map((c) => html`<td class="small muted">${c.split("|")[1]}${models.length > 1 ? html`<br>${c.split("|")[0]}` : ""}</td>`)}</tr>
        ${s.tasks.map((t) => html`<tr><td><strong>${t.title}</strong><br><span class="small muted mono">${t.task}</span></td>
          ${cols.map((c) => {
            const cell = t.cells[c];
            if (!cell) return html`<td class="faint">–</td>`;
            const ok = cell.successes === cell.runs, none = cell.successes === 0;
            const f = tfile(t.task, c);
            const inner = html`<span class="${ok ? "saved" : none ? "bad-text" : ""}">${ok ? "✓" : none ? "✗" : "◐"} ${cell.successes}/${cell.runs}</span><br>
              <span class="small muted">${fmtTok(cell.input_tokens / cell.runs)} tok</span>
              ${cell.errors.length ? html`<br><span class="small bad-text" title="${cell.errors.join("\n")}">${cell.errors[0].slice(0, 40)}…</span>` : ""}`;
            return html`<td>${f ? html`<a class="cell-link" href="#/bench/${m.run_id}/t/${encodeURIComponent(f)}">${inner}</a>` : inner}</td>`;
          })}</tr>`)}
      </table></div>
    </div>
  </div>`;
}, { nav: "bench" });

route("/bench/([^/]+)/t/(.+)", async (runId, name) => {
  const d = await api(`/api/bench/${encodeURIComponent(runId)}/transcripts/${encodeURIComponent(name)}`);
  const r = d.result;
  const blocks = d.transcript.map((e) => {
    if (e.event) return html`<p class="note" style="margin:14px 0">${e.event}</p>`;
    if (e.role === "user") return html`<div class="tr-user"><span class="pane-label"><span>User</span></span><p>${e.content}</p></div>`;
    if (e.role === "assistant") {
      const u = e.usage || {};
      return html`<div class="tr-asst"><div class="pane-label"><span>Model</span>
          <span>${fmtTok((u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0))} in · ${fmtTok(u.output || 0)} out</span></div>
        ${e.content.filter((b) => b.type === "text" || b.type === "tool_use").map((b) => b.type === "text"
          ? html`<p style="white-space:pre-wrap">${b.text}</p>`
          : html`<pre class="small">${b.name}(${JSON.stringify(b.input, null, 1)})</pre>`)}</div>`;
    }
    if (e.tool) return html`<details class="call"><summary><span class="status ${e.is_error ? "error" : "ok"}"></span>
        <span class="mono" style="flex:1">${e.tool} → result</span><span class="small muted">${e.result_chars.toLocaleString()} chars to the model</span></summary>
        <div class="body"><pre>${e.result_head}${e.result_chars > 1500 ? "\n… (only the start is kept in transcripts)" : ""}</pre></div></details>`;
    return "";
  });
  return html`<div>
    <p class="small"><a href="#/bench/${runId}">← Run ${runId}</a></p>
    <h1 style="margin-top:8px"><span class="mono" style="font-size:26px">${r.task}</span> · ${r.config}</h1>
    <p class="sub">${r.model} · ${r.success ? html`<span class="saved">correct</span>` : html`<span class="bad-text">not correct</span>`}
      · ${fmtTok(r.input_tokens)} input tokens · peak ${fmtTok(r.peak_context_tokens)} · ${r.api_calls} model calls · ${r.tool_calls} tool calls · ${r.wall_s}s</p>
    <dl class="kv" style="margin-top:18px"><dt>Answer</dt><dd>${r.answer || "(none)"}</dd><dt>Expected</dt><dd>${r.expected}</dd>
      ${r.error ? html`<dt>Error</dt><dd class="bad-text">${r.error}</dd>` : ""}</dl>
    <div class="section" style="margin-top:28px">${blocks}</div>
  </div>`;
}, { nav: "bench" });

// ------------------------------------------------------------------ Settings

route("/settings", async () => {
  const [s, o] = await Promise.all([api("/api/settings"), api("/api/overview")]);
  const c = s.config, rt = c.runtime, out = c.output;
  const stdio = JSON.stringify({ mcpServers: { toolkernel: { command: s.cli, args: ["connect", o.connect.mcp_url.replace(/\/mcp$/, ""), ...(o.connect.has_key ? ["--key", "<your key>"] : [])] } } }, null, 2);
  const base = o.connect.mcp_url.replace(/\/mcp$/, "");
  const rest = `curl -s ${base}/v1/tools?format=openai${o.connect.has_key ? ' -H "Authorization: Bearer <your key>"' : ""}`;
  let theme = "auto";
  try { theme = localStorage.getItem("tk-theme") || "auto"; } catch {}
  return html`<div>
    <h1>Settings</h1>
    <p class="sub">How agents connect, what they're told, and how runtimes behave. Edit <span class="mono">toolkernel.yaml</span> to change these.</p>

    <div class="section"><h2 style="margin-bottom:14px">Connect an agent</h2>
      <div class="field"><label>Claude Code</label>${codeBlock(connectSnippet(o))}</div>
      <div class="field"><label>Clients that only speak stdio (Claude Desktop and others)</label>${codeBlock(stdio)}</div>
      <div class="field"><label>Any LLM through REST: tool definitions in OpenAI or Anthropic format</label>${codeBlock(rest)}</div>
    </div>

    <div class="section"><h2 style="margin-bottom:6px">Runtime</h2>
      ${rt.driver === "local" ? html`<p class="note" style="margin:12px 0 18px">Runtimes are running as <strong>local processes, without isolation</strong>.
        That's fine on your own machine with code you trust. For anything else, use the Docker driver: <span class="mono">--driver docker</span>.</p>` : ""}
      <dl class="kv">
        <dt>Driver</dt><dd>${rt.driver}${rt.driver === "docker" ? ` · ${rt.image}${rt.hardened ? " · gVisor" : ""}` : ""}</dd>
        <dt>State</dt><dd>${rt.state === "stateful" ? "stateful: variables persist between calls" : "stateless: a fresh runtime for every call"}</dd>
        <dt>Time limit</dt><dd>${rt.exec_timeout_s} s per run</dd>
        <dt>Idle timeout</dt><dd>${Math.round(rt.idle_timeout_s / 60)} min</dd>
        ${rt.driver === "docker" ? html`<dt>Limits</dt><dd>${rt.cpu} CPU · ${rt.memory} memory · ${rt.pids} processes</dd>` : ""}
        <dt>Output budget</dt><dd>${out.budget_chars.toLocaleString()} chars (${Math.round(out.head_ratio * 100)}% head, ${100 - Math.round(out.head_ratio * 100)}% tail)</dd>
        <dt>Static check</dt><dd>${rt.static_check ? "on" : "off"}</dd>
        <dt>Direct call_tool</dt><dd>${c.agent_interface.hybrid_call_tool ? "on" : "off"}</dd>
        <dt>Policy rules</dt><dd>${(c.policies.rules || []).length} · default ${c.policies.default}</dd>
      </dl>
    </div>

    <div class="section"><h2 style="margin-bottom:14px">What agents are told</h2>
      <pre>${s.instructions}</pre>
      <details style="margin-top:12px"><summary class="small muted" style="cursor:pointer">The ${s.meta_tools.length} tools agents see</summary>
        <div class="list" style="margin-top:10px">${s.meta_tools.map((t) => html`<div class="item"><span class="grow"><span class="title mono">${t.name}</span>
          <span class="meta" style="white-space:normal">${t.description}</span></span></div>`)}</div></details>
    </div>

    <div class="section"><h2 style="margin-bottom:14px">Files</h2>
      <dl class="kv">
        <dt>Settings</dt><dd>${s.config_file || "defaults (no toolkernel.yaml)"}</dd>
        <dt>Servers</dt><dd>${s.mcp_json}</dd>
        <dt>Data</dt><dd>${s.data_dir}</dd>
      </dl>
    </div>

    <div class="section"><h2 style="margin-bottom:14px">Appearance</h2>
      <div class="row-gap" role="radiogroup" aria-label="Theme">
        ${["auto", "light", "dark"].map((t) => html`<button class="${theme === t ? "primary" : ""}" data-theme-pick="${t}" role="radio" aria-checked="${theme === t}">${t[0].toUpperCase() + t.slice(1)}</button>`)}
      </div>
    </div>
  </div>`;
}, {
  nav: "settings",
  after() {
    $$("[data-theme-pick]").forEach((b) => (b.onclick = () => { setTheme(b.dataset.themePick); render(true); }));
  },
});

function setTheme(t) {
  try { localStorage.setItem("tk-theme", t); } catch {}
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = t;
}

// ------------------------------------------------------------------ boot

(function boot() {
  try { const t = localStorage.getItem("tk-theme"); if (t && t !== "auto") document.documentElement.dataset.theme = t; } catch {}
  render();
  connectEvents();
  updateBadge();
  setInterval(() => { if (current?.nav === "home" && !document.hidden) render(true); }, 60000);
})();
