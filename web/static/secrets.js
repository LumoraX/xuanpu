// 租户密钥管理（仅租户管理员可见）：下拉选择业务包声明的密钥名、只填密钥值；不回显明文，支持二次确认删除。
"use strict";

const SECRET_NAME_RE = /^[a-z][a-z0-9-]{0,62}$/;
const SECRET_CUSTOM_OPTION = "__custom__";
const secretsState = { generation: 0, confirmingDelete: null, items: [] };

function secretNotice(text, kind = "") {
  const node = $("secret-notice");
  if (!node) return;
  node.textContent = text || "";
  node.className = "status" + (kind ? " " + kind : "");
}

function syncCustomSecretInput() {
  const select = $("secret-select");
  const nameInput = $("secret-name");
  if (!select || !nameInput) return;
  const custom = select.value === SECRET_CUSTOM_OPTION;
  nameInput.classList.toggle("hidden", !custom);
  nameInput.required = custom;
  if (!custom) nameInput.value = "";
}

function selectedSecretName() {
  const select = $("secret-select");
  const nameInput = $("secret-name");
  if (select && select.value && select.value !== SECRET_CUSTOM_OPTION) {
    return select.value.trim();
  }
  return (nameInput ? nameInput.value : "").trim();
}

function formatSecretUsage(usedBy) {
  if (!Array.isArray(usedBy) || !usedBy.length) return "自定义 / 未被当前包声明";
  const labels = [];
  for (const ref of usedBy) {
    const label = (ref.display_name || ref.package || "") + (ref.connector ? ` (${ref.connector})` : "");
    if (label && !labels.includes(label)) labels.push(label);
  }
  return labels.join("、");
}

function secretOptionLabel(item) {
  const state = item.configured ? "已配置" : "待配置";
  const usage = Array.isArray(item.used_by) && item.used_by.length
    ? item.used_by.map(u => u.display_name || u.package).filter(Boolean).join("、")
    : "";
  return usage ? `${usage} · ${item.name}（${state}）` : `${item.name}（${state}）`;
}

function mergeSecretItems(names, declared) {
  const byName = new Map();
  for (const item of Array.isArray(declared) ? declared : []) {
    byName.set(item.name, {
      name: item.name,
      configured: Boolean(item.configured),
      used_by: Array.isArray(item.used_by) ? item.used_by : [],
    });
  }
  for (const name of Array.isArray(names) ? names : []) {
    const existing = byName.get(name);
    if (existing) existing.configured = true;
    else byName.set(name, { name, configured: true, used_by: [] });
  }
  // 待配置的排在最前，便于管理员第一时间看到需要补填的密钥
  return [...byName.values()].sort((a, b) => {
    if (a.configured !== b.configured) return a.configured ? 1 : -1;
    return a.name.localeCompare(b.name);
  });
}

function renderSecretOptions(items, preferredName) {
  const select = $("secret-select");
  if (!select) return;
  const previous = preferredName || select.value;
  select.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = secretOptionLabel(item);
    select.appendChild(option);
  }
  const customOption = document.createElement("option");
  customOption.value = SECRET_CUSTOM_OPTION;
  customOption.textContent = "自定义新密钥名…";
  select.appendChild(customOption);

  if (previous && items.some(item => item.name === previous)) {
    select.value = previous;
  } else if (items.length) {
    // 默认优先选中第一个待配置的业务包密钥
    select.value = items[0].name;
  } else {
    select.value = SECRET_CUSTOM_OPTION;
  }
  syncCustomSecretInput();
}

function focusSecretForEdit(item) {
  secretsState.confirmingDelete = null;
  const select = $("secret-select");
  const valueInput = $("secret-value");
  if (select) {
    select.value = item.name;
    syncCustomSecretInput();
  }
  if (valueInput) {
    valueInput.value = "";
    if (typeof valueInput.focus === "function") valueInput.focus();
  }
  const verb = item.configured ? "更新" : "配置";
  secretNotice(`已选中 ${item.name}：直接粘贴密钥值并点击「保存密钥」即可完成${verb}。`);
}

