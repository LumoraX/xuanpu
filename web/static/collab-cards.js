// 共享事项、工作交接与组织审批的对话内卡片。请求体、幂等键与版本号与功能页一致。
// 交接对象、资料版本与审批决定都由本人在卡片里选择并点击；上传与下载核验固定成果仍在「更多」。
"use strict";

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value)));
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

function field(host, label, value, options) {
  const wrap = el("label", label + " ");
  const input = el(options ? "select" : "input");
  if (options) for (const [key, text] of options) { const o = el("option", text); o.value = key; input.appendChild(o); }
  input.value = value || "";
  wrap.appendChild(input);
  host.appendChild(wrap);
  return input;
}

async function allPages(path) {
  const page = await entryApi("GET", path + (path.includes("?") ? "&" : "?") + "limit=100");
  return page.items || [];
}

// ---------- 共享事项（/v1/shared-matters） ----------

function sharedCard(draft, proposalId) {
  const node = card(draft ? "请确认新建共享事项" : "共享事项");
  if (draft) { sharedDraft(node, draft, proposalId); return node; }
  const list = el("ul"), status = el("p", "", "meta");
  node.append(list, status, el("p", "邀请成员与发布资料请在「更多」的共享事项里完成。", "meta"));
  loadInto(list, status, "/v1/shared-matters?limit=50", body => body.items || [],
    item => el("li", item.title + " · " + (item.status === "active" ? "进行中" : item.status)));
  return node;
}

// 与功能页同一幂等键：草稿标识加名称与正文的摘要，跨页重复确认只建一次。
async function sharedDraft(node, draft, proposalId) {
  const values = { title: draft.title || "", content: draft.content || "" };
  node.append(el("p", "名称：" + values.title), el("p", "正文：" + values.content),
    el("p", "只创建共享事项；后续由你明确邀请具体员工。", "meta"));
  const epoch = entry.epoch;
  const suffix = (await sha256Hex(values)).slice(0, 32);
  if (isStale(epoch)) return;
  const id = proposalId ? proposalId + ":shared:" + suffix : requestId();
  node.append(...frozenAction("确认新建", "POST", "/v1/shared-matters", { request_id: id, confirmed: true, ...values },
    async result => { node.appendChild(el("p", "已新建：" + (result.title || values.title))); }, "primary"));
}

// ---------- 工作交接（/v1/handoffs） ----------

const HANDOFF_SIMPLE = [["accept", "接收这项工作"], ["start", "开始处理"], ["cancel", "取消尚未接收的工作"],
  ["confirm_cancel", "确认取消"], ["reopen", "确认重新办理"]];
const HANDOFF_REASON = [["return", "退回工作"], ["request_information", "请求补充"], ["request_cancel", "请求取消"],
  ["dispute", "对本次交付提出争议"]];
const HANDOFF_NATIVE = ["transfer", "provide_information", "publish_result", "deliver", "submit_approval"];

function collaborationCard(draft, proposalId) {
  const node = card(draft ? "请核对交接" : "工作交接与审批");
  if (draft) { handoffDraft(node, draft, proposalId); return node; }
  const list = el("ul"), status = el("p", "", "meta"), approvals = el("ul"), approvalStatus = el("p", "", "meta");
  node.append(el("h4", "我收到的交接"), list, status, el("h4", "我的组织审批"), approvals, approvalStatus);
  const load = () => {
    loadInto(list, status, "/v1/handoffs?view=received&limit=50", body => body.items || [],
      item => detailRow(item.title, "/v1/handoffs/" + encodeURIComponent(item.id), handoffActions, load));
    loadInto(approvals, approvalStatus, "/v1/approval-requests?limit=50", body => body.items || [],
      item => detailRow("审批 · 第 " + (item.current_step + 1) + "/" + item.steps.length + " 步",
        "/v1/approval-requests/" + encodeURIComponent(item.id), approvalActions, load));
  };
  load();
  return node;
}

