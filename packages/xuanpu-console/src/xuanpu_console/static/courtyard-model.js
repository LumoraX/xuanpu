// 小院纯模型（R7）：执行记录（spans）+ 派工与编组细节 → 角色位置、动画节拍、「经过」条目。
// 没有 DOM、没有请求，可以在 Node 里直接测。画面上的每个动作都来自一条已有事实。
"use strict";

(function () {
  const TERMINAL = new Set(["completed", "failed", "cancelled", "unknown"]);
  const WAIT_TOOLS = new Set(["call_skill", "await_skills", "join_skill", "wait_peer", "wait_any"]);
  const PLACE = { cabinet: "档案柜", books: "书架", computer: "电脑", tv: "看板", bed: "小床", bench: "工作台" };
  // 管家自己的工具 → 它用自己的话怎么说；派工与编组由派工 span 表达，这里不重复
  const BUTLER_SAYS = {
    list_artisans: "我翻了名册，看看谁能办。", answer_artisan: "匠人问的事你说过，我照你的原话答了。",
    rate_divergence: "我比对了几位匠人的结论。", propose: "我拟了一份草稿，等你确认。",
    read_personal: "我看了看你手上的事。", check_dispatch: "我去问了问进展。",
    cancel_dispatch: "我请匠人先停下。", cancel_group: "我请大家先停下。", ask_user: "匠人的问题我转给你了。",
    extend_turn: "这事要多花点时间，我把时限放宽了。", raise_budget: "这事比较大，我提高了这次的预算。",
  };
  const BUTLER_PROPS = { list_artisans: "book", answer_artisan: "phone", rate_divergence: "magnifier", propose: "quill" };
  const REASONS = {
    start_failed: "没能开工。", check_terminal: "还没办完就停下了。", check_succeeded: "这次没办成。",
    check_outputs: "办完了，但没交出成果。", check_no_pending: "还有没确认的事。", check_no_unknown: "有一步的结果不明。",
    tool_error: "工具出错了。", cancelled: "被叫停了。", member_unavailable: "这位匠人现在不能用。",
    outcome_unknown: "有一步的结果不明。",
  };

  const ms = iso => (iso ? Date.parse(iso) : NaN);
  const byStart = (a, b) => (ms(a.started_at) - ms(b.started_at)) || (a.span_id < b.span_id ? -1 : 1);

  // 工具名 → 家具。顺序有讲究：等待优先，其次书架、工作台、看板，带「.」或「__」的是连接器（电脑）。
  function stationFor(name) {
    const n = String(name || "").toLowerCase();
    if (WAIT_TOOLS.has(n) || n.startsWith("wait")) return "bed";
    if (n === "read_skill" || n.startsWith("read_") || n.includes("doc") || n.includes("knowledge")) return "books";
    if (n === "run_script" || n.startsWith("exec") || n.includes("script")) return "bench";
    if (/report|dashboard|trend|chart|stat/.test(n)) return "tv";
    if (n.includes(".") || n.includes("__")) return "computer";
    return "cabinet";
  }

  function reasonText(code) {
    if (!code) return "这次没办成。";
    return REASONS[code] || "这次没办成（" + code + "）。";
  }

  function index(spans) {
    const map = new Map();
    for (const span of spans) map.set(span.span_id, span);
    return map;
  }

  const pkgOf = dispatch => String(dispatch.name || "").split("/")[0] || "匠人";

  // 往上找：遇到派工 span 归那位匠人；遇到管家根或编组归管家。缺父或成环时为 null。
  function ownerOf(map, span) {
    const seen = new Set();
    for (let cur = span; cur && !seen.has(cur.span_id); cur = map.get(cur.parent_span_id)) {
      seen.add(cur.span_id);
      if (cur.kind === "dispatch") return pkgOf(cur);
      if (cur.kind === "butler" || cur.kind === "group") return "butler";
    }
    return null;
  }

  function dispatchAbove(map, span) {
    const seen = new Set();
    for (let cur = span; cur && !seen.has(cur.span_id); cur = map.get(cur.parent_span_id)) {
      seen.add(cur.span_id);
      if (cur.kind === "dispatch") return cur;
    }
    return null;
  }

  // 交代方式：会诊 → 圆桌当面；任务承载 → 写信；其余（会话承载、细节读不到）→ 走过去当面说。
  function howFor(map, dispatch, details) {
    const parent = map.get(dispatch.parent_span_id);
    if (parent && parent.kind === "group") {
      const group = details.group(parent.ref);
      const member = group && (group.members || []).find(m => m.dispatch_id === dispatch.ref);
      const step = member && (group.steps || [])[member.step];
      if (step && step.mode === "consult") return "table";
    }
    const detail = details.dispatch(dispatch.ref);
    return detail && detail.host === "task" ? "letter" : "face";
  }

  // 这条 trace 里出场的匠人，按第一次派工的先后。
  function artisans(spans) {
    const order = [];
    for (const span of spans.filter(s => s.kind === "dispatch").sort(byStart)) {
      const pkg = pkgOf(span);
      if (!order.includes(pkg)) order.push(pkg);
    }
    return order;
  }

  // 管家的根：同一条 trace 里可能有多轮（第一轮交代完就回话，之后被唤醒复核），取最早的一轮。
  function roots(spans) {
    return spans.filter(s => s.kind === "butler" && !s.parent_span_id).sort(byStart);
  }
  function rootOf(spans) {
    return roots(spans)[0] || spans.find(s => s.kind === "butler") || null;
  }
  // 这次请求办完了：管家各轮与全部派工都到了终态。管家第一轮回话时匠人往往还在干活。
  function finished(spans) {
    const all = roots(spans), dispatches = spans.filter(s => s.kind === "dispatch");
    return all.length > 0 && all.every(s => TERMINAL.has(s.state)) && dispatches.every(s => TERMINAL.has(s.state));
  }
  const busy = spans => spans.some(s => s.kind === "dispatch" && !TERMINAL.has(s.state));
  // 这条 trace 里还在等本人处理的项（提问、审批、裁决）。span 都结束了也可能还在等本人裁决。
  function openItems(spans, details) {
    return spans.filter(s => s.kind === "dispatch").flatMap(s => ((details.dispatch(s.ref) || {}).items || []))
      .filter(i => ["question", "approval", "ruling"].includes(i.kind) && (i.state === "open" || i.state === "forwarding"));
  }

  const openItem = (detail, kinds) => (detail && detail.items || []).find(i => kinds.includes(i.kind) && (i.state === "open" || i.state === "forwarding"));

  // 角色此刻的样子：在哪儿、什么姿势、拿什么、头顶说什么。快照（刷新、已结束的 trace）直接画这个。
  function poses(spans, details) {
    const map = index(spans), result = {}, root = rootOf(spans);
    const butler = { at: "home", pose: null, face: "open", prop: null, bubble: null, red: false };
    const ruling = openItems(spans, details).some(i => i.kind === "ruling");
    if (root) {
      if (root.state === "waiting_input" || ruling) Object.assign(butler, { at: "table", bubble: "等你裁决", red: true, prop: null });
      else if (root.state === "waiting_children" || busy(spans)) Object.assign(butler, { prop: "tea", face: "happy" });
      // 管家还在想怎么办（还没派出任何人）
      else if (root.state === "running" && !spans.some(s => s.kind === "dispatch")) Object.assign(butler, { bubble: "我想想怎么办…" });
      else if (root.state === "completed") butler.face = "happy";
      else if (root.state === "failed") butler.face = "dizzy";
    }
    result.butler = butler;
    for (const pkg of artisans(spans)) {
      const dispatch = spans.filter(s => s.kind === "dispatch" && pkgOf(s) === pkg).sort(byStart).pop();
      result[pkg] = artisanPose(map, spans, dispatch, details);
    }
    return result;
  }

  function artisanPose(map, spans, dispatch, details) {
    const pose = { at: "home", station: null, pose: "sleeping", face: "closed", prop: null, bubble: null, red: false };
    if (TERMINAL.has(dispatch.state) || dispatch.state === null) {
      if (dispatch.state === "failed") Object.assign(pose, { pose: null, face: "dizzy" });
      return pose;
    }
    const detail = details.dispatch(dispatch.ref);
    const mine = spans.filter(s => dispatchAbove(map, s) === dispatch);
    const steps = mine.filter(s => s.kind === "tool" || s.kind === "wait").sort(byStart);
    const tool = steps.filter(s => !TERMINAL.has(s.state)).pop();
    // 真实工具往往一眨眼就完，匠人大部分时间在两步之间想：执行还没完就留在上一步的家具旁
    const running = mine.some(s => s.kind === "artisan" && s.state === "running");
    const lastStep = running ? steps[steps.length - 1] : null;
    const waiting = mine.some(s => s.kind === "artisan" && s.state === "waiting_input") || dispatch.state === "waiting_input";
    if (waiting) {
      const approval = openItem(detail, ["approval"]);
      return Object.assign(pose, { pose: "asking", face: "open", prop: approval ? "paper" : "phone",
        bubble: approval ? "等你盖章" : ((openItem(detail, ["question"]) || {}).question || "有个问题"), red: Boolean(approval) });
    }
    if (tool) {
      const station = tool.kind === "wait" ? "bed" : stationFor(tool.name);
      return Object.assign(pose, { at: "station", station, pose: station === "bed" ? "sleeping" : "working",
        face: station === "bed" ? "closed" : "open", bubble: tool.say || PLACE[station] });
    }
    if (lastStep) {
      const station = lastStep.kind === "wait" ? "bed" : stationFor(lastStep.name);
      return Object.assign(pose, { at: "station", station, pose: "working", face: "open", bubble: "想想下一步…" });
    }
    // 已接下活、还没动手：醒着，告诉大家开始办了
    const how = howFor(map, dispatch, details);
    return Object.assign(pose, { at: how === "table" ? "table" : "home", pose: null, face: "open", bubble: "收到，开始办…" });
  }

  // 交代时管家头顶说的一句：任务说明的第一句，去掉「处理员工最新请求：」这类开场。
  function briefLine(instruction) {
    if (!instruction) return "这件事交给你了";
    const body = String(instruction).replace(/^[^：:]{0,12}[：:]/, "").trim();
    const first = body.split(/[。！？!?；;\n]/)[0].trim() || body;
    return first.length > 18 ? first.slice(0, 17) + "…" : first;
  }

  // 两次快照之间发生了什么 → 一串动画节拍。同一快照比两次不产生节拍。
  function diff(prevSpans, nextSpans, details) {
    const prev = index(prevSpans), map = index(nextSpans), beats = [];
    for (const span of nextSpans.slice().sort(byStart)) {
      const before = prev.get(span.span_id), fresh = !before, changed = fresh || before.state !== span.state;
      if (!changed) continue;
      const who = ownerOf(map, span);
      if (span.kind === "dispatch") {
        const pkg = pkgOf(span);
        if (fresh) beats.push({ type: "brief", actor: pkg, how: howFor(map, span, details), dispatch: span.ref, span: span.span_id,
          text: briefLine((details.dispatch(span.ref) || {}).instruction) });
        if (TERMINAL.has(span.state) && (fresh || !TERMINAL.has(before.state))) {
          const detail = details.dispatch(span.ref), ok = span.state === "completed";
          beats.push({ type: "return", actor: pkg, ok, how: howFor(map, span, details), dispatch: span.ref });
          beats.push({ type: "check", actor: "butler", ok, checks: detail ? detail.checks : null, of: pkg, dispatch: span.ref });
        }
      } else if (span.kind === "artisan" && who && who !== "butler") {
        if (fresh && map.get(span.parent_span_id) && map.get(span.parent_span_id).kind === "dispatch") beats.push({ type: "wake", actor: who, span: span.span_id });
        if (span.state === "waiting_input") {
          const detail = details.dispatch((dispatchAbove(map, span) || {}).ref);
          const approval = openItem(detail, ["approval"]), question = openItem(detail, ["question"]);
          beats.push(approval ? { type: "seal", actor: who, text: approval.question }
            : { type: "ask", actor: who, text: question ? question.question : "有个问题" });
        }
      } else if ((span.kind === "tool" || span.kind === "wait") && who) {
        if (who === "butler") {
          if (fresh && BUTLER_SAYS[span.name]) beats.push({ type: "butler_tool", actor: "butler", tool: span.name, prop: BUTLER_PROPS[span.name] || null, text: BUTLER_SAYS[span.name] });
        } else if (fresh) {
          const station = span.kind === "wait" ? "bed" : stationFor(span.name);
          beats.push({ type: "work", actor: who, station, label: span.say || PLACE[station], span: span.span_id });
          if (TERMINAL.has(span.state)) beats.push({ type: "done_tool", actor: who, ok: span.state === "completed", span: span.span_id });
        } else if (TERMINAL.has(span.state)) {
          beats.push({ type: "done_tool", actor: who, ok: span.state === "completed", span: span.span_id });
        }
      } else if (span.kind === "butler" && !span.parent_span_id) {
        if (fresh && span !== rootOf(nextSpans)) beats.push({ type: "butler_tool", actor: "butler", tool: "review", prop: "magnifier", text: "复核结果" });
        else if (fresh && span.state === "running") beats.push({ type: "butler_tool", actor: "butler", tool: "think", prop: null, text: "我想想怎么办…" });
        if (span.state === "waiting_children") beats.push({ type: "wait", actor: "butler" });
        else if (span.state === "waiting_input") beats.push({ type: "ruling", actor: "butler" });
        else if (span.state === "completed") beats.push({ type: "reply", actor: "butler" });
      }
    }
    return beats;
  }

  // 「经过」：像群聊一样，每个角色用自己的话讲做了什么。按时间排序。
  function story(spans, details) {
    const map = index(spans), items = [];
    for (const span of spans) {
      const who = ownerOf(map, span), at = ms(span.started_at);
      if (span.kind === "dispatch") {
        const pkg = pkgOf(span), detail = details.dispatch(span.ref);
        if (detail && detail.instruction) items.push({ at, who: "butler", to: pkg, type: "letter", how: howFor(map, span, details), text: detail.instruction });
        for (const item of (detail && detail.items) || []) items.push(fromItem(item, pkg));
        if (TERMINAL.has(span.state)) {
          const end = ms(span.ended_at) || at;
          if (detail && detail.result) items.push({ at: end, who: pkg, type: "result", text: resultText(detail.result, spans.filter(s => s.say && dispatchAbove(map, s) === span).map(s => s.say)) });
          else if (span.state !== "completed") items.push({ at: end, who: pkg, type: "fail", text: reasonText(span.end_reason || (detail && detail.end_reason)) });
          items.push({ at: end + 1, who: "butler", to: pkg, type: "check", ok: span.state === "completed", checks: detail ? detail.checks : null });
        }
      } else if ((span.kind === "tool" || span.kind === "wait") && who === "butler") {
        if (BUTLER_SAYS[span.name]) items.push({ at, who: "butler", type: "say", text: BUTLER_SAYS[span.name], tool: span.name });
      } else if ((span.kind === "tool" || span.kind === "wait") && who) {
        const station = span.kind === "wait" ? "bed" : stationFor(span.name), done = TERMINAL.has(span.state);
        items.push({
          at, who, type: "step", place: station, tool: span.name, said: Boolean(span.say),
          text: span.say || (station === "bed" ? "我在等「" + span.name + "」的结果。" : "我在" + PLACE[station] + "用了「" + span.name + "」。"),
          running: !done, failed: span.state === "failed", ms: done ? ms(span.ended_at) - at : null,
        });
      } else if (span.kind === "butler" && !span.parent_span_id && span !== rootOf(spans)) {
        items.push({ at, who: "butler", type: "say", text: "匠人交回了结果，我复核了一遍。" });
      }
    }
    if (finished(spans) && !openItems(spans, details).length) {
      const end = Math.max(...spans.map(s => ms(s.ended_at)).filter(n => !Number.isNaN(n)));
      items.push({ at: (Number.isFinite(end) ? end : 0) + 2, who: "butler", type: "done", text: "这一趟办完了，结果在上面的对话里。" });
    }
    return items.filter(i => i).sort((a, b) => (a.at || 0) - (b.at || 0));
  }

  // 结果摘录：去掉前面已经作为步骤讲过的旁白，再去掉 Markdown 的加粗与标题记号。
  function resultText(text, said) {
    let out = String(text);
    for (const line of said) out = out.split(line).join("");
    out = out.replace(/\*\*|__/g, "").replace(/^#{1,6}\s*/gm, "").replace(/\n{3,}/g, "\n\n").trim();
    return out || String(text);
  }

  function fromItem(item, pkg) {
    const at = ms(item.created_at);
    if (item.kind === "proxied") return { at, who: "butler", to: pkg, type: "answer", text: item.answer || "", quote: item.source_quote };
    if (item.kind === "approval") return { at, who: pkg, type: "seal", text: item.question, open: item.state === "open", pending: item.pending_id };
    if (item.kind === "ruling") {
      const open = item.state === "open" || item.state === "forwarding";
      return { at, who: "butler", type: "ruling", text: open ? "几位匠人的结论分歧大，这事我不能替你定，请你裁决。" : "你已经裁决过了。",
        more: resultText(item.question, []), moreLabel: "看各位的结论", answer: item.answer, open, pending: open ? item.pending_id : null };
    }
    const open = item.state === "open" || item.state === "forwarding";
    return { at, who: pkg, type: "ask", text: item.question, answer: item.answer, open, pending: open ? item.pending_id : null };
  }

  // 顶部一句：状态、请了几位匠人、动了几次工具、用了多久。
  function summary(spans, now, details) {
    const root = rootOf(spans), map = index(spans);
    const tools = spans.filter(s => s.kind === "tool" && ownerOf(map, s) && ownerOf(map, s) !== "butler").length;
    const waitYou = spans.some(s => s.state === "waiting_input") || (details ? openItems(spans, details).length > 0 : false);
    const start = Math.min(...spans.map(s => ms(s.started_at)).filter(n => !Number.isNaN(n)));
    const done = finished(spans);
    const ended = done ? Math.max(...spans.map(s => ms(s.ended_at)).filter(n => !Number.isNaN(n))) : now;
    const state = !root ? "还没开始" : waitYou ? "停在这儿，等你" : !done ? "正在办"
      : root.state === "failed" ? "没办成" : "办完了";
    return { state, artisans: artisans(spans).length, tools, seconds: Number.isFinite(start) && Number.isFinite(ended) ? Math.max(0, Math.round((ended - start) / 1000)) : null };
  }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.model = { PLACE, TERMINAL, finished, openItems, briefLine, resultText, stationFor, reasonText, ownerOf, artisans, poses, diff, story, summary, howFor, index };
})();
