// 提示：服务端确定性生成（随概览返回，最多 3 条），点一下填进输入框；
// 系统状态提示（例如管家暂不可用）显示在同一位置，并说明此时还能做什么。
"use strict";

let systemStatus = null;

function renderTips(overview) {
  const box = $("tips");
  const nodes = [];
  if (systemStatus) nodes.push(el("p", systemStatus, "status"));
  for (const tip of (overview && overview.tips) || []) {
    nodes.push(button(tip.text, () => { $("message").value = tip.prompt || tip.text; $("message").focus(); }));
  }
  box.replaceChildren(...nodes);
}

async function refreshTips() {
  // 提示随概览一起返回；这里只负责在概览读取前先显示系统状态
  renderTips(null);
}

function showSystemStatus(error) {
  if (!error || (error.status !== 503 && error.status !== 429)) return;
  systemStatus = error.status === 503
    ? "管家暂时不可用，你的原话已保留。概览、通知与「更多」里的功能照常可用。"
    : "本次请求的处理额度已用完，请稍后再试；「更多」里的功能照常可用。";
  renderTips(null);
  refreshOverview().catch(report);
}

function clearSystemStatus() {
  systemStatus = null;
}
