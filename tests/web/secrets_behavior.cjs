// 租户密钥管理面板行为测试：下拉自动列举业务包所需密钥、默认选中待配置项、只填密钥值保存、自定义密钥名、二次确认删除与身份切换清零。
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function makeNode(tag) {
  let _innerHTML = "";
  return {
    tagName: String(tag || "div").toUpperCase(),
    value: "",
    textContent: "",
    get innerHTML() { return _innerHTML; },
    set innerHTML(v) { _innerHTML = String(v); if (!v) this.children = []; },
    className: "",
    type: "",
    required: false,
    colSpan: 1,
    children: [],
    dataset: {},
    listeners: {},
    focused: false,
    classList: {
      values: new Set(),
      add(v) { this.values.add(v); },
      remove(v) { this.values.delete(v); },
      toggle(v, yes) { (yes ?? !this.values.has(v)) ? this.values.add(v) : this.values.delete(v); },
      contains(v) { return this.values.has(v); },
    },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    appendChild(child) { this.children.push(child); return child; },
    querySelector(sel) {
      if (sel === "tbody") {
        if (!this._tbody) this._tbody = makeNode("tbody");
        return this._tbody;
      }
      return null;
    },
    querySelectorAll() { return []; },
    removeAttribute() {},
    setAttribute() {},
    focus() { this.focused = true; },
  };
}

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, makeNode("div"));
  return elements.get(id);
}

const storage = () => ({ getItem() { return ""; }, setItem() {}, removeItem() {} });
const requests = [];
const storedNames = new Set(["office-token"]);
const declaredSpecs = [
  { name: "amap-key", used_by: [{ package: "common-skills", display_name: "通用地理与出行技能库", connector: "amap" }] },
  { name: "office-token", used_by: [{ package: "order-desk", display_name: "订单助手", connector: "orders" }] },
];

const context = vm.createContext({
  console, URL, URLSearchParams, AbortController, DOMException,
  setTimeout, clearTimeout, localStorage: storage(), sessionStorage: storage(),
  location: { origin: "https://xuanpu.example", pathname: "/console/", search: "?admin=1", hash: "", assign() {}, replace() {} },
  history: { replaceState() {} },
  document: {
    getElementById: element,
    querySelectorAll() { return []; },
    createElement: makeNode,
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
    addEventListener() {},
  },
  window: { addEventListener() {} },
  fetch: async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    requests.push({ method, url, init });
    if (url.endsWith("/v1/me")) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ tenant_id: "acme", role: "tenant_admin", key_id: "k1" }) };
    }
    if (url.endsWith("/v1/secrets/declared") && method === "GET") {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          names: [...storedNames].sort(),
          declared: declaredSpecs.map(spec => ({ ...spec, configured: storedNames.has(spec.name) })),
        }),
      };
    }
    const secretMatch = url.match(/\/v1\/secrets\/([^/?]+)$/);
    if (secretMatch) {
      const name = decodeURIComponent(secretMatch[1]);
      if (method === "PUT") {
        storedNames.add(name);
        return { ok: true, status: 200, text: async () => JSON.stringify({ name }) };
      }
      if (method === "DELETE") {
        storedNames.delete(name);
        return { ok: true, status: 200, text: async () => JSON.stringify({ deleted: true }) };
      }
    }
    return { ok: true, status: 200, text: async () => JSON.stringify({ packages: [], sessions: [], items: [] }) };
  },
});

for (const name of ["app.js", "notification-alerts.js", "notification-center.js", "reminders.js", "identity.js", "secrets.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[2] + "/" + name, "utf8"), context);
}

function findButtons(node, out = []) {
  if (!node) return out;
  if (node.tagName === "BUTTON") out.push(node);
  for (const child of node.children || []) findButtons(child, out);
  return out;
}

(async () => {
  await new Promise(resolve => setTimeout(resolve, 10));
  vm.runInContext('conn.me = { tenant_id: "acme", role: "tenant_admin", key_id: "k1" };', context);
  await vm.runInContext("loadSecrets();", context);

  // 1. 加载后下拉框自动包含 amap-key（待配置排最前且默认选中）与 office-token，自定义输入框隐藏
  const select = element("secret-select");
  assert.equal(select.value, "amap-key", "应默认选中首个待配置的业务包密钥 amap-key");
  assert.equal(element("secret-name").classList.contains("hidden"), true, "选中已有声明密钥时隐藏自定义输入框");
  assert.match(select.children[0].textContent, /通用地理与出行技能库.*amap-key.*待配置/);

  // 2. 用户只需输入 key 值并保存，无需手敲密钥名
  element("secret-value").value = "real-amap-secret-xyz";
  await element("secret-save").listeners.submit({ preventDefault() {} });
  const putReq = requests.find(r => r.method === "PUT" && r.url.endsWith("/v1/secrets/amap-key"));
  assert.ok(putReq, "应发送 PUT /v1/secrets/amap-key");
  assert.deepEqual(JSON.parse(putReq.init.body), { value: "real-amap-secret-xyz" });
  assert.equal(element("secret-value").value, "", "保存后必须立刻清空密钥明文输入框");
  const tbody = element("secret-table").querySelector("tbody");
  assert.equal(JSON.stringify(tbody.children).includes("real-amap-secret-xyz"), false, "列表严禁回显明文");

  // 3. 选择「自定义新密钥名…」时显示自定义输入框，并校验非法名称
  select.value = "__custom__";
  select.listeners.change();
  assert.equal(element("secret-name").classList.contains("hidden"), false);
  element("secret-name").value = "BAD_KEY";
  element("secret-value").value = "val";
  await element("secret-save").listeners.submit({ preventDefault() {} });
  assert.match(element("secret-notice").textContent, /小写字母/);
  assert.equal(requests.some(r => r.url.includes("BAD_KEY")), false);

  // 4. 删除需要二次确认：删除 amap-key 后重新回到「待配置」状态（仍保留在业务包声明清单中）
  let btns = findButtons(tbody);
  const delBtn1 = btns.find(b => b.textContent === "删除");
  assert.ok(delBtn1);
  delBtn1.listeners.click();
  assert.equal(requests.some(r => r.method === "DELETE"), false, "首次点击删除只进入确认态");
  btns = findButtons(tbody);
  const delBtn2 = btns.find(b => b.textContent === "确认删除");
  assert.ok(delBtn2);
  delBtn2.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(requests.some(r => r.method === "DELETE" && r.url.endsWith("/v1/secrets/amap-key")));
  assert.equal(select.value, "amap-key", "删除后 amap-key 仍保留在下拉选项里供重新配置");

  // 5. 身份切换清理：清空明文输入与表格
  element("secret-value").value = "unsaved-secret";
  vm.runInContext("resetSecrets();", context);
  assert.equal(element("secret-value").value, "");
  assert.equal(tbody.innerHTML, "");

  console.log("secrets behavior passed");
})().catch(error => {
  console.error(error);
  process.exit(1);
});
