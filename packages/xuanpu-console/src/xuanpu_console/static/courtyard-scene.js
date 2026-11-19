// 小院场景（R7）：SVG 画布、角色走动与动作。只接收模型给的节拍（play）或终态（settle），
// 自己不读记录、不发请求。家具与角色的画法是常量片段；名字与气泡文字一律走 textContent。
"use strict";

(function () {
  const NS = "http://www.w3.org/2000/svg";
  const art = () => globalThis.Courtyard.art;
  const HOMES = [300, 400, 500, 600, 700];
  const HOME_Y = 330, DESK = { x: 170, y: 398 }, TABLE = { x: 450, y: 496 };
  const SEATS = [{ x: 352, y: 450 }, { x: 548, y: 450 }, { x: 400, y: 470 }, { x: 500, y: 470 }, { x: 450, y: 440 }];
  const ST = { cabinet: { x: 345, y: 248 }, books: { x: 455, y: 248 }, computer: { x: 580, y: 250 },
    tv: { x: 715, y: 256 }, bed: { x: 835, y: 238 }, bench: { x: 810, y: 422 } };
  const SHORT = { list_artisans: "翻名册…", answer_artisan: "照你原话答", rate_divergence: "比对结论", propose: "拟草稿",
    review: "复核结果", think: "我想想怎么办…", read_personal: "看看你的事", check_dispatch: "问问进展", cancel_dispatch: "请先停下", cancel_group: "请先停下",
    ask_user: "转问你", extend_turn: "多给点时间", raise_budget: "提高预算" };
  const SIGNAL_WAIT_MS = 6000;

  const make = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  };
  const wide = c => c.charCodeAt(0) > 0x2000;
  const textWidth = (s, fs = 14) => [...s].reduce((w, c) => w + (wide(c) ? fs : fs * 0.58), 0);
  const clip = (s, n) => { const t = String(s || ""); return [...t].length > n ? [...t].slice(0, n - 1).join("") + "…" : t; };
  const ease = p => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);

  // 时钟：按场景各自推进；销毁后作废全部在途动作。
  function clock() {
    const c = { speed: 1, tasks: new Set(), alive: true, last: 0 };
    const tick = now => {
      if (!c.alive) return;
      const dt = Math.min(64, now - (c.last || now)); c.last = now;
      for (const t of [...c.tasks]) {
        t.left -= dt * c.speed;
        if (t.onTick) t.onTick(1 - Math.max(0, t.left) / t.total);
        if (t.left <= 0) { c.tasks.delete(t); t.resolve(); }
      }
      if (c.onFrame) c.onFrame();
      requestAnimationFrame(tick);
    };
    c.run = (ms, onTick) => new Promise((resolve, reject) => c.tasks.add({ left: ms, total: Math.max(1, ms), onTick, resolve, reject }));
    c.stop = () => { c.alive = false; for (const t of c.tasks) t.reject(new DOMException("场景已关闭", "AbortError")); c.tasks.clear(); };
    requestAnimationFrame(tick);
    return c;
  }

  function bubbleNodes(group, text, kind) {
    group.replaceChildren();
    if (!text) return 0;
    const w = textWidth(text) + 24, h = 28, seal = kind === "red", bad = kind === "bad";
    const fill = seal ? "#fff1ee" : bad ? "#e6e8ee" : "#ffffff", stroke = seal ? "#d2432f" : art().INK;
    make("path", { d: "M-6,-1 L0,8 L6,-1Z", fill, stroke, "stroke-width": 2, "stroke-linejoin": "round" }, group);
    make("rect", { class: "bx", x: -w / 2, y: -h, width: w, height: h, rx: 14, fill, stroke, "stroke-width": 2 }, group);
    make("path", { d: "M-4.5,-1 h9", stroke: fill, "stroke-width": 3 }, group);
    const label = make("text", { class: "bt", x: 0, y: -9, "text-anchor": "middle", fill: seal ? "#b3311f" : art().INK }, group);
    label.textContent = text;
    return w;
  }

  class Actor {
    constructor(scene, key, x, y) {
      Object.assign(this, { scene, key, x, y, home: { x, y }, chain: Promise.resolve(), bw: 0 });
      this.g = make("g", { class: "actor", "data-face": "open", role: "button", tabindex: "0" }, scene.layers.actors);
      this.g._y = () => this.y;
      this.g.addEventListener("click", () => scene.onSelect(key));
      make("ellipse", { cx: 0, cy: 1, rx: 26, ry: 6, fill: "#2b2f3a", opacity: ".13" }, this.g);
      make("ellipse", { class: "ring", cx: 0, cy: 1, rx: 33, ry: 10, fill: "none", stroke: "#2f6f5e", "stroke-width": 2.6, "stroke-dasharray": "6 4" }, this.g);
      const bob = make("g", { class: "bob" }, this.g), scale = make("g", { transform: "scale(1.2)" }, bob);
      this.skinNode = make("g", {}, scale);
      this.propNode = make("g", {}, scale);
      const zz = make("text", { class: "zz", x: 26, y: -74 }, bob); zz.textContent = "z";
      this.tag = make("text", { class: "tag", x: 0, y: 19, "text-anchor": "middle" }, this.g);
      this.bubbleNode = make("g", {}, scene.layers.bubbles);
    }
    skin(look) {
      this.look = look;
      this.skinNode.innerHTML = art().charMarkup(look);
      this.tag.textContent = look.name;
      this.g.setAttribute("aria-label", look.name);
      this.place();
    }
    place() {
      this.g.setAttribute("transform", `translate(${this.x.toFixed(1)},${this.y.toFixed(1)})`);
      // 贴边时只挪气泡框，尖角仍指着角色
      const half = this.bw / 2, shift = Math.max(half + 4 - this.x, Math.min(0, 896 - half - this.x));
      const top = art().TOP[this.look ? this.look.species : ""] || 100;
      this.bubbleNode.setAttribute("transform", `translate(${this.x.toFixed(1)},${(this.y - top).toFixed(1)})`);
      for (const n of this.bubbleNode.querySelectorAll(".bx,.bt")) n.setAttribute("transform", `translate(${shift.toFixed(1)},0)`);
    }
    // 动作按角色排队；场景销毁时的中止不算错误
    do(fn) {
      this.chain = this.chain.then(() => fn(this)).catch(e => { if (e && e.name !== "AbortError") console.error(e); });
      return this.chain;
    }
    async walk(x, y) {
      const sx = this.x, sy = this.y, dx = x - sx, dy = y - sy, dist = Math.hypot(dx, dy);
      if (dist < 2) return;
      this.g.classList.add("walking");
      await this.scene.clock.run(dist / 0.27, p => { const e = ease(p); this.x = sx + dx * e; this.y = sy + dy * e; this.place(); });
      this.g.classList.remove("walking");
    }
    face(f) { this.g.dataset.face = f || "open"; }
    pose(p) { this.g.classList.remove("working", "asking", "sleeping"); if (p) this.g.classList.add(p); }
    hold(k) { this.propNode.innerHTML = k ? art().PROPS[k] : ""; }
    bubble(text, kind) { this.bw = bubbleNodes(this.bubbleNode, text ? clip(text, 16) : "", kind); this.place(); }
    async say(text, ms, kind) { this.bubble(text, kind); await this.scene.clock.run(ms); this.bubble(null); }
    jump(x, y) { this.x = x; this.y = y; this.place(); }
  }

  function create(host, options) {
    const svg = make("svg", { viewBox: "0 0 900 520", role: "img", "aria-label": "小院：管家与匠人正在办这件事，文字经过见下方" });
    svg.classList.add("courtyard-svg");
    host.appendChild(svg);
    const layers = {};
    for (const name of ["back", "actors", "fx", "bubbles"]) layers[name] = make("g", {}, svg);
    layers.back.innerHTML = roomMarkup();
    const scene = { svg, layers, actors: new Map(), clock: clock(), signals: new Map(), onSelect: options.onSelect || (() => {}), stationUse: {} };
    scene.clock.onFrame = () => sortActors(scene);
    for (const item of furniture()) {
      const g = make("g", { transform: `translate(${item.x},${item.y})` }, layers.actors);
      g.classList.add("station"); g.dataset.station = item.key; g._y = () => item.y; g.innerHTML = item.markup;
    }
    scene.api = {
      cast: keys => cast(scene, keys, options.look),
      settle: poses => settle(scene, poses),
      play: beats => { for (const beat of beats) direct(scene, beat); },
      // 所有角色的动作都排完时兑现；之后由卡片把画面对齐到最新事实
      idle: async () => {
        for (;;) {
          const chains = [...scene.actors.values()].map(a => a.chain);
          await Promise.all(chains);
          if ([...scene.actors.values()].every((a, i) => a.chain === chains[i])) return;
        }
      },
      restyle: () => { for (const [key, actor] of scene.actors) actor.skin(options.look(key)); },
      select: key => { for (const [k, a] of scene.actors) a.g.classList.toggle("sel", k === key); },
      destroy: () => { scene.clock.stop(); svg.remove(); },
    };
    return scene.api;
  }

  function sortActors(scene) {
    const kids = [...scene.layers.actors.children], sorted = kids.slice().sort((a, b) => a._y() - b._y());
    if (kids.some((k, i) => k !== sorted[i])) for (const n of sorted) scene.layers.actors.appendChild(n);
  }

  // 出场：管家固定在管家台，匠人按第一次派工的先后坐上垫子（最多 5 位）。
  function cast(scene, keys, look) {
    for (const key of ["butler", ...keys.slice(0, HOMES.length)]) {
      if (scene.actors.has(key)) continue;
      const slot = scene.actors.size - 1, actor = key === "butler"
        ? new Actor(scene, key, DESK.x, DESK.y) : new Actor(scene, key, HOMES[slot], HOME_Y);
      actor.seat = key === "butler" ? null : SEATS[slot];
      scene.actors.set(key, actor);
      actor.skin(look(key));
      if (key !== "butler") { actor.pose("sleeping"); actor.face("closed"); }
    }
    return keys.length > HOMES.length ? keys.length - HOMES.length : 0;
  }

  function stationOn(scene, key, on) {
    const node = scene.svg.querySelector(`[data-station="${key}"]`);
    if (!node) return;
    scene.stationUse[key] = Math.max(0, (scene.stationUse[key] || 0) + (on ? 1 : -1));
    node.classList.toggle("on", scene.stationUse[key] > 0);
  }

  function spot(actor, poseItem) {
    if (poseItem.at === "station" && ST[poseItem.station]) return ST[poseItem.station];
    if (poseItem.at === "table") return actor.key === "butler" ? TABLE : actor.seat || TABLE;
    return actor.home;
  }

  // 终态：直接把每个角色放到该在的地方，不播动画（刷新、已结束、减少动效）。
  function settle(scene, poses) {
    for (const key of Object.keys(scene.stationUse)) { scene.stationUse[key] = 1; stationOn(scene, key, false); }
    for (const [key, actor] of scene.actors) {
      // 这件事里没出场的角色：回到待命（管家在管家台，匠人在垫子上歇着）
      const p = poses[key] || (key === "butler" ? { at: "home", face: "open" } : { at: "home", pose: "sleeping", face: "closed" });
      const where = spot(actor, p);
      actor.jump(where.x, where.y);
      actor.pose(p.pose); actor.face(p.face); actor.hold(p.prop);
      actor.bubble(p.bubble, p.red ? "red" : null);
      if (p.at === "station") stationOn(scene, p.station, true);
      actor.station = p.at === "station" ? p.station : null;
    }
  }

  function signal(scene, name) {
    if (!scene.signals.has(name)) { let fire; const promise = new Promise(r => { fire = r; }); scene.signals.set(name, { promise, fire }); }
    return scene.signals.get(name);
  }
  const fire = (scene, name) => signal(scene, name).fire();
  const waitFor = (scene, name) => Promise.race([signal(scene, name).promise, scene.clock.run(SIGNAL_WAIT_MS)]);

  function fly(scene, markup, from, to, ms, arc) {
    const g = make("g", {}, scene.layers.fx);
    g.innerHTML = markup;
    return scene.clock.run(ms, p => {
      const e = ease(p);
      g.setAttribute("transform", `translate(${(from.x + (to.x - from.x) * e).toFixed(1)},${(from.y + (to.y - from.y) * e - arc * 4 * e * (1 - e)).toFixed(1)}) rotate(${((1 - e) * -20).toFixed(1)})`);
    }).finally(() => g.remove());
  }

  function leave(scene, actor) {
    if (actor.station) { stationOn(scene, actor.station, false); actor.station = null; }
    actor.pose(null); actor.face("open"); actor.bubble(null);
  }

  // 一个节拍 → 一个或两个角色排一段动作。跨角色的先后用信号对齐（等不到就按时放行）。
  // 信号按派工区分：同一位匠人被再次派工时，不会沿用上一次已经兑现的信号。
  function direct(scene, beat) {
    const butler = scene.actors.get("butler"), actor = scene.actors.get(beat.actor), handler = BEATS[beat.type];
    if (!actor || !butler || !handler) return;
    handler({ scene, beat, butler, actor, key: beat.dispatch || beat.actor, run: ms => scene.clock.run(ms), F: art().FLY });
  }

  async function briefLetter(c, b) {
    b.hold("quill"); await b.say("写信交代", 900); b.hold(null);
    await fly(c.scene, c.F.letter, { x: b.x + 24, y: b.y - 60 }, { x: c.actor.x, y: c.actor.y - 60 }, 800, 110);
  }

  async function briefInPerson(c, b) {
    if (c.beat.how === "table") {
      await b.walk(TABLE.x, TABLE.y); fire(c.scene, "call:" + c.key);
      await waitFor(c.scene, "seated:" + c.key); await b.say("请你看看这件事", 1400);
    } else {
      await b.walk(c.actor.home.x - 62, c.actor.home.y + 16); fire(c.scene, "call:" + c.key);
      await b.say("交代任务", 1500);
    }
  }

  async function returnHome(c, a) {
    const F = c.F, butler = c.butler;
    if (!c.beat.ok) {
      a.face("dizzy"); await a.say("没办成", 1100, "bad");
      await fly(c.scene, F.note, { x: a.x, y: a.y - 60 }, { x: butler.x + 24, y: butler.y - 60 }, 800, 100);
    } else if (c.beat.how === "table") {
      a.face("happy"); a.hold("scroll"); await a.walk(a.seat.x, a.seat.y); a.hold(null); await a.say("说完了", 1200);
    } else if (c.beat.how === "letter") {
      a.face("happy"); await fly(c.scene, F.letter, { x: a.x, y: a.y - 60 }, { x: butler.x + 24, y: butler.y - 60 }, 800, 100);
    } else {
      a.face("happy"); a.hold("scroll"); await a.walk(DESK.x + 122, DESK.y + 50); a.hold(null);
      await fly(c.scene, F.scroll, { x: a.x - 10, y: a.y - 50 }, { x: butler.x + 30, y: butler.y - 40 }, 450, 24);
    }
  }

  const BEATS = {
    butler_tool: c => c.butler.do(async b => { b.hold(c.beat.prop); await b.say(SHORT[c.beat.tool] || clip(c.beat.text, 8), 1300); b.hold(null); }),
    brief: c => {
      c.butler.do(async b => {
        b.face("open"); b.hold(null);
        if (c.beat.how === "letter") await briefLetter(c, b); else await briefInPerson(c, b);
        fire(c.scene, "briefed:" + c.key);
        if (c.beat.how !== "letter") await b.walk(DESK.x, DESK.y);
        b.hold("tea"); b.face("happy");
      });
      c.actor.do(async a => {
        if (c.beat.how === "table") {
          await waitFor(c.scene, "call:" + c.key); a.pose(null); a.face("open");
          await a.walk(a.seat.x, a.seat.y); fire(c.scene, "seated:" + c.key);
        }
        await waitFor(c.scene, "briefed:" + c.key);
        a.pose(null); a.face("open");
        if (c.beat.how === "letter") { a.hold("letter"); await c.run(800); a.hold(null); }
        await a.say("收到！", 700);
      });
    },
    wake: c => c.actor.do(async a => { a.pose(null); a.face("open"); }),
    work: c => c.actor.do(async a => {
      leave(c.scene, a);
      const s = ST[c.beat.station], n = c.scene.stationUse[c.beat.station] || 0, off = [0, 34, -34][n % 3];
      await a.walk(s.x + off, s.y);
      stationOn(c.scene, c.beat.station, true); a.station = c.beat.station;
      if (c.beat.station === "bed") { a.pose("sleeping"); a.face("closed"); } else { a.face("open"); a.pose("working"); }
      a.bubble(c.beat.label);
    }),
    done_tool: c => c.actor.do(async a => {
      await c.run(500); leave(c.scene, a);
      if (c.beat.ok) { a.face("happy"); await a.say("✓", 450); a.face("open"); } else await a.say("出错了", 900, "bad");
    }),
    ask: c => c.actor.do(async a => {
      a.pose("asking"); a.hold("phone"); a.bubble(c.beat.text);
      const phone = c.scene.svg.querySelector(".deskphone");
      if (phone) { phone.classList.add("ring"); await c.run(1500); phone.classList.remove("ring"); }
    }),
    seal: c => c.actor.do(async a => { a.pose("asking"); a.hold("paper"); a.bubble("等你盖章", "red"); }),
    return: c => c.actor.do(async a => {
      leave(c.scene, a); a.hold(null);
      await returnHome(c, a);
      fire(c.scene, "returned:" + c.key);
      if (c.beat.how !== "table") { await a.walk(a.home.x, a.home.y); if (c.beat.ok) { a.pose("sleeping"); a.face("closed"); } }
    }),
    check: c => c.butler.do(async b => {
      await waitFor(c.scene, "returned:" + c.key);
      await b.walk(DESK.x, DESK.y);
      const passed = c.beat.checks ? Object.values(c.beat.checks).filter(Boolean).length : 5;
      b.hold("magnifier"); b.face("open");
      if (c.beat.ok) for (let i = 1; i <= passed; i++) { b.bubble("验收 " + "✓".repeat(i)); await c.run(220); }
      else { b.bubble("验收 ✗", "bad"); await c.run(900); }
      await c.run(400); b.bubble(null); b.hold(null);
    }),
    wait: c => c.butler.do(async b => { await b.walk(DESK.x, DESK.y); b.hold("tea"); b.face("happy"); }),
    ruling: c => c.butler.do(async b => { b.hold(null); await b.walk(TABLE.x, TABLE.y); b.bubble("等你裁决", "red"); }),
    reply: c => c.butler.do(async b => {
      b.bubble(null); b.face("happy"); b.hold("scroll"); await b.say("这就回话", 900); b.hold(null);
      await fly(c.scene, c.F.scroll, { x: b.x + 20, y: b.y - 56 }, { x: 84, y: 160 }, 800, 50);
      await b.walk(DESK.x, DESK.y);
      for (const [key, a] of c.scene.actors) if (key !== "butler" && a.seat && a.x !== a.home.x) a.do(async x => { x.bubble(null); await x.walk(x.home.x, x.home.y); x.pose("sleeping"); x.face("closed"); });
    }),
  };

  // ---------- 小院：后墙、家具（常量画法） ----------
  function shelfMarkup() {
    const cols = ["#f4a9a3", "#f2c14e", "#7fa99b", "#a4c9e3", "#c9b8e6", "#fffaf0", "#5e9c76"], ink = art().INK;
    let m = `<rect class="ik" x="-50" y="-130" width="100" height="130" rx="6" fill="#b98a5e"/>`, k = 0;
    for (let r = 0; r < 3; r++) {
      const y = -123 + r * 41;
      m += `<rect x="-43" y="${y}" width="86" height="34" rx="2" fill="#7a573c"/>`;
      let x = -40;
      while (x < 33) { const w = 6 + ((k * 5) % 4) * 1.5, h = 21 + ((k * 7) % 4) * 3, pick = r === 0 && k === 4 ? ' class="pick"' : ""; m += `<rect${pick} x="${x}" y="${y + 34 - h}" width="${w}" height="${h}" rx="1.5" fill="${cols[k % cols.length]}" stroke="${ink}" stroke-width="1.4"/>`; x += w + 1.5; k++; }
    }
    return m;
  }

  function roomMarkup() {
    const ink = art().INK;
    const planks = [244, 292, 350, 418, 492].map(y => `<path d="M0,${y} H900" stroke="#dfcba6" stroke-width="2"/>`).join("");
    const cushions = HOMES.map(x => `<ellipse cx="${x}" cy="334" rx="33" ry="9.5" fill="#c9dfd2" stroke="#9dbfae" stroke-width="2"/>`).join("");
    const label = (x, y, t) => `<text x="${x}" y="${y}" text-anchor="middle" font-size="12.5" fill="#4a5262">${t}</text>`;
    return `
  <rect width="900" height="200" fill="#e3ece6"/><rect y="166" width="900" height="34" fill="#cddbd3"/><path d="M0,166 H900" stroke="#b5c8bd" stroke-width="2"/>
  <rect y="200" width="900" height="320" fill="#ecdcc0"/>${planks}<path class="ik" d="M0,200 H900" fill="none"/>
  <g transform="translate(80,98)"><circle r="54" fill="#bfe0d0" stroke="#8a6243" stroke-width="7"/><circle r="50.5" fill="none" stroke="${ink}" stroke-width="2"/><path d="M-30,40 q6,-44 -4,-78 M-18,44 q10,-40 4,-70" fill="none" stroke="#5e9c76" stroke-width="3" stroke-linecap="round"/><path d="M-33,-8 q-14,-4 -16,-16 q13,2 16,16Z M-30,-24 q10,-10 22,-8 q-8,10 -22,8Z M-14,6 q12,-8 22,-2 q-10,8 -22,2Z" fill="#5e9c76"/><circle cx="14" cy="6" r="13" fill="${ink}"/><path d="M-9,50 C-9,26 37,26 37,48 Q14,58 -9,50Z" fill="${ink}"/></g>
  ${label(80, 180, "你在窗外")}
  <g transform="translate(868,0)"><path class="ik" d="M0,0 V24" fill="none"/><ellipse class="ik" cx="0" cy="44" rx="15" ry="20" fill="#f2c14e"/><path class="ik" d="M-9,27 h18 M-9,61 h18 M0,64 v12" fill="none"/></g>
  <g transform="translate(520,26)"><rect class="ik" x="-60" y="-16" width="120" height="32" rx="5" fill="#8a6243"/><text x="3" y="7" text-anchor="middle" font-size="19" letter-spacing="6" fill="#f6e7c8" font-family="Kaiti SC,STKaiti,KaiTi,serif">玄圃小院</text></g>
  <ellipse cx="170" cy="448" rx="152" ry="50" fill="#cfe3d6" stroke="#a9cbb8" stroke-width="2"/><ellipse cx="170" cy="448" rx="138" ry="41" fill="none" stroke="#a9cbb8" stroke-width="2" stroke-dasharray="3 7"/>
  ${cushions}
  ${label(345, 88, "档案柜")}${label(455, 78, "书架")}${label(580, 90, "电脑")}${label(715, 52, "看板")}${label(835, 124, "小床")}`;
  }

  function furniture() {
    const ink = art().INK;
    const drawer = (y, mid) => `<g>${mid ? `<g class="peek"><rect class="ik" x="-22" y="${y - 11}" width="14" height="14" rx="2" fill="#f2c14e"/><rect class="ik" x="-4" y="${y - 14}" width="14" height="17" rx="2" fill="#fffaf0"/><rect class="ik" x="13" y="${y - 9}" width="12" height="12" rx="2" fill="#f4a9a3"/></g>` : ""}<rect class="ik" x="-30" y="${y}" width="60" height="31" rx="4" fill="#a9c9bb"/><rect x="-9" y="${y + 12}" width="18" height="6" rx="3" fill="${ink}"/></g>`;
    const name = (t, y = 16) => `<text x="0" y="${y}" text-anchor="middle" font-size="12.5" fill="#4a5262">${t}</text>`;
    return [
      { key: "cabinet", x: 345, y: 215, markup: `<rect class="ik" x="-37" y="-120" width="74" height="120" rx="7" fill="#7fa99b"/>${drawer(-112)}${drawer(-75, true)}${drawer(-38)}` },
      { key: "books", x: 455, y: 215, markup: shelfMarkup() },
      { key: "computer", x: 580, y: 215, markup: `<rect class="ik" x="-37" y="-118" width="74" height="52" rx="6" fill="${ink}"/><rect class="scr" x="-31" y="-112" width="62" height="40" rx="3" fill="#9fb8ae"/><g><rect class="ln" x="-25" y="-106" width="30" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-98" width="46" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-90" width="22" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-82" width="38" height="4" rx="2" fill="#2f6f5e"/></g><path class="ik" d="M-7,-66 h14 l3,8 h-20Z" fill="#5a6072"/><rect class="ik" x="-61" y="-58" width="122" height="11" rx="4" fill="#c99a6b"/><rect class="ik" x="-55" y="-47" width="9" height="47" fill="#b98a5e"/><rect class="ik" x="46" y="-47" width="9" height="47" fill="#b98a5e"/><rect class="ik" x="-21" y="-65" width="42" height="7" rx="2" fill="#fffaf0"/>` },
      { key: "tv", x: 715, y: 215, markup: `<rect class="ik" x="-53" y="-154" width="106" height="68" rx="7" fill="${ink}"/><rect class="scr" x="-47" y="-148" width="94" height="56" rx="3" fill="#56607a"/><g>${[16, 28, 20, 36, 24].map((h, i) => `<rect class="tvbar" x="${-38 + i * 16}" y="${-98 - h}" width="10" height="${h}" rx="2" fill="${i % 2 ? "#7cc48f" : "#f2c14e"}"/>`).join("")}</g><rect class="ik" x="-47" y="-42" width="94" height="42" rx="5" fill="#c99a6b"/><path class="ik" d="M0,-42 V0" fill="none"/><circle cx="-7" cy="-21" r="2.4" fill="${ink}"/><circle cx="7" cy="-21" r="2.4" fill="${ink}"/>` },
      { key: "bed", x: 835, y: 215, markup: `<rect class="ik" x="-49" y="-82" width="98" height="66" rx="13" fill="#b98a5e"/><rect class="ik" x="-45" y="-32" width="90" height="72" rx="8" fill="#fffaf0"/><ellipse class="ik" cx="0" cy="-22" rx="29" ry="10.5" fill="#ffffff"/>` },
      { key: "blanket", x: 835, y: 262, markup: `<g transform="translate(0,-47)"><rect class="ik" x="-47" y="-8" width="94" height="52" rx="9" fill="#9fcfb2"/><path d="M-47,5 h94" stroke="#fff" stroke-width="3" opacity=".7"/><rect class="ik" x="-50" y="38" width="100" height="12" rx="5" fill="#b98a5e"/></g>` },
      { key: "desk", x: 170, y: 430, markup: `<rect class="ik" x="-80" y="-36" width="160" height="36" rx="5" fill="#b98a5e"/><rect class="ik" x="-87" y="-48" width="174" height="13" rx="5" fill="#c99a6b"/><g transform="translate(-60,-48)"><g class="deskphone"><rect class="ik" x="-14" y="-11" width="28" height="11" rx="3.5" fill="${ink}"/><path d="M-16,-17 q16,-10 32,0" fill="none" stroke="${ink}" stroke-width="6" stroke-linecap="round"/><circle cx="0" cy="-5.5" r="2.6" fill="#f2c14e"/></g></g><g transform="translate(62,-48)"><path class="ik" d="M-10,0 a10,10 0 0 1 20,0Z" fill="#f2c14e"/><rect x="-12" y="-1.5" width="24" height="3.5" rx="1.7" fill="${ink}"/></g><text x="0" y="-13" text-anchor="middle" font-size="12.5" fill="#f6e7c8">管家台</text>` },
      { key: "table", x: 450, y: 455, markup: `<path class="ik" d="M-8,-12 h16 v10 h12 v5 h-40 v-5 h12Z" fill="#b98a5e"/><ellipse class="ik" cx="0" cy="-30" rx="64" ry="20" fill="#c99a6b"/><g transform="translate(0,-38)"><ellipse class="ik" rx="12" ry="8.5" fill="#7fa99b"/><circle class="ik" cx="0" cy="-9" r="2.6" fill="#7fa99b"/></g>${name("圆桌")}` },
      { key: "bench", x: 810, y: 455, markup: `<rect class="ik" x="-62" y="-30" width="10" height="30" fill="#b98a5e"/><rect class="ik" x="52" y="-30" width="10" height="30" fill="#b98a5e"/><rect class="ik" x="-70" y="-42" width="140" height="13" rx="4" fill="#c99a6b"/><rect class="ik" x="-54" y="-57" width="20" height="15" rx="2" fill="#5a6072"/><g class="gear"><circle class="ik" cx="0" cy="-53" r="10" fill="#f2c14e" stroke-dasharray="5 3.4"/><circle cx="0" cy="-53" r="3.4" fill="${ink}"/></g><rect class="ik" x="30" y="-59" width="24" height="17" rx="2" fill="#7fa99b"/>${name("工作台")}` },
    ];
  }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.scene = { create, clip };
})();
