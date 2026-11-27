// 对话内卡片：确认卡、列表卡、按钮卡、答复卡、通知卡。
// 所有操作调用原模块的接口，带原幂等键与版本号；必须本人原生选择的动作只给按钮，由本人点击。
"use strict";

const LIST_KINDS = { reminder: "提醒", todo: "待办", event: "日程", matter: "事项", file: "文件" };
const MORE = {
  workspace: ["个人工作台", "index.html?classic=1#functions"],
};

function card(title, className) {
  const node = el("section", null, "card " + (className || ""));
  node.appendChild(el("h3", title));
  return node;
}

// 一条管家或分流回执里的全部卡片。
function renderRouteCards(route, options = {}) {
  const proposalId = route.proposal_id;
  if (Array.isArray(route.executed_actions) && route.executed_actions.length) {
    appendCard(executedActionsCard(route.executed_actions, options.requestId || route.request_id || "", options));
  }
  if (route.reminder_draft && proposalId) appendCard(confirmCard("reminder", route.reminder_draft, proposalId, options));
  if (route.workspace_draft && proposalId) appendCard(confirmCard("workspace", route.workspace_draft, proposalId, options));
  if (route.people_draft && proposalId) appendCard(peopleContactCard(route.people_draft, proposalId, options));
  if (route.open_reminders) appendCard(listCard("reminder"));
  if (route.open_workspace) appendCard(LIST_KINDS[route.open_workspace] ? listCard(route.open_workspace) : moreCard("workspace"));
  if (route.open_people) {
    if (typeof openContactsDrawer === "function") {
      openContactsDrawer().catch(report);
      const node = card("通讯录");
      node.appendChild(button("打开通讯录", () => openContactsDrawer().catch(report), "primary"));
      appendCard(node);
    } else {
      appendCard(moreCard("workspace"));
    }
  }
  if (route.work_task_id) appendCard(workTaskCard(route.work_task_id));
  // 其余五个模块的卡片见 module-cards.js 与 collab-cards.js
  if (route.proactive_draft || route.open_proactive) appendCard(proactiveCard(route.proactive_draft));
  if (route.task_definition_draft || route.open_task_definitions) appendCard(definitionCard(route.task_definition_draft));
  if (route.open_connections) appendCard(connectionsCard());
  if (route.shared_draft || route.open_shared) appendCard(sharedCard(route.shared_draft, proposalId));
  if (route.handoff_draft || route.open_collaboration) appendCard(collaborationCard(route.handoff_draft, proposalId));
}

// 私人助理自主办理回执卡：展示已直接落库的动作清单，支持一键撤回并在刷新后恢复撤回态。
function executedActionsCard(actions, requestId, options = {}) {
  const reqId = String(requestId || "").replace(/:assistant$/, "");
  const node = card("助理已办好", "auto-actions-card");
  const list = el("ul", null, "auto-actions-list");
  const controls = new Map();
  for (const item of actions) {
    const li = el("li", null, "auto-action-item");
    const info = el("div", null, "auto-action-info");
    info.append(el("span", item.title || "已办理", "auto-action-tag"));
    if (item.summary) info.append(el("span", item.summary, "auto-action-summary"));
    if (item.diff) {
      const det = el("details", null, "action-diff");
      det.appendChild(el("summary", "查看修改 Diff"));
      const pre = el("pre", null, "diff-lines");
      for (const line of String(item.diff).split("\n")) {
        const cls = line.startsWith("+") && !line.startsWith("+++")
          ? "diff-add"
          : line.startsWith("-") && !line.startsWith("---")
            ? "diff-del"
            : line.startsWith("@@") ? "diff-hunk" : "diff-ctx";
        pre.appendChild(el("span", line + "\n", cls));
      }
      det.appendChild(pre);
      info.appendChild(det);
    }
    li.appendChild(info);
    const status = el("span", "", "meta auto-action-state");
    if (item.action === "capture_bundle" && typeof openContactsDrawer === "function") {
      li.appendChild(button("查看通讯录", () => openContactsDrawer().catch(report), "auto-action-undo"));
    }
    if (item.undoable && reqId && item.action_id) {
      const btn = button("撤回", async () => {
        const epoch = entry.epoch;
        btn.disabled = true;
        status.textContent = "撤回中…";
        try {
          await entryApi(
            "POST",
            "/v1/assistant/actions/" + encodeURIComponent(reqId) + "/" + encodeURIComponent(item.action_id) + "/undo",
            {}
          );
          if (isStale(epoch)) return;
          btn.classList.add("hidden");
          status.textContent = "已撤回";
          refreshOverview().catch(report);
        } catch (error) {
          if (isStale(epoch)) return;
          btn.disabled = false;
          status.textContent = error.message || "撤回失败";
        }
      }, "auto-action-undo");
      controls.set(item.action_id, { btn, status });
      li.append(btn, status);
    } else {
      li.appendChild(status);
    }
    list.appendChild(li);
  }
  node.appendChild(list);
  if (actions.some(item => item.action === "create_reminder") && typeof createBrowserNotificationBanner === "function") {
    const banner = createBrowserNotificationBanner();
    if (banner) node.appendChild(banner);
  }
  if (options.restore && reqId && controls.size) {
    restoreUndoneActions(reqId, controls);
  }
  return node;
}

