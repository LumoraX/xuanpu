// 玄圃小院「书架 · 资料室」统一抽屉：通讯录 | 公司 | 项目 | 记录 | 文件 | 动态知识库。
// 全部复用既有 /v1/people、/v1/workspace、/v1/me/knowledge 接口；纯原生 DOM（一律 textContent），零依赖。
"use strict";

const AB_REL_LABELS = {
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
  former_colleague: "前同事",
  introduced_by: "引荐",
  reports_to: "汇报给",
  family: "家人",
  partner: "合作伙伴",
  client: "客户",
  vendor: "供应商",
  supervisor: "上级",
  subordinate: "下属",
  classmate: "同学",
  spouse: "配偶",
  acquaintance: "熟人",
  matter_role: "参与事项",
  org_matter_role: "关联事项",
};

const AB_CONTACT_LABELS = { phone: "手机", wechat: "微信", email: "邮箱", feishu: "飞书", qq: "QQ", other: "联系" };
const AB_GENDER_LABELS = { male: "男", female: "女", other: "其他", unknown: "" };
const AB_RECORD_LABELS = { meeting: "会议记录", note: "随手记", card: "名片", call: "通话", other: "记录" };
const AB_MATTER_STATUS = { active: "进行中", completed: "已完成", archived: "已归档" };

const abState = {
  open: false,
  tab: "people", // "people" | "orgs" | "matters" | "notes" | "files" | "knowledge"
  query: "",
  orgFilter: "",
  people: [],
  orgs: [],
  matters: [],
  notes: [],
  files: [],
  knowledgeEnabled: false,
  knowledgeSources: [],
  workMemories: [],
  knowledgeHits: [],
  knowledgeQuery: "",
  peopleById: new Map(),
  orgsById: new Map(),
  mattersById: new Map(),
  notesById: new Map(),
  selectedPersonId: null,
  selectedOrgId: null,
  selectedMatterId: null,
  selectedNoteId: null,
  selectedFileId: null,
  detail: null,
  orgDetail: null,
  matterPeople: [],
  editing: null, // null | "create" | "edit" | "create_matter" | "import"
  importState: null, // { filename, parsing, committing, preview, decisions, requestId }
  loadingList: false,
  loadingDetail: false,
  addingNote: false,
  error: "",
  status: "",
  detailGen: 0,
  keyBound: false,
};

function abAvatarTone(name) {
  const s = String(name || "?");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 6;
  return "tone-" + h;
}

function abFormatPhone(val) {
  const digits = String(val || "").replace(/\s+/g, "");
  if (/^1\d{10}$/.test(digits)) return digits.slice(0, 3) + " " + digits.slice(3, 7) + " " + digits.slice(7);
  return String(val || "");
}

function abFormatDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function abFormatSize(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function abPrimaryPhone(person) {
  const contacts = (person && person.profile && person.profile.contacts) || [];
  const phone = contacts.find((c) => c.kind === "phone" && c.primary) || contacts.find((c) => c.kind === "phone");
  return phone ? phone.value : "";
}

function abCopyText(text, btn) {
  const done = () => {
    const old = btn.textContent;
    btn.textContent = "已复制";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1400);
  };
  if (navigator && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => {});
  }
}

function abNotifyWorld() {
  if (globalThis.Courtyard && Courtyard.stage && typeof Courtyard.stage.refreshWorld === "function") {
    Courtyard.stage.refreshWorld().catch(() => {});
  }
}

function abSyncNavButton() {
  const btn = $("entry-contacts");
  if (!btn) return;
  btn.classList.toggle("active", abState.open);
  btn.setAttribute("aria-pressed", abState.open ? "true" : "false");
}

function abBindKeys() {
  if (abState.keyBound || typeof document === "undefined" || !document.addEventListener) return;
  abState.keyBound = true;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && abState.open) closeContactsDrawer();
  });
}

function closeContactsDrawer(resetData = false) {
  abState.open = false;
  abState.editing = null;
  abState.importState = null;
  abState.addingNote = false;
  if (resetData) {
    abState.people = [];
    abState.orgs = [];
    abState.matters = [];
    abState.notes = [];
    abState.files = [];
    abState.knowledgeEnabled = false;
    abState.knowledgeSources = [];
    abState.workMemories = [];
    abState.knowledgeHits = [];
    abState.knowledgeQuery = "";
    abState.peopleById = new Map();
    abState.orgsById = new Map();
    abState.mattersById = new Map();
    abState.notesById = new Map();
    abState.selectedPersonId = null;
    abState.selectedOrgId = null;
    abState.selectedMatterId = null;
    abState.selectedNoteId = null;
    abState.selectedFileId = null;
    abState.detail = null;
    abState.orgDetail = null;
    abState.matterPeople = [];
    abState.query = "";
    abState.orgFilter = "";
    abState.error = "";
    abState.status = "";
    if (typeof resetPeopleGraphState === "function") resetPeopleGraphState();
  }
  const overlay = $("contacts-overlay");
  if (overlay) {
    overlay.classList.add("hidden");
    overlay.replaceChildren();
  }
  abSyncNavButton();
}

async function openContactsDrawer(options = {}) {
  if (!entry.me) return;
  if (typeof closeBoardDrawer === "function") closeBoardDrawer();
  abBindKeys();
  abState.open = true;
  const validTabs = new Set(["people", "orgs", "matters", "graph", "notes", "files", "knowledge"]);
  if (options.tab && validTabs.has(options.tab)) abState.tab = options.tab;
  if (options.personId) { abState.tab = "people"; abState.selectedPersonId = options.personId; }
  if (options.orgId) { abState.tab = "orgs"; abState.selectedOrgId = options.orgId; }
  if (options.matterId) { abState.tab = "matters"; abState.selectedMatterId = options.matterId; }
  if (options.noteId) { abState.tab = "notes"; abState.selectedNoteId = options.noteId; }
  if (options.query !== undefined) abState.query = String(options.query || "");
  abState.editing = options.create ? "create" : null;
  abState.addingNote = false;
  abState.error = "";
  abState.status = "";
  abSyncNavButton();
  renderContactsDrawer();
  await refreshContactsData();
}

function openBookshelfDrawer(options = {}) {
  return openContactsDrawer(options);
}

async function refreshContactsData() {
  const epoch = entry.epoch;
  abState.loadingList = true;
  abState.error = "";
  renderContactsDrawer();
  try {
    const [pRes, oRes, mRes, nRes, fRes, kRes, memRes] = await Promise.all([
      entryApi("GET", "/v1/people?limit=100"),
      entryApi("GET", "/v1/people/orgs?limit=100").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=matter").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/people/notes?limit=100").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/workspace/items?kind=file").catch(() => ({ items: [] })),
      entryApi("GET", "/v1/me/knowledge/sources").then((r) => ({ ok: true, items: r.items || [] })).catch(() => ({ ok: false, items: [] })),
      entryApi("GET", "/v1/me/work-memory").then((r) => ({ ok: true, items: r.items || [] })).catch(() => ({ ok: false, items: [] })),
    ]);
    if (isStale(epoch) || !abState.open) return;
    abState.people = pRes.items || [];
    abState.orgs = oRes.items || [];
    abState.matters = (mRes.items || []).filter((m) => !m.trashed_at);
    abState.notes = (nRes.items || []).filter((n) => !n.trashed_at);
    abState.files = (fRes.items || []).filter((f) => !f.trashed_at);
    abState.knowledgeEnabled = Boolean(kRes.ok || memRes.ok);
    abState.knowledgeSources = kRes.items || [];
    abState.workMemories = (memRes.items || []).filter((m) => m && m.status === "confirmed");
    abState.peopleById = new Map(abState.people.map((p) => [p.id, p]));
    abState.orgsById = new Map(abState.orgs.map((o) => [o.id, o]));
    abState.mattersById = new Map(abState.matters.map((m) => [m.id, m]));
    abState.notesById = new Map(abState.notes.map((n) => [n.id, n]));
    abState.loadingList = false;

    if (abState.tab === "knowledge" && !abState.knowledgeEnabled) abState.tab = "people";
    await abSyncSelectionForTab();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    abState.loadingList = false;
    abState.error = (error && error.message) || "资料室读取失败";
    renderContactsDrawer();
  }
}

async function abSyncSelectionForTab() {
  if (abState.tab === "people" && abState.editing !== "create" && abState.editing !== "import") {
    if (!abState.selectedPersonId || !abState.peopleById.has(abState.selectedPersonId)) {
      abState.selectedPersonId = abState.people.length ? abState.people[0].id : null;
    }
    if (abState.selectedPersonId) await loadPersonDetail(abState.selectedPersonId);
    else { abState.detail = null; renderContactsDrawer(); }
    return;
  }
  if (abState.tab === "orgs") {
    if (!abState.selectedOrgId || !abState.orgsById.has(abState.selectedOrgId)) {
      abState.selectedOrgId = abState.orgs.length ? abState.orgs[0].id : null;
    }
    if (abState.selectedOrgId) await loadOrgDetail(abState.selectedOrgId);
    else { abState.orgDetail = null; renderContactsDrawer(); }
    return;
  }
  if (abState.tab === "matters" && abState.editing !== "create_matter") {
    if (!abState.selectedMatterId || !abState.mattersById.has(abState.selectedMatterId)) {
      abState.selectedMatterId = abState.matters.length ? abState.matters[0].id : null;
    }
    if (abState.selectedMatterId) await loadMatterDetail(abState.selectedMatterId);
    else { abState.matterPeople = []; renderContactsDrawer(); }
    return;
  }
  if (abState.tab === "graph") {
    if (typeof loadPeopleGraph === "function") await loadPeopleGraph();
    else renderContactsDrawer();
    return;
  }
  if (abState.tab === "notes") {
    if (!abState.selectedNoteId || !abState.notesById.has(abState.selectedNoteId)) {
      abState.selectedNoteId = abState.notes.length ? abState.notes[0].id : null;
    }
    renderContactsDrawer();
    return;
  }
  if (abState.tab === "files") {
    if (!abState.selectedFileId || !abState.files.some((f) => f.id === abState.selectedFileId)) {
      abState.selectedFileId = abState.files.length ? abState.files[0].id : null;
    }
    renderContactsDrawer();
    return;
  }
  renderContactsDrawer();
}

