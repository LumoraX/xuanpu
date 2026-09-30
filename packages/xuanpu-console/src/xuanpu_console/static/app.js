// xuanpu 控制台：直接调用平台的 HTTP 接口。没有构建步骤，没有依赖。
"use strict";

const $ = (id) => document.getElementById(id);

// ---------- 连接 ----------

const conn = {
  baseUrl: localStorage.getItem("xuanpu.baseUrl") || "",
  apiKey: sessionStorage.getItem("xuanpu.apiKey") || "",
};

function url(path) {
  return (conn.baseUrl || "").replace(/\/+$/, "") + path;
}

function headers(extra) {
  return { Authorization: "Bearer " + conn.apiKey, ...(extra || {}) };
}

async function api(method, path, options = {}) {
  const init = { method, headers: headers(options.headers) };
  if (options.json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.json);
  } else if (options.body !== undefined) {
    init.body = options.body;
  }
  const response = await fetch(url(path), init);
  const text = await response.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  if (!response.ok) {
    const error = body.error || { code: "http_" + response.status, message: text.slice(0, 200) };
    throw Object.assign(new Error(error.message), { code: error.code, details: error });
  }
  return body;
}

function toast(message, kind) {
  const box = $("toast");
  box.textContent = message;
  box.className = "toast " + (kind || "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 5000);
}

function fail(error) {
  console.error(error);
  toast(error.message || String(error));
}

function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "";
}

$("connect").addEventListener("submit", async (event) => {
  event.preventDefault();
  conn.baseUrl = $("base-url").value.trim();
  conn.apiKey = $("api-key").value.trim();
  localStorage.setItem("xuanpu.baseUrl", conn.baseUrl);
  // 凭据只留在本标签页的会话里，关掉页面即忘
  sessionStorage.setItem("xuanpu.apiKey", conn.apiKey);
  const status = $("conn-status");
  try {
    const health = await api("GET", "/healthz");
    status.textContent = "已连接 · " + health.instance;
    status.className = "status ok";
    await loadSessions();
    await loadPackages();
  } catch (error) {
    status.textContent = "连接失败：" + error.message;
    status.className = "status bad";
  }
});

$("base-url").value = conn.baseUrl;
$("api-key").value = conn.apiKey;

// ---------- 标签页 ----------

$("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  for (const item of $("tabs").querySelectorAll("button")) item.classList.toggle("active", item === button);
  for (const section of document.querySelectorAll(".tab")) {
    section.classList.toggle("active", section.id === "tab-" + button.dataset.tab);
  }
  if (button.dataset.tab === "usage" && !$("usage-since").value) {
    const today = new Date().toISOString().slice(0, 10);
    $("usage-until").value = today;
    $("usage-since").value = new Date(Date.now() - 29 * 86400000).toISOString().slice(0, 10);
    loadUsage().catch(fail);
  }
});

// ---------- 会话 ----------

const chat = { sessionId: null, status: null };

async function loadSessions() {
  const status = $("show-closed").checked ? "" : "&status=active";
  const body = await api("GET", "/v1/sessions?limit=100" + status);
  const list = $("session-list");
  list.innerHTML = "";
  for (const session of body.sessions) {
    const item = document.createElement("li");
    item.dataset.id = session.session_id;
    item.classList.toggle("active", session.session_id === chat.sessionId);
    item.innerHTML =
      `<div>${escape(session.package)} <span class="tag ${session.status === "active" ? "ok" : ""}">${session.status}</span></div>` +
      `<div class="meta">${escape(session.session_id)}</div>` +
      `<div class="meta">${escape(session.entry)} · ${when(session.updated_at)}</div>`;
    item.addEventListener("click", () => openSession(session));
    list.appendChild(item);
  }
}

$("refresh-sessions").addEventListener("click", () => loadSessions().catch(fail));
$("show-closed").addEventListener("change", () => loadSessions().catch(fail));

$("new-session").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const json = { package: $("new-package").value.trim() };
    const entry = $("new-entry").value.trim();
    if (entry) json.entry = entry;
    const session = await api("POST", "/v1/sessions", { json });
    await loadSessions();
    openSession(session);
  } catch (error) { fail(error); }
});

function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function append(kind, text) {
  const node = document.createElement("div");
  node.className = "msg " + kind;
  node.textContent = text;
  $("transcript").appendChild(node);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  return node;
}

async function openSession(session) {
  chat.sessionId = session.session_id;
  chat.status = session.status;
  for (const item of $("session-list").children) item.classList.toggle("active", item.dataset.id === session.session_id);
  $("chat-head").innerHTML =
    `<span>${escape(session.package)} ${escape(session.version)} · ${escape(session.entry)} <span class="mono">${escape(session.session_id)}</span></span>` +
    `<span><button type="button" id="close-session" class="danger" ${session.status !== "active" ? "disabled" : ""}>关闭会话</button></span>`;
  $("close-session").addEventListener("click", closeSession);
  $("transcript").innerHTML = "";
  const active = session.status === "active";
  $("message").disabled = !active;
  $("send-btn").disabled = !active;
  await loadHistory(session.session_id);
  await loadPending();
}

