// 极简入口核心：请求、身份代际、对话消息。没有构建步骤，没有依赖。
// 身份切换或退出时代际加一并中止全部在途请求；旧代际的异步结果一律丢弃，不回填新身份。
"use strict";

const $ = (id) => document.getElementById(id);
const entry = {
  epoch: 0, controller: new AbortController(), me: null, inflight: 0,
  cards: new Map(), cleanups: new Set(), seen: 0, shown: new Set(),
};
// 管家常在线（ADR 0020）：结果随时可能写进主对话（进行中的一轮结束、匠人回报），按游标补读
const FOLLOW_MS = 5000;

// 新代际：中止在途请求、关掉流、清空页面上的全部个人状态。
function resetEntry() {
  entry.epoch += 1;
  entry.controller.abort();
  entry.controller = new AbortController();
  for (const cleanup of entry.cleanups) cleanup();
  entry.cleanups.clear();
  entry.cards.clear();
  entry.me = null;
  entry.inflight = 0;
  entry.seen = 0;
  entry.shown = new Set();
  for (const id of ["overview", "stage", "transcript", "tips"]) if ($(id)) $(id).replaceChildren();
  $("message").value = "";
  $("entry-user").textContent = "";
  $("entry-main").classList.add("hidden");
}

function isStale(epoch) { return epoch !== entry.epoch; }

// 统一请求：同源 cookie，写请求带防跨站头；响应回来时代际已变就当作中止。
async function entryApi(method, path, options = {}) {
  const epoch = entry.epoch;
  const init = { method, credentials: "same-origin", signal: options.signal || entry.controller.signal,
    headers: { "X-Requested-By": "xuanpu", ...(options.headers || {}) } };
  if (options.json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.json);
  }
  return readJson(await fetch(path, init), epoch);
}

// 读 JSON 响应：代际已变就当作中止；非 2xx 抛出带错误码与状态的错误（401 时回到未登录）。
async function readJson(response, epoch) {
  const text = await response.text();
  if (isStale(epoch)) throw new DOMException("身份已切换", "AbortError");
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  if (!response.ok) {
    const error = body.error || { code: "http_" + response.status, message: "请求失败（" + response.status + "）" };
    if (response.status === 401 && entry.me) resetEntry();
    throw Object.assign(new Error(error.message), { code: error.code, status: response.status });
  }
  return body;
}

// 小工具：建元素，文本一律走 textContent，不拼 HTML。
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function button(label, onClick, className) {
  const node = el("button", label, className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function announce(text) { $("entry-status").textContent = text; }

function report(error) {
  if (error && error.name === "AbortError") return;
  console.error(error);
  announce(error && error.message ? error.message : String(error));
}

function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "";
}

function timezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai"; }
  catch { return "Asia/Shanghai"; }
}

function append(role, text, extra) {
  const node = el("div", text, "msg " + role + (extra ? " " + extra : ""));
  $("transcript").appendChild(node);
  return node;
}

function appendCard(card) {
  $("transcript").appendChild(card);
  if (typeof card.scrollIntoView === "function") card.scrollIntoView({ block: "nearest" });
  return card;
}

// 读取身份；未登录（401）是正常状态，只显示登录按钮。
async function connectEntry() {
  resetEntry();
  const epoch = entry.epoch;
  try {
    const me = await entryApi("GET", "/v1/me");
    if (isStale(epoch)) return false;
    if (!me.user_id) throw new Error("新入口只给员工使用，管理员请走管理员入口。");
    // 未启用极简入口的租户仍用旧页
    if (me.entry !== "minimal") { location.replace("index.html"); return false; }
    entry.me = me;
    $("entry-user").textContent = me.display_name || "";
    $("entry-login").classList.add("hidden");
    $("entry-password-login").classList.add("hidden");
    $("entry-main").classList.remove("hidden");
    // 常驻小院（R7）：打开就在，说一句话后跟上这次请求；不支持 SVG 时没有这一块
    if (globalThis.Courtyard && Courtyard.stage) Courtyard.stage.mount($("stage"), { onSelect: (s, k) => Courtyard.card.select(s, k) });
    // 历史与概览各自失败只提示，不当作未登录
    await Promise.all([loadHistory(epoch).catch(report), refreshOverview().catch(report), refreshTips()]);
    followMessages(epoch);
    return !isStale(epoch);
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return false;
    $("entry-login").classList.remove("hidden");
    $("entry-password-login").classList.remove("hidden");
    if (error.status !== 401) report(error);
    return false;
  }
}

// 刷新后恢复：读主对话最近 50 条，按原样重建消息与卡片。
async function loadHistory(epoch) {
  const body = await entryApi("GET", "/v1/workspace/messages?latest=true&limit=50");
  if (isStale(epoch)) return;
  for (const message of body.items || []) showMessage(message, { restore: true });
}

// 主对话里的一条消息：按标识去重（本页已显示过的不再显示），记下游标。
function showMessage(message, options = {}) {
  if (typeof message.id === "number") entry.seen = Math.max(entry.seen, message.id);
  const key = message.request_id || "";
  if (key && entry.shown.has(key)) return;
  if (key) entry.shown.add(key);
  append(message.role === "user" ? "user" : "assistant", message.text);
  renderMessageCards(message.metadata || {}, key, options);
}