function renderSecretRows(items) {
  const table = $("secret-table");
  const tbody = table && table.querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!items || !items.length) {
    const empty = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "meta";
    cell.textContent = "尚未配置任何密钥。";
    empty.appendChild(cell);
    tbody.appendChild(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "mono";
    nameCell.textContent = item.name;

    const usageCell = document.createElement("td");
    usageCell.textContent = formatSecretUsage(item.used_by);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = item.configured ? "tag ok" : "tag";
    badge.textContent = item.configured ? "已配置（明文不可读）" : "待配置";
    statusCell.appendChild(badge);

    const actionCell = document.createElement("td");
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "ghost";
    editBtn.textContent = item.configured ? "更新" : "去配置";
    editBtn.addEventListener("click", () => focusSecretForEdit(item));
    actionCell.appendChild(editBtn);

    if (item.configured) {
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = secretsState.confirmingDelete === item.name ? "danger" : "ghost";
      delBtn.textContent = secretsState.confirmingDelete === item.name ? "确认删除" : "删除";
      delBtn.addEventListener("click", () => {
        if (secretsState.confirmingDelete !== item.name) {
          secretsState.confirmingDelete = item.name;
          renderSecretRows(items);
          secretNotice(`再次点击「确认删除」将移除密钥 ${item.name}，依赖它的连接器会立即失效。`);
          return;
        }
        deleteSecretByName(item.name).catch(fail);
      });
      actionCell.appendChild(document.createTextNode(" "));
      actionCell.appendChild(delBtn);
    }

    row.appendChild(nameCell);
    row.appendChild(usageCell);
    row.appendChild(statusCell);
    row.appendChild(actionCell);
    tbody.appendChild(row);
  }
}

function resetSecrets() {
  secretsState.generation += 1;
  secretsState.confirmingDelete = null;
  secretsState.items = [];
  const select = $("secret-select");
  const nameInput = $("secret-name");
  const valueInput = $("secret-value");
  if (select) select.innerHTML = "";
  if (nameInput) {
    nameInput.value = "";
    nameInput.classList.add("hidden");
  }
  if (valueInput) valueInput.value = "";
  secretNotice("");
  const table = $("secret-table");
  const tbody = table && table.querySelector("tbody");
  if (tbody) tbody.innerHTML = "";
}

async function loadSecrets(preferredName = "") {
  if (!conn.me || typeof isAdmin !== "function" || !isAdmin()) return;
  const epoch = conn.epoch;
  const generation = ++secretsState.generation;
  let body;
  try {
    body = await api("GET", "/v1/secrets/declared");
  } catch (error) {
    if (error.status === 404) body = await api("GET", "/v1/secrets");
    else throw error;
  }
  if (epoch !== conn.epoch || generation !== secretsState.generation) return;
  secretsState.confirmingDelete = null;
  secretsState.items = mergeSecretItems(body.names, body.declared);
  renderSecretOptions(secretsState.items, preferredName);
  renderSecretRows(secretsState.items);
}

async function deleteSecretByName(name) {
  const epoch = conn.epoch;
  await api("DELETE", `/v1/secrets/${encodeURIComponent(name)}`);
  if (epoch !== conn.epoch) return;
  secretsState.confirmingDelete = null;
  secretNotice(`已删除密钥 ${name}。`, "ok");
  await loadSecrets(name);
}

const secretSelect = $("secret-select");
if (secretSelect) {
  secretSelect.addEventListener("change", () => {
    syncCustomSecretInput();
    if (secretSelect.value === SECRET_CUSTOM_OPTION) {
      const nameInput = $("secret-name");
      if (nameInput && typeof nameInput.focus === "function") nameInput.focus();
    }
  });
}

const secretSaveForm = $("secret-save");
if (secretSaveForm) {
  secretSaveForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const epoch = conn.epoch;
    const nameInput = $("secret-name");
    const valueInput = $("secret-value");
    const name = selectedSecretName();
    const value = valueInput ? valueInput.value : "";
    if (!SECRET_NAME_RE.test(name)) {
      secretNotice("密钥名须以小写字母开头，只含小写字母、数字和连字符（最长 63 位），例如 amap-key。", "bad");
      return;
    }
    if (!value) {
      secretNotice("请输入密钥值。", "bad");
      return;
    }
    secretNotice("保存中…");
    try {
      await api("PUT", `/v1/secrets/${encodeURIComponent(name)}`, { json: { value } });
      if (epoch !== conn.epoch) return;
      if (valueInput) valueInput.value = "";
      if (nameInput) nameInput.value = "";
      secretNotice(`已保存密钥 ${name}（明文不回显，已加密存入租户密钥库）。`, "ok");
      await loadSecrets(name);
    } catch (error) {
      if (epoch !== conn.epoch || error.name === "AbortError") return;
      secretNotice("保存失败：" + (error.message || String(error)), "bad");
    }
  });
}

const refreshSecretsBtn = $("refresh-secrets");
if (refreshSecretsBtn) {
  refreshSecretsBtn.addEventListener("click", () => {
    secretNotice("");
    loadSecrets().catch(fail);
  });
}