async function restoreUndoneActions(reqId, controls) {
  const epoch = entry.epoch;
  try {
    const body = await entryApi("GET", "/v1/assistant/actions/" + encodeURIComponent(reqId));
    if (isStale(epoch)) return;
    for (const aid of body.undone || []) {
      const ctrl = controls.get(aid);
      if (ctrl) {
        ctrl.btn.classList.add("hidden");
        ctrl.status.textContent = "已撤回";
      }
    }
  } catch (error) {
    if (!isStale(epoch) && error.status !== 404) report(error);
  }
}

// 需要本人原生选择或确认的动作：给一个明确按钮，由本人点击后在「更多」里完成。
function moreCard(key) {
  const [name, href] = MORE[key];
  const node = card(name);
  node.appendChild(el("p", "这一步需要你本人选择或确认，请在下面打开后操作。", "meta"));
  const link = el("a", "打开" + name);
  link.href = href;
  node.appendChild(link);
  return node;
}

function draftLines(kind, draft) {
  if (kind === "reminder") return [draft.title, when(draft.due_at) + " · " + (draft.timezone || "")];
  if (kind === "people") {
    const names = (draft.persons || []).map((item) => (item.action === "update" ? "更新 " : "新建 ") + (item.profile?.name || ""));
    const lines = [];
    if (names.length) lines.push("人物：" + names.join("、"));
    if ((draft.relations || []).length) lines.push("关系：" + draft.relations.length + " 条");
    if (draft.note?.content) lines.push("备注：" + draft.note.content.slice(0, 80));
    return lines.length ? lines : ["保存人物与关系记录"];
  }
  const data = draft.data || {};
  return [(LIST_KINDS[draft.kind] || draft.kind) + "：" + (data.title || data.name || "")];
}

// 个人知识图谱「原始记录拆解确认卡」位于 capture-card.js。

