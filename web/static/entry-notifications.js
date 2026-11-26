// 玄圃小院实时通知：订阅 /v1/notifications/events SSE、网页内悬浮提醒卡片、
// 顶部未读角标与 Chrome / 浏览器原生桌面弹窗。全部纯 DOM textContent，不拼 HTML。
"use strict";

const entryNotifications = {
  cursor: 0,
  watermark: 0,
  snapshotReady: false,
  unreadCount: 0,
  controller: null,
  retryTimer: null,
  cards: new Map(),
  delayed: new Map(),
  notifiedBrowser: new Set(),
};

const ENTRY_NOTIF_RETRY_MS = 3000;
const ENTRY_NOTIF_DELAY_RETRY_MS = 15000;
const ENTRY_NOTIF_MAX_CARDS = 3;

function browserNotificationState() {
  if (globalThis.isSecureContext === false) return "insecure";
  const Ctor = globalThis.Notification;
  if (typeof Ctor !== "function") return "unsupported";
  return Ctor.permission || "default";
}

function entryNotificationHeading(item) {
  if (!item) return "你有未读通知";
  if (item.source === "work_task") return "交办工作有新进展";
  if (item.source === "proactive_check" || item.source === "proactive_followup") {
    return "有一项业务需要跟进";
  }
  return "提醒时间到了";
}

function updateEntryNotificationBadge(count) {
  entryNotifications.unreadCount = Math.max(0, Number(count) || 0);
  const btn = $("entry-notifications");
  if (!btn) return;
  const n = entryNotifications.unreadCount;
  btn.textContent = n > 0 ? "通知 (" + (n > 99 ? "99+" : n) + ")" : "通知";
  btn.classList.toggle("has-unread", n > 0);
  btn.title = n > 0 ? "有 " + n + " 条未读通知，点击查看" : "查看最近通知";
}

function clearEntryNotificationAlerts() {
  entryNotifications.cards.clear();
  const list = $("entry-notification-alert-list");
  if (list) list.replaceChildren();
  const more = $("entry-notification-alert-more");
  if (more) more.classList.add("hidden");
  const wrap = $("entry-notification-alerts");
  if (wrap) wrap.classList.add("hidden");
}

function resetEntryNotifications() {
  if (entryNotifications.controller) {
    entryNotifications.controller.abort();
    entryNotifications.controller = null;
  }
  if (entryNotifications.retryTimer !== null) {
    clearTimeout(entryNotifications.retryTimer);
    entryNotifications.retryTimer = null;
  }
  for (const pending of entryNotifications.delayed.values()) {
    if (pending.timer !== null) clearTimeout(pending.timer);
  }
  entryNotifications.delayed.clear();
  entryNotifications.cursor = 0;
  entryNotifications.watermark = 0;
  entryNotifications.snapshotReady = false;
  entryNotifications.notifiedBrowser.clear();
  clearEntryNotificationAlerts();
  updateEntryNotificationBadge(0);
}

function dismissEntryNotificationAlert(id) {
  const existing = entryNotifications.cards.get(id);
  if (existing && existing.node) existing.node.remove();
  entryNotifications.cards.delete(id);
  if (!entryNotifications.cards.size) {
    const wrap = $("entry-notification-alerts");
    if (wrap) wrap.classList.add("hidden");
    const more = $("entry-notification-alert-more");
    if (more) more.classList.add("hidden");
  }
}

function openEntryNotificationView() {
  if (!entry.me) return;
  clearEntryNotificationAlerts();
  if (typeof notificationCard === "function") {
    appendCard(notificationCard());
  }
}

async function requestBrowserNotificationPermission({ sendTest = false } = {}) {
  const state = browserNotificationState();
  if (state === "insecure" || state === "unsupported") return state;
  const Ctor = globalThis.Notification;
  if (Ctor.permission === "granted") {
    if (sendTest) sendTestBrowserNotification();
    return "granted";
  }
  if (Ctor.permission === "denied") {
    announce("浏览器已禁止桌面通知，可在地址栏左侧网站设置中重新开启。");
    return "denied";
  }
  try {
    const result = await Ctor.requestPermission();
    if (result === "granted") {
      announce("已开启浏览器桌面弹窗提醒，切换标签页也能准时收到通知。");
      if (sendTest) sendTestBrowserNotification();
    } else if (result === "denied") {
      announce("浏览器已禁止桌面通知，可在地址栏左侧网站设置中重新开启。");
    }
    if (typeof boardState !== "undefined" && boardState.open && typeof renderBoardDrawer === "function") {
      renderBoardDrawer();
    }
    return result;
  } catch {
    return browserNotificationState();
  }
}

