// 玄圃小院「看板 · 待办与日程」抽屉：展示并直接操作真实待办、日程、提醒与动态主动跟进；
// 任何勾选或新建都会同步刷新小院看板画面与右侧概览。全部纯 DOM textContent，不拼 HTML。
"use strict";

const boardState = {
  open: false,
  tab: "todos", // todos | events | reminders | proactive
  query: "",
  showDone: false,
  showCompletedReminders: null,
  loading: false,
  error: "",
  notice: "",
  todos: [],
  events: [],
  reminders: [],
  proactive: [],
  proactiveEnabled: false,
  keyBound: false,
};

const BD_REPEAT_LABELS = {
  once: "仅一次",
  daily: "每天",
  weekly: "每周",
  monthly: "每月",
  weekdays: "工作日",
};

function boardOverlay() {
  return $("board-overlay");
}

function boardCloseOnEscape(event) {
  if (event.key === "Escape" && boardState.open) {
    event.preventDefault();
    closeBoardDrawer();
  }
}

function boardFormatTime(raw) {
  if (!raw) return "";
  if (typeof raw === "number") {
    const d = new Date(raw > 1e12 ? raw : raw * 1000);
    return Number.isNaN(d.getTime()) ? String(raw) : d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  const s = String(raw).trim();
  const parsed = Date.parse(s);
  if (!Number.isNaN(parsed) && /^\d{4}-\d{2}-\d{2}/.test(s)) {
    const d = new Date(parsed);
    const hasTime = /T|\d{2}:\d{2}/.test(s);
    return hasTime
      ? d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  }
  return s;
}

function boardIsDone(item) {
  const status = String((item && item.data && item.data.status) || "").toLowerCase();
  return status === "done" || status === "completed" || status === "closed" || status === "cancelled";
}

function boardTodoTitle(item) {
  const d = (item && item.data) || {};
  return d.title || d.text || d.summary || d.name || "未命名待办";
}

function boardTodoDue(item) {
  const d = (item && item.data) || {};
  return d.due || d.due_at || d.deadline || d.date || "";
}

function boardEventTitle(item) {
  const d = (item && item.data) || {};
  return d.title || d.name || d.summary || "未命名日程";
}

function boardEventStart(item) {
  const d = (item && item.data) || {};
  return d.start || d.start_at || d.when || d.date || "";
}

function boardSyncWorld() {
  if (typeof refreshOverview === "function") refreshOverview().catch(() => {});
  if (globalThis.Courtyard && Courtyard.stage && typeof Courtyard.stage.refreshWorld === "function") {
    Courtyard.stage.refreshWorld().catch(() => {});
  }
}

function boardAskButler(promptText) {
  closeBoardDrawer();
  const input = $("message");
  if (!input) return;
  input.value = promptText;
  if (typeof input.focus === "function") input.focus();
}

function closeBoardDrawer(resetData = false) {
  boardState.open = false;
  const overlay = boardOverlay();
  if (overlay) {
    overlay.classList.add("hidden");
    overlay.replaceChildren();
  }
  const btn = $("entry-board");
  if (btn) btn.setAttribute("aria-pressed", "false");
  if (resetData) {
    boardState.tab = "todos";
    boardState.query = "";
    boardState.showDone = false;
    boardState.showCompletedReminders = null;
    boardState.error = "";
    boardState.notice = "";
    boardState.todos = [];
    boardState.events = [];
    boardState.reminders = [];
    boardState.proactive = [];
    boardState.proactiveEnabled = false;
  }
}

async function openBoardDrawer(options = {}) {
  if (!entry.me) return;
  if (typeof closeContactsDrawer === "function") closeContactsDrawer(false);
  const overlay = boardOverlay();
  if (!overlay) return;
  if (options.tab && ["todos", "events", "reminders", "proactive"].includes(options.tab)) {
    boardState.tab = options.tab;
  }
  boardState.open = true;
  boardState.error = "";
  boardState.notice = "";
  overlay.classList.remove("hidden");
  const btn = $("entry-board");
  if (btn) btn.setAttribute("aria-pressed", "true");
  if (!boardState.keyBound && typeof document.addEventListener === "function") {
    document.addEventListener("keydown", boardCloseOnEscape);
    boardState.keyBound = true;
  }
  renderBoardDrawer();
  await refreshBoardData();
}

async function refreshBoardData() {
  const epoch = entry.epoch;
  boardState.loading = true;
  boardState.error = "";
  renderBoardDrawer();
  try {
    const [todosRes, eventsRes, remRes, proRes] = await Promise.all([
      entryApi("GET", "/v1/workspace/items?kind=todo").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=event").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/reminders").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/proactive").then(body => ({ ok: true, items: body.items || [] })).catch(() => ({ ok: false, items: [] })),
    ]);
    if (isStale(epoch) || !boardState.open) return;
    boardState.todos = (todosRes.items || []).filter(i => !i.trashed_at);
    boardState.events = (eventsRes.items || []).filter(i => !i.trashed_at);
    boardState.reminders = (remRes.items || []).filter(r => r && r.status !== "cancelled");
    boardState.proactiveEnabled = Boolean(proRes.ok && proRes.items.length > 0);
    boardState.proactive = proRes.items || [];
    if (boardState.tab === "proactive" && !boardState.proactiveEnabled) boardState.tab = "todos";
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return;
    boardState.error = error.message || "看板数据加载失败";
  } finally {
    if (!isStale(epoch)) {
      boardState.loading = false;
      if (boardState.open) renderBoardDrawer();
    }
  }
}

