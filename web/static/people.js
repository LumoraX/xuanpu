// 纯个人空间「人物与关系」控制台模块：名片新建/编辑、头像与照片附件、手动建关系、软合并/撤销合并、1–2 跳关系网络与对话确认卡。
// 不依赖模型也能直接增删改查；所有异步响应核对 conn.epoch 与代际，登出或切身份立即清空。
"use strict";

const peopleState = { generation: 0, selectedId: null, items: [], trashed: false, lastMerge: null };

function peopleLive(epoch, generation) {
  return epoch === conn.epoch && generation === peopleState.generation;
}
function peopleNotice(text) {
  const el = $("people-feedback");
  if (el) el.textContent = text;
}
function peopleEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function peopleBtn(label, handler, className) {
  const btn = peopleEl("button", label, className);
  btn.type = "button";
  btn.addEventListener("click", handler);
  return btn;
}
function peopleReqId(prefix) {
  return prefix + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
}
function formatBirth(birth) {
  if (!birth) return "";
  const parts = [];
  if (birth.year) parts.push(birth.year + "年");
  if (birth.month) parts.push(birth.month + "月");
  if (birth.day) parts.push(birth.day + "日");
  return (birth.calendar === "lunar" ? "农历 " : "") + parts.join("");
}

function resetPeople() {
  peopleState.generation += 1;
  peopleState.selectedId = null;
  peopleState.items = [];
  peopleState.trashed = false;
  peopleState.lastMerge = null;
  if ($("people-list")) $("people-list").replaceChildren();
  if ($("people-detail")) $("people-detail").replaceChildren();
  peopleNotice("");
  if ($("people-panel")) $("people-panel").classList.add("hidden");
}

async function openPeople(focusPersonId = null) {
  const fnTab = document.querySelector('[data-tab="functions"]');
  if (fnTab && !fnTab.classList.contains("active")) fnTab.click();
  const epoch = conn.epoch, generation = ++peopleState.generation;
  $("people-panel").classList.remove("hidden");
  $("people-panel").scrollIntoView?.({ block: "start" });
  await loadPeopleList(epoch, generation);
  if (focusPersonId && peopleLive(epoch, generation)) {
    await loadPersonDetail(focusPersonId, epoch, generation);
  }
}

async function loadPeopleList(epoch, generation) {
  const q = $("people-query").value.trim();
  const month = $("people-birth-month").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (month) params.set("birth_month", month);
  if (peopleState.trashed) params.set("trashed", "true");
  peopleNotice("正在读取本人人物档案…");
  try {
    const body = await api("GET", "/v1/people" + (params.toString() ? "?" + params : ""));
    if (!peopleLive(epoch, generation)) return;
    peopleState.items = Array.isArray(body.items) ? body.items : [];
    renderPeopleList(epoch, generation);
    peopleNotice("仅本人可见；共 " + peopleState.items.length + " 条人物记录。");
  } catch (error) {
    if (peopleLive(epoch, generation)) peopleNotice(error.message);
  }
}

function renderPeopleList(epoch, generation) {
  const host = $("people-list");
  host.replaceChildren();
  if (peopleState.lastMerge) {
    const m = peopleState.lastMerge;
    const banner = peopleEl("div", null, "function-card");
    banner.append(
      peopleEl("p", "刚将「" + m.sourceName + "」合并到「" + m.targetName + "」。如误合并可立即撤销：", "meta"),
      peopleBtn("撤销合并", () => undoLastMerge(epoch, generation), "ghost")
    );
    host.append(banner);
  }
  if (!peopleState.items.length) {
    host.append(peopleEl("p", peopleState.trashed ? "回收站为空。" : "还没有人物记录，点击上方「新建人物」或直接在对话中记录。", "status"));
    return;
  }
  for (const item of peopleState.items) {
    const p = item.profile || {};
    const card = peopleEl("article", null, "function-card");
    const head = peopleEl("div", null, "row");
    if (item.has_avatar) {
      const img = document.createElement("img");
      img.className = "avatar";
      img.alt = p.name || "";
      img.src = "/v1/people/" + encodeURIComponent(item.id) + "/avatar?t=" + encodeURIComponent(String(item.avatar_updated_at || item.version));
      head.append(img);
    }
    head.append(peopleEl("h4", p.name + (p.organization ? " · " + p.organization : "")));
    card.append(head);
    const meta = [p.title, p.city, formatBirth(p.birth), (p.tags || []).join(" / ")].filter(Boolean).join(" · ");
    if (meta) card.append(peopleEl("p", meta, "meta"));
    if (p.summary) card.append(peopleEl("p", p.summary));
    const actions = peopleEl("div", null, "row");
    if (peopleState.trashed) {
      actions.append(peopleBtn("恢复", () => restorePersonRecord(item, epoch, generation)));
    } else {
      actions.append(
        peopleBtn("查看详情与关系", () => loadPersonDetail(item.id, epoch, generation)),
        peopleBtn("编辑", () => showPersonForm(item, epoch, generation), "ghost"),
        peopleBtn("移入回收站", () => trashPersonRecord(item, epoch, generation), "ghost")
      );
    }
    card.append(actions);
    host.append(card);
  }
}

