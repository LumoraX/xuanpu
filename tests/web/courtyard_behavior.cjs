// 小院（R7）：纯模型（家具归类、节拍、经过、角色位置）与角色画法，在 Node 里直接跑真实脚本。
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
// vm 里建的数组与对象原型不同，比较前转成普通数据
const same = (actual, expected, message) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);

function load(dir, names, extra = {}) {
  const context = vm.createContext({ console, Math, JSON, Date, Map, Set, Number, String, Object, Array, ...extra });
  for (const name of names) vm.runInContext(fs.readFileSync(path.join(dir, name), "utf8"), context, { filename: name });
  return context.Courtyard;
}

const T = s => new Date(Date.UTC(2026, 10, 19, 8, 0, s)).toISOString();
const span = (span_id, kind, name, parent, state, s, extra = {}) =>
  ({ span_id, kind, name, parent_span_id: parent, state, started_at: T(s), ended_at: null, ref: null, say: null, ...extra });
const none = { dispatch: () => undefined, group: () => undefined };

function stations(C) {
  const cases = {
    query_reimbursements: "cabinet", list_payments: "cabinet", read_skill: "books", read_documents: "books",
    run_script: "bench", "report.cost_trend": "tv", dashboard_view: "tv", "bank.list_payments": "computer",
    mcp__crm__update_ticket: "computer", call_skill: "bed", wait_any: "bed", "feishu.send_message": "computer",
  };
  for (const [name, want] of Object.entries(cases)) assert.equal(C.model.stationFor(name), want, name);
}

// 一位会话承载的匠人：派工 → 匠人执行 → 工具（带旁白）→ 完成 → 验收。
function soloTimeline() {
  const root = span("ex.b", "butler", "管家", null, "running", 0);
  const roster = span("tl.b1", "tool", "list_artisans", "ex.b", "completed", 1);
  const dp = span("dp.1", "dispatch", "finance/check", "ex.b", "running", 2, { ref: "dsp_1" });
  const ex = span("ex.a", "artisan", "报销核对", "dp.1", "running", 3);
  const tool = span("tl.a1", "tool", "query_reimbursements", "ex.a", "running", 4, { say: "我先去档案柜翻上月的报销单。" });
  return { root, roster, dp, ex, tool };
}

function beatsInOrder(C) {
  const t = soloTimeline();
  const details = { dispatch: () => ({ host: "session", instruction: "查上月没付的报销", checks: { terminal: true, succeeded: true, outputs: true, no_pending: true, no_unknown: true }, result: "两笔没付。", items: [] }), group: () => undefined };
  const step1 = [t.root, t.roster, t.dp, t.ex, t.tool];
  const first = C.model.diff([], step1, details).map(b => b.tool || b.type);
  same(first, ["think", "list_artisans", "brief", "wake", "work"], "管家先想、翻名册，再交代；匠人醒来去干活");
  assert.equal(C.model.diff(step1, step1, details).length, 0, "同一快照比两次不产生节拍");
  const done = [{ ...t.root, state: "waiting_children" }, t.roster, { ...t.dp, state: "completed", ended_at: T(9) },
    { ...t.ex, state: "completed" }, { ...t.tool, state: "completed", ended_at: T(7) }];
  const later = C.model.diff(step1, done, details);
  same(later.map(b => b.type), ["wait", "return", "check", "done_tool"]);
  assert.equal(later.find(b => b.type === "brief"), undefined);
  assert.equal(later.find(b => b.type === "check").checks.outputs, true);
  const work = C.model.diff([], step1, details).find(b => b.type === "work");
  assert.equal(work.station, "cabinet");
  assert.equal(work.label, "我先去档案柜翻上月的报销单。");
}

function storyUsesSayThenTemplate(C) {
  const t = soloTimeline();
  const second = span("tl.a2", "tool", "bank.list_payments", "ex.a", "completed", 6, { ended_at: T(8) });
  const details = { dispatch: () => ({ host: "session", instruction: "查上月没付的报销", checks: null, result: null, items: [] }), group: () => undefined };
  const items = C.model.story([t.root, t.roster, t.dp, t.ex, t.tool, second], details);
  same(items.map(i => i.type), ["say", "letter", "step", "step"]);
  assert.equal(items[1].text, "查上月没付的报销");
  assert.equal(items[1].how, "face");
  assert.equal(items[2].text, "我先去档案柜翻上月的报销单。");
  assert.ok(items[2].running);
  assert.equal(items[3].text, "我在电脑用了「bank.list_payments」。", "没有旁白时用模板句");
  assert.equal(items[3].ms, 2000);
}

function missingDetailsDegrade(C) {
  const t = soloTimeline();
  const failed = { dispatch: () => null, group: () => null };
  const items = C.model.story([t.root, t.dp, t.ex, t.tool], failed);
  assert.equal(items.filter(i => i.type === "letter").length, 0, "细节读不到时不出现空信纸");
  const brief = C.model.diff([], [t.root, t.dp], failed).find(b => b.type === "brief");
  assert.equal(brief.how, "face", "细节读不到时按当面交代");
  const finished = C.model.story([t.root, { ...t.dp, state: "failed", end_reason: "check_outputs", ended_at: T(9) }], failed);
  assert.equal(finished.find(i => i.type === "fail").text, "办完了，但没交出成果。");
}

