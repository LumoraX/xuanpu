// 对话内卡片：确认卡、列表卡、按钮卡、答复卡、通知卡。
// 所有操作调用原模块的接口，带原幂等键与版本号；必须本人原生选择的动作只给按钮，由本人点击。
"use strict";

const LIST_KINDS = { reminder: "提醒", todo: "待办", event: "日程", matter: "事项", file: "文件" };
const MORE = {
  proactive: ["业务跟进", "index.html?classic=1#functions"],
  definition: ["执行安排", "index.html?classic=1#functions"],
  connections: ["企业连接", "index.html?classic=1&connections=1"],
  shared: ["共享协作", "index.html?classic=1#functions"],
  collaboration: ["工作交接与审批", "index.html?classic=1#functions"],
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
  if (route.reminder_draft && proposalId) appendCard(confirmCard("reminder", route.reminder_draft, proposalId, options));
  if (route.workspace_draft && proposalId) appendCard(confirmCard("workspace", route.workspace_draft, proposalId, options));
  if (route.open_reminders) appendCard(listCard("reminder"));
  if (route.open_workspace) appendCard(LIST_KINDS[route.open_workspace] ? listCard(route.open_workspace) : moreCard("workspace"));
  if (route.work_task_id) appendCard(workTaskCard(route.work_task_id));
  if (route.proactive_draft || route.open_proactive) appendCard(moreCard("proactive"));
  if (route.task_definition_draft || route.open_task_definitions) appendCard(moreCard("definition"));
  if (route.open_connections) appendCard(moreCard("connections"));
  if (route.shared_draft || route.open_shared) appendCard(moreCard("shared"));
  if (route.handoff_draft || route.open_collaboration) appendCard(moreCard("collaboration"));
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
  const data = draft.data || {};
  return [(LIST_KINDS[draft.kind] || draft.kind) + "：" + (data.title || data.name || "")];
}

// 草稿确认卡：确认或取消都走 /v1/assistant/proposals，以草稿标识为幂等键；刷新后按持久状态恢复。
function confirmCard(kind, draft, proposalId, options = {}) {
  const node = card("请确认");
  for (const line of draftLines(kind, draft)) node.appendChild(el("p", line));
  const row = el("div", null, "row"), status = el("p", "", "meta");
  const data = kind === "reminder" ? withoutRequestId(draft) : (draft.data || {});
  const path = "/v1/assistant/proposals/" + encodeURIComponent(proposalId);
  const settle = (text) => { row.replaceChildren(); status.textContent = text; };
  const act = (suffix, body, done) => async () => {
    const epoch = entry.epoch;
    for (const item of row.children) item.disabled = true;
    try {
      await entryApi("POST", path + suffix, body === undefined ? {} : { json: body });
      if (!isStale(epoch)) { settle(done); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      for (const item of row.children) item.disabled = false;
      status.textContent = error.message;
    }
  };
  row.append(button("确认", act("/confirm", { data }, "已保存"), "primary"),
    button("取消", act("/cancel", undefined, "已取消")));
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
  reminder: () => "/v1/reminders?status=active",
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
      const items = (body.items || []).slice(0, 20);
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
    row.append(el("span", item.title + " · " + when(item.due_at) + " "),
      button("取消提醒", mutate("DELETE", "/v1/reminders/" + encodeURIComponent(item.reminder_id) +
        "?version=" + item.version, undefined, reload)));
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
  node.append(list, status);
  const load = async () => {
    const epoch = entry.epoch;
    try {
      const body = await entryApi("GET", "/v1/notifications?latest=true&limit=20");
      if (isStale(epoch)) return;
      const items = (body.items || []).slice().reverse();
      list.replaceChildren(...items.map(item => {
        const row = el("li");
        row.appendChild(el("span", item.title + " · " + when(item.created_at) + " "));
        if (!item.read_at) row.appendChild(button("标为已读", mutate("POST",
          "/v1/notifications/" + item.notification_id + "/read", undefined, load)));
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
