# -*- coding: utf-8 -*-
"""控制台：一个零构建、零前端依赖的只读面板。

为什么不用框架
--------------
它是给运维和调试看的，不是产品。单文件 HTML + 原生 fetch 足够，
而且对「可复用开源框架」这个定位很重要 —— 别人 clone 下来就能用，
不需要 npm install。

能力边界
--------
只读 + 触发健康探测。不做启用/禁用 agent、不取消任务 ——
控制台一旦能写，就得配鉴权，那是另一个量级的事。
"""

from __future__ import annotations

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>a2a-hub 控制台</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --panel2: #1e222b; --line: #2a2f3a;
    --fg: #e6e9ef; --fg2: #9aa3b2; --fg3: #6b7280;
    --ok: #3fb950; --bad: #f85149; --warn: #d29922; --accent: #58a6ff;
    --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 13px/1.55 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 14px 20px; border-bottom: 1px solid var(--line);
    background: var(--panel); position: sticky; top: 0; z-index: 10;
  }
  h1 { font-size: 15px; font-weight: 500; margin: 0; letter-spacing: .3px; }
  h1 span { color: var(--fg3); font-weight: 400; }
  .stats { display: flex; gap: 18px; flex-wrap: wrap; }
  .stat { display: flex; align-items: baseline; gap: 6px; }
  .stat b { font-size: 16px; font-weight: 500; font-family: var(--mono); }
  .stat i { font-style: normal; color: var(--fg3); font-size: 12px; }
  .spacer { flex: 1; }
  button {
    background: var(--panel2); color: var(--fg); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 12px;
    font-family: inherit;
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.active { background: var(--accent); color: #06101f; border-color: var(--accent); }
  label.auto { display: flex; align-items: center; gap: 6px; color: var(--fg2); font-size: 12px; cursor: pointer; }
  nav { display: flex; gap: 8px; padding: 12px 20px 0; }
  main { padding: 16px 20px 60px; }
  section { display: none; }
  section.on { display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; color: var(--fg3); font-weight: 400; padding: 6px 10px;
    border-bottom: 1px solid var(--line); white-space: nowrap;
  }
  td { padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tr.clickable { cursor: pointer; }
  tr.clickable:hover td { background: var(--panel2); }
  .mono { font-family: var(--mono); font-size: 12px; }
  .dim { color: var(--fg3); }
  .pill {
    display: inline-block; padding: 1px 7px; border-radius: 10px;
    font-size: 11px; font-family: var(--mono); border: 1px solid var(--line);
  }
  .pill.ok { color: var(--ok); border-color: #1d3b25; background: #0f1f14; }
  .pill.bad { color: var(--bad); border-color: #4a1f1f; background: #1f1010; }
  .pill.warn { color: var(--warn); border-color: #3d3115; background: #1f1a0f; }
  .pill.unknown { color: var(--fg3); }
  .pill.tag { color: var(--accent); border-color: #1d3252; background: #0f1725; margin-right: 4px; }
  .empty { color: var(--fg3); padding: 30px 0; text-align: center; }
  .trace-head {
    display: flex; gap: 20px; flex-wrap: wrap; align-items: center;
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px 16px; margin-bottom: 16px;
  }
  .gantt { margin: 6px 0 20px; }
  .grow { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .glabel { width: 190px; flex: none; text-align: right; color: var(--fg2); font-size: 12px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gtrack { position: relative; flex: 1; height: 18px; background: var(--panel2); border-radius: 4px; }
  .gbar { position: absolute; height: 100%; border-radius: 4px; background: #1f6feb;
          min-width: 2px; }
  .gbar.ok { background: #238636; }
  .gbar.bad { background: #b62324; }
  .gbar.canceled { background: #6b7280; }
  .gtime { width: 70px; flex: none; color: var(--fg3); font-size: 11.5px; font-family: var(--mono); }
  .tl { font-family: var(--mono); font-size: 11.5px; }
  .tl td { padding: 4px 10px; }
  .kind { display: inline-block; min-width: 62px; color: var(--fg2); }
  .kind.thinking { color: #a371f7; }
  .kind.tool_call { color: #d29922; }
  .kind.tool_result { color: #3fb950; }
  .kind.error { color: var(--bad); }
  .kind.status { color: var(--fg3); }
  .err { color: var(--bad); }

  /* ---- 实时视图 ---- */
  .liverow { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .liverow select, .liverow input {
    background: var(--panel2); color: var(--fg); border: 1px solid var(--line);
    border-radius: 5px; padding: 6px 9px; font: inherit;
  }
  .liverow input { flex: 1; min-width: 240px; }
  .feed { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  #live-feed { max-height: 62vh; overflow-y: auto; }
  .feed .row {
    display: grid; grid-template-columns: 78px 84px 1fr;
    gap: 10px; padding: 5px 10px; border-top: 1px solid var(--line);
    font-size: 12px; align-items: baseline;
  }
  .feed .row:first-child { border-top: 0; }
  .feed .row.terminal { background: rgba(63,185,80,.08); }
  .feed .row.terminal.bad { background: rgba(248,81,73,.08); }
  .feed .t { color: var(--fg3); font-variant-numeric: tabular-nums; }
  .feed .gap { color: var(--warn); font-style: italic; }
  .live-dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--ok); margin-right: 6px; animation: pulse 1.4s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .25 } }
</style>
</head>
<body>
<header>
  <h1>a2a-hub <span id="ver"></span></h1>
  <div class="stats" id="stats"></div>
  <div class="spacer"></div>
  <label class="auto"><input type="checkbox" id="auto"> 自动刷新 5s</label>
  <button id="refresh">刷新</button>
</header>

<nav>
  <button data-view="live" class="active">实时</button>
  <button data-view="overview">概览</button>
  <button data-view="agents">注册表</button>
  <button data-view="traces">Trace</button>
</nav>

<main>
  <section id="view-live" class="on">
    <div class="liverow">
      <select id="live-agent"></select>
      <input id="live-prompt" placeholder="给这个 agent 派个任务，边跑边看…">
      <button id="live-start">派任务</button>
      <button id="live-stop" disabled>断开</button>
    </div>
    <div class="dim" id="live-status" style="margin:8px 0 12px"></div>
    <div id="live-feed" class="feed"><div class="empty">还没有运行中的任务。</div></div>
  </section>

  <section id="view-overview">
    <h2 style="font-size:13px;font-weight:400;color:var(--fg2);margin:0 0 10px">最近的 Trace</h2>
    <div id="ov-traces"></div>
    <h2 style="font-size:13px;font-weight:400;color:var(--fg2);margin:22px 0 10px">最近的任务</h2>
    <div id="ov-tasks"></div>
  </section>

  <section id="view-agents">
    <div style="margin-bottom:10px">
      <button id="probe">探测全部</button>
      <span class="dim" style="margin-left:10px" id="probe-msg"></span>
    </div>
    <div id="agents"></div>
  </section>

  <section id="view-traces">
    <div id="trace-list"></div>
    <div id="trace-detail"></div>
  </section>
</main>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const short = (s, n = 60) => { s = String(s ?? "").replace(/\s+/g, " "); return s.length > n ? s.slice(0, n) + "…" : s; };
const ms = (v) => v == null ? "-" : (v < 1000 ? v + "ms" : (v / 1000).toFixed(1) + "s");
const hhmmss = (iso) => iso ? iso.slice(11, 23) : "-";

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r.json();
}

let state = { health: null, agents: [], traces: [], tasks: null, trace: null };

function renderStats() {
  const h = state.health || {};
  const t = state.tasks || {};
  const running = (t.running || []).length;
  const ok = (t.recent || []).filter(x => x.state === "completed").length;
  const fail = (t.recent || []).filter(x => x.state === "failed").length;
  $("#ver").textContent = h.schemaVersion != null ? "v0.2.0 · schema " + h.schemaVersion : "";
  $("#stats").innerHTML = [
    ["任务", h.tasks ?? "-"],
    ["Agent", h.agents ?? "-"],
    ["运行中", running],
    ["最近成功", ok],
    ["最近失败", fail],
  ].map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><i>${k}</i></div>`).join("");
}

function renderAgents() {
  const rows = state.agents || [];
  if (!rows.length) { $("#agents").innerHTML = '<div class="empty">注册表为空</div>'; return; }
  $("#agents").innerHTML = `<table>
    <thead><tr><th>name</th><th>kind</th><th>health</th><th>tags</th><th>endpoint / adapter</th></tr></thead>
    <tbody>${rows.map(a => `
      <tr>
        <td class="mono">${esc(a.name)}</td>
        <td class="dim">${esc(a.kind)}</td>
        <td><span class="pill ${esc(a.health)}">${esc(a.health)}</span></td>
        <td>${(a.tags || []).map(t => `<span class="pill tag">${esc(t)}</span>`).join("") || '<span class="dim">-</span>'}</td>
        <td class="mono dim">${esc(a.endpoint || "-")}</td>
      </tr>`).join("")}</tbody></table>`;
}

function renderTraces() {
  const rows = state.traces || [];
  if (!rows.length) {
    $("#trace-list").innerHTML = '<div class="empty">还没有 trace。发一个 SendMessage 或 RunPlan 试试。</div>';
    return;
  }
  $("#trace-list").innerHTML = `<table>
    <thead><tr><th>traceId</th><th>开始</th><th>任务</th><th>成功/失败</th><th>agents</th></tr></thead>
    <tbody>${rows.map(t => `
      <tr class="clickable" data-trace="${esc(t.trace_id)}">
        <td class="mono">${esc(t.trace_id)}</td>
        <td class="dim mono">${esc(hhmmss(t.started_at))}</td>
        <td>${esc(t.tasks)}</td>
        <td><span class="pill ok">${esc(t.completed)}</span>${t.failed ? ` <span class="pill bad">${esc(t.failed)}</span>` : ""}</td>
        <td class="dim">${esc(t.agents || "-")}</td>
      </tr>`).join("")}</tbody></table>`;
  $("#trace-list").querySelectorAll("tr[data-trace]").forEach(tr => {
    tr.onclick = () => loadTrace(tr.dataset.trace);
  });
}

function renderTasks() {
  const rows = (state.tasks && state.tasks.recent) || [];
  if (!rows.length) { $("#ov-tasks").innerHTML = '<div class="empty">还没有任务</div>'; return; }
  $("#ov-tasks").innerHTML = `<table>
    <thead><tr><th>step</th><th>agent</th><th>state</th><th>耗时</th><th>prompt</th></tr></thead>
    <tbody>${rows.map(t => `
      <tr class="clickable" data-trace="${esc(t.traceId || "")}">
        <td class="mono">${esc(t.stepId || "-")}</td>
        <td class="dim">${esc(t.agent || "-")}</td>
        <td><span class="pill ${t.state === "completed" ? "ok" : t.state === "failed" ? "bad" : "unknown"}">${esc(t.state)}</span></td>
        <td class="mono dim">${ms(t.durationMs)}</td>
        <td>${esc(short(t.prompt, 70))}${t.error ? ` <span class="err">${esc(short(t.error, 40))}</span>` : ""}</td>
      </tr>`).join("")}</tbody></table>`;
  $("#ov-tasks").querySelectorAll("tr[data-trace]").forEach(tr => {
    if (tr.dataset.trace) tr.onclick = () => { switchView("traces"); loadTrace(tr.dataset.trace); };
  });
}

function renderTraceDetail() {
  const d = state.trace;
  if (!d) { $("#trace-detail").innerHTML = ""; return; }
  const s = d.summary;
  const tasks = d.tasks || [];
  const t0 = new Date(s.startedAt).getTime();
  const span = Math.max(s.durationMs || 1, 1);

  const head = `<div class="trace-head">
    <div class="mono">${esc(d.traceId)}</div>
    <div class="dim">context ${esc(s.contextId || "-")}</div>
    <div class="dim">plan ${esc(s.planId || "-")}</div>
    <div>${s.tasks} 任务 · <span style="color:var(--ok)">${s.completed}</span>${s.failed ? ` / <span style="color:var(--bad)">${s.failed}</span>` : ""} · ${ms(s.durationMs)}</div>
    <div class="dim mono">${esc(JSON.stringify(s.usage || {}))}</div>
  </div>`;

  const gantt = tasks.map(t => {
    const start = (new Date(t.startedAt).getTime() - t0) / span * 100;
    const width = (t.durationMs || 0) / span * 100;
    const cls = t.state === "completed" ? "ok" : t.state === "failed" ? "bad" : t.state === "canceled" ? "canceled" : "";
    const label = (t.stepId || t.taskId.slice(0, 8)) + "  " + (t.agent || "-");
    return `<div class="grow">
      <div class="glabel" title="${esc(label)}">${esc(label)}</div>
      <div class="gtrack"><div class="gbar ${cls}" style="left:${start.toFixed(2)}%;width:${Math.max(width, 0.6).toFixed(2)}%"></div></div>
      <div class="gtime">${ms(t.durationMs)}</div>
    </div>`;
  }).join("");

  const timeline = (d.timeline || []).map(e => `
    <tr>
      <td class="mono dim">${esc(hhmmss(e.at))}</td>
      <td class="dim">${esc(e.agent || "-")}</td>
      <td><span class="kind ${esc(e.kind || "")}">${esc(e.kind || "-")}</span></td>
      <td>${esc(short(e.text, 130))}</td>
    </tr>`).join("");

  $("#trace-detail").innerHTML = head
    + `<div class="gantt">${gantt}</div>`
    + `<h2 style="font-size:13px;font-weight:400;color:var(--fg2);margin:18px 0 6px">过程时间线</h2>`
    + `<div class="dim" style="margin-bottom:6px;font-size:12px">`
    + `时间戳为<b>事件发生时刻</b>。想看还没跑完的任务，去「实时」页 —— `
    + `那里是边跑边推的。注：HTTP 类下游的时间戳取自轮询时刻（下游没在 history 里带自己的时间），`
    + `精度受轮询间隔限制。</div>`
    + `<table class="tl"><thead><tr><th>时间</th><th>agent</th><th>kind</th><th>内容</th></tr></thead><tbody>${timeline}</tbody></table>`;
}

async function loadTrace(id) {
  try {
    state.trace = await getJSON("/admin/trace/" + encodeURIComponent(id));
  } catch (e) {
    state.trace = null;
    $("#trace-detail").innerHTML = `<div class="empty err">加载失败：${esc(e.message)}</div>`;
    return;
  }
  renderTraceDetail();
  const el = $("#trace-detail");
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadAll() {
  const [h, a, tr, tk] = await Promise.all([
    getJSON("/healthz").catch(() => null),
    getJSON("/admin/agents").then(r => r.agents || []).catch(() => []),
    getJSON("/admin/traces?limit=30").then(r => r.traces || []).catch(() => []),
    getJSON("/admin/tasks?limit=30").catch(() => null),
  ]);
  state.health = h; state.agents = a; state.traces = tr; state.tasks = tk;
  renderStats(); renderAgents(); renderTraces(); renderTasks(); renderLiveAgents();
}

// ---- 实时视图 --------------------------------------------------------------
// 用 fetch + ReadableStream 读 SSE，而**不是** EventSource：
// EventSource 只能发 GET，带不了 message 体。原生 API，依旧零依赖。
let liveAbort = null;

function renderLiveAgents() {
  const sel = $("#live-agent");
  const cur = sel.value;
  sel.innerHTML = (state.agents || [])
    .map(a => `<option value="${esc(a.name)}">${esc(a.name)} · ${esc(a.kind)}</option>`)
    .join("");
  if (cur) sel.value = cur;
}

function liveRow(t, agent, kind, text, cls) {
  return `<div class="row ${cls || ""}">
    <span class="t">${esc(t)}</span>
    <span class="dim">${esc(agent || "-")}</span>
    <span><span class="kind ${esc(kind || "")}">${esc(kind || "-")}</span> ${esc(text || "")}</span>
  </div>`;
}

function handleFrame(frame, t0, agent) {
  const line = frame.split("\n").find(l => l.startsWith("data:"));
  if (!line) return;                        // 心跳（": keep-alive"）直接忽略
  let p; try { p = JSON.parse(line.slice(5).trim()); } catch (e) { return; }
  const r = p.result || {};
  const t = ((performance.now() - t0) / 1000).toFixed(2) + "s";
  const feed = $("#live-feed");
  const empty = feed.querySelector(".empty");
  if (empty) empty.remove();

  if (r.statusUpdate) {
    const su = r.statusUpdate, md = su.metadata || {}, st = su.status || {};
    let text = "";
    for (const part of ((st.message || {}).parts || [])) text += part.text || "";
    // 服务器在跟不上时会丢事件并带上累计 dropped —— 如实标出断点，
    // 不假装自己看到的是完整过程。
    if (md.dropped) {
      feed.insertAdjacentHTML("beforeend",
        `<div class="row"><span class="t">${esc(t)}</span><span></span>
         <span class="gap">…中间丢了 ${md.dropped} 条事件（客户端读得太慢）</span></div>`);
    }
    feed.insertAdjacentHTML("beforeend", liveRow(t, agent, md.kind, short(text, 220)));
  } else if (r.task) {
    const st = (r.task.status || {}).state || "";
    const bad = st !== "TASK_STATE_COMPLETED";
    feed.insertAdjacentHTML("beforeend",
      liveRow(t, agent, "终态", st, "terminal" + (bad ? " bad" : "")));
  }
  feed.scrollTop = feed.scrollHeight;
}

function startLive() {
  const prompt = $("#live-prompt").value.trim();
  const agent = $("#live-agent").value;
  if (!prompt) { $("#live-status").textContent = "先写点什么再派。"; return; }
  stopLive();
  const ctrl = new AbortController();
  liveAbort = ctrl;
  $("#live-feed").innerHTML = "";
  $("#live-start").disabled = true;
  $("#live-stop").disabled = false;
  $("#live-status").innerHTML =
    `<span class="live-dot"></span>运行中 · agent=${esc(agent)} · 事件在发生的那一刻就推过来`;

  const t0 = performance.now();
  fetch("/", {
    method: "POST",
    signal: ctrl.signal,
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: "live", method: "SendStreamingMessage",
      params: { agent, message: { messageId: "m" + Date.now(), role: "ROLE_USER",
                                  parts: [{ text: prompt }] } },
    }),
  }).then(async (resp) => {
    if (!resp.ok) throw new Error(resp.status + " " + resp.statusText);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      // SSE 以空行分帧。必须自己按 "\n\n" 切 —— 一个 chunk 里可能有多帧，
      // 也可能只有半帧，直接 JSON.parse 整块会漏事件。
      while ((i = buf.indexOf("\n\n")) >= 0) {
        handleFrame(buf.slice(0, i), t0, agent);
        buf = buf.slice(i + 2);
      }
    }
    $("#live-status").textContent = "流已结束。";
  }).catch((e) => {
    if (e.name !== "AbortError") $("#live-status").textContent = "出错：" + e.message;
  }).finally(() => {
    liveAbort = null;
    $("#live-start").disabled = false;
    $("#live-stop").disabled = true;
  });
}

function stopLive() {
  if (liveAbort) { liveAbort.abort(); liveAbort = null; }
  $("#live-start").disabled = false;
  $("#live-stop").disabled = true;
}

$("#live-start").onclick = startLive;
$("#live-stop").onclick = () => { stopLive(); $("#live-status").textContent = "已断开。"; };
$("#live-prompt").onkeydown = (e) => { if (e.key === "Enter") startLive(); };

function switchView(v) {
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll("section").forEach(s => s.classList.toggle("on", s.id === "view-" + v));
}

document.querySelectorAll("nav button").forEach(b => {
  b.onclick = () => switchView(b.dataset.view);
});
$("#refresh").onclick = () => loadAll();
$("#probe").onclick = async () => {
  $("#probe-msg").textContent = "探测中…";
  try {
    const r = await getJSON("/admin/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const ok = Object.values(r.probed || {}).filter(v => v === "ok").length;
    $("#probe-msg").textContent = `完成：${ok}/${Object.keys(r.probed || {}).length} 健康`;
    await loadAll();
  } catch (e) {
    $("#probe-msg").textContent = "探测失败：" + e.message;
  }
};

let timer = null;
$("#auto").onchange = (e) => {
  if (e.target.checked) timer = setInterval(loadAll, 5000);
  else { clearInterval(timer); timer = null; }
};

loadAll();
</script>
</body>
</html>
"""


def render_console() -> str:
    return CONSOLE_HTML
