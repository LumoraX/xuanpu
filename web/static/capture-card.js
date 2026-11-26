// 个人知识图谱「原始记录拆解确认卡」：以原始记录为根，动态展示公司 / 人物 / 项目 / 行动项 / 跨实体关系，支持就地编辑、逐项勾选、「确认保存全部」与「仅存原始记录」。
const KG_RECORD_LABELS = {
  meeting: "会议记录",
  note: "随手记",
  card: "名片记录",
  call: "通话记录",
  other: "原始记录",
};

const KG_RELATION_LABELS = {
  subsidiary_of: "下属公司",
  parent_of: "母公司",
  partner_org: "合作机构",
  client_org: "客户单位",
  works_at: "任职于",
  former_at: "曾任职于",
  advisor_of: "顾问",
  friend: "朋友",
  friend_of: "朋友",
  colleague: "同事",
  introduced_by: "引荐",
  reports_to: "汇报给",
  family: "家人",
  partner: "合作伙伴",
  client: "客户",
  vendor: "供应商",
  supervisor: "上级",
  subordinate: "下属",
  classmate: "同学",
  matter_role: "参与事项",
  org_matter_role: "合作事项",
};

function kgInput(value, placeholder = "", type = "text") {
  const inp = document.createElement("input");
  inp.type = type;
  inp.value = value == null ? "" : String(value);
  if (placeholder) inp.placeholder = placeholder;
  return inp;
}

function kgTextarea(value, placeholder = "", rows = 2) {
  const area = document.createElement("textarea");
  area.rows = rows;
  area.value = value == null ? "" : String(value);
  if (placeholder) area.placeholder = placeholder;
  return area;
}

function kgField(label, input, full = false) {
  const wrap = el("label", null, "pcard-field" + (full ? " full" : ""));
  wrap.append(el("span", label, "pcard-label"), input);
  return wrap;
}

function kgKeepToggle(targetBlock) {
  const keepLabel = el("label", null, "pcard-toggle");
  const keepChk = document.createElement("input");
  keepChk.type = "checkbox";
  keepChk.checked = true;
  keepLabel.append(keepChk, document.createTextNode("保存"));
  keepChk.addEventListener("change", () => targetBlock.classList.toggle("muted", !keepChk.checked));
  return { keepLabel, keepChk };
}

function kgFormatDateInput(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function kgParseDateInput(text, fallbackTs) {
  const v = (text || "").trim();
  if (!v) return fallbackTs ?? null;
  const parsed = Date.parse(v.includes("T") ? v : v + "T12:00:00+08:00");
  return Number.isNaN(parsed) ? (fallbackTs ?? null) : Math.floor(parsed / 1000);
}

function kgFormatDateTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("zh-CN", { hour12: false });
}