async function loadPersonDetail(personId) {
  const epoch = entry.epoch;
  const gen = ++abState.detailGen;
  abState.selectedPersonId = personId;
  abState.editing = null;
  abState.addingNote = false;
  abState.loadingDetail = true;
  abState.status = "";
  renderContactsDrawer();
  try {
    const body = await entryApi("GET", "/v1/people/" + encodeURIComponent(personId));
    if (isStale(epoch) || !abState.open || gen !== abState.detailGen) return;
    abState.detail = body;
    if (body && body.person) abState.peopleById.set(body.person.id, body.person);
    abState.loadingDetail = false;
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    abState.loadingDetail = false;
    abState.error = (error && error.message) || "联系人详情读取失败";
    renderContactsDrawer();
  }
}

async function loadOrgDetail(orgId) {
  const epoch = entry.epoch;
  const gen = ++abState.detailGen;
  abState.selectedOrgId = orgId;
  abState.loadingDetail = true;
  abState.status = "";
  renderContactsDrawer();
  try {
    const body = await entryApi("GET", "/v1/people/orgs/" + encodeURIComponent(orgId));
    if (isStale(epoch) || !abState.open || gen !== abState.detailGen) return;
    abState.orgDetail = body;
    abState.loadingDetail = false;
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    abState.loadingDetail = false;
    abState.error = (error && error.message) || "公司详情读取失败";
    renderContactsDrawer();
  }
}

async function loadMatterDetail(matterId) {
  const epoch = entry.epoch;
  const gen = ++abState.detailGen;
  abState.selectedMatterId = matterId;
  abState.editing = null;
  abState.loadingDetail = true;
  abState.status = "";
  renderContactsDrawer();
  try {
    const body = await entryApi("GET", "/v1/workspace/items/" + encodeURIComponent(matterId) + "/people").catch(() => ({ items: [] }));
    if (isStale(epoch) || !abState.open || gen !== abState.detailGen) return;
    abState.matterPeople = body.items || [];
    abState.loadingDetail = false;
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    abState.loadingDetail = false;
    renderContactsDrawer();
  }
}

