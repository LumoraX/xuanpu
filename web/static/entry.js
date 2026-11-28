// 极简入口核心：请求、身份代际、对话消息。没有构建步骤，没有依赖。
// 身份切换或退出时代际加一并中止全部在途请求；旧代际的异步结果一律丢弃，不回填新身份。
"use strict";

const $ = (id) => document.getElementById(id);
const entry = {
  epoch: 0, controller: new AbortController(), me: null, inflight: 0,
  cards: new Map(), cleanups: new Set(), seen: 0, shown: new Set(),
  location: null, locationState: "unknown", replies: new Map(), steps: new Map(), latestReply: "",
  attachments: [], fileMeta: new Map(), fileMetaPending: new Map(), settings: null,
};
// 管家常在线（ADR 0020）：结果随时可能写进主对话（进行中的一轮结束、匠人回报），按游标补读
const FOLLOW_MS = 5000;
const LOCATION_TTL_MS = 120000;
const LOCATION_HINT_RE = /位置|定位|出发|路线|导航|附近|周边|天气|酒店|怎么去|打车|地铁|公交|开车|驾车|步行|骑行|到.{1,12}去/;
const REMINDER_HINT_RE = /提醒|闹钟|定时|叫我|remind/i;

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
  entry.replies = new Map();
  entry.steps = new Map();
  entry.latestReply = "";
  entry.attachments = [];
  entry.fileMeta = new Map();
  entry.fileMetaPending = new Map();
  entry.settings = null;
  renderComposerFiles();
  if (typeof closeAttachmentViewer === "function") closeAttachmentViewer();
  if (typeof closeContactsDrawer === "function") closeContactsDrawer(true);
  if (typeof closeBoardDrawer === "function") closeBoardDrawer(true);
  if (typeof closeWorkbenchDrawer === "function") closeWorkbenchDrawer();
  if (globalThis.XuanpuPluginHost) XuanpuPluginHost.resetTenantPlugins();
  if (typeof resetEntryNotifications === "function") resetEntryNotifications();
  for (const id of ["overview", "stage", "transcript", "tips"]) if ($(id)) $(id).replaceChildren();
  $("message").value = "";
  $("entry-user").textContent = "";
  $("entry-main").classList.add("hidden");
  if ($("entry-auth-gate")) $("entry-auth-gate").classList.remove("hidden");
  if (document.body && document.body.classList) {
    document.body.classList.remove("authenticated");
    document.body.classList.add("unauthenticated");
  }
}

function isStale(epoch) { return epoch !== entry.epoch; }

function preferredLanguages() {
  const docLang = globalThis.document && document.documentElement && document.documentElement.lang;
  const nav = globalThis.navigator || {};
  const languages = [docLang, ...(nav.languages && nav.languages.length ? nav.languages : [nav.language])];
  return [...new Set(languages.filter(Boolean))].join(",") || "zh-CN";
}

