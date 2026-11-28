// 常驻小院（R7）：打开入口就在页面上，管家在管家台、本人可用的匠人在垫子上待命。
// 员工说一句话后，小院立刻跟上这次请求（焦点），按执行事实演出；进展卡只讲「经过」。
// 一次只演一件事：最新发出的请求，或员工在某张进展卡上点「在小院里看」的那件。
"use strict";

(function () {
  const C = () => globalThis.Courtyard;
  const stage = { scene: null, focus: null, played: new WeakMap(), roster: [], rosterLoaded: false, caption: null,
    drawer: null, toggle: null, action: null, themeButtons: [], openPendingId: null, expanded: false, looks: null, log: [],
    replyBox: null, replyTitle: null, replyBody: null, replyExpand: null, replyExpanded: false, replies: new Map(), steps: new Map(), latestReply: "",
    hub: null, hubTab: "outcome", hubBody: null, hubTabs: null, hubNoteIdx: 0, hubAudit: null,
    worldData: { notes: [], todos: [], events: [], reminders: [], people: [], orgs: [], matters: [], files: [], folders: [] },
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
        link.href = "/workspace/marketplace";
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
    stage.hub = buildMainHub();
    box.append(host, bar, stage.replyBox, stage.hub, stage.drawer);
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
      hub: null, hubTab: "outcome", hubBody: null, hubTabs: null, hubNoteIdx: 0, hubAudit: null,
      worldData: { notes: [], todos: [], events: [], reminders: [], people: [], orgs: [], matters: [], files: [], folders: [] },
      worldCounts: { openTodos: 0, activeReminders: 0 } });
  }

  function fmtHubTime(ts) {
    if (!ts) return "";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtHubDate(ts) {
    if (!ts) return "";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return `${d.getMonth() + 1}月${d.getDate()}日`;
  }

  function fmtHubSize(bytes) {
    const n = Number(bytes) || 0;
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  function buildMainHub() {
    const hub = el("section", null, "cy-hub");
    hub.setAttribute("aria-label", "主界面成果与编排看板");
    const head = el("div", null, "cy-hub-head");
    stage.hubTabs = el("div", null, "cy-hub-tabs");
    head.append(stage.hubTabs);
    stage.hubBody = el("div", null, "cy-hub-body");
    hub.append(head, stage.hubBody);
    renderMainHub();
    return hub;
  }

  function setHubTab(tab) {
    stage.hubTab = tab;
    renderMainHub();
  }

  async function toggleHubTodo(item) {
    if (!item || !item.id || typeof entryApi !== "function") return;
    const isDone = String((item.data && item.data.status) || "").toLowerCase() === "done";
    try {
      await entryApi("PATCH", "/v1/workspace/items/" + encodeURIComponent(item.id), {
        json: { version: item.version, data: { status: isDone ? "open" : "done" } },
      });
      await refreshWorld();
      if (typeof refreshOverview === "function") refreshOverview().catch(() => {});
    } catch (error) {
      if (typeof report === "function") report(error);
    }
  }

  async function inspectHubSpan(progress, span) {
    if (!progress || !span) return;
    stage.hubAudit = { span, loading: true, data: null, error: null };
    renderMainHub();
    const epoch = entry.epoch;
    try {
      const data = await entryApi(
        "GET",
        "/v1/traces/" + encodeURIComponent(progress.traceId) + "/spans/" + encodeURIComponent(span.span_id) + "/audit"
      );
      if (isStale(epoch)) return;
      stage.hubAudit = { span, loading: false, data, error: null };
    } catch (error) {
      if (isStale(epoch)) return;
      stage.hubAudit = { span, loading: false, data: null, error: (error && error.message) || "读取失败" };
    }
    renderMainHub();
  }

  function renderMainHub() {
    if (!stage.hub || !stage.hubTabs || !stage.hubBody) return;
    const wd = stage.worldData;
    const spans = stage.focus && Array.isArray(stage.focus.prev) ? stage.focus.prev : [];
    const running = spans.length > 0 && C().model && !C().model.finished(spans);
    const openTodosCount = (wd.todos || []).filter(t => String((t.data && t.data.status) || "open").toLowerCase() !== "done").length;

    const defs = [
      ["outcome", "最新成果" + (wd.notes.length ? " (" + wd.notes.length + ")" : "")],
      ["dag", "执行编排 DAG" + (running ? " · 进行中" : spans.length ? " (" + spans.length + "步)" : "")],
      ["assets", "待办与文档" + (openTodosCount || wd.files.length ? ` (${openTodosCount}/${wd.files.length})` : "")],
    ];
    stage.hubTabs.replaceChildren(...defs.map(([id, label]) => {
      const b = button(label, () => setHubTab(id), "cy-hub-tab" + (stage.hubTab === id ? " active" : "") + (id === "dag" && running ? " live" : ""));
      b.dataset.hubTab = id;
      return b;
    }));

    if (stage.hubTab === "dag") {
      renderHubDag(spans);
    } else if (stage.hubTab === "assets") {
      renderHubAssets(wd);
    } else {
      renderHubOutcome(wd);
    }
  }

  function renderHubOutcome(wd) {
    const body = stage.hubBody;
    const notes = wd.notes || [];
    const openTodos = (wd.todos || []).filter(t => String((t.data && t.data.status) || "open").toLowerCase() !== "done");
    const files = wd.files || [];
    const folderMap = new Map((wd.folders || []).map(f => [f.id, (f.data && f.data.name) || ""]));
    const matterMap = new Map((wd.matters || []).map(m => [m.id, (m.data && m.data.title) || ""]));
    const orgMap = new Map((wd.orgs || []).map(o => [o.id, o.name || ""]));
    const personMap = new Map((wd.people || []).map(p => [p.id, p]));
    const openShelf = typeof openBookshelfDrawer === "function" ? openBookshelfDrawer : (typeof openContactsDrawer === "function" ? openContactsDrawer : null);

    const grid = el("div", null, "cy-hub-grid");

    // 左列：最新会议/整理成果全景（标题、时间、摘要、核心结论、待确认、实体与文档直达）
    const left = el("div", null, "cy-hub-col primary");
    if (!notes.length) {
      const empty = el("div", null, "cy-hub-empty");
      empty.append(
        el("strong", "暂无会议纪要或整理记录"),
        el("p", "直接上传会议录音/文档或发一句话，管家会自动拆解结论、人脉、待办与正式纪要，并实时展示在这里。", "meta")
      );
      left.append(empty);
    } else {
      const idx = Math.min(stage.hubNoteIdx, notes.length - 1);
      const note = notes[idx];
      if (notes.length > 1) {
        const switcher = el("div", null, "cy-hub-switcher");
        notes.slice(0, 4).forEach((n, i) => {
          const label = (n.title || n.summary || "记录").slice(0, 14);
          switcher.append(button(label, () => { stage.hubNoteIdx = i; renderMainHub(); }, "cy-hub-pill" + (i === idx ? " active" : "")));
        });
        left.append(switcher);
      }
      const kindMap = { meeting: "会议纪要", note: "随手记", card: "名片", call: "通话记录" };
      const top = el("div", null, "cy-hub-note-head");
      top.append(
        el("span", kindMap[note.record_kind] || "记录", "outcome-kind-badge"),
        el("strong", note.title || "未命名记录", "cy-hub-note-title")
      );
      left.append(top);
      const meta = [
        note.occurred_at ? "时间：" + fmtHubTime(note.occurred_at) : "",
        note.location ? "地点：" + note.location : "",
      ].filter(Boolean).join(" · ");
      if (meta) left.append(el("div", meta, "outcome-meta"));
      if (note.summary) left.append(el("p", note.summary, "outcome-summary"));

      const decisions = Array.isArray(note.decisions) ? note.decisions : [];
      const questions = Array.isArray(note.open_questions) ? note.open_questions : [];
      if (decisions.length || questions.length) {
        const cols = el("div", null, "outcome-cols");
        if (decisions.length) {
          const box = el("div", null, "outcome-panel decisions");
          box.append(el("div", "核心结论（" + decisions.length + "）", "outcome-panel-title"));
          const ul = el("ul", null, "outcome-bullets");
          for (const d of decisions) ul.append(el("li", d));
          box.append(ul);
          cols.append(box);
        }
        if (questions.length) {
          const box = el("div", null, "outcome-panel questions");
          box.append(el("div", "待确认项（" + questions.length + "）", "outcome-panel-title"));
          const ul = el("ul", null, "outcome-bullets");
          for (const q of questions) ul.append(el("li", q));
          box.append(ul);
          cols.append(box);
        }
        left.append(cols);
      }

      // 关联实体芯片（项目 / 公司 / 人脉）
      const chips = el("div", null, "outcome-entities");
      for (const mid of note.matter_ids || (note.matter_id ? [note.matter_id] : [])) {
        const title = matterMap.get(mid);
        if (title) chips.append(button("项目 · " + title, () => openShelf && openShelf({ tab: "matters" }).catch(report), "outcome-chip matter"));
      }
      for (const oid of note.org_ids || []) {
        const name = orgMap.get(oid);
        if (name) chips.append(button("公司 · " + name, () => openShelf && openShelf({ tab: "orgs" }).catch(report), "outcome-chip org"));
      }
      for (const pid of note.person_ids || []) {
        const p = personMap.get(pid);
        if (p) chips.append(button("人脉 · " + p.name, () => openShelf && openShelf({ tab: "people" }).catch(report), "outcome-chip person"));
      }
      if (chips.children.length) left.append(chips);
    }

    // 右列：活跃待办（可就地勾选）+ 最近文档与纪要（一键分屏看全文）
    const right = el("div", null, "cy-hub-col side");
    const todoCard = el("div", null, "outcome-panel todos");
    const todoHead = el("div", null, "outcome-panel-head");
    todoHead.append(el("span", "活跃待办（" + openTodos.length + "）", "outcome-panel-title"));
    if (typeof openBoardDrawer === "function") {
      todoHead.append(button("全部待办", () => openBoardDrawer({ tab: "todos" }).catch(report), "outcome-mini-btn"));
    }
    todoCard.append(todoHead);
    if (!openTodos.length) {
      todoCard.append(el("p", "当前暂无未完成待办。", "meta"));
    } else {
      const ul = el("ul", null, "cy-hub-todo-list");
      for (const t of openTodos.slice(0, 5)) {
        const d = t.data || {};
        const li = el("li", null, "cy-hub-todo-row");
        const chk = document.createElement("input");
        chk.type = "checkbox";
        chk.checked = false;
        chk.title = "勾选标记已完成";
        chk.addEventListener("change", () => toggleHubTodo(t));
        const textWrap = el("div", null, "cy-hub-todo-main");
        textWrap.append(el("div", d.title || "待办", "cy-hub-todo-title"));
        const subParts = [d.matter_id ? matterMap.get(d.matter_id) : "", d.due_at ? "截止 " + fmtHubDate(d.due_at) : ""].filter(Boolean);
        if (subParts.length) textWrap.append(el("div", subParts.join(" · "), "cy-hub-todo-sub"));
        li.append(chk, textWrap);
        ul.append(li);
      }
      todoCard.append(ul);
    }
    right.append(todoCard);

    const fileCard = el("div", null, "outcome-panel files");
    const fileHead = el("div", null, "outcome-panel-head");
    fileHead.append(el("span", "最近文档与纪要（" + files.length + "）", "outcome-panel-title"));
    if (openShelf) {
      fileHead.append(button("全部文件", () => openShelf({ tab: "files" }).catch(report), "outcome-mini-btn"));
    }
    fileCard.append(fileHead);
    if (!files.length) {
      fileCard.append(el("p", "尚未上传或生成工作台文档。", "meta"));
    } else {
      const list = el("div", null, "cy-hub-file-list");
      for (const f of files.slice(0, 4)) {
        const d = f.data || {};
        const row = el("div", null, "cy-hub-file-row");
        const metaWrap = el("div", null, "cy-hub-file-meta");
        metaWrap.append(el("strong", d.name || f.id, "cy-hub-file-name"));
        const badges = el("div", null, "outcome-file-badges");
        const folderName = d.parent_id ? folderMap.get(d.parent_id) : "";
        if (folderName) badges.append(el("span", folderName, "outcome-folder-pill"));
        const mTitle = d.matter_id ? matterMap.get(d.matter_id) : "";
        if (mTitle) badges.append(el("span", mTitle, "outcome-tag-pill"));
        badges.append(el("span", fmtHubSize(d.size), "meta"));
        metaWrap.append(badges);
        row.append(metaWrap);
        if (typeof openWorkspaceFilePreview === "function") {
          row.append(button("看全文", () => openWorkspaceFilePreview(f.id, d.name || f.id).catch(report), "primary outcome-mini-btn"));
        }
        list.append(row);
      }
      fileCard.append(list);
    }
    right.append(fileCard);

    grid.append(left, right);
    body.replaceChildren(grid);
  }

  function renderHubDag(spans) {
    const body = stage.hubBody;
    if (!spans.length) {
      const empty = el("div", null, "cy-hub-empty");
      empty.append(
        el("strong", "等待执行编排"),
        el("p", "发送一条任务或上传文档后，这里会以宽屏 DAG 节点流实时展示每一步并行与串行编排，并与上方小院联动。", "meta")
      );
      body.replaceChildren(empty);
      return;
    }
    const wrap = el("div", null, "cy-hub-dag-wrap");
    const tipRow = el("div", null, "outcome-panel-head");
    tipRow.append(
      el("span", "执行编排图（点击任意工具节点可直接查看入参、返回与耗时）", "outcome-panel-title"),
      button("查看完整执行树", () => setExpanded(true), "outcome-mini-btn")
    );
    wrap.append(tipRow);

    if (typeof renderExecutionDag === "function" && stage.focus && stage.focus.progress) {
      const dag = renderExecutionDag(stage.focus.progress, spans, null, (sp) => {
        if (sp.kind === "tool") inspectHubSpan(stage.focus.progress, sp);
      });
      if (dag) wrap.append(dag);
    }
    if (stage.hubAudit) {
      const box = el("div", null, "cy-hub-audit-box");
      const h = el("div", null, "outcome-panel-head");
      h.append(
        el("strong", "节点审计：" + (stage.hubAudit.span.name || stage.hubAudit.span.span_id)),
        button("关闭", () => { stage.hubAudit = null; renderMainHub(); }, "outcome-mini-btn")
      );
      box.append(h);
      if (typeof renderAuditDetailBox === "function") {
        box.append(renderAuditDetailBox(stage.hubAudit));
      }
      wrap.append(box);
    }
    body.replaceChildren(wrap);
  }

  function renderHubAssets(wd) {
    const body = stage.hubBody;
    const folderMap = new Map((wd.folders || []).map(f => [f.id, (f.data && f.data.name) || ""]));
    const matterMap = new Map((wd.matters || []).map(m => [m.id, (m.data && m.data.title) || ""]));
    const wrap = el("div", null, "cy-hub-grid");

    const todoCol = el("div", null, "cy-hub-col");
    const openTodos = (wd.todos || []).filter(t => String((t.data && t.data.status) || "open").toLowerCase() !== "done");
    todoCol.append(el("div", "全部未完成待办（" + openTodos.length + "）", "outcome-panel-title"));
    const ul = el("ul", null, "cy-hub-todo-list");
    for (const t of openTodos) {
      const d = t.data || {};
      const li = el("li", null, "cy-hub-todo-row");
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.addEventListener("change", () => toggleHubTodo(t));
      const tw = el("div", null, "cy-hub-todo-main");
      tw.append(el("div", d.title || "待办", "cy-hub-todo-title"));
      const sub = [d.matter_id ? matterMap.get(d.matter_id) : "", d.due_at ? "截止 " + fmtHubDate(d.due_at) : "", d.description ? String(d.description).slice(0, 60) : ""].filter(Boolean).join(" · ");
      if (sub) tw.append(el("div", sub, "cy-hub-todo-sub"));
      li.append(chk, tw);
      ul.append(li);
    }
    if (!openTodos.length) todoCol.append(el("p", "暂无待办。", "meta"));
    else todoCol.append(ul);

    const fileCol = el("div", null, "cy-hub-col");
    const files = wd.files || [];
    fileCol.append(el("div", "工作台全部文档（" + files.length + "）", "outcome-panel-title"));
    const fl = el("div", null, "cy-hub-file-list");
    for (const f of files) {
      const d = f.data || {};
      const row = el("div", null, "cy-hub-file-row");
      const mw = el("div", null, "cy-hub-file-meta");
      mw.append(el("strong", d.name || f.id, "cy-hub-file-name"));
      const badges = el("div", null, "outcome-file-badges");
      if (d.parent_id && folderMap.get(d.parent_id)) badges.append(el("span", folderMap.get(d.parent_id), "outcome-folder-pill"));
      for (const tg of (d.tags || []).slice(0, 3)) badges.append(el("span", "#" + tg, "outcome-tag-pill"));
      badges.append(el("span", fmtHubSize(d.size), "meta"));
      mw.append(badges);
      row.append(mw);
      if (typeof openWorkspaceFilePreview === "function") {
        row.append(button("看全文", () => openWorkspaceFilePreview(f.id, d.name || f.id).catch(report), "primary outcome-mini-btn"));
      }
      fl.append(row);
    }
    if (!files.length) fileCol.append(el("p", "暂无文档。", "meta"));
    else fileCol.append(fl);

    wrap.append(todoCol, fileCol);
    body.replaceChildren(wrap);
  }

  async function refreshWorld() {
    if (!stage.scene || typeof stage.scene.setWorld !== "function") return;
    const epoch = entry.epoch;
    const [todosRes, eventsRes, remRes, peopleRes, orgsRes, mattersRes, notesRes, filesRes, foldersRes] = await Promise.all([
      entryApi("GET", "/v1/workspace/items?kind=todo").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=event").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/reminders?status=active").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/people").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/people/orgs").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=matter").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/people/notes?limit=6").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=file").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=folder").catch(() => ({ items: [] })),
    ]);
    if (isStale(epoch) || !stage.scene || typeof stage.scene.setWorld !== "function") return;
    const rawTodos = (todosRes.items || []).filter(i => i && !i.trashed_at);
    const rawEvents = (eventsRes.items || []).filter(i => i && !i.trashed_at);
    const rawMatters = (mattersRes.items || []).filter(i => i && !i.trashed_at);
    const rawFiles = (filesRes.items || []).filter(i => i && !i.trashed_at).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const rawFolders = (foldersRes.items || []).filter(i => i && !i.trashed_at);
    const rawNotes = (notesRes.items || []).filter(n => n && !n.trashed_at);
    const rawPeople = (peopleRes.items || []).filter(p => p && !p.trashed_at);
    const rawOrgs = (orgsRes.items || []).filter(o => o && !o.trashed_at);
    const todos = rawTodos.map(i => {
      const d = i.data || {};
      const st = String(d.status || "").toLowerCase();
      return { id: i.id, title: d.title || d.text || d.summary || "待办", done: st === "done" || st === "completed" || st === "closed" };
    });
    const events = rawEvents.map(i => {
      const d = i.data || {};
      return { id: i.id, title: d.title || d.name || "日程" };
    });
    const reminders = (remRes.items || []).filter(r => r && r.status !== "cancelled").map(r => ({ id: r.reminder_id || r.id, title: r.title || "提醒" }));
    const booksCount = rawPeople.length + rawMatters.length;
    stage.worldCounts = { openTodos: todos.filter(t => !t.done).length, activeReminders: reminders.length };
    stage.worldData = {
      notes: rawNotes,
      todos: rawTodos,
      events: rawEvents,
      reminders,
      people: rawPeople,
      orgs: rawOrgs,
      matters: rawMatters,
      files: rawFiles,
      folders: rawFolders,
    };
    stage.scene.setWorld({ todos, events, reminders, booksCount });
    renderMainHub();
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

  // 保留常驻待命匠人（如文档分析、联网调研匠人）在各自垫子上，不因本轮未派工而消失。
  function castFor(spans) {
    const active = C().model.artisans(spans || []);
    return [...new Set([...stage.roster, ...active])];
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
    stage.scene.cast(castFor(spans));
    stage.scene.settle(C().model.poses(spans, state.details()));
    stage.played.set(state, spans);
    stage.scene.select(state.sel);
    attachPanel(state);
    if (options.expand) setExpanded(true);
    describe(state);
    syncFocusedReply();
    renderMainHub();
  }

  // 卡片每处理完一帧交过来；不是焦点的卡片不演（员工点「在小院里看」才切过来）。
  function update(state, frame, next, details) {
    if (!mounted()) return;
    if (state.wantsFocus) { state.wantsFocus = false; stage.focus = state; stage.played.delete(state); }
    if (stage.focus !== state) return;
    attachPanel(state);
    const model = C().model, before = stage.played.get(state);
    stage.scene.cast(castFor(next));
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
    const isNowFinished = model.finished(next);
    if (!isNowFinished && next.length > 0) {
      // 任务执行中自动把宽屏主面板切到 DAG 编排图，与上方小院动画实时联动
      stage.hubTab = "dag";
    } else if (isNowFinished && (!before || !model.finished(before))) {
      // 刚完成时切回「最新成果」并立刻刷新数据，让最终成果直接呈现在主界面
      stage.hubTab = "outcome";
      stage.hubNoteIdx = 0;
      refreshWorld().catch(() => {});
    }
    renderMainHub();
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
  globalThis.Courtyard.stage = { mount, mounted, focus, update, select, restyle, setTheme, setReply, setStep, stepOf, refreshWorld, setHubTab, isFocus, hasFocus,
    setExpanded, isExpanded, recent: () => stage.log.slice(),
    // 测试用：交还焦点不等待
    immediate: on => { stage.immediate = Boolean(on); } };
})();
