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
  const first = C.model.diff([], step1, details).map(b => b.type);
  same(first, ["butler_tool", "brief", "wake", "work"]);
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

const dir = process.argv[2];
const C = load(dir, ["courtyard-art.js", "courtyard-model.js"]);
stations(C);
beatsInOrder(C);
storyUsesSayThenTemplate(C);
missingDetailsDegrade(C);
waitingForYou(C);
consultSitsAtTable(C);
finishedSnapshotPoses(C);
orphanSpansDoNotCrash(C);
looks(C);
console.log("courtyard model passed");