function buildRecordSection(rawRec) {
  const rec = rawRec || null;
  if (!rec || !rec.content) return null;
  const sec = el("div", null, "pcard-record-box");
  const kind = rec.record_kind || "note";
  const head = el("div", null, "pcard-title-row");
  head.append(
    el("span", KG_RECORD_LABELS[kind] || "原始记录", "pcard-badge record"),
    el("span", "事实源头（所有拆解实体与关系均溯源至此）", "pcard-sub")
  );
  sec.append(head);

  const titleIn = kgInput(rec.title, kind === "meeting" ? "会议主题" : "记录标题（可选）");
  const locIn = kgInput(rec.location, "地点（可选，如 半岛酒店 201）");
  const dateIn = kgInput(kgFormatDateInput(rec.occurred_at), "日期（YYYY-MM-DD）");
  const contentIn = kgTextarea(rec.content, "原始记录全文", kind === "meeting" ? 4 : 2);
  const grid = el("div", null, "pcard-grid");
  grid.append(
    kgField("标题", titleIn),
    kgField("日期", dateIn),
    kgField("地点", locIn, true),
    kgField("原始全文", contentIn, true)
  );
  const inputs = [titleIn, locIn, dateIn, contentIn];

  let summaryIn = null, decisionsIn = null, questionsIn = null;
  const isMeeting = kind === "meeting" || rec.summary || (rec.decisions || []).length || (rec.open_questions || []).length;
  if (isMeeting) {
    summaryIn = kgTextarea(rec.summary, "会议核心摘要", 2);
    decisionsIn = kgTextarea((rec.decisions || []).join("\n"), "关键结论（每行一条）", 2);
    questionsIn = kgTextarea((rec.open_questions || []).join("\n"), "待确认问题（每行一条）", 2);
    grid.append(
      kgField("纪要摘要", summaryIn, true),
      kgField("关键结论", decisionsIn),
      kgField("待确认问题", questionsIn)
    );
    inputs.push(summaryIn, decisionsIn, questionsIn);
  }
  sec.append(grid);
  const splitLines = (inp) => (inp ? inp.value.split(/\n+/).map((s) => s.trim()).filter(Boolean) : []);
  return {
    node: sec,
    inputs,
    build(rawOnly, droppedRefs) {
      const content = contentIn.value.trim() || rec.content;
      const keep = (arr) => rawOnly ? [] : (arr || []).filter((r) => !droppedRefs.has(r));
      return {
        record_kind: kind,
        title: titleIn.value.trim(),
        location: locIn.value.trim(),
        content,
        occurred_at: kgParseDateInput(dateIn.value, rec.occurred_at),
        end_at: rec.end_at ?? null,
        summary: summaryIn ? summaryIn.value.trim() : (rec.summary || ""),
        decisions: summaryIn ? splitLines(decisionsIn) : (rec.decisions || []),
        open_questions: summaryIn ? splitLines(questionsIn) : (rec.open_questions || []),
        person_refs: keep(rec.person_refs),
        org_refs: keep(rec.org_refs),
        matter_id: rawOnly || droppedRefs.has(rec.matter_id) ? null : (rec.matter_id ?? null),
        matter_refs: keep(rec.matter_refs),
        attachment_file_ids: rec.attachment_file_ids || [],
        source_message_id: rec.source_message_id ?? null,
      };
    },
  };
}

function buildOrgEditors(orgs) {
  if (!orgs.length) return { node: null, editors: [] };
  const sec = el("div", null, "pcard-section");
  sec.append(el("div", "公司 / 组织（" + orgs.length + "）", "pcard-section-title"));
  const editors = [];
  for (const item of orgs) {
    const p = item.profile || {};
    const box = el("div", null, "pcard-matter");
    const head = el("div", null, "pcard-title-row");
    const isUpd = item.action === "update" || item.action === "link";
    const badge = el("span", item.action === "link" ? "关联已有公司" : (item.action === "update" ? "更新已有公司" : "新建公司"), "pcard-badge" + (isUpd ? " update" : ""));
    const { keepLabel, keepChk } = kgKeepToggle(box);
    head.append(el("strong", p.name || "未命名公司", "pcard-sub-name"), badge, keepLabel);
    box.append(head);

    let chosenOrgId = item.org_id || "";
    let chosenAction = item.action || "create";
    const sim = item.similar_orgs || [];
    if (item.action === "create" && sim.length) {
      const sel = document.createElement("select");
      sel.append(new Option("作为新公司建档", ""));
      for (const s of sim) sel.append(new Option("关联/更新已有：" + s.name + (s.city ? " · " + s.city : ""), s.org_id));
      sel.addEventListener("change", () => {
        chosenOrgId = sel.value;
        chosenAction = chosenOrgId ? "update" : "create";
        badge.textContent = chosenOrgId ? "更新已有公司" : "新建公司";
        badge.classList.toggle("update", !!chosenOrgId);
      });
      box.append(kgField("已有相近公司", sel, true));
    }

    const nameIn = kgInput(p.name, "规范公司全称");
    const aliasIn = kgInput((p.aliases || []).join("，"), "简称 / 别名（逗号分隔）");
    const cityIn = kgInput(p.city, "城市");
    const addrIn = kgInput(p.address, "公司地址（可选）");
    const sumIn = kgInput(p.summary, "说明 / 隶属背景（可选）");
    const grid = el("div", null, "pcard-grid");
    grid.append(
      kgField("公司名称", nameIn, true),
      kgField("简称 / 别名", aliasIn),
      kgField("城市", cityIn),
      kgField("地址", addrIn, true),
      kgField("说明", sumIn, true)
    );
    box.append(grid);
    sec.append(box);
    editors.push({
      refKey: item.temp_ref || item.org_id || "",
      keepChk,
      inputs: [nameIn, aliasIn, cityIn, addrIn, sumIn],
      isKept() { return keepChk.checked; },
      build() {
        const { similar_orgs, ...rest } = item;
        const aliases = aliasIn.value.trim() ? aliasIn.value.trim().split(/[,，、]+/).map((s) => s.trim()).filter(Boolean) : [];
        const profile = {
          name: nameIn.value.trim() || p.name,
          aliases,
          org_type: p.org_type || "",
          industry: p.industry || "",
          city: cityIn.value.trim(),
          address: addrIn.value.trim(),
          website: p.website || "",
          tags: p.tags || [],
          summary: sumIn.value.trim(),
          extra: p.extra || {},
        };
        if (chosenOrgId && chosenAction !== "create") {
          const matched = sim.find((s) => s.org_id === chosenOrgId);
          return { ...rest, action: chosenAction, org_id: chosenOrgId, expected_version: rest.expected_version ?? matched?.version ?? null, profile };
        }
        return { ...rest, action: "create", org_id: null, profile };
      },
    });
  }
  return { node: sec, editors };
}