// 按游标补读主对话的新消息：进行中的一轮结束后的回复、匠人完成后管家的回报、别处说的话。
function followMessages(epoch) {
  if (typeof setTimeout !== "function") return;
  let timer = null;
  const tick = async () => {
    if (isStale(epoch)) return;
    try {
      const body = await entryApi("GET", "/v1/workspace/messages?after_id=" + entry.seen + "&limit=50");
      if (isStale(epoch)) return;
      for (const message of body.items || []) showMessage(message);
    } catch (error) {
      if (!isStale(epoch)) report(error);
    }
    if (!isStale(epoch)) timer = setTimeout(tick, FOLLOW_MS);
  };
  timer = setTimeout(tick, FOLLOW_MS);
  entry.cleanups.add(() => { if (timer !== null) clearTimeout(timer); });
}

// 一条助手消息带的卡片：草稿确认卡、列表卡、按钮卡、进展卡。
function renderMessageCards(metadata, requestId, options = {}) {
  const route = metadata.route || {};
  if (typeof renderRouteCards === "function") renderRouteCards(route, options);
  const traceId = metadata.butler && metadata.butler.trace_id;
  if (traceId && typeof openProgressCard === "function") openProgressCard(traceId, { follow: !options.restore });
}

function requestId() {
  return "web-" + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
}

// 发送：先占位本人原话；不锁输入框，员工可以接着交代下一件事。回执以流到达（R7）：先拿到 trace 标识，
// 常驻小院立刻跟上这件事；管家的回复逐段出现。服务不给流时照旧等 JSON 回执。失败时输入回到输入框。
async function sendMessage(text) {
  if (!entry.me || !text.trim()) return;
  const epoch = entry.epoch, id = requestId(), live = { bubble: null };
  entry.inflight += 1;
  entry.shown.add(id + ":user");
  append("user", text);
  try {
    const reply = await postMessage({ request_id: id, text, timezone: timezone() }, epoch, live);
    if (!isStale(epoch)) finishReply(id, reply, live);
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return;
    if (error.soft) { endBubble(live, error.message, "meta"); return; }
    if (live.bubble) live.bubble.remove();
    if (!$("message").value) $("message").value = text;
    if (typeof showSystemStatus === "function") showSystemStatus(error);
    report(error);
  } finally {
    if (!isStale(epoch)) entry.inflight -= 1;
  }
}

// 发出一句话：要流；拿到流就边读边显示，否则按 JSON 读回执。
async function postMessage(body, epoch, live) {
  const response = await fetch("/v1/assistant/messages", {
    method: "POST", credentials: "same-origin", signal: entry.controller.signal, body: JSON.stringify(body),
    headers: { "X-Requested-By": "xuanpu", "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
  });
  if (isStale(epoch)) throw new DOMException("身份已切换", "AbortError");
  const type = (response.headers && typeof response.headers.get === "function" && response.headers.get("content-type")) || "";
  if (!response.ok || !type.includes("text/event-stream") || !response.body || typeof readFrames !== "function") {
    return readJson(response, epoch);
  }
  let receipt = null, failure = null;
  await readFrames(response, (name, data) => {
    if (isStale(epoch)) return;
    if (name === "started") onStarted(data, live);
    else if (name === "delta") onDelta(data, live);
    else if (name === "receipt") receipt = data;
    else if (name === "error") failure = Object.assign(new Error(data.message || "管家暂时无法处理"), { code: data.code, status: 503 });
  });
  if (failure) throw failure;
  // 流断了但这一轮还在后台办：不当作失败，结果会经补读出现在对话里
  if (!receipt) throw Object.assign(new Error("连接断了，这件事还在后台办，结果会出现在这里。"), { soft: true });
  return receipt;
}

// 第一帧：先放一个正在写的回复气泡，再开进展卡并让常驻小院跟上这件事。
function onStarted(data, live) {
  if (!live.bubble) live.bubble = append("assistant", "", "streaming");
  if (data.trace_id && typeof openProgressCard === "function") openProgressCard(data.trace_id, { follow: true, focus: true });
}

function onDelta(data, live) {
  if (!live.bubble) live.bubble = append("assistant", "", "streaming");
  live.bubble.textContent += data.text || "";
}

function endBubble(live, text, extra) {
  if (!live.bubble) { append("assistant", text, extra); return; }
  live.bubble.textContent = text;
  live.bubble.className = "msg assistant" + (extra ? " " + extra : "");
}

// 回执到了：回复以回执为准（流里拼的只是过程），再出卡片。
function finishReply(id, reply, live) {
  if (reply.state === "running") {
    endBubble(live, "收到，正在处理。好了我会在这里告诉你，你可以先说别的。", "meta");
    if (reply.trace_id && typeof openProgressCard === "function") openProgressCard(reply.trace_id, { follow: true, focus: true });
    return;
  }
  entry.shown.add(id + ":assistant");
  endBubble(live, reply.message || "已收到。");
  renderMessageCards({ route: reply, butler: reply.trace_id ? { trace_id: reply.trace_id } : null }, id);
  refreshOverview().catch(report);
}

async function logoutEntry() {
  resetEntry();
  try { await fetch("/v1/auth/logout", { method: "POST", credentials: "same-origin",
    headers: { "X-Requested-By": "xuanpu" } }); }
  finally {
    $("entry-login").classList.remove("hidden");
    $("entry-password-login").classList.remove("hidden");
    announce("已退出");
  }
}
