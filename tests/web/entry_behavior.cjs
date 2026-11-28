// 运行真实的极简入口脚本：概览、发送、确认卡、列表卡、进展卡、刷新恢复、身份切换与系统状态。
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element(tag = "div") {
  const classes = new Set();
  const node = {
    tag, children: [], listeners: {}, dataset: {}, style: {}, attrs: {}, value: "", disabled: false, parentNode: null,
    _text: "", rows: 0, href: "", type: "",
    get textContent() { return this._text + this.children.map(c => c.textContent).join(""); },
    set textContent(value) { this._text = String(value); this.children = []; },
    get className() { return [...classes].join(" "); },
    set className(value) { classes.clear(); for (const c of String(value).split(/\s+/)) if (c) classes.add(c); },
    classList: {
      add: v => classes.add(v), remove: v => classes.delete(v), contains: v => classes.has(v),
      toggle: (v, yes) => { const on = yes === undefined ? !classes.has(v) : yes; if (on) classes.add(v); else classes.delete(v); return on; },
    },
    appendChild(child) { if (child.parentNode) child.remove(); this.children.push(child); child.parentNode = this; return child; },
    insertBefore(child, ref) {
      if (child.parentNode) child.remove();
      const idx = this.children.indexOf(ref);
      if (idx === -1) this.children.push(child);
      else this.children.splice(idx, 0, child);
      child.parentNode = this;
      return child;
    },
    append(...items) { for (const item of items) this.appendChild(item); },
    replaceChildren(...items) { for (const c of this.children) c.parentNode = null; this.children = []; this.append(...items); },
    remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(n => n !== this); this.parentNode = null; } },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() {}, scrollIntoView() {}, requestSubmit() { this.listeners.submit({ preventDefault() {} }); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
  };
  return node;
}

// 深度遍历：按标签与文本找元素。
function all(root, predicate, found = []) {
  for (const child of root.children) { if (predicate(child)) found.push(child); all(child, predicate, found); }
  return found;
}
const byText = (root, tag, text) => all(root, n => n.tag === tag && n.textContent.includes(text));

