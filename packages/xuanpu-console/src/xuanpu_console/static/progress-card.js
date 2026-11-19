// 进展卡：一条 trace 一张。订阅 /v1/traces/{id}/events（快照 → 变化 → end），
// 支持 SVG 时由小院（courtyard-card.js，R7）演出并讲「经过」，执行树与瀑布放进「执行树」页签；
// 不支持时只显示执行树。记录不完整时明确标注。刷新后由主对话历史重建、重新订阅。
"use strict";

const STATE_NAMES = {
  queued: "排队中", running: "进行中", waiting_children: "等匠人", waiting_input: "等你回复",
  completed: "已完成", failed: "失败", cancelled: "已取消", unknown: "未知",
};
const KIND_NAMES = { butler: "管家", dispatch: "派工", group: "编组", artisan: "匠人", tool: "工具", wait: "等待" };
const MAX_FOLLOWED = 3;

function openProgressCard(traceId, options = {}) {
  const existing = entry.cards.get("trace:" + traceId);
  if (existing) { existing.follow(); return existing.node; }
  const progress = createProgress(traceId);
  entry.cards.set("trace:" + traceId, progress);
  appendCard(progress.node);
  // 历史里的旧 trace 只在最近几张上自动订阅，其余点「查看进展」再订阅
  const followed = [...entry.cards.values()].filter(c => c.following).length;
  if (options.follow || followed < MAX_FOLLOWED) progress.follow();
  return progress.node;
}

function createProgress(traceId) {
  const node = card("进展"), summary = el("p", "尚未读取", "meta"), tree = el("div", null, "tree hidden");
  const fall = el("div", null, "waterfall hidden"), row = el("div", null, "row");
  const progress = { node, following: false, spans: new Map(), header: null };
  const toggleTree = button("展开执行树", () => {
    tree.classList.toggle("hidden");
    toggleTree.textContent = tree.classList.contains("hidden") ? "展开执行树" : "收起执行树";
  });
  const showFall = button("看瀑布", () => loadWaterfall(traceId, fall));
  const court = Boolean(globalThis.Courtyard && Courtyard.card && Courtyard.card.supported());
  progress.follow = () => {
    if (progress.following) return;
    progress.following = true;
    followTrace(traceId, progress, frame => {
      renderProgress(progress, summary, tree);
      if (court) Courtyard.card.update(progress, frame);
    });
  };
  if (court) {
    row.append(button("查看进展", () => progress.follow()));
    node.append(summary, row);
    const panel = Courtyard.card.mount(progress, node);
    tree.classList.remove("hidden");
    panel.append(tree, button("看瀑布", () => loadWaterfall(traceId, fall)), fall);
    return progress;
  }
  row.append(button("查看进展", () => progress.follow()), toggleTree, showFall);
  node.append(summary, row, tree, fall);
  return progress;
}

// 读 SSE：fetch 流按行解析；身份切换时随代际一起中止。
async function followTrace(traceId, progress, render) {
  const epoch = entry.epoch, controller = new AbortController();
  const stop = () => controller.abort();
  entry.cleanups.add(stop);
  try {
    const response = await fetch("/v1/traces/" + encodeURIComponent(traceId) + "/events", {
      credentials: "same-origin", signal: controller.signal, headers: { "X-Requested-By": "xuanpu" } });
    if (!response.ok) throw new Error("进展读取失败（" + response.status + "）");
    await readFrames(response, (name, data) => {
      if (isStale(epoch)) return;
      applyFrame(progress, name, data);
      render(name);
    });
  } catch (error) {
    if (!isStale(epoch) && error.name !== "AbortError") report(error);
  } finally {
    entry.cleanups.delete(stop);
    if (!isStale(epoch)) progress.following = false;
  }
}

function applyFrame(progress, name, data) {
  if (name === "snapshot") progress.spans.clear();
  for (const span of data.spans || []) progress.spans.set(span.span_id, span);
  progress.header = data;
}

async function readFrames(response, onFrame) {
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = parseFrame(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      if (frame) onFrame(frame.name, frame.data);
    }
  }
}

function parseFrame(text) {
  let name = null;
  const data = [];
  for (const line of text.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event: ")) name = line.slice(7);
    else if (line.startsWith("data: ")) data.push(line.slice(6));
  }
  return name ? { name, data: data.length ? JSON.parse(data.join("\n")) : {} } : null;
}

function renderProgress(progress, summary, tree) {
  const header = progress.header || {};
  const spans = [...progress.spans.values()];
  const waiting = spans.some(s => s.state === "waiting_input");
  const state = header.active ? (waiting ? "等你回复" : "进行中") : "已结束";
  summary.textContent = (header.title || "") + " · " + state + " · " + spans.length + " 个执行单元";
  summary.className = header.incomplete || header.truncated ? "meta warn" : "meta";
  if (header.incomplete || header.truncated) summary.textContent += " · 记录不完整";
  tree.replaceChildren(spanTree(spans, null, new Set()));
}

// 执行树：按父子嵌套；父级缺失的挂到根层，不递归挂死。
function spanTree(spans, parent, seen) {
  const list = el("ul");
  const known = new Set(spans.map(s => s.span_id));
  for (const span of spans) {
    const isChild = parent === null ? !span.parent_span_id || !known.has(span.parent_span_id) : span.parent_span_id === parent;
    if (!isChild || seen.has(span.span_id)) continue;
    seen.add(span.span_id);
    const item = el("li", (KIND_NAMES[span.kind] || span.kind) + " · " + span.name + " · " +
      (STATE_NAMES[span.state] || span.state) + (span.incomplete ? " · 记录不完整" : ""));
    item.appendChild(spanTree(spans, span.span_id, seen));
    list.appendChild(item);
  }
  return list;
}

async function loadWaterfall(traceId, box) {
  const epoch = entry.epoch;
  try {
    const body = await entryApi("GET", "/v1/traces/" + encodeURIComponent(traceId) + "/waterfall");
    if (isStale(epoch)) return;
    const rows = body.rows || [];
    const total = Math.max(1, ...rows.map(r => r.offset_ms + (r.duration_ms || 0)));
    box.replaceChildren(...rows.map(r => waterfallRow(r, total)));
    box.classList.remove("hidden");
  } catch (error) {
    if (!isStale(epoch)) report(error);
  }
}

function waterfallRow(row, total) {
  const line = el("div", "  ".repeat(row.depth) + row.name + " " +
    (row.duration_ms === null ? "进行中" : row.duration_ms + " ms"), "meta");
  const bar = el("div", null, "bar" + (row.duration_ms === null ? " running" : ""));
  bar.style.marginLeft = (100 * row.offset_ms / total).toFixed(1) + "%";
  bar.style.width = row.duration_ms === null ? "2%" : Math.max(1, 100 * row.duration_ms / total).toFixed(1) + "%";
  line.appendChild(bar);
  return line;
}