// 统一请求：同源 cookie，写请求带防跨站头；响应回来时代际已变就当作中止。
async function entryApi(method, path, options = {}) {
  const epoch = entry.epoch;
  const init = { method, credentials: "same-origin", signal: options.signal || entry.controller.signal,
    headers: { "X-Requested-By": "xuanpu", "Accept-Language": preferredLanguages(), ...(options.headers || {}) } };
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

function syncLocationButton() {
  const btn = $("entry-location");
  if (!btn) return;
  const labels = {
    granted: "已定位",
    loading: "定位中…",
    denied: "定位未开",
    unavailable: "无法定位",
    prompt: "开启定位",
    unknown: "开启定位",
  };
  btn.textContent = labels[entry.locationState] || "开启定位";
  btn.className = "loc-btn" + (entry.locationState === "granted" ? " ok" : entry.locationState === "denied" ? " warn" : "");
}

// 静默探测浏览器定位权限状态（不弹授权框），用于初始化定位按钮状态。
async function probeLocationPermission() {
  const nav = globalThis.navigator;
  if (!nav || !nav.geolocation) { entry.locationState = "unavailable"; syncLocationButton(); return; }
  if (nav.permissions && typeof nav.permissions.query === "function") {
    try {
      const status = await nav.permissions.query({ name: "geolocation" });
      if (status && status.state) {
        entry.locationState = status.state;
        syncLocationButton();
      }
      return;
    } catch { /* 浏览器不支持 permissions.query 时留作按需触发 */ }
  }
  entry.locationState = "prompt";
  syncLocationButton();
}

// 获取浏览器经纬度：已授权直接读取；未授权时若员工点击开启或问题涉及位置/路线/天气，则主动唤起浏览器定位授权。
async function currentLocation(text = "", { forcePrompt = false } = {}) {
  const nav = globalThis.navigator;
  if (!nav || !nav.geolocation) {
    entry.locationState = "unavailable";
    syncLocationButton();
    return null;
  }
  if (!forcePrompt && entry.location && Date.now() - entry.location.at < LOCATION_TTL_MS) {
    entry.locationState = "granted";
    syncLocationButton();
    return {
      latitude: entry.location.latitude,
      longitude: entry.location.longitude,
      ...(typeof entry.location.accuracy_meters === "number" ? { accuracy_meters: entry.location.accuracy_meters } : {}),
    };
  }
  let state = entry.locationState;
  if (nav.permissions && typeof nav.permissions.query === "function") {
    try {
      const status = await nav.permissions.query({ name: "geolocation" });
      if (status && status.state) state = status.state;
    } catch { /* 忽略不支持的权限查询 */ }
  }
  const wantPrompt = forcePrompt || LOCATION_HINT_RE.test(String(text || ""));
  if (state === "denied") {
    entry.locationState = "denied";
    syncLocationButton();
    if (forcePrompt) announce("浏览器已禁用本站点定位，请在地址栏左侧允许位置权限后重试。");
    return null;
  }
  if (state !== "granted" && !wantPrompt) return null;
  entry.locationState = "loading";
  syncLocationButton();
  try {
    const timeout = state === "granted" ? 2500 : 10000;
    const pos = await new Promise((resolve, reject) => {
      nav.geolocation.getCurrentPosition(resolve, reject, { maximumAge: LOCATION_TTL_MS, timeout, enableHighAccuracy: false });
    });
    if (!pos || !pos.coords) {
      entry.locationState = "prompt";
      syncLocationButton();
      return null;
    }
    const loc = { latitude: pos.coords.latitude, longitude: pos.coords.longitude };
    if (typeof pos.coords.accuracy === "number") loc.accuracy_meters = pos.coords.accuracy;
    entry.location = { ...loc, at: Date.now() };
    entry.locationState = "granted";
    syncLocationButton();
    return loc;
  } catch (err) {
    entry.locationState = err && err.code === 1 ? "denied" : "prompt";
    syncLocationButton();
    if (forcePrompt && err && err.code === 1) announce("未授予浏览器定位权限，请在地址栏开启后重试。");
    return null;
  }
}

// 助手气泡清洗与 Markdown/表格/Mermaid 排版已拆分至 bubble-format.js。

function recordButlerStep(traceId, rawText) {
  const text = String(rawText || "").trim();
  if (traceId) {
    if (text) entry.steps.set(traceId, text);
    else entry.steps.delete(traceId);
  }
  if (globalThis.Courtyard && Courtyard.stage && typeof Courtyard.stage.setStep === "function") {
    Courtyard.stage.setStep(traceId || null, text);
  }
}

function recordButlerReply(traceId, rawText, options = {}) {
  const text = cleanUserText(rawText);
  if (!text) return;
  if (traceId) entry.replies.set(traceId, text);
  entry.latestReply = text;
  if (globalThis.Courtyard && Courtyard.stage && typeof Courtyard.stage.setReply === "function") {
    Courtyard.stage.setReply(traceId || null, text, options);
  }
}

// 对话记录在宽窗口里是独立的滚动区：员工没往上翻时，任何新内容（含卡片后来长高）都跟着滚到底。
function pinTranscript() {
  const list = $("transcript");
  if (typeof MutationObserver !== "function" || list.dataset.pinned) return;
  list.dataset.pinned = "1";
  // 只有员工自己滚动（滚轮、触摸、按键、拖动）才改变「跟到底」；布局变化引起的滚动不算
  let pinned = true, touchedAt = 0;
  for (const name of ["wheel", "touchmove", "keydown", "mousedown"]) list.addEventListener(name, () => { touchedAt = Date.now(); }, { passive: true });
  list.addEventListener("scroll", () => {
    if (Date.now() - touchedAt < 1500) pinned = list.scrollHeight - list.scrollTop - list.clientHeight < 160;
  });
  const follow = () => { if (pinned) list.scrollTop = list.scrollHeight; };
  const observer = new MutationObserver(follow);
  observer.observe(list, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["class", "open"] });
  // 卡片里的图形、折叠区长高不一定有 DOM 变化：定时再对齐一次
  const timer = setInterval(follow, 500);
  entry.cleanups.add(() => { observer.disconnect(); clearInterval(timer); delete list.dataset.pinned; });
}

