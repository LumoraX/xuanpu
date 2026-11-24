// 能力市场（匠人市场 + 通用技能库市场）：按类型、分类与关键词浏览，支持自动依赖解析的一键安装与密钥引导。
"use strict";

const marketplaceState = {
  generation: 0,
  kind: "all",
  category: "",
  query: "",
  items: [],
  categories: [],
  counts: { all: 0, artisan: 0, skill_library: 0 },
  installing: new Set(),
};

function marketplaceLive(epoch, generation) {
  return epoch === conn.epoch && generation === marketplaceState.generation;
}

function marketplaceNotice(text, kind = "", missingSecrets = []) {
  const node = $("marketplace-notice");
  if (!node) return;
  node.replaceChildren();
  node.className = "status" + (kind ? " " + kind : "");
  if (text) node.append(document.createTextNode(text));
  if (Array.isArray(missingSecrets) && missingSecrets.length) {
    for (const secretName of missingSecrets) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost market-secret-jump";
      btn.textContent = `去配置密钥 ${secretName} →`;
      btn.addEventListener("click", () => jumpToSecretConfig(secretName));
      node.append(document.createTextNode(" "), btn);
    }
  }
}

function jumpToSecretConfig(secretName) {
  const tabs = $("tabs");
  const secretBtn = tabs && tabs.querySelector('button[data-tab="secrets"]');
  if (secretBtn && !secretBtn.classList.contains("hidden")) {
    for (const item of tabs.querySelectorAll("button")) item.classList.toggle("active", item === secretBtn);
    for (const section of document.querySelectorAll(".tab")) {
      section.classList.toggle("active", section.id === "tab-secrets");
    }
    if (typeof loadSecrets === "function") {
      loadSecrets(secretName).catch(fail);
    }
  }
}

function syncMarketKindTabs() {
  const host = $("marketplace-kinds");
  if (!host) return;
  const counts = marketplaceState.counts || {};
  const labels = {
    all: `全部 (${counts.all || 0})`,
    artisan: `匠人市场 (${counts.artisan || 0})`,
    skill_library: `Skill 市场 (${counts.skill_library || 0})`,
  };
  for (const btn of host.querySelectorAll("button[data-market-kind]")) {
    const k = btn.dataset.marketKind;
    btn.textContent = labels[k] || btn.textContent;
    btn.setAttribute("aria-pressed", String(k === marketplaceState.kind));
  }
}

function renderMarketCategories(categories) {
  const select = $("marketplace-category");
  if (!select) return;
  const current = marketplaceState.category;
  select.replaceChildren();
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "全部分类";
  select.appendChild(allOpt);
  for (const cat of Array.isArray(categories) ? categories : []) {
    const opt = document.createElement("option");
    opt.value = cat;
    opt.textContent = cat;
    select.appendChild(opt);
  }
  select.value = categories.includes(current) ? current : "";
}

function makeBadge(text, cls = "") {
  const span = document.createElement("span");
  span.className = "market-badge" + (cls ? " " + cls : "");
  span.textContent = text;
  return span;
}

