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
    tag, children: [], listeners: {}, dataset: {}, style: {}, value: "", disabled: false, parentNode: null,
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
    append(...items) { for (const item of items) this.appendChild(item); },
    replaceChildren(...items) { for (const c of this.children) c.parentNode = null; this.children = []; this.append(...items); },
    remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(n => n !== this); this.parentNode = null; } },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() {}, scrollIntoView() {}, requestSubmit() { this.listeners.submit({ preventDefault() {} }); },
    setAttribute() {},
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
    const call = { method, url, body: init.body ? JSON.parse(init.body) : undefined, headers: init.headers || {} };
    calls.push(call);
    const handler = routes[method + " " + url] || routes[method + " " + url.split("?")[0]];
    if (!handler) throw new Error("未预期的请求 " + method + " " + url);
    const result = await handler(call, init);
    const status = result.status || 200;
    return { ok: status < 400, status, text: async () => JSON.stringify(result.json || {}), body: result.stream };
  };
  const context = vm.createContext({
    console: { error() {}, log: console.log }, crypto, AbortController, DOMException, TextDecoder, URLSearchParams, Intl,
    document: { getElementById: node, createElement: element }, fetch, Math, JSON, Date, Promise, Map, Set,
    location: { search: "", pathname: "/console/entry.html", assign() {} },
  });
  return { context, nodes: node, calls };
}

function load(h, dir) {
  for (const name of ["entry.js", "cards.js", "progress-card.js", "dashboard.js", "tips.js", "entry-boot.js"]) {
    vm.runInContext(fs.readFileSync(path.join(dir, name), "utf8"), h.context, { filename: name });
  }
}

const settle = () => new Promise(resolve => setImmediate(resolve));
async function flush() { for (let i = 0; i < 20; i++) await settle(); }

const ME = { json: { user_id: "usr_alice", tenant_id: "acme", display_name: "Alice" } };
const OVERVIEW = { json: { blocks: [
  { key: "today", status: "ok", count: 2, items: [{ kind: "todo", ref: "ws_1", title: "周报", at: 1 }] },
  { key: "waiting", status: "error", error: "runtime_unavailable", count: null, items: [] },
  { key: "unread", status: "ok", count: 0, items: [] },
], tips: [{ text: "试试：明天提醒我开会", prompt: "明天上午9点提醒我开会" }] } };

function base(extra = {}) {
  return {
    "GET /v1/me": () => ME,
    "GET /v1/workspace/messages": () => ({ json: { items: [] } }),
    "GET /v1/dashboard": () => OVERVIEW,
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

(async () => {
  const dir = process.argv[2];
  await overviewAndTips(dir);
  await sendAndConfirm(dir);
  await listCardUsesVersion(dir);
  await progressFromHistoryAndRestore(dir);
  await staleResultsAreDropped(dir);
  await unavailableKeepsInput(dir);
  console.log("entry behavior passed");
})().catch(error => { console.error(error); process.exit(1); });
