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
  if (route.reminder_draft && proposalId) appendCard(confirmCard("reminder", route.reminder_draft, proposalId, options));
  if (route.workspace_draft && proposalId) appendCard(confirmCard("workspace", route.workspace_draft, proposalId, options));
  if (route.people_draft && proposalId) appendCard(peopleContactCard(route.people_draft, proposalId, options));
  if (route.open_reminders) appendCard(listCard("reminder"));
  if (route.open_workspace) appendCard(LIST_KINDS[route.open_workspace] ? listCard(route.open_workspace) : moreCard("workspace"));
  if (route.open_people) appendCard(moreCard("workspace"));
  if (route.work_task_id) appendCard(workTaskCard(route.work_task_id));
  // 其余五个模块的卡片见 module-cards.js 与 collab-cards.js
  if (route.proactive_draft || route.open_proactive) appendCard(proactiveCard(route.proactive_draft));
  if (route.task_definition_draft || route.open_task_definitions) appendCard(definitionCard(route.task_definition_draft));
  if (route.open_connections) appendCard(connectionsCard());
  if (route.shared_draft || route.open_shared) appendCard(sharedCard(route.shared_draft, proposalId));
  if (route.handoff_draft || route.open_collaboration) appendCard(collaborationCard(route.handoff_draft, proposalId));
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

function peopleContactField(labelText, inputEl, fullWidth = false) {
  const wrap = el("label", null, "pcard-field" + (fullWidth ? " full" : ""));
  wrap.append(el("span", labelText, "pcard-label"), inputEl);
  return wrap;
}

function peopleInput(value, placeholder = "", type = "text") {
  const inp = document.createElement("input");
  inp.type = type;
  inp.value = value == null ? "" : String(value);
  if (placeholder) inp.placeholder = placeholder;
  return inp;
}