function renderMarketCard(item) {
  const card = document.createElement("article");
  card.className = "function-card market-card";
  card.dataset.package = item.name;
  card.dataset.kind = item.kind;

  // 头部：类型标签 + 版本 + 安装状态
  const head = document.createElement("div");
  head.className = "market-card-head";
  const kindText = item.kind === "skill_library" ? "Skill 技能库" : "匠人";
  head.appendChild(makeBadge(kindText, item.kind === "skill_library" ? "kind-skill" : "kind-artisan"));
  head.appendChild(makeBadge("v" + item.version, "ver"));
  if (item.upgrade_available) {
    head.appendChild(makeBadge(`可升级（当前 v${item.installed_version}）`, "state-upgrade"));
  } else if (item.installed && item.active) {
    head.appendChild(makeBadge("已启用", "state-active"));
  } else if (item.installed) {
    head.appendChild(makeBadge("已安装未启用", "state-installed"));
  } else {
    head.appendChild(makeBadge("未安装", "state-none"));
  }
  card.appendChild(head);

  // 标题与技术包名
  const title = document.createElement("h3");
  title.textContent = item.display_name || item.name;
  card.appendChild(title);
  const sub = document.createElement("p");
  sub.className = "meta mono";
  sub.textContent = item.name + (item.source === "tenant" ? " · 租户自定义" : "");
  card.appendChild(sub);

  if (item.description) {
    const desc = document.createElement("p");
    desc.className = "market-desc";
    desc.textContent = item.description;
    card.appendChild(desc);
  }

  // 依赖的通用技能库
  if (Array.isArray(item.uses) && item.uses.length) {
    const deps = document.createElement("div");
    deps.className = "market-row";
    deps.appendChild(makeBadge("依赖技能库：" + item.uses.join("、"), "dep"));
    card.appendChild(deps);
  }

  // 包含的技能与分类（渐进摘要）
  if (Array.isArray(item.skills) && item.skills.length) {
    const skillsBox = document.createElement("div");
    skillsBox.className = "market-skills";
    const shown = item.skills.slice(0, 6);
    for (const sk of shown) {
      const label = sk.category ? `[${sk.category}] ${sk.display_name || sk.skill_id}` : (sk.display_name || sk.skill_id);
      skillsBox.appendChild(makeBadge(label, "skill-chip"));
    }
    if (item.skills.length > shown.length) {
      skillsBox.appendChild(makeBadge(`+${item.skills.length - shown.length} 项技能`, "skill-chip"));
    }
    card.appendChild(skillsBox);
  }

  // 所需密钥状态
  if (Array.isArray(item.secrets) && item.secrets.length) {
    const secRow = document.createElement("div");
    secRow.className = "market-row";
    for (const sec of item.secrets) {
      if (sec.configured) {
        secRow.appendChild(makeBadge(`密钥 ${sec.name} · 已配置`, "secret-ok"));
      } else {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "market-badge secret-missing";
        btn.textContent = `密钥 ${sec.name} · 待配置（去填）`;
        btn.addEventListener("click", () => jumpToSecretConfig(sec.name));
        secRow.appendChild(btn);
      }
    }
    card.appendChild(secRow);
  }

  // 底部安装操作
  const foot = document.createElement("div");
  foot.className = "market-card-foot";
  if (item.source === "catalog") {
    const btn = document.createElement("button");
    btn.type = "button";
    const busy = marketplaceState.installing.has(item.name);
    if (busy) {
      btn.textContent = "安装中…";
      btn.disabled = true;
    } else if (item.upgrade_available) {
      btn.textContent = `升级到 v${item.version}`;
    } else if (item.installed && item.active) {
      btn.textContent = "已启用";
      btn.className = "ghost";
      btn.disabled = true;
    } else if (item.installed) {
      btn.textContent = "立即启用";
    } else {
      btn.textContent = "一键安装";
    }
    btn.addEventListener("click", () => installMarketplaceItem(item));
    foot.appendChild(btn);
  } else {
    foot.appendChild(makeBadge("由租户手动发布", "meta"));
  }
  card.appendChild(foot);
  return card;
}

function renderMarketplaceGrid() {
  const grid = $("marketplace-grid");
  if (!grid) return;
  grid.replaceChildren();
  if (!marketplaceState.items.length) {
    const empty = document.createElement("p");
    empty.className = "status";
    empty.textContent = "当前筛选条件下没有可展示的匠人或技能库。";
    grid.appendChild(empty);
    return;
  }
  for (const item of marketplaceState.items) {
    grid.appendChild(renderMarketCard(item));
  }
}

