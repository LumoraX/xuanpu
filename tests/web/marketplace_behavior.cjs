// 能力市场前端行为测试（在 Node 中运行真实 marketplace.js）：分类切换、一键安装、缺密钥跳转与登出清理。
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function makeNode(tag = "div") {
  const classes = new Set();
  const attrs = {};
  const listeners = {};
  const node = {
    tag,
    children: [],
    dataset: {},
    value: "",
    disabled: false,
    _text: "",
    get textContent() { return this._text + this.children.map(c => c.textContent).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); for (const c of String(v).split(/\s+/)) if (c) classes.add(c); },
    classList: {
      add: v => classes.add(v),
      remove: v => classes.delete(v),
      contains: v => classes.has(v),
      toggle: (v, yes) => { const on = yes === undefined ? !classes.has(v) : yes; if (on) classes.add(v); else classes.delete(v); return on; },
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute: k => attrs[k],
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    replaceChildren(...cs) { this._text = ""; this.children = []; this.append(...cs); },
    addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    listeners,
    closest(sel) {
      if (sel === "button[data-market-kind]" && this.tag === "button" && this.dataset.marketKind) return this;
      return null;
    },
    querySelector(sel) {
      return findAll(this, n => matchesSel(n, sel))[0] || null;
    },
    querySelectorAll(sel) {
      return findAll(this, n => matchesSel(n, sel));
    },
  };
  return node;
}

function matchesSel(node, sel) {
  if (sel === "button") return node.tag === "button";
  if (sel === "button[data-market-kind]") return node.tag === "button" && Boolean(node.dataset.marketKind);
  if (sel === 'button[data-tab="secrets"]') return node.tag === "button" && node.dataset.tab === "secrets";
  if (sel === ".tab") return node.classList.contains("tab");
  return false;
}

function findAll(root, pred, out = []) {
  for (const c of root.children) {
    if (c && typeof c === "object") {
      if (pred(c)) out.push(c);
      if (Array.isArray(c.children)) findAll(c, pred, out);
    }
  }
  return out;
}