function waitingForYou(C) {
  const t = soloTimeline();
  const asking = [t.root, { ...t.dp, state: "waiting_input" }, { ...t.ex, state: "waiting_input" }];
  const approval = { dispatch: () => ({ host: "task", items: [{ pending_id: "pnd_1", kind: "approval", question: "发出简报？", state: "open", created_at: T(5) }] }), group: () => undefined };
  const beats = C.model.diff([t.root, t.dp, t.ex], asking, approval);
  same(beats.map(b => b.type), ["seal"]);
  const pose = C.model.poses(asking, approval).finance;
  assert.equal(pose.prop, "paper");
  assert.ok(pose.red);
  const seal = C.model.story(asking, approval).find(i => i.type === "seal");
  assert.equal(seal.pending, "pnd_1");
  assert.ok(seal.open);
  const question = { dispatch: () => ({ host: "task", items: [{ pending_id: "pnd_2", kind: "question", question: "客户全称？", state: "open", created_at: T(5) }] }), group: () => undefined };
  assert.equal(C.model.diff([t.root, t.dp, t.ex], asking, question)[0].text, "客户全称？");
  assert.equal(C.model.poses(asking, question).finance.prop, "phone");
}

function consultSitsAtTable(C) {
  const root = span("ex.b", "butler", "管家", null, "running", 0);
  const gp = span("gp.1", "group", "报价会诊", "ex.b", "running", 1, { ref: "grp_1" });
  const dp = span("dp.1", "dispatch", "finance/check", "gp.1", "running", 2, { ref: "dsp_1" });
  const details = { dispatch: () => ({ host: "session", items: [] }),
    group: () => ({ steps: [{ mode: "consult", members: ["finance"] }], members: [{ step: 0, member: 1, dispatch_id: "dsp_1" }] }) };
  assert.equal(C.model.diff([], [root, gp, dp], details).find(b => b.type === "brief").how, "table");
  assert.equal(C.model.poses([root, gp, dp], details).finance.at, "table");
}

function finishedSnapshotPoses(C) {
  const t = soloTimeline();
  const done = [{ ...t.root, state: "completed", ended_at: T(10) }, { ...t.dp, state: "completed" }, { ...t.ex, state: "completed" }, { ...t.tool, state: "completed" }];
  const poses = C.model.poses(done, none);
  assert.equal(poses.butler.face, "happy");
  assert.equal(poses.finance.pose, "sleeping", "办完的匠人回垫子打盹");
  const summary = C.model.summary(done, Date.parse(T(30)));
  assert.equal(summary.state, "办完了");
  assert.equal(summary.artisans, 1);
  assert.equal(summary.tools, 1);
  assert.equal(summary.seconds, 10);
}

// 真实模型验收发现：管家第一轮交代完就回话（根完成），匠人还在干活；之后被唤醒再跑一轮复核。
function butlerRepliesBeforeArtisanFinishes(C) {
  const t = soloTimeline();
  const replied = { ...t.root, state: "completed", ended_at: T(3) };
  const working = [replied, t.dp, t.ex, t.tool];
  same(C.model.summary(working, Date.parse(T(5))).state, "正在办", "匠人还在干活就不算办完");
  assert.equal(C.model.story(working, none).filter(i => i.type === "done").length, 0);
  assert.equal(C.model.poses(working, none).butler.prop, "tea", "管家端茶等匠人");
  const review = span("ex.r", "butler", "管家", null, "completed", 12, { ended_at: T(14) });
  const all = [replied, { ...t.dp, state: "completed", ended_at: T(10) }, { ...t.ex, state: "completed" }, { ...t.tool, state: "completed", ended_at: T(8) }, review];
  const items = C.model.story(all, none);
  assert.equal(items.filter(i => i.type === "done").length, 1, "办完只说一次");
  assert.equal(items[items.length - 1].type, "done", "办完排在最后");
  assert.ok(items.some(i => i.text === "匠人交回了结果，我复核了一遍。"));
  same(C.model.summary(all, Date.parse(T(30))).seconds, 14);
  assert.ok(C.model.diff(working, all, none).some(b => b.tool === "review"));
}

// 真实模型的结果摘录会把旁白再说一遍，还带 Markdown 记号。
function resultDropsRepeatedNarration(C) {
  const said = ["我先查询工单 T-1001。", "现在运行 SLA 计算。"];
  const raw = "我先查询工单 T-1001。现在运行 SLA 计算。工单 **T-1001** 查询结果：\n\n\n- **状态：** 开放";
  assert.equal(C.model.resultText(raw, said), "工单 T-1001 查询结果：\n\n- 状态： 开放");
  assert.equal(C.model.resultText("我先查询工单 T-1001。", said), "我先查询工单 T-1001。", "全被去掉时保留原文");
}

// 真实会诊发现：span 全部结束后仍在等本人裁决；裁决项正文是各位的完整结论（带 Markdown）。
function rulingKeepsWaitingAndStaysShort(C) {
  const t = soloTimeline();
  const ended = [{ ...t.root, state: "completed", ended_at: T(3) }, { ...t.dp, state: "completed", ended_at: T(9) }, { ...t.ex, state: "completed" }];
  const long = "### 评估依据\n- **服务质量：** 无证据\n".repeat(20);
  const details = { dispatch: () => ({ host: "session", items: [{ pending_id: "pnd_r", kind: "ruling", question: long, state: "open", created_at: T(10) }] }), group: () => undefined };
  same(C.model.summary(ended, Date.parse(T(20)), details).state, "停在这儿，等你");
  const items = C.model.story(ended, details);
  assert.equal(items.filter(i => i.type === "done").length, 0, "还在等裁决就不说办完");
  const ruling = items.find(i => i.type === "ruling");
  assert.equal(ruling.text, "几位匠人的结论分歧大，这事我不能替你定，请你裁决。");
  assert.ok(!ruling.more.includes("**") && !ruling.more.includes("###"), "结论去掉 Markdown 记号后折叠");
  assert.equal(ruling.pending, "pnd_r");
  const butler = C.model.poses(ended, details).butler;
  assert.ok(butler.red && butler.at === "table", "管家在圆桌边等你裁决");
}