function sendTestBrowserNotification() {
  const Ctor = globalThis.Notification;
  if (typeof Ctor !== "function" || Ctor.permission !== "granted") return false;
  try {
    const popup = new Ctor("玄圃桌面通知已开启", {
      body: "如果你看到了这条系统通知，说明 Chrome 系统级提醒弹窗已正常工作。",
    });
    popup.onclick = () => {
      if (typeof globalThis.focus === "function") globalThis.focus();
      if (typeof popup.close === "function") popup.close();
    };
    return true;
  } catch {
    return false;
  }
}

// 浏览器原生桌面弹窗权限状态与测试条。
function createBrowserNotificationBanner(onChanged) {
  const state = browserNotificationState();
  if (state === "unsupported") return null;
  const bar = el("div", null, "notif-perm-bar" + (state === "denied" || state === "insecure" ? " warn" : ""));
  const messages = {
    insecure: "当前不是 HTTPS 或 localhost，Chrome 会禁用系统级桌面通知；请改用 localhost 或 HTTPS 访问。",
    denied: "Chrome 已拦截本站点通知，请点击地址栏左侧图标 → 将「通知」设为允许后刷新。",
    default: "尚未开启 Chrome 系统级弹窗，点击开启后即使切到其他标签页也能收到系统通知。",
    granted: "Chrome 系统级弹窗已授权（若未弹出请检查系统「通知 → Google Chrome」与勿扰模式）。",
  };
  const text = el("span", messages[state] || messages.default, "notif-perm-text");
  bar.append(text);
  if (state === "default") {
    const btn = button("开启桌面弹窗", async () => {
      btn.disabled = true;
      const next = await requestBrowserNotificationPermission({ sendTest: true });
      if (typeof onChanged === "function") onChanged(next);
      else if (bar.parentNode) {
        const replacement = createBrowserNotificationBanner(onChanged);
        if (replacement && typeof bar.parentNode.replaceChild === "function") {
          bar.parentNode.replaceChild(replacement, bar);
        } else if (replacement) {
          bar.className = replacement.className;
          bar.replaceChildren(...replacement.children);
        }
      }
    }, "ab-mini-btn primary-soft");
    bar.append(btn);
  } else if (state === "granted") {
    const testBtn = button("发送测试弹窗", () => {
      const ok = sendTestBrowserNotification();
      announce(ok
        ? "已发送 Chrome 系统测试通知；若屏幕右上角未显示，请检查 macOS/系统通知设置中是否允许 Google Chrome 通知。"
        : "浏览器拒绝创建桌面通知，请检查浏览器权限。");
    }, "ab-mini-btn");
    bar.append(testBtn);
  }
  return bar;
}

function showBrowserDesktopNotification(item) {
  if (browserNotificationState() !== "granted") return false;
  const Ctor = globalThis.Notification;
  if (!item || typeof Ctor !== "function") return false;
  const id = item.notification_id;
  if (id != null && entryNotifications.notifiedBrowser.has(id)) return false;
  const lines = [item.title || "你有新的提醒"];
  if (item.due_at != null) lines.push("原定 " + when(item.due_at));
  if (item.body) lines.push(item.body);
  const title = entryNotificationHeading(item);
  const body = lines.join("\n");
  const opts = { body };
  if (id != null) opts.tag = "xuanpu-notification-" + id;
  let popup = null;
  try {
    popup = new Ctor(title, opts);
  } catch {
    try { popup = new Ctor(title, { body }); } catch { return false; }
  }
  popup.onclick = () => {
    if (typeof globalThis.focus === "function") globalThis.focus();
    openEntryNotificationView();
    if (typeof popup.close === "function") popup.close();
  };
  if (id != null) entryNotifications.notifiedBrowser.add(id);
  return true;
}

