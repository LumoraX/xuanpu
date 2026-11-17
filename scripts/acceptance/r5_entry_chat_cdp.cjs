// R5-E 验收辅助：在真实 Chrome 的极简入口里逐条对管家说话，等回复与卡片；
// 草稿卡点「确认」并等「已保存」，列表卡等读取完成。结果写成 JSON。
// 用法：node r5_entry_chat_cdp.cjs <chrome> <起始地址> <配置目录> <步骤JSON> <输出JSON> <截图路径>
// 只连本机 loopback；不需要第三方依赖（Node 自带 WebSocket）。
"use strict";
const { spawn } = require("node:child_process");
const fs = require("node:fs");

const [binary, url, profile, stepsPath, outPath, shot] = process.argv.slice(2);
const steps = JSON.parse(fs.readFileSync(stepsPath, "utf8"));
const REPLY_MS = 150000;   // 管家一轮服务端最长 90 秒，留余量
const CARD_MS = 30000;

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

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// 页面里执行的表达式：只读 DOM 与页面自己的状态，不改产品代码。
const READY = `location.pathname.endsWith("entry.html") && !!document.getElementById("overview")
  && document.getElementById("overview").children.length > 0 && !!entry.me`;
const COUNT = `document.getElementById("transcript").children.length`;
const submit = text => `(() => { const box = document.getElementById("message");
  box.value = ${JSON.stringify(text)}; document.getElementById("composer").requestSubmit(); return true; })()`;
const settled = before => `!entry.sending && document.getElementById("transcript").children.length > ${before} + 1`;
const collect = before => `JSON.stringify([...document.getElementById("transcript").children].slice(${before}).map(n => ({
  kind: n.classList.contains("card") ? "card" : (n.classList.contains("assistant") ? "assistant" : "user"),
  title: n.querySelector("h3") ? n.querySelector("h3").textContent : null, text: n.innerText })))`;
// 新出现的第一张卡：草稿卡点「确认」；返回是否点到
const clickConfirm = before => `(() => {
  const cards = [...document.getElementById("transcript").children].slice(${before}).filter(n => n.classList.contains("card"));
  for (const card of cards) { const b = [...card.querySelectorAll("button")].find(x => x.textContent === "确认");
    if (b) { b.click(); return true; } }
  return false; })()`;
const cardsText = before => `[...document.getElementById("transcript").children].slice(${before})
  .filter(n => n.classList.contains("card")).map(n => n.innerText).join("\\n---\\n")`;

async function until(run, expression, ms, what) {
  const deadline = Date.now() + ms;
  for (;;) {
    const { result } = await run("Runtime.evaluate", { expression, returnByValue: true });
    if (result.value) return result.value;
    if (Date.now() > deadline) throw new Error("TIMEOUT " + what);
    await sleep(500);
  }
}

async function value(run, expression) {
  const { result } = await run("Runtime.evaluate", { expression, returnByValue: true });
  return result.value;
}

// 一步：说一句话 → 等回复 → 按预期处理卡片。出错只记在这一步，不中断后面的步骤。
async function step(run, item) {
  const before = await value(run, COUNT);
  await value(run, submit(item.text));
  const record = { id: item.id, module: item.module, expect: item.expect, text: item.text };
  try {
    await until(run, settled(before), REPLY_MS, "reply " + item.id);
    await sleep(1000);
    if (item.expect === "confirm") {
      record.clicked = await value(run, clickConfirm(before));
      if (record.clicked) {
        await until(run, `(${cardsText(before)}).includes("已保存")`, CARD_MS, "saved " + item.id)
          .then(() => { record.saved = true; }, () => { record.saved = false; });
      }
    } else {
      await until(run, `!(${cardsText(before)}).includes("读取中")`, CARD_MS, "list " + item.id)
        .then(() => { record.loaded = true; }, () => { record.loaded = false; });
    }
  } catch (error) {
    record.error = error.message;
  }
  record.nodes = JSON.parse(await value(run, collect(before)));
  return record;
}

async function main() {
  const { child, endpoint } = await launch();
  try {
    const cdp = await client(endpoint);
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const run = (method, params) => cdp.send(method, params, sessionId);
    await run("Page.enable");
    await run("Page.navigate", { url });
    await until(run, READY, 30000, "entry ready");
    const results = [];
    for (const item of steps) results.push(await step(run, item));
    const { data } = await run("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(shot, Buffer.from(data, "base64"));
    fs.writeFileSync(outPath, JSON.stringify(results, null, 2));
    console.log(JSON.stringify({ steps: results.length }));
    cdp.close();
  } finally {
    child.kill();
  }
}

main().catch(error => { console.error(error.message); process.exit(1); });