// 草稿确认卡：确认或取消都走 /v1/assistant/proposals，以草稿标识为幂等键；刷新后按持久状态恢复。
function confirmCard(kind, draft, proposalId, options = {}) {
  const node = card("请确认");
  for (const line of draftLines(kind, draft)) node.appendChild(el("p", line));
  const picks = kind === "people" ? (draft.persons || []).map(() => "") : [];
  if (kind === "people") {
    (draft.persons || []).forEach((item, idx) => {
      const cands = item.similar_candidates || [];
      if (item.action === "create" && cands.length) {
        const label = el("label", "发现疑似已有人物：", "meta");
        const sel = document.createElement("select");
        sel.append(new Option("作为新人物建档", ""));
        for (const c of cands) {
          sel.append(new Option("更新已有：" + c.name + (c.organization ? " · " + c.organization : ""), c.person_id));
        }
        sel.addEventListener("change", () => { picks[idx] = sel.value; });
        label.appendChild(sel);
        node.appendChild(label);
      }
    });
  }
  const row = el("div", null, "row"), status = el("p", "", "meta");
  const buildData = () => (kind === "reminder" ? withoutRequestId(draft) : kind === "people" ? cleanPeopleDraft(draft, picks) : (draft.data || {}));
  const path = "/v1/assistant/proposals/" + encodeURIComponent(proposalId);
  const settle = (text) => { row.replaceChildren(); status.textContent = text; };
  const act = (suffix, getBody, done) => async () => {
    const epoch = entry.epoch;
    if (kind === "reminder" && suffix === "/confirm" &&
        typeof browserNotificationState === "function" && browserNotificationState() === "default" &&
        typeof requestBrowserNotificationPermission === "function") {
      requestBrowserNotificationPermission().catch(() => {});
    }
    for (const item of row.children) item.disabled = true;
    try {
      const body = getBody ? getBody() : undefined;
      await entryApi("POST", path + suffix, body === undefined ? {} : { json: body });
      if (!isStale(epoch)) { settle(done); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      for (const item of row.children) item.disabled = false;
      status.textContent = error.message;
    }
  };
  row.append(button("确认", act("/confirm", () => ({ data: buildData() }), "已保存"), "primary"),
    button("取消", act("/cancel", null, "已取消")));
  node.append(row, status);
  if (options.restore) restoreProposal(path, row, settle);
  entry.cards.set("proposal:" + proposalId, node);
  return node;
}

function withoutRequestId(draft) {
  const copy = { ...draft };
  delete copy.request_id;
  return copy;
}

async function restoreProposal(path, row, settle) {
  const epoch = entry.epoch;
  try {
    const saved = await entryApi("GET", path);
    if (isStale(epoch)) return;
    if (saved.state === "confirmed") settle("已保存");
    else if (saved.state === "cancelled") settle("已取消");
  } catch (error) {
    if (!isStale(epoch)) { row.replaceChildren(); report(error); }
  }
}

const LIST_SOURCES = {
  reminder: () => "/v1/reminders",
  todo: () => "/v1/workspace/items?kind=todo",
  event: () => "/v1/workspace/items?kind=event",
  matter: () => "/v1/workspace/items?kind=matter",
  file: () => "/v1/workspace/items?kind=file",
};

// 列表卡：读原模块接口；可做的操作带原版本号。
function listCard(kind) {
  const node = card("你的" + LIST_KINDS[kind]);
  const list = el("ul"), status = el("p", "读取中…", "meta");
  node.append(list, status);
  const load = async () => {
    const epoch = entry.epoch;
    try {
      const body = await entryApi("GET", LIST_SOURCES[kind]());
      if (isStale(epoch)) return;
      let items = body.items || [];
      if (kind === "reminder") {
        items = items
          .filter(item => item && item.status !== "cancelled")
          .sort((a, b) => (a.status === "active" ? 0 : 1) - (b.status === "active" ? 0 : 1));
      }
      items = items.slice(0, 20);
      list.replaceChildren(...items.map(item => listRow(kind, item, load)));
      status.textContent = items.length ? "" : "暂无记录";
    } catch (error) {
      if (!isStale(epoch)) status.textContent = "读取失败：" + error.message;
    }
  };
  load();
  return node;
}

function listRow(kind, item, reload) {
  const row = el("li");
  if (kind === "reminder") {
    const isActive = !item.status || item.status === "active";
    const stateLabel = isActive ? "等待提醒" : "已提醒";
    row.append(el("span", item.title + " · " + when(item.due_at) + " · " + stateLabel + " "));
    if (isActive) {
      row.append(button("取消提醒", mutate("DELETE", "/v1/reminders/" + encodeURIComponent(item.reminder_id) +
        "?version=" + item.version, undefined, reload)));
    }
    return row;
  }
  const data = item.data || {};
  row.appendChild(el("span", (data.title || data.name || "") + " "));
  if (kind === "todo" && data.status === "open") {
    row.appendChild(button("标记完成", mutate("PUT", "/v1/workspace/items/" + encodeURIComponent(item.id),
      { data: { ...data, status: "done" }, version: item.version }, reload)));
  }
  if (kind === "event") row.appendChild(el("span", when(data.start_at), "meta"));
  if (kind === "file") {
    const link = el("a", "下载");
    link.href = "/v1/workspace/files/" + encodeURIComponent(item.id) + "/content";
    row.appendChild(link);
  }
  return row;
}

function mutate(method, path, json, reload) {
  return async (event) => {
    const epoch = entry.epoch, target = event && event.target;
    if (target) target.disabled = true;
    try {
      await entryApi(method, path, json === undefined ? {} : { json });
      if (!isStale(epoch)) { await reload(); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      if (target) target.disabled = false;
      report(error);
    }
  };
}

// 交办工作：显示受理后的状态；成果与行动项在「更多」里处理。
function workTaskCard(taskId) {
  const node = card("交办工作"), status = el("p", "读取中…", "meta");
  const link = el("a", "在「更多」里查看成果");
  link.href = "index.html?classic=1#functions";
  node.append(status, link);
  const epoch = entry.epoch;
  entryApi("GET", "/v1/work-tasks/" + encodeURIComponent(taskId)).then(task => {
    if (!isStale(epoch)) status.textContent = "状态：" + task.status;
  }).catch(error => { if (!isStale(epoch)) status.textContent = "读取失败：" + error.message; });
  return node;
}

// 匠人提问的答复卡：原话转给匠人；审批类问题只提示去待确认处理。
function answerCard(item) {
  const node = card(item.kind === "approval" ? "等你审批" : "匠人在问你");
  node.appendChild(el("p", item.question));
  if (item.kind === "approval") {
    node.appendChild(el("p", "请在待确认里处理这项审批。", "meta"));
    return node;
  }
  const input = el("textarea"), status = el("p", "", "meta"), id = requestId();
  input.rows = 2;
  const send = button("答复", async () => {
    const epoch = entry.epoch;
    send.disabled = true;
    try {
      await entryApi("POST", "/v1/assistant/pending/" + encodeURIComponent(item.pending_id) + "/answer",
        { json: { request_id: id, text: input.value } });
      if (!isStale(epoch)) { status.textContent = "已转达"; send.remove(); refreshOverview().catch(report); }
    } catch (error) {
      if (!isStale(epoch)) { send.disabled = false; status.textContent = error.message; }
    }
  }, "primary");
  node.append(input, send, status);
  return node;
}

// 通知卡：最近的通知，可逐条标为已读（原通知接口）。
function notificationCard() {
  const node = card("通知"), list = el("ul"), status = el("p", "读取中…", "meta");
  if (typeof createBrowserNotificationBanner === "function") {
    const banner = createBrowserNotificationBanner();
    if (banner) node.appendChild(banner);
  }
  node.append(list, status);
  const load = async () => {
    const epoch = entry.epoch;
    try {
      const body = await entryApi("GET", "/v1/notifications?latest=true&limit=20");
      if (isStale(epoch)) return;
      const items = (body.items || []).slice().reverse();
      list.replaceChildren(...items.map(item => {
        const row = el("li");
        const timeText = item.due_at != null ? "原定 " + when(item.due_at) : when(item.created_at);
        row.appendChild(el("span", item.title + (timeText ? " · " + timeText : "") + " "));
        if (item.body) row.appendChild(el("span", item.body + " ", "meta"));
        if (!item.read_at) {
          row.appendChild(button("标为已读", mutate("POST",
            "/v1/notifications/" + item.notification_id + "/read", undefined, async () => {
              if (typeof dismissEntryNotificationAlert === "function") dismissEntryNotificationAlert(item.notification_id);
              await load();
              if (typeof refreshEntryNotifications === "function") refreshEntryNotifications(epoch, { alertNew: false }).catch(() => {});
            })));
        } else {
          row.appendChild(el("span", "已读", "meta"));
        }
        return row;
      }));
      status.textContent = items.length ? "" : "暂无通知";
    } catch (error) {
      if (!isStale(epoch)) status.textContent = "读取失败：" + error.message;
    }
  };
  load();
  return node;
}
