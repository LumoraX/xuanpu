// 进展卡：一条 trace 一张。订阅 /v1/traces/{id}/events（快照 → 变化 → end），
// 支持 SVG 时由小院（courtyard-card.js，R7）演出并讲「经过」，执行树与瀑布放进「执行树」页签；
// 不支持时只显示执行树。记录不完整时明确标注。刷新后由主对话历史重建、重新订阅。
"use strict";

const STATE_NAMES = {
  queued: "排队中", running: "进行中", waiting_children: "等匠人", waiting_input: "等你回复",
  completed: "已完成", failed: "失败", cancelled: "已取消", unknown: "未知",
};
const KIND_NAMES = { butler: "管家", dispatch: "派工", group: "编组", artisan: "匠人", tool: "工具", wait: "等待" };
const TOOL_DISPLAY_NAMES = {
  read_personal: "查资料 / 读文档",
  act_personal: "私域办理 / 归档",
  propose: "拟定确认卡",
  list_artisans: "查看可用匠人",
  dispatch_artisan: "派工匠人",
  dispatch_group: "多匠人编排",
  check_dispatch: "检查派工进展",
  check_group: "检查编组进展",
  update_task_board: "更新编排任务板",
  diagnose_trace: "执行诊断",
  forge_artisan: "造匠工作坊",
  answer_pending: "转交员工答复",
  answer_artisan: "代答匠人",
  rate_divergence: "会诊分歧比对",
};
const MAX_FOLLOWED = 3;

function openProgressCard(traceId, options = {}) {
  const existing = entry.cards.get("trace:" + traceId);
  if (existing) {
    existing.follow();
    if (options.focus && existing.court && globalThis.Courtyard && Courtyard.stage) {
      Courtyard.stage.focus(existing.court, { expand: Boolean(options.expand) });
    }
    return existing.node;
  }
  const progress = createProgress(traceId, options);
  entry.cards.set("trace:" + traceId, progress);
  appendCard(progress.node);
  // 历史里的旧 trace 只在最近几张上自动订阅，其余点「查看进展」再订阅
  const followed = [...entry.cards.values()].filter(c => c.following).length;
  if (options.follow || followed < MAX_FOLLOWED) progress.follow();
  return progress.node;
}

function createProgress(traceId, options = {}) {
  const court = Boolean(globalThis.Courtyard && Courtyard.card && Courtyard.card.supported());
  const useStage = court && Boolean(Courtyard.stage && Courtyard.stage.mounted());
  const node = useStage ? el("div", null, "progress-inline") : card("进展");
  const summary = el(useStage ? "span" : "p", useStage ? "进展读取中…" : "尚未读取", "meta");
  const flowBox = el("div", null, "progress-inline-flow");
  const tree = el("div", null, "tree hidden");
  const fall = el("div", null, "waterfall hidden"), row = el("div", null, "row");
  // focus：刚发出的请求，常驻小院立刻跟上它
  const progress = { traceId, node, following: false, spans: new Map(), header: null, wantsFocus: Boolean(options.focus),
    waitStart: Boolean(options.focus), audits: new Map() };
  const toggleTree = button("展开执行树", () => {
    tree.classList.toggle("hidden");
    toggleTree.textContent = tree.classList.contains("hidden") ? "展开执行树" : "收起执行树";
  });
  const showFall = button("看瀑布", () => loadWaterfall(traceId, fall));
  progress.follow = () => {
    if (progress.following) return;
    progress.following = true;
    followTrace(traceId, progress, frame => {
      renderProgress(progress, summary, tree, flowBox);
      if (court) Courtyard.card.update(progress, frame);
    });
  };
  if (useStage) {
    // 对话流中直接展示精炼的可视化执行编排流程（DAG 节点与并行波次），完整执行树在小院折叠区
    const headRow = el("div", null, "progress-inline-head");
    headRow.append(summary);
    node.append(headRow);
    const panel = Courtyard.card.mount(progress, headRow);
    node.append(flowBox);
    tree.classList.remove("hidden");
    panel.append(tree, button("看瀑布", () => loadWaterfall(traceId, fall)), fall);
    return progress;
  }
  if (court) {
    row.append(button("查看进展", () => progress.follow()));
    node.append(summary, row, flowBox);
    const panel = Courtyard.card.mount(progress, node);
    tree.classList.remove("hidden");
    panel.append(tree, button("看瀑布", () => loadWaterfall(traceId, fall)), fall);
    return progress;
  }
  row.append(button("查看进展", () => progress.follow()), toggleTree, showFall);
  node.append(summary, row, flowBox, tree, fall);
  return progress;
}