function abFilteredPeople() {
  const q = abState.query.trim().toLowerCase();
  const org = abState.orgFilter;
  return abState.people.filter((p) => {
    const prof = p.profile || {};
    if (org && (p.organization || prof.organization || "") !== org) return false;
    if (!q) return true;
    const hay = [
      p.name, p.organization, prof.department, prof.title, prof.city, prof.summary,
      ...(prof.aliases || []), ...(prof.tags || []), ...((prof.contacts || []).map((c) => c.value)),
      prof.extra && prof.extra.address,
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  });
}

function abFilteredOrgs() {
  const q = abState.query.trim().toLowerCase();
  return abState.orgs.filter((o) => {
    if (!q) return true;
    const prof = o.profile || {};
    return [o.name, prof.industry, prof.city, prof.address, prof.summary, ...(prof.aliases || []), ...(prof.tags || [])]
      .filter(Boolean).join(" ").toLowerCase().includes(q);
  });
}

function abFilteredMatters() {
  const q = abState.query.trim().toLowerCase();
  return abState.matters.filter((m) => {
    if (!q) return true;
    const d = m.data || {};
    return [d.title, d.description, d.status].filter(Boolean).join(" ").toLowerCase().includes(q);
  });
}

function abFilteredNotes() {
  const q = abState.query.trim().toLowerCase();
  return abState.notes.filter((n) => {
    if (!q) return true;
    return [n.title, n.location, n.summary, n.content, n.record_kind].filter(Boolean).join(" ").toLowerCase().includes(q);
  });
}

function abFilteredFiles() {
  const q = abState.query.trim().toLowerCase();
  return abState.files.filter((f) => {
    if (!q) return true;
    const d = f.data || {};
    return [d.name, d.mime_type].filter(Boolean).join(" ").toLowerCase().includes(q);
  });
}

function abAvatarNode(name, sizeClass, person) {
  const box = el("div", null, "ab-avatar " + sizeClass + " " + abAvatarTone(name));
  if (person && person.has_avatar) {
    const img = document.createElement("img");
    img.src = "/v1/people/" + encodeURIComponent(person.id) + "/avatar?v=" + encodeURIComponent(person.version || 1);
    img.alt = name || "";
    box.appendChild(img);
  } else {
    box.textContent = (name || "?").trim().slice(0, 1);
  }
  return box;
}

function renderContactsDrawer() {
  const overlay = $("contacts-overlay");
  if (!overlay) return;
  if (!abState.open) {
    overlay.classList.add("hidden");
    return;
  }
  overlay.classList.remove("hidden");
  const activeId = document.activeElement ? document.activeElement.id : null;
  const caretPos = activeId === "ab-search-input" || activeId === "ab-kb-input" || activeId === "ab-graph-search" ? document.activeElement.selectionStart : null;

  const sheet = el("section", null, "ab-sheet");
  sheet.addEventListener("click", (e) => e.stopPropagation());
  overlay.onclick = () => closeContactsDrawer();

  // 顶栏：书架 · 资料室 + 动态模块页签 + 快捷操作
  const header = el("header", null, "ab-header");
  const titleWrap = el("div", null, "ab-title-wrap");
  titleWrap.append(
    el("h2", "书架 · 资料室", "ab-title"),
    el("span", `${abState.people.length} 人 · ${abState.orgs.length} 公司 · ${abState.matters.length} 项目 · ${abState.notes.length} 记录`, "ab-count")
  );

  const tabSpecs = [
    { id: "people", label: "通讯录", count: abState.people.length },
    { id: "orgs", label: "公司", count: abState.orgs.length },
    { id: "matters", label: "项目", count: abState.matters.length },
    { id: "graph", label: "关系图", count: 0 },
    { id: "notes", label: "记录", count: abState.notes.length },
    { id: "files", label: "文件", count: abState.files.length },
  ];
  if (abState.knowledgeEnabled) {
    tabSpecs.push({ id: "knowledge", label: "知识库", count: abState.knowledgeSources.length + abState.workMemories.length });
  }
  const tabs = el("div", null, "ab-tabs");
  for (const spec of tabSpecs) {
    const label = spec.count > 0 ? `${spec.label} ${spec.count}` : spec.label;
    tabs.appendChild(button(label, () => {
      abState.tab = spec.id;
      abState.editing = null;
      abState.query = "";
      abState.status = "";
      abSyncSelectionForTab();
    }, "ab-tab" + (abState.tab === spec.id ? " active" : "")));
  }

  const headActions = el("div", null, "ab-head-actions");
  if (abState.tab === "matters") {
    headActions.appendChild(button("+ 新建项目", () => {
      abState.editing = "create_matter";
      abState.status = "";
      renderContactsDrawer();
    }, "primary ab-new-btn"));
  } else {
    headActions.appendChild(button("导入 / 关联", () => {
      abState.tab = "people";
      abState.editing = "import";
      abState.importState = {
        source: "file",
        filename: "",
        parsing: false,
        committing: false,
        preview: null,
        decisions: [],
        requestId: "",
        google: { loading: true, busy: false, data: null },
      };
      abState.error = "";
      abState.status = "";
      renderContactsDrawer();
      abLoadGoogleStatus();
    }, "ab-close-btn"));
    headActions.appendChild(button("+ 新建联系人", () => {
      abState.tab = "people";
      abState.editing = "create";
      abState.status = "";
      renderContactsDrawer();
    }, "primary ab-new-btn"));
  }
  headActions.appendChild(button("关闭", () => closeContactsDrawer(), "ab-close-btn"));
  header.append(titleWrap, tabs, headActions);
  sheet.appendChild(header);

  if (abState.error) sheet.appendChild(el("p", abState.error, "ab-banner error"));
  if (abState.status) sheet.appendChild(el("p", abState.status, "ab-banner ok"));

  const isSingle = abState.tab === "knowledge" || abState.tab === "graph";
  const body = el("div", null, isSingle ? "ab-body single" + (abState.tab === "graph" ? " graph-body" : "") : "ab-body");
  if (abState.tab === "knowledge") {
    body.appendChild(buildKnowledgePane());
  } else if (abState.tab === "graph" && typeof buildPeopleGraphPane === "function") {
    body.appendChild(buildPeopleGraphPane());
  } else {
    body.append(buildAbListPane(), buildAbDetailPane());
  }
  sheet.appendChild(body);
  overlay.replaceChildren(sheet);

  if (activeId === "ab-search-input" || activeId === "ab-kb-input" || activeId === "ab-graph-search") {
    const inp = $(activeId);
    if (inp) {
      inp.focus();
      if (caretPos !== null && typeof inp.setSelectionRange === "function") inp.setSelectionRange(caretPos, caretPos);
    }
  }
}

function buildAbListPane() {
  const pane = el("aside", null, "ab-list-pane");
  const placeholders = {
    people: "搜索姓名、公司、手机号…",
    orgs: "搜索公司名称、行业、城市…",
    matters: "搜索项目名称或说明…",
    notes: "搜索会议标题、地点、内容…",
    files: "搜索文件名…",
  };
  const searchWrap = el("div", null, "ab-search-wrap");
  const search = document.createElement("input");
  search.id = "ab-search-input";
  search.type = "search";
  search.className = "ab-search";
  search.placeholder = placeholders[abState.tab] || "搜索…";
  search.value = abState.query;
  search.addEventListener("input", () => {
    abState.query = search.value;
    renderContactsDrawer();
  });
  searchWrap.appendChild(search);
  pane.appendChild(searchWrap);

  if (abState.tab === "people") {
    const orgNames = [...new Set(abState.people.map((p) => p.organization || (p.profile && p.profile.organization) || "").filter(Boolean))];
    if (orgNames.length > 0) {
      const chips = el("div", null, "ab-filter-chips");
      chips.appendChild(button("全部", () => { abState.orgFilter = ""; renderContactsDrawer(); }, "ab-chip" + (!abState.orgFilter ? " active" : "")));
      for (const name of orgNames.slice(0, 6)) {
        const shortName = name.length > 10 ? name.slice(0, 10) + "…" : name;
        const chip = button(shortName, () => {
          abState.orgFilter = abState.orgFilter === name ? "" : name;
          renderContactsDrawer();
        }, "ab-chip" + (abState.orgFilter === name ? " active" : ""));
        chip.title = name;
        chips.appendChild(chip);
      }
      pane.appendChild(chips);
    }
  }

  const list = el("div", null, "ab-list");
  if (abState.loadingList && !abState.people.length && !abState.orgs.length && !abState.matters.length && !abState.notes.length) {
    list.appendChild(el("p", "正在整理书架资料…", "ab-empty"));
  } else if (abState.tab === "people") {
    const items = abFilteredPeople();
    if (!items.length) {
      list.appendChild(el("p", abState.people.length ? "没有匹配的联系人" : "通讯录还是空的，可以直接对管家说联系人信息，或点击右上角新建。", "ab-empty"));
    } else {
      for (const p of items) {
        const prof = p.profile || {};
        const selected = abState.editing !== "create" && abState.selectedPersonId === p.id;
        const row = el("button", null, "ab-row" + (selected ? " selected" : ""));
        row.type = "button";
        row.addEventListener("click", () => loadPersonDetail(p.id));
        row.appendChild(abAvatarNode(p.name, "sm", p));
        const info = el("div", null, "ab-row-info");
        const top = el("div", null, "ab-row-top");
        top.appendChild(el("span", p.name || "未命名", "ab-row-name"));
        if ((prof.aliases || []).length) top.appendChild(el("span", prof.aliases[0], "ab-row-alias"));
        info.appendChild(top);
        const subParts = [p.organization || prof.organization, prof.title].filter(Boolean);
        if (subParts.length) info.appendChild(el("div", subParts.join(" · "), "ab-row-sub"));
        const phone = abPrimaryPhone(p);
        if (phone) info.appendChild(el("div", abFormatPhone(phone), "ab-row-phone"));
        row.appendChild(info);
        list.appendChild(row);
      }
    }
  } else if (abState.tab === "orgs") {
    const items = abFilteredOrgs();
    if (!items.length) {
      list.appendChild(el("p", abState.orgs.length ? "没有匹配的公司" : "还没有公司档案，对话里提到公司关系时管家会自动沉淀。", "ab-empty"));
    } else {
      for (const o of items) {
        const prof = o.profile || {};
        const selected = abState.selectedOrgId === o.id;
        const row = el("button", null, "ab-row" + (selected ? " selected" : ""));
        row.type = "button";
        row.addEventListener("click", () => loadOrgDetail(o.id));
        row.appendChild(abAvatarNode(o.name, "sm org", null));
        const info = el("div", null, "ab-row-info");
        info.appendChild(el("span", o.name || "未命名公司", "ab-row-name"));
        const sub = [prof.aliases && prof.aliases[0], prof.city, prof.industry].filter(Boolean).join(" · ");
        if (sub) info.appendChild(el("div", sub, "ab-row-sub"));
        row.appendChild(info);
        list.appendChild(row);
      }
    }
  } else if (abState.tab === "matters") {
    const items = abFilteredMatters();
    if (!items.length) {
      list.appendChild(el("p", abState.matters.length ? "没有匹配的项目" : "还没有项目事项，点击右上角新建或在对话里告诉管家。", "ab-empty"));
    } else {
      for (const m of items) {
        const d = m.data || {};
        const selected = abState.editing !== "create_matter" && abState.selectedMatterId === m.id;
        const row = el("button", null, "ab-row" + (selected ? " selected" : ""));
        row.type = "button";
        row.addEventListener("click", () => loadMatterDetail(m.id));
        row.appendChild(abAvatarNode(d.title || "项", "sm org", null));
        const info = el("div", null, "ab-row-info");
        const top = el("div", null, "ab-row-top");
        top.appendChild(el("span", d.title || "未命名项目", "ab-row-name"));
        top.appendChild(el("span", AB_MATTER_STATUS[d.status] || "进行中", "ab-row-alias"));
        info.appendChild(top);
        if (d.description) info.appendChild(el("div", d.description, "ab-row-sub"));
        row.appendChild(info);
        list.appendChild(row);
      }
    }
  } else if (abState.tab === "notes") {
    const items = abFilteredNotes();
    if (!items.length) {
      list.appendChild(el("p", abState.notes.length ? "没有匹配的记录" : "还没有原始记录或会议纪要。", "ab-empty"));
    } else {
      for (const n of items) {
        const selected = abState.selectedNoteId === n.id;
        const row = el("button", null, "ab-row" + (selected ? " selected" : ""));
        row.type = "button";
        row.addEventListener("click", () => { abState.selectedNoteId = n.id; renderContactsDrawer(); });
        const info = el("div", null, "ab-row-info");
        const top = el("div", null, "ab-row-top");
        top.appendChild(el("span", AB_RECORD_LABELS[n.record_kind] || "记录", "ab-note-kind"));
        top.appendChild(el("span", n.title || n.content.slice(0, 22), "ab-row-name"));
        info.appendChild(top);
        const meta = [abFormatDate(n.occurred_at), n.location].filter(Boolean).join(" · ");
        if (meta) info.appendChild(el("div", meta, "ab-row-sub"));
        row.appendChild(info);
        list.appendChild(row);
      }
    }
  } else if (abState.tab === "files") {
    const items = abFilteredFiles();
    if (!items.length) {
      list.appendChild(el("p", abState.files.length ? "没有匹配的文件" : "工作台里还没有上传或生成的个人文件。", "ab-empty"));
    } else {
      for (const f of items) {
        const d = f.data || {};
        const selected = abState.selectedFileId === f.id;
        const row = el("button", null, "ab-row" + (selected ? " selected" : ""));
        row.type = "button";
        row.addEventListener("click", () => { abState.selectedFileId = f.id; renderContactsDrawer(); });
        row.appendChild(abAvatarNode("文", "sm org", null));
        const info = el("div", null, "ab-row-info");
        info.appendChild(el("span", d.name || "未命名文件", "ab-row-name"));
        info.appendChild(el("div", [d.mime_type, abFormatSize(d.size)].filter(Boolean).join(" · "), "ab-row-sub"));
        row.appendChild(info);
        list.appendChild(row);
      }
    }
  }
  pane.appendChild(list);
  return pane;
}

function buildAbDetailPane() {
  const pane = el("section", null, "ab-detail-pane");
  if (abState.tab === "people") {
    if (abState.editing === "import") return buildImportPane(pane);
    if (abState.editing === "create") return buildPersonForm(pane, null);
    if (abState.editing === "edit" && abState.detail && abState.detail.person) return buildPersonForm(pane, abState.detail.person);
    if (abState.loadingDetail) { pane.appendChild(el("p", "正在读取名片详情…", "ab-empty")); return pane; }
    if (!abState.detail || !abState.detail.person) { pane.appendChild(el("p", "在左侧选择一位联系人查看名片、关系与往来记录。", "ab-empty")); return pane; }
    return buildPersonDetailView(pane, abState.detail);
  }
  if (abState.tab === "orgs") {
    if (abState.loadingDetail) { pane.appendChild(el("p", "正在读取公司详情…", "ab-empty")); return pane; }
    if (!abState.orgDetail || !abState.orgDetail.org) { pane.appendChild(el("p", "在左侧选择一家公司查看下属关系、相关人员与记录。", "ab-empty")); return pane; }
    return buildOrgDetailView(pane, abState.orgDetail);
  }
  if (abState.tab === "matters") {
    if (abState.editing === "create_matter") return buildMatterForm(pane);
    const matter = abState.mattersById.get(abState.selectedMatterId);
    if (!matter) { pane.appendChild(el("p", "在左侧选择一个项目查看关联人脉、会议记录与进展。", "ab-empty")); return pane; }
    return buildMatterDetailView(pane, matter);
  }
  if (abState.tab === "notes") {
    const note = abState.notesById.get(abState.selectedNoteId);
    if (!note) { pane.appendChild(el("p", "在左侧选择一条记录查看全文与关联实体。", "ab-empty")); return pane; }
    return buildNoteDetailView(pane, note);
  }
  if (abState.tab === "files") {
    const file = abState.files.find((f) => f.id === abState.selectedFileId);
    if (!file) { pane.appendChild(el("p", "在左侧选择一份文件查看详情或下载。", "ab-empty")); return pane; }
    return buildFileDetailView(pane, file);
  }
  return pane;
}

function buildPersonDetailView(pane, detail) {
  const person = detail.person;
  const prof = person.profile || {};
  const relations = detail.relations || [];
  const notes = detail.notes || [];

  const hero = el("div", null, "ab-hero");
  hero.appendChild(abAvatarNode(person.name, "lg", person));
  const heroMain = el("div", null, "ab-hero-main");
  const nameRow = el("div", null, "ab-hero-name-row");
  nameRow.appendChild(el("h3", person.name || "未命名", "ab-hero-name"));
  for (const a of (prof.aliases || []).slice(0, 3)) nameRow.appendChild(el("span", a, "ab-tag"));
  if (AB_GENDER_LABELS[prof.gender]) nameRow.appendChild(el("span", AB_GENDER_LABELS[prof.gender], "ab-tag muted"));
  heroMain.appendChild(nameRow);
  const orgLine = [prof.organization || person.organization, prof.department, prof.title].filter(Boolean).join(" · ");
  if (orgLine) heroMain.appendChild(el("div", orgLine, "ab-hero-sub"));
  if ((prof.tags || []).length) {
    const tagRow = el("div", null, "ab-tags-row");
    for (const t of prof.tags) tagRow.appendChild(el("span", "#" + t, "ab-tag accent"));
    heroMain.appendChild(tagRow);
  }
  hero.appendChild(heroMain);

  const actions = el("div", null, "ab-hero-actions");
  if (typeof openEntityInGraph === "function") {
    actions.appendChild(button("关系图", () => openEntityInGraph("person", person.id), "ab-act-btn"));
  }
  actions.appendChild(button("问管家", () => {
    closeContactsDrawer();
    const box = $("message");
    if (box) { box.value = `查一下 ${person.name} 的全部关联信息、项目和最近记录`; box.focus(); }
  }, "ab-act-btn primary-soft"));
  actions.appendChild(button("编辑", () => { abState.editing = "edit"; abState.status = ""; renderContactsDrawer(); }, "ab-act-btn"));
  let armedDelete = false;
  const delBtn = button("删除", async () => {
    if (!armedDelete) {
      armedDelete = true;
      delBtn.textContent = "确认删除？";
      delBtn.classList.add("danger");
      setTimeout(() => { if (armedDelete) { armedDelete = false; delBtn.textContent = "删除"; delBtn.classList.remove("danger"); } }, 3500);
      return;
    }
    delBtn.disabled = true;
    try {
      await entryApi("DELETE", "/v1/people/" + encodeURIComponent(person.id) + "?version=" + encodeURIComponent(person.version));
      abState.selectedPersonId = null;
      abState.detail = null;
      abState.status = `已移除「${person.name}」`;
      abNotifyWorld();
      await refreshContactsData();
    } catch (error) {
      delBtn.disabled = false;
      abState.error = (error && error.message) || "删除失败";
      renderContactsDrawer();
    }
  }, "ab-act-btn");
  actions.appendChild(delBtn);
  hero.appendChild(actions);
  pane.appendChild(hero);

  const contacts = prof.contacts || [];
  const addr = (prof.extra && (prof.extra.address || prof.extra["地址"])) || "";
  const city = prof.city || "";
  if (contacts.length || addr || city) {
    const grid = el("div", null, "ab-contact-grid");
    for (const c of contacts) {
      const item = el("div", null, "ab-contact-card");
      const meta = el("div", null, "ab-contact-meta");
      meta.append(el("span", c.label || AB_CONTACT_LABELS[c.kind] || "联系", "ab-contact-label"),
        el("strong", c.kind === "phone" ? abFormatPhone(c.value) : c.value, "ab-contact-val"));
      const copyBtn = button("复制", () => abCopyText(c.value, copyBtn), "ab-copy-btn");
      item.append(meta, copyBtn);
      grid.appendChild(item);
    }
    if (addr || city) {
      const fullAddr = city && addr && !addr.includes(city) ? `${city} · ${addr}` : (addr || city);
      const item = el("div", null, "ab-contact-card");
      const meta = el("div", null, "ab-contact-meta");
      meta.append(el("span", "地址 / 城市", "ab-contact-label"), el("strong", fullAddr, "ab-contact-val"));
      const copyBtn = button("复制", () => abCopyText(fullAddr, copyBtn), "ab-copy-btn");
      item.append(meta, copyBtn);
      grid.appendChild(item);
    }
    pane.appendChild(grid);
  }

  if (prof.summary) {
    const bio = el("div", null, "ab-section");
    bio.append(el("h4", "备注", "ab-sec-title"), el("p", prof.summary, "ab-bio"));
    pane.appendChild(bio);
  }

  if (relations.length) {
    const relSec = el("div", null, "ab-section");
    relSec.appendChild(el("h4", `关联关系与项目（${relations.length}）`, "ab-sec-title"));
    const chips = el("div", null, "ab-rel-grid");
    for (const r of relations) {
      const isFromSelf = (r.from_kind || "person") === "person" && r.from_person_id === person.id;
      const otherKind = isFromSelf ? r.target_kind : (r.from_kind || "person");
      const otherId = isFromSelf ? r.target_id : r.from_person_id;
      const relLabel = AB_REL_LABELS[r.relation_type] || r.relation_type;
      let otherName = otherId;
      let onClick = null;
      if (otherKind === "person") {
        const peer = abState.peopleById.get(otherId);
        otherName = peer ? peer.name : otherId;
        onClick = () => loadPersonDetail(otherId);
      } else if (otherKind === "org") {
        const org = abState.orgsById.get(otherId);
        otherName = org ? org.name : otherId;
        onClick = () => { abState.tab = "orgs"; loadOrgDetail(otherId); };
      } else if (otherKind === "matter") {
        const mat = abState.mattersById.get(otherId);
        otherName = mat && mat.data ? mat.data.title : otherId;
        if (mat) onClick = () => { abState.tab = "matters"; loadMatterDetail(otherId); };
      }
      const chip = el(onClick ? "button" : "div", null, "ab-rel-pill " + otherKind + (onClick ? " clickable" : ""));
      if (onClick) { chip.type = "button"; chip.addEventListener("click", onClick); }
      chip.append(el("span", relLabel, "ab-rel-type"), el("strong", otherName, "ab-rel-target"));
      if (r.role) chip.appendChild(el("span", r.role, "ab-rel-role"));
      chips.appendChild(chip);
    }
    relSec.appendChild(chips);
    pane.appendChild(relSec);
  }

  const noteSec = el("div", null, "ab-section");
  const noteHead = el("div", null, "ab-sec-head");
  noteHead.append(
    el("h4", `往来记录（${notes.length}）`, "ab-sec-title"),
    button(abState.addingNote ? "收起" : "+ 记一条", () => { abState.addingNote = !abState.addingNote; renderContactsDrawer(); }, "ab-mini-btn")
  );
  noteSec.appendChild(noteHead);

  if (abState.addingNote) {
    const form = el("form", null, "ab-note-form");
    const area = document.createElement("textarea");
    area.rows = 2;
    area.placeholder = `记录与 ${person.name} 的互动、沟通要点或备忘…`;
    const saveBtn = button("保存记录", null, "primary ab-mini-btn");
    saveBtn.type = "submit";
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = area.value.trim();
      if (!text) return;
      saveBtn.disabled = true;
      try {
        await entryApi("POST", "/v1/people/notes", {
          json: {
            request_id: "ab_note_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
            data: { record_kind: "note", content: text, person_ids: [person.id] },
          },
        });
        abState.addingNote = false;
        abState.status = "已保存往来记录";
        abNotifyWorld();
        await refreshContactsData();
      } catch (error) {
        saveBtn.disabled = false;
        abState.error = (error && error.message) || "记录保存失败";
        renderContactsDrawer();
      }
    });
    form.append(area, saveBtn);
    noteSec.appendChild(form);
  }

  if (!notes.length) {
    noteSec.appendChild(el("p", "暂无往来记录。", "ab-muted"));
  } else {
    noteSec.appendChild(buildTimelineNodes(notes));
  }
  pane.appendChild(noteSec);
  return pane;
}