async function loadHistory(sessionId) {
  // 从审计里把这个会话的对话重建出来：用户消息看 message_sent，回复看 turn_text；
  // 工具调用与挂起作为事件行
  const params = `session_id=${encodeURIComponent(sessionId)}&limit=1000`;
  let after = 0;
  const records = [];
  for (;;) {
    const page = await api("GET", `/v1/audit?${params}&after_id=${after}`);
    records.push(...page.records);
    if (page.next_after_id == null) break;
    after = page.next_after_id;
  }
  for (const record of records) {
    if (record.kind === "session_created") append("event", "会话创建 · " + when(record.at));
    else if (record.kind === "turn_started") append("event", "— 一轮对话 " + when(record.at) + " —");
    else if (record.kind === "message_sent" && record.data.text) append("user", record.data.text);
    else if (record.kind === "tool_call_completed") append("event", `工具 ${record.data.name}${record.data.is_error ? " ✗" : " ✓"} ${record.data.duration_ms ?? ""}ms`);
    else if (record.kind === "turn_text" && record.data.text) append("assistant", record.data.text);
    else if (record.kind === "turn_suspended") append("event", "等待审批");
    else if (record.kind === "turn_failed") append("error", "本轮失败：" + (record.data.error || ""));
    else if (record.kind === "suspension_resolved_by") append("event", "审批结果已送回");
  }
}

async function loadPending() {
  const box = $("pending");
  if (!chat.sessionId) { box.classList.add("hidden"); return; }
  const body = await api("GET", `/v1/sessions/${chat.sessionId}/pending`);
  box.innerHTML = "";
  if (!body.pending.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  for (const item of body.pending) {
    const card = document.createElement("div");
    card.className = "card";
    const detail = item.detail || {};
    const summary = detail.target ? `${detail.scope || ""} ${detail.target}` : item.reason;
    card.innerHTML =
      `<div><strong>${escape(item.reason === "permission" ? "审批" : item.reason)}</strong> ${escape(summary)}<br>` +
      `<code>${escape(JSON.stringify(detail.args ?? detail, null, 1))}</code></div>` +
      `<div><button type="button" data-grant="true">批准</button> <button type="button" data-grant="false" class="ghost">拒绝</button></div>`;
    for (const button of card.querySelectorAll("button")) {
      button.addEventListener("click", () => resolve(item.request_id, button.dataset.grant === "true"));
    }
    box.appendChild(card);
  }
}

async function resolve(requestId, granted) {
  const resolutions = {};
  resolutions[requestId] = { granted, reason: granted ? "控制台批准" : "控制台拒绝" };
  append("event", (granted ? "批准" : "拒绝") + " " + requestId);
  await streamTurn(`/v1/sessions/${chat.sessionId}/resume`, { resolutions });
}

$("send").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text || !chat.sessionId) return;
  $("message").value = "";
  append("user", text);
  await streamTurn(`/v1/sessions/${chat.sessionId}/messages`, { text });
});

$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("send").requestSubmit();
  }
});

async function streamTurn(path, json) {
  $("send-btn").disabled = true;
  $("pending").classList.add("hidden");
  let bubble = null;
  try {
    const response = await fetch(url(path), {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(json),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error((body.error && body.error.message) || ("HTTP " + response.status));
    }
    for await (const [name, data] of sse(response)) {
      if (name === "assistant_text") {
        if (!bubble) bubble = append("assistant", "");
        bubble.textContent += data.data.delta || "";
        $("transcript").scrollTop = $("transcript").scrollHeight;
      } else if (name === "tool_call_started") {
        bubble = null;
        append("event", `调用工具 ${data.data.name}`);
      } else if (name === "tool_call_completed") {
        append("event", `工具 ${data.data.name}${data.data.is_error ? " ✗" : " ✓"} ${data.data.duration_ms ?? ""}ms`);
      } else if (name === "turn_suspended") {
        append("event", "等待审批");
      } else if (name === "turn_failed") {
        append("error", "本轮失败：" + (data.data.error || ""));
      }
    }
  } catch (error) {
    fail(error);
  } finally {
    $("send-btn").disabled = chat.status !== "active";
    await loadPending().catch(fail);
    await loadSessions().catch(fail);
  }
}

// 解析 SSE：一帧一个 [事件名, 数据]
async function* sse(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = null;
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data.push(line.slice(6));
      }
      if (name) yield [name, data.length ? JSON.parse(data.join("\n")) : {}];
    }
  }
}

async function closeSession() {
  if (!chat.sessionId || !confirm("关闭这个会话？进行中的对话会被取消。")) return;
  try {
    const session = await api("DELETE", `/v1/sessions/${chat.sessionId}`);
    await loadSessions();
    await openSession(session);
  } catch (error) { fail(error); }
}

// ---------- 业务包 ----------