// 列表行：点开后读取详情（含当前可做的动作与版本），再给出动作。
function detailRow(title, path, render, reload) {
  const row = el("li");
  row.append(el("span", title + " "), button("查看与处理", async () => {
    const epoch = entry.epoch;
    try {
      const record = await entryApi("GET", path);
      if (!isStale(epoch)) { row.replaceChildren(el("span", title + " ")); render(row, record, path, reload); }
    } catch (error) {
      if (!isStale(epoch)) row.appendChild(el("p", error.message, "meta"));
    }
  }));
  return row;
}

function handoffActions(row, record, path, reload) {
  row.appendChild(el("p", record.summary || ""));
  const actions = new Set(record.allowed_actions || []);
  const body = (extra = {}) => ({ request_id: requestId(), confirmed: true, expected_version: record.version, ...extra });
  for (const [action, label] of HANDOFF_SIMPLE) {
    if (actions.has(action)) row.append(...frozenAction(label, "POST", path + "/" + action.replaceAll("_", "-"), body(), reload));
  }
  if (actions.has("sign_off")) {
    row.append(...frozenAction("签收本次固定交付", "POST", path + "/sign-off",
      body({ delivery_version: record.delivery_version }), reload));
  }
  for (const [action, label] of HANDOFF_REASON) {
    if (actions.has(action)) reasonAction(row, label, path + "/" + action.replaceAll("_", "-"), body, true, reload);
  }
  if (HANDOFF_NATIVE.some(a => actions.has(a))) {
    const link = el("a", "转交、补充资料或提交成果（需要选择文件）请在「更多」里完成");
    link.href = "/workspace/functions";
    row.appendChild(link);
  }
}

// 需要说明原因的动作：本人填写后点击确认；同一张卡重试复用同一请求。
function reasonAction(row, label, path, body, required, reload) {
  const reason = el("textarea");
  reason.rows = 2;
  let request = null;
  const status = el("p", "", "meta");
  const go = button(label, async () => {
    const epoch = entry.epoch;
    if (required && !reason.value.trim()) { status.textContent = "请先说明原因。"; return; }
    request = request || body({ reason: reason.value });
    go.disabled = true;
    try {
      await entryApi("POST", path, { json: request });
      if (!isStale(epoch)) { status.textContent = "已提交"; await reload(); refreshOverview().catch(report); }
    } catch (error) {
      if (isStale(epoch)) return;
      status.textContent = error.message;
      if (error.status && error.status < 500) request = null;
      go.disabled = false;
    }
  });
  row.append(reason, go, status);
}

// ---------- 组织审批（/v1/approval-requests） ----------

const APPROVAL_LABELS = { approve: "批准本次固定成果", reject: "驳回", return: "退回补充", withdraw: "撤销本人申请" };

function approvalActions(row, record, path, reload) {
  row.appendChild(el("p", "只针对当前固定成果与当前步骤；下载核验成果请在「更多」里完成。", "meta"));
  const body = (extra = {}) => ({ request_id: requestId(), confirmed: true, expected_version: record.version, ...extra });
  for (const action of record.allowed_actions || []) {
    if (APPROVAL_LABELS[action]) reasonAction(row, APPROVAL_LABELS[action], path + "/" + action, body, false, reload);
  }
  for (const d of record.decisions || []) row.appendChild(el("p", "已记录决定 · 第" + (d.step_index + 1) + "步", "meta"));
}

// ---------- 管家起草的交接 ----------