function buildTimelineNodes(records) {
  const timeline = el("div", null, "ab-timeline");
  for (const n of records) {
    const cardNode = el("article", null, "ab-note-item");
    const meta = el("div", null, "ab-note-meta");
    meta.appendChild(el("span", AB_RECORD_LABELS[n.record_kind] || "记录", "ab-note-kind"));
    const dt = abFormatDate(n.occurred_at);
    if (dt) meta.appendChild(el("span", dt, "ab-note-date"));
    if (n.location) meta.appendChild(el("span", n.location, "ab-note-loc"));
    cardNode.appendChild(meta);
    if (n.title) cardNode.appendChild(el("strong", n.title, "ab-note-title"));
    cardNode.appendChild(el("p", n.summary || n.content, "ab-note-body"));
    timeline.appendChild(cardNode);
  }
  return timeline;
}

function buildOrgDetailView(pane, detail) {
  const org = detail.org;
  const prof = org.profile || {};
  const relations = detail.relations || [];
  const records = detail.records || [];

  const hero = el("div", null, "ab-hero");
  hero.appendChild(abAvatarNode(org.name, "lg org", null));
  const heroMain = el("div", null, "ab-hero-main");
  const nameRow = el("div", null, "ab-hero-name-row");
  nameRow.appendChild(el("h3", org.name || "未命名公司", "ab-hero-name"));
  for (const a of (prof.aliases || []).slice(0, 3)) nameRow.appendChild(el("span", a, "ab-tag"));
  heroMain.appendChild(nameRow);
  const sub = [prof.org_type, prof.industry, prof.city].filter(Boolean).join(" · ");
  if (sub) heroMain.appendChild(el("div", sub, "ab-hero-sub"));
  hero.appendChild(heroMain);
  const actions = el("div", null, "ab-hero-actions");
  if (typeof openEntityInGraph === "function") {
    actions.appendChild(button("关系图", () => openEntityInGraph("org", org.id), "ab-act-btn"));
  }
  actions.appendChild(button("问管家", () => {
    closeContactsDrawer();
    const box = $("message");
    if (box) { box.value = `查一下 ${org.name} 关联的全部人员、上下级公司和项目`; box.focus(); }
  }, "ab-act-btn primary-soft"));
  hero.appendChild(actions);
  pane.appendChild(hero);

  if (prof.address || prof.summary) {
    const sec = el("div", null, "ab-section");
    if (prof.address) sec.appendChild(el("p", "地址：" + prof.address, "ab-bio"));
    if (prof.summary) sec.appendChild(el("p", prof.summary, "ab-bio"));
    pane.appendChild(sec);
  }

  if (relations.length) {
    const relSec = el("div", null, "ab-section");
    relSec.appendChild(el("h4", `关联人员与上下级公司（${relations.length}）`, "ab-sec-title"));
    const chips = el("div", null, "ab-rel-grid");
    for (const r of relations) {
      const isFromSelf = (r.from_kind || "person") === "org" && r.from_person_id === org.id;
      const otherKind = isFromSelf ? r.target_kind : (r.from_kind || "person");
      const otherId = isFromSelf ? r.target_id : r.from_person_id;
      let relLabel = AB_REL_LABELS[r.relation_type] || r.relation_type;
      if (!isFromSelf && r.relation_type === "subsidiary_of") relLabel = "下属公司";
      else if (isFromSelf && r.relation_type === "subsidiary_of") relLabel = "上级公司";
      else if (!isFromSelf && r.relation_type === "works_at") relLabel = "相关人员";
      let otherName = otherId;
      let onClick = null;
      if (otherKind === "person") {
        const peer = abState.peopleById.get(otherId);
        otherName = peer ? peer.name : otherId;
        onClick = () => { abState.tab = "people"; loadPersonDetail(otherId); };
      } else if (otherKind === "org") {
        const peerOrg = abState.orgsById.get(otherId);
        otherName = peerOrg ? peerOrg.name : otherId;
        onClick = () => loadOrgDetail(otherId);
      } else if (otherKind === "matter") {
        const mat = abState.mattersById.get(otherId);
        otherName = mat && mat.data ? mat.data.title : otherId;
        if (mat) onClick = () => { abState.tab = "matters"; loadMatterDetail(otherId); };
      }
      const chip = el(onClick ? "button" : "div", null, "ab-rel-pill " + otherKind + (onClick ? " clickable" : ""));
      if (onClick) { chip.type = "button"; chip.addEventListener("click", onClick); }
      chip.append(el("span", relLabel, "ab-rel-type"), el("strong", otherName, "ab-rel-target"));
      chips.appendChild(chip);
    }
    relSec.appendChild(chips);
    pane.appendChild(relSec);
  }

  if (records.length) {
    const recSec = el("div", null, "ab-section");
    recSec.append(el("h4", `相关记录（${records.length}）`, "ab-sec-title"), buildTimelineNodes(records));
    pane.appendChild(recSec);
  }
  return pane;
}