function sse(frames) {
  const text = frames.map(([name, data]) => `id: 1\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join("");
  const chunks = [Buffer.from(": heartbeat\n\n" + text)];
  return { getReader: () => ({ read: async () => chunks.length ? { value: chunks.shift(), done: false } : { done: true } }) };
}

function harness(routes) {
  const nodes = new Map(), calls = [];
  const node = id => { if (!nodes.has(id)) nodes.set(id, element(id === "composer" ? "form" : "div")); return nodes.get(id); };
  const fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    let parsedBody;
    if (typeof init.body === "string") {
      try { parsedBody = JSON.parse(init.body); } catch { parsedBody = init.body; }
    } else if (init.body !== undefined) {
      parsedBody = init.body;
    }
    const call = { method, url, body: parsedBody, headers: init.headers || {} };
    calls.push(call);
    const handler = routes[method + " " + url] || routes[method + " " + url.split("?")[0]];
    if (!handler) throw new Error("未预期的请求 " + method + " " + url);
    const result = await handler(call, init);
    const status = result.status || 200;
    const headers = { get: name => (result.headers || {})[name.toLowerCase()] || null };
    const rawBytes = result.bytes !== undefined
      ? Buffer.from(result.bytes)
      : (result.text !== undefined ? Buffer.from(String(result.text), "utf8") : null);
    return {
      ok: status < 400,
      status,
      headers,
      text: async () => (result.text !== undefined ? String(result.text) : JSON.stringify(result.json || {})),
      arrayBuffer: async () => {
        const buf = rawBytes || Buffer.from(JSON.stringify(result.json || {}), "utf8");
        return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
      },
      body: result.stream,
    };
  };
  const context = vm.createContext({
    console: { error() {}, log: console.log }, crypto, AbortController, DOMException, TextDecoder, TextEncoder, URL, URLSearchParams, Uint8Array, Intl,
    document: { getElementById: node, createElement: element, createElementNS: (_, tag) => element(tag) }, fetch, Math, JSON, Date, Promise, Map, Set,
    location: { search: "", hash: "", pathname: "/", assign() {}, replaced: null, replace(to) { this.replaced = to; } },
  });
  return { context, nodes: node, calls };
}

function load(h, dir) {
  for (const name of ["entry.js", "plugin-host.js", "builtin-plugins.js", "bubble-format.js", "entry-attachments.js", "entry-notifications.js", "capture-card.js", "people-graph.js", "contacts-drawer.js", "board-drawer.js", "workbench-drawer.js", "cards.js", "module-cards.js", "collab-cards.js", "progress-card.js", "dashboard.js", "tips.js", "entry-boot.js"]) {
    vm.runInContext(fs.readFileSync(path.join(dir, name), "utf8"), h.context, { filename: name });
  }
}

const settle = () => new Promise(resolve => setImmediate(resolve));
async function flush() { for (let i = 0; i < 20; i++) await settle(); }

const ME = { json: { user_id: "usr_alice", tenant_id: "acme", display_name: "Alice", entry: "minimal" } };
const OVERVIEW = { json: { blocks: [
  { key: "today", status: "ok", count: 2, items: [{ kind: "todo", ref: "ws_1", title: "周报", at: 1 }] },
  { key: "waiting", status: "error", error: "runtime_unavailable", count: null, items: [] },
  { key: "unread", status: "ok", count: 0, items: [] },
], tips: [{ text: "试试：明天提醒我开会", prompt: "明天上午9点提醒我开会" }] } };

function pendingStream(init) {
  return new Promise((_, reject) => {
    if (init && init.signal) {
      if (init.signal.aborted) { reject(new DOMException("aborted", "AbortError")); return; }
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }
  });
}

function base(extra = {}) {
  return {
    "GET /v1/me": () => ME,
    "GET /v1/workspace/messages": () => ({ json: { items: [] } }),
    "GET /v1/dashboard": () => OVERVIEW,
    "GET /v1/notifications": () => ({ json: { items: [] } }),
    "GET /v1/notifications/events": (_call, init) => pendingStream(init),
    ...extra,
  };
}

async function overviewAndTips(dir) {
  const h = harness(base());
  load(h, dir); await flush();
  const overview = h.nodes("overview");
  assert.equal(overview.children.length, 3);
  assert.ok(overview.children[1].textContent.includes("暂时读不出"));
  assert.ok(overview.children[0].textContent.includes("2"));
  assert.ok(!h.nodes("entry-main").classList.contains("hidden"));
  const tip = h.nodes("tips").children[0];
  tip.listeners.click();
  assert.equal(h.nodes("message").value, "明天上午9点提醒我开会");
  assert.ok(h.calls.some(c => c.url.startsWith("/v1/dashboard?timezone=")));
}

async function sendAndConfirm(dir) {
  const draft = { title: "开会", due_at: 2000000000, timezone: "UTC", repeat: "once", channels: ["inbox"], request_id: "x" };
  const h = harness(base({
    "POST /v1/assistant/messages": call => ({ json: { mode: "butler", message: "请确认提醒", reminder_draft: draft, proposal_id: call.body.request_id } }),
    "POST /v1/assistant/proposals/web-fixed/confirm": () => ({ json: { state: "confirmed" } }),
  }));
  h.context.crypto = { randomUUID: () => "fixed" };
  load(h, dir); await flush();
  h.nodes("message").value = "明天提醒我开会";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  const sent = h.calls.find(c => c.url === "/v1/assistant/messages");
  assert.equal(sent.body.request_id, "web-fixed");
  assert.equal(sent.body.text, "明天提醒我开会");
  assert.equal(sent.headers["X-Requested-By"], "xuanpu");
  const transcript = h.nodes("transcript");
  const confirm = byText(transcript, "button", "确认")[0];
  confirm.listeners.click(); await flush();
  const confirmed = h.calls.find(c => c.url.endsWith("/confirm"));
  assert.deepEqual(confirmed.body, { data: { title: "开会", due_at: 2000000000, timezone: "UTC", repeat: "once", channels: ["inbox"] } });
  assert.ok(transcript.textContent.includes("已保存"));
  assert.equal(byText(transcript, "button", "确认").length, 0);
}

async function listCardUsesVersion(dir) {
  const todo = { id: "ws_1", version: 3, data: { title: "周报", status: "open" } };
  let done = false;
  const h = harness(base({
    "POST /v1/assistant/messages": () => ({ json: { mode: "butler", message: "你的待办", open_workspace: "todo" } }),
    "GET /v1/workspace/items": () => ({ json: { items: done ? [] : [todo] } }),
    "PUT /v1/workspace/items/ws_1": () => { done = true; return { json: {} }; },
  }));
  load(h, dir); await flush();
  h.nodes("message").value = "我的待办";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  const transcript = h.nodes("transcript");
  byText(transcript, "button", "标记完成")[0].listeners.click({ target: element("button") }); await flush();
  const put = h.calls.find(c => c.method === "PUT");
  assert.deepEqual(put.body, { data: { title: "周报", status: "done" }, version: 3 });
  assert.ok(transcript.textContent.includes("暂无记录"));
}

async function progressFromHistoryAndRestore(dir) {
  const history = [
    { role: "user", text: "查工单并总结", request_id: "r1:user", metadata: {} },
    { role: "assistant", text: "已交给工单匠人", request_id: "r1:assistant",
      metadata: { route: { reminder_draft: { title: "跟进", due_at: 1, timezone: "UTC" }, proposal_id: "r1" }, butler: { trace_id: "trc_1" } } },
  ];
  const spans = [
    { span_id: "ex.a", parent_span_id: null, kind: "butler", name: "管家", state: "running" },
    { span_id: "dp.1", parent_span_id: "ex.a", kind: "dispatch", name: "工单", state: "waiting_input" },
  ];
  const h = harness(base({
    "GET /v1/workspace/messages": () => ({ json: { items: history } }),
    "GET /v1/assistant/proposals/r1": () => ({ json: { state: "confirmed" } }),
    "GET /v1/traces/trc_1/events": () => ({ stream: sse([["snapshot", { title: "查工单", active: true, incomplete: true, spans }], ["end", { title: "查工单", active: true, incomplete: true }]]) }),
    "GET /v1/traces/trc_1/waterfall": () => ({ json: { rows: [
      { span_id: "ex.a", name: "管家", depth: 0, offset_ms: 0, duration_ms: null },
      { span_id: "dp.1", name: "工单", depth: 1, offset_ms: 10, duration_ms: 40 }] } }),
  }));
  load(h, dir); await flush();
  const transcript = h.nodes("transcript");
  assert.ok(transcript.textContent.includes("已保存"), "刷新后草稿按持久状态恢复");
  assert.equal(byText(transcript, "button", "确认").length, 0);
  assert.ok(transcript.textContent.includes("记录不完整"));
  assert.ok(transcript.textContent.includes("等你回复"));
  const tree = all(transcript, n => n.className.includes("tree"))[0];
  assert.equal(all(tree, n => n.tag === "li").length, 2);
  assert.ok(all(tree, n => n.tag === "li")[0].textContent.includes("派工 · 工单"), "派工嵌在管家下");
  byText(transcript, "button", "看瀑布")[0].listeners.click(); await flush();
  const bars = all(transcript, n => n.className.startsWith("bar"));
  assert.equal(bars.length, 2);
  assert.ok(bars[0].className.includes("running"));
}

// R7：回执以流到达——第一帧就开进展卡，回复逐段出现，最后以回执为准。
async function streamingReply(dir) {
  const h = harness(base({
    "POST /v1/assistant/messages": () => ({ headers: { "content-type": "text/event-stream" }, stream: sse([
      ["started", { mode: "butler", request_id: "x", trace_id: "trc_s" }],
      ["delta", { text: "你好，" }], ["delta", { text: "我在" }],
      ["receipt", { mode: "butler", state: "done", message: "你好，我在。", trace_id: "trc_s" }]]) }),
    "GET /v1/traces/trc_s/events": () => ({ stream: sse([["snapshot", { title: "问候", active: false, spans: [] }], ["end", {}]]) }),
  }));
  load(h, dir); await flush();
  h.nodes("message").value = "你好";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  const sent = h.calls.find(c => c.url === "/v1/assistant/messages");
  assert.ok(sent.headers.Accept.includes("text/event-stream"), "要流");
  assert.ok(h.calls.some(c => c.url === "/v1/traces/trc_s/events"), "第一帧就订阅进展");
  const replies = all(h.nodes("transcript"), n => n.className.startsWith("msg assistant"));
  assert.equal(replies.length, 1);
  assert.equal(replies[0].textContent, "你好，我在。", "回复以回执为准");
  assert.ok(!replies[0].className.includes("streaming"));
}

async function streamingErrorKeepsInput(dir) {
  const h = harness(base({
    "POST /v1/assistant/messages": () => ({ headers: { "content-type": "text/event-stream" }, stream: sse([
      ["started", { mode: "butler", request_id: "x", trace_id: null }],
      ["error", { code: "runtime_unavailable", message: "管家暂时无法处理，输入已保留，请稍后重试" }]]) }),
  }));
  load(h, dir); await flush();
  h.nodes("message").value = "帮我办件事";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  h.nodes("message").value = "";
  await flush();
  assert.equal(h.nodes("message").value, "帮我办件事", "失败时原话回到输入框");
  assert.equal(all(h.nodes("transcript"), n => n.className.startsWith("msg assistant")).length, 0, "没写完的回复气泡撤掉");
}

async function staleResultsAreDropped(dir) {
  let release;
  const h = harness(base({
    "POST /v1/assistant/messages": () => new Promise(resolve => { release = () => resolve({ json: { message: "迟到的答复" } }); }),
    "POST /v1/auth/logout": () => ({ json: {} }),
  }));
  load(h, dir); await flush();
  h.nodes("message").value = "你好";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  h.nodes("entry-logout").listeners.click(); await flush();
  assert.equal(h.nodes("transcript").children.length, 0);
  assert.equal(h.nodes("overview").children.length, 0);
  release(); await flush();
  assert.equal(h.nodes("transcript").children.length, 0, "旧身份的异步结果不回填");
  assert.ok(!h.nodes("entry-login").classList.contains("hidden"));
}

async function unavailableKeepsInput(dir) {
  const h = harness(base({
    "POST /v1/assistant/messages": () => ({ status: 503, json: { error: { code: "runtime_unavailable", message: "管家暂时无法处理" } } }),
  }));
  load(h, dir); await flush();
  h.nodes("message").value = "帮我查工单";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  assert.equal(h.nodes("message").value, "帮我查工单");
  assert.ok(h.nodes("tips").textContent.includes("管家暂时不可用"));
  assert.ok(h.nodes("tips").textContent.includes("更多"));
}

// 管家常在线：慢的一句先回「进行中」、不锁输入；接着说的一句照常回复；之后按游标补读出慢的那句的回复，
// 已显示过的不重复。
async function runningReplyFollowsLater(dir) {
  const timers = [];
  let later = [];
  const h = harness(base({
    "POST /v1/assistant/messages": call => call.body.text === "整理全年报表"
      ? { json: { mode: "butler", state: "running", request_id: call.body.request_id, trace_id: "trc_9" } }
      : { json: { mode: "butler", state: "done", message: "今天周二。" } },
    "GET /v1/workspace/messages": call => ({ json: { items: call.url.includes("after_id=") ? later : [] } }),
    "GET /v1/traces/trc_9/events": () => ({ stream: sse([["snapshot", { title: "整理报表", active: true, spans: [] }]]) }),
  }));
  h.context.setTimeout = fn => { timers.push(fn); return timers.length; };
  h.context.clearTimeout = () => {};
  let n = 0;
  h.context.crypto = { randomUUID: () => "id" + (++n) };
  load(h, dir); await flush();
  const send = async text => { h.nodes("message").value = text; h.nodes("composer").listeners.submit({ preventDefault() {} }); await flush(); };
  await send("整理全年报表");
  const transcript = h.nodes("transcript");
  assert.ok(transcript.textContent.includes("正在处理"));
  await send("今天周几");
  assert.ok(transcript.textContent.includes("今天周二。"), "进行中时照常交代下一件事");
  later = [
    { id: 7, role: "user", text: "今天周几", request_id: "web-id2:user", metadata: {} },
    { id: 8, role: "assistant", text: "今天周二。", request_id: "web-id2:assistant", metadata: {} },
    { id: 9, role: "assistant", text: "全年报表整理好了。", request_id: "web-id1:assistant", metadata: {} },
  ];
  await timers.shift()(); await flush();
  assert.ok(transcript.textContent.includes("全年报表整理好了。"));
  assert.equal(byText(transcript, "div", "今天周二。").length, 1, "已显示过的不重复");
  later = [];
  await timers.shift()(); await flush();
  assert.ok(h.calls.some(c => c.url.includes("after_id=9")), "游标前移");
}

async function classicTenantGoesBack(dir) {
  const h = harness(base({ "GET /v1/me": () => ({ json: { ...ME.json, entry: "classic" } }) }));
  load(h, dir); await flush();
  assert.equal(h.context.location.replaced, "/workspace");
  assert.ok(h.nodes("entry-main").classList.contains("hidden"));
  assert.ok(!h.calls.some(c => c.url.startsWith("/v1/dashboard")), "未启用的租户不读概览");
}

// 发一句话，让管家回执渲染成卡片。
async function routeTo(routes, reply) {
  const h = harness(base({ "POST /v1/assistant/messages": () => ({ json: { mode: "butler", message: "好的", ...reply } }), ...routes }));
  h.context.crypto = { randomUUID: () => "fixed", subtle: crypto.webcrypto.subtle };
  load(h, dir_); await flush();
  h.nodes("message").value = "处理一下";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  return h;
}
let dir_ = null;
// 网页加密摘要在线程池里完成，等按钮出现再点。
async function click(h, text, index = 0) {
  for (let i = 0; i < 50 && byText(h.nodes("transcript"), "button", text).length <= index; i++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  byText(h.nodes("transcript"), "button", text)[index].listeners.click();
  await flush();
  await new Promise(resolve => setTimeout(resolve, 5));
  await flush();
}
const posted = (h, url) => h.calls.filter(c => c.method === "POST" && c.url === url);

async function proactiveDraftRetriesSameRequest() {
  let failures = 1;
  const h = await routeTo({
    "GET /v1/proactive/pa_1": () => ({ json: { id: "pa_1", version: 2, item_version: 5, title: "回访客户" } }),
    "POST /v1/proactive/pa_1/actions": () => failures-- > 0 ? { status: 503, json: {} } : { json: {} },
  }, { proactive_draft: { id: "pa_1", version: 2, action: "complete" }, proposal_id: "p0" });
  await click(h, "确认完成");
  await click(h, "重试同一操作");
  const calls = posted(h, "/v1/proactive/pa_1/actions");
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].body, calls[1].body, "重试复用同一请求");
  assert.equal(calls[0].body.version, 2);
  assert.equal(calls[0].body.item_version, 5);
  assert.equal(calls[0].body.action, "complete");
  assert.ok(h.nodes("transcript").textContent.includes("已处理"));
}

async function definitionDraftAndList() {
  const request = { request_id: "td-1", title: "周报", template: { text: "汇总" }, trigger: { kind: "time", repeat: "once", at: "2030-01-01T09:00:00+08:00" },
    timezone: "Asia/Shanghai", max_executions: 1, max_tokens: 1000, valid_until: "2030-02-01T00:00:00+08:00" };
  let h = await routeTo({ "POST /v1/task-definitions": () => ({ json: { title: "周报" } }) },
    { task_definition_draft: { request, display: {} }, proposal_id: "td-1" });
  await click(h, "确认启用安排");
  assert.deepEqual(posted(h, "/v1/task-definitions")[0].body, { ...request, confirmed: true });
  h = await routeTo({
    "GET /v1/task-definitions": () => ({ json: { items: [{ definition_id: "d1", title: "周报", enabled: true, cancelled: false, state_version: 4 }] } }),
    "POST /v1/task-definitions/d1/pause": () => ({ json: {} }),
  }, { open_task_definitions: true });
  await click(h, "暂停");
  assert.deepEqual(posted(h, "/v1/task-definitions/d1/pause")[0].body, { request_id: "web-fixed", expected_version: 4, stop_running: false });
}

async function connectionConsentAndRevoke() {
  const h = await routeTo({
    "GET /v1/me/connections": () => ({ json: { enabled: true, connections: [{ id: "wecom", name: "企业微信", status: "active",
      operations: ["self_profile.read"], consents: [{ consent_id: "c1", status: "active", version: 2, purpose: "核对" }] }] } }),
    "POST /v1/me/connections/wecom/consents/c1/revoke": () => ({ json: {} }),
    "POST /v1/me/connections/wecom/consents": () => ({ json: {} }),
  }, { open_connections: true });
  await click(h, "撤销这项授权");
  assert.deepEqual(posted(h, "/v1/me/connections/wecom/consents/c1/revoke")[0].body, { request_id: "web-fixed", expected_version: 2 });
  await click(h, "同意读取本人资料");
  const consent = posted(h, "/v1/me/connections/wecom/consents")[0].body;
  assert.deepEqual(consent.operations, ["self_profile.read"]);
  assert.equal(consent.purpose, "本人基础资料核对");
}

async function sharedDraftUsesProposalKey() {
  const h = await routeTo({ "POST /v1/shared-matters": () => ({ json: { title: "季度计划" } }) },
    { shared_draft: { title: "季度计划", content: "正文" }, proposal_id: "p1" });
  await click(h, "确认新建");
  const body = posted(h, "/v1/shared-matters")[0].body;
  const digest = crypto.createHash("sha256").update(JSON.stringify({ title: "季度计划", content: "正文" })).digest("hex").slice(0, 32);
  assert.deepEqual(body, { request_id: "p1:shared:" + digest, confirmed: true, title: "季度计划", content: "正文" });
}

async function handoffAndApprovalActions() {
  const h = await routeTo({
    "GET /v1/handoffs": () => ({ json: { items: [{ id: "h1", title: "合同交接" }] } }),
    "GET /v1/approval-requests": () => ({ json: { items: [{ id: "a1", current_step: 0, steps: [{}] }] } }),
    "GET /v1/handoffs/h1": () => ({ json: { id: "h1", version: 3, summary: "请接收", allowed_actions: ["accept", "return", "deliver"] } }),
    "GET /v1/approval-requests/a1": () => ({ json: { id: "a1", version: 7, allowed_actions: ["approve"], decisions: [] } }),
    "POST /v1/handoffs/h1/accept": () => ({ json: {} }),
    "POST /v1/handoffs/h1/return": () => ({ json: {} }),
    "POST /v1/approval-requests/a1/approve": () => ({ json: {} }),
  }, { open_collaboration: true });
  await click(h, "查看与处理", 0);
  await click(h, "退回工作");
  assert.equal(posted(h, "/v1/handoffs/h1/return").length, 0, "必须说明原因");
  assert.ok(h.nodes("transcript").textContent.includes("在「更多」里完成"), "需要选文件的动作指向更多");
  await click(h, "接收这项工作");
  assert.deepEqual(posted(h, "/v1/handoffs/h1/accept")[0].body, { request_id: "web-fixed", confirmed: true, expected_version: 3 });
  await click(h, "查看与处理", 1);
  await click(h, "批准本次固定成果");
  assert.deepEqual(posted(h, "/v1/approval-requests/a1/approve")[0].body,
    { request_id: "web-fixed", confirmed: true, expected_version: 7, reason: "" });
}

async function handoffDraftUsesProposalKey() {
  const h = await routeTo({
    "GET /v1/organization/people": () => ({ json: { items: [{ user_id: "usr_alice", display_name: "Alice" }, { user_id: "usr_bob", display_name: "Bob" }] } }),
    "GET /v1/shared-matters": () => ({ json: { items: [{ id: "sm1", title: "合同", owner_id: "usr_alice", status: "active" }] } }),
    "GET /v1/approval-policies": () => ({ json: { items: [] } }),
    "GET /v1/shared-matters/sm1/materials": () => ({ json: { items: [{ name: "合同.pdf", revisions: [{ id: "rev1", revision: 2 }] }] } }),
    "GET /v1/shared-matters/sm1": () => ({ json: { id: "sm1", version: 9 } }),
    "POST /v1/handoffs": () => ({ json: { title: "合同交接" } }),
  }, { handoff_draft: { title: "合同交接", summary: "请跟进" }, proposal_id: "p2" });
  const box = all(h.nodes("transcript"), n => n.tag === "input" && n.type === "checkbox" && n.dataset.revision === "rev1")[0];
  box.checked = true;
  await click(h, "核对交接");
  await click(h, "确认提交交接");
  const body = posted(h, "/v1/handoffs")[0].body;
  const { request_id, ...rest } = body;
  assert.deepEqual(rest, { confirmed: true, expected_version: 9, shared_id: "sm1", target_kind: "user", target_id: "usr_bob",
    title: "合同交接", summary: "请跟进", include_content: false, material_revision_ids: ["rev1"] });
  assert.equal(request_id, "handoff:p2:" + crypto.createHash("sha256").update(JSON.stringify(rest)).digest("hex"));
}

async function friendlyFormatAndLocationPrompt(dir) {
  const geoCalls = [];
  const h = harness(base({
    "POST /v1/assistant/messages": call => ({
      json: {
        mode: "butler",
        trace_id: "trc_loc",
        message: '「出行规划匠人」已完成，结果如下：\n{\n  "resolved": false,\n  "city": null\n}请补充出发城市。\n- 当前所在城市\n- 具体出发地址\n\n辅助判断（仅供参考，不改变上面的结论）：需要补充出发地。',
      },
    }),
    "GET /v1/traces/trc_loc/events": () => ({ stream: sse([["snapshot", { title: "butler", active: false, spans: [] }], ["end", {}]]) }),
  }));
  h.context.navigator = {
    permissions: { query: async () => ({ state: "prompt" }) },
    geolocation: {
      getCurrentPosition(ok) {
        geoCalls.push(true);
        ok({ coords: { latitude: 31.2304, longitude: 121.4737, accuracy: 35 } });
      },
    },
  };
  load(h, dir); await flush();
  h.nodes("message").value = "从我现在的位置出发到半岛酒店怎么走";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  assert.equal(geoCalls.length, 1, "涉及位置/路线时应主动调用浏览器定位");
  const sent = h.calls.find(c => c.url === "/v1/assistant/messages");
  assert.deepEqual(sent.body.location, { latitude: 31.2304, longitude: 121.4737, accuracy_meters: 35 });
  const text = h.nodes("transcript").textContent;
  assert.ok(!text.includes('"resolved"'), "终端用户气泡不得出现原始 JSON");
  assert.ok(text.includes("请补充出发城市。"));
  assert.ok(text.includes("管家 · 已完成"), "进展条应把内部 butler 显示为管家");
}

async function executedActionsCardAndUndo(dir) {
  let undone = false;
  const routes = base({
    "POST /v1/assistant/messages": call => {
      const reqId = call.body.request_id;
      routes["POST /v1/assistant/actions/" + reqId + "/act_1/undo"] = () => {
        undone = true;
        return { json: { state: "undone", action_id: "act_1", result: { undone: 1, skipped: 0 } } };
      };
      return {
        json: {
          mode: "butler",
          state: "done",
          request_id: reqId,
          message: "已帮你直接整理并建好待办。",
          executed_actions: [
            { action_id: "act_1", action: "create_todo", title: "已新建待办", summary: "明天提交壁仞方案", undoable: true },
          ],
        },
      };
    },
  });
  const h = harness(routes);
  load(h, dir); await flush();
  h.nodes("message").value = "记个待办明天提交壁仞方案";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  const transcript = h.nodes("transcript");
  assert.ok(transcript.textContent.includes("助理已办好"), "自主办理应展示回执卡");
  assert.ok(transcript.textContent.includes("明天提交壁仞方案"));
  const undoBtn = byText(transcript, "button", "撤回")[0];
  assert.ok(undoBtn, "可撤回动作应显示撤回按钮");
  undoBtn.listeners.click();
  await flush();
  assert.equal(undone, true, "点击撤回按钮应调用后端撤回接口");
  assert.ok(transcript.textContent.includes("已撤回"));
}

async function contactsDrawerAddressBook(dir) {
  const routes = base({
    "GET /v1/people": () => ({
      json: {
        items: [
          {
            id: "per_1", name: "袁新", organization: "上海理想信息产业（集团）有限公司", version: 2, has_avatar: false,
            profile: {
              name: "袁新", aliases: [], gender: "male", organization: "上海理想信息产业（集团）有限公司",
              contacts: [{ kind: "phone", label: "手机", value: "18017087533", primary: true }],
              tags: [], summary: "何总的朋友", extra: {},
            },
          },
          {
            id: "per_2", name: "何冠良", organization: "", version: 1, has_avatar: false,
            profile: { name: "何冠良", aliases: ["何总"], gender: "male", contacts: [], tags: [], summary: "", extra: { address: "半岛酒店201" } },
          },
        ],
      },
    }),
    "GET /v1/people/orgs": () => ({
      json: {
        items: [
          { id: "org_1", name: "上海理想信息产业（集团）有限公司", version: 1, profile: { name: "上海理想信息产业（集团）有限公司", aliases: ["理想信息"], city: "上海" } },
        ],
      },
    }),
    "GET /v1/workspace/items": call => {
      if (call.url.includes("kind=file")) return { json: { items: [{ id: "ws_f1", kind: "file", data: { name: "壁仞报价单.pdf", size: 2048 }, trashed_at: null }] } };
      return { json: { items: [{ id: "ws_m1", kind: "matter", data: { title: "壁仞项目", status: "active" }, trashed_at: null }] } };
    },
    "GET /v1/people/notes": () => ({
      json: {
        items: [
          { id: "n_1", record_kind: "meeting", title: "11月22日项目沟通记录", location: "半岛酒店201", occurred_at: 1795276800, content: "沟通壁仞项目", summary: "沟通壁仞项目", links: [{ target_kind: "person", target_id: "per_1" }, { target_kind: "matter", target_id: "ws_m1" }] },
        ],
      },
    }),
    "GET /v1/me/knowledge/sources": () => ({ status: 404, json: {} }),
    "GET /v1/me/work-memory": () => ({ status: 404, json: {} }),
    "GET /v1/people/per_1": () => ({
      json: {
        person: {
          id: "per_1", name: "袁新", organization: "上海理想信息产业（集团）有限公司", version: 2, has_avatar: false,
          profile: {
            name: "袁新", aliases: [], gender: "male", organization: "上海理想信息产业（集团）有限公司",
            contacts: [{ kind: "phone", label: "手机", value: "18017087533", primary: true }],
            tags: [], summary: "何总的朋友", extra: {},
          },
        },
        relations: [
          { id: "rel_1", from_kind: "person", from_person_id: "per_1", target_kind: "person", target_id: "per_2", relation_type: "friend", role: "" },
          { id: "rel_2", from_kind: "person", from_person_id: "per_1", target_kind: "matter", target_id: "ws_m1", relation_type: "matter_role", role: "沟通参与者" },
        ],
        notes: [
          { id: "n_1", record_kind: "meeting", title: "11月22日项目沟通记录", location: "半岛酒店201", occurred_at: 1795276800, content: "沟通壁仞项目", summary: "沟通壁仞项目" },
        ],
      },
    }),
    "GET /v1/people/orgs/org_1": () => ({
      json: {
        org: { id: "org_1", name: "上海理想信息产业（集团）有限公司", version: 1, profile: { name: "上海理想信息产业（集团）有限公司", aliases: ["理想信息"], city: "上海", summary: "上海电信下属公司" } },
        relations: [{ id: "rel_w", from_kind: "person", from_person_id: "per_1", target_kind: "org", target_id: "org_1", relation_type: "works_at" }],
        records: [],
      },
    }),
  });
  const h = harness(routes);
  load(h, dir); await flush();
  h.nodes("entry-contacts").listeners.click();
  await flush();
  const overlay = h.nodes("contacts-overlay");
  assert.ok(!overlay.classList.contains("hidden"), "点击通讯录应打开抽屉");
  assert.ok(overlay.textContent.includes("180 1708 7533"), "名片详情应展示格式化手机号");
  assert.ok(overlay.textContent.includes("何冠良") && overlay.textContent.includes("壁仞项目"), "关系区应解析人名与项目标题");
  assert.ok(overlay.textContent.includes("11月22日项目沟通记录"), "应展示往来记录");
  // 切换到公司页签
  const orgTab = byText(overlay, "button", "公司")[0];
  assert.ok(orgTab);
  orgTab.listeners.click();
  await flush();
  assert.ok(overlay.textContent.includes("上海电信下属公司"), "公司页签应展示公司详情");
  // 切换到项目与记录页签
  const matterTab = byText(overlay, "button", "项目")[0];
  assert.ok(matterTab);
  matterTab.listeners.click();
  await flush();
  assert.ok(overlay.textContent.includes("壁仞项目"), "项目页签应展示项目资料");
  const noteTab = byText(overlay, "button", "记录")[0];
  assert.ok(noteTab);
  noteTab.listeners.click();
  await flush();
  assert.ok(overlay.textContent.includes("11月22日项目沟通记录"), "记录页签应展示往来记录");
}

async function contactsDrawerFileImport(dir) {
  let people = [
    {
      id: "per_1", name: "老王", organization: "", version: 1, has_avatar: false,
      profile: { name: "老王", aliases: [], gender: "unknown", contacts: [{ kind: "phone", label: "手机", value: "13800138000", primary: true }], tags: [], summary: "", extra: {} },
    },
  ];
  let committedPayload = null;
  const routes = base({
    "GET /v1/people": () => ({ json: { items: people.slice() } }),
    "GET /v1/people/orgs": () => ({ json: { items: [] } }),
    "GET /v1/people/notes": () => ({ json: { items: [] } }),
    "GET /v1/people/per_1": () => ({ json: { person: people[0], relations: [], notes: [] } }),
    "GET /v1/people/per_2": () => ({ json: { person: people[1], relations: [], notes: [] } }),
    "GET /v1/people/google/status": () => ({
      json: { enabled: false, configured: false, linked: false, account_email: "", synced_count: 0 },
    }),
    "POST /v1/people/import/preview": () => ({
      json: {
        format: "vcf",
        total_rows: 2,
        valid_count: 2,
        skipped_count: 0,
        new_count: 1,
        duplicate_count: 1,
        remaining_quota: 4999,
        warnings: [],
        items: [
          {
            index: 0,
            profile: { name: "王建国", organization: "星辰科技", contacts: [{ kind: "phone", value: "+86 13800138000", label: "手机", primary: true }] },
            status: "duplicate",
            duplicate_in_file: false,
            candidates: [{ person_id: "per_1", name: "老王", organization: "", title: "", version: 1 }],
            suggested_action: "merge",
            warnings: [],
          },
          {
            index: 1,
            profile: { name: "陈琳", organization: "明镜资本", contacts: [{ kind: "phone", value: "13900139000", label: "手机", primary: true }] },
            status: "new",
            duplicate_in_file: false,
            candidates: [],
            suggested_action: "create",
            warnings: [],
          },
        ],
      },
    }),
    "POST /v1/people/import": call => {
      committedPayload = call.body;
      const createdPerson = { id: "per_2", name: "陈琳", organization: "明镜资本", version: 1, has_avatar: false, profile: call.body.items[1].profile };
      people = [
        { ...people[0], version: 2, organization: "星辰科技" },
        createdPerson,
      ];
      return {
        status: 201,
        json: { created_count: 1, merged_count: 1, skipped_count: 0, created: [createdPerson], merged: [people[0]] },
      };
    },
  });
  const h = harness(routes);
  load(h, dir); await flush();
  h.nodes("entry-contacts").listeners.click();
  await flush();
  const overlay = h.nodes("contacts-overlay");
  const importBtn = byText(overlay, "button", "导入 / 关联")[0];
  assert.ok(importBtn, "通讯录抽屉顶栏应提供导入 / 关联按钮");
  importBtn.listeners.click();
  await flush();
  assert.ok(overlay.textContent.includes("导入通讯录 / 外部账号关联"), "点击后应进入导入与关联视图");
  assert.ok(overlay.textContent.includes("Google 通讯录关联") && overlay.textContent.includes("未配置"), "未配置时应展示 Google 关联未配置状态");
  // 模拟选择 .vcf 文件触发预览
  const fileInput = all(overlay, n => n.tag === "input" && n.type === "file")[0];
  assert.ok(fileInput);
  fileInput.files = [{ name: "google.vcf", type: "text/vcard" }];
  fileInput.listeners.change();
  await flush();
  assert.ok(overlay.textContent.includes("疑似重复") && overlay.textContent.includes("陈琳"), "预览列表应展示新增与重复提示");
  // 点击确认导入
  const commitBtn = all(overlay, n => n.tag === "button" && n.id === "ab-import-commit")[0];
  assert.ok(commitBtn && !commitBtn.disabled);
  commitBtn.listeners.click();
  await flush();
  assert.ok(committedPayload && committedPayload.items.length === 2, "确认导入应提交全部条目决策");
  assert.equal(committedPayload.items[0].action, "merge");
  assert.equal(committedPayload.items[0].target_person_id, "per_1");
  assert.equal(committedPayload.items[1].action, "create");
  assert.ok(overlay.textContent.includes("导入完成：新建 1 人，合并更新 1 人"), "导入完成后应展示统计提示");
}

async function boardDrawerTodosAndQuickAdd(dir) {
  const todos = [
    { id: "ws_t1", kind: "todo", version: 1, data: { title: "把壁仞方案发给何总", status: "open", due: "明天 10:00" }, trashed_at: null },
  ];
  const routes = base({
    "GET /v1/workspace/items": call => {
      if (call.url.includes("kind=todo")) return { json: { items: todos.slice() } };
      return { json: { items: [] } };
    },
    "PUT /v1/workspace/items/ws_t1": call => {
      todos[0] = { ...todos[0], version: 2, data: call.body.data };
      return { json: todos[0] };
    },
    "POST /v1/workspace/items": call => {
      const added = { id: "ws_t2", kind: call.body.kind, version: 1, data: call.body.data, trashed_at: null };
      todos.push(added);
      return { json: added };
    },
    "GET /v1/reminders": () => ({ json: { items: [{ reminder_id: "rem_1", title: "回袁新电话", due_at: "2026-11-27T09:00:00+08:00", repeat: "once", status: "active", version: 1 }] } }),
    "GET /v1/proactive": () => ({ status: 404, json: {} }),
  });
  const h = harness(routes);
  load(h, dir); await flush();
  h.nodes("entry-board").listeners.click();
  await flush();
  const overlay = h.nodes("board-overlay");
  assert.ok(!overlay.classList.contains("hidden"), "点击看板应打开抽屉");
  assert.ok(overlay.textContent.includes("把壁仞方案发给何总"), "看板应展示真实待办");
  // 勾选完成第一条待办
  const checkBtn = all(overlay, n => n.tag === "button" && String(n.className).includes("bd-check"))[0];
  assert.ok(checkBtn);
  checkBtn.listeners.click();
  await flush();
  const putCall = h.calls.find(c => c.method === "PUT" && c.url === "/v1/workspace/items/ws_t1");
  assert.ok(putCall && putCall.body.data.status === "done" && putCall.body.version === 1, "勾选待办应带版本号写回 done");
  // 快速新建待办
  const addForm = all(overlay, n => n.tag === "form" && String(n.className).includes("bd-add-bar"))[0];
  const titleInput = all(addForm, n => n.tag === "input")[0];
  titleInput.value = "预约机场接送";
  addForm.listeners.submit({ preventDefault() {} });
  await flush();
  assert.ok(overlay.textContent.includes("预约机场接送"), "新建待办应立刻显示在看板中");
}

async function liveNotificationsAndBrowserPopup(dir) {
  let notifications = [];
  let streamController = null;
  const popups = [];
  let focused = false;
  let sseAborted = false;
  const routes = base({
    "GET /v1/notifications": () => ({ json: { items: notifications.slice() } }),
    "GET /v1/notifications/events": (_call, init) => {
      if (init && init.signal) init.signal.addEventListener("abort", () => { sseAborted = true; });
      const chunks = [];
      let waiter = null;
      streamController = {
        push(name, data) {
          const buf = Buffer.from(`id: ${data.notification_id || 1}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
          if (waiter) { const w = waiter; waiter = null; w({ value: buf, done: false }); }
          else chunks.push(buf);
        },
        close() {
          if (waiter) { const w = waiter; waiter = null; w({ done: true }); }
        },
      };
      return {
        stream: {
          getReader: () => ({
            read: () => {
              if (chunks.length) return Promise.resolve({ value: chunks.shift(), done: false });
              return new Promise((resolve, reject) => {
                waiter = resolve;
                if (init && init.signal) {
                  init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
                }
              });
            },
          }),
        },
      };
    },
    "POST /v1/notifications/42/read": () => {
      notifications = notifications.map(n => n.notification_id === 42 ? { ...n, read_at: 1795600100 } : n);
      return { json: notifications[0] || {} };
    },
    "POST /v1/auth/logout": () => ({ json: {} }),
  });
  const h = harness(routes);
  function FakeNotification(title, opts) {
    this.title = title;
    this.opts = opts;
    this.closed = false;
    this.close = () => { this.closed = true; };
    popups.push(this);
  }
  FakeNotification.permission = "granted";
  FakeNotification.requestPermission = async () => "granted";
  h.context.Notification = FakeNotification;
  h.context.focus = () => { focused = true; };
  load(h, dir); await flush();
  assert.equal(h.nodes("entry-notifications").textContent, "通知");

  const dashboardBefore = h.calls.filter(c => c.url.startsWith("/v1/dashboard")).length;
  const fired = { notification_id: 42, title: "给何总回电话", due_at: 1795600000, created_at: 1795600000, read_at: null, source: "reminder", attention_active: true };
  notifications = [fired];
  streamController.push("notification", fired);
  await flush();

  assert.ok(!h.nodes("entry-notification-alerts").classList.contains("hidden"), "收到到点提醒应弹出网页内悬浮卡片");
  assert.ok(h.nodes("entry-notification-alert-list").textContent.includes("给何总回电话"), "悬浮卡片应展示提醒内容");
  assert.equal(popups.length, 1, "已授权浏览器通知时应触发 Chrome 桌面弹窗");
  assert.equal(popups[0].title, "提醒时间到了");
  assert.ok(popups[0].opts.body.includes("给何总回电话"));
  assert.equal(h.nodes("entry-notifications").textContent, "通知 (1)", "顶部通知按钮应显示未读角标");
  assert.ok(h.nodes("entry-notifications").classList.contains("has-unread"));
  assert.ok(h.calls.filter(c => c.url.startsWith("/v1/dashboard")).length > dashboardBefore, "新通知应同步刷新概览");

  // 点击悬浮卡上的「标为已读」
  const readBtn = byText(h.nodes("entry-notification-alert-list"), "button", "标为已读")[0];
  assert.ok(readBtn);
  readBtn.listeners.click();
  await flush();
  assert.ok(h.nodes("entry-notification-alerts").classList.contains("hidden"), "标为已读后应关闭悬浮卡片");
  assert.equal(h.nodes("entry-notifications").textContent, "通知", "全部已读后应清除未读角标");

  // 点击浏览器桌面弹窗应聚焦窗口并打开通知卡（且提供「发送测试弹窗」按钮）
  popups[0].onclick();
  await flush();
  assert.equal(focused, true, "点击桌面弹窗应聚焦标签页");
  assert.ok(h.nodes("transcript").textContent.includes("给何总回电话"), "点击桌面弹窗应展开通知列表");
  const testPopupBtn = byText(h.nodes("transcript"), "button", "发送测试弹窗")[0];
  assert.ok(testPopupBtn, "已授权时通知卡应提供测试系统弹窗按钮");
  testPopupBtn.listeners.click();
  assert.equal(popups.length, 2, "点击测试按钮应立即触发一条浏览器系统通知");

  // 退出登录应中止通知 SSE 流
  h.nodes("entry-logout").listeners.click();
  await flush();
  assert.equal(sseAborted, true, "退出登录必须中止通知 SSE 连接");
}

