// 进展卡里的小院（R7）：订阅到的执行记录 → 取派工与编组细节 → 场景动画 + 「经过」 + 执行树页签。
// 所有文字走 textContent；SVG 只放常量画法。浏览器不支持 SVG 时进展卡照旧只显示执行树。
"use strict";

(function () {
  const C = () => globalThis.Courtyard;
  const NS = "http://www.w3.org/2000/svg";
  const live = new Set();
  const PLACE_ICON = {
    cabinet: "M5 2h10v16H5zM5 7.5h10M5 12.5h10", books: "M3 3h4v14H3zM8 5h4v12H8zM14 6l3 10.5",
    computer: "M3 3h14v10H3zM7 17h6M10 13v4", tv: "M2 3h16v11H2zM6 11V8M10 11V6M14 11V9",
    bed: "M2 16V6M2 13h16v3M18 13v-2a3 3 0 0 0-3-3H8v5", bench: "M2 9h16M4 9v8M16 9v8M4 13h12",
  };

  function supported() {
    const c = C();
    return typeof document.createElementNS === "function" && Boolean(c && c.scene && c.model && c.art);
  }

  function reducedMotion() {
    try { return Boolean(globalThis.matchMedia && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch { return false; }
  }

  function svgIcon(markup, size, className) {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", markup.viewBox);
    svg.setAttribute("width", size); svg.setAttribute("height", size);
    svg.setAttribute("aria-hidden", "true");
    if (className) svg.setAttribute("class", className);
    svg.innerHTML = markup.body;
    return svg;
  }

  // 头像：常量画法拼出的小角色。
  function avatar(state, key, size, face) {
    const look = C().art.resolveLook(state.looks, key);
    return svgIcon({ viewBox: "-40 -86 80 96", body: `<g class="actor" data-face="${face || "open"}">${C().art.charMarkup(look)}</g>` }, size, "cy-av");
  }

  function placeChip(key) {
    const chip = el("span", null, "cy-place");
    chip.append(svgIcon({ viewBox: "0 0 20 20", body: `<path d="${PLACE_ICON[key]}"/>` }, 13), el("span", C().model.PLACE[key]));
    return chip;
  }

  // 挂到一张进展卡上；返回执行树页签的容器，进展卡把原来的执行树与瀑布放进去。
  function mount(progress, node) {
    const state = { progress, prev: [], dispatch: new Map(), group: new Map(), chain: Promise.resolve(),
      sel: "all", tab: "story", looks: C().art.loadLooks(), editing: "butler", settled: false };
    const wrap = el("div", null, "courtyard"), stage = el("div", null, "cy-stage");
    const tabs = el("div", null, "cy-tabs"), story = el("div", null, "cy-panel"), tree = el("div", null, "cy-panel hidden");
    const looks = el("div", null, "cy-panel cy-looks hidden");
    state.nodes = { stage, story, tree, looks, who: el("div", null, "cy-who"), head: el("div", null, "cy-head"), feed: el("div", null, "cy-feed") };
    story.append(state.nodes.who, state.nodes.head, state.nodes.feed);
    const tab = (label, key) => { const b = button(label, () => showTab(state, key)); b.dataset.tab = key; return b; };
    tabs.append(tab("经过", "story"), tab("执行树", "tree"), tab("换角色", "looks"));
    state.nodes.tabs = tabs;
    wrap.append(stage, tabs, story, tree, looks);
    node.appendChild(wrap);
    state.scene = C().scene.create(stage, {
      look: key => C().art.resolveLook(state.looks, key),
      onSelect: key => select(state, key),
    });
    progress.court = state;
    live.add(state);
    entry.cleanups.add(() => { live.delete(state); state.scene.destroy(); });
    showTab(state, "story");
    return tree;
  }

  function showTab(state, key) {
    state.tab = key;
    for (const [name, panel] of [["story", state.nodes.story], ["tree", state.nodes.tree], ["looks", state.nodes.looks]]) panel.classList.toggle("hidden", name !== key);
    for (const b of state.nodes.tabs.children) b.setAttribute("aria-pressed", String(b.dataset.tab === key));
    if (key === "looks") renderLooks(state);
  }

  function select(state, key) {
    state.sel = key === state.sel && key !== "all" ? "all" : key;
    showTab(state, "story");
    state.scene.select(state.sel);
    renderStory(state);
  }

  // 每来一帧排一次：先补齐细节，再出动画，最后重画经过。帧之间不并发。
  function update(progress, frame) {
    const state = progress.court;
    if (!state) return;
    state.chain = state.chain.then(() => step(state, frame)).catch(error => { if (error && error.name !== "AbortError") console.error(error); });
  }

  async function step(state, frame) {
    const next = [...state.progress.spans.values()].map(s => ({ ...s }));
    await fetchDetails(state, next);
    const details = lookup(state), model = C().model;
    state.scene.cast(model.artisans(next));
    if (frame === "snapshot" || !state.settled || reducedMotion()) {
      state.scene.settle(model.poses(next, details));
      state.settled = true;
    } else {
      state.scene.play(model.diff(state.prev, next, details));
      // 动画是讲解，事实为准：排完后对齐到最新记录（期间又来了新帧则交给那一帧）
      state.scene.idle().then(() => { if (state.prev === next) state.scene.settle(model.poses(next, lookup(state))); });
    }
    state.prev = next;
    renderStory(state);
  }

  const lookup = state => ({ dispatch: id => state.dispatch.get(id), group: id => state.group.get(id) });

  // 新出现或状态变了的派工、编组才去取细节；读不到记为 null，之后状态再变才重试。
  async function fetchDetails(state, next) {
    const prev = C().model.index(state.prev), map = C().model.index(next), wanted = { dispatch: new Set(), group: new Set() };
    for (const span of next) {
      const before = prev.get(span.span_id);
      if (before && before.state === span.state) continue;
      for (let cur = span, hops = 0; cur && hops < 64; cur = map.get(cur.parent_span_id), hops++) {
        if (cur.kind === "dispatch" && cur.ref) { wanted.dispatch.add(cur.ref); break; }
        if (cur.kind === "group" && cur.ref) { wanted.group.add(cur.ref); break; }
      }
    }
    for (const span of next) if (span.kind === "dispatch" && span.ref && !state.dispatch.has(span.ref)) wanted.dispatch.add(span.ref);
    const get = async (kind, id) => {
      try { state[kind].set(id, await entryApi("GET", (kind === "dispatch" ? "/v1/assistant/dispatches/" : "/v1/assistant/groups/") + encodeURIComponent(id))); }
      catch (error) { if (error && error.name === "AbortError") throw error; state[kind].set(id, null); }
    };
    await Promise.all([...[...wanted.group].map(id => get("group", id)), ...[...wanted.dispatch].map(id => get("dispatch", id))]);
  }

  // ---------- 经过 ----------
  const nameOf = (state, key) => C().art.resolveLook(state.looks, key).name;

  function renderStory(state) {
    const { who, head, feed } = state.nodes, spans = state.prev, model = C().model, details = lookup(state);
    const keys = ["butler", ...model.artisans(spans)];
    const all = button("全部经过", () => select(state, "all"), "cy-chip");
    all.setAttribute("aria-pressed", String(state.sel === "all"));
    who.replaceChildren(all, ...keys.map(key => {
      const chip = button("", () => select(state, key), "cy-chip");
      chip.append(avatar(state, key, 26), el("span", nameOf(state, key)));
      chip.setAttribute("aria-pressed", String(state.sel === key));
      return chip;
    }));
    const sum = model.summary(spans, Date.now());
    const line = [sum.artisans ? "请了 " + sum.artisans + " 位匠人" : "", sum.tools ? "动了 " + sum.tools + " 次工具" : "",
      sum.seconds !== null ? "用了 " + sum.seconds + " 秒" : ""].filter(Boolean).join("，");
    const focus = state.sel === "all" ? "butler" : state.sel, pose = model.poses(spans, details)[focus] || {};
    const title = state.sel === "all" ? sum.state : nameOf(state, focus) + "：" + doing(pose);
    const sub = state.sel === "all" ? line : focus === "butler" ? "平台内置的管家，不属于任何业务包" : "来自业务包 " + focus;
    const text = el("div");
    text.append(el("strong", title), el("span", sub, "meta"));
    head.replaceChildren(avatar(state, focus, 44, pose.face || "open"), text);
    const items = model.story(spans, details).filter(i => state.sel === "all" || i.who === state.sel || i.to === state.sel);
    const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    feed.replaceChildren(...(items.length ? items.map(item => message(state, item)) : [el("p", "还没有经过。", "meta")]));
    if (stick) feed.scrollTop = feed.scrollHeight;
  }

  // 一个角色此刻在做什么，用它自己的口吻。
  function doing(pose) {
    if (pose.red) return "停下了，在等你";
    if (pose.pose === "asking") return "在等回复";
    if (pose.pose === "working") return "正在忙";
    if (pose.face === "dizzy") return "这次没办成";
    if (pose.at === "station" && pose.station === "bed") return "在等结果";
    if (pose.prop === "tea") return "在等匠人们";
    if (pose.face === "happy") return "办完了";
    return pose.pose === "sleeping" ? "歇着" : "在这儿";
  }

  function message(state, item) {
    const row = el("div", null, "cy-msg"), body = el("div", null, "cy-body"), nm = el("div", null, "cy-nm");
    const face = item.type === "fail" ? "dizzy" : (item.type === "result" || (item.type === "check" && item.ok)) ? "happy" : "open";
    const me = nameOf(state, item.who), them = item.to ? nameOf(state, item.to) : "";
    const verbs = { letter: { letter: "写信给", table: "在圆桌边请", face: "当面交代" }[item.how] || "交代",
      answer: "代你答复", ask: "打电话问管家", seal: "等你盖章", ruling: "等你裁决", result: "交回成果", fail: "没办成", check: "验收" };
    nm.append(el("b", me));
    if (verbs[item.type]) nm.append(el("span", " " + verbs[item.type] + " "));
    if (them && ["letter", "answer", "check"].includes(item.type)) nm.append(el("b", them));
    if (item.place) { nm.append(el("span", " 在 ")); nm.append(placeChip(item.place)); }
    if (item.ms !== null && item.ms !== undefined) nm.append(el("span", " " + (item.ms < 1500 ? "一下子" : Math.round(item.ms / 1000) + " 秒"), "meta"));
    const text = item.type === "check" ? (item.ok ? "五项都过了。" : "没通过，我记成失败，不会偷偷重跑。") : item.text;
    const bubble = el("div", text, "cy-bub " + item.type + (item.open ? " open" : "") + (item.failed ? " fail" : ""));
    if (item.running) bubble.append(el("span", " …", "cy-dots"));
    body.append(nm, bubble);
    if (item.quote) body.appendChild(el("p", "出处：" + item.quote, "cy-quote"));
    if (item.type === "check" && item.checks) {
      const chips = el("div", null, "cy-checks"), names = { terminal: "到终态", succeeded: "成功", outputs: "有成果", no_pending: "没有未决确认", no_unknown: "没有未知结果" };
      for (const [k, ok] of Object.entries(item.checks)) chips.appendChild(el("span", (ok ? "✓ " : "✗ ") + names[k], ok ? "" : "no"));
      body.appendChild(chips);
    }
    if (item.answer) body.appendChild(el("p", "你的答复：" + item.answer, "cy-quote"));
    if (item.open && item.pending && typeof openPending === "function") body.appendChild(button("去处理", () => openPending(item.pending), "primary"));
    if (item.tool && item.type === "step") {
      const more = el("details", null, "cy-raw");
      more.append(el("summary", "细节"), el("p", "工具：" + item.tool + (item.failed ? "（出错）" : "") + (item.said ? "" : "；匠人这一步没说话，句子是按工具名拼的"), "meta"));
      body.appendChild(more);
    }
    row.append(avatar(state, item.who, 34, face), body);
    return row;
  }

  // ---------- 换角色 ----------
  function renderLooks(state) {
    const box = state.nodes.looks, art = C().art, key = state.editing, own = state.looks[key] || {};
    const current = art.resolveLook(state.looks, key);
    const keys = ["butler", ...C().model.artisans(state.prev)];
    const row = (label, ...nodes) => { const r = el("div", null, "cy-row"); r.append(el("span", label, "meta"), ...nodes); return r; };
    const choose = (obj, field) => { const wrap = el("div", null, "cy-opts"); for (const [k, v] of Object.entries(obj)) { const b = button(typeof v === "string" ? v : v.n, () => change(state, key, field, k), "cy-chip"); b.setAttribute("aria-pressed", String(current[field] === k)); wrap.appendChild(b); } return wrap; };
    const who = el("div", null, "cy-opts");
    for (const k of keys) { const b = button("", () => { state.editing = k; renderLooks(state); }, "cy-chip"); b.append(avatar(state, k, 26), el("span", nameOf(state, k))); b.setAttribute("aria-pressed", String(k === key)); who.appendChild(b); }
    const preview = el("div", null, "cy-preview");
    preview.append(avatar(state, key, 96), el("div", current.name));
    const rows = [row("改谁", who), row("样子", choose(art.SPECIES, "species")), row("颜色", choose(art.COLORS, "color")), row("配饰", choose(art.ACCS, "acc"))];
    if (key === "butler") {
      const input = el("input"); input.value = own.name || ""; input.placeholder = "管家"; input.maxLength = 6;
      input.addEventListener("change", () => change(state, key, "name", input.value));
      rows.push(row("称呼", input));
    } else rows.push(row("名字", el("span", "来自业务包，不能改", "meta")));
    rows.push(row("", button("恢复默认", () => { delete state.looks[key]; persist(state); })));
    const list = el("div", null, "cy-rows");
    list.append(...rows, el("p", "只记在这台设备上。", "meta"));
    box.replaceChildren(preview, list);
  }

  function change(state, key, field, value) {
    state.looks[key] = { ...(state.looks[key] || {}), [field]: value };
    persist(state);
  }

  // 存到本设备，并让所有打开的进展卡一起换。
  function persist(state) {
    C().art.saveLooks(state.looks);
    for (const other of live) {
      if (other !== state) other.looks = JSON.parse(JSON.stringify(state.looks));
      other.scene.restyle();
      renderStory(other);
    }
    renderLooks(state);
  }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.card = { supported, mount, update };
})();