async function undoLastMerge(epoch, generation) {
  const m = peopleState.lastMerge;
  if (!m) return;
  try {
    const res = await api("POST", "/v1/people/" + encodeURIComponent(m.sourceId) + "/unmerge", {
      json: { source_version: m.sourceVersion, target_version: m.targetVersion, request_id: peopleReqId("unmerge") },
    });
    if (!peopleLive(epoch, generation)) return;
    peopleState.lastMerge = null;
    peopleNotice("已撤销合并，恢复「" + res.source.name + "」与「" + res.target.name + "」独立档案。");
    await loadPeopleList(epoch, generation);
    await loadPersonDetail(res.source.id, epoch, generation);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

async function trashPersonRecord(item, epoch, generation) {
  try {
    await api("DELETE", "/v1/people/" + encodeURIComponent(item.id) + "?version=" + item.version);
    if (!peopleLive(epoch, generation)) return;
    if (peopleState.selectedId === item.id) { peopleState.selectedId = null; $("people-detail").replaceChildren(); }
    peopleNotice("已移入回收站。");
    await loadPeopleList(epoch, generation);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

async function restorePersonRecord(item, epoch, generation) {
  try {
    await api("POST", "/v1/people/" + encodeURIComponent(item.id) + "/restore?version=" + item.version);
    if (!peopleLive(epoch, generation)) return;
    peopleNotice("已恢复人物档案。");
    await loadPeopleList(epoch, generation);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

function parseExtraText(text) {
  const extra = {};
  for (const rawLine of String(text || "").split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const idx = line.indexOf("=") >= 0 ? line.indexOf("=") : line.indexOf("：");
    if (idx <= 0) continue;
    const k = line.slice(0, idx).trim();
    const v = line.slice(idx + 1).trim();
    if (k && v) extra[k] = v;
  }
  return extra;
}

function formatExtraText(extra) {
  return Object.entries(extra || {})
    .map(([k, v]) => k + "=" + (Array.isArray(v) ? v.join(",") : String(v)))
    .join("\n");
}

function parsePeopleFormProfile(form, preservedContacts, attachmentFileIds) {
  const val = (name) => form.elements[name].value.trim();
  const splitList = (name) => val(name) ? val(name).split(/[,，、]+/).map((s) => s.trim()).filter(Boolean) : [];
  const year = val("birth_year") ? Number(val("birth_year")) : null;
  const month = val("birth_month") ? Number(val("birth_month")) : null;
  const day = val("birth_day") ? Number(val("birth_day")) : null;
  const birth = (year || month || day) ? { year, month, day, calendar: val("birth_calendar") || "solar" } : null;
  const contacts = [...preservedContacts];
  if (val("phone")) contacts.push({ kind: "phone", value: val("phone"), label: "手机", primary: true });
  if (val("wechat")) contacts.push({ kind: "wechat", value: val("wechat"), label: "微信", primary: false });
  if (val("email")) contacts.push({ kind: "email", value: val("email"), label: "邮箱", primary: false });
  return {
    name: val("name"),
    aliases: splitList("aliases"),
    gender: val("gender") || "unknown",
    birth,
    organization: val("organization"),
    department: val("department"),
    title: val("title"),
    city: val("city"),
    contacts,
    tags: splitList("tags"),
    summary: val("summary"),
    attachment_file_ids: attachmentFileIds,
    extra: parseExtraText(val("extra")),
  };
}

async function showPersonForm(existing, epoch, generation) {
  const host = $("people-detail");
  const isEdit = Boolean(existing);
  const p = existing?.profile || {};
  host.replaceChildren(peopleEl("h3", isEdit ? "编辑人物名片：" + p.name : "新建人物名片"));
  const form = peopleEl("form", null, "function-card");

  const firstContact = (kind) => (p.contacts || []).find((c) => c.kind === kind)?.value || "";
  const preservedContacts = (p.contacts || []).filter((c) => !["phone", "wechat", "email"].includes(c.kind));
  const selectedFiles = new Set(p.attachment_file_ids || []);

  const fields = [
    ["name", "姓名（必填）", "text", true, p.name || ""],
    ["aliases", "别名/称呼（逗号分隔）", "text", false, (p.aliases || []).join("，")],
    ["organization", "公司/机构", "text", false, p.organization || ""],
    ["department", "部门", "text", false, p.department || ""],
    ["title", "职位", "text", false, p.title || ""],
    ["city", "常驻城市", "text", false, p.city || ""],
    ["birth_year", "出生年份（如 1988）", "number", false, p.birth?.year ?? ""],
    ["birth_month", "出生月（1-12）", "number", false, p.birth?.month ?? ""],
    ["birth_day", "出生日（1-31）", "number", false, p.birth?.day ?? ""],
    ["phone", "手机号", "text", false, firstContact("phone")],
    ["wechat", "微信号", "text", false, firstContact("wechat")],
    ["email", "邮箱", "email", false, firstContact("email")],
    ["tags", "标签（逗号分隔）", "text", false, (p.tags || []).join("，")],
    ["summary", "人物简介/备注", "text", false, p.summary || ""],
  ];
  for (const [name, label, type, req, initial] of fields) {
    const wrap = peopleEl("label", label + " ");
    const input = document.createElement("input");
    input.name = name; input.type = type; input.value = String(initial);
    if (req) input.required = true;
    wrap.append(input); form.append(wrap);
  }
  const calLabel = peopleEl("label", "历法 ");
  const calSel = document.createElement("select"); calSel.name = "birth_calendar";
  calSel.append(new Option("公历", "solar"), new Option("农历", "lunar"));
  calSel.value = p.birth?.calendar || "solar";
  calLabel.append(calSel); form.append(calLabel);

  const gLabel = peopleEl("label", "性别 ");
  const gSel = document.createElement("select"); gSel.name = "gender";
  gSel.append(new Option("未填", "unknown"), new Option("男", "male"), new Option("女", "female"), new Option("其他", "other"));
  gSel.value = p.gender || "unknown";
  gLabel.append(gSel); form.append(gLabel);

  const extraLabel = peopleEl("label", "更多扩展信息（每行 键=值，如 母校=浙大） ");
  const extraArea = document.createElement("textarea");
  extraArea.name = "extra"; extraArea.rows = 3; extraArea.value = formatExtraText(p.extra);
  extraLabel.append(extraArea); form.append(extraLabel);

  // 可选关联本人个人文件（名片原图、合影等）
  const fileBox = peopleEl("div", null, "row");
  fileBox.append(peopleEl("span", "关联照片/名片原图：", "meta"));
  const fileSel = document.createElement("select");
  fileSel.append(new Option("从我的文件添加…", ""));
  const fileTags = peopleEl("span", "", "meta");
  const renderFileTags = () => {
    fileTags.replaceChildren();
    for (const fid of selectedFiles) {
      const chip = peopleEl("span", fid + " ", "attachment-chip");
      chip.append(peopleBtn("×", () => { selectedFiles.delete(fid); renderFileTags(); }, "ghost"));
      fileTags.append(chip);
    }
  };
  renderFileTags();
  fileSel.addEventListener("change", () => {
    if (fileSel.value && selectedFiles.size < 10) { selectedFiles.add(fileSel.value); renderFileTags(); }
    fileSel.value = "";
  });
  fileBox.append(fileSel, fileTags);
  form.append(fileBox);

  const actions = peopleEl("div", null, "row");
  const submit = peopleEl("button", isEdit ? "保存修改" : "保存人物"); submit.type = "submit";
  actions.append(submit);
  if (isEdit) actions.append(peopleBtn("取消", () => loadPersonDetail(existing.id, epoch, generation), "ghost"));
  form.append(actions);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const profile = parsePeopleFormProfile(form, preservedContacts, Array.from(selectedFiles));
      const saved = isEdit
        ? await api("PUT", "/v1/people/" + encodeURIComponent(existing.id), { json: { profile, version: existing.version } })
        : await api("POST", "/v1/people", { json: { profile, request_id: peopleReqId("per") } });
      if (!peopleLive(epoch, generation)) return;
      peopleNotice((isEdit ? "已更新：" : "已保存：") + saved.name);
      await loadPeopleList(epoch, generation);
      await loadPersonDetail(saved.id, epoch, generation);
    } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
  });
  host.append(form);

  try {
    const { items: files } = await api("GET", "/v1/workspace/items?kind=file");
    if (!peopleLive(epoch, generation)) return;
    for (const f of files || []) {
      fileSel.append(new Option(String(f.data?.name || f.id), f.id));
    }
  } catch { /* 文件列表加载失败不阻断名片编辑 */ }
}

async function loadPersonDetail(personId, epoch, generation) {
  peopleState.selectedId = personId;
  try {
    const [detail, graph] = await Promise.all([
      api("GET", "/v1/people/" + encodeURIComponent(personId) + "?include_history=true"),
      api("GET", "/v1/people/" + encodeURIComponent(personId) + "/graph?depth=2"),
    ]);
    if (!peopleLive(epoch, generation)) return;
    renderPersonDetail(detail, graph, epoch, generation);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

function renderPersonDetail(detail, graph, epoch, generation) {
  const host = $("people-detail");
  const person = detail.person, p = person.profile || {};
  host.replaceChildren();
  const box = peopleEl("section", null, "function-card");
  const header = peopleEl("div", null, "row");
  header.append(
    peopleEl("h3", p.name + (p.aliases?.length ? "（" + p.aliases.join(" / ") + "）" : "")),
    peopleBtn("编辑名片", () => showPersonForm(person, epoch, generation), "ghost"),
    peopleBtn("合并到其他档案", () => showMergeForm(person, epoch, generation), "ghost")
  );
  box.append(header);

  if (person.has_avatar) {
    const img = document.createElement("img");
    img.className = "avatar"; img.alt = p.name;
    img.src = "/v1/people/" + encodeURIComponent(person.id) + "/avatar?t=" + encodeURIComponent(String(person.avatar_updated_at || person.version));
    box.append(img);
  }
  const avatarRow = peopleEl("div", null, "row");
  const avatarInput = document.createElement("input");
  avatarInput.type = "file"; avatarInput.accept = "image/jpeg,image/png,image/webp"; avatarInput.className = "hidden";
  avatarInput.addEventListener("change", async () => {
    const file = avatarInput.files?.[0]; if (!file) return;
    try {
      await api("PUT", "/v1/people/" + encodeURIComponent(person.id) + "/avatar?version=" + person.version, {
        body: file, headers: { "Content-Type": file.type || "application/octet-stream" },
      });
      if (!peopleLive(epoch, generation)) return;
      peopleNotice("头像已更新。");
      await loadPeopleList(epoch, generation);
      await loadPersonDetail(person.id, epoch, generation);
    } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
  });
  avatarRow.append(avatarInput, peopleBtn("上传头像", () => avatarInput.click(), "ghost"));
  if (person.has_avatar) {
    avatarRow.append(peopleBtn("清除头像", async () => {
      try {
        await api("DELETE", "/v1/people/" + encodeURIComponent(person.id) + "/avatar?version=" + person.version);
        if (!peopleLive(epoch, generation)) return;
        await loadPeopleList(epoch, generation);
        await loadPersonDetail(person.id, epoch, generation);
      } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
    }, "ghost"));
  }
  box.append(avatarRow);

  const infoLines = [
    ["公司/职位", [p.organization, p.department, p.title].filter(Boolean).join(" · ")],
    ["常驻城市", p.city],
    ["出生信息", formatBirth(p.birth)],
    ["联系方式", (p.contacts || []).map((c) => (c.label || c.kind) + ": " + c.value).join("；")],
    ["标签", (p.tags || []).join(" / ")],
    ["备注", p.summary],
  ];
  for (const [k, v] of infoLines) if (v) box.append(peopleEl("p", k + "：" + v));
  const extraEntries = Object.entries(p.extra || {});
  if (extraEntries.length) {
    box.append(peopleEl("p", "更多信息：" + extraEntries.map(([k, v]) => k + "=" + (Array.isArray(v) ? v.join(",") : String(v))).join("；"), "meta"));
  }
  if ((p.attachment_file_ids || []).length) {
    box.append(peopleEl("p", "关联照片/名片文件：" + p.attachment_file_ids.join("、"), "meta"));
  }

  const relHead = peopleEl("div", null, "row");
  relHead.append(
    peopleEl("h4", "关系（" + (detail.relations || []).length + "）"),
    peopleBtn("添加关系", () => showAddRelationForm(person, relFormHost, epoch, generation), "ghost")
  );
  const relFormHost = peopleEl("div");
  box.append(relHead, relFormHost);

  const nameMap = new Map(peopleState.items.map((i) => [i.id, i.name]));
  for (const node of graph?.nodes || []) {
    if (node.kind === "person" && node.person) nameMap.set(node.id, node.person.name);
  }
  for (const rel of detail.relations || []) {
    const peerId = rel.target_kind === "matter" ? rel.target_id : (rel.from_person_id === person.id ? rel.target_id : rel.from_person_id);
    const peerLabel = rel.target_kind === "matter" ? "事项:" + peerId : (nameMap.get(peerId) || peerId);
    const label = rel.relation_type + " → " + peerLabel + (rel.role ? "（" + rel.role + "）" : "") + (rel.valid_from ? " [" + rel.valid_from + (rel.valid_to ? "~" + rel.valid_to : "起") + "]" : "");
    const row = peopleEl("div", null, "row");
    row.append(peopleEl("span", label));
    if (rel.target_kind === "person") {
      row.append(peopleBtn("查看对方", () => loadPersonDetail(peerId, epoch, generation), "ghost"));
    }
    row.append(peopleBtn("删除", async () => {
      try {
        await api("DELETE", "/v1/people/relations/" + encodeURIComponent(rel.id) + "?version=" + rel.version);
        if (peopleLive(epoch, generation)) await loadPersonDetail(person.id, epoch, generation);
      } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
    }, "ghost"));
    box.append(row);
  }

  const graphNodes = (graph?.nodes || []).filter((n) => n.hop > 0);
  if (graphNodes.length) {
    const summary = graphNodes.map((n) => (n.kind === "person" ? n.person.name : "事项:" + n.id) + " (" + n.hop + "跳)").join("、");
    box.append(peopleEl("p", "关联网络：" + summary, "meta"));
  }

  box.append(peopleEl("h4", "原始互动记录（" + (detail.notes || []).length + "）"));
  for (const note of detail.notes || []) {
    box.append(peopleEl("p", note.content + (note.attachment_file_ids?.length ? " [附件:" + note.attachment_file_ids.length + "]" : ""), "meta"));
  }
  const noteRow = peopleEl("div", null, "row");
  const noteInput = document.createElement("input");
  noteInput.type = "text"; noteInput.placeholder = "补充一条互动或背景记录…"; noteInput.maxLength = 2000;
  noteRow.append(noteInput, peopleBtn("记下", async () => {
    const text = noteInput.value.trim(); if (!text) return;
    try {
      await api("POST", "/v1/people/notes", {
        json: { data: { content: text, person_ids: [person.id] }, request_id: peopleReqId("pnote") },
      });
      if (peopleLive(epoch, generation)) { noteInput.value = ""; await loadPersonDetail(person.id, epoch, generation); }
    } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
  }));
  box.append(noteRow);
  host.append(box);
}

async function showAddRelationForm(person, container, epoch, generation) {
  container.replaceChildren(peopleEl("p", "正在加载可选人物与事项…", "meta"));
  try {
    const [pRes, mRes] = await Promise.all([
      api("GET", "/v1/people?limit=100"),
      api("GET", "/v1/workspace/items?kind=matter"),
    ]);
    if (!peopleLive(epoch, generation)) return;
    const otherPeople = (pRes.items || []).filter((x) => x.id !== person.id);
    const matters = mRes.items || [];
    const form = peopleEl("form", null, "function-card");
    const kindSel = document.createElement("select");
    kindSel.append(new Option("关联人物", "person"), new Option("关联个人事项（项目）", "matter"));
    const targetSel = document.createElement("select");
    const fillTargets = () => {
      targetSel.replaceChildren();
      const list = kindSel.value === "person" ? otherPeople : matters;
      for (const item of list) {
        const text = kindSel.value === "person" ? item.name + (item.organization ? " · " + item.organization : "") : (item.data?.title || item.id);
        targetSel.append(new Option(text, item.id));
      }
      if (!list.length) targetSel.append(new Option(kindSel.value === "person" ? "暂无其他人物" : "暂无活动事项", ""));
    };
    fillTargets();
    kindSel.addEventListener("change", fillTargets);

    const typeInput = document.createElement("input");
    typeInput.type = "text"; typeInput.required = true;
    typeInput.placeholder = "关系类型（如 同事/介绍人/参与项目/合作方）";
    const roleInput = document.createElement("input");
    roleInput.type = "text"; roleInput.placeholder = "角色说明（可选，如 甲方决策人）";
    const fromInput = document.createElement("input");
    fromInput.type = "text"; fromInput.placeholder = "开始日期（可选 YYYY-MM）";
    const submit = peopleEl("button", "保存关系"); submit.type = "submit";

    form.append(kindSel, targetSel, typeInput, roleInput, fromInput, submit, peopleBtn("收起", () => container.replaceChildren(), "ghost"));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!targetSel.value) { peopleNotice("请先选择关联目标。"); return; }
      try {
        await api("POST", "/v1/people/relations", {
          json: {
            request_id: peopleReqId("rel"),
            data: {
              from_person_id: person.id,
              target_kind: kindSel.value,
              target_id: targetSel.value,
              relation_type: typeInput.value.trim(),
              role: roleInput.value.trim(),
              valid_from: fromInput.value.trim() || null,
            },
          },
        });
        if (!peopleLive(epoch, generation)) return;
        peopleNotice("关系已保存。");
        await loadPersonDetail(person.id, epoch, generation);
      } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
    });
    container.replaceChildren(form);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

async function showMergeForm(source, epoch, generation) {
  const host = $("people-detail");
  try {
    const { items } = await api("GET", "/v1/people?limit=100");
    if (!peopleLive(epoch, generation)) return;
    const targets = (items || []).filter((x) => x.id !== source.id);
    if (!targets.length) { peopleNotice("没有其他可合并的人物档案。"); return; }
    const box = peopleEl("form", null, "function-card");
    box.append(peopleEl("h4", "将「" + source.name + "」合并到已有主档案"));
    box.append(peopleEl("p", "合并会保留原名作为别名、合并联系方式/标签/关系与笔记，且支持撤销。", "meta"));
    const sel = document.createElement("select");
    for (const t of targets) {
      sel.append(new Option(t.name + (t.organization ? " · " + t.organization : ""), t.id));
    }
    const submit = peopleEl("button", "确认合并"); submit.type = "submit";
    box.append(sel, submit, peopleBtn("取消", () => loadPersonDetail(source.id, epoch, generation), "ghost"));
    box.addEventListener("submit", async (event) => {
      event.preventDefault();
      const target = targets.find((x) => x.id === sel.value);
      if (!target) return;
      try {
        const res = await api("POST", "/v1/people/" + encodeURIComponent(source.id) + "/merge", {
          json: {
            target_id: target.id,
            source_version: source.version,
            target_version: target.version,
            request_id: peopleReqId("merge"),
          },
        });
        if (!peopleLive(epoch, generation)) return;
        peopleState.lastMerge = {
          sourceId: res.source.id,
          targetId: res.target.id,
          sourceVersion: res.source.version,
          targetVersion: res.target.version,
          sourceName: source.name,
          targetName: res.target.name,
        };
        peopleNotice("已将「" + source.name + "」合并到「" + res.target.name + "」。");
        await loadPeopleList(epoch, generation);
        await loadPersonDetail(res.target.id, epoch, generation);
      } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
    });
    host.prepend(box);
  } catch (error) { if (peopleLive(epoch, generation)) peopleNotice(error.message); }
}

// 经典控制台主对话里的复合人物确认卡（支持重名候选一键切换为更新已有档案）
function showPeopleDraft(draft, proposalId) {
  const host = $("workspace-draft");
  if (!host) return;
  host.replaceChildren(peopleEl("h3", "请确认人物与关系草稿"));
  const picks = (draft.persons || []).map(() => "");
  (draft.persons || []).forEach((item, index) => {
    const p = item.profile || {};
    const row = peopleEl("div", null, "function-card");
    const desc = [p.name, p.organization, p.title, formatBirth(p.birth)].filter(Boolean).join(" · ");
    row.append(peopleEl("p", (item.action === "update" ? "更新：" : "新建：") + desc));
    const cands = item.similar_candidates || [];
    if (item.action === "create" && cands.length) {
      const label = peopleEl("label", "发现疑似已有人物：", "meta");
      const sel = document.createElement("select");
      sel.append(new Option("作为新人物建档", ""));
      for (const c of cands) {
        sel.append(new Option("更新已有：" + c.name + (c.organization ? " · " + c.organization : ""), c.person_id));
      }
      sel.addEventListener("change", () => { picks[index] = sel.value; });
      label.append(sel);
      row.append(label);
    }
    host.append(row);
  });
  if ((draft.relations || []).length) {
    host.append(peopleEl("p", "将建立 " + draft.relations.length + " 条关系边。", "meta"));
  }
  if (draft.note?.content) {
    host.append(peopleEl("p", "原始留痕：" + draft.note.content.slice(0, 140), "meta"));
  }
  const status = peopleEl("p", "", "meta");
  const actions = peopleEl("div", null, "row");
  actions.append(
    peopleBtn("确认保存", async () => {
      const epoch = conn.epoch;
      try {
        const persons = (draft.persons || []).map((item, i) => {
          const { similar_candidates, ...clean } = item;
          if (picks[i]) {
            const chosen = (similar_candidates || []).find((c) => c.person_id === picks[i]);
            return { ...clean, action: "update", person_id: picks[i], expected_version: chosen?.version ?? null };
          }
          return clean;
        });
        const body = { persons, relations: draft.relations || [], note: draft.note || null };
        const res = await api("POST", "/v1/assistant/proposals/" + encodeURIComponent(proposalId) + "/confirm", {
          json: { data: body },
        });
        if (epoch !== conn.epoch) return;
        host.classList.add("hidden");
        const names = (res.result?.persons || []).map((x) => x.name).join("、") || "人物记录";
        append("assistant", "已保存人物与关系：" + names);
      } catch (error) { if (epoch === conn.epoch) status.textContent = error.message; }
    }),
    peopleBtn("取消", async () => {
      const epoch = conn.epoch;
      try {
        await api("POST", "/v1/assistant/proposals/" + encodeURIComponent(proposalId) + "/cancel", {});
        if (epoch !== conn.epoch) return;
        host.classList.add("hidden");
        append("assistant", "已取消这项人物记录草稿。");
      } catch (error) { if (epoch === conn.epoch) status.textContent = error.message; }
    }, "ghost")
  );
  host.append(actions, status);
  host.classList.remove("hidden");
}

if (typeof $ === "function" && $("open-people")) {
  $("open-people").addEventListener("click", () => openPeople());
  $("close-people").addEventListener("click", () => resetPeople());
  $("people-search").addEventListener("click", () => loadPeopleList(conn.epoch, peopleState.generation));
  $("people-new").addEventListener("click", () => showPersonForm(null, conn.epoch, peopleState.generation));
  $("people-toggle-trash").addEventListener("click", () => {
    peopleState.trashed = !peopleState.trashed;
    $("people-toggle-trash").textContent = peopleState.trashed ? "返回活动列表" : "回收站";
    loadPeopleList(conn.epoch, peopleState.generation);
  });
}
