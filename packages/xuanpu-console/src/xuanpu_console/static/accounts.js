// 玄圃自有账号（ADR 0019）：用户名口令登录、兑现设置链接、改口令、管理员开通账号与签发设置链接。
// 与 identity.js 共用请求入口与身份切换；登录、设口令只在同源页面经 HttpOnly cookie 完成。
"use strict";

const SETUP_KEY = "password_setup";
const accountState = { setupToken: "" };

function loginTenant() {
  return new URLSearchParams(location.search).get("tenant") || null;
}

// 先把令牌从地址栏片段里取出并抹掉，免得被书签、截图或浏览记录带走。
function takeSetupToken() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get(SETUP_KEY) || "";
  if (token && history.replaceState) history.replaceState(null, "", location.pathname + location.search);
  return token;
}

function setupNotice(text) { $("password-setup-notice").textContent = text; }

async function showPasswordSetup(token) {
  accountState.setupToken = token;
  $("password-setup").classList.remove("hidden");
  try {
    const info = await api("POST", "/v1/auth/password/setup/inspect", { json: { token } });
    const action = info.purpose === "reset" ? "重置口令" : "设置口令";
    $("password-setup-who").textContent = `${info.display_name}（用户名 ${info.username}）：${action}后直接登录。`;
  } catch (error) {
    if (error.name === "AbortError") return;
    $("password-setup-form").classList.add("hidden");
    setupNotice(error.message || "设置链接无效或已过期，请联系管理员重新签发。");
  }
}

$("password-setup-form").addEventListener("submit", async event => {
  event.preventDefault();
  const password = $("setup-password").value;
  if (password !== $("setup-confirm").value) { setupNotice("两次输入的口令不一致。"); return; }
  setupNotice("");
  try {
    await api("POST", "/v1/auth/password/setup", { json: { token: accountState.setupToken, password } });
    accountState.setupToken = "";
    $("setup-password").value = $("setup-confirm").value = "";
    $("password-setup").classList.add("hidden");
    await connectIdentity();
  } catch (error) {
    if (error.name === "AbortError") return;
    setupNotice(error.message || "设置失败，请重试。");
  }
});

$("password-login").addEventListener("submit", async event => {
  event.preventDefault();
  if (!sameOrigin()) return;
  $("login-error").classList.add("hidden");
  const body = { tenant: loginTenant(), username: $("login-username").value.trim(), password: $("login-password").value };
  try {
    await api("POST", "/v1/auth/password/login", { json: body });
    $("login-password").value = "";
    await connectIdentity();
  } catch (error) {
    if (error.name === "AbortError") return;
    $("login-error").textContent = error.message || "登录失败，请重试。";
    $("login-error").classList.remove("hidden");
  }
});

$("change-password-toggle").addEventListener("click", () => {
  $("change-password").classList.toggle("hidden");
  $("change-password-notice").textContent = "";
});

$("change-password-form").addEventListener("submit", async event => {
  event.preventDefault();
  const json = { current_password: $("current-password").value, new_password: $("new-password").value };
  try {
    await api("POST", "/v1/me/password", { json });
    $("current-password").value = $("new-password").value = "";
    $("change-password-notice").textContent = "口令已更新，其他设备上的登录已退出。";
  } catch (error) {
    if (error.name === "AbortError") return;
    $("change-password-notice").textContent = error.message || "修改失败，请重试。";
  }
});

// 设置链接只显示在管理员自己的页面上，由管理员经企业渠道转交员工。
function showAccountLink(link) {
  const kind = link.purpose === "reset" ? "重置链接" : "设置链接";
  $("account-link").textContent = `${link.username} 的${kind}（${when(link.expires_at)} 前有效，只显示这一次）：${link.url}`;
  $("account-link").classList.remove("hidden");
}

$("account-create").addEventListener("submit", async event => {
  event.preventDefault();
  $("users-notice").textContent = "";
  const json = { username: $("account-username").value.trim(), display_name: $("account-display-name").value.trim(), role: $("account-role").value };
  try {
    const created = await api("POST", "/v1/users", { json });
    $("account-username").value = $("account-display-name").value = "";
    showAccountLink(created.setup_link);
    $("users-notice").textContent = "账号已开通，请把设置链接转交给员工。";
    await loadUsers(true);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("users-notice").textContent = "开通失败：" + error.message;
  }
});

// 员工还没有用户名（如飞书来的）时，服务端要求给 username，这里再问一次。
async function issuePasswordLink(button, user) {
  button.disabled = true;
  $("users-notice").textContent = "";
  const path = `/v1/users/${encodeURIComponent(user.user_id)}/password-link`;
  try {
    let link;
    try {
      link = await api("POST", path, { json: {} });
    } catch (error) {
      if (error.status !== 400 || !/username/.test(error.message)) throw error;
      const username = (window.prompt?.(`为 ${user.display_name} 指定用户名（英文字母、数字、. _ -）`) || "").trim();
      if (!username) return;
      link = await api("POST", path, { json: { username } });
    }
    showAccountLink(link);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("users-notice").textContent = "签发失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}

{
  const token = takeSetupToken();
  if (token) showPasswordSetup(token).catch(fail);
}