async function loadPackages() {
  const body = await api("GET", "/v1/packages");
  const rows = $("package-table").querySelector("tbody");
  rows.innerHTML = "";
  for (const item of body.packages) {
    const row = document.createElement("tr");
    row.innerHTML =
      `<td>${escape(item.name)}</td><td class="mono">${escape(item.version)}</td>` +
      `<td>${item.active ? '<span class="tag ok">生效</span>' : ""}</td>` +
      `<td>${when(item.published_at)}</td><td class="mono">${escape(item.published_by)}</td>` +
      `<td class="mono">${escape(item.digest.slice(7, 19))}</td>` +
      `<td><button type="button" class="ghost" data-act="activate" ${item.active ? "disabled" : ""}>设为生效</button> ` +
      `<button type="button" class="ghost" data-act="eval">评测</button></td>`;
    row.querySelector('[data-act="activate"]').addEventListener("click", async () => {
      try {
        await api("POST", `/v1/packages/${item.name}/activate`, { json: { version: item.version } });
        await loadPackages();
      } catch (error) { fail(error); }
    });
    row.querySelector('[data-act="eval"]').addEventListener("click", async () => {
      const report = $("eval-report");
      report.classList.remove("hidden");
      report.textContent = `正在评测 ${item.name} ${item.version}…`;
      try {
        const result = await api("POST", `/v1/packages/${item.name}/evals`, { json: { version: item.version } });
        const lines = [`${result.package} ${result.version}：${result.total} 个用例，${result.failed} 个未通过，共 ${result.total_tokens} token`];
        for (const c of result.cases) {
          lines.push(`  [${c.passed ? "通过" : "未通过"}] ${c.name}  ${c.outcome}  ${c.total_tokens} token  ${c.duration_ms} ms`);
          for (const reason of c.reasons) lines.push(`      - ${reason}`);
          if (c.reply) lines.push(`      回复：${c.reply.slice(0, 200)}`);
        }
        report.textContent = lines.join("\n");
      } catch (error) { report.textContent = "评测失败：" + error.message; }
    });
    rows.appendChild(row);
  }
}

$("publish").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("archive").files[0];
  if (!file) return;
  const status = $("publish-status");
  status.textContent = "上传中…";
  status.className = "status";
  try {
    const activate = $("publish-activate").checked ? "true" : "false";
    const result = await api("POST", `/v1/packages?activate=${activate}`, {
      body: await file.arrayBuffer(),
      headers: { "Content-Type": "application/zip" },
    });
    status.textContent = `已发布 ${result.name} ${result.version}`;
    status.className = "status ok";
    await loadPackages();
  } catch (error) {
    const problems = (error.details && error.details.problems) || [];
    status.textContent = error.message + (problems.length ? "：" + problems.join("；") : "");
    status.className = "status bad";
  }
});

// ---------- 审计 ----------

const audit = { after: 0 };

async function loadAudit(reset) {
  if (reset) {
    audit.after = 0;
    $("audit-table").querySelector("tbody").innerHTML = "";
  }
  const params = new URLSearchParams({ limit: "50", after_id: String(audit.after) });
  if ($("audit-session").value.trim()) params.set("session_id", $("audit-session").value.trim());
  if ($("audit-kind").value.trim()) params.set("kind", $("audit-kind").value.trim());
  if ($("audit-source").value) params.set("source", $("audit-source").value);
  const body = await api("GET", "/v1/audit?" + params.toString());
  const rows = $("audit-table").querySelector("tbody");
  for (const record of body.records) {
    const row = document.createElement("tr");
    row.innerHTML =
      `<td class="mono">${record.audit_id}</td><td>${when(record.at)}</td>` +
      `<td><span class="tag">${escape(record.source)}</span></td><td class="mono">${escape(record.kind)}</td>` +
      `<td class="mono">${escape(record.actor)}</td><td class="mono">${escape(record.session_id || "")}</td>` +
      `<td class="data">${escape(JSON.stringify(record.data))}</td>`;
    rows.appendChild(row);
  }
  audit.after = body.next_after_id;
  $("audit-more").disabled = body.next_after_id == null;
}

$("audit-filter").addEventListener("submit", (event) => { event.preventDefault(); loadAudit(true).catch(fail); });
$("audit-more").addEventListener("click", () => loadAudit(false).catch(fail));

// ---------- 用量 ----------

async function loadUsage() {
  const since = $("usage-since").value;
  const until = $("usage-until").value;
  const body = await api("GET", `/v1/usage?since=${since}&until=${until}`);
  const rows = $("usage-table").querySelector("tbody");
  rows.innerHTML = "";
  let total = { turns: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 };
  for (const day of body.usage) {
    const row = document.createElement("tr");
    row.innerHTML = `<td class="mono">${day.day}</td><td>${day.turns}</td><td>${day.input_tokens}</td><td>${day.output_tokens}</td><td>${day.total_tokens}</td>`;
    rows.appendChild(row);
    for (const key of Object.keys(total)) total[key] += day[key];
  }
  const sum = document.createElement("tr");
  sum.innerHTML = `<td><strong>合计</strong></td><td>${total.turns}</td><td>${total.input_tokens}</td><td>${total.output_tokens}</td><td>${total.total_tokens}</td>`;
  rows.appendChild(sum);
}

$("usage-filter").addEventListener("submit", (event) => { event.preventDefault(); loadUsage().catch(fail); });

// 已经有凭据时自动连接
if (conn.apiKey) $("connect").requestSubmit();
