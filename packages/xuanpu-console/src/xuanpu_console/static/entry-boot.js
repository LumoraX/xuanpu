// 极简入口装配：绑定事件、处理登录错误提示、首次加载时尝试恢复登录。
"use strict";

const ENTRY_LOGIN_ERRORS = {
  tenant_unavailable: "此租户暂未启用飞书登录，请联系管理员。",
  feishu_denied: "你取消了飞书授权，请重新登录。",
  state_invalid: "登录请求已失效，请重新发起登录。",
  identity_unavailable: "飞书身份服务暂不可用，请稍后重试。",
  not_in_app_scope: "你不在飞书应用的可用范围内，请联系管理员。",
  tenant_mismatch: "此飞书账号不属于当前企业。",
  user_disabled: "你的平台账号已停用，请联系管理员。",
};

function bootEntry() {
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("message").value;
    $("message").value = "";
    if (typeof clearSystemStatus === "function") clearSystemStatus();
    sendMessage(text).catch(report);
  });
  $("message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("entry-login").addEventListener("click", () => {
    const params = new URLSearchParams();
    const tenant = new URLSearchParams(location.search).get("tenant");
    if (tenant) params.set("tenant", tenant);
    params.set("return_to", location.pathname);
    location.assign("/v1/auth/feishu/login?" + params);
  });
  $("entry-logout").addEventListener("click", () => logoutEntry().catch(report));
  $("entry-notifications").addEventListener("click", () => { if (entry.me) appendCard(notificationCard()); });
  const loginError = new URLSearchParams(location.search).get("login_error");
  if (loginError) {
    $("entry-login-error").textContent = ENTRY_LOGIN_ERRORS[loginError] || "登录失败，请重新登录或联系管理员。";
    $("entry-login-error").classList.remove("hidden");
  }
  connectEntry().catch(report);
}

bootEntry();