function showEntryNotificationAlert(item) {
  if (!item || item.read_at || item.attention_active === false) return;
  const id = item.notification_id;
  if (entryNotifications.cards.has(id)) return;
  const wrap = $("entry-notification-alerts");
  const list = $("entry-notification-alert-list");
  if (!wrap || !list) {
    showBrowserDesktopNotification(item);
    return;
  }
  const epoch = entry.epoch;
  const cardNode = el("article", null, "entry-notif-card");
  cardNode.dataset.notificationId = String(id);

  const head = el("div", null, "entry-notif-head");
  head.append(el("strong", entryNotificationHeading(item), "entry-notif-title"));
  const closeBtn = button("×", () => {
    if (!isStale(epoch)) dismissEntryNotificationAlert(id);
  }, "entry-notif-close");
  closeBtn.setAttribute("aria-label", "关闭提醒提示");
  head.append(closeBtn);
  cardNode.append(head);

  cardNode.append(el("p", item.title || "你有新的提醒", "entry-notif-body"));
  if (item.due_at != null) cardNode.append(el("p", "原定 " + when(item.due_at), "entry-notif-meta"));
  if (item.body) cardNode.append(el("p", item.body, "entry-notif-reason"));

  const actions = el("div", null, "entry-notif-actions");
  actions.append(button("查看通知", () => {
    if (!isStale(epoch)) openEntryNotificationView();
  }, "primary"));
  const readBtn = button("标为已读", async () => {
    if (isStale(epoch) || readBtn.disabled) return;
    readBtn.disabled = true;
    try {
      await entryApi("POST", "/v1/notifications/" + encodeURIComponent(id) + "/read", {});
      if (isStale(epoch)) return;
      dismissEntryNotificationAlert(id);
      await refreshEntryNotifications(epoch, { alertNew: false });
      if (typeof refreshOverview === "function") refreshOverview().catch(() => {});
    } catch (error) {
      if (!isStale(epoch)) {
        readBtn.disabled = false;
        report(error);
      }
    }
  });
  actions.append(readBtn);
  if (browserNotificationState() === "default") {
    const permBtn = button("开启桌面弹窗", async () => {
      permBtn.disabled = true;
      const result = await requestBrowserNotificationPermission({ sendTest: true });
      if (result === "granted" || result === "denied") permBtn.remove();
      else permBtn.disabled = false;
    });
    actions.append(permBtn);
  }
  cardNode.append(actions);

  if (entryNotifications.cards.size >= ENTRY_NOTIF_MAX_CARDS) {
    const oldestKey = entryNotifications.cards.keys().next().value;
    dismissEntryNotificationAlert(oldestKey);
    const more = $("entry-notification-alert-more");
    if (more) more.classList.remove("hidden");
  }
  entryNotifications.cards.set(id, { node: cardNode });
  list.appendChild(cardNode);
  wrap.classList.remove("hidden");
  announce(entryNotificationHeading(item) + "：" + (item.title || ""));
  showBrowserDesktopNotification(item);
}

function scheduleDelayedEntryNotification(item, epoch) {
  if (typeof setTimeout !== "function") return;
  const id = item.notification_id;
  const prev = entryNotifications.delayed.get(id);
  if (prev && prev.timer !== null) clearTimeout(prev.timer);
  const pending = { item, epoch, timer: null };
  entryNotifications.delayed.set(id, pending);
  const delay = Math.min(2147483647, Math.max(0, Math.ceil((item.alert_after - Date.now() / 1000) * 1000)));
  pending.timer = setTimeout(() => verifyDelayedEntryNotification(pending), delay);
}

async function verifyDelayedEntryNotification(pending) {
  const id = pending.item.notification_id;
  const current = () => !isStale(pending.epoch) && entryNotifications.delayed.get(id) === pending;
  if (!current()) return;
  pending.timer = null;
  try {
    const body = await entryApi("GET", "/v1/notifications?after_id=" + Math.max(0, id - 1) + "&limit=1");
    if (!current()) return;
    const fresh = (body.items || []).find(item => item.notification_id === id);
    if (!fresh || fresh.read_at || fresh.attention_active === false) {
      entryNotifications.delayed.delete(id);
      dismissEntryNotificationAlert(id);
      return;
    }
    if (fresh.alert_after > Date.now() / 1000) {
      scheduleDelayedEntryNotification(fresh, pending.epoch);
      return;
    }
    entryNotifications.delayed.delete(id);
    showEntryNotificationAlert(fresh);
  } catch {
    if (!current()) return;
    pending.timer = setTimeout(() => verifyDelayedEntryNotification(pending), ENTRY_NOTIF_DELAY_RETRY_MS);
  }
}