function buildPersonEditors(persons, showToggle) {
  const genderLabel = { male: "男", female: "女", other: "其他", unknown: "" };
  const blocks = [];
  const editors = [];
  for (const item of persons) {
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
    const isUpdate = item.action === "update";
    const badge = el("span", isUpdate ? "更新已有联系人" : "新建联系人", "pcard-badge" + (isUpdate ? " update" : ""));
    titleRow.append(nameTitle, genderPill, badge);
    let keepChk = null;
    if (showToggle) {
      const t = kgKeepToggle(block);
      keepChk = t.keepChk;
      titleRow.append(t.keepLabel);
    }
    const sub = el("p", [p.organization, p.title, p.city].filter(Boolean).join(" · ") || "请核对或补全名片信息", "pcard-sub");
    titleWrap.append(titleRow, sub);
    if (isUpdate && item.previous_organization && item.previous_organization !== p.organization) {
      titleWrap.append(el("p", "原公司：" + item.previous_organization + " → " + (p.organization || "未填"), "pcard-diff"));
    }
    head.append(avatar, titleWrap);
    block.append(head);

    let chosenCandidate = "";
    const cands = item.similar_candidates || [];
    if (item.action === "create" && cands.length) {
      const sel = document.createElement("select");
      sel.append(new Option("作为新联系人建档", ""));
      for (const c of cands) sel.append(new Option("合并更新已有：" + c.name + (c.organization ? " · " + c.organization : ""), c.person_id));
      sel.addEventListener("change", () => {
        chosenCandidate = sel.value;
        badge.textContent = chosenCandidate ? "合并更新已有" : "新建联系人";
        badge.classList.toggle("update", !!chosenCandidate);
      });
      block.append(kgField("疑似重名联系人", sel, true));
    }

    const nameIn = kgInput(p.name, "姓名");
    const genderSel = document.createElement("select");
    genderSel.append(new Option("未填", "unknown"), new Option("女", "female"), new Option("男", "male"), new Option("其他", "other"));
    genderSel.value = p.gender || "unknown";
    const orgIn = kgInput(p.organization, "公司/机构全称");
    const deptTitleIn = kgInput([p.department, p.title].filter(Boolean).join(" / "), "部门 / 职位（可选）");
    const phoneIn = kgInput(firstOf("phone"), "手机号");
    const wechatIn = kgInput(firstOf("wechat"), "微信号（可选）");
    const emailIn = kgInput(firstOf("email"), "邮箱（可选）", "email");
    const cityIn = kgInput(p.city, "城市");
    const addrIn = kgInput(initialAddress, "完整详细地址");
    const birthIn = kgInput(p.birth ? [p.birth.year, p.birth.month, p.birth.day].filter((x) => x != null).join("-") : "", "出生年份或生日");
    const tagsIn = kgInput((p.tags || []).join("，"), "标签（逗号分隔）");
    const summaryIn = kgInput(p.summary, "备注 / 认识背景");

    nameIn.addEventListener("input", () => { const v = nameIn.value.trim() || "人"; nameTitle.textContent = v; avatar.textContent = v.slice(0, 1); });
    orgIn.addEventListener("input", () => { sub.textContent = [orgIn.value.trim(), cityIn.value.trim()].filter(Boolean).join(" · ") || "请核对或补全名片信息"; });
    genderSel.addEventListener("change", () => { const txt = genderLabel[genderSel.value] || ""; genderPill.textContent = txt; genderPill.classList.toggle("hidden", !txt); });

    const grid = el("div", null, "pcard-grid");
    grid.append(
      kgField("姓名", nameIn), kgField("性别", genderSel),
      kgField("公司 / 机构", orgIn, true), kgField("部门 / 职位", deptTitleIn),
      kgField("手机", phoneIn), kgField("微信", wechatIn),
      kgField("城市", cityIn), kgField("完整地址", addrIn, true),
      kgField("邮箱", emailIn), kgField("出生年份 / 生日", birthIn),
      kgField("标签", tagsIn, true), kgField("备注", summaryIn, true)
    );
    block.append(grid);
    blocks.push(block);
    editors.push({
      refKey: item.temp_ref || item.person_id || "",
      keepChk,
      inputs: [nameIn, genderSel, orgIn, deptTitleIn, phoneIn, wechatIn, emailIn, cityIn, addrIn, birthIn, tagsIn, summaryIn],
      isKept() { return !this.keepChk || this.keepChk.checked; },
      build() {
        const { similar_candidates, previous_organization, ...rest } = item;
        const contacts = [...preservedContacts];
        if (phoneIn.value.trim()) contacts.push({ kind: "phone", value: phoneIn.value.trim(), label: "手机", primary: true });
        if (wechatIn.value.trim()) contacts.push({ kind: "wechat", value: wechatIn.value.trim(), label: "微信", primary: false });
        if (emailIn.value.trim()) contacts.push({ kind: "email", value: emailIn.value.trim(), label: "邮箱", primary: false });
        const dt = deptTitleIn.value.trim().split(/[/／]/).map((s) => s.trim()).filter(Boolean);
        const extra = { ...extraObj };
        if (addrIn.value.trim()) extra.address = addrIn.value.trim();
        let birth = p.birth || null;
        const bText = birthIn.value.trim();
        if (!bText) birth = null;
        else {
          const nums = bText.split(/[-/.年 月日]+/).map((s) => Number(s)).filter((n) => Number.isInteger(n) && n > 0);
          if (nums.length === 1 && nums[0] >= 1900) birth = { year: nums[0], month: null, day: null, calendar: "solar" };
          else if (nums.length === 2 && nums[0] <= 12) birth = { year: null, month: nums[0], day: nums[1], calendar: "solar" };
          else if (nums.length >= 3) birth = { year: nums[0], month: nums[1], day: nums[2], calendar: "solar" };
        }
        const tags = tagsIn.value.trim() ? tagsIn.value.trim().split(/[,，、]+/).map((s) => s.trim()).filter(Boolean) : [];
        const profile = {
          name: nameIn.value.trim() || p.name, aliases: p.aliases || [], gender: genderSel.value || "unknown",
          birth, organization: orgIn.value.trim(), department: dt.length > 1 ? dt[0] : "",
          title: dt.length > 1 ? dt.slice(1).join(" / ") : (dt[0] || ""), city: cityIn.value.trim(),
          contacts, tags, summary: summaryIn.value.trim(), attachment_file_ids: p.attachment_file_ids || [], extra,
        };
        if (chosenCandidate) {
          const matched = cands.find((c) => c.person_id === chosenCandidate);
          return { ...rest, action: "update", person_id: chosenCandidate, expected_version: matched?.version ?? null, profile };
        }
        return { ...rest, profile };
      },
    });
  }
  return { blocks, editors };
}

