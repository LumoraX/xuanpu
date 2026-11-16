// 本地启动页：显示并修改本机服务器设置，然后连接；只有本页能调用原生命令。
const $ = (selector) => document.querySelector(selector);
const invoke = (command, args) => window.__TAURI__.core.invoke(command, args);
const status = $("#status");
const detail = $("#detail");
const retry = $("#retry");
const form = $("#settings");
const formError = $("#form-error");

// 读取当前设置并更新服务器一行；返回设置摘要
async function loadSettings() {
  const view = await invoke("get_settings");
  $("#server").textContent = view.service_url || "未设置";
  $("#url").value = view.service_url || "";
  if (view.config_path) $("#config-path").textContent = view.config_path;
  return view;
}

// 显示连接失败并允许重试
function showFailure(message) {
  status.textContent = "暂时无法连接玄圃";
  detail.textContent = message;
  retry.hidden = false;
  retry.disabled = false;
  $("#connection").setAttribute("aria-busy", "false");
}

// 连接当前服务器；成功时原生侧导航到工作站
async function connect() {
  retry.disabled = true;
  $("#connection").setAttribute("aria-busy", "true");
  status.textContent = "正在连接你的工作站…";
  detail.textContent = "请稍候";
  try {
    await invoke("connect_service");
  } catch (error) {
    showFailure(typeof error === "string" ? error : "请稍后重试，或联系企业管理员。");
  }
}

// 首次使用：还没有服务器地址，请用户填写，不自动连接
function showSetup() {
  status.textContent = "欢迎使用玄圃";
  detail.textContent = "请填写要连接的玄圃服务器地址。";
  retry.hidden = true;
  $("#connection").setAttribute("aria-busy", "false");
  openForm();
}

// 展开设置表单
function openForm() {
  form.hidden = false;
  formError.hidden = true;
  $("#url").focus();
}

$("#toggle").addEventListener("click", () => (form.hidden ? openForm() : (form.hidden = true)));
retry.addEventListener("click", connect);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  try {
    await invoke("save_settings", { serviceUrl: $("#url").value });
    await loadSettings();
    form.hidden = true;
    connect();
  } catch (error) {
    formError.textContent = typeof error === "string" ? error : "保存失败";
    formError.hidden = false;
  }
});
$("#reset").addEventListener("click", async () => {
  try {
    await invoke("reset_settings");
    await loadSettings();
    showSetup();
  } catch (error) {
    formError.textContent = typeof error === "string" ? error : "清除失败";
    formError.hidden = false;
  }
});
$("#logs").addEventListener("click", () => invoke("open_logs").catch(() => {}));

// 启动：读设置；配置有误或从菜单打开设置时不自动连接
(async () => {
  try {
    const view = await loadSettings();
    if (view.needs_setup) {
      showSetup();
    } else if (view.error) {
      showFailure(view.error);
      openForm();
    } else if (view.open_settings) {
      status.textContent = "服务器设置";
      detail.textContent = "修改后保存即可重新连接。";
      $("#connection").setAttribute("aria-busy", "false");
      retry.hidden = false;
      retry.disabled = false;
      openForm();
    } else {
      connect();
    }
  } catch (error) {
    showFailure(typeof error === "string" ? error : "无法读取客户端设置。");
  }
})();