async function run(dir) {
  const nodes = {};
  const register = (id, tag = "div") => { const n = makeNode(tag); n.id = id; nodes[id] = n; return n; };

  const tabs = register("tabs", "nav");
  const marketTabBtn = makeNode("button"); marketTabBtn.dataset.tab = "marketplace"; marketTabBtn.className = "active";
  const secretTabBtn = makeNode("button"); secretTabBtn.dataset.tab = "secrets";
  tabs.append(marketTabBtn, secretTabBtn);

  const tabMarket = register("tab-marketplace", "section"); tabMarket.className = "tab active";
  const tabSecrets = register("tab-secrets", "section"); tabSecrets.className = "tab";

  const kinds = register("marketplace-kinds", "div");
  const kindAll = makeNode("button"); kindAll.dataset.marketKind = "all";
  const kindArtisan = makeNode("button"); kindArtisan.dataset.marketKind = "artisan";
  const kindSkill = makeNode("button"); kindSkill.dataset.marketKind = "skill_library";
  kinds.append(kindAll, kindArtisan, kindSkill);

  register("marketplace-filter", "form");
  register("marketplace-category", "select");
  register("marketplace-query", "input");
  register("refresh-marketplace", "button");
  register("marketplace-notice", "p");
  register("marketplace-grid", "div");

  const calls = [];
  const jumpedSecrets = [];
  let installed = false;

  const api = async (method, url, opts) => {
    calls.push([method, url, opts]);
    if (method === "GET" && url.startsWith("/v1/marketplace/items")) {
      const items = [
        {
          name: "common-skills", version: "1.0.0", kind: "skill_library",
          display_name: "通用地理与出行技能库", description: "地图定位天气",
          categories: ["geo", "environment"], tags: [], source: "catalog",
          skills: [{ skill_id: "weather", display_name: "天气预报", category: "environment", tags: [], description: "天气", exported: true, entry: false }],
          uses: [], connectors: ["amap"], installed, active: installed, installed_version: installed ? "1.0.0" : null,
          upgrade_available: false, secrets: [{ name: "amap-key", configured: false }],
        },
        {
          name: "travel-planner", version: "1.0.0", kind: "artisan",
          display_name: "出行规划匠人", description: "路线天气出发提醒",
          categories: ["planning"], tags: [], source: "catalog",
          skills: [{ skill_id: "travel-planner", display_name: "出行规划", category: "planning", tags: [], description: "规划", exported: false, entry: true }],
          uses: ["common-skills"], connectors: [], installed, active: installed, installed_version: installed ? "1.0.0" : null,
          upgrade_available: false, secrets: [{ name: "amap-key", configured: false }],
        },
      ];
      const filtered = url.includes("kind=skill_library") ? items.filter(i => i.kind === "skill_library") : items;
      return { items: filtered, categories: ["environment", "geo", "planning"], counts: { all: 2, artisan: 1, skill_library: 1 } };
    }
    if (method === "POST" && url === "/v1/marketplace/install") {
      installed = true;
      return {
        installed: [
          { name: "common-skills", display_name: "通用地理与出行技能库", version: "1.0.0", kind: "skill_library", status: "installed" },
          { name: "travel-planner", display_name: "出行规划匠人", version: "1.0.0", kind: "artisan", status: "installed" },
        ],
        missing_secrets: ["amap-key"],
      };
    }
    throw new Error("unexpected call " + method + " " + url);
  };

  const document = {
    createElement: tag => makeNode(tag),
    createTextNode: text => ({ textContent: String(text), children: [] }),
    querySelectorAll: sel => sel === ".tab" ? [tabMarket, tabSecrets] : [],
  };

  const context = vm.createContext({
    console, URLSearchParams, Set, Array, Object, String, Boolean, Promise,
    document,
    $: id => nodes[id] || null,
    conn: { me: { role: "tenant_admin" }, epoch: 1 },
    api,
    fail: e => { throw e; },
    loadPackages: async () => {},
    loadSecrets: async name => { jumpedSecrets.push(name); },
  });
  vm.runInContext(fs.readFileSync(path.join(dir, "marketplace.js"), "utf8"), context, { filename: "marketplace.js" });

  // 1. 加载全部市场项
  await context.loadMarketplace();
  assert.equal(nodes["marketplace-grid"].children.length, 2, "全部视图展示 1 个技能库 + 1 位匠人");
  assert.ok(kindArtisan.textContent.includes("匠人市场 (1)"));
  assert.ok(kindSkill.textContent.includes("Skill 市场 (1)"));

  // 2. 切到 Skill 市场
  kinds.listeners.click[0]({ target: kindSkill });
  await new Promise(r => setImmediate(r));
  assert.equal(nodes["marketplace-grid"].children.length, 1, "Skill 市场只展示通用技能库");
  assert.equal(kindSkill.getAttribute("aria-pressed"), "true");

  // 3. 切回全部并点击一键安装出行规划匠人
  kinds.listeners.click[0]({ target: kindAll });
  await new Promise(r => setImmediate(r));
  const plannerCard = nodes["marketplace-grid"].children.find(c => c.dataset.package === "travel-planner");
  const installBtn = findAll(plannerCard, n => n.tag === "button" && n.textContent === "一键安装")[0];
  assert.ok(installBtn, "未安装匠人有「一键安装」按钮");
  await installBtn.listeners.click[0]();

  assert.ok(calls.some(c => c[0] === "POST" && c[1] === "/v1/marketplace/install" && c[2].json.name === "travel-planner"));
  const jumpBtn = findAll(nodes["marketplace-notice"], n => n.tag === "button" && n.textContent.includes("amap-key"))[0];
  assert.ok(jumpBtn, "安装后提示缺失密钥并提供跳转按钮");

  // 4. 点击跳转按钮直接切到密钥页并选中 amap-key
  jumpBtn.listeners.click[0]();
  assert.ok(tabSecrets.classList.contains("active"), "切换到密钥页");
  assert.deepEqual(jumpedSecrets, ["amap-key"], "自动选中待配置的 amap-key");

  // 5. 登出/切换身份时清空状态
  context.resetMarketplace();
  assert.equal(nodes["marketplace-grid"].children.length, 0);
  console.log("marketplace behavior passed");
}

run(process.argv[2]).catch(err => { console.error(err); process.exit(1); });
