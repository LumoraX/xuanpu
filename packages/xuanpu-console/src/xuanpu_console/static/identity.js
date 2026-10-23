// 员工登录、凭据隔离及管理员用户页；与 app.js 共用请求入口。
"use strict";

const usersPage = { after: null, generation: 0 };
const loginErrors = {
  tenant_unavailable: "此租户暂未启用飞书登录，请联系管理员。",
  feishu_denied: "你取消了飞书授权，请重新登录。",
  state_invalid: "登录请求已失效，请重新发起登录。",
  identity_unavailable: "飞书身份服务暂不可用，请稍后重试。",
  not_in_app_scope: "你不在飞书应用的可用范围内，请联系管理员。",
  tenant_mismatch: "此飞书账号不属于当前企业。",
  user_disabled: "你的平台账号已停用，请联系管理员。",
};

// Cookie 仅用于同源请求；API key 模式绝不附带浏览器登录 cookie。
function requestOptions() {
  if (!conn.apiKey && !sameOrigin()) throw new Error("员工登录只支持当前站点，请清空平台地址。");
  return { credentials: conn.apiKey ? "omit" : "same-origin", signal: conn.controller.signal };
}

function sameOrigin(value = conn.baseUrl) {
  try { return !value || new URL(value).origin === location.origin; }
  catch { return false; }
}

function isAdmin() { return conn.me?.role === "tenant_admin"; }

// 不解析相对地址，不允许 data/javascript/http 头像；姓名始终作为纯文本展示。
function safeAvatar(value) {
  try { const parsed = new URL(value); return parsed.protocol === "https:" ? parsed.href : null; }
  catch { return null; }
}

function applyPermissions() {
  $("functions-tab").classList.toggle("hidden", !conn.me?.user_id || !conn.ready);
  $("admin-connection").classList.toggle("hidden", !adminMode || (!!conn.me && !isAdmin()));
  if (conn.me) $("admin-connection").open = false;
  $("feishu-login").classList.toggle("hidden", !!conn.me?.user_id || !sameOrigin());
  for (const node of document.querySelectorAll("[data-admin]")) node.classList.toggle("hidden", !isAdmin());
  $("current-user").classList.toggle("hidden", !conn.me);
  $("user-name").textContent = conn.me ? (conn.me.display_name || "API key") + (isAdmin() ? " · 管理员" : " · 成员") : "";
  const avatar = $("user-avatar");
  avatar.removeAttribute("src");
  avatar.classList.add("hidden");
}

// 先取消旧请求并递增代际，再清除可见的身份数据；旧响应不能把上一位员工的数据填回来。
function resetIdentity() {
  if (typeof resetOrganization === "function") resetOrganization();
  if (typeof resetConnections === "function") resetConnections();
  resetReminders();
  if (typeof resetWorkspace === "function") resetWorkspace();
  conn.controller.abort();
  conn.controller = new AbortController();
  conn.epoch += 1;
  conn.me = null;
  conn.ready = false;
  $("new-package").innerHTML = '<option value="">登录后自动加载</option>';
  $("new-package").disabled = true;
  $("new-session-btn").disabled = true;
  $("business-hint").textContent = "登录后直接说出需求，助手会找到对应流程。";
  chat.generation += 1;
  chat.creating = false;
  chat.routing = false;
  chat.routeContext = [];
  $("welcome").classList.remove("hidden");
  $("suggestions").classList.add("hidden");
  chat.sessionId = null;
  chat.status = null;
  chat.streaming = null;
  audit.after = 0;
  usersPage.after = null;
  usersPage.generation += 1;
  $("users-more").disabled = true;
  $("audit-more").disabled = true;
  $("show-closed").checked = false;
  for (const id of ["session-list", "transcript", "pending", "eval-report"]) $(id).innerHTML = "";
  for (const id of ["package-table", "audit-table", "usage-table", "users-table"]) $(id).querySelector("tbody").innerHTML = "";
  $("chat-head").textContent = "直接告诉我，你想办什么？";
  $("message").value = "";
  $("message").disabled = true;
  $("send-btn").disabled = true;
  $("send-btn").classList.remove("hidden");
  $("stop-btn").classList.add("hidden");
  $("pending").classList.add("hidden");
  for (const id of ["archive", "new-package", "new-entry", "audit-session", "audit-kind", "audit-source", "usage-since", "usage-until", "users-status", "users-role", "users-unverified"]) $(id).value = "";
  $("publish-status").textContent = "";
  $("eval-report").classList.add("hidden");
  $("toast").classList.add("hidden");
  $("users-notice").textContent = "";
  $("conn-status").textContent = "未连接";
  $("conn-status").className = "status";
  for (const node of $("tabs").querySelectorAll("button")) node.classList.toggle("active", node.dataset.tab === "sessions");
  for (const node of document.querySelectorAll(".tab")) node.classList.toggle("active", node.id === "tab-sessions");
  applyPermissions();
}