// 真实试用发现：工具一眨眼就完，匠人在两步之间想时应留在上一步的家具旁，而不是坐回垫子。
function artisanStaysAtLastStationWhileThinking(C) {
  const t = soloTimeline();
  const between = [t.root, t.dp, t.ex, { ...t.tool, state: "completed", ended_at: T(5) }];
  const pose = C.model.poses(between, none).finance;
  assert.equal(pose.at, "station");
  assert.equal(pose.station, "cabinet");
  assert.equal(pose.bubble, "想想下一步…");
  const done = [t.root, { ...t.dp, state: "completed" }, { ...t.ex, state: "completed" }, { ...t.tool, state: "completed" }];
  assert.equal(C.model.poses(done, none).finance.at, "home", "办完回垫子");
}

// 真实试用发现：交代时只有「交代任务」看不出交代了什么；管家要说出任务的第一句。
function briefSaysTheTask(C) {
  assert.equal(C.model.briefLine("处理员工最新请求：报销上周五打车去客户公司 58 元，有发票。请按制度核对"), "报销上周五打车去客户公司 58 元…");
  assert.equal(C.model.briefLine("查上月没付的报销"), "查上月没付的报销");
  assert.equal(C.model.briefLine(undefined), "这件事交给你了");
  const t = soloTimeline();
  const details = { dispatch: () => ({ host: "session", instruction: "查上月差旅没付的报销。只读。", items: [] }), group: () => undefined };
  assert.equal(C.model.diff([t.root], [t.root, t.dp], details).find(b => b.type === "brief").text, "查上月差旅没付的报销");
  const pose = C.model.poses([t.root, t.dp], details).finance;
  assert.equal(pose.face, "open", "接下活还没动手时是醒着的");
  assert.equal(pose.bubble, "收到，开始办…");
}

// 真实使用发现：管家已调用并完成工具后（如多次 read_personal），正在组织回复时气泡不应停留在「我想想怎么办…」。
function butlerBubbleTracksRunningPhaseAndStep(C) {
  const root = span("ex.b", "butler", "管家", null, "running", 0);
  // 1. 刚开始还没调任何工具：仍是「我想想怎么办…」
  assert.equal(C.model.poses([root], none).butler.bubble, "我想想怎么办…");
  assert.equal(C.model.poses([root], { ...none, step: () => "正在理解你的请求…" }).butler.bubble, "我想想怎么办…");

  // 2. 实时步骤已推进到查档案（即使 span 还没落库）：气泡同步最新步骤
  assert.equal(
    C.model.poses([root], { ...none, step: () => "正在查询个人档案与历史记录…" }).butler.bubble,
    "正在查询个人档案与历史记录…"
  );

  // 3. 管家工具正在执行（刷新后无流式 step 时也能准确显示工具状态与道具）
  const readRunning = span("tl.b1", "tool", "read_personal", "ex.b", "running", 1);
  assert.equal(C.model.poses([root, readRunning], none).butler.bubble, "正在查询个人档案与工作台记录…");
  const actRunning = span("tl.b1b", "tool", "act_personal", "ex.b", "running", 1);
  const actPose = C.model.poses([root, actRunning], none).butler;
  assert.equal(actPose.bubble, "正在为你整理并办理私人事务…");
  assert.equal(actPose.prop, "quill");
  const proposeRunning = span("tl.b2", "tool", "propose", "ex.b", "running", 2);
  const proposePose = C.model.poses([root, { ...readRunning, state: "completed", ended_at: T(2) }, proposeRunning], none).butler;
  assert.equal(proposePose.bubble, "正在整理待确认卡片…");
  assert.equal(proposePose.prop, "quill");

  // 4. 多次工具均已完成、根 span 仍在运行（组织回复阶段）：即使没有流式 step 也不再回退成「我想想怎么办…」
  const readDone1 = { ...readRunning, state: "completed", ended_at: T(2) };
  const readDone2 = span("tl.b3", "tool", "read_personal", "ex.b", "completed", 2, { ended_at: T(3) });
  const afterTools = [root, readDone1, readDone2];
  assert.equal(C.model.poses(afterTools, none).butler.bubble, "处理完成，正在组织回复…");
  assert.equal(C.model.poses(afterTools, { ...none, step: () => "处理完成，正在组织回复…" }).butler.bubble, "处理完成，正在组织回复…");
  assert.equal(C.model.poses(afterTools, { ...none, step: () => "正在回复…" }).butler.bubble, "正在回复…");

  // 5. 同一帧并发多次相同管家工具只播一次节拍，避免反复刷屏阻塞后续状态对齐
  const beats = C.model.diff([root], afterTools, none);
  assert.equal(beats.filter(b => b.type === "butler_tool" && b.tool === "read_personal").length, 1);

  // 6. 被唤醒的第二轮复核正在运行时，展示复核状态而非第一轮的已完成态
  const t = soloTimeline();
  const reviewRunning = span("ex.r", "butler", "管家", null, "running", 12);
  const reviewing = [{ ...t.root, state: "completed", ended_at: T(3) }, { ...t.dp, state: "completed", ended_at: T(10) }, reviewRunning];
  const reviewPose = C.model.poses(reviewing, none).butler;
  assert.equal(reviewPose.prop, "magnifier");
  assert.equal(reviewPose.bubble, "复核完成，正在组织回复…");
}

