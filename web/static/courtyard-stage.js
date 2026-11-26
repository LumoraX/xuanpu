// 常驻小院（R7）：打开入口就在页面上，管家在管家台、本人可用的匠人在垫子上待命。
// 员工说一句话后，小院立刻跟上这次请求（焦点），按执行事实演出；进展卡只讲「经过」。
// 一次只演一件事：最新发出的请求，或员工在某张进展卡上点「在小院里看」的那件。
"use strict";

(function () {
  const C = () => globalThis.Courtyard;
  const stage = { scene: null, focus: null, played: new WeakMap(), roster: [], rosterLoaded: false, caption: null,
    drawer: null, toggle: null, action: null, themeButtons: [], openPendingId: null, expanded: false, looks: null, log: [],
    replyBox: null, replyTitle: null, replyBody: null, replyExpand: null, replyExpanded: false, replies: new Map(), steps: new Map(), latestReply: "",
    worldCounts: { openTodos: 0, activeReminders: 0 } };
  // 最近演了什么（排查用）：每帧一条，记焦点、帧类型、是对齐终态还是出节拍
  function note(entry) { stage.log.push({ at: Date.now(), ...entry }); if (stage.log.length > 80) stage.log.shift(); }
  const IDLE = "管家在管家台待命。点击书架看资料、点击看板管待办，或直接说一句话。";

  function renderIdleCaption() {
    if (!stage.caption || stage.focus) return;
    if (stage.roster.length > 0) {
      stage.caption.textContent = "管家在管家台待命，" + stage.roster.length + " 位匠人在小院值守。点击书架看资料、点击看板管待办，或直接说一句话。";
      return;
    }
    if (stage.rosterLoaded) {
      stage.caption.replaceChildren(
        el("span", "管家在管家台待命；当前暂未启用企业匠人，私人事务与资料整理可直接交代给管家。")
      );
      if (globalThis.entry && entry.me && entry.me.role === "tenant_admin") {
        const link = el("a", "去能力市场添匠人", "cy-caption-link");
        link.href = "index.html?classic=1#marketplace";
        stage.caption.append(link);
      }
      return;
    }
    stage.caption.textContent = IDLE;
  }

  function mounted() { return stage.scene !== null; }

  function reducedMotion() {
    try { return Boolean(globalThis.matchMedia && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch { return false; }
  }

  function setExpanded(on) {
    stage.expanded = Boolean(on && stage.focus);
    if (stage.drawer) stage.drawer.classList.toggle("hidden", !stage.expanded);
    if (stage.toggle) {
      stage.toggle.textContent = stage.expanded ? "收起进展" : "展开进展";
      stage.toggle.setAttribute("aria-expanded", String(stage.expanded));
    }
  }

  function attachPanel(state) {
    if (!stage.drawer || !state || !state.panel) return;
    if (stage.drawer.children[0] !== state.panel) stage.drawer.replaceChildren(state.panel);
    if (stage.toggle) stage.toggle.classList.remove("hidden");
  }

  function syncThemeButtons(activeId) {
    for (const item of stage.themeButtons) item.btn.setAttribute("aria-pressed", String(item.id === activeId));
  }

  function setTheme(id) {
    if (!stage.scene || typeof stage.scene.setTheme !== "function") return null;
    const active = stage.scene.setTheme(id);
    syncThemeButtons(active);
    return active;
  }

  function buildThemeSwitcher(actions) {
    stage.themeButtons = [];
    const sceneMod = C().scene;
    if (!sceneMod || !Array.isArray(sceneMod.THEME_ORDER) || !sceneMod.THEMES) return;
    const group = el("div", null, "cy-themes");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "场景风格");
    for (const id of sceneMod.THEME_ORDER) {
      const theme = sceneMod.THEMES[id];
      if (!theme) continue;
      const btn = button(theme.label, () => setTheme(id), "cy-theme-btn");
      btn.setAttribute("aria-pressed", "false");
      btn.dataset.theme = id;
      group.append(btn);
      stage.themeButtons.push({ id, btn });
    }
    if (stage.themeButtons.length) actions.append(group);
  }

  function renderReplyCard(text, { streaming = false } = {}) {
    if (!stage.replyBox || !stage.replyBody) return;
    const cleaned = typeof globalThis.cleanUserText === "function" ? globalThis.cleanUserText(text) : String(text || "").trim();
    if (!cleaned) {
      stage.replyBox.classList.add("hidden");
      return;
    }
    stage.replyBox.classList.remove("hidden");
    if (stage.replyTitle) stage.replyTitle.textContent = streaming ? "管家正在回话…" : "管家回话";
    if (typeof globalThis.renderAssistantBubble === "function") {
      globalThis.renderAssistantBubble(stage.replyBody, cleaned);
    } else {
      stage.replyBody.textContent = cleaned;
    }
    if (stage.replyExpand) {
      const longText = cleaned.length > 140 || cleaned.split("\n").length > 3;
      stage.replyExpand.classList.toggle("hidden", !longText);
      stage.replyBox.classList.toggle("expanded", stage.replyExpanded);
      stage.replyExpand.textContent = stage.replyExpanded ? "收起" : "展开全文";
    }
  }

  function syncFocusedReply() {
    const traceId = stage.focus && stage.focus.progress ? stage.focus.progress.traceId : null;
    const text = (traceId && stage.replies.get(traceId)) || stage.latestReply || "";
    renderReplyCard(text, { streaming: false });
  }

  function stepOf(traceId) {
    return (traceId && stage.steps.get(traceId)) || "";
  }

  function setStep(traceId, text) {
    const label = String(text || "").trim();
    if (traceId) {
      const prev = stage.steps.get(traceId) || "";
      if (prev === label) return;
      if (label) stage.steps.set(traceId, label);
      else stage.steps.delete(traceId);
    }
    if (!mounted() || !stage.scene) return;
    const focus = stage.focus;
    const focusedTrace = focus && focus.progress ? focus.progress.traceId : null;
    if (!focus || (focusedTrace && traceId && focusedTrace !== traceId)) return;
    const model = C().model;
    const spans = focus.prev || [];
    if (!model || !spans.length || model.finished(spans) || spans.some(s => s.kind === "dispatch" && !model.TERMINAL.has(s.state))) return;
    const token = stage.played.get(focus);
    const apply = () => {
      if (!mounted() || stage.focus !== focus || stage.played.get(focus) !== token) return;
      const curSpans = focus.prev || [];
      if (!curSpans.length || model.finished(curSpans) || curSpans.some(s => s.kind === "dispatch" && !model.TERMINAL.has(s.state))) return;
      stage.scene.settle(model.poses(curSpans, focus.details()));
    };
    if (typeof stage.scene.idle === "function") stage.scene.idle().then(apply);
    else apply();
  }

  function setReply(traceId, text, options = {}) {
    const cleaned = typeof globalThis.cleanUserText === "function" ? globalThis.cleanUserText(text) : String(text || "").trim();
    if (!cleaned) return;
    if (traceId) stage.replies.set(traceId, cleaned);
    stage.latestReply = cleaned;
    if (!mounted()) return;
    const focusedTrace = stage.focus && stage.focus.progress ? stage.focus.progress.traceId : null;
    if (!focusedTrace || !traceId || focusedTrace === traceId) {
      renderReplyCard(cleaned, options);
      if (stage.focus && stage.scene && !options.streaming) {
        const spans = stage.focus.prev || [];
        if (C().model && C().model.finished(spans)) {
          stage.scene.settle(C().model.poses(spans, stage.focus.details()));
        }
      }
    }
  }

  // 挂到页面上的 #stage；读本人可用的匠人让他们上垫子。进展面板默认折叠在小院下方，交互留在小院里。
  function mount(container, options = {}) {
    if (mounted() || !container || typeof document.createElementNS !== "function") return false;
    stage.looks = options.looks || (() => C().art.loadLooks());
    const box = el("div", null, "courtyard cy-stage-wrap"), host = el("div", null, "cy-stage");
    const bar = el("div", null, "cy-stage-bar"), actions = el("div", null, "cy-stage-actions");
    stage.caption = el("p", IDLE, "cy-caption");
    buildThemeSwitcher(actions);
    const openShelf = typeof openBookshelfDrawer === "function" ? openBookshelfDrawer : (typeof openContactsDrawer === "function" ? openContactsDrawer : null);
    if (openShelf) {
      const abBtn = button("书架", () => openShelf({ tab: "people" }).catch(report), "cy-stage-shortcut");
      abBtn.title = "打开书架资料室（通讯录、公司、项目、记录与文件）";
      actions.append(abBtn);
    }
    if (typeof openBoardDrawer === "function") {
      const bdBtn = button("看板", () => openBoardDrawer({ tab: "todos" }).catch(report), "cy-stage-shortcut");
      bdBtn.title = "打开看板（待办、日程与提醒）";
      actions.append(bdBtn);
    }
    stage.action = button("去处理", () => { if (stage.openPendingId && typeof openPending === "function") openPending(stage.openPendingId); }, "primary cy-stage-action hidden");
    stage.toggle = button("展开进展", () => setExpanded(!stage.expanded), "cy-stage-toggle hidden");
    stage.toggle.setAttribute("aria-expanded", "false");
    actions.append(stage.action, stage.toggle);
    bar.append(stage.caption, actions);
    // 持久展示管家最后说出的对话内容，不随动画结束而消失
    stage.replyBox = el("section", null, "cy-reply-card hidden");
    stage.replyBox.setAttribute("aria-label", "管家最新回话");
    const replyHead = el("div", null, "cy-reply-head");
    stage.replyTitle = el("strong", "管家回话", "cy-reply-title");
    stage.replyExpand = button("展开全文", () => {
      stage.replyExpanded = !stage.replyExpanded;
      stage.replyBox.classList.toggle("expanded", stage.replyExpanded);
      stage.replyExpand.textContent = stage.replyExpanded ? "收起" : "展开全文";
    }, "cy-reply-toggle hidden");
    replyHead.append(stage.replyTitle, stage.replyExpand);
    stage.replyBody = el("div", null, "cy-reply-body");
    stage.replyBox.append(replyHead, stage.replyBody);
    stage.drawer = el("div", null, "cy-stage-drawer hidden");
    box.append(host, bar, stage.replyBox, stage.drawer);
    container.replaceChildren(box);
    stage.scene = C().scene.create(host, {
      look: key => C().art.resolveLook(stage.looks(), key),
      onSelect: key => {
        if (!stage.focus) return;
        if (options.onSelect) options.onSelect(stage.focus, key);
        setExpanded(true);
      },
      onStationClick: key => {
        if (key === "books" && openShelf) {
          openShelf({ tab: "people" }).catch(report);
        } else if (key === "tv" && typeof openBoardDrawer === "function") {
          const defaultTab = stage.worldCounts.openTodos === 0 && stage.worldCounts.activeReminders > 0 ? "reminders" : "todos";
          openBoardDrawer({ tab: defaultTab }).catch(report);
        } else if (key === "cabinet" && openShelf) {
          openShelf({ tab: "matters" }).catch(report);
        }
      },
    });
    if (typeof stage.scene.theme === "function") syncThemeButtons(stage.scene.theme());
    stage.scene.cast([]);
    stage.scene.settle({});
    entry.cleanups.add(unmount);
    loadRoster();
    refreshWorld().catch(() => {});
    return true;
  }

  function unmount() {
    if (stage.scene) stage.scene.destroy();
    Object.assign(stage, { scene: null, focus: null, played: new WeakMap(), roster: [], rosterLoaded: false, caption: null,
      drawer: null, toggle: null, action: null, themeButtons: [], openPendingId: null, expanded: false,
      replyBox: null, replyTitle: null, replyBody: null, replyExpand: null, replyExpanded: false,
      replies: new Map(), steps: new Map(), latestReply: "",
      worldCounts: { openTodos: 0, activeReminders: 0 } });
  }

  async function refreshWorld() {
    if (!stage.scene || typeof stage.scene.setWorld !== "function") return;
    const epoch = entry.epoch;
    const [todosRes, eventsRes, remRes, peopleRes, mattersRes] = await Promise.all([
      entryApi("GET", "/v1/workspace/items?kind=todo").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=event").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/reminders?status=active").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/people").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=matter").catch(() => ({ items: [] })),
    ]);
    if (isStale(epoch) || !stage.scene || typeof stage.scene.setWorld !== "function") return;
    const todos = (todosRes.items || []).filter(i => i && !i.trashed_at).map(i => {
      const d = i.data || {};
      const st = String(d.status || "").toLowerCase();
      return { id: i.id, title: d.title || d.text || d.summary || "待办", done: st === "done" || st === "completed" || st === "closed" };
    });
    const events = (eventsRes.items || []).filter(i => i && !i.trashed_at).map(i => {
      const d = i.data || {};
      return { id: i.id, title: d.title || d.name || "日程" };
    });
    const reminders = (remRes.items || []).filter(r => r && r.status !== "cancelled").map(r => ({ id: r.reminder_id || r.id, title: r.title || "提醒" }));
    const booksCount = (peopleRes.items || []).length + (mattersRes.items || []).filter(i => i && !i.trashed_at).length;
    stage.worldCounts = { openTodos: todos.filter(t => !t.done).length, activeReminders: reminders.length };
    stage.scene.setWorld({ todos, events, reminders, booksCount });
  }

  async function loadRoster() {
    const epoch = entry.epoch;
    try {
      const body = await entryApi("GET", "/v1/assistant/artisans");
      if (isStale(epoch) || !stage.scene) return;
      const items = body.items || [];
      C().art.setNames(items);
      stage.roster = [...new Set(items.map(i => i.package))];
      stage.rosterLoaded = true;
      stage.scene.cast(stage.roster);
      stage.scene.restyle();
      if (typeof stage.scene.setWorld === "function") stage.scene.setWorld({ artisans: items });
      if (!stage.focus) {
        stage.scene.settle({});
        renderIdleCaption();
      }
    } catch (error) {
      stage.rosterLoaded = true;
      if (!stage.focus) renderIdleCaption();
      if (error && error.name !== "AbortError" && error.status !== 403) console.error("小院读不到可用匠人", error);
    }
  }

  // 让小院演这件事：先把画面对齐到它此刻的事实，之后的帧再出动画。已在焦点时不重复 settle，避免打断正在走的动画。
  function focus(state, options = {}) {
    if (!mounted() || !state) return;
    if (stage.focus === state) {
      attachPanel(state);
      if (options.expand) setExpanded(true);
      describe(state);
      syncFocusedReply();
      return;
    }
    note({ focus: true, spans: (state.prev || []).length });
    stage.focus = state;
    const spans = state.prev || [];
    stage.scene.cast(C().model.artisans(spans));
    stage.scene.settle(C().model.poses(spans, state.details()));
    stage.played.set(state, spans);
    stage.scene.select(state.sel);
    attachPanel(state);
    if (options.expand) setExpanded(true);
    describe(state);
    syncFocusedReply();
  }

  // 卡片每处理完一帧交过来；不是焦点的卡片不演（员工点「在小院里看」才切过来）。
  function update(state, frame, next, details) {
    if (!mounted()) return;
    if (state.wantsFocus) { state.wantsFocus = false; stage.focus = state; stage.played.delete(state); }
    if (stage.focus !== state) return;
    attachPanel(state);
    const model = C().model, before = stage.played.get(state);
    stage.scene.cast(model.artisans(next));
    if (!before || frame === "snapshot" || reducedMotion()) {
      note({ frame, settle: true, spans: next.length });
      stage.scene.settle(model.poses(next, details));
    } else {
      const beats = model.diff(before, next, details);
      note({ frame, beats: beats.map(b => b.type + ":" + b.actor) });
      stage.scene.play(beats);
      // 动画是讲解，事实为准：排完后对齐到最新记录（期间又来了新帧则交给那一帧）
      stage.scene.idle().then(() => { if (stage.played.get(state) === next) stage.scene.settle(model.poses(next, state.details())); });
    }
    stage.played.set(state, next);
    if (model.finished(next) && (!before || !model.finished(before))) {
      refreshWorld().catch(() => {});
    }
    describe(state);
    handBack(state, next, details);
  }

  // 焦点这件事已经办完（例如员工答复匠人提问的那一句），而更早的事还在办：小院回到那件事上。
  // 先让员工看清「办完了」，过一会儿再交还；期间员工又发了新话（焦点变了）就不交还。
  const HAND_BACK_MS = 6000;
  function handBack(state, next, details) {
    const model = C().model;
    if (!model.finished(next) || model.openItems(next, details).length || state.handingBack) return;
    state.handingBack = true;
    const back = () => {
      state.handingBack = false;
      if (stage.focus !== state) return;
      const other = (C().card.busyStates ? C().card.busyStates() : []).find(s => s !== state);
      if (other) focus(other);
    };
    if (typeof setTimeout === "function" && !stage.immediate) setTimeout(back, HAND_BACK_MS); else back();
  }

  // 小院下方一句：在演哪件事、办到哪了；有待本人处理项时直接在折叠栏上给快捷入口。
  function describe(state) {
    if (!stage.caption) return;
    const spans = state.prev || [], details = state.details();
    const sum = C().model.summary(spans, Date.now(), details);
    const parts = [sum.artisans ? sum.artisans + " 位匠人" : "", sum.tools ? sum.tools + " 次工具" : "",
      sum.seconds !== null ? sum.seconds + " 秒" : ""].filter(Boolean);
    stage.caption.textContent = "这件事" + sum.state + (parts.length ? "（" + parts.join("，") + "）" : "") + "。点角色或展开看经过。";
    if (stage.action) {
      const open = C().model.openItems(spans, details).find(i => i.pending_id);
      if (open && typeof openPending === "function") {
        stage.openPendingId = open.pending_id;
        stage.action.textContent = open.kind === "approval" ? "去盖章" : open.kind === "ruling" ? "去裁决" : "去回复";
        stage.action.classList.remove("hidden");
      } else {
        stage.openPendingId = null;
        stage.action.classList.add("hidden");
      }
    }
  }

  function select(state, key) { if (mounted() && stage.focus === state) stage.scene.select(key); }
  function restyle() { if (mounted()) stage.scene.restyle(); }
  function isFocus(state) { return stage.focus === state; }
  function hasFocus() { return stage.focus !== null; }
  function isExpanded() { return stage.expanded; }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.stage = { mount, mounted, focus, update, select, restyle, setTheme, setReply, setStep, stepOf, refreshWorld, isFocus, hasFocus,
    setExpanded, isExpanded, recent: () => stage.log.slice(),
    // 测试用：交还焦点不等待
    immediate: on => { stage.immediate = Boolean(on); } };
})();