// 姓名代表个人身份；租户代码和后台凭据不能冒充已登录员工。
function identityStatus(me) {
  if (me.key_id) return "后台凭据已连接 · 非个人登录";
  return "已登录 · " + (me.display_name || "企业用户");
}

async function connectIdentity({ automatic = false } = {}) {
  resetIdentity();
  const epoch = conn.epoch;
  try {
    const me = await api("GET", "/v1/me");
    conn.me = me;
    applyPermissions();
    $("conn-status").textContent = identityStatus(me);
    $("conn-status").className = "status ok";
    if (me.user_id) {
      const user = await api("GET", `/v1/users/${encodeURIComponent(me.user_id)}`);
      const avatar = safeAvatar(user.avatar_url);
      if (avatar) { $("user-avatar").src = avatar; $("user-avatar").classList.remove("hidden"); }
    }
    await loadBusinessChoices();
    const sessions = await loadSessions();
    const remembered = sessionStorage.getItem(sessionMemoryKey());
    const current = sessions.find(session => session.session_id === remembered);
    if (current) await openSession(current);
    else startConversation();
    if (isAdmin()) await loadPackages();
    if (epoch !== conn.epoch) return false;
    conn.ready = true;
    applyPermissions();
    if (typeof openConnections === "function" && new URLSearchParams(location.search).get("connections") === "1") openConnections().catch(fail);
    startReminders().catch(fail);
    if (typeof startWorkspace === "function") startWorkspace().catch(fail);
    return epoch === conn.epoch;
  } catch (error) {
    if (epoch !== conn.epoch || error.name === "AbortError") return false;
    resetIdentity();
    if (!automatic || error.status !== 401) {
      $("conn-status").textContent = "连接失败：" + error.message;
      $("conn-status").className = "status bad";
    }
    return false;
  }
}

$("connect").addEventListener("submit", (event) => {
  event.preventDefault();
  conn.baseUrl = $("base-url").value.trim();
  conn.apiKey = $("api-key").value.trim();
  localStorage.setItem("xuanpu.baseUrl", conn.baseUrl);
  sessionStorage.setItem("xuanpu.apiKey", conn.apiKey);
  connectIdentity().catch(fail);
});

// 业务目录沿用有租户隔离的只读接口，仅展示已生效业务；入口由服务器选默认值。
async function loadBusinessChoices() {
  const body = await api("GET", "/v1/packages");
  const choices = body.packages.filter(item => item.active);
  const select = $("new-package");
  const previous = select.value;
  select.innerHTML = choices.length ? choices.map(item =>
    `<option value="${escape(item.name)}">${escape(businessName(item.name))}</option>`
  ).join("") : '<option value="">暂无可用业务</option>';
  select.value = choices.some(item => item.name === previous) ? previous : (choices[0]?.name || "");
  select.disabled = !choices.length;
  $("new-session-btn").disabled = !conn.me;
  $("business-hint").textContent = "直接输入你的需求，无需选择业务。";
}

function updateLoginButton() {
  $("feishu-login").classList.toggle("hidden", !sameOrigin($("base-url").value.trim()));
}

$("base-url").addEventListener("input", updateLoginButton);
$("feishu-login").addEventListener("click", () => {
  if (!sameOrigin($("base-url").value.trim())) return;
  resetIdentity();
  conn.apiKey = "";
  conn.baseUrl = "";
  sessionStorage.removeItem("xuanpu.apiKey");
  localStorage.removeItem("xuanpu.baseUrl");
  const tenant = new URLSearchParams(location.search).get("tenant");
  const params = new URLSearchParams();
  if (tenant) params.set("tenant", tenant);
  params.set("return_to", location.pathname + (tenant ? "?tenant=" + encodeURIComponent(tenant) : ""));
  location.assign("/v1/auth/feishu/login?" + params);
});

$("logout").addEventListener("click", async () => {
  const cookieMode = !conn.apiKey;
  resetIdentity();
  const epoch = conn.epoch;
  try {
    if (cookieMode) await api("POST", "/v1/auth/logout");
    conn.apiKey = "";
    sessionStorage.removeItem("xuanpu.apiKey");
    $("api-key").value = "";
    $("conn-status").textContent = "已退出";
  } catch (error) {
    if (epoch !== conn.epoch || error.name === "AbortError") return;
    $("conn-status").textContent = "退出未完成，请重试";
    $("current-user").classList.remove("hidden");
    fail(error);
  }
});

