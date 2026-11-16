// 其余五个模块的对话内卡片：业务跟进、执行安排、企业连接、共享事项、交接与审批。
// 一律调用原模块接口，请求体与幂等键、版本号与功能页相同；同一张卡重试复用冻结的请求，避免重复处理。
// 需要本人原生选择的动作（授权同意、审批决定、交接对象与资料）都在卡片里由本人点击完成。
"use strict";

// 同一张卡上的一次提交：冻结请求体，失败可用同一请求重试；代际变化后结果丢弃。
function frozenAction(label, method, path, request, onDone, className) {
  const status = el("p", "", "meta");
  const node = button(label, async () => {
    const epoch = entry.epoch;
    node.disabled = true;
    status.textContent = "正在提交…";
    try {
      const result = await entryApi(method, path, { json: request });
      if (isStale(epoch)) return;
      status.textContent = "";
      await onDone(result);
      refreshOverview().catch(report);
    } catch (error) {
      if (isStale(epoch)) return;
      status.textContent = error.status === 409 ? "记录已经变化，请重新读取后再操作。" : error.message;
      // 4xx 是确定的拒绝；其余结果未知，保留同一请求重试
      if (!error.status || error.status >= 500) { node.disabled = false; node.textContent = "重试同一操作"; }
    }
  }, className);
  return [node, status];
}

// 读一个列表并逐条渲染；失败只影响这张卡。
async function loadInto(list, status, path, pick, render) {
  const epoch = entry.epoch;
  status.textContent = "读取中…";
  try {
    const body = await entryApi("GET", path);
    if (isStale(epoch)) return;
    const items = pick(body);
    list.replaceChildren(...items.slice(0, 20).map(render));
    status.textContent = items.length ? "" : "暂无记录";
  } catch (error) {
    if (!isStale(epoch)) status.textContent = "读取失败：" + error.message;
  }
}

function requestTime(input) {
  const stamp = new Date(input.value).getTime() / 1000;
  if (!Number.isFinite(stamp) || stamp <= Date.now() / 1000) throw new Error("请选择将来的具体时间。");
  return stamp;
}

// ---------- 业务跟进（/v1/proactive） ----------

const PROACTIVE_LABELS = { complete: "完成了", progress: "有新进展", ignore_stage: "本阶段不用跟进",
  postpone: "延后", set_reminder: "设置提醒" };

function proactiveRequest(item, action, dueAt) {
  return { request_id: requestId(), version: item.version,
    ...(item.item_version ? { item_version: item.item_version } : {}),
    action, due_at: dueAt, timezone: timezone(), channels: ["inbox"] };
}

function proactiveRow(item, reload) {
  const row = el("li");
  row.appendChild(el("span", item.title + " · " + (item.reason || item.body || "待核实最新进展") + " "));
  const path = "/v1/proactive/" + encodeURIComponent(item.id) + "/actions";
  for (const action of ["complete", "progress", "ignore_stage"]) {
    row.append(...frozenAction(PROACTIVE_LABELS[action], "POST", path, proactiveRequest(item, action, null), reload));
  }
  for (const action of ["postpone", "set_reminder"]) {
    row.appendChild(button(PROACTIVE_LABELS[action], () => proactiveTime(row, item, action, path, reload)));
  }
  return row;
}