async function chatReminderPromptRequestsDesktopPermission(dir) {
  let requested = false;
  const h = harness(base({
    "POST /v1/assistant/messages": () => ({ json: { mode: "butler", state: "done", message: "已设置提醒" } }),
  }));
  function FakeNotification() {}
  FakeNotification.permission = "default";
  FakeNotification.requestPermission = async () => { requested = true; return "granted"; };
  h.context.Notification = FakeNotification;
  load(h, dir); await flush();
  h.nodes("message").value = "1分钟后提醒我喝水";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush();
  assert.equal(requested, true, "在对话里直接发提醒消息时应趁用户提交手势请求 Chrome 桌面通知权限");
}

async function boardDrawerCompletedRemindersAndTimestamp(dir) {
  let reminders = [
    { reminder_id: "rem_done", title: "已到点的测试提醒", due_at: 1795600000, timezone: "Asia/Shanghai", repeat: "once", status: "completed", version: 2 },
  ];
  let createdReminderBody = null;
  const routes = base({
    "GET /v1/workspace/items": () => ({ json: { items: [] } }),
    "GET /v1/reminders": () => ({ json: { items: reminders.slice() } }),
    "POST /v1/reminders": call => {
      createdReminderBody = call.body;
      const added = { reminder_id: "rem_new", ...call.body, status: "active", version: 1 };
      reminders = [added, ...reminders];
      return { json: added };
    },
    "GET /v1/proactive": () => ({ status: 404, json: {} }),
  });
  const h = harness(routes);
  let requestedPerm = false;
  function FakeNotification() {}
  FakeNotification.permission = "default";
  FakeNotification.requestPermission = async () => { requestedPerm = true; FakeNotification.permission = "granted"; return "granted"; };
  h.context.Notification = FakeNotification;
  load(h, dir); await flush();

  h.nodes("entry-board").listeners.click();
  await flush();
  const overlay = h.nodes("board-overlay");
  const remTab = byText(overlay, "button", "提醒")[0];
  assert.ok(remTab);
  remTab.listeners.click();
  await flush();
  assert.ok(overlay.textContent.includes("已到点的测试提醒"), "无待触发提醒时应自动展开已提醒记录，避免点开后消失");
  assert.ok(overlay.textContent.includes("开启桌面弹窗"), "未授权浏览器通知时应显示引导条");

  // 看板内手动新建提醒：due_at 必须转为 Unix 秒级数字，且自动触发一次权限引导
  const addForm = all(overlay, n => n.tag === "form" && String(n.className).includes("bd-add-bar"))[0];
  const inputs = all(addForm, n => n.tag === "input");
  inputs[0].value = "下午四点开评审会";
  inputs[1].value = "2030-05-01T16:00";
  addForm.listeners.submit({ preventDefault() {} });
  await flush();
  assert.ok(createdReminderBody, "看板内新建提醒应调用 POST /v1/reminders");
  assert.equal(typeof createdReminderBody.due_at, "number", "due_at 必须是 Unix 秒级数值而非 ISO 字符串");
  assert.deepEqual(createdReminderBody.channels, ["inbox"]);
  assert.equal(requestedPerm, true, "手动新建提醒时应尝试申请浏览器桌面弹窗权限");
  assert.ok(overlay.textContent.includes("下午四点开评审会"), "新建提醒应展示在活动提醒列表中");

  // 概览点击提醒打开对话提醒列表卡，也应能看到已提醒记录
  h.context.openSummary({ kind: "reminder", ref: "rem_done", title: "已到点的测试提醒" });
  await flush();
  assert.ok(h.nodes("transcript").textContent.includes("已到点的测试提醒"));
  assert.ok(h.nodes("transcript").textContent.includes("已提醒"));
}