function orphanSpansDoNotCrash(C) {
  const lost = [span("tl.x", "tool", "query", "ex.missing", "running", 1), span("a", "artisan", "x", "b", "running", 0), span("b", "artisan", "y", "a", "running", 0)];
  same(C.model.diff([], lost, none), []);
  same(C.model.story(lost, none), []);
}

function looks(C) {
  const a = C.art.lookFor("finance"), b = C.art.lookFor("finance");
  assert.equal(JSON.stringify(a), JSON.stringify(b), "同一个包名长一样");
  assert.ok(C.art.SPECIES[a.species] && C.art.COLORS[a.color] && C.art.ACCS[a.acc] !== undefined);
  const resolved = C.art.resolveLook({ butler: { species: "cat", name: "  阿福管家很长的名字 " }, finance: { name: "改名" } }, "butler");
  assert.equal(resolved.species, "cat");
  assert.equal(resolved.name, "阿福管家很长");
  assert.equal(C.art.resolveLook({ finance: { name: "改名" } }, "finance").name, "finance", "匠人名字来自包名");
  assert.ok(C.art.charMarkup(a).includes("eyes-open"));
  same(C.art.loadLooks(), {}, "存储不可用时为空，不抛异常");
}


// ---------- 进展卡接入：假 DOM + 记录调用的假场景 ----------
function element(tag = "div") {
  const classes = new Set(), attrs = {};
  const node = {
    tag, children: [], dataset: {}, listeners: {}, parentNode: null, _text: "", innerHTML: "", value: "",
    scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    get textContent() { return this._text + this.children.map(c => c.textContent).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); for (const c of String(v).split(/\s+/)) if (c) classes.add(c); },
    classList: { add: v => classes.add(v), remove: v => classes.delete(v), contains: v => classes.has(v),
      toggle: (v, yes) => { const on = yes === undefined ? !classes.has(v) : yes; if (on) classes.add(v); else classes.delete(v); return on; } },
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute: k => attrs[k],
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    replaceChildren(...cs) { this.children = []; this.append(...cs); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
  };
  return node;
}
const find = (root, pred, out = []) => { for (const c of root.children) { if (pred(c)) out.push(c); find(c, pred, out); } return out; };