// 延后与提醒需要本人选定具体时间，按浏览器时区解释。
function proactiveTime(row, item, action, path, reload, suggested) {
  const input = el("input");
  input.type = "datetime-local";
  if (suggested) input.value = new Date(suggested * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const status = el("p", "按 " + timezone() + " 解释。", "meta");
  let request = null;
  const confirm = button("确认" + PROACTIVE_LABELS[action], async () => {
    const epoch = entry.epoch;
    try {
      request = request || proactiveRequest(item, action, requestTime(input));
      confirm.disabled = input.disabled = true;
      await entryApi("POST", path, { json: request });
      if (!isStale(epoch)) { status.textContent = "已保存"; await reload(); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      status.textContent = error.message;
      if (error.status && error.status < 500) { request = null; input.disabled = false; }
      confirm.disabled = false;
    }
  }, "primary");
  row.append(input, confirm, status);
}

function proactiveCard(draft) {
  const node = card("业务跟进"), list = el("ul"), status = el("p", "", "meta");
  node.append(list, status);
  const load = () => loadInto(list, status, "/v1/proactive", body => body.items || [],
    item => proactiveRow(item, load));
  if (draft) proactiveDraft(node, draft, load); else load();
  return node;
}

// 管家提出的处理草稿：按持久记录核对版本后，由本人确认同一动作。
async function proactiveDraft(node, draft, reload) {
  const epoch = entry.epoch;
  try {
    const item = await entryApi("GET", "/v1/proactive/" + encodeURIComponent(draft.id));
    if (isStale(epoch)) return;
    node.appendChild(el("p", item.title));
    if (item.version !== draft.version) { node.appendChild(el("p", "这项跟进已有更新，请按最新状态选择。", "meta")); reload(); return; }
    const row = el("div", null, "row"), path = "/v1/proactive/" + encodeURIComponent(item.id) + "/actions";
    if (["postpone", "set_reminder"].includes(draft.action)) proactiveTime(row, item, draft.action, path, reload, draft.due_at);
    else row.append(...frozenAction("确认" + PROACTIVE_LABELS[draft.action], "POST", path,
      proactiveRequest(item, draft.action, null), async () => { row.replaceChildren(el("p", "已处理")); }, "primary"));
    node.appendChild(row);
  } catch (error) {
    if (!isStale(epoch)) node.appendChild(el("p", "读取失败：" + error.message, "meta"));
  }
}

// ---------- 执行安排（/v1/task-definitions） ----------

function definitionLines(draft) {
  const r = draft.request, trigger = r.trigger, display = draft.display || {};
  const when = trigger.kind === "time" ? ({ once: "执行一次", daily: "每天", weekly: "每周" }[trigger.repeat] + " · 首次 " + trigger.at)
    : "事件触发：" + (display.event_subject || trigger.event_type);
  return [r.title, r.template.text, when, "时区：" + r.timezone,
    "最多执行 " + r.max_executions + " 次；累计模型额度 " + r.max_tokens + " tokens；授权截止 " + r.valid_until];
}

function definitionCard(draft) {
  const node = card(draft ? "请确认执行安排" : "你的执行安排"), list = el("ul"), status = el("p", "", "meta");
  if (draft) {
    for (const line of definitionLines(draft)) node.appendChild(el("p", line));
    // 与功能页相同：原样提交服务端核对过的请求（含其幂等键），只加本人确认
    node.append(...frozenAction("确认启用安排", "POST", "/v1/task-definitions",
      { ...JSON.parse(JSON.stringify(draft.request)), confirmed: true },
      async record => { node.appendChild(el("p", "已启用安排：" + record.title)); }, "primary"));
    return node;
  }
  node.append(list, status);
  const load = () => loadInto(list, status, "/v1/task-definitions?limit=50", body => body.items || [],
    record => definitionRow(record, load));
  load();
  return node;
}

function definitionRow(record, reload) {
  const row = el("li");
  row.appendChild(el("span", record.title + " · " + (record.cancelled ? "已取消" : record.enabled ? "已启用" : "已暂停") + " "));
  if (record.cancelled) return row;
  const path = action => "/v1/task-definitions/" + encodeURIComponent(record.definition_id) + "/" + action;
  const body = stop => ({ request_id: requestId(), expected_version: record.state_version, stop_running: stop });
  if (record.enabled) row.append(...frozenAction("暂停", "POST", path("pause"), body(false), reload));
  else row.append(...frozenAction("恢复", "POST", path("resume"), body(false), reload));
  row.append(...frozenAction("取消安排", "POST", path("cancel"), body(true), reload));
  return row;
}

// ---------- 企业连接（/v1/me/connections） ----------

function connectionsCard() {
  const node = card("企业连接"), list = el("ul"), status = el("p", "", "meta");
  node.append(el("p", "只读范围由企业应用与本人同意共同决定；同意与撤销都由你本人点击。", "meta"), list, status);
  const load = () => loadInto(list, status, "/v1/me/connections", body => body.connections || [],
    record => connectionRow(record, load));
  load();
  return node;
}

function connectionRow(record, reload) {
  const row = el("li"), cid = encodeURIComponent(record.id);
  row.appendChild(el("span", record.name + " · " + (record.status === "active" ? "已配置" : "尚未启用") + " "));
  if (record.status === "active") {
    // 绑定要跳转到企业微信官方授权页完成，只能本人在原生页面操作
    row.appendChild(button("绑定我的企业微信", () => connectionBind(cid, row)));
    if ((record.operations || []).includes("self_profile.read")) {
      row.append(...frozenAction("同意读取本人资料（1小时）", "POST", "/v1/me/connections/" + cid + "/consents",
        { request_id: requestId(), operations: ["self_profile.read"], expires_at: Date.now() / 1000 + 3600,
          purpose: "本人基础资料核对" }, reload));
    }
  }
  for (const consent of record.consents || []) {
    if (consent.status !== "active") continue;
    row.appendChild(el("span", " 已同意：" + consent.purpose + " "));
    row.append(...frozenAction("撤销这项授权", "POST", "/v1/me/connections/" + cid + "/consents/" +
      encodeURIComponent(consent.consent_id) + "/revoke",
      { request_id: requestId(), expected_version: consent.version }, reload));
  }
  return row;
}

async function connectionBind(cid, row) {
  const epoch = entry.epoch;
  try {
    const result = await entryApi("POST", "/v1/me/connections/" + cid + "/binding/start", { json: {} });
    if (isStale(epoch)) return;
    const target = new URL(result.authorization_url);
    if (target.origin !== "https://open.weixin.qq.com") throw new Error("授权地址无效");
    location.assign(target.href);
  } catch (error) {
    if (!isStale(epoch)) row.appendChild(el("p", error.message, "meta"));
  }
}
