// 本地启动页只请求连接预置企业服务，不接收用户输入的地址或密钥。
const status = document.querySelector("#status");
const detail = document.querySelector("#detail");
const retry = document.querySelector("#retry");
async function connect() {
  retry.disabled = true;
  document.querySelector("#connection").setAttribute("aria-busy", "true");
  status.textContent = "正在连接你的工作站…";
  detail.textContent = "请稍候";
  try {
    await window.__TAURI__.core.invoke("connect_service");
  } catch (error) {
    status.textContent = "暂时无法连接玄圃";
    detail.textContent = typeof error === "string" ? error : "请稍后重试，或联系企业管理员。";
    retry.hidden = false;
    retry.disabled = false;
    document.querySelector("#connection").setAttribute("aria-busy", "false");
  }
}
retry.addEventListener("click", connect);
connect();
