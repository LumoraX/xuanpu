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
  if (location.hash && new URLSearchParams(location.hash.slice(1)).get("password_setup")) {
    location.replace("/workspace" + (location.search || "") + location.hash);
    return;
  }
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
  const loginTenant = new URLSearchParams(location.search).get("tenant");
  if (loginTenant) $("entry-password-login").href = "/workspace?tenant=" + encodeURIComponent(loginTenant);
  const passwordForm = $("entry-password-form");
  if (passwordForm) {
    passwordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      $("entry-login-error").classList.add("hidden");
      const username = ($("entry-username") ? $("entry-username").value : "").trim();
      const password = $("entry-password") ? $("entry-password").value : "";
      try {
        await entryApi("POST", "/v1/auth/password/login", { json: { tenant: loginTenant || null, username, password } });
        if ($("entry-password")) $("entry-password").value = "";
        await connectEntry();
      } catch (error) {
        if (error && error.name === "AbortError") return;
        $("entry-login-error").textContent = (error && error.message) || "登录失败，请重试。";
        $("entry-login-error").classList.remove("hidden");
      }
    });
  }
  const locBtn = $("entry-location");
  if (locBtn && typeof currentLocation === "function") {
    locBtn.addEventListener("click", () => { currentLocation("", { forcePrompt: true }).catch(report); });
  }
  const attachBtn = $("entry-attach");
  const fileInput = $("entry-file-input");
  if (attachBtn && fileInput && typeof uploadAttachmentFiles === "function") {
    attachBtn.addEventListener("click", () => { if (typeof fileInput.click === "function") fileInput.click(); });
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files.length) {
        uploadAttachmentFiles(fileInput.files).catch(report).finally(() => { fileInput.value = ""; });
      }
    });
  }
  const contactsBtn = $("entry-contacts");
  if (contactsBtn && typeof openContactsDrawer === "function") {
    contactsBtn.addEventListener("click", () => { openContactsDrawer().catch(report); });
  }
  const boardBtn = $("entry-board");
  if (boardBtn && typeof openBoardDrawer === "function") {
    boardBtn.addEventListener("click", () => { openBoardDrawer().catch(report); });
  }
  const wbBtn = $("entry-workbench");
  if (wbBtn && typeof openWorkbenchDrawer === "function") {
    wbBtn.addEventListener("click", () => { openWorkbenchDrawer().catch(report); });
  }
  const liveFocusBtn = $("entry-live-focus");
  if (liveFocusBtn && typeof openWorkbenchDrawer === "function") {
    liveFocusBtn.addEventListener("click", () => { openWorkbenchDrawer({ tab: "tasks" }).catch(report); });
  }
  const msgInput = $("message");
  if (msgInput && typeof uploadAttachmentFiles === "function") {
    msgInput.addEventListener("paste", (event) => {
      const items = event.clipboardData && event.clipboardData.files;
      if (items && items.length) {
        event.preventDefault();
        uploadAttachmentFiles(items).catch(report);
      }
    });
  }
  const autonomyBtn = $("entry-autonomy");
  if (autonomyBtn && typeof toggleAutonomySetting === "function") {
    autonomyBtn.addEventListener("click", () => { toggleAutonomySetting().catch(report); });
  }
  const clearBtn = $("entry-clear");
  if (clearBtn && typeof clearConversation === "function") {
    clearBtn.addEventListener("click", () => { clearConversation().catch(report); });
  }
  $("entry-logout").addEventListener("click", () => logoutEntry().catch(report));
  $("entry-notifications").addEventListener("click", () => {
    if (!entry.me) return;
    if (typeof openEntryNotificationView === "function") openEntryNotificationView();
    else appendCard(notificationCard());
  });
  const notifMoreBtn = $("entry-notification-alert-more");
  if (notifMoreBtn) {
    notifMoreBtn.addEventListener("click", () => {
      if (typeof openEntryNotificationView === "function") openEntryNotificationView();
      else if (entry.me) appendCard(notificationCard());
    });
  }
  const loginError = new URLSearchParams(location.search).get("login_error");
  if (loginError) {
    $("entry-login-error").textContent = ENTRY_LOGIN_ERRORS[loginError] || "登录失败，请重新登录或联系管理员。";
    $("entry-login-error").classList.remove("hidden");
  }
  connectEntry().catch(report);
}

bootEntry();
