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
    console: { error() {}, log: console.log }, crypto, AbortController, DOMException, TextDecoder, TextEncoder, URLSearchParams, Intl,
    document: { getElementById: node, createElement: element }, fetch, Math, JSON, Date, Promise, Map, Set,
    location: { search: "", pathname: "/console/entry.html", assign() {}, replaced: null, replace(to) { this.replaced = to; } },
  });
  return { context, nodes: node, calls };
}

function load(h, dir) {
  for (const name of ["entry.js", "cards.js", "module-cards.js", "collab-cards.js", "progress-card.js", "dashboard.js", "tips.js", "entry-boot.js"]) {
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

async function classicTenantGoesBack(dir) {
  const h = harness(base({ "GET /v1/me": () => ({ json: { ...ME.json, entry: "classic" } }) }));
  load(h, dir); await flush();
  assert.equal(h.context.location.replaced, "index.html");
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

(async () => {
  const dir = process.argv[2];
  dir_ = dir;
  await classicTenantGoesBack(dir);
  await overviewAndTips(dir);
  await sendAndConfirm(dir);
  await listCardUsesVersion(dir);
  await progressFromHistoryAndRestore(dir);
  await staleResultsAreDropped(dir);
  await unavailableKeepsInput(dir);
  await proactiveDraftRetriesSameRequest();
  await definitionDraftAndList();
  await connectionConsentAndRevoke();
  await sharedDraftUsesProposalKey();
  await handoffAndApprovalActions();
  await handoffDraftUsesProposalKey();
  console.log("entry behavior passed");
})().catch(error => { console.error(error); process.exit(1); });