function stickBottom(wasNear) {
  const list = $("transcript");
  if (wasNear && typeof list.scrollHeight === "number") list.scrollTop = list.scrollHeight;
}

function nearBottom() {
  const list = $("transcript");
  return typeof list.scrollHeight !== "number" || list.scrollHeight - list.scrollTop - list.clientHeight < 160;
}

function append(role, text, extra, options) {
  const near = nearBottom();
  const hasAttachments = options && Array.isArray(options.attachments) && options.attachments.length > 0;
  const node = el("div", (role === "assistant" && !extra) || hasAttachments ? null : text, "msg " + role + (extra ? " " + extra : ""));
  if (role === "assistant" && !extra && text) {
    renderAssistantBubble(node, text);
  } else if (hasAttachments) {
    if (text) node.appendChild(el("div", text, "msg-user-text"));
    if (typeof renderMessageAttachments === "function") renderMessageAttachments(node, options.attachments);
    else node.appendChild(el("div", "[附件：" + options.attachments.map(f => (typeof f === "string" ? f : f.name || f.item_id)).join("、") + "]", "msg-attachments-fallback"));
  }
  $("transcript").appendChild(node);
  stickBottom(near);
  return node;
}

function appendCard(card) {
  const near = nearBottom();
  $("transcript").appendChild(card);
  stickBottom(near);
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
    if ($("entry-auth-gate")) $("entry-auth-gate").classList.add("hidden");
    if (document.body && document.body.classList) {
      document.body.classList.add("authenticated");
      document.body.classList.remove("unauthenticated");
    }
    $("entry-main").classList.remove("hidden");
    pinTranscript();
    // 常驻小院（R7）：打开就在，说一句话后跟上这次请求；不支持 SVG 时没有这一块
    if (globalThis.Courtyard && Courtyard.stage) Courtyard.stage.mount($("stage"), { onSelect: (s, k) => Courtyard.card.select(s, k) });
    probeLocationPermission().catch(() => {});
    loadAutonomySetting(epoch).catch(() => {});
    if (typeof syncLiveFocusPill === "function") syncLiveFocusPill(epoch).catch(() => {});
    if (globalThis.XuanpuPluginHost) XuanpuPluginHost.loadRemotePlugins(epoch).catch(() => {});
    // 历史与概览各自失败只提示，不当作未登录
    await Promise.all([loadHistory(epoch).catch(report), refreshOverview().catch(report), refreshTips()]);
    followMessages(epoch);
    if (typeof startEntryNotifications === "function") startEntryNotifications(epoch);
    return !isStale(epoch);
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return false;
    $("entry-login").classList.remove("hidden");
    $("entry-password-login").classList.remove("hidden");
    if (error.status !== 401) report(error);
    return false;
  }
}