function buildMatterEditors(matters) {
  if (!matters.length) return { node: null, editors: [] };
  const sec = el("div", null, "pcard-section");
  sec.append(el("div", "关联项目 / 个人事项（" + matters.length + "）", "pcard-section-title"));
  const editors = [];
  for (const m of matters) {
    const box = el("div", null, "pcard-matter");
    const head = el("div", null, "pcard-title-row");
    const badge = el("span", m.action === "link" ? "关联已有事项" : "新建项目事项", "pcard-badge" + (m.action === "link" ? " update" : ""));
    const { keepLabel, keepChk } = kgKeepToggle(box);
    head.append(badge, keepLabel);
    box.append(head);
    let linkedId = m.action === "link" ? (m.matter_id || "") : "";
    const sim = m.similar_matters || [];
    if (m.action === "create" && sim.length) {
      const sel = document.createElement("select");
      sel.append(new Option("作为新事项创建", ""));
      for (const s of sim) sel.append(new Option("关联已有事项：" + s.title, s.matter_id));
      sel.addEventListener("change", () => {
        linkedId = sel.value;
        badge.textContent = linkedId ? "关联已有事项" : "新建项目事项";
        badge.classList.toggle("update", !!linkedId);
      });
      box.append(kgField("已有相近事项", sel, true));
    }
    const titleIn = kgInput(m.title, "项目 / 事项标题");
    const descIn = kgInput(m.description, "背景与沟通要点（可选）");
    const grid = el("div", null, "pcard-grid");
    grid.append(kgField("项目名称", titleIn), kgField("说明", descIn));
    box.append(grid);
    sec.append(box);
    editors.push({
      refKey: m.temp_ref || m.matter_id || "",
      keepChk,
      inputs: [titleIn, descIn],
      isKept() { return keepChk.checked; },
      build() {
        const { similar_matters, ...rest } = m;
        const title = titleIn.value.trim() || m.title;
        const description = descIn.value.trim();
        return linkedId ? { ...rest, action: "link", matter_id: linkedId, title, description } : { ...rest, action: "create", matter_id: null, title, description };
      },
    });
  }
  return { node: sec, editors };
}