function buildMatterDetailView(pane, matter) {
  const d = matter.data || {};
  const hero = el("div", null, "ab-hero");
  hero.appendChild(abAvatarNode(d.title || "项", "lg org", null));
  const heroMain = el("div", null, "ab-hero-main");
  const nameRow = el("div", null, "ab-hero-name-row");
  nameRow.append(
    el("h3", d.title || "未命名项目", "ab-hero-name"),
    el("span", AB_MATTER_STATUS[d.status] || "进行中", "ab-tag accent")
  );
  heroMain.appendChild(nameRow);
  hero.appendChild(heroMain);
  const actions = el("div", null, "ab-hero-actions");
  if (typeof openEntityInGraph === "function") {
    actions.appendChild(button("关系图", () => openEntityInGraph("matter", matter.id), "ab-act-btn"));
  }
  actions.appendChild(button("问管家", () => {
    closeContactsDrawer();
    const box = $("message");
    if (box) { box.value = `梳理一下「${d.title}」的关联人物、历史会议记录和下一步待办`; box.focus(); }
  }, "ab-act-btn primary-soft"));
  hero.appendChild(actions);
  pane.appendChild(hero);

  if (d.description) {
    const bio = el("div", null, "ab-section");
    bio.append(el("h4", "项目背景", "ab-sec-title"), el("p", d.description, "ab-bio"));
    pane.appendChild(bio);
  }

  const peopleSec = el("div", null, "ab-section");
  peopleSec.appendChild(el("h4", `关联人物（${abState.matterPeople.length}）`, "ab-sec-title"));
  if (abState.loadingDetail) {
    peopleSec.appendChild(el("p", "读取中…", "ab-muted"));
  } else if (!abState.matterPeople.length) {
    peopleSec.appendChild(el("p", "暂无关联人物。", "ab-muted"));
  } else {
    const chips = el("div", null, "ab-rel-grid");
    for (const item of abState.matterPeople) {
      const p = item.person || item;
      const pid = p.id || item.person_id;
      const role = (item.relation && item.relation.role) || item.role || "";
      const chip = button("", () => { abState.tab = "people"; loadPersonDetail(pid); }, "ab-rel-pill person clickable");
      chip.append(el("span", "人物", "ab-rel-type"), el("strong", p.name || pid, "ab-rel-target"));
      if (role) chip.appendChild(el("span", role, "ab-rel-role"));
      chips.appendChild(chip);
    }
    peopleSec.appendChild(chips);
  }
  pane.appendChild(peopleSec);

  const linkedNotes = abState.notes.filter((n) => n.matter_id === matter.id || (n.matter_ids || []).includes(matter.id));
  const noteSec = el("div", null, "ab-section");
  noteSec.appendChild(el("h4", `关联记录（${linkedNotes.length}）`, "ab-sec-title"));
  if (!linkedNotes.length) noteSec.appendChild(el("p", "暂无关联记录。", "ab-muted"));
  else noteSec.appendChild(buildTimelineNodes(linkedNotes));
  pane.appendChild(noteSec);
  return pane;
}

function buildMatterForm(pane) {
  pane.appendChild(el("h3", "新建项目事项", "ab-form-title"));
  const form = el("form", null, "ab-form");
  const grid = el("div", null, "pcard-grid");
  const titleIn = document.createElement("input");
  titleIn.type = "text";
  titleIn.placeholder = "如：壁仞算力合作项目";
  grid.appendChild(kgField("项目名称 *", titleIn, true));
  const descArea = document.createElement("textarea");
  descArea.rows = 3;
  descArea.placeholder = "项目背景、目标或关键说明";
  grid.appendChild(kgField("项目说明", descArea, true));
  form.appendChild(grid);
  const foot = el("div", null, "ab-form-actions");
  const saveBtn = button("创建项目", null, "primary");
  saveBtn.type = "submit";
  foot.append(saveBtn, button("取消", () => { abState.editing = null; renderContactsDrawer(); }));
  form.appendChild(foot);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = titleIn.value.trim();
    if (!title) { abState.error = "请填写项目名称"; renderContactsDrawer(); return; }
    saveBtn.disabled = true;
    try {
      const saved = await entryApi("POST", "/v1/workspace/items", {
        json: {
          kind: "matter",
          request_id: "ab_mat_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
          data: { title, description: descArea.value.trim() },
        },
      });
      abState.editing = null;
      abState.selectedMatterId = saved.id;
      abState.status = `已新建项目「${title}」`;
      abNotifyWorld();
      await refreshContactsData();
    } catch (error) {
      saveBtn.disabled = false;
      abState.error = (error && error.message) || "新建项目失败";
      renderContactsDrawer();
    }
  });
  pane.appendChild(form);
  return pane;
}

function buildNoteDetailView(pane, note) {
  const hero = el("div", null, "ab-hero");
  const heroMain = el("div", null, "ab-hero-main");
  const topRow = el("div", null, "ab-hero-name-row");
  topRow.append(
    el("span", AB_RECORD_LABELS[note.record_kind] || "记录", "ab-note-kind"),
    el("h3", note.title || "原始记录", "ab-hero-name")
  );
  heroMain.appendChild(topRow);
  const meta = [abFormatDate(note.occurred_at), note.location].filter(Boolean).join(" · ");
  if (meta) heroMain.appendChild(el("div", meta, "ab-hero-sub"));
  hero.appendChild(heroMain);
  pane.appendChild(hero);

  if (note.summary) {
    const sec = el("div", null, "ab-section");
    sec.append(el("h4", "摘要", "ab-sec-title"), el("p", note.summary, "ab-bio"));
    pane.appendChild(sec);
  }
  const contentSec = el("div", null, "ab-section");
  contentSec.append(el("h4", "原始全文", "ab-sec-title"), el("p", note.content, "ab-bio"));
  pane.appendChild(contentSec);

  if ((note.decisions || []).length) {
    const sec = el("div", null, "ab-section");
    sec.appendChild(el("h4", "关键结论", "ab-sec-title"));
    const ul = el("ul", null, "msg-list");
    for (const d of note.decisions) ul.appendChild(el("li", d));
    sec.appendChild(ul);
    pane.appendChild(sec);
  }
  if ((note.open_questions || []).length) {
    const sec = el("div", null, "ab-section");
    sec.appendChild(el("h4", "待确认问题", "ab-sec-title"));
    const ul = el("ul", null, "msg-list");
    for (const q of note.open_questions) ul.appendChild(el("li", q));
    sec.appendChild(ul);
    pane.appendChild(sec);
  }

  // 关联实体双向跳转
  const pids = note.person_ids || [];
  const oids = note.org_ids || [];
  const mids = [...new Set([...(note.matter_ids || []), ...(note.matter_id ? [note.matter_id] : [])])];
  if (pids.length || oids.length || mids.length) {
    const sec = el("div", null, "ab-section");
    sec.appendChild(el("h4", "关联实体（点击直达）", "ab-sec-title"));
    const chips = el("div", null, "ab-rel-grid");
    for (const pid of pids) {
      const p = abState.peopleById.get(pid);
      const chip = button("", () => { abState.tab = "people"; loadPersonDetail(pid); }, "ab-rel-pill person clickable");
      chip.append(el("span", "人物", "ab-rel-type"), el("strong", p ? p.name : pid, "ab-rel-target"));
      chips.appendChild(chip);
    }
    for (const oid of oids) {
      const o = abState.orgsById.get(oid);
      const chip = button("", () => { abState.tab = "orgs"; loadOrgDetail(oid); }, "ab-rel-pill org clickable");
      chip.append(el("span", "公司", "ab-rel-type"), el("strong", o ? o.name : oid, "ab-rel-target"));
      chips.appendChild(chip);
    }
    for (const mid of mids) {
      const m = abState.mattersById.get(mid);
      const chip = button("", () => { abState.tab = "matters"; loadMatterDetail(mid); }, "ab-rel-pill matter clickable");
      chip.append(el("span", "项目", "ab-rel-type"), el("strong", m && m.data ? m.data.title : mid, "ab-rel-target"));
      chips.appendChild(chip);
    }
    sec.appendChild(chips);
    pane.appendChild(sec);
  }
  return pane;
}