async function toggleTodoItem(item) {
  if (!item || !item.id) return;
  const epoch = entry.epoch;
  const nextStatus = boardIsDone(item) ? "open" : "done";
  const nextData = { ...(item.data || {}), status: nextStatus };
  try {
    await entryApi("PUT", "/v1/workspace/items/" + encodeURIComponent(item.id), {
      json: { data: nextData, version: item.version },
    });
    if (isStale(epoch)) return;
    boardState.notice = nextStatus === "done" ? "已完成待办「" + boardTodoTitle(item) + "」" : "已重新打开待办「" + boardTodoTitle(item) + "」";
    boardState.error = "";
    await refreshBoardData();
    boardSyncWorld();
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return;
    boardState.error = error.message || "更新待办失败";
    renderBoardDrawer();
  }
}

async function createTodoItem(title, due) {
  const cleanTitle = String(title || "").trim();
  if (!cleanTitle) {
    boardState.error = "请先填写待办内容";
    renderBoardDrawer();
    return false;
  }
  const epoch = entry.epoch;
  const data = { title: cleanTitle, status: "open" };
  if (String(due || "").trim()) data.due = String(due).trim();
  try {
    await entryApi("POST", "/v1/workspace/items", {
      json: { kind: "todo", data, request_id: requestId() },
    });
    if (isStale(epoch)) return false;
    boardState.notice = "已添加待办「" + cleanTitle + "」";
    boardState.error = "";
    await refreshBoardData();
    boardSyncWorld();
    return true;
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return false;
    boardState.error = error.message || "添加待办失败";
    renderBoardDrawer();
    return false;
  }
}

async function createEventItem(title, start, location) {
  const cleanTitle = String(title || "").trim();
  if (!cleanTitle) {
    boardState.error = "请先填写日程标题";
    renderBoardDrawer();
    return false;
  }
  const epoch = entry.epoch;
  const data = { title: cleanTitle };
  if (String(start || "").trim()) data.start = String(start).trim();
  if (String(location || "").trim()) data.location = String(location).trim();
  try {
    await entryApi("POST", "/v1/workspace/items", {
      json: { kind: "event", data, request_id: requestId() },
    });
    if (isStale(epoch)) return false;
    boardState.notice = "已添加日程「" + cleanTitle + "」";
    boardState.error = "";
    await refreshBoardData();
    boardSyncWorld();
    return true;
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return false;
    boardState.error = error.message || "添加日程失败";
    renderBoardDrawer();
    return false;
  }
}