$("base-url").value = conn.baseUrl;
$("api-key").value = conn.apiKey;
updateLoginButton();
const loginError = new URLSearchParams(location.search).get("login_error");
if (loginError) {
  $("login-error").textContent = loginErrors[loginError] || "登录失败，请重新登录或联系管理员。";
  $("login-error").classList.remove("hidden");
}
// 初次同源访问尝试恢复 HttpOnly 登录 cookie；401 是未登录的正常状态。
if (conn.apiKey || sameOrigin()) connectIdentity({ automatic: true }).catch(fail);

// ---------- 管理员用户页 ----------

function usersQuery(after) {
  const params = new URLSearchParams({ limit: "50" });
  for (const field of ["status", "role", "unverified"]) {
    const value = $("users-" + field).value;
    if (value) params.set(field, value);
  }
  if (after) params.set("after", after);
  return params;
}

async function loadUsers(reset) {
  if (!isAdmin()) return;
  const generation = ++usersPage.generation;
  $("users-more").disabled = true;
  const body = await api("GET", "/v1/users?" + usersQuery(reset ? null : usersPage.after));
  if (generation !== usersPage.generation) return;
  const rows = $("users-table").querySelector("tbody");
  if (reset) rows.innerHTML = "";
  for (const user of body.users) rows.appendChild(userRow(user));
  usersPage.after = body.next_after;
  $("users-more").disabled = !body.next_after;
}

function userRow(user) {
  const row = document.createElement("tr");
  row.dataset.userId = user.user_id;
  // 所有远端文本经 escape；按钮监听器直接捕获 ID，不把远端文本写进脚本属性。
  row.innerHTML = `<td>${escape(user.display_name)}<div class="meta mono">${escape(user.user_id)}</div></td>` +
    `<td>${user.role === "tenant_admin" ? "管理员" : "成员"}</td>` +
    `<td>${user.status === "active" ? "启用" : "停用"}</td>` +
    `<td>${when(user.last_login_at) || "尚未登录"}</td>` +
    `<td>${user.unverified_since ? "飞书查不到 · " + when(user.unverified_since) : "—"}</td><td></td>`;
  const actions = row.lastElementChild;
  const role = user.role === "tenant_admin" ? "tenant_member" : "tenant_admin";
  const status = user.status === "active" ? "disabled" : "active";
  addUserAction(actions, role === "tenant_admin" ? "设为管理员" : "改为成员", user, "role", role);
  addUserAction(actions, status === "disabled" ? "停用" : "启用", user, "status", status);
  if (user.status === "disabled") addUserAction(actions, "重试停用收尾", user, "status", "disabled");
  return row;
}

function addUserAction(container, label, user, field, value) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = value === "disabled" ? "danger" : "ghost";
  button.addEventListener("click", () => changeUser(button, user, field, value));
  container.appendChild(button);
}

function userChangeNotice(user) {
  if (user.cleanup_failed?.length) return "员工已停用，但以下收尾失败：" + user.cleanup_failed.join("、") + "。请点击“重试停用收尾”；如仍失败，请联系平台管理员。";
  return user.status === "disabled" ? "员工已停用。" : "已启用；员工需重新登录，已关闭的会话不会恢复。";
}

async function changeUser(button, user, field, value) {
  button.disabled = true;
  $("users-notice").textContent = "";
  try {
    const updated = await api("PUT", `/v1/users/${encodeURIComponent(user.user_id)}/${field}`, { json: { [field]: value } });
    const notice = field === "status" ? userChangeNotice(updated) : "角色已更新。";
    if (user.user_id === conn.me?.user_id) {
      // 自己降级或停用时立即重新确认权限，不能留下已失去权限的管理员界面。
      if (!await connectIdentity()) return;
    } else {
      await loadUsers(true);
    }
    $("users-notice").textContent = notice;
    toast(notice, updated.cleanup_failed?.length ? "" : "info");
  } catch (error) {
    if (error.name === "AbortError") return;
    $("users-notice").textContent = error.status === 409 ? "无法修改：" + error.message : "操作失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}

$("users-filter").addEventListener("submit", event => {
  event.preventDefault();
  $("users-notice").textContent = "";
  loadUsers(true).catch(fail);
});
$("users-more").addEventListener("click", () => loadUsers(false).catch(fail));