function observeEntryNotifications(items, epoch, { snapshot = false, alertNew = true } = {}) {
  if (isStale(epoch) || !entry.me) return [];
  const now = Date.now() / 1000;
  const initial = !entryNotifications.snapshotReady;
  const previousWatermark = entryNotifications.watermark;
  const list = Array.isArray(items) ? items : [];

  for (const item of list) {
    const id = Number(item.notification_id || 0);
    if (id > entryNotifications.cursor) entryNotifications.cursor = id;
    if (id > entryNotifications.watermark) entryNotifications.watermark = id;

    const existing = entryNotifications.delayed.get(id);
    if (item.read_at || item.attention_active === false) {
      if (existing && existing.timer !== null) clearTimeout(existing.timer);
      entryNotifications.delayed.delete(id);
      dismissEntryNotificationAlert(id);
    } else if (item.alert_after > now && (!existing || existing.item.alert_after !== item.alert_after)) {
      scheduleDelayedEntryNotification(item, epoch);
    }
  }

  if (snapshot) {
    const unread = list.filter(item => !item.read_at && item.attention_active !== false);
    updateEntryNotificationBadge(unread.length);
    if (initial) {
      entryNotifications.snapshotReady = true;
      return [];
    }
  }

  if (!alertNew || initial) return [];
  const freshReady = list
    .filter(item => !item.read_at && item.attention_active !== false &&
      (!item.alert_after || item.alert_after <= now) &&
      Number(item.notification_id || 0) > previousWatermark)
    .sort((a, b) => Number(a.notification_id || 0) - Number(b.notification_id || 0));
  for (const item of freshReady) showEntryNotificationAlert(item);
  return freshReady;
}

function syncEntryViewsAfterNotification() {
  if (typeof refreshOverview === "function") refreshOverview().catch(() => {});
  if (globalThis.Courtyard && Courtyard.stage && typeof Courtyard.stage.refreshWorld === "function") {
    Courtyard.stage.refreshWorld().catch(() => {});
  }
  if (typeof boardState !== "undefined" && boardState.open && typeof refreshBoardData === "function") {
    refreshBoardData().catch(() => {});
  }
}

async function refreshEntryNotifications(epoch = entry.epoch, options = {}) {
  if (isStale(epoch) || !entry.me) return [];
  const body = await entryApi("GET", "/v1/notifications?latest=true&limit=100");
  if (isStale(epoch)) return [];
  const items = body.items || [];
  const alerted = observeEntryNotifications(items, epoch, {
    snapshot: true,
    alertNew: options.alertNew !== false,
  });
  if (alerted.length) syncEntryViewsAfterNotification();
  return items;
}

async function readNotificationStreamFrames(response, onFrame) {
  if (typeof readFrames === "function") {
    return readFrames(response, onFrame);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = null;
      const data = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith(":")) continue;
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data.push(line.slice(6));
      }
      if (name) onFrame(name, data.length ? JSON.parse(data.join("\n")) : {});
    }
  }
}

async function followEntryNotifications(epoch) {
  if (isStale(epoch) || !entry.me) return;
  const controller = new AbortController();
  entryNotifications.controller = controller;
  const stop = () => controller.abort();
  entry.cleanups.add(stop);
  try {
    const response = await fetch("/v1/notifications/events?after_id=" + entryNotifications.cursor, {
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "X-Requested-By": "xuanpu", "Accept-Language": preferredLanguages() },
    });
    if (isStale(epoch)) return;
    if (response.status === 401) { resetEntry(); return; }
    if (!response.ok || !response.body) return;
    await readNotificationStreamFrames(response, (name, data) => {
      if (isStale(epoch)) return;
      if (name === "notification") {
        const prevUnread = entryNotifications.unreadCount;
        const alerted = observeEntryNotifications([data], epoch, { snapshot: false, alertNew: true });
        if (!data.read_at && data.attention_active !== false) {
          updateEntryNotificationBadge(prevUnread + (alerted.length ? 1 : 0));
        }
        syncEntryViewsAfterNotification();
        refreshEntryNotifications(epoch, { alertNew: false }).catch(() => {});
      } else if (name === "shared_notification" || name === "collaboration_notification") {
        syncEntryViewsAfterNotification();
      }
    });
  } catch (error) {
    if (error && error.name === "AbortError") return;
  } finally {
    entry.cleanups.delete(stop);
    if (entryNotifications.controller === controller) entryNotifications.controller = null;
    if (!isStale(epoch) && entry.me && typeof setTimeout === "function") {
      entryNotifications.retryTimer = setTimeout(async () => {
        entryNotifications.retryTimer = null;
        if (isStale(epoch) || !entry.me) return;
        await refreshEntryNotifications(epoch, { alertNew: true }).catch(() => {});
        if (!isStale(epoch) && entry.me) followEntryNotifications(epoch);
      }, ENTRY_NOTIF_RETRY_MS);
    }
  }
}

async function startEntryNotifications(epoch = entry.epoch) {
  if (isStale(epoch) || !entry.me) return;
  entry.cleanups.add(resetEntryNotifications);
  try {
    await refreshEntryNotifications(epoch, { alertNew: false });
  } catch {
    // 首次读取失败不阻断 SSE 订阅，重连时会自动补读
    entryNotifications.snapshotReady = true;
  }
  if (!isStale(epoch) && entry.me) {
    followEntryNotifications(epoch);
  }
}