async function createReminderItem(title, dueRaw) {
  const cleanTitle = String(title || "").trim();
  if (!cleanTitle) {
    boardState.error = "请先填写提醒事项";
    renderBoardDrawer();
    return false;
  }
  const epoch = entry.epoch;
  const raw = String(dueRaw || "").trim();
  let dueAt;
  if (!raw) {
    dueAt = Math.floor(Date.now() / 1000) + 3600;
  } else {
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) {
      boardState.error = "提醒时间格式无效，请重新选择";
      renderBoardDrawer();
      return false;
    }
    dueAt = Math.floor(parsed.getTime() / 1000);
  }
  if (dueAt <= Date.now() / 1000) {
    boardState.error = "提醒时间必须晚于当前时间";
    renderBoardDrawer();
    return false;
  }
  if (typeof browserNotificationState === "function" && browserNotificationState() === "default" &&
      typeof requestBrowserNotificationPermission === "function") {
    requestBrowserNotificationPermission().catch(() => {});
  }
  try {
    await entryApi("POST", "/v1/reminders", {
      json: { title: cleanTitle, due_at: dueAt, timezone: timezone(), repeat: "once", channels: ["inbox"], request_id: requestId() },
    });
    if (isStale(epoch)) return false;
    boardState.notice = "已设置提醒「" + cleanTitle + "」";
    boardState.error = "";
    await refreshBoardData();
    boardSyncWorld();
    return true;
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return false;
    boardState.error = error.message || "添加提醒失败";
    renderBoardDrawer();
    return false;
  }
}

async function cancelReminderItem(rem) {
  const id = rem && (rem.reminder_id || rem.id);
  if (!id) return;
  const epoch = entry.epoch;
  try {
    const qs = rem.version !== undefined ? "?version=" + encodeURIComponent(rem.version) : "";
    await entryApi("DELETE", "/v1/reminders/" + encodeURIComponent(id) + qs);
    if (isStale(epoch)) return;
    boardState.notice = "已取消提醒「" + (rem.title || "") + "」";
    boardState.error = "";
    await refreshBoardData();
    boardSyncWorld();
  } catch (error) {
    if (isStale(epoch) || error.name === "AbortError") return;
    boardState.error = error.message || "取消提醒失败";
    renderBoardDrawer();
  }
}

function renderBoardTodos(body) {
  const addBar = el("form", null, "bd-add-bar");
  const titleInput = el("input", null, "ab-search bd-add-main");
  titleInput.placeholder = "添加一条新待办，例如：明天把壁仞方案发给何总";
  titleInput.setAttribute("aria-label", "待办内容");
  const dueInput = el("input", null, "ab-search bd-add-sub");
  dueInput.placeholder = "截止时间（可选）";
  dueInput.setAttribute("aria-label", "截止时间");
  const submitBtn = el("button", "添加待办", "primary ab-new-btn");
  submitBtn.type = "submit";
  addBar.append(titleInput, dueInput, submitBtn);
  addBar.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await createTodoItem(titleInput.value, dueInput.value);
    if (ok) { titleInput.value = ""; dueInput.value = ""; }
  });
  body.append(addBar);

  const q = boardState.query.trim().toLowerCase();
  const match = (item) => !q || (boardTodoTitle(item) + " " + boardTodoDue(item) + " " + ((item.data && item.data.notes) || "")).toLowerCase().includes(q);
  const openItems = boardState.todos.filter(i => !boardIsDone(i) && match(i));
  const doneItems = boardState.todos.filter(i => boardIsDone(i) && match(i));

  const listWrap = el("div", null, "bd-list");
  if (!openItems.length) {
    listWrap.append(el("p", boardState.loading ? "加载中…" : "当前没有未完成的待办事项，清清爽爽！", "ab-empty"));
  } else {
    for (const item of openItems) listWrap.append(buildTodoRow(item, false));
  }
  body.append(listWrap);

  if (doneItems.length) {
    const doneWrap = el("div", null, "bd-done-sec");
    const toggleBtn = button(
      (boardState.showDone ? "▾ 收起已完成（" : "▸ 查看已完成（") + doneItems.length + "）",
      () => { boardState.showDone = !boardState.showDone; renderBoardDrawer(); },
      "bd-done-toggle"
    );
    doneWrap.append(toggleBtn);
    if (boardState.showDone) {
      const doneList = el("div", null, "bd-list");
      for (const item of doneItems) doneList.append(buildTodoRow(item, true));
      doneWrap.append(doneList);
    }
    body.append(doneWrap);
  }
}