function cardHarness(dir, routes, withSvg = true, names = ["courtyard-art.js", "courtyard-model.js", "courtyard-card.js"]) {
  const calls = [], cleanups = new Set();
  const document = { createElement: element };
  if (withSvg) document.createElementNS = (ns, tag) => element(tag);
  const el = (tag, text, cls) => { const n = element(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (label, onClick, cls) => { const b = el("button", label, cls); b.addEventListener("click", onClick); return b; };
  const entryApi = async (method, url) => {
    calls.push(url);
    const handler = routes[url];
    if (!handler) throw Object.assign(new Error("未预期的请求 " + url), { status: 404 });
    return handler();
  };
  const C = load(dir, names, { document, el, button, entryApi, entry: { cleanups, epoch: 0 }, isStale: () => false, openPending: id => calls.push("open:" + id) });
  const scene = [];
  C.scene = { create: () => ({ cast: keys => scene.push(["cast", [...keys]]), settle: p => scene.push(["settle", p]), play: b => scene.push(["play", b.map(x => x.type)]), restyle() {}, select() {}, destroy() {}, idle: async () => {} }) };
  return { C, calls, scene };
}

async function finishedSnapshotSettles(dir) {
  const t = soloTimeline();
  const { C, scene } = cardHarness(dir, {
    "/v1/assistant/dispatches/dsp_1": () => ({ host: "session", instruction: "查上月没付的报销", checks: { terminal: true, succeeded: true, outputs: true, no_pending: true, no_unknown: true }, result: "两笔没付。", items: [] }),
  });
  const progress = { spans: new Map(), header: {} }, node = element();
  C.card.mount(progress, node);
  for (const s of [{ ...t.root, state: "completed", ended_at: T(10) }, t.roster, { ...t.dp, state: "completed", ended_at: T(9) }, { ...t.ex, state: "completed" }, { ...t.tool, state: "completed", ended_at: T(6) }]) progress.spans.set(s.span_id, s);
  C.card.update(progress, "snapshot");
  await progress.court.chain;
  same(scene.map(x => x[0]), ["cast", "settle"], "已结束的快照直接画终态，不播动画");
  const feed = find(node, n => n.className === "cy-feed")[0];
  const text = feed.textContent;
  for (const want of ["查上月没付的报销", "我先去档案柜翻上月的报销单。", "两笔没付。", "五项都过了。", "这一趟办完了"]) assert.ok(text.includes(want), want);
  C.card.update(progress, "spans");
  await progress.court.chain;
  const lastPlay = () => scene.filter(x => x[0] === "play").slice(-1)[0];
  same(lastPlay(), ["play", []], "没有变化的帧不出节拍");
  await Promise.resolve();
  same(scene.slice(-1)[0][0], "settle", "动画排完后对齐到最新事实");
}

async function missingDetailStillRenders(dir) {
  const t = soloTimeline();
  const { C, scene, calls } = cardHarness(dir, {});
  const progress = { spans: new Map(), header: {} }, node = element();
  C.card.mount(progress, node);
  for (const s of [t.root, t.dp, t.ex]) progress.spans.set(s.span_id, s);
  C.card.update(progress, "snapshot");
  await progress.court.chain;
  progress.spans.set(t.tool.span_id, t.tool);
  C.card.update(progress, "spans");
  await progress.court.chain;
  assert.ok(calls.includes("/v1/assistant/dispatches/dsp_1"));
  same(scene.filter(x => x[0] === "play").slice(-1)[0], ["play", ["work"]]);
  const feed = find(node, n => n.className === "cy-feed")[0];
  assert.equal(find(feed, n => String(n.className).includes("letter")).length, 0, "细节读不到时没有空信纸");
  assert.ok(feed.textContent.includes("我先去档案柜翻上月的报销单。"));
}

async function openItemOffersAction(dir) {
  const t = soloTimeline();
  const { C, calls } = cardHarness(dir, {
    "/v1/assistant/dispatches/dsp_1": () => ({ host: "task", instruction: "发简报", checks: null, result: null,
      items: [{ pending_id: "pnd_9", kind: "approval", question: "把简报发给 3 位同事？", state: "open", created_at: T(5) }] }),
  });
  const progress = { spans: new Map(), header: {} }, node = element();
  C.card.mount(progress, node);
  for (const s of [t.root, { ...t.dp, state: "waiting_input" }, { ...t.ex, state: "waiting_input" }]) progress.spans.set(s.span_id, s);
  C.card.update(progress, "snapshot");
  await progress.court.chain;
  const go = find(node, n => n.tag === "button" && n.textContent === "去处理")[0];
  assert.ok(go, "轮到你时有「去处理」");
  go.listeners.click();
  assert.ok(calls.includes("open:pnd_9"));
}

// 复核发现：工具每变一次都重取派工细节，忙的 trace 上动画越拖越后；展开的「细节」每帧被收起。
async function toolStepsDoNotRefetchAndDetailsStayOpen(dir) {
  const t = soloTimeline();
  const { C, calls } = cardHarness(dir, { "/v1/assistant/dispatches/dsp_1": () => ({ host: "session", instruction: "查", items: [] }) });
  const progress = { spans: new Map(), header: {} }, node = element();
  C.card.mount(progress, node);
  for (const s of [t.root, t.dp, t.ex, t.tool]) progress.spans.set(s.span_id, s);
  C.card.update(progress, "snapshot");
  await progress.court.chain;
  const fetches = () => calls.filter(u => u.startsWith("/v1/assistant/dispatches/")).length;
  const before = fetches();
  const details = () => find(node, n => n.tag === "details");
  details()[0].open = true; details()[0].listeners.toggle();
  progress.spans.set(t.tool.span_id, { ...t.tool, state: "completed", ended_at: T(6) });
  progress.spans.set("tl.a2", span("tl.a2", "tool", "bank.list_payments", "ex.a", "running", 7));
  C.card.update(progress, "spans");
  await progress.court.chain;
  assert.equal(fetches(), before, "只有工具在变时不重取派工细节");
  assert.equal(details()[0].open, true, "本人展开过的细节在下一帧仍展开");
  progress.spans.set(t.ex.span_id, { ...t.ex, state: "waiting_input" });
  C.card.update(progress, "spans");
  await progress.court.chain;
  assert.equal(fetches(), before + 1, "匠人状态变了才重取");
}

// R7：常驻小院——打开就在（可用匠人待命），新请求立刻上台演；进展面板默认折叠在小院内，对话区只留紧凑入口。
async function stagedCourtyardFollowsFocus(dir) {
  const t = soloTimeline();
  const { C, scene } = cardHarness(dir, {
    "/v1/assistant/artisans": () => ({ items: [{ package: "finance" }, { package: "support-desk" }] }),
    "/v1/assistant/dispatches/dsp_1": () => ({ host: "session", instruction: "查", items: [] }),
  }, true, ["courtyard-art.js", "courtyard-model.js", "courtyard-card.js", "courtyard-stage.js"]);
  const stageBox = element();
  assert.ok(C.stage.mount(stageBox, {}));
  C.stage.immediate(true);
  await new Promise(resolve => setImmediate(resolve));
  same(scene.filter(x => x[0] === "cast").slice(-1)[0], ["cast", ["finance", "support-desk"]], "打开就让可用匠人上垫子待命");
  const progress = { spans: new Map(), header: {}, wantsFocus: true }, node = element();
  C.card.mount(progress, node);
  assert.equal(find(node, n => n.textContent === "在小院里看").length, 1, "对话区只保留紧凑的「在小院里看」入口");
  assert.equal(find(node, n => n.className === "cy-feed").length, 0, "经过列表不再塞进对话区");
  for (const s of [t.root, t.dp]) progress.spans.set(s.span_id, s);
  C.card.update(progress, "snapshot");
  await progress.court.chain;
  assert.ok(C.stage.isFocus(progress.court), "刚发出的请求成为小院焦点");
  const drawer = find(stageBox, n => String(n.className).includes("cy-stage-drawer"))[0];
  const toggle = find(stageBox, n => String(n.className).includes("cy-stage-toggle"))[0];
  assert.ok(drawer && drawer.classList.contains("hidden"), "小院里的进展面板默认折叠");
  assert.equal(C.stage.isExpanded(), false);
  assert.equal(find(drawer, n => n.className === "cy-feed").length, 1, "经过面板挂在小院抽屉里");
  toggle.listeners.click();
  assert.equal(C.stage.isExpanded(), true, "点击展开按钮在小院内展开进展");
  assert.ok(!drawer.classList.contains("hidden"));
  toggle.listeners.click();
  assert.equal(C.stage.isExpanded(), false, "再次点击收起");
  const settlesBefore = scene.filter(x => x[0] === "settle").length;
  C.stage.focus(progress.court);
  assert.equal(scene.filter(x => x[0] === "settle").length, settlesBefore, "重复聚焦同一件事不重走 settle 打断动画");
  progress.spans.set(t.ex.span_id, t.ex);
  progress.spans.set(t.tool.span_id, t.tool);
  C.card.update(progress, "spans");
  await progress.court.chain;
  same(scene.filter(x => x[0] === "play").slice(-1)[0], ["play", ["wake", "work"]], "之后的帧在小院里演出");
  const other = { spans: new Map([[t.root.span_id, t.root]]), header: {} }, otherNode = element();
  C.card.mount(other, otherNode);
  const plays = scene.filter(x => x[0] === "play").length;
  C.card.update(other, "spans");
  await other.court.chain;
  assert.equal(scene.filter(x => x[0] === "play").length, plays, "不是焦点的卡片不演");
  find(otherNode, n => n.textContent === "在小院里看")[0].listeners.click();
  assert.ok(C.stage.isFocus(other.court) && C.stage.isExpanded(), "点「在小院里看」切焦点并在小院里展开");
  C.stage.focus(progress.court);
  // 真实试用发现：员工答复匠人提问那一句会开新的一轮，办完后小院应回到还在办的那件事
  const reply = { spans: new Map(), header: {}, wantsFocus: true };
  C.card.mount(reply, element());
  reply.spans.set("ex.r", span("ex.r", "butler", "管家", null, "running", 20));
  C.card.update(reply, "snapshot");
  await reply.court.chain;
  assert.ok(C.stage.isFocus(reply.court), "答复那一句先成为焦点");
  reply.spans.set("ex.r", { ...span("ex.r", "butler", "管家", null, "completed", 20), ended_at: T(22) });
  C.card.update(reply, "spans");
  await reply.court.chain;
  assert.ok(C.stage.isFocus(progress.court), "答复办完后回到还在办的那件事");
  // 实时步骤推送（如「处理完成，正在组织回复…」）会立刻刷新当前焦点小院里的管家气泡
  const liveStepTrace = { traceId: "trc_live", spans: new Map(), header: {}, wantsFocus: true };
  C.card.mount(liveStepTrace, element());
  liveStepTrace.spans.set("ex.l", span("ex.l", "butler", "管家", null, "running", 30));
  C.card.update(liveStepTrace, "snapshot");
  await liveStepTrace.court.chain;
  assert.equal(scene.filter(x => x[0] === "settle").slice(-1)[0][1].butler.bubble, "我想想怎么办…");
  C.stage.setStep("trc_live", "处理完成，正在组织回复…");
  await Promise.resolve();
  assert.equal(scene.filter(x => x[0] === "settle").slice(-1)[0][1].butler.bubble, "处理完成，正在组织回复…");
  // 已有在跑的匠人派工时，setStep 不应打断匠人动画
  liveStepTrace.spans.set("dp.live", span("dp.live", "dispatch", "finance/check", "ex.l", "running", 31, { ref: "dsp_1" }));
  C.card.update(liveStepTrace, "spans");
  await liveStepTrace.court.chain;
  await Promise.resolve();
  const settlesWithDispatch = scene.filter(x => x[0] === "settle").length;
  C.stage.setStep("trc_live", "正在回复…");
  await Promise.resolve();
  assert.equal(scene.filter(x => x[0] === "settle").length, settlesWithDispatch, "有运行中派工时不强行 settle 打断动画");
}

function unsupportedFallsBack(dir) {
  const { C } = cardHarness(dir, {}, false);
  assert.equal(C.card.supported(), false, "没有 SVG 时进展卡只显示执行树");
}

const dir = process.argv[2];
const C = load(dir, ["courtyard-art.js", "courtyard-model.js"]);
stations(C);
beatsInOrder(C);
storyUsesSayThenTemplate(C);
missingDetailsDegrade(C);
waitingForYou(C);
consultSitsAtTable(C);
finishedSnapshotPoses(C);
butlerRepliesBeforeArtisanFinishes(C);
resultDropsRepeatedNarration(C);
rulingKeepsWaitingAndStaysShort(C);
artisanStaysAtLastStationWhileThinking(C);
briefSaysTheTask(C);
butlerBubbleTracksRunningPhaseAndStep(C);
orphanSpansDoNotCrash(C);
looks(C);
function makeSceneDom() {
  const makeEl = tag => {
    const n = element(tag);
    n.style = { props: {}, setProperty(k, v) { this.props[k] = String(v); } };
    n.remove = () => { if (n.parent) n.parent.children = n.parent.children.filter(c => c !== n); };
    const origAppend = n.appendChild;
    n.appendChild = child => {
      if (child.parent) child.parent.children = child.parent.children.filter(c => c !== child);
      child.parent = n;
      return origAppend.call(n, child);
    };
    n.querySelector = () => null;
    n.querySelectorAll = () => [];
    return n;
  };
  const store = new Map();
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  const document = { createElement: makeEl, createElementNS: (_ns, tag) => makeEl(tag) };
  return { makeEl, document, localStorage, store };
}

async function multiRowExpansionAndThemes(dir) {
  // 三套不同尺度的空间布局：小院自动多排、四合院分居东西厢房、故宫分驻东西六宫与外路偏殿，且支持热切换不丢角色。
  const { makeEl, document, localStorage, store } = makeSceneDom();
  const C = load(dir, ["courtyard-art.js", "courtyard-scene.js"], {
    document, localStorage, requestAnimationFrame: () => 0, DOMException,
  });
  const host = makeEl("div");
  const scene = C.scene.create(host, {
    look: key => C.art.resolveLook(C.art.loadLooks(), key),
  });
  const svg = find(host, n => n.tag === "svg")[0];
  assert.equal(svg.getAttribute("viewBox"), "0 0 900 520", "默认小院 1 排高度 520");

  // 7 位匠人自动扩为 2 排（8 个角色 = 管家 + 7 匠人）
  scene.cast(["a1", "a2", "a3", "a4", "a5", "travel-planner", "driver-dispatch"]);
  scene.settle({ butler: { at: "home", face: "open" }, "travel-planner": { at: "home", pose: "working", face: "open" } });
  let actors = find(host, n => String(n.getAttribute("class") || "").includes("actor"));
  assert.equal(actors.length, 8, "管家 + 7 位匠人全部在场不挤掉");
  assert.equal(svg.getAttribute("viewBox"), "0 0 900 638", "小院 7 位匠人自动扩为 2 排");

  // 切换到四合院：画布切换为 1080 宽四合院，匠人分居东西厢房（不再挤在同一排）
  const backLayer = svg.children[0];
  assert.equal(scene.setTheme("siheyuan"), "siheyuan");
  assert.equal(host.dataset.theme, "siheyuan");
  assert.ok(svg.getAttribute("aria-label").includes("玄圃四合院"));
  assert.equal(svg.getAttribute("viewBox"), "0 0 1080 856", "四合院 7 位匠人自动建 4 对东西厢房");
  assert.ok(backLayer.innerHTML.includes("西厢1院") && backLayer.innerHTML.includes("东厢1院") && backLayer.innerHTML.includes("垂花门"));
  assert.equal(store.get("xuanpu-courtyard-theme"), "siheyuan");
  const parseX = a => Number(String(a.getAttribute("transform")).match(/translate\(([^,]+)/)[1]);
  const siheyuanArtisanXs = find(host, n => String(n.getAttribute("class") || "").includes("actor") && n.getAttribute("aria-label") !== "管家").map(parseX);
  assert.ok(siheyuanArtisanXs.some(x => x < 200) && siheyuanArtisanXs.some(x => x > 850), "四合院匠人分散在西厢房与东厢房");

  // 切换到故宫：画布切换为 1320 宽紫禁城，匠人分散在东西六宫独立宫院
  assert.equal(scene.setTheme("palace"), "palace");
  assert.equal(host.dataset.theme, "palace");
  assert.ok(svg.getAttribute("aria-label").includes("玄圃紫禁城"));
  assert.equal(svg.getAttribute("viewBox"), "0 0 1320 880", "故宫默认 12 宫（东西六宫）容纳 7 位匠人");
  assert.ok(backLayer.innerHTML.includes("景仁宫") && backLayer.innerHTML.includes("储秀宫") && backLayer.innerHTML.includes("太和殿") && backLayer.innerHTML.includes("午 门"));
  const palaceArtisanXs = find(host, n => String(n.getAttribute("class") || "").includes("actor") && n.getAttribute("aria-label") !== "管家").map(parseX);
  assert.ok(palaceArtisanXs.some(x => x < 350) && palaceArtisanXs.some(x => x > 950), "故宫匠人分散在西六宫与东六宫各处");

  // 继续增加到 14 位匠人：故宫自动向外路增建偏殿
  scene.cast(["a8", "a9", "a10", "a11", "a12", "a13", "a14"]);
  actors = find(host, n => String(n.getAttribute("class") || "").includes("actor"));
  assert.equal(actors.length, 15, "管家 + 14 位匠人全部在场");
  assert.equal(svg.getAttribute("viewBox"), "0 0 1320 1014", "超过 12 位匠人时故宫自动增建外路偏殿");
  for (const actor of actors) {
    const tf = String(actor.getAttribute("transform") || "");
    assert.ok(tf.startsWith("translate(") && !tf.includes("NaN") && !tf.includes("undefined"), "角色坐标不得为 NaN：" + tf);
  }
  scene.destroy();
}

async function stageThemeSwitcherControlsRealScene(dir) {
  const { makeEl, document, localStorage } = makeSceneDom();
  const el = (tag, text, cls) => { const n = makeEl(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (label, onClick, cls) => { const b = el("button", label, cls); b.addEventListener("click", onClick); return b; };
  const C = load(dir, ["courtyard-art.js", "courtyard-model.js", "courtyard-scene.js", "courtyard-stage.js"], {
    document, localStorage, el, button, requestAnimationFrame: () => 0, DOMException,
    entryApi: async () => ({ items: [{ package: "a1" }, { package: "a2" }, { package: "a3" }, { package: "a4" }, { package: "a5" }, { package: "a6" }] }),
    entry: { cleanups: new Set(), epoch: 0 }, isStale: () => false,
  });
  const stageBox = makeEl("div");
  assert.ok(C.stage.mount(stageBox, {}));
  await new Promise(resolve => setImmediate(resolve));
  const btns = find(stageBox, n => String(n.className).includes("cy-theme-btn"));
  same(btns.map(b => b.textContent), ["小院", "四合院", "故宫"]);
  assert.equal(btns[0].getAttribute("aria-pressed"), "true");
  btns[2].listeners.click();
  assert.equal(btns[0].getAttribute("aria-pressed"), "false");
  assert.equal(btns[2].getAttribute("aria-pressed"), "true");
  const svg = find(stageBox, n => n.tag === "svg")[0];
  assert.ok(svg.getAttribute("aria-label").includes("玄圃紫禁城"), "点击故宫按钮立即切换为紫禁城建筑群");
  assert.equal(svg.getAttribute("viewBox"), "0 0 1320 880", "故宫展示完整紫禁城尺度");
  // 管家回话卡片持久留在小院视图中，不会随动画结束消失
  C.stage.setReply("trc_demo", "已为你查好路线与天气，建议 13:45 出发。", { streaming: false });
  const replyCard = find(stageBox, n => String(n.className).includes("cy-reply-card"))[0];
  assert.ok(replyCard && !replyCard.classList.contains("hidden"), "管家回话卡应持久展示在视图上");
  assert.ok(replyCard.textContent.includes("建议 13:45 出发"), "视图上应看到管家最后说的内容");
}

async function toolPickupAndLiveBoard(dir) {
  // 1. 管家与匠人工具均映射到实体工位与可拿起道具
  const C = load(dir, ["courtyard-art.js", "courtyard-model.js"]);
  same(C.model.toolTarget("read_personal", { owner: "butler", hint: "查看今日待办" }), { station: "tv", prop: "sticky", itemKey: "sticky" });
  same(C.model.toolTarget("read_personal", { owner: "butler", hint: "查询通讯录" }), { station: "books", prop: "book", itemKey: "contacts" });
  same(C.model.toolTarget("amap.route_plan"), { station: "computer", prop: "compass", itemKey: "compass" });
  same(C.model.toolTarget("run_script"), { station: "bench", prop: "wrench", itemKey: "wrench" });
  same(C.model.toolTarget("query_reimbursements"), { station: "cabinet", prop: "stamp", itemKey: "stamp" });

  // 2. diff 节拍携带 station/prop/itemKey，驱动角色走去拿起道具
  const root = span("ex.b", "butler", "管家", null, "running", 0);
  const readTodo = span("tl.b1", "tool", "read_personal", "ex.b", "running", 1, { say: "看看今日待办" });
  const beats = C.model.diff([root], [root, readTodo], none);
  const bt = beats.find(b => b.type === "butler_tool");
  assert.equal(bt.station, "tv");
  assert.equal(bt.prop, "sticky");
  assert.equal(bt.itemKey, "sticky");

  // 3. 真实待办直接画在 SVG 看板上，且切换四合院时保留
  const { makeEl, document, localStorage } = makeSceneDom();
  const S = load(dir, ["courtyard-art.js", "courtyard-scene.js"], {
    document, localStorage, requestAnimationFrame: () => 0, DOMException,
  });
  const host = makeEl("div");
  const scene = S.scene.create(host, { look: key => S.art.resolveLook(S.art.loadLooks(), key) });
  scene.setWorld({
    todos: [{ id: "t1", title: "把壁仞方案发给何总", done: false }, { id: "t2", title: "已完成项", done: true }],
    reminders: [{ id: "r1", title: "回袁新电话" }],
    events: [],
    booksCount: 8,
    artisans: [],
  });
  const svg = find(host, n => n.tag === "svg")[0];
  const board = find(svg, n => n.getAttribute("class") === "board-live")[0];
  assert.ok(board && board.textContent.includes("把壁仞方案发给何"), "看板应显示真实未完成待办");
  assert.ok(!board.textContent.includes("已完成项"), "已完成待办不占看板待办行");
  assert.ok(board.textContent.includes("2"), "看板角标应显示 2 项未完成事项");
  scene.setTheme("siheyuan");
  const boardAfter = find(svg, n => n.getAttribute("class") === "board-live")[0];
  assert.ok(boardAfter && boardAfter.textContent.includes("把壁仞方案发给何"), "切换四合院后看板仍显示真实待办");
  scene.destroy();

  // 4. 0 匠人时准确提示未启用企业匠人，管理员看到能力市场入口
  const { makeEl: mk2, document: doc2, localStorage: ls2 } = makeSceneDom();
  const el = (tag, text, cls) => { const n = mk2(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (label, onClick, cls) => { const b = el("button", label, cls); b.addEventListener("click", onClick); return b; };
  const ST = load(dir, ["courtyard-art.js", "courtyard-model.js", "courtyard-scene.js", "courtyard-stage.js"], {
    document: doc2, localStorage: ls2, el, button, requestAnimationFrame: () => 0, DOMException,
    entryApi: async () => ({ items: [] }),
    entry: { cleanups: new Set(), epoch: 0, me: { role: "tenant_admin" } }, isStale: () => false,
  });
  const box = mk2("div");
  ST.stage.mount(box, {});
  await new Promise(resolve => setImmediate(resolve));
  const cap = find(box, n => String(n.className).includes("cy-caption"))[0];
  assert.ok(cap.textContent.includes("暂未启用企业匠人"), "0 匠人时不应误报匠人在垫子上歇着");
  assert.ok(cap.textContent.includes("去能力市场添匠人"), "管理员应看到去能力市场添匠人快捷入口");
}

(async () => {
  await finishedSnapshotSettles(dir);
  await missingDetailStillRenders(dir);
  await openItemOffersAction(dir);
  await toolStepsDoNotRefetchAndDetailsStayOpen(dir);
  await stagedCourtyardFollowsFocus(dir);
  await multiRowExpansionAndThemes(dir);
  await stageThemeSwitcherControlsRealScene(dir);
  await toolPickupAndLiveBoard(dir);
  unsupportedFallsBack(dir);
  console.log("courtyard model passed");
})().catch(error => { console.error(error); process.exit(1); });