async function loadMarketplace() {
  if (!conn.me) return;
  const epoch = conn.epoch;
  const generation = ++marketplaceState.generation;
  const params = new URLSearchParams();
  if (marketplaceState.kind && marketplaceState.kind !== "all") params.set("kind", marketplaceState.kind);
  if (marketplaceState.category) params.set("category", marketplaceState.category);
  if (marketplaceState.query) params.set("q", marketplaceState.query);
  const qs = params.toString();
  const data = await api("GET", "/v1/marketplace/items" + (qs ? "?" + qs : ""));
  if (!marketplaceLive(epoch, generation)) return;
  marketplaceState.items = Array.isArray(data.items) ? data.items : [];
  marketplaceState.categories = Array.isArray(data.categories) ? data.categories : [];
  marketplaceState.counts = data.counts || { all: marketplaceState.items.length, artisan: 0, skill_library: 0 };
  syncMarketKindTabs();
  renderMarketCategories(marketplaceState.categories);
  renderMarketplaceGrid();
}

async function installMarketplaceItem(item) {
  if (marketplaceState.installing.has(item.name)) return;
  const epoch = conn.epoch;
  const generation = marketplaceState.generation;
  marketplaceState.installing.add(item.name);
  renderMarketplaceGrid();
  marketplaceNotice(`正在安装「${item.display_name || item.name}」并解析依赖技能库…`);
  try {
    const result = await api("POST", "/v1/marketplace/install", {
      json: { name: item.name, activate: true },
    });
    if (epoch !== conn.epoch) return;
    const installedNames = (result.installed || [])
      .filter(x => x.status === "installed" || x.status === "activated")
      .map(x => `${x.display_name || x.name} v${x.version}`);
    const summary = installedNames.length
      ? `已完成安装并启用：${installedNames.join("、")}。`
      : `「${item.display_name || item.name}」已是最新启用状态。`;
    const missing = Array.isArray(result.missing_secrets) ? result.missing_secrets : [];
    marketplaceNotice(
      missing.length ? summary + "还需要配置以下密钥才能调用外部接口：" : summary,
      missing.length ? "" : "ok",
      missing,
    );
    if (typeof loadPackages === "function") await loadPackages().catch(() => {});
    if (marketplaceLive(epoch, generation)) await loadMarketplace();
  } catch (error) {
    if (epoch === conn.epoch) marketplaceNotice(error.message || String(error), "err");
  } finally {
    marketplaceState.installing.delete(item.name);
    if (epoch === conn.epoch) renderMarketplaceGrid();
  }
}

function resetMarketplace() {
  marketplaceState.generation++;
  marketplaceState.kind = "all";
  marketplaceState.category = "";
  marketplaceState.query = "";
  marketplaceState.items = [];
  marketplaceState.categories = [];
  marketplaceState.counts = { all: 0, artisan: 0, skill_library: 0 };
  marketplaceState.installing.clear();
  const grid = $("marketplace-grid");
  if (grid) grid.replaceChildren();
  const query = $("marketplace-query");
  if (query) query.value = "";
  marketplaceNotice("");
  syncMarketKindTabs();
}

if ($("marketplace-kinds")) {
  $("marketplace-kinds").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-market-kind]");
    if (!btn) return;
    const next = btn.dataset.marketKind;
    if (next === marketplaceState.kind) return;
    marketplaceState.kind = next;
    syncMarketKindTabs();
    loadMarketplace().catch(fail);
  });
}

if ($("marketplace-category")) {
  $("marketplace-category").addEventListener("change", (event) => {
    marketplaceState.category = event.target.value || "";
    loadMarketplace().catch(fail);
  });
}

if ($("marketplace-filter")) {
  $("marketplace-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("marketplace-query");
    marketplaceState.query = input ? input.value.trim() : "";
    loadMarketplace().catch(fail);
  });
}

if ($("refresh-marketplace")) {
  $("refresh-marketplace").addEventListener("click", () => {
    const input = $("marketplace-query");
    marketplaceState.query = input ? input.value.trim() : "";
    loadMarketplace().catch(fail);
  });
}