async function contactsDrawerPeopleGraph(dir) {
  let fullGraphCalls = 0;
  let focusGraphUrl = null;
  const h = harness({
    "GET /v1/me": () => ME,
    "GET /v1/workspace/settings": () => ({ json: { auto_execute_personal: true } }),
    "GET /v1/dashboard/overview": () => OVERVIEW,
    "GET /v1/workspace/messages": () => ({ json: { items: [] } }),
    "GET /v1/assistant/pending": () => ({ json: { items: [] } }),
    "GET /v1/notifications": () => ({ json: { items: [] } }),
    "GET /v1/notifications/events": (_, init) => ({ stream: pendingStream(init) }),
    "GET /v1/people": () => ({ json: { items: [
      { id: "per_1", name: "袁新", organization: "上海理想", version: 1, profile: { name: "袁新", organization: "上海理想", title: "总监" } },
      { id: "per_2", name: "王强", organization: "上海理想", version: 1, profile: { name: "王强", organization: "上海理想", title: "经理" } },
    ] } }),
    "GET /v1/people/orgs": () => ({ json: { items: [
      { id: "org_1", name: "上海理想", version: 1, profile: { name: "上海理想", city: "上海" } },
    ] } }),
    "GET /v1/workspace/items": () => ({ json: { items: [] } }),
    "GET /v1/people/notes": () => ({ json: { items: [] } }),
    "GET /v1/me/knowledge/sources": () => ({ status: 404, json: {} }),
    "GET /v1/me/work-memory": () => ({ status: 404, json: {} }),
    "GET /v1/people/per_1": () => ({ json: {
      person: { id: "per_1", name: "袁新", organization: "上海理想", version: 1, profile: { name: "袁新", organization: "上海理想" } },
      relations: [{ id: "rel_1", from_kind: "person", from_person_id: "per_1", target_kind: "org", target_id: "org_1", relation_type: "works_at", role: "总监", is_symmetric: false }],
      notes: [],
    } }),
    "GET /v1/people/graph": () => {
      fullGraphCalls += 1;
      return { json: {
        nodes: [
          { kind: "person", id: "per_1", name: "袁新", organization: "上海理想", subtitle: "上海理想 · 总监", summary: "", version: 1, degree: 1 },
          { kind: "person", id: "per_2", name: "王强", organization: "上海理想", subtitle: "上海理想 · 经理", summary: "", version: 1, degree: 0 },
          { kind: "org", id: "org_1", name: "上海理想", subtitle: "上海", summary: "", version: 1, degree: 1 },
        ],
        edges: [
          { id: "rel_1", from_kind: "person", from_person_id: "per_1", target_kind: "org", target_id: "org_1", relation_type: "works_at", role: "总监", is_symmetric: false },
        ],
        truncated: false,
        stats: { total_persons: 2, total_orgs: 1, total_matters: 0, total_edges: 1, shown_nodes: 3, shown_edges: 1 },
      } };
    },
    "GET /v1/people/graph/person/per_1": (call) => {
      focusGraphUrl = call.url;
      return { json: {
        center: { kind: "person", id: "per_1" },
        depth: 2,
        nodes: [
          { kind: "person", id: "per_1", name: "袁新", organization: "上海理想", subtitle: "上海理想 · 总监", summary: "", version: 1 },
          { kind: "org", id: "org_1", name: "上海理想", subtitle: "上海", summary: "", version: 1 },
          { kind: "matter", id: "mat_1", name: "壁仞项目", subtitle: "active", summary: "", version: 1 },
          { kind: "person", id: "per_2", name: "王强", organization: "", subtitle: "", summary: "", version: 1 },
        ],
        edges: [
          { id: "rel_1", from_kind: "person", from_person_id: "per_1", target_kind: "org", target_id: "org_1", relation_type: "works_at", role: "总监", is_symmetric: false },
          { id: "rel_2", from_kind: "person", from_person_id: "per_1", target_kind: "matter", target_id: "mat_1", relation_type: "project_introducer", role: "项目介绍人", is_symmetric: false },
          { id: "rel_3", from_kind: "person", from_person_id: "per_2", target_kind: "matter", target_id: "mat_1", relation_type: "co_creator", role: "", is_symmetric: false },
        ],
        records: [{ id: "note_1", title: "半岛酒店沟通会", occurred_at: 1700000000, summary: "确认壁仞项目推进节奏", content: "确认壁仞项目推进节奏" }],
      } };
    },
  });
  load(h, dir); await flush();

  // 打开书架资料室，切到「关系图」Tab，验证全景 SVG 与自动同公司连线渲染
  h.nodes("entry-contacts").listeners.click();
  await flush();
  const overlay = h.nodes("contacts-overlay");
  const graphTab = byText(overlay, "button", "关系图")[0];
  assert.ok(graphTab, "书架资料室应包含「关系图」页签");
  graphTab.listeners.click();
  await flush();
  assert.equal(fullGraphCalls, 1, "切到关系图页签应请求 GET /v1/people/graph");
  const svgs = all(overlay, n => n.tag === "svg");
  assert.equal(svgs.length, 1, "关系图应渲染原生 SVG 画布");
  assert.ok(overlay.textContent.includes("袁新") && overlay.textContent.includes("上海理想"));
  assert.ok(overlay.textContent.includes("任职于"), "应展示中文关系标签");
  assert.ok(overlay.textContent.includes("同公司"), "默认开启同公司连线时应为王强补齐虚线聚合边");

  // 选中节点后点击「以它为中心」进入 2 跳聚焦模式，展示溯源记录
  const perNode = all(overlay, n => n.tag === "g" && n.dataset && n.dataset.nodeKey === "person:per_1")[0];
  assert.ok(perNode, "应找到袁新图谱节点");
  perNode.listeners.click({ stopPropagation() {} });
  await flush();
  const focusBtn = byText(overlay, "button", "以它为中心")[0];
  assert.ok(focusBtn, "右侧详情应提供「以它为中心」按钮");
  focusBtn.listeners.click();
  await flush();
  assert.ok(focusGraphUrl && focusGraphUrl.includes("/v1/people/graph/person/per_1?depth=2"), "应拉取 2 跳聚焦图谱接口");
  assert.ok(overlay.textContent.includes("半岛酒店沟通会"), "聚焦模式右侧应展示溯源记录");
  assert.ok(overlay.textContent.includes("项目介绍人") && !overlay.textContent.includes("project_introducer"), "英文关系类型应展示为中文角色");
  assert.ok(overlay.textContent.includes("共创人"), "co_creator 应翻译为中文");
  // 验证聚焦 2 跳布局不会退化为竖直共线（x 坐标应横向充分展开，且每对节点间保持安全间距）
  const posList = vm.runInContext("[...abGraphState.positions.values()]", h.context);
  const xs = posList.map(p => p.x);
  assert.ok(Math.max(...xs) - Math.min(...xs) > 250, "2 跳聚焦节点应横向展开而非竖排重叠");
  for (let i = 0; i < posList.length; i += 1) {
    for (let j = i + 1; j < posList.length; j += 1) {
      assert.ok(Math.hypot(posList[i].x - posList[j].x, posList[i].y - posList[j].y) >= 120, "任意两节点距离应 >= 120px 避免文字重叠");
    }
  }
}