async function handoffDraft(node, draft, proposalId) {
  const epoch = entry.epoch;
  try {
    const [people, matters, policies] = await Promise.all([allPages("/v1/organization/people"),
      allPages("/v1/shared-matters"), allPages("/v1/approval-policies")]);
    if (isStale(epoch)) return;
    const own = matters.filter(m => m.owner_id === entry.me.user_id && m.status === "active");
    const choices = people.filter(p => p.user_id !== entry.me.user_id).map(p => [p.user_id, p.display_name]);
    if (!own.length || !choices.length) {
      node.appendChild(el("p", own.length ? "还没有可接收的同事。" : "请先创建一项由你负责的共享事项。", "meta"));
      return;
    }
    handoffForm(node, draft, proposalId, { own, choices, policies });
  } catch (error) {
    if (!isStale(epoch)) node.appendChild(el("p", "读取失败：" + error.message, "meta"));
  }
}

function handoffForm(node, draft, proposalId, data) {
  const form = el("div");
  const source = field(form, "来源事项", data.own[0].id, data.own.map(m => [m.id, m.title]));
  const person = field(form, "接收员工", data.choices[0][0], data.choices);
  const policy = field(form, "成果审批规则", "", [["", "无需组织审批"], ...data.policies.map(p => [p.id, p.name])]);
  const title = field(form, "工作名称", draft.title), summary = field(form, "交接说明", draft.summary);
  const include = el("input");
  include.type = "checkbox";
  const includeLabel = el("label", "包括本次正文固定副本 ");
  includeLabel.appendChild(include);
  const materials = el("div"), status = el("p", "", "meta");
  form.append(includeLabel, materials);
  const pick = () => handoffMaterials(materials, source.value);
  source.addEventListener("change", pick);
  pick();
  const review = button("核对交接", () => handoffReview(node, { source, person, policy, title, summary, include, materials },
    data, proposalId, status), "primary");
  node.append(form, review, status);
}

async function handoffMaterials(host, matterId) {
  const epoch = entry.epoch;
  host.replaceChildren(el("p", "读取资料…", "meta"));
  try {
    const items = await allPages("/v1/shared-matters/" + encodeURIComponent(matterId) + "/materials");
    if (isStale(epoch)) return;
    host.replaceChildren();
    host.boxes = [];
    for (const material of items) for (const revision of material.revisions || []) {
      const box = el("input");
      box.type = "checkbox";
      box.dataset.revision = revision.id;
      host.boxes.push(box);
      const label = el("label", material.name + " · 固定版本 " + revision.revision + " ");
      label.appendChild(box);
      host.appendChild(label);
    }
  } catch (error) {
    if (!isStale(epoch)) host.replaceChildren(el("p", error.message, "meta"));
  }
}

// 与功能页同序同键的请求体；草稿来的交接以草稿标识加请求摘要作幂等键。
async function handoffReview(node, f, data, proposalId, status) {
  const epoch = entry.epoch;
  try {
    const matter = await entryApi("GET", "/v1/shared-matters/" + encodeURIComponent(f.source.value));
    if (isStale(epoch)) return;
    const picked = (f.materials.boxes || []).filter(b => b.checked).map(b => b.dataset.revision);
    if (picked.length > 8) { status.textContent = "每次最多选择8个固定版本。"; return; }
    const body = { confirmed: true, expected_version: matter.version, shared_id: matter.id, target_kind: "user",
      target_id: f.person.value, title: f.title.value, summary: f.summary.value, include_content: Boolean(f.include.checked),
      material_revision_ids: picked, ...(f.policy.value ? { policy_id: f.policy.value } : {}) };
    const key = proposalId ? proposalId + ":" + await sha256Hex(body) : requestId();
    if (isStale(epoch)) return;
    body.request_id = "handoff:" + key;
    const person = data.choices.find(p => p[0] === f.person.value);
    node.append(el("p", "接收人：" + (person ? person[1] : f.person.value) + "；资料 " + picked.length + " 个版本；接收人需本人接收。"),
      ...frozenAction("确认提交交接", "POST", "/v1/handoffs", body,
        async result => { node.appendChild(el("p", "已提交交接：" + (result.title || body.title))); }, "primary"));
  } catch (error) {
    if (!isStale(epoch)) status.textContent = error.message;
  }
}