// 联系人名片确认卡：可视化名片布局 + 字段就地可补全编辑 + 重名候选切换 + 确认后锁定展示。
function peopleContactCard(draft, proposalId, options = {}) {
  const node = el("section", null, "card people-contact-card");
  const editors = [];
  const genderLabel = { male: "男", female: "女", other: "其他", unknown: "" };

  (draft.persons || []).forEach((item) => {
    const p = item.profile || {};
    const firstOf = (kind) => (p.contacts || []).find((c) => c.kind === kind)?.value || "";
    const preservedContacts = (p.contacts || []).filter((c) => !["phone", "wechat", "email"].includes(c.kind));
    const extraObj = { ...(p.extra || {}) };
    const initialAddress = extraObj.address || extraObj["地址"] || "";
    delete extraObj.address;
    delete extraObj["地址"];

    const block = el("div", null, "pcard-person");
    const head = el("div", null, "pcard-head");
    const avatar = el("div", (p.name || "人").slice(0, 1), "pcard-avatar");
    const titleWrap = el("div", null, "pcard-title-wrap");
    const titleRow = el("div", null, "pcard-title-row");
    const nameTitle = el("strong", p.name || "未命名联系人", "pcard-name");
    const genderPill = el("span", genderLabel[p.gender] || "", "pcard-pill" + (genderLabel[p.gender] ? "" : " hidden"));
    const badge = el("span", item.action === "update" ? "更新联系人" : "新建联系人", "pcard-badge");
    titleRow.append(nameTitle, genderPill, badge);
    const sub = el("p", [p.organization, p.title, p.city].filter(Boolean).join(" · ") || "请核对或补全名片信息", "pcard-sub");
    titleWrap.append(titleRow, sub);
    head.append(avatar, titleWrap);
    block.append(head);

    let chosenCandidate = "";
    const cands = item.similar_candidates || [];
    if (item.action === "create" && cands.length) {
      const sel = document.createElement("select");
      sel.append(new Option("作为新联系人建档", ""));
      for (const c of cands) {
        sel.append(new Option("合并更新已有：" + c.name + (c.organization ? " · " + c.organization : ""), c.person_id));
      }
      sel.addEventListener("change", () => {
        chosenCandidate = sel.value;
        badge.textContent = chosenCandidate ? "合并更新已有" : "新建联系人";
      });
      block.append(peopleContactField("疑似重名联系人", sel, true));
    }

    const nameIn = peopleInput(p.name, "姓名");
    const genderSel = document.createElement("select");
    genderSel.append(new Option("未填", "unknown"), new Option("女", "female"), new Option("男", "male"), new Option("其他", "other"));
    genderSel.value = p.gender || "unknown";
    const orgIn = peopleInput(p.organization, "公司/机构全称（如 中国电信股份有限公司上海分公司）");
    const deptTitleIn = peopleInput([p.department, p.title].filter(Boolean).join(" / "), "部门 / 职位（可选）");
    const phoneIn = peopleInput(firstOf("phone"), "手机号");
    const wechatIn = peopleInput(firstOf("wechat"), "微信号（可选）");
    const emailIn = peopleInput(firstOf("email"), "邮箱（可选）", "email");
    const cityIn = peopleInput(p.city, "城市（如 上海）");
    const addrIn = peopleInput(initialAddress, "完整详细地址（省市区 + 园区/路号 + 楼栋楼层）");
    const birthIn = peopleInput(
      p.birth ? [p.birth.year, p.birth.month, p.birth.day].filter((x) => x != null).join("-") : "",
      "出生年份或生日（如 1988 或 1988-05-12）"
    );
    const tagsIn = peopleInput((p.tags || []).join("，"), "标签（逗号分隔）");
    const summaryIn = peopleInput(p.summary, "备注 / 认识背景");

    nameIn.addEventListener("input", () => {
      const v = nameIn.value.trim() || "人";
      nameTitle.textContent = v;
      avatar.textContent = v.slice(0, 1);
    });
    orgIn.addEventListener("input", () => {
      sub.textContent = [orgIn.value.trim(), cityIn.value.trim()].filter(Boolean).join(" · ") || "请核对或补全名片信息";
    });
    genderSel.addEventListener("change", () => {
      const txt = genderLabel[genderSel.value] || "";
      genderPill.textContent = txt;
      genderPill.classList.toggle("hidden", !txt);
    });

    const grid = el("div", null, "pcard-grid");
    grid.append(
      peopleContactField("姓名", nameIn),
      peopleContactField("性别", genderSel),
      peopleContactField("公司 / 机构", orgIn, true),
      peopleContactField("部门 / 职位", deptTitleIn),
      peopleContactField("手机", phoneIn),
      peopleContactField("微信", wechatIn),
      peopleContactField("城市", cityIn),
      peopleContactField("完整地址", addrIn, true),
      peopleContactField("邮箱", emailIn),
      peopleContactField("出生年份 / 生日", birthIn),
      peopleContactField("标签", tagsIn, true),
      peopleContactField("备注", summaryIn, true)
    );
    block.append(grid);
    node.append(block);

    editors.push({
      item,
      preservedContacts,
      extraObj,
      inputs: [nameIn, genderSel, orgIn, deptTitleIn, phoneIn, wechatIn, emailIn, cityIn, addrIn, birthIn, tagsIn, summaryIn],
      build() {
        const { similar_candidates, ...rest } = item;
        const contacts = [...preservedContacts];
        if (phoneIn.value.trim()) contacts.push({ kind: "phone", value: phoneIn.value.trim(), label: "手机", primary: true });
        if (wechatIn.value.trim()) contacts.push({ kind: "wechat", value: wechatIn.value.trim(), label: "微信", primary: false });
        if (emailIn.value.trim()) contacts.push({ kind: "email", value: emailIn.value.trim(), label: "邮箱", primary: false });
        const dt = deptTitleIn.value.trim().split(/[/／]/).map((s) => s.trim()).filter(Boolean);
        const department = dt.length > 1 ? dt[0] : "";
        const title = dt.length > 1 ? dt.slice(1).join(" / ") : (dt[0] || "");
        const extra = { ...extraObj };
        if (addrIn.value.trim()) extra.address = addrIn.value.trim();
        let birth = p.birth || null;
        const bText = birthIn.value.trim();
        if (!bText) {
          birth = null;
        } else {
          const nums = bText.split(/[-/.年 月日]+/).map((s) => Number(s)).filter((n) => Number.isInteger(n) && n > 0);
          if (nums.length === 1 && nums[0] >= 1900) birth = { year: nums[0], month: null, day: null, calendar: "solar" };
          else if (nums.length === 2 && nums[0] <= 12) birth = { year: null, month: nums[0], day: nums[1], calendar: "solar" };
          else if (nums.length >= 3) birth = { year: nums[0], month: nums[1], day: nums[2], calendar: "solar" };
        }
        const tags = tagsIn.value.trim() ? tagsIn.value.trim().split(/[,，、]+/).map((s) => s.trim()).filter(Boolean) : [];
        const profile = {
          name: nameIn.value.trim() || p.name,
          aliases: p.aliases || [],
          gender: genderSel.value || "unknown",
          birth,
          organization: orgIn.value.trim(),
          department,
          title,
          city: cityIn.value.trim(),
          contacts,
          tags,
          summary: summaryIn.value.trim(),
          attachment_file_ids: p.attachment_file_ids || [],
          extra,
        };
        if (chosenCandidate) {
          const matched = (similar_candidates || []).find((c) => c.person_id === chosenCandidate);
          return { ...rest, action: "update", person_id: chosenCandidate, expected_version: matched?.version ?? null, profile };
        }
        return { ...rest, profile };
      },
    });
  });

  if ((draft.relations || []).length) {
    const relList = el("div", null, "pcard-meta-row");
    relList.append(el("span", "关联关系：" + draft.relations.map((r) => r.relation_type + (r.role ? "(" + r.role + ")" : "")).join(" · "), "meta"));
    node.append(relList);
  }
  if (draft.note?.content) {
    node.append(el("p", "原始记录：" + draft.note.content, "pcard-note"));
  }

  const row = el("div", null, "row pcard-actions"), status = el("p", "", "meta");
  const path = "/v1/assistant/proposals/" + encodeURIComponent(proposalId);
  const lockInputs = () => {
    for (const ed of editors) for (const inp of ed.inputs) inp.disabled = true;
    for (const sel of node.querySelectorAll("select")) sel.disabled = true;
  };
  const settle = (text, saved = false) => {
    lockInputs();
    row.replaceChildren();
    status.textContent = text;
    if (saved) {
      const link = el("a", "在「人物与关系」中查看 →", "pcard-link");
      link.href = "index.html?classic=1#functions";
      row.append(link);
    }
  };
  const act = (suffix, getBody, done, saved) => async () => {
    const epoch = entry.epoch;
    for (const item of row.children) item.disabled = true;
    try {
      const body = getBody ? getBody() : undefined;
      await entryApi("POST", path + suffix, body === undefined ? {} : { json: body });
      if (!isStale(epoch)) { settle(done, saved); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      for (const item of row.children) item.disabled = false;
      status.textContent = error.message;
    }
  };
  row.append(
    button("确认保存名片", act("/confirm", () => ({
      data: { persons: editors.map((e) => e.build()), relations: draft.relations || [], note: draft.note || null },
    }), "已保存到个人人物库", true), "primary"),
    button("取消", act("/cancel", null, "已取消", false))
  );
  node.append(row, status);
  if (options.restore) restoreProposal(path, row, (t) => settle(t, t === "已保存"));
  entry.cards.set("proposal:" + proposalId, node);
  return node;
}

function cleanPeopleDraft(draft, picks = []) {
  return {
    persons: (draft.persons || []).map((item, idx) => {
      const { similar_candidates, ...rest } = item;
      const chosenId = picks[idx];
      if (chosenId) {
        const chosen = (similar_candidates || []).find((c) => c.person_id === chosenId);
        return { ...rest, action: "update", person_id: chosenId, expected_version: chosen?.version ?? null };
      }
      return rest;
    }),
    relations: draft.relations || [],
    note: draft.note || null,
  };
}

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