async function planFocusSupersededDiffAndWorkbench(dir) {
  let promoted = false;
  let rolledBack = false;
  const h = harness({
    ...base({
      "GET /v1/me": () => ({ json: { user_id: "usr_alice", tenant_id: "acme", display_name: "Alice", role: "tenant_admin", entry: "minimal" } }),
      "POST /v1/assistant/messages": () => ({
        headers: { "content-type": "text/event-stream" },
        stream: sse([
          ["started", { mode: "butler", request_id: "req_plan", trace_id: "trc_wb" }],
          ["plan", { text: "先查档案，再局部修订周报" }],
          ["focus", { board_id: "btb_1", trace_id: "trc_wb", title: "修订周报", focus: "正在更新进度", status: "active", steps: [{ id: "s1", title: "读周报", status: "done" }, { id: "s2", title: "改进度", status: "in_progress" }] }],
          ["delta", { text: "已更新周报。" }],
          ["receipt", {
            mode: "butler", state: "done", request_id: "req_plan", trace_id: "trc_wb",
            message: "| 项目 | 状态 |\n| --- | --- |\n| 周报 | 完成 |",
            executed_actions: [{
              action_id: "act_1", action: "patch_personal_file", title: "已局部编辑文档",
              summary: "weekly.md（1 处修改）", diff: "--- a/weekly.md\n+++ b/weekly.md\n@@ -1 +1 @@\n-80%\n+100%",
              undoable: true,
            }],
          }],
        ]),
      }),
      "GET /v1/traces/trc_wb/events": () => ({ stream: sse([["snapshot", { title: "butler", active: false, spans: [] }], ["end", {}]]) }),
      "GET /v1/assistant/task-boards": () => ({ json: { items: [{ board_id: "btb_1", trace_id: "trc_wb", title: "修订周报", focus: "正在更新进度", status: "active", steps: [{ id: "s1", title: "读周报", status: "done" }] }] } }),
      "GET /v1/traces/trc_wb/artifacts": () => ({ json: { items: [{ artifact_id: "art_1", name: "report.md", mime_type: "text/markdown", contains_external_input: true, promoted_item_id: null }] } }),
      "GET /v1/artifacts/art_1/preview": () => ({ json: { text: "# 报告\n\n核心结论 A" } }),
      "POST /v1/artifacts/art_1/promote": () => { promoted = true; return { json: { ok: true } }; },
      "GET /v1/diagnostics/failed-transcripts": () => ({ json: { items: [{ trace_id: "trc_err", span_id: "sp_err", status: "failed", error_code: "timeout", node_id: "node-a", line_count: 12 }] } }),
      "GET /v1/traces/trc_err/transcripts/sp_err": () => ({ json: { mode: "compact", source: "remote_node", lines: [{ kind: "error", status: "failed", payload: { error_code: "timeout" } }] } }),
      "GET /v1/releases/artisan/support-desk": () => ({ json: { items: [{ release_id: "rel_1", generation: 1, target_name: "support-desk", version: "1.0.0", state: "retired" }, { release_id: "rel_2", generation: 2, target_name: "support-desk", version: "1.1.0", state: "active" }] } }),
      "POST /v1/releases/artisan/support-desk/rollback": () => { rolledBack = true; return { json: { generation: 3, state: "active" } }; },
    }),
  });
  load(h, dir); await flush();

  // 1. 发送消息并验证 SSE plan / focus / 表格渲染 / Unified Diff
  h.nodes("message").value = "把周报进度改成 100%";
  h.nodes("composer").requestSubmit();
  await flush();
  assert.ok(!h.nodes("entry-live-focus").classList.contains("hidden"), "Live Focus 胶囊应显示");
  assert.ok(h.nodes("entry-live-focus").textContent.includes("正在更新进度"));
  assert.ok(h.nodes("transcript").textContent.includes("查看修改 Diff"), "局部编辑回执卡应包含 Unified Diff 折叠视图");
  assert.ok(all(h.nodes("transcript"), n => n.tag === "table").length >= 1, "气泡应支持轻 Markdown 表格渲染");

  // 2. 打开辅助工作台，依次验证任务板、过程产物预览+转存、跨节点诊断、发布节点回退
  h.nodes("entry-workbench").listeners.click();
  await flush();
  const wb = h.nodes("workbench-overlay");
  assert.ok(wb.textContent.includes("修订周报"), "任务板应展示跨轮任务");

  byText(wb, "button", "过程产物")[0].listeners.click();
  await flush();
  assert.ok(wb.textContent.includes("report.md") && wb.textContent.includes("外部联网输入"));
  byText(wb, "button", "预览正文")[0].listeners.click();
  await flush();
  assert.ok(wb.textContent.includes("核心结论 A"));
  byText(wb, "button", "转存个人空间")[0].listeners.click();
  await flush();
  assert.equal(promoted, true, "点击转存应调用 POST /v1/artifacts/art_1/promote");

  byText(wb, "button", "跨节点诊断")[0].listeners.click();
  await flush();
  assert.ok(wb.textContent.includes("sp_err"));
  byText(wb, "button", "读紧凑版")[0].listeners.click();
  await flush();
  assert.ok(wb.textContent.includes("remote_node"));

  byText(wb, "button", "发布与回退")[0].listeners.click();
  await flush();
  const relInput = all(wb, n => n.tag === "input")[0];
  relInput.value = "support-desk";
  byText(wb, "button", "查询代次")[0].listeners.click();
  await flush();
  assert.ok(wb.textContent.includes("R1 · support-desk@1.0.0"));
  byText(wb, "button", "回退到此版本")[0].listeners.click();
  await flush();
  assert.equal(rolledBack, true, "点击回退应调用 POST /v1/releases/artisan/support-desk/rollback");
}