function buildTodoRow(item, done) {
  const row = el("div", null, "bd-row" + (done ? " done" : ""));
  const checkBtn = button(done ? "✓" : "", () => toggleTodoItem(item), "bd-check" + (done ? " checked" : ""));
  checkBtn.setAttribute("aria-label", done ? "重新打开待办" : "标记已完成");
  checkBtn.title = done ? "点击重新打开" : "点击标记完成";
  const main = el("div", null, "bd-row-main");
  main.append(el("div", boardTodoTitle(item), "bd-row-title"));
  const metaParts = [];
  const due = boardTodoDue(item);
  if (due) metaParts.push("截止：" + boardFormatTime(due));
  if (item.data && item.data.priority) metaParts.push("优先级：" + item.data.priority);
  if (item.data && item.data.notes) metaParts.push(item.data.notes);
  if (metaParts.length) main.append(el("div", metaParts.join(" · "), "bd-row-meta"));
  const askBtn = button("交给管家", () => boardAskButler("帮我跟进待办「" + boardTodoTitle(item) + "」"), "ab-mini-btn");
  row.append(checkBtn, main, askBtn);
  return row;
}

function renderBoardEvents(body) {
  const addBar = el("form", null, "bd-add-bar");
  const titleInput = el("input", null, "ab-search bd-add-main");
  titleInput.placeholder = "添加日程，例如：与袁新沟通壁仞项目";
  titleInput.setAttribute("aria-label", "日程标题");
  const startInput = el("input", null, "ab-search bd-add-sub");
  startInput.placeholder = "时间（如 2026-11-27 14:00）";
  startInput.setAttribute("aria-label", "日程时间");
  const locInput = el("input", null, "ab-search bd-add-sub");
  locInput.placeholder = "地点（可选）";
  locInput.setAttribute("aria-label", "日程地点");
  const submitBtn = el("button", "添加日程", "primary ab-new-btn");
  submitBtn.type = "submit";
  addBar.append(titleInput, startInput, locInput, submitBtn);
  addBar.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await createEventItem(titleInput.value, startInput.value, locInput.value);
    if (ok) { titleInput.value = ""; startInput.value = ""; locInput.value = ""; }
  });
  body.append(addBar);

  const q = boardState.query.trim().toLowerCase();
  const items = boardState.events.filter(i => !q || (boardEventTitle(i) + " " + boardEventStart(i) + " " + ((i.data && i.data.location) || "")).toLowerCase().includes(q));
  const listWrap = el("div", null, "bd-list");
  if (!items.length) {
    listWrap.append(el("p", boardState.loading ? "加载中…" : "暂无日程安排，可以在上方添加或直接对管家说「明早 10 点开会」。", "ab-empty"));
  } else {
    for (const item of items) {
      const d = item.data || {};
      const row = el("div", null, "bd-row");
      const badge = el("span", "日程", "ab-note-kind");
      const main = el("div", null, "bd-row-main");
      main.append(el("div", boardEventTitle(item), "bd-row-title"));
      const meta = [boardFormatTime(boardEventStart(item)), d.location, d.notes].filter(Boolean).join(" · ");
      if (meta) main.append(el("div", meta, "bd-row-meta"));
      const askBtn = button("问管家", () => boardAskButler("关于日程「" + boardEventTitle(item) + "」，帮我整理一下要点"), "ab-mini-btn");
      row.append(badge, main, askBtn);
      listWrap.append(row);
    }
  }
  body.append(listWrap);
}