// 读 SSE：fetch 流按行解析；身份切换时随代际一起中止。
async function followTrace(traceId, progress, render) {
  const epoch = entry.epoch, controller = new AbortController();
  const stop = () => controller.abort();
  entry.cleanups.add(stop);
  try {
    const response = await openEvents(traceId, progress, controller, epoch);
    if (!response) return;
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

// 刚发出的请求：管家这一轮开始后执行记录才落库（约半秒一批），之前读是 404，等它出现再订阅。
const START_WAIT_TRIES = 120;
async function openEvents(traceId, progress, controller, epoch) {
  for (let tries = 0; ; tries++) {
    const response = await fetch("/v1/traces/" + encodeURIComponent(traceId) + "/events", {
      credentials: "same-origin", signal: controller.signal, headers: { "X-Requested-By": "xuanpu" } });
    if (response.status !== 404 || !progress.waitStart || tries >= START_WAIT_TRIES) return response;
    await new Promise(resolve => setTimeout(resolve, 1000));
    if (isStale(epoch)) return null;
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

function fmtSpanDuration(span) {
  const s = span && span.started_at ? Date.parse(span.started_at) : NaN;
  const e = span && span.ended_at ? Date.parse(span.ended_at) : NaN;
  if (Number.isNaN(s) || Number.isNaN(e) || e < s) return span && span.state === "running" ? "执行中…" : "";
  const ms = Math.round(e - s);
  return ms < 1000 ? ms + "ms" : (ms / 1000).toFixed(1) + "s";
}

function spanLabel(span) {
  if (span.kind === "butler") return "管家主控";
  if (span.kind === "dispatch") return "匠人 · " + String(span.name || "").split("/")[0];
  if (span.kind === "group") return "编组协作";
  return TOOL_DISPLAY_NAMES[span.name] || span.name || KIND_NAMES[span.kind] || "步骤";
}

// 将扁平的执行 span 按时间波次聚合成 DAG 编排图（并行调用落入同一纵列，串行波次用箭头连接）。
function renderExecutionDag(progress, spans, tree) {
  if (!spans || !spans.length) return null;
  const ordered = [...spans].sort((a, b) => (Date.parse(a.started_at || 0) - Date.parse(b.started_at || 0)) || (a.span_id < b.span_id ? -1 : 1));
  const roots = ordered.filter((s) => s.kind === "butler" && !s.parent_span_id);
  const children = ordered.filter((s) => !(s.kind === "butler" && !s.parent_span_id));
  const stages = [];
  if (roots.length) stages.push([roots[0]]);
  for (const sp of children) {
    const startMs = Date.parse(sp.started_at || 0) || 0;
    const prevStage = stages.length > (roots.length ? 1 : 0) ? stages[stages.length - 1] : null;
    const prevStart = prevStage ? (Date.parse(prevStage[0].started_at || 0) || 0) : -1;
    // 同一波次并行发起的工具/派工（启动时间差 ≤ 1.2s 且同父）归入同一并行列
    if (prevStage && prevStage.length < 6 && Math.abs(startMs - prevStart) <= 1200 && prevStage[0].parent_span_id === sp.parent_span_id) {
      prevStage.push(sp);
    } else {
      stages.push([sp]);
    }
  }
  if (!stages.length) return null;

  const dag = el("div", null, "exec-dag");
  dag.setAttribute("role", "list");
  dag.setAttribute("aria-label", "执行编排流程");
  stages.forEach((stageSpans, idx) => {
    if (idx > 0) dag.appendChild(el("div", "→", "exec-dag-arrow"));
    const col = el("div", null, "exec-dag-stage");
    for (const sp of stageSpans) {
      const icon = sp.state === "completed" ? "✓" : sp.state === "failed" ? "✕" : sp.state === "cancelled" ? "⊘" : "▸";
      const stCls = sp.state === "completed" ? "completed" : sp.state === "failed" ? "failed" : "running";
      const nodeBtn = el("button", null, "exec-dag-node " + stCls);
      nodeBtn.type = "button";
      nodeBtn.title = "点击查看执行详情";
      const head = el("div", null, "exec-dag-node-head");
      head.append(el("span", icon), el("span", spanLabel(sp)));
      const dur = fmtSpanDuration(sp);
      const subText = [STATE_NAMES[sp.state] || sp.state, dur].filter(Boolean).join(" · ");
      nodeBtn.append(head, el("div", subText, "exec-dag-node-sub"));
      nodeBtn.addEventListener("click", () => {
        if (progress.court && globalThis.Courtyard && Courtyard.stage && Courtyard.stage.mounted()) {
          Courtyard.stage.focus(progress.court, { expand: true });
        }
        if (sp.kind === "tool" && tree) {
          tree.classList.remove("hidden");
          toggleSpanAudit(progress, sp, tree);
        }
      });
      col.appendChild(nodeBtn);
    }
    dag.appendChild(col);
  });
  return dag;
}

function renderProgress(progress, summary, tree, flowBox) {
  const header = progress.header || {};
  const spans = [...progress.spans.values()];
  const waiting = spans.some(s => s.state === "waiting_input");
  const state = header.active ? (waiting ? "等你回复" : "进行中") : "已完成";
  const rawTitle = header.title || "";
  const title = rawTitle === "butler" ? "管家" : (KIND_NAMES[rawTitle] || rawTitle || "管家");
  summary.textContent = title + " · " + state + " · " + spans.length + " 步";
  summary.className = header.incomplete || header.truncated ? "meta warn" : "meta";
  if (header.incomplete || header.truncated) summary.textContent += " · 记录不完整";
  if (flowBox) {
    const dag = renderExecutionDag(progress, spans, tree);
    if (dag) flowBox.replaceChildren(dag);
    else flowBox.replaceChildren();
  }
  tree.replaceChildren(spanTree(progress, spans, null, new Set()));
}

function prettyJsonText(raw) {
  if (raw == null || raw === "") return "（无）";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return String(raw);
  }
}

function renderAuditDetailBox(state) {
  const box = el("div", null, "span-audit");
  if (state.loading) { box.append(el("p", "正在读取审计详情…", "meta")); return box; }
  if (state.error) { box.append(el("p", "读取失败：" + state.error, "warn")); return box; }
  const d = state.data || {};
  const metaParts = [];
  if (d.call_id) metaParts.push("call_id: " + d.call_id);
  if (typeof d.duration_ms === "number") metaParts.push(d.duration_ms + " ms");
  if (d.is_error === true) metaParts.push("返回错误");
  else if (d.is_error === false) metaParts.push("调用成功");
  if (metaParts.length) box.append(el("p", metaParts.join(" · "), "meta"));
  box.append(el("div", "入参 (arguments)", "span-audit-label"));
  const argPre = el("pre", prettyJsonText(d.arguments), "span-code");
  box.append(argPre);
  box.append(el("div", "返回 (output)", "span-audit-label"));
  const outPre = el("pre", prettyJsonText(d.output), "span-code");
  box.append(outPre);
  return box;
}

async function toggleSpanAudit(progress, span, tree) {
  const cur = progress.audits.get(span.span_id) || { open: false, loading: false, data: null, error: null };
  if (cur.open) {
    cur.open = false;
    progress.audits.set(span.span_id, cur);
    tree.replaceChildren(spanTree(progress, [...progress.spans.values()], null, new Set()));
    return;
  }
  cur.open = true;
  if (!cur.data || span.state === "running") {
    cur.loading = true;
    cur.error = null;
    progress.audits.set(span.span_id, cur);
    tree.replaceChildren(spanTree(progress, [...progress.spans.values()], null, new Set()));
    const epoch = entry.epoch;
    try {
      const data = await entryApi(
        "GET",
        "/v1/traces/" + encodeURIComponent(progress.traceId) + "/spans/" + encodeURIComponent(span.span_id) + "/audit"
      );
      if (isStale(epoch)) return;
      cur.loading = false;
      cur.data = data;
    } catch (error) {
      if (isStale(epoch)) return;
      cur.loading = false;
      cur.error = error.message || "未知错误";
    }
  }
  progress.audits.set(span.span_id, cur);
  tree.replaceChildren(spanTree(progress, [...progress.spans.values()], null, new Set()));
}

// 执行树：按父子嵌套；父级缺失的挂到根层，不递归挂死。工具节点支持按需展开审计入参与返回值。
function spanTree(progress, spans, parent, seen) {
  const list = el("ul");
  const known = new Set(spans.map(s => s.span_id));
  for (const span of spans) {
    const isChild = parent === null ? !span.parent_span_id || !known.has(span.parent_span_id) : span.parent_span_id === parent;
    if (!isChild || seen.has(span.span_id)) continue;
    seen.add(span.span_id);
    const item = el("li");
    const head = el("span", (KIND_NAMES[span.kind] || span.kind) + " · " + span.name + " · " +
      (STATE_NAMES[span.state] || span.state) + (span.incomplete ? " · 记录不完整" : ""));
    item.append(head);
    if (span.kind === "tool") {
      const auditState = progress.audits && progress.audits.get(span.span_id);
      const isOpen = Boolean(auditState && auditState.open);
      const btn = button(isOpen ? "收起详情" : "审计详情", (ev) => {
        const treeEl = ev.currentTarget.closest(".tree");
        toggleSpanAudit(progress, span, treeEl);
      }, "span-audit-btn");
      item.append(" ", btn);
      if (isOpen) item.append(renderAuditDetailBox(auditState));
    }
    item.appendChild(spanTree(progress, spans, span.span_id, seen));
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