// 刷新后恢复：读主对话最近 50 条，按原样重建消息与卡片；常驻小院默认跟上最近一条进展（折叠态）。
async function loadHistory(epoch) {
  const body = await entryApi("GET", "/v1/workspace/messages?latest=true&limit=50");
  if (isStale(epoch)) return;
  for (const message of body.items || []) showMessage(message, { restore: true });
  if (globalThis.Courtyard && Courtyard.stage && Courtyard.stage.mounted() && !Courtyard.stage.hasFocus()) {
    const traces = [...entry.cards.entries()].filter(([k, c]) => k.startsWith("trace:") && c.court);
    if (traces.length) {
      const latest = traces[traces.length - 1][1];
      latest.follow();
      Courtyard.stage.focus(latest.court);
    }
  }
}

// 主对话里的一条消息：按标识去重（本页已显示过的不再显示），记下游标。
function showMessage(message, options = {}) {
  if (typeof message.id === "number") entry.seen = Math.max(entry.seen, message.id);
  const key = message.request_id || "";
  if (key && entry.shown.has(key)) return;
  if (key) entry.shown.add(key);
  const attachments = Array.isArray(message.attachments) && message.attachments.length ? message.attachments : null;
  append(message.role === "user" ? "user" : "assistant", message.text, null, attachments ? { attachments } : undefined);
  const traceId = message.metadata && message.metadata.butler && message.metadata.butler.trace_id;
  if (message.role !== "user" && message.text) recordButlerReply(traceId || null, message.text, { streaming: false });
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

// 一条助手消息带的卡片：自主办理回执卡、草稿确认卡、列表卡、按钮卡、进展卡。
function renderMessageCards(metadata, requestId, options = {}) {
  const route = metadata.route || {};
  if (typeof renderRouteCards === "function") renderRouteCards(route, { ...options, requestId });
  const traceId = metadata.butler && metadata.butler.trace_id;
  if (traceId && typeof openProgressCard === "function") openProgressCard(traceId, { follow: !options.restore });
}

function syncAutonomyButton() {
  const btn = $("entry-autonomy");
  if (!btn || !btn.dataset || btn.dataset.autonomy !== "1") return;
  const on = !entry.settings || entry.settings.auto_execute_personal !== false;
  btn.textContent = on ? "自主办理：开" : "自主办理：关";
  btn.setAttribute("aria-pressed", String(on));
  btn.classList.toggle("active", on);
}

async function loadAutonomySetting(epoch) {
  const btn = $("entry-autonomy");
  if (!btn || !btn.dataset || btn.dataset.autonomy !== "1") return;
  const data = await entryApi("GET", "/v1/workspace/settings");
  if (isStale(epoch)) return;
  entry.settings = data;
  syncAutonomyButton();
}

async function toggleAutonomySetting() {
  if (!entry.me) return;
  const epoch = entry.epoch;
  if (!entry.settings) await loadAutonomySetting(epoch);
  if (isStale(epoch) || !entry.settings) return;
  const next = entry.settings.auto_execute_personal === false;
  const body = {
    assistant_name: entry.settings.assistant_name || "玄圃助手",
    user_title: entry.settings.user_title || "",
    style: entry.settings.style || "简洁、清晰",
    timezone: entry.settings.timezone || timezone(),
    auto_execute_personal: next,
    proactive_checks: entry.settings.proactive_checks,
    proactive_channels: entry.settings.proactive_channels || ["inbox"],
    version: entry.settings.version,
  };
  const updated = await entryApi("PUT", "/v1/workspace/settings", { json: body });
  if (isStale(epoch)) return;
  entry.settings = updated;
  syncAutonomyButton();
  announce(next ? "已开启私人事务自主办理（可一键撤回）" : "已切换为先确认草稿模式");
}

function renderComposerFiles() {
  const box = $("composer-files");
  if (!box) return;
  if (!entry.attachments.length) {
    box.replaceChildren();
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.replaceChildren(...entry.attachments.map((file, idx) => {
    const chip = el("span", null, "composer-file-chip");
    const nameBtn = button(file.name, () => {
      if (typeof openAttachmentViewer === "function") openAttachmentViewer(file).catch(report);
    }, "composer-file-view");
    nameBtn.title = "点击查看附件内容";
    const dl = typeof buildDownloadLink === "function" ? buildDownloadLink(file, "composer-file-dl", "下载") : null;
    const removeBtn = button("×", () => { entry.attachments.splice(idx, 1); renderComposerFiles(); }, "composer-file-remove");
    removeBtn.title = "移除附件";
    if (dl) chip.append(nameBtn, dl, removeBtn);
    else chip.append(nameBtn, removeBtn);
    return chip;
  }));
}

async function uploadAttachmentFiles(fileList) {
  if (!entry.me || !fileList || !fileList.length) return;
  const epoch = entry.epoch;
  const files = [...fileList].slice(0, Math.max(0, 8 - entry.attachments.length));
  for (const file of files) {
    const reqId = requestId();
    const params = new URLSearchParams({ name: file.name, request_id: reqId });
    const response = await fetch("/v1/workspace/files?" + params, {
      method: "POST",
      credentials: "same-origin",
      signal: entry.controller.signal,
      headers: {
        "X-Requested-By": "xuanpu",
        "Accept-Language": preferredLanguages(),
        "Content-Type": file.type || "text/plain",
      },
      body: file,
    });
    const saved = await readJson(response, epoch);
    if (isStale(epoch)) return;
    const meta = {
      item_id: saved.id,
      revision: (saved.data && saved.data.current_revision) || 1,
      name: (saved.data && saved.data.name) || file.name,
      mime_type: (saved.data && saved.data.mime_type) || file.type || "",
      size: (saved.data && typeof saved.data.size === "number") ? saved.data.size : (file.size || 0),
    };
    if (typeof attCacheMeta === "function") attCacheMeta(meta);
    else if (entry.fileMeta) entry.fileMeta.set(meta.item_id, meta);
    entry.attachments.push(meta);
    renderComposerFiles();
  }
  announce("附件已上传，发送消息即可让助理自动整理");
}

function requestId() {
  return "web-" + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
}

// 发送：先占位本人原话；不锁输入框，员工可以接着交代下一件事。回执以流到达（R7）：先拿到 trace 标识，
// 常驻小院立刻跟上这件事；管家的回复逐段出现。服务不给流时照旧等 JSON 回执。失败时输入回到输入框。
async function sendMessage(text) {
  const attached = entry.attachments.slice();
  const cleanText = String(text || "").trim() || (attached.length ? "请帮我读取并自动整理我上传的文档：" + attached.map(f => f.name).join("、") : "");
  if (!entry.me || !cleanText) return;
  const epoch = entry.epoch, id = requestId(), live = { bubble: null, traceId: null };
  entry.inflight += 1;
  entry.shown.add(id + ":user");
  entry.attachments = [];
  renderComposerFiles();
  append("user", cleanText, null, attached.length ? { attachments: attached } : undefined);
  // 员工在对话里设置提醒时，趁当前发送手势顺便请求浏览器系统弹窗权限，避免到点时因未授权而无系统通知。
  if (REMINDER_HINT_RE.test(cleanText) &&
      typeof browserNotificationState === "function" && browserNotificationState() === "default" &&
      typeof requestBrowserNotificationPermission === "function") {
    requestBrowserNotificationPermission().catch(() => {});
  }
  try {
    const payload = { request_id: id, text: cleanText, timezone: timezone() };
    if (attached.length) payload.input_files = attached.map(f => ({ item_id: f.item_id, revision: f.revision }));
    const loc = await currentLocation(cleanText);
    if (loc) payload.location = loc;
    const reply = await postMessage(payload, epoch, live);
    if (!isStale(epoch)) finishReply(id, reply, live);
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return;
    if (error.soft) { endBubble(live, error.message, "meta"); return; }
    if (live.bubble) live.bubble.remove();
    if (!$("message").value) $("message").value = text;
    if (attached.length && !entry.attachments.length) { entry.attachments = attached; renderComposerFiles(); }
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
    headers: { "X-Requested-By": "xuanpu", "Accept-Language": preferredLanguages(),
      "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
  });
  if (isStale(epoch)) throw new DOMException("身份已切换", "AbortError");
  const type = (response.headers && typeof response.headers.get === "function" && response.headers.get("content-type")) || "";
  if (!response.ok || !type.includes("text/event-stream") || !response.body || typeof readFrames !== "function") {
    return readJson(response, epoch);
  }
  let receipt = null, failure = null, superseded = null;
  await readFrames(response, (name, data) => {
    if (isStale(epoch)) return;
    if (name === "started") onStarted(data, live);
    else if (name === "plan") onPlan(data, live);
    else if (name === "focus") onFocus(data, live);
    else if (name === "step") onStep(data, live);
    else if (name === "delta") onDelta(data, live);
    else if (name === "superseded") { superseded = data; onSuperseded(data, live); }
    else if (name === "receipt") receipt = data;
    else if (name === "error") failure = Object.assign(new Error(data.message || "管家暂时无法处理"), { code: data.code, status: 503 });
  });
  if (failure) throw failure;
  if (superseded && !receipt) return { mode: "butler", state: "superseded", ...superseded };
  // 流断了但这一轮还在后台办：不当作失败，结果会经补读出现在对话里
  if (!receipt) throw Object.assign(new Error("连接断了，这件事还在后台办，结果会出现在这里。"), { soft: true });
  return receipt;
}

// 第一帧：先放一个正在写的回复气泡，再开进展卡并让常驻小院跟上这件事。
function onStarted(data, live) {
  if (!live.bubble) live.bubble = append("assistant", "", "streaming");
  if (data.trace_id) {
    live.traceId = data.trace_id;
    if (typeof openProgressCard === "function") openProgressCard(data.trace_id, { follow: true, focus: true });
  }
}

function ensureLiveRegions(live) {
  if (!live.bubble) live.bubble = append("assistant", "", "streaming");
  if (!live.stepsBox) {
    live.stepsBox = el("div", null, "live-steps");
    live.textBox = el("div", "", "live-text");
    live.steps = [];
    live.bubble.replaceChildren(live.stepsBox, live.textBox);
  }
}

function onPlan(data, live) {
  const planText = (data && data.text) || "";
  if (!planText) return;
  const near = nearBottom();
  ensureLiveRegions(live);
  if (!live.planBox) {
    live.planBox = el("div", null, "live-plan");
    live.bubble.insertBefore(live.planBox, live.stepsBox);
  }
  live.planBox.textContent = "规划：" + planText;
  // 开场规划已物理分离，清空前置文本缓冲以便只展示末轮正文
  if (live.textBox) live.textBox.textContent = "";
  stickBottom(near);
}

function onFocus(data, live) {
  if (!data) return;
  if (typeof updateLiveFocusPill === "function") updateLiveFocusPill(data);
  if (data.focus) recordButlerStep(live.traceId || data.trace_id, data.focus);
}

function onSuperseded(data, live) {
  endBubble(live, "已并入后续新消息合并重办…", "meta");
  recordButlerStep(live.traceId, "");
}

function onStep(data, live) {
  const label = (data && data.text) || "";
  if (!label) return;
  const near = nearBottom();
  ensureLiveRegions(live);
  for (const prev of live.stepsBox.children) {
    prev.classList.remove("active");
    if (prev.firstChild) prev.firstChild.textContent = "✓";
  }
  const item = el("div", null, "live-step active");
  item.append(el("span", "▸"), el("span", label));
  live.stepsBox.append(item);
  live.steps.push(label);
  recordButlerStep(live.traceId, label);
  recordButlerReply(live.traceId, label, { streaming: true });
  if (typeof updateLiveFocusPill === "function" && (!workbenchState.liveFocus || workbenchState.liveFocus.status !== "active")) {
    updateLiveFocusPill({ status: "step", focus: label, trace_id: live.traceId || null });
  }
  stickBottom(near);
}

function onDelta(data, live) {
  const near = nearBottom();
  const chunk = data.text || "";
  if (chunk.trim() && !live.streamingText) {
    live.streamingText = true;
    recordButlerStep(live.traceId, "正在回复…");
  }
  if (live.textBox) {
    for (const prev of live.stepsBox.children) {
      prev.classList.remove("active");
      if (prev.firstChild) prev.firstChild.textContent = "✓";
    }
    live.textBox.textContent += chunk;
    if (live.textBox.textContent.trim()) recordButlerReply(live.traceId, live.textBox.textContent, { streaming: true });
  } else {
    if (!live.bubble) live.bubble = append("assistant", "", "streaming");
    live.bubble.textContent += chunk;
    if (live.bubble.textContent.trim()) recordButlerReply(live.traceId, live.bubble.textContent, { streaming: true });
  }
  stickBottom(near);
}

function endBubble(live, text, extra) {
  if (!live.bubble) { append("assistant", text, extra); return; }
  const savedSteps = Array.isArray(live.steps) ? live.steps.slice() : [];
  const savedPlan = live.planBox ? live.planBox.textContent : "";
  live.bubble.className = "msg assistant" + (extra ? " " + extra : "");
  if (!extra) {
    renderAssistantBubble(live.bubble, text);
    // 保留本轮执行过程摘要为可展开折叠条，避免完成后规划与步骤瞬间消失
    if (savedSteps.length || savedPlan) {
      const recap = el("details", null, "live-trace-recap");
      recap.appendChild(el("summary", "执行过程（" + Math.max(1, savedSteps.length) + " 步）"));
      if (savedPlan) recap.appendChild(el("div", savedPlan, "live-plan"));
      for (const st of savedSteps) {
        const row = el("div", null, "live-step");
        row.append(el("span", "✓"), el("span", st));
        recap.appendChild(row);
      }
      live.bubble.insertBefore(recap, live.bubble.firstChild || null);
    }
  } else {
    live.bubble.textContent = text;
  }
}

// 回执到了：回复以回执为准（流里拼的只是过程），再出卡片。
function finishReply(id, reply, live) {
  const traceId = reply.trace_id || live.traceId || null;
  if (reply.state === "superseded") {
    return;
  }
  if (reply.state === "queued") {
    const pos = reply.queue_position ? "（第 " + reply.queue_position + " 位）" : "";
    endBubble(live, "已排入队列" + pos + "，前一条办完后会自动接着处理。", "meta");
    return;
  }
  if (reply.state === "running") {
    const text = "收到，正在处理。好了我会在这里告诉你，你可以先说别的。";
    endBubble(live, text, "meta");
    recordButlerStep(traceId, "正在处理…");
    recordButlerReply(traceId, text, { streaming: true });
    if (traceId && typeof openProgressCard === "function") openProgressCard(traceId, { follow: true, focus: true });
    return;
  }
  entry.shown.add(id + ":assistant");
  const finalText = reply.message || "已收到。";
  endBubble(live, finalText);
  recordButlerStep(traceId, "");
  recordButlerReply(traceId, finalText, { streaming: false });
  renderMessageCards({ route: reply, butler: traceId ? { trace_id: traceId } : null }, id);
  if (typeof syncLiveFocusPill === "function") syncLiveFocusPill(entry.epoch).catch(() => {});
  refreshOverview().catch(report);
}

async function clearConversation() {
  if (!entry.me) return;
  const epoch = entry.epoch;
  const res = await entryApi("POST", "/v1/workspace/messages/clear", {});
  if (isStale(epoch)) return;
  if (typeof res.cleared_after_id === "number") entry.seen = Math.max(entry.seen, res.cleared_after_id);
  entry.shown.clear();
  entry.cards.clear();
  $("transcript").replaceChildren();
  append("assistant", "已清空当前对话上下文，可以开始新话题（已保存的联系人、事项与提醒不受影响）。", "meta");
  announce("已清空当前对话");
}

async function logoutEntry() {
  resetEntry();
  try { await fetch("/v1/auth/logout", { method: "POST", credentials: "same-origin",
    headers: { "X-Requested-By": "xuanpu" } }); }
  finally {
    if ($("entry-auth-gate")) $("entry-auth-gate").classList.remove("hidden");
    $("entry-login").classList.remove("hidden");
    $("entry-password-login").classList.remove("hidden");
    announce("已退出");
  }
}