function buildActionableEditors(todos, events, reminders) {
  const total = todos.length + events.length + reminders.length;
  if (!total) return { node: null, todoEd: [], eventEd: [], reminderEd: [] };
  const sec = el("div", null, "pcard-section");
  sec.append(el("div", "行动项：待办 / 日程 / 提醒（" + total + "）", "pcard-section-title"));
  const todoEd = [], eventEd = [], reminderEd = [];
  for (const t of todos) {
    const box = el("div", null, "pcard-matter");
    const { keepLabel, keepChk } = kgKeepToggle(box);
    const head = el("div", null, "pcard-title-row");
    head.append(el("span", "待办", "pcard-badge"), t.due_at ? el("span", "截止 " + kgFormatDateTime(t.due_at), "pcard-sub") : el("span", "", ""), keepLabel);
    const titleIn = kgInput(t.title, "待办事项");
    const descIn = kgInput(t.description, "补充说明");
    const grid = el("div", null, "pcard-grid");
    grid.append(kgField("待办内容", titleIn), kgField("说明", descIn));
    box.append(head, grid);
    sec.append(box);
    todoEd.push({ keepChk, inputs: [titleIn, descIn], isKept: () => keepChk.checked, build: (dropped) => ({ ...t, title: titleIn.value.trim() || t.title, description: descIn.value.trim(), matter_ref: dropped.has(t.matter_ref) ? null : (t.matter_ref ?? null) }) });
  }
  for (const ev of events) {
    const box = el("div", null, "pcard-matter");
    const { keepLabel, keepChk } = kgKeepToggle(box);
    const head = el("div", null, "pcard-title-row");
    head.append(el("span", "日程", "pcard-badge update"), el("span", kgFormatDateTime(ev.start_at) + " – " + kgFormatDateTime(ev.end_at), "pcard-sub"), keepLabel);
    const titleIn = kgInput(ev.title, "日程标题");
    const descIn = kgInput(ev.description, "补充说明");
    const grid = el("div", null, "pcard-grid");
    grid.append(kgField("日程主题", titleIn), kgField("说明", descIn));
    box.append(head, grid);
    sec.append(box);
    eventEd.push({ keepChk, inputs: [titleIn, descIn], isKept: () => keepChk.checked, build: (dropped) => ({ ...ev, title: titleIn.value.trim() || ev.title, description: descIn.value.trim(), matter_ref: dropped.has(ev.matter_ref) ? null : (ev.matter_ref ?? null) }) });
  }
  for (const r of reminders) {
    const box = el("div", null, "pcard-matter");
    const { keepLabel, keepChk } = kgKeepToggle(box);
    const head = el("div", null, "pcard-title-row");
    head.append(el("span", "提醒", "pcard-badge record"), el("span", "提醒时间 " + kgFormatDateTime(r.due_at), "pcard-sub"), keepLabel);
    const titleIn = kgInput(r.title, "提醒标题");
    const bodyIn = kgInput(r.body, "提醒内容");
    const grid = el("div", null, "pcard-grid");
    grid.append(kgField("提醒标题", titleIn), kgField("内容", bodyIn));
    box.append(head, grid);
    sec.append(box);
    reminderEd.push({ keepChk, inputs: [titleIn, bodyIn], isKept: () => keepChk.checked, build: () => ({ ...r, title: titleIn.value.trim() || r.title, body: bodyIn.value.trim() }) });
  }
  return { node: sec, todoEd, eventEd, reminderEd };
}