function buildFileDetailView(pane, file) {
  const d = file.data || {};
  const hero = el("div", null, "ab-hero");
  hero.appendChild(abAvatarNode("文", "lg org", null));
  const heroMain = el("div", null, "ab-hero-main");
  heroMain.append(
    el("h3", d.name || "未命名文件", "ab-hero-name"),
    el("div", [d.mime_type, abFormatSize(d.size), d.current_revision ? "版本 v" + d.current_revision : ""].filter(Boolean).join(" · "), "ab-hero-sub")
  );
  hero.appendChild(heroMain);
  const actions = el("div", null, "ab-hero-actions");
  const dl = el("a", "下载原件", "ab-act-btn primary-soft");
  dl.href = "/v1/workspace/files/" + encodeURIComponent(file.id) + "/content";
  dl.rel = "noopener noreferrer";
  dl.setAttribute("download", d.name || "");
  if (typeof openAttachmentViewer === "function") {
    actions.append(button("预览内容", () => { openAttachmentViewer(file).catch(report); }, "ab-act-btn"));
  }
  actions.append(
    dl,
    button("让管家总结", () => {
      closeContactsDrawer();
      const box = $("message");
      if (box) { box.value = `帮我读取工作台文件「${d.name || file.id}」并整理要点`; box.focus(); }
    }, "ab-act-btn")
  );
  hero.appendChild(actions);
  pane.appendChild(hero);
  if (d.matter_id && abState.mattersById.has(d.matter_id)) {
    const m = abState.mattersById.get(d.matter_id);
    const sec = el("div", null, "ab-section");
    sec.appendChild(el("h4", "所属项目", "ab-sec-title"));
    const chip = button("", () => { abState.tab = "matters"; loadMatterDetail(d.matter_id); }, "ab-rel-pill matter clickable");
    chip.append(el("span", "项目", "ab-rel-type"), el("strong", (m.data && m.data.title) || d.matter_id, "ab-rel-target"));
    sec.appendChild(chip);
    pane.appendChild(sec);
  }
  return pane;
}

function buildKnowledgePane() {
  const pane = el("section", null, "ab-detail-pane");
  const form = el("form", null, "ab-kb-search");
  const inp = document.createElement("input");
  inp.id = "ab-kb-input";
  inp.type = "search";
  inp.className = "ab-search";
  inp.placeholder = "检索个人知识库…";
  inp.value = abState.knowledgeQuery;
  inp.addEventListener("input", () => { abState.knowledgeQuery = inp.value; });
  const go = button("检索", null, "primary ab-mini-btn");
  go.type = "submit";
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = inp.value.trim();
    if (!q) return;
    go.disabled = true;
    try {
      const res = await entryApi("POST", "/v1/me/knowledge/search", { json: { query: q, limit: 10 } });
      abState.knowledgeHits = res.items || [];
      renderContactsDrawer();
    } catch (error) {
      abState.error = (error && error.message) || "知识库检索失败";
      renderContactsDrawer();
    } finally { go.disabled = false; }
  });
  form.append(inp, go);
  pane.appendChild(form);

  if (abState.knowledgeHits.length) {
    const hitSec = el("div", null, "ab-section");
    hitSec.appendChild(el("h4", `检索结果（${abState.knowledgeHits.length}）`, "ab-sec-title"));
    const list = el("div", null, "ab-timeline");
    for (const h of abState.knowledgeHits) {
      const item = el("article", null, "ab-note-item");
      item.append(el("strong", h.title || h.source_title || "知识片段", "ab-note-title"), el("p", h.snippet || h.text || h.content || "", "ab-note-body"));
      list.appendChild(item);
    }
    hitSec.appendChild(list);
    pane.appendChild(hitSec);
  }

  const memSec = el("div", null, "ab-section");
  memSec.appendChild(el("h4", `已确认工作记忆（${abState.workMemories.length}）`, "ab-sec-title"));
  if (!abState.workMemories.length) {
    memSec.appendChild(el("p", "暂无已确认的工作记忆。", "ab-muted"));
  } else {
    const list = el("div", null, "ab-timeline");
    for (const m of abState.workMemories.slice(0, 20)) {
      const item = el("article", null, "ab-note-item");
      item.append(el("strong", m.title || m.category || "记忆", "ab-note-title"), el("p", m.content || m.summary || "", "ab-note-body"));
      list.appendChild(item);
    }
    memSec.appendChild(list);
  }
  pane.appendChild(memSec);

  const srcSec = el("div", null, "ab-section");
  srcSec.appendChild(el("h4", `知识来源（${abState.knowledgeSources.length}）`, "ab-sec-title"));
  if (!abState.knowledgeSources.length) {
    srcSec.appendChild(el("p", "暂无知识来源。", "ab-muted"));
  } else {
    const chips = el("div", null, "ab-rel-grid");
    for (const s of abState.knowledgeSources.slice(0, 20)) {
      const chip = el("div", null, "ab-rel-pill");
      chip.append(el("span", s.kind || "来源", "ab-rel-type"), el("strong", s.title || s.name || s.id, "ab-rel-target"));
      chips.appendChild(chip);
    }
    srcSec.appendChild(chips);
  }
  pane.appendChild(srcSec);
  return pane;
}