async function frontendPluginHostBridgeAndRichRenderers(dir) {
  const rpcCalls = [];
  const h = harness({
    "GET /v1/me": () => ME,
    "GET /v1/workspace/settings": () => ({ json: { auto_execute_personal: true } }),
    "GET /v1/workspace/messages": () => ({ json: { items: [], has_more: false } }),
    "GET /v1/dashboard": () => OVERVIEW,
    "GET /v1/notifications": () => ({ json: { items: [], unread_count: 0 } }),
    "GET /v1/notifications/events": (_, init) => pendingStream(init),
    "GET /v1/workspace/messages/events": (_, init) => pendingStream(init),
    "GET /v1/assistant/task-boards": () => ({ json: { items: [] } }),
    "GET /v1/plugins/ui": () => ({
      json: {
        items: [
          {
            plugin_id: "artisan:invoice-bot:invoice-board",
            title: "审单对照台",
            slot: "aux-tab",
            mode: "sandbox_iframe",
            source_layer: "L1_artisan",
            package: "invoice-bot",
            version: "0.1.0",
            entry_url: "/v1/plugins/ui/invoice-bot/0.1.0/invoice-board/index.html",
            mime_types: [],
            bridge_permissions: ["collection.write", "workspace.propose"],
          },
        ],
      },
    }),
    "POST /v1/plugins/bridge/rpc": (call) => {
      rpcCalls.push(call.body);
      return { json: { ok: true, result: { record_id: "plr_1", version: 1 } } };
    },
  });
  load(h, dir);
  await flush();

  // 1. 验证 L2 内置消息渲染插件：离线 Mermaid SVG（含时序图）+ 行级 Diff + 声明式指标卡 + 数学公式 + 代码高亮
  const bubble = h.context.document.createElement("div");
  h.context.renderAssistantBubble(
    bubble,
    '架构如下：\n```mermaid\nsequenceDiagram\n  Butler->>Artisan: 派工\n  Artisan-->>Butler: 返回产物\n```\n```diff\n@@ -1 +1 @@\n-旧文案\n+新文案\n```\n$$\\frac{\\Delta y}{x^2} = \\sigma$$\n```python\n# 计算\ndef run():\n    return "ok"\n```\n```xuanpu-ui\n{"title":"Q4 概览","metrics":[{"label":"营收","value":"1200万","delta":"+18%"}]}\n```'
  );
  assert.ok(all(bubble, n => n.tag === "svg").length >= 1, "Mermaid 插件应渲染离线 SVG 时序图");
  assert.ok(all(bubble, n => n.className.includes("diff-add")).length === 1, "Diff 插件应高亮新增行");
  assert.ok(all(bubble, n => n.className.includes("math-frac")).length === 1, "数学公式插件应渲染分式");
  assert.ok(all(bubble, n => n.className.includes("tok-kw")).length >= 2, "代码高亮插件应高亮关键字");
  assert.ok(bubble.textContent.includes("Q4 概览") && bubble.textContent.includes("1200万"), "声明式 UI 插件应渲染指标卡");

  // 2. 验证右侧工作台自动挂载分屏调宽手柄、L1 匠人 aux-tab 插件与安全沙盒 iframe（绝不含 allow-same-origin）
  h.nodes("entry-workbench").listeners.click();
  await flush();
  const wb = h.nodes("workbench-overlay");
  assert.ok(all(wb, n => n.className.includes("wb-resizer")).length === 1, "右侧分屏工作台应提供拖拽调宽手柄");
  const tabBtns = byText(wb, "button", "审单对照台");
  assert.equal(tabBtns.length, 1, "工作台应动态挂出 L1 匠人插件标签页");
  tabBtns[0].listeners.click();
  await flush();
  const frames = all(wb, n => n.tag === "iframe");
  assert.equal(frames.length, 1, "点击插件标签应挂载沙盒 iframe");
  assert.equal(frames[0].attrs.sandbox, "allow-scripts", "沙盒 iframe 严禁包含 allow-same-origin");
  assert.equal(frames[0].attrs.src, "/v1/plugins/ui/invoice-bot/0.1.0/invoice-board/index.html");

  // 3. 验证 postMessage Bridge：放行声明权限、拦截未声明权限
  const posted = [];
  const fakeWin = { postMessage(msg) { posted.push(msg); } };
  frames[0].contentWindow = fakeWin;
  frames[0].listeners.load();

  await h.context.XuanpuPluginHost.handleBridgeMessage({
    source: fakeWin,
    data: { type: "xuanpu:rpc", reqId: "r1", method: "collection.write", params: { collection: "c1", data: { k: 1 } } },
  });
  assert.equal(rpcCalls.length, 1);
  assert.equal(rpcCalls[0].method, "collection.write");
  assert.equal(posted.find(m => m.reqId === "r1").ok, true);

  await h.context.XuanpuPluginHost.handleBridgeMessage({
    source: fakeWin,
    data: { type: "xuanpu:rpc", reqId: "r2", method: "artifact.read", params: { artifact_id: "art_1" } },
  });
  assert.equal(rpcCalls.length, 1, "未授权方法必须在宿主侧直接拦截，不得转发后端");
  assert.equal(posted.find(m => m.reqId === "r2").error, "permission_denied");

  // 4. Clean Unmounting：注销插件后 Bridge 句柄与注册表彻底清空
  assert.equal(h.context.XuanpuPluginHost.unregister("artisan:invoice-bot:invoice-board"), true);
  assert.equal(h.context.XuanpuPluginHost.get("artisan:invoice-bot:invoice-board"), null);
  assert.equal(h.context.XuanpuPluginHost._mountedFrames.has(fakeWin), false);
}