function peopleContactCard(draft, proposalId, options = {}) {
  const node = el("section", null, "card people-contact-card");
  const rawRecord = draft.record || draft.note || null;
  const orgs = draft.orgs || [];
  const persons = draft.persons || [];
  const matters = draft.matters || [];
  const todos = draft.todos || [];
  const events = draft.events || [];
  const reminders = draft.reminders || [];
  const relations = draft.relations || [];
  const branchCount = orgs.length + persons.length + matters.length + todos.length + events.length + reminders.length;

  if (branchCount > 1 || orgs.length > 0 || matters.length > 0 || rawRecord?.record_kind === "meeting") {
    const parts = [];
    if (orgs.length) parts.push(orgs.length + " 家公司");
    if (persons.length) parts.push(persons.length + " 位人物");
    if (matters.length) parts.push(matters.length + " 个项目");
    if (todos.length + events.length + reminders.length) parts.push((todos.length + events.length + reminders.length) + " 条行动项");
    if (relations.length) parts.push(relations.length + " 条关系");
    const label = rawRecord?.record_kind === "meeting" ? "会议记录拆解草稿" : "原始记录拆解草稿";
    node.append(el("div", label + (parts.length ? " · " + parts.join(" · ") : ""), "pcard-summary"));
  }

  const recordEd = buildRecordSection(rawRecord);
  if (recordEd) node.append(recordEd.node);

  const orgPart = buildOrgEditors(orgs);
  if (orgPart.node) node.append(orgPart.node);

  const personPart = buildPersonEditors(persons, branchCount > 1 || !!recordEd);
  if (personPart.blocks.length) {
    const pWrap = el("div", null, orgs.length || recordEd ? "pcard-section" : "");
    if (orgs.length || matters.length || recordEd) pWrap.append(el("div", "人物档案（" + persons.length + "）", "pcard-section-title"));
    for (const b of personPart.blocks) pWrap.append(b);
    node.append(pWrap);
  }

  const matterPart = buildMatterEditors(matters);
  if (matterPart.node) node.append(matterPart.node);

  const actionPart = buildActionableEditors(todos, events, reminders);
  if (actionPart.node) node.append(actionPart.node);

  if (relations.length) {
    const refNames = {};
    for (const o of orgs) { if (o.temp_ref) refNames[o.temp_ref] = o.profile?.name || o.temp_ref; if (o.org_id) refNames[o.org_id] = o.profile?.name || o.org_id; }
    for (const p of persons) { if (p.temp_ref) refNames[p.temp_ref] = p.profile?.name || p.temp_ref; if (p.person_id) refNames[p.person_id] = p.profile?.name || p.person_id; }
    for (const m of matters) { if (m.temp_ref) refNames[m.temp_ref] = m.title || m.temp_ref; if (m.matter_id) refNames[m.matter_id] = m.title || m.matter_id; }
    const relSec = el("div", null, "pcard-section");
    relSec.append(el("div", "跨实体关联关系（" + relations.length + "）", "pcard-section-title"));
    const chips = el("div", null, "pcard-rel-list");
    for (const r of relations) {
      const arrow = r.symmetric ? " ↔ " : " → ";
      const relLabel = KG_RELATION_LABELS[r.relation_type] || r.relation_type;
      const roleText = r.role ? "（" + r.role + "）" : "";
      chips.append(el("span", (refNames[r.from_ref] || r.from_ref) + arrow + (refNames[r.target_ref] || r.target_ref) + " · " + relLabel + roleText, "pcard-rel-chip"));
    }
    relSec.append(chips);
    node.append(relSec);
  }

  const row = el("div", null, "row pcard-actions"), status = el("p", "", "meta");
  const path = "/v1/assistant/proposals/" + encodeURIComponent(proposalId);
  const lockInputs = () => {
    for (const ctrl of node.querySelectorAll("input, select, textarea")) ctrl.disabled = true;
  };
  const settle = (text, saved = false) => {
    lockInputs();
    row.replaceChildren();
    status.textContent = text;
    if (saved) {
      if (typeof openContactsDrawer === "function") {
        row.append(button("在通讯录中查看 →", () => openContactsDrawer().catch(report), "link pcard-link"));
      } else {
        const link = el("a", "在「人脉与图谱」中查看 →", "pcard-link");
        link.href = "index.html?classic=1#functions";
        row.append(link);
      }
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
  const buildPayload = (rawOnly) => {
    if (rawOnly && recordEd) {
      return { data: { record: recordEd.build(true, new Set()), orgs: [], persons: [], matters: [], todos: [], events: [], reminders: [], relations: [] } };
    }
    const dropped = new Set([
      ...orgPart.editors.filter((e) => !e.isKept()).map((e) => e.refKey),
      ...personPart.editors.filter((e) => !e.isKept()).map((e) => e.refKey),
      ...matterPart.editors.filter((e) => !e.isKept()).map((e) => e.refKey),
    ].filter(Boolean));
    return {
      data: {
        record: recordEd ? recordEd.build(false, dropped) : null,
        orgs: orgPart.editors.filter((e) => e.isKept()).map((e) => e.build()),
        persons: personPart.editors.filter((e) => e.isKept()).map((e) => e.build()),
        matters: matterPart.editors.filter((e) => e.isKept()).map((e) => e.build()),
        todos: actionPart.todoEd.filter((e) => e.isKept()).map((e) => e.build(dropped)),
        events: actionPart.eventEd.filter((e) => e.isKept()).map((e) => e.build(dropped)),
        reminders: actionPart.reminderEd.filter((e) => e.isKept()).map((e) => e.build()),
        relations: relations.filter((r) => !dropped.has(r.from_ref) && !dropped.has(r.target_ref)),
      },
    };
  };
  const btnText = branchCount > 1 || orgs.length || matters.length ? "确认保存全部" : "确认保存名片";
  row.append(button(btnText, act("/confirm", () => buildPayload(false), "已保存到个人知识图谱", true), "primary"));
  if (recordEd && branchCount > 0) {
    row.append(button("仅存原始记录", act("/confirm", () => buildPayload(true), "已仅保存原始记录", true)));
  }
  row.append(button("取消", act("/cancel", null, "已取消", false)));
  node.append(row, status);
  if (options.restore) restoreProposal(path, row, (t) => settle(t, t === "已保存"));
  entry.cards.set("proposal:" + proposalId, node);
  return node;
}

function cleanPeopleDraft(draft, picks = []) {
  return {
    record: draft.record || draft.note || null,
    orgs: (draft.orgs || []).map(({ similar_orgs, ...rest }) => rest),
    persons: (draft.persons || []).map((item, idx) => {
      const { similar_candidates, previous_organization, ...rest } = item;
      const chosenId = picks[idx];
      if (chosenId) {
        const chosen = (similar_candidates || []).find((c) => c.person_id === chosenId);
        return { ...rest, action: "update", person_id: chosenId, expected_version: chosen?.version ?? null };
      }
      return rest;
    }),
    matters: (draft.matters || []).map(({ similar_matters, ...rest }) => rest),
    todos: draft.todos || [],
    events: draft.events || [],
    reminders: draft.reminders || [],
    relations: draft.relations || [],
  };
}