function buildPersonForm(pane, existing) {
  const isEdit = Boolean(existing);
  const prof = (existing && existing.profile) || {};
  const contacts = prof.contacts || [];
  const firstOf = (k) => (contacts.find((c) => c.kind === k) || {}).value || "";
  const preservedContacts = contacts.filter((c) => !["phone", "wechat", "email"].includes(c.kind));
  const extraCopy = { ...(prof.extra || {}) };
  const initAddr = extraCopy.address || extraCopy["地址"] || "";
  delete extraCopy.address;
  delete extraCopy["地址"];

  pane.appendChild(el("h3", isEdit ? `编辑「${existing.name}」` : "新建联系人", "ab-form-title"));
  const form = el("form", null, "ab-form");
  const grid = el("div", null, "pcard-grid");

  const mkInput = (label, value, placeholder, full = false) => {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = value || "";
    if (placeholder) inp.placeholder = placeholder;
    grid.appendChild(kgField(label, inp, full));
    return inp;
  };

  const nameIn = mkInput("姓名 *", prof.name, "必填");
  const aliasIn = mkInput("别名 / 称呼", (prof.aliases || []).join("，"), "如 何总");
  const orgIn = mkInput("公司 / 单位", prof.organization || (existing && existing.organization), "规范公司名称", true);
  const deptIn = mkInput("部门", prof.department, "可选");
  const titleIn = mkInput("职位", prof.title, "可选");
  const phoneIn = mkInput("手机号", firstOf("phone"), "手机或座机");
  const wechatIn = mkInput("微信", firstOf("wechat"), "微信号");
  const emailIn = mkInput("邮箱", firstOf("email"), "电子邮箱");
  const cityIn = mkInput("城市", prof.city, "如 上海");
  const addrIn = mkInput("地址", initAddr, "详细办公或联系地址", true);
  const tagsIn = mkInput("标签", (prof.tags || []).join("，"), "逗号分隔，如 客户，电信", true);

  const sumArea = document.createElement("textarea");
  sumArea.rows = 3;
  sumArea.value = prof.summary || "";
  sumArea.placeholder = "认识经过、合作背景或重要备注";
  grid.appendChild(kgField("备注", sumArea, true));

  form.appendChild(grid);
  const foot = el("div", null, "ab-form-actions");
  const saveBtn = button(isEdit ? "保存修改" : "创建联系人", null, "primary");
  saveBtn.type = "submit";
  foot.append(saveBtn, button("取消", () => { abState.editing = null; renderContactsDrawer(); }));
  form.appendChild(foot);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = nameIn.value.trim();
    if (!name) { abState.error = "请填写联系人姓名"; renderContactsDrawer(); return; }
    saveBtn.disabled = true;
    const splitList = (v) => v.trim() ? v.trim().split(/[,，、]+/).map((s) => s.trim()).filter(Boolean) : [];
    const nextContacts = [...preservedContacts];
    if (phoneIn.value.trim()) nextContacts.push({ kind: "phone", value: phoneIn.value.trim(), label: "手机", primary: true });
    if (wechatIn.value.trim()) nextContacts.push({ kind: "wechat", value: wechatIn.value.trim(), label: "微信", primary: false });
    if (emailIn.value.trim()) nextContacts.push({ kind: "email", value: emailIn.value.trim(), label: "邮箱", primary: false });
    const nextExtra = { ...extraCopy };
    if (addrIn.value.trim()) nextExtra.address = addrIn.value.trim();

    const profile = {
      name, aliases: splitList(aliasIn.value), gender: prof.gender || "unknown", birth: prof.birth || null,
      organization: orgIn.value.trim(), department: deptIn.value.trim(), title: titleIn.value.trim(),
      city: cityIn.value.trim(), contacts: nextContacts, tags: splitList(tagsIn.value),
      summary: sumArea.value.trim(), attachment_file_ids: prof.attachment_file_ids || [], extra: nextExtra,
    };
    try {
      let saved;
      if (isEdit) {
        saved = await entryApi("PUT", "/v1/people/" + encodeURIComponent(existing.id), { json: { profile, version: existing.version } });
      } else {
        saved = await entryApi("POST", "/v1/people", { json: { profile, request_id: "ab_new_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7) } });
      }
      abState.editing = null;
      abState.selectedPersonId = saved.id;
      abState.status = isEdit ? `已更新「${saved.name}」` : `已新建联系人「${saved.name}」`;
      abNotifyWorld();
      await refreshContactsData();
    } catch (error) {
      saveBtn.disabled = false;
      abState.error = (error && error.message) || "保存失败";
      renderContactsDrawer();
    }
  });

  pane.appendChild(form);
  return pane;
}

function abInitImportDecisions(preview) {
  return (preview.items || []).map((item) => {
    const firstCand = (item.candidates || [])[0];
    if (item.suggested_action === "merge" && firstCand) {
      return { action: "merge", target_person_id: firstCand.person_id, target_version: firstCand.version };
    }
    if (item.suggested_action === "skip") {
      return { action: "skip", target_person_id: null, target_version: null };
    }
    return { action: "create", target_person_id: null, target_version: null };
  });
}

async function abLoadGoogleStatus() {
  const st = abState.importState;
  if (!entry.me || !st) return;
  const epoch = entry.epoch;
  st.google = { ...(st.google || {}), loading: true };
  renderContactsDrawer();
  try {
    const data = await entryApi("GET", "/v1/people/google/status");
    if (isStale(epoch) || !abState.open || abState.importState !== st) return;
    st.google = { loading: false, busy: false, data };
    renderContactsDrawer();
  } catch {
    if (isStale(epoch) || !abState.open || abState.importState !== st) return;
    st.google = { loading: false, busy: false, data: { enabled: false, configured: false, linked: false } };
    renderContactsDrawer();
  }
}

async function abStartGoogleConnect() {
  const st = abState.importState;
  if (!entry.me || !st || !st.google || st.google.busy) return;
  const epoch = entry.epoch;
  st.google.busy = true;
  abState.error = "";
  renderContactsDrawer();
  try {
    const res = await entryApi("POST", "/v1/people/google/connect/start", {
      json: { return_to: "/" },
    });
    if (isStale(epoch) || !abState.open) return;
    if (res && res.authorization_url && typeof location !== "undefined" && typeof location.assign === "function") {
      location.assign(res.authorization_url);
      return;
    }
    st.google.busy = false;
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.google.busy = false;
    abState.error = (error && error.message) || "无法发起 Google 账号关联";
    renderContactsDrawer();
  }
}

async function abPreviewGoogleContacts(fullSync = false) {
  const st = abState.importState;
  if (!entry.me || !st || !st.google || st.google.busy) return;
  const epoch = entry.epoch;
  st.source = "google";
  st.parsing = true;
  st.google.busy = true;
  st.preview = null;
  st.decisions = [];
  abState.error = "";
  abState.status = "";
  renderContactsDrawer();
  try {
    const preview = await entryApi("POST", "/v1/people/google/preview", {
      json: { full_sync: Boolean(fullSync) },
    });
    if (isStale(epoch) || !abState.open || abState.importState !== st) return;
    st.parsing = false;
    st.google.busy = false;
    st.preview = preview;
    st.decisions = abInitImportDecisions(preview);
    st.requestId = "ab_gsync_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.parsing = false;
    st.google.busy = false;
    abState.error = (error && error.message) || "拉取 Google 通讯录失败";
    renderContactsDrawer();
  }
}

async function abAutoSyncGoogle() {
  const st = abState.importState;
  if (!entry.me || !st || !st.google || st.google.busy) return;
  const epoch = entry.epoch;
  st.google.busy = true;
  abState.error = "";
  renderContactsDrawer();
  try {
    const result = await entryApi("POST", "/v1/people/google/sync", {
      json: {
        mode: "auto",
        request_id: "ab_gauto_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
      },
    });
    if (isStale(epoch) || !abState.open) return;
    const first = (result.created && result.created[0]) || (result.merged && result.merged[0]) || null;
    abState.editing = null;
    abState.importState = null;
    if (first && first.id) abState.selectedPersonId = first.id;
    const parts = [`新建 ${result.created_count || 0} 人`];
    if (result.merged_count) parts.push(`合并更新 ${result.merged_count} 人`);
    const doneMsg = (result.created_count || result.merged_count)
      ? ("Google 同步完成：" + parts.join("，"))
      : "Google 通讯录已是最新，无新增变更";
    abNotifyWorld();
    await refreshContactsData();
    if (!isStale(epoch) && abState.open) {
      abState.status = doneMsg;
      renderContactsDrawer();
    }
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.google.busy = false;
    abState.error = (error && error.message) || "Google 增量同步失败";
    renderContactsDrawer();
  }
}

async function abUnlinkGoogle() {
  const st = abState.importState;
  if (!entry.me || !st || !st.google || st.google.busy) return;
  const epoch = entry.epoch;
  st.google.busy = true;
  abState.error = "";
  renderContactsDrawer();
  try {
    await entryApi("POST", "/v1/people/google/unlink", { json: {} });
    if (isStale(epoch) || !abState.open || abState.importState !== st) return;
    st.preview = st.source === "google" ? null : st.preview;
    abState.status = "已解除 Google 账号关联（已导入的本地联系人已保留）";
    await abLoadGoogleStatus();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.google.busy = false;
    abState.error = (error && error.message) || "解除关联失败";
    renderContactsDrawer();
  }
}

async function abUploadContactsFile(file) {
  if (!entry.me || !file) return;
  const epoch = entry.epoch;
  const st = abState.importState || { source: "file", filename: "", parsing: false, committing: false, preview: null, decisions: [], requestId: "", google: { loading: false, busy: false, data: null } };
  st.source = "file";
  st.filename = file.name || "contacts";
  st.parsing = true;
  st.preview = null;
  st.decisions = [];
  st.requestId = "";
  abState.importState = st;
  abState.error = "";
  abState.status = "";
  renderContactsDrawer();
  try {
    const params = new URLSearchParams({ filename: st.filename, format: "auto" });
    const response = await fetch("/v1/people/import/preview?" + params, {
      method: "POST",
      credentials: "same-origin",
      signal: entry.controller.signal,
      headers: {
        "X-Requested-By": "xuanpu",
        "Accept-Language": preferredLanguages(),
        "Content-Type": file.type || "text/plain",
      },
      body: file,
    });
    const preview = await readJson(response, epoch);
    if (isStale(epoch) || !abState.open || abState.editing !== "import") return;
    st.parsing = false;
    st.preview = preview;
    st.decisions = abInitImportDecisions(preview);
    st.requestId = "ab_imp_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.parsing = false;
    abState.error = (error && error.message) || "通讯录文件解析失败";
    renderContactsDrawer();
  }
}

async function abCommitImport() {
  const st = abState.importState;
  if (!entry.me || !st || !st.preview || st.committing) return;
  const epoch = entry.epoch;
  const isGoogle = st.source === "google";
  const items = (st.preview.items || []).map((row, idx) => {
    const dec = st.decisions[idx] || { action: "skip" };
    const payload = { action: dec.action, profile: row.profile };
    if (isGoogle) {
      payload.resource_name = row.resource_name || "";
      payload.etag = row.etag || "";
    }
    if (dec.action === "merge") {
      payload.target_person_id = dec.target_person_id;
      payload.target_version = dec.target_version;
    }
    return payload;
  });
  const hasActionable = items.some((i) => i.action !== "skip");
  if (!hasActionable && !(isGoogle && st.preview.next_sync_token)) return;
  st.committing = true;
  abState.error = "";
  renderContactsDrawer();
  try {
    const reqId = st.requestId || ((isGoogle ? "ab_gsync_" : "ab_imp_") + Date.now());
    const result = isGoogle
      ? await entryApi("POST", "/v1/people/google/sync", {
        json: {
          mode: "preview",
          request_id: reqId,
          items,
          next_sync_token: st.preview.next_sync_token || null,
          deleted_resource_names: st.preview.deleted_resource_names || [],
        },
      })
      : await entryApi("POST", "/v1/people/import", {
        json: { request_id: reqId, items },
      });
    if (isStale(epoch) || !abState.open) return;
    const first = (result.created && result.created[0]) || (result.merged && result.merged[0]) || null;
    abState.editing = null;
    abState.importState = null;
    if (first && first.id) abState.selectedPersonId = first.id;
    const parts = [`新建 ${result.created_count || 0} 人`];
    if (result.merged_count) parts.push(`合并更新 ${result.merged_count} 人`);
    if (result.skipped_count) parts.push(`跳过 ${result.skipped_count} 人`);
    const doneMsg = (isGoogle ? "Google 同步完成：" : "导入完成：") + parts.join("，");
    abNotifyWorld();
    await refreshContactsData();
    if (!isStale(epoch) && abState.open) {
      abState.status = doneMsg;
      renderContactsDrawer();
    }
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError")) return;
    st.committing = false;
    abState.error = (error && error.message) || "通讯录导入失败";
    renderContactsDrawer();
  }
}

function buildGoogleConnectCard(st) {
  const card = el("div", null, "ab-google-card");
  const head = el("div", null, "ab-google-head");
  const titleBox = el("div", null, "ab-row-top");
  titleBox.appendChild(el("strong", "Google 通讯录关联", "ab-row-name"));
  const g = st.google || { loading: false, busy: false, data: null };
  const data = g.data;
  if (g.loading) {
    titleBox.appendChild(el("span", "检查中…", "ab-tag muted"));
  } else if (data && data.linked) {
    titleBox.appendChild(el("span", "已关联", "ab-tag accent"));
  } else if (data && data.configured) {
    titleBox.appendChild(el("span", "未关联", "ab-tag muted"));
  } else {
    titleBox.appendChild(el("span", "未配置", "ab-tag muted"));
  }
  head.appendChild(titleBox);
  card.appendChild(head);

  if (g.loading) {
    card.appendChild(el("p", "正在读取 Google 通讯录关联状态…", "ab-muted"));
    return card;
  }
  if (!data || !data.configured) {
    card.appendChild(el(
      "p",
      "当前环境尚未配置 Google OAuth 凭据（不会主动连接外网）。管理员在服务端启用 google_contacts 并填入 client_id / client_secret 后即可关联并同步；现在可直接使用下方 .vcf / .csv 文件导入。",
      "ab-muted"
    ));
    const btn = button("关联 Google 账号（未配置）", () => {}, "ab-mini-btn");
    btn.disabled = true;
    card.appendChild(btn);
    return card;
  }

  if (!data.linked) {
    card.appendChild(el(
      "p",
      "通过只读 OAuth 授权关联你的 Google 账号，拉取 Google Contacts 预览查重并支持后续增量同步。",
      "ab-muted"
    ));
    const btn = button(g.busy ? "正在跳转授权…" : "关联 Google 账号", () => abStartGoogleConnect(), "primary ab-mini-btn");
    btn.id = "ab-google-connect";
    btn.disabled = g.busy || st.committing;
    card.appendChild(btn);
    return card;
  }

  const metaParts = [data.account_email || data.account_name || "Google 账号"];
  if (data.synced_count) metaParts.push(`已映射 ${data.synced_count} 人`);
  if (data.last_synced_at) metaParts.push(`上次同步 ${abFormatDate(data.last_synced_at)}`);
  card.appendChild(el("p", metaParts.join(" · "), "ab-muted"));
  const acts = el("div", null, "ab-import-bulk");
  const pvBtn = button(g.busy ? "正在拉取…" : "拉取预览", () => abPreviewGoogleContacts(false), "primary ab-mini-btn");
  pvBtn.id = "ab-google-preview";
  pvBtn.disabled = g.busy || st.committing;
  const syncBtn = button("一键增量同步", () => abAutoSyncGoogle(), "ab-mini-btn");
  syncBtn.id = "ab-google-sync";
  syncBtn.disabled = g.busy || st.committing;
  const unlinkBtn = button("解除关联", () => abUnlinkGoogle(), "ab-mini-btn");
  unlinkBtn.id = "ab-google-unlink";
  unlinkBtn.disabled = g.busy || st.committing;
  acts.append(pvBtn, syncBtn, unlinkBtn);
  card.appendChild(acts);
  return card;
}

function buildImportPane(pane) {
  const st = abState.importState || { source: "file", filename: "", parsing: false, committing: false, preview: null, decisions: [], requestId: "", google: { loading: false, busy: false, data: null } };
  abState.importState = st;

  pane.appendChild(el("h3", "导入通讯录 / 外部账号关联", "ab-form-title"));
  pane.appendChild(buildGoogleConnectCard(st));

  pane.appendChild(el("h4", "本地文件导入（.vcf / .csv）", "ab-sec-title"));
  pane.appendChild(el(
    "p",
    "支持 Google Contacts、Apple iCloud、Outlook 等导出的 .vcf（vCard）或 .csv 文件（最大 5 MiB，单次最多 1,000 人）。上传后先预览查重，确认后再写入。",
    "ab-muted"
  ));

  const pickerRow = el("div", null, "ab-import-picker");
  const fileInput = document.createElement("input");
  fileInput.id = "ab-import-file";
  fileInput.type = "file";
  fileInput.accept = ".vcf,.vcard,.csv,text/vcard,text/x-vcard,text/csv";
  fileInput.className = "hidden";
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) abUploadContactsFile(file);
    fileInput.value = "";
  });
  const pickBtn = button(st.preview && st.source === "file" ? "重新选择文件" : "选择 .vcf / .csv 文件", () => fileInput.click(), "primary ab-mini-btn");
  pickBtn.disabled = st.parsing || st.committing;
  pickerRow.append(fileInput, pickBtn);
  if (st.filename && st.source === "file") pickerRow.appendChild(el("span", st.filename, "ab-muted"));
  pane.appendChild(pickerRow);

  if (st.parsing) {
    pane.appendChild(el("p", st.source === "google" ? "正在拉取 Google 通讯录并与现有联系人查重…" : "正在解析通讯录文件并与现有联系人查重…", "ab-empty"));
    return pane;
  }
  if (!st.preview) {
    const foot = el("div", null, "ab-form-actions");
    foot.appendChild(button("取消", () => { abState.editing = null; abState.importState = null; renderContactsDrawer(); }));
    pane.appendChild(foot);
    return pane;
  }

  const pv = st.preview;
  const rows = pv.items || [];
  let createCnt = 0;
  let mergeCnt = 0;
  let skipCnt = 0;
  for (const dec of st.decisions) {
    if (dec.action === "create") createCnt += 1;
    else if (dec.action === "merge") mergeCnt += 1;
    else skipCnt += 1;
  }

  const summary = el("div", null, "ab-import-summary");
  const summaryText = st.source === "google"
    ? `Google 通讯录${pv.is_incremental ? "（增量）" : "（全量）"} · 获取 ${pv.valid_count || 0} 人（新增 ${pv.new_count || 0} · 已关联更新 ${pv.linked_count || 0} · 疑似重复 ${pv.duplicate_count || 0}）· 剩余配额 ${pv.remaining_quota ?? "-"}`
    : `格式 ${(pv.format || "").toUpperCase()} · 解析 ${pv.valid_count || 0} 人（新增 ${pv.new_count || 0} · 疑似重复 ${pv.duplicate_count || 0} · 无效跳过 ${pv.skipped_count || 0}）· 剩余配额 ${pv.remaining_quota ?? "-"}`;
  summary.appendChild(el("div", summaryText, "ab-import-stats"));
  if (rows.length) {
    const bulk = el("div", null, "ab-import-bulk");
    bulk.append(
      button("重复项默认合并", () => {
        st.decisions = abInitImportDecisions(pv);
        renderContactsDrawer();
      }, "ab-mini-btn"),
      button("重复项全部跳过", () => {
        st.decisions = rows.map((r) => (r.status === "new"
          ? { action: "create", target_person_id: null, target_version: null }
          : { action: "skip", target_person_id: null, target_version: null }));
        renderContactsDrawer();
      }, "ab-mini-btn"),
      button("全部作为新联系人", () => {
        st.decisions = rows.map(() => ({ action: "create", target_person_id: null, target_version: null }));
        renderContactsDrawer();
      }, "ab-mini-btn")
    );
    summary.appendChild(bulk);
  }
  pane.appendChild(summary);

  if ((pv.warnings || []).length) {
    const warnBox = el("div", null, "ab-import-warnings");
    warnBox.appendChild(el("strong", `解析提示（${pv.warnings.length}）`, "ab-sec-title"));
    for (const w of pv.warnings.slice(0, 5)) warnBox.appendChild(el("div", w, "ab-muted"));
    if (pv.warnings.length > 5) warnBox.appendChild(el("div", `另有 ${pv.warnings.length - 5} 条提示…`, "ab-muted"));
    pane.appendChild(warnBox);
  }

  if (!rows.length) {
    pane.appendChild(el("p", st.source === "google" ? "没有新的 Google 联系人变更。" : "文件中没有可导入的有效联系人。", "ab-empty"));
  } else {
    const list = el("div", null, "ab-import-list");
    rows.forEach((row, idx) => {
      const prof = row.profile || {};
      const dec = st.decisions[idx] || { action: "skip" };
      const itemEl = el("div", null, "ab-import-row" + (dec.action === "skip" ? " skipped" : ""));
      const info = el("div", null, "ab-import-info");
      const top = el("div", null, "ab-row-top");
      top.appendChild(el("strong", prof.name || "未命名", "ab-row-name"));
      if (row.status === "linked") {
        top.appendChild(el("span", "已关联更新", "ab-tag accent"));
      } else if (row.status === "duplicate") {
        top.appendChild(el("span", "疑似重复", "ab-tag warn"));
      } else if (row.status === "duplicate_in_file") {
        top.appendChild(el("span", "文件内重复", "ab-tag warn"));
      } else {
        top.appendChild(el("span", "新增", "ab-tag accent"));
      }
      info.appendChild(top);
      const subParts = [
        prof.organization,
        prof.title,
        ...((prof.contacts || []).slice(0, 2).map((c) => c.value)),
      ].filter(Boolean);
      if (subParts.length) info.appendChild(el("div", subParts.join(" · "), "ab-row-sub"));
      if ((row.warnings || []).length) info.appendChild(el("div", row.warnings.join("；"), "ab-import-row-warn"));

      const sel = document.createElement("select");
      sel.className = "ab-import-select";
      sel.setAttribute("aria-label", `处理 ${prof.name || idx + 1}`);
      const addOpt = (label, value) => {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        sel.appendChild(opt);
      };
      for (const cand of row.candidates || []) {
        const label = `合并到：${cand.name}${cand.organization ? "（" + cand.organization + "）" : ""}`;
        addOpt(label, `merge:${cand.person_id}:${cand.version}`);
      }
      addOpt((row.candidates || []).length ? "仍作为新联系人新建" : "新建联系人", "create");
      addOpt("跳过不导入", "skip");
      sel.value = dec.action === "merge"
        ? `merge:${dec.target_person_id}:${dec.target_version}`
        : dec.action;
      sel.disabled = st.committing;
      sel.addEventListener("change", () => {
        const val = sel.value;
        if (val.startsWith("merge:")) {
          const [, pid, ver] = val.split(":");
          st.decisions[idx] = { action: "merge", target_person_id: pid, target_version: Number(ver) };
        } else {
          st.decisions[idx] = { action: val, target_person_id: null, target_version: null };
        }
        renderContactsDrawer();
      });

      itemEl.append(info, sel);
      list.appendChild(itemEl);
    });
    pane.appendChild(list);
  }

  const foot = el("div", null, "ab-form-actions");
  const verb = st.source === "google" ? "确认同步" : "确认导入";
  const commitLabel = st.committing
    ? "正在写入…"
    : `${verb}（新建 ${createCnt} · 合并 ${mergeCnt}${skipCnt ? " · 跳过 " + skipCnt : ""}）`;
  const commitBtn = button(commitLabel, () => abCommitImport(), "primary");
  commitBtn.id = "ab-import-commit";
  commitBtn.disabled = st.committing || (createCnt + mergeCnt === 0 && !(st.source === "google" && pv.next_sync_token));
  const cancelBtn = button("取消", () => { abState.editing = null; abState.importState = null; renderContactsDrawer(); });
  cancelBtn.disabled = st.committing;
  foot.append(commitBtn, cancelBtn);
  pane.appendChild(foot);
  return pane;
}