async function chatAttachmentsPreviewAndDownload(dir) {
  const mdBody = "# 会议纪要\n\n- 参会人：**袁新**、石总\n- 附带配置 `{env: \"prod\"}` 需原样保留\n";
  const h = harness(base({
    "GET /v1/workspace/messages": () => ({
      json: {
        items: [
          {
            id: 10, role: "user", text: "历史上传的会议材料", request_id: "hist-1:user",
            attachments: ["itm_hist_md", "itm_hist_xlsx"], metadata: {},
          },
        ],
      },
    }),
    "GET /v1/workspace/items/itm_hist_md": () => ({
      json: { id: "itm_hist_md", kind: "file", data: { name: "历史纪要.md", mime_type: "text/plain", size: 128, current_revision: 2 } },
    }),
    "GET /v1/workspace/items/itm_hist_xlsx": () => ({
      json: { id: "itm_hist_xlsx", kind: "file", data: { name: "报价单.xlsx", mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", size: 4096, current_revision: 1 } },
    }),
    "POST /v1/workspace/files": () => ({
      json: { id: "itm_new_md", kind: "file", data: { name: "周五 14点45分.md", mime_type: "text/plain", size: 96, current_revision: 1 } },
    }),
    "GET /v1/workspace/files/itm_new_md/content": () => ({
      text: mdBody,
      headers: { "content-type": "text/plain; charset=utf-8" },
    }),
    "GET /v1/workspace/files/itm_hist_md/content": () => ({
      text: "## 历史回顾\n\n内容正常可读。",
      headers: { "content-type": "text/markdown; charset=utf-8" },
    }),
    "POST /v1/assistant/messages": () => ({
      json: { mode: "butler", state: "done", message: "已帮你记录本次壁仞科技交流。" },
    }),
  }));
  load(h, dir); await flush();

  // 1. 刷新恢复的历史消息附件：自动补齐文件名，提供点击查看与直接下载
  const transcript = h.nodes("transcript");
  const histViewBtns = all(transcript, n => n.tag === "button" && n.classList.contains("msg-att-view"));
  assert.equal(histViewBtns.length, 2, "历史消息的两个附件都渲染为可点击查看按钮");
  assert.ok(histViewBtns[0].textContent.includes("历史纪要.md"));
  assert.ok(histViewBtns[1].textContent.includes("报价单.xlsx"));
  const histDls = all(transcript, n => n.tag === "a" && n.classList.contains("msg-att-dl"));
  assert.equal(histDls[0].href, "/v1/workspace/files/itm_hist_md/content?revision=2");
  assert.equal(histDls[0].attrs.download, "历史纪要.md");
  assert.equal(histDls[1].href, "/v1/workspace/files/itm_hist_xlsx/content?revision=1");

  // 点击不支持内联预览的二进制附件：不拉取大文件字节，直接显示下载引导
  histViewBtns[1].listeners.click(); await flush();
  const overlay = h.nodes("attachment-overlay");
  assert.ok(!overlay.classList.contains("hidden"));
  assert.ok(overlay.textContent.includes("报价单.xlsx"));
  assert.ok(overlay.textContent.includes("暂不支持网页内直接预览正文"));
  assert.ok(!h.calls.some(c => c.url.includes("/itm_hist_xlsx/content")), "二进制附件预览不应多余下载正文");
  byText(overlay, "button", "关闭")[0].listeners.click();
  assert.ok(overlay.classList.contains("hidden"));

  // 2. 新上传附件并发送：输入区和已发气泡均支持查看与下载，不再是静态 [附件：...] 纯文本
  const fileInput = h.nodes("entry-file-input");
  fileInput.files = [{ name: "周五 14点45分.md", type: "", size: 96 }];
  fileInput.listeners.change(); await flush();
  const composerFiles = h.nodes("composer-files");
  assert.ok(!composerFiles.classList.contains("hidden"));
  assert.equal(all(composerFiles, n => n.tag === "button" && n.classList.contains("composer-file-view")).length, 1);
  assert.equal(all(composerFiles, n => n.tag === "a" && n.classList.contains("composer-file-dl"))[0].href, "/v1/workspace/files/itm_new_md/content?revision=1");

  h.nodes("message").value = "帮我记录一下这个会议";
  h.nodes("composer").listeners.submit({ preventDefault() {} }); await flush();
  assert.ok(!transcript.textContent.includes("[附件："), "发送后的消息不应再退化成不可点击的纯文本附件标签");

  const newViewBtn = all(transcript, n => n.tag === "button" && n.classList.contains("msg-att-view") && n.textContent.includes("周五 14点45分.md"))[0];
  assert.ok(newViewBtn, "刚发送的消息气泡内应有可点击的附件按钮");
  const newDlLink = all(transcript, n => n.tag === "a" && n.classList.contains("msg-att-dl") && n.attrs.download === "周五 14点45分.md")[0];
  assert.ok(newDlLink && newDlLink.href === "/v1/workspace/files/itm_new_md/content?revision=1", "刚发送的附件应提供带版本的直接下载链接");

  // 3. 点击刚发送的 Markdown 附件查看正文、切换原文，并验证原文花括号未被误清洗
  newViewBtn.listeners.click(); await flush();
  assert.ok(!overlay.classList.contains("hidden"));
  assert.ok(overlay.textContent.includes("会议纪要"));
  assert.ok(overlay.textContent.includes('{env: "prod"}'), "附件中的 JSON/花括号必须忠实保留");
  assert.ok(all(overlay, n => n.tag === "a" && n.href === "/v1/workspace/files/itm_new_md/content?revision=1").length >= 1, "预览弹窗顶部应有下载原件按钮");

  const rawBtn = byText(overlay, "button", "查看原文")[0];
  assert.ok(rawBtn, "Markdown 附件应提供查看原文切换按钮");
  rawBtn.listeners.click();
  assert.ok(all(overlay, n => n.tag === "pre" && n.textContent.includes("# 会议纪要")).length === 1, "切换后应以源码 pre 展示原始 Markdown");

  // 4. 退出/切换身份时应自动关闭附件预览弹窗
  h.context.resetEntry();
  assert.ok(overlay.classList.contains("hidden"), "身份重置时必须关闭已打开的附件预览弹窗");
}

async function outcomeShowcaseAndDagFlow(dir) {
  const routes = base({
    "GET /v1/workspace/files/ws_doc_1/content": () => ({ text: "# 壁仞科技会议纪要\n\n- 12月3日完成第一阶段交付\n- 分批开具发票" }),
    "GET /v1/traces/trc_dag_1/events": () => ({
      stream: sse([
        [
          "snapshot",
          {
            trace_id: "trc_dag_1", title: "butler", active: false,
            spans: [
              { span_id: "ex.1", kind: "butler", name: "butler", state: "completed", started_at: "2026-11-28T00:39:00Z", ended_at: "2026-11-28T00:40:00Z", parent_span_id: null },
              { span_id: "tl.1", kind: "tool", name: "read_personal", state: "completed", started_at: "2026-11-28T00:39:02Z", ended_at: "2026-11-28T00:39:03Z", parent_span_id: "ex.1" },
              { span_id: "tl.2", kind: "tool", name: "act_personal", state: "completed", started_at: "2026-11-28T00:39:10Z", ended_at: "2026-11-28T00:39:11Z", parent_span_id: "ex.1" },
            ],
          },
        ],
        ["end", {}],
      ]),
    }),
    "POST /v1/assistant/messages": call => ({
      json: {
        mode: "butler", state: "done", request_id: call.body.request_id, trace_id: "trc_dag_1",
        message: "已整理并归档本次会议纪要。",
        executed_actions: [
          {
            action_id: "act_b1", action: "capture_bundle", title: "已自动整理归档", summary: "公司：壁仞科技；1 条待办", undoable: true,
            details: {
              record: { id: "pnote_1", record_kind: "meeting", title: "壁仞科技项目当面交流", location: "上海壁仞科技", occurred_at: 1795761900, summary: "对齐第一阶段交付与开票。", decisions: ["12月3日完成第一阶段交付"], open_questions: ["确认发票月份"], attachment_file_ids: ["ws_raw_1"] },
              orgs: [{ id: "org_1", name: "壁仞科技" }],
              persons: [{ id: "per_1", name: "石总", organization: "壁仞科技" }],
              matters: [{ id: "ws_m1", title: "壁仞项目" }],
              todos: [{ id: "ws_t1", title: "准备第一阶段交付成果", due_at: 1796292000, status: "open" }],
              events: [], reminders: [], relations_count: 4,
            },
          },
          {
            action_id: "act_f1", action: "save_note_file", title: "已保存整理文档", summary: "2026-11-27-壁仞科技会议纪要.md", undoable: true,
            details: { item_id: "ws_doc_1", name: "2026-11-27-壁仞科技会议纪要.md", size: 800, preview: "# 壁仞科技会议纪要\n\n- 12月3日完成第一阶段交付" },
          },
          {
            action_id: "act_c1", action: "classify_file", title: "已自动业务归类归档", summary: "2026-11-27-壁仞科技会议纪要.md → 03-会议纪要", undoable: true,
            details: { item_id: "ws_doc_1", name: "2026-11-27-壁仞科技会议纪要.md", category_name: "会议纪要", folder_name: "03-会议纪要", tags: ["会议纪要", "壁仞项目"], knowledge_indexed: true },
          },
        ],
      },
    }),
  });
  const h = harness(routes);
  load(h, dir); await flush();
  h.nodes("message").value = "整理会议纪要";
  h.nodes("composer").listeners.submit({ preventDefault() {} });
  await flush(); await flush();
  const transcript = h.nodes("transcript");
  assert.ok(transcript.textContent.includes("壁仞科技项目当面交流"), "应渲染一次性成果总览标题");
  assert.ok(transcript.textContent.includes("核心结论（1）"), "应渲染核心结论区");
  assert.ok(transcript.textContent.includes("03-会议纪要") && transcript.textContent.includes("已入知识库"), "应渲染归档目录与知识库状态");
  assert.ok(transcript.textContent.includes("管家主控") && transcript.textContent.includes("查资料 / 读文档"), "对话内应直接渲染执行编排 DAG 节点");
  const previewBtn = byText(transcript, "button", "在工作台看全文")[0];
  assert.ok(previewBtn, "成果卡文档应提供右侧工作台全文预览按钮");
  previewBtn.listeners.click();
  await flush();
  const wb = h.nodes("workbench-overlay");
  assert.ok(!wb.classList.contains("hidden") && wb.textContent.includes("分批开具发票"), "点击后应在右侧分屏工作台展示文档全文");
}

async function passwordSetupHashRedirectsToWorkspace(dir) {
  const h = harness(base());
  h.context.location.hash = "#password_setup=tok_setup_1";
  h.context.location.search = "?tenant=acme";
  load(h, dir); await flush();
  assert.equal(h.context.location.replaced, "/workspace?tenant=acme#password_setup=tok_setup_1");
  assert.equal(h.calls.length, 0, "口令设置链接应立即转入完整工作台处理");
}

(async () => {
  const dir = process.argv[2];
  dir_ = dir;
  await passwordSetupHashRedirectsToWorkspace(dir);
  await classicTenantGoesBack(dir);
  await overviewAndTips(dir);
  await sendAndConfirm(dir);
  await listCardUsesVersion(dir);
  await progressFromHistoryAndRestore(dir);
  await streamingReply(dir);
  await streamingErrorKeepsInput(dir);
  await staleResultsAreDropped(dir);
  await unavailableKeepsInput(dir);
  await runningReplyFollowsLater(dir);
  await proactiveDraftRetriesSameRequest();
  await definitionDraftAndList();
  await connectionConsentAndRevoke();
  await sharedDraftUsesProposalKey();
  await handoffAndApprovalActions();
  await handoffDraftUsesProposalKey();
  await friendlyFormatAndLocationPrompt(dir);
  await executedActionsCardAndUndo(dir);
  await contactsDrawerAddressBook(dir);
  await contactsDrawerFileImport(dir);
  await contactsDrawerPeopleGraph(dir);
  await boardDrawerTodosAndQuickAdd(dir);
  await liveNotificationsAndBrowserPopup(dir);
  await chatReminderPromptRequestsDesktopPermission(dir);
  await boardDrawerCompletedRemindersAndTimestamp(dir);
  await planFocusSupersededDiffAndWorkbench(dir);
  await frontendPluginHostBridgeAndRichRenderers(dir);
  await chatAttachmentsPreviewAndDownload(dir);
  await outcomeShowcaseAndDagFlow(dir);
  console.log("entry behavior passed");
})().catch(error => { console.error(error); process.exit(1); });
