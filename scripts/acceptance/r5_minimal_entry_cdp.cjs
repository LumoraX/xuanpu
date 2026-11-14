// R5-E 验收辅助：经 Chrome DevTools 协议把页面模拟成 375px 移动设备，量横向溢出并截图。
// 用法：node r5_minimal_entry_cdp.cjs <chrome> <起始地址> <截图路径> <配置目录>
// 只连本机 loopback；不需要第三方依赖（Node 自带 WebSocket）。
"use strict";
const { spawn } = require("node:child_process");
const fs = require("node:fs");

const [binary, url, shot, profile] = process.argv.slice(2);

function launch() {
  const child = spawn(binary, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  return new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(new Error("CHROME_NO_DEVTOOLS")), 20000);
    child.stderr.on("data", chunk => {
      text += chunk;
      const found = text.match(/DevTools listening on (ws:\/\/\S+)/);
      if (found) { clearTimeout(timer); resolve({ child, endpoint: found[1] }); }
    });
  });
}

function client(endpoint) {
  const socket = new WebSocket(endpoint);
  let next = 0;
  const waiting = new Map();
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id && waiting.has(message.id)) {
      const { resolve, reject } = waiting.get(message.id);
      waiting.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++next;
    waiting.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  return new Promise(resolve => socket.addEventListener("open", () => resolve({ send, close: () => socket.close() })));
}

const MEASURE = `(() => {
  const send = document.getElementById("send").getBoundingClientRect();
  const overview = document.getElementById("overview");
  return JSON.stringify({ path: location.pathname, viewport: innerWidth,
    scroll_width: document.documentElement.scrollWidth, send_right: Math.round(send.right),
    blocks: overview ? overview.children.length : 0 });
})()`;
const READY = `location.pathname.endsWith("entry.html") && !!document.getElementById("overview")
  && document.getElementById("overview").children.length > 0`;

async function main() {
  const { child, endpoint } = await launch();
  try {
    const cdp = await client(endpoint);
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const run = (method, params) => cdp.send(method, params, sessionId);
    await run("Emulation.setDeviceMetricsOverride", { width: 375, height: 812, deviceScaleFactor: 2, mobile: true });
    await run("Page.enable");
    await run("Page.navigate", { url });
    const deadline = Date.now() + 30000;
    for (;;) {
      const { result } = await run("Runtime.evaluate", { expression: READY, returnByValue: true });
      if (result.value === true) break;
      if (Date.now() > deadline) throw new Error("ENTRY_NOT_READY");
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    await new Promise(resolve => setTimeout(resolve, 500));
    const { result } = await run("Runtime.evaluate", { expression: MEASURE, returnByValue: true });
    const { data } = await run("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(shot, Buffer.from(data, "base64"));
    console.log(result.value);
    cdp.close();
  } finally {
    child.kill();
  }
}

main().catch(error => { console.error(error.message); process.exit(1); });