function renderBoardReminders(body) {
  if (typeof createBrowserNotificationBanner === "function") {
    const banner = createBrowserNotificationBanner(() => renderBoardDrawer());
    if (banner) body.append(banner);
  }
  const addBar = el("form", null, "bd-add-bar");
  const titleInput = el("input", null, "ab-search bd-add-main");
  titleInput.placeholder = "添加提醒，例如：提醒我回何总电话";
  titleInput.setAttribute("aria-label", "提醒内容");
  const dueInput = el("input", null, "ab-search bd-add-sub");
  dueInput.type = "datetime-local";
  dueInput.setAttribute("aria-label", "提醒时间");
  const submitBtn = el("button", "设提醒", "primary ab-new-btn");
  submitBtn.type = "submit";
  addBar.append(titleInput, dueInput, submitBtn);
  addBar.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await createReminderItem(titleInput.value, dueInput.value);
    if (ok) { titleInput.value = ""; dueInput.value = ""; }
  });
  body.append(addBar);

  const q = boardState.query.trim().toLowerCase();
  const matched = boardState.reminders.filter(r => !q || String(r.title || "").toLowerCase().includes(q));
  const activeItems = matched.filter(r => r.status === "active" || !r.status);
  const completedItems = matched.filter(r => r.status === "completed");

  const listWrap = el("div", null, "bd-list");
  if (!activeItems.length) {
    listWrap.append(el("p", boardState.loading ? "加载中…" : "当前没有待触发的提醒。", "ab-empty"));
  } else {
    for (const rem of activeItems) {
      const row = el("div", null, "bd-row");
      const badge = el("span", BD_REPEAT_LABELS[rem.repeat] || "提醒", "ab-note-kind");
      const main = el("div", null, "bd-row-main");
      main.append(el("div", rem.title || "未命名提醒", "bd-row-title"));
      const meta = [rem.due_at ? "触发时间：" + boardFormatTime(rem.due_at) : "", rem.timezone].filter(Boolean).join(" · ");
      if (meta) main.append(el("div", meta, "bd-row-meta"));
      const cancelBtn = button("取消提醒", () => cancelReminderItem(rem), "ab-mini-btn");
      row.append(badge, main, cancelBtn);
      listWrap.append(row);
    }
  }
  body.append(listWrap);

  if (completedItems.length) {
    const showCompleted = boardState.showCompletedReminders === null
      ? !activeItems.length
      : Boolean(boardState.showCompletedReminders);
    const doneWrap = el("div", null, "bd-done-sec");
    const toggleBtn = button(
      (showCompleted ? "▾ 收起已提醒（" : "▸ 查看已提醒（") + completedItems.length + "）",
      () => { boardState.showCompletedReminders = !showCompleted; renderBoardDrawer(); },
      "bd-done-toggle"
    );
    doneWrap.append(toggleBtn);
    if (showCompleted) {
      const doneList = el("div", null, "bd-list");
      for (const rem of completedItems) {
        const row = el("div", null, "bd-row done");
        const badge = el("span", "已提醒", "ab-note-kind bd-row-tag-done");
        const main = el("div", null, "bd-row-main");
        main.append(el("div", rem.title || "未命名提醒", "bd-row-title"));
        const meta = [rem.due_at ? "原定时间：" + boardFormatTime(rem.due_at) : "", rem.timezone].filter(Boolean).join(" · ");
        if (meta) main.append(el("div", meta, "bd-row-meta"));
        const notifBtn = button("查看通知", () => {
          closeBoardDrawer();
          if (typeof openEntryNotificationView === "function") openEntryNotificationView();
          else if (typeof notificationCard === "function") appendCard(notificationCard());
        }, "ab-mini-btn");
        row.append(badge, main, notifBtn);
        doneList.append(row);
      }
      doneWrap.append(doneList);
    }
    body.append(doneWrap);
  }
}

function renderBoardProactive(body) {
  const listWrap = el("div", null, "bd-list");
  if (!boardState.proactive.length) {
    listWrap.append(el("p", "当前暂无主动跟进项。", "ab-empty"));
  } else {
    for (const item of boardState.proactive) {
      const row = el("div", null, "bd-row");
      const badge = el("span", item.status || "跟进", "ab-note-kind");
      const main = el("div", null, "bd-row-main");
      main.append(el("div", item.title || item.summary || "主动跟进事项", "bd-row-title"));
      const meta = [item.reason || item.detail || "", item.due_at ? boardFormatTime(item.due_at) : ""].filter(Boolean).join(" · ");
      if (meta) main.append(el("div", meta, "bd-row-meta"));
      const askBtn = button("去处理", () => boardAskButler("帮我处理主动跟进事项「" + (item.title || "") + "」"), "ab-mini-btn");
      row.append(badge, main, askBtn);
      listWrap.append(row);
    }
  }
  body.append(listWrap);
}

function renderBoardDrawer() {
  const overlay = boardOverlay();
  if (!overlay || !boardState.open) return;

  const sheet = el("div", null, "ab-sheet bd-sheet");
  sheet.addEventListener("click", (e) => e.stopPropagation());

  const openTodoCount = boardState.todos.filter(i => !boardIsDone(i)).length;
  const activeReminderCount = boardState.reminders.filter(r => r.status === "active" || !r.status).length;
  const header = el("header", null, "ab-header");
  const titleWrap = el("div", null, "ab-title-wrap");
  titleWrap.append(
    el("h2", "看板 · 待办与日程", "ab-title"),
    el("span", openTodoCount + " 项待办 · " + boardState.events.length + " 条日程 · " + activeReminderCount + " 条提醒", "ab-count")
  );

  const tabs = el("div", null, "ab-tabs");
  tabs.setAttribute("role", "tablist");
  const tabDefs = [
    ["todos", "待办" + (openTodoCount ? " (" + openTodoCount + ")" : "")],
    ["events", "日程"],
    ["reminders", "提醒" + (activeReminderCount ? " (" + activeReminderCount + ")" : "")],
  ];
  if (boardState.proactiveEnabled) tabDefs.push(["proactive", "主动跟进"]);
  for (const [key, label] of tabDefs) {
    const t = button(label, () => {
      boardState.tab = key;
      boardState.error = "";
      renderBoardDrawer();
    }, "ab-tab" + (boardState.tab === key ? " active" : ""));
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", String(boardState.tab === key));
    tabs.append(t);
  }

  const headActions = el("div", null, "ab-head-actions");
  headActions.append(
    button("刷新", () => refreshBoardData(), "ab-close-btn"),
    button("关闭", () => closeBoardDrawer(), "ab-close-btn")
  );
  header.append(titleWrap, tabs, headActions);
  sheet.append(header);

  if (boardState.error) sheet.append(el("div", boardState.error, "ab-banner error"));
  else if (boardState.notice) sheet.append(el("div", boardState.notice, "ab-banner ok"));

  const body = el("div", null, "ab-body single bd-body");
  const filterWrap = el("div", null, "bd-filter-row");
  const search = el("input", null, "ab-search");
  search.placeholder = "筛选当前列表…";
  search.value = boardState.query;
  search.addEventListener("input", () => {
    boardState.query = search.value;
    renderBoardDrawer();
  });
  filterWrap.append(search);
  body.append(filterWrap);

  if (boardState.tab === "todos") renderBoardTodos(body);
  else if (boardState.tab === "events") renderBoardEvents(body);
  else if (boardState.tab === "reminders") renderBoardReminders(body);
  else if (boardState.tab === "proactive") renderBoardProactive(body);

  sheet.append(body);
  overlay.replaceChildren(sheet);
  overlay.onclick = () => closeBoardDrawer();
}
