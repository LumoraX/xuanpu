// 小院场景（R7）：SVG 画布、多尺度建筑空间布局（小院 / 四合院 / 故宫）与角色动画。
// 底层业务与工位语义（cabinet/books/computer/tv/bed/bench/table/desk）完全不变，仅展示空间与建筑形态不同：
// - 小院：单间工作室，匠人同屋席地而坐，超出 5 位自动多排扩容；
// - 四合院：正房 + 东西厢房 + 倒座房 + 中庭院心，匠人分居东西各厢房，超出 6 位自动沿院落纵深增建厢房；
// - 故宫：中轴三大殿 + 东西六宫 + 外朝诸署，匠人分驻各自独立宫殿院落，超出 12 位自动向外东/西路增建偏殿。
// 家具与建筑画法是常量片段；名字与气泡文字一律由场景用 textContent 写入。
"use strict";

(function () {
  const NS = "http://www.w3.org/2000/svg";
  const art = () => globalThis.Courtyard.art;
  const THEME_KEY = "xuanpu-courtyard-theme";
  const ROW_XS = [300, 400, 500, 600, 700];
  const FIRST_ROW_Y = 330, ROW_STEP = 118;
  const PALACE_NAMES = [
    "景仁宫", "翊坤宫", "承乾宫", "储秀宫",
    "钟粹宫", "永寿宫", "延禧宫", "启祥宫",
    "永和宫", "长春宫", "景阳宫", "咸福宫",
    "宁寿东院", "慈宁西院", "畅音阁", "寿安宫",
  ];
  const SHORT = {
    list_artisans: "翻名册…", answer_artisan: "照你原话答", rate_divergence: "比对结论", propose: "拟草稿",
    act_personal: "替你办好了",
    review: "复核结果", think: "我想想怎么办…", read_personal: "看看你的事", check_dispatch: "问问进展",
    check_group: "问问编组进展", answer_pending: "转答匠人",
    cancel_dispatch: "请先停下", cancel_group: "请先停下", ask_user: "转问你", extend_turn: "多给点时间",
    raise_budget: "提高预算",
  };
  const SIGNAL_WAIT_MS = 6000;

  // 三套不同尺度的空间主题：同一套底层工位与节拍，不同空间组织方式。
  const THEMES = {
    courtyard: {
      id: "courtyard", label: "小院", title: "玄圃小院", windowLabel: "你在窗外",
      wall: "#e3ece6", wainscot: "#cddbd3", wainscotLine: "#b5c8bd", floor: "#ecdcc0", plank: "#dfcba6",
      plaqueFill: "#8a6243", plaqueText: "#f6e7c8", rugFill: "#cfe3d6", rugStroke: "#a9cbb8",
      cushionFill: "#c9dfd2", cushionStroke: "#9dbfae", labelColor: "#4a5262",
      woodDark: "#b98a5e", woodLight: "#c99a6b", cabinetFill: "#7fa99b", drawerFill: "#a9c9bb",
      deskLabel: "管家台", tableLabel: "圆桌", benchLabel: "工作台",
      stations: { cabinet: "档案柜", books: "书架", computer: "电脑", tv: "看板", bed: "小床" },
    },
    siheyuan: {
      id: "siheyuan", label: "四合院", title: "玄圃四合院", windowLabel: "垂花门外",
      wall: "#d4dce0", wainscot: "#b5c0c6", wainscotLine: "#8d9aa2", floor: "#cfd6d9", plank: "#b8c2c6",
      plaqueFill: "#33424c", plaqueText: "#f2e7cf", rugFill: "#d5e0da", rugStroke: "#8fa89b",
      cushionFill: "#d9beb2", cushionStroke: "#a46d5b", labelColor: "#38434c",
      woodDark: "#8f3f36", woodLight: "#b4594a", cabinetFill: "#5f7d7e", drawerFill: "#8faaa9",
      deskLabel: "正房·总管案", tableLabel: "院心石桌", benchLabel: "巧工坊",
      stations: { cabinet: "卷宗阁", books: "东书斋", computer: "电算房", tv: "榜文壁", bed: "歇息轩" },
    },
    palace: {
      id: "palace", label: "故宫", title: "玄圃紫禁城", windowLabel: "午门之外",
      wall: "#a33329", wainscot: "#84261f", wainscotLine: "#d9a94b", floor: "#e8dec7", plank: "#d8cab0",
      plaqueFill: "#1f3354", plaqueText: "#f7d47c", rugFill: "#f5e3b3", rugStroke: "#d4a442",
      cushionFill: "#f1cf7a", cushionStroke: "#b87e24", labelColor: "#542f24",
      woodDark: "#8c2620", woodLight: "#b83c30", cabinetFill: "#9e3f33", drawerFill: "#d9a94b",
      deskLabel: "太和殿·内务府", tableLabel: "中和殿·议政台", benchLabel: "造办处",
      stations: { cabinet: "皇史宬", books: "文渊阁", computer: "军机处", tv: "钦天监", bed: "养心轩" },
    },
  };
  const THEME_ORDER = ["courtyard", "siheyuan", "palace"];

  function loadTheme() {
    try {
      const saved = globalThis.localStorage && globalThis.localStorage.getItem(THEME_KEY);
      return THEMES[saved] ? saved : "courtyard";
    } catch { return "courtyard"; }
  }
  function saveTheme(id) {
    try { if (globalThis.localStorage) globalThis.localStorage.setItem(THEME_KEY, id); } catch { /* 忽略不可用存储 */ }
  }

  function palaceHallName(index) {
    if (index < PALACE_NAMES.length) return PALACE_NAMES[index];
    const side = index % 2 === 0 ? "东" : "西";
    return `${side}路偏殿${Math.floor((index - PALACE_NAMES.length) / 2) + 1}`;
  }

  // 根据主题与匠人数量计算完整空间几何：画布尺寸、角色缩放、各工位坐标、以及每位匠人的驻地坐标。
  function computeLayout(themeId, artisanCount) {
    const id = THEMES[themeId] ? themeId : "courtyard";
    if (id === "siheyuan") return computeSiheyuanLayout(artisanCount);
    if (id === "palace") return computePalaceLayout(artisanCount);
    return computeCourtyardLayout(artisanCount);
  }

  // 1. 小院：单间工作室，多排垫子自动扩展
  function computeCourtyardLayout(artisanCount) {
    const count = Math.max(ROW_XS.length, artisanCount || 0);
    const rows = Math.max(1, Math.ceil(count / ROW_XS.length));
    const extraH = (rows - 1) * ROW_STEP;
    const width = 900, height = 520 + extraH;
    const slots = [];
    for (let r = 0; r < rows; r++) {
      const y = FIRST_ROW_Y + r * ROW_STEP;
      const inRow = r === rows - 1 ? count - r * ROW_XS.length : ROW_XS.length;
      const xs = inRow === ROW_XS.length
        ? ROW_XS
        : Array.from({ length: inRow }, (_, i) => Math.round(300 + ((i + 0.5) / inRow) * 400));
      for (const x of xs) slots.push({ x, y, briefAt: { x: x - 62, y: y + 16 } });
    }
    const desk = { x: 170, y: 398 + extraH };
    const table = { x: 450, y: 496 + extraH };
    const bench = { x: 810, y: 422 + extraH };
    const stations = {
      cabinet: { x: 345, y: 248 }, books: { x: 455, y: 248 }, computer: { x: 580, y: 250 },
      tv: { x: 715, y: 256 }, bed: { x: 835, y: 238 }, bench,
    };
    const seats = [
      { x: 352, y: 450 + extraH }, { x: 548, y: 450 + extraH },
      { x: 400, y: 470 + extraH }, { x: 500, y: 470 + extraH }, { x: 450, y: 440 + extraH },
    ];
    const t = THEMES.courtyard;
    const w1 = ((166 / height) * 100).toFixed(2), w2 = ((200 / height) * 100).toFixed(2);
    return {
      themeId: "courtyard", width, height, rows, extraH, actorScale: 1.2, furnitureScale: 1,
      slots, desk, submit: { x: desk.x + 122, y: desk.y + 50 }, table, bench, stations, seats,
      gate: { x: 84, y: 160 },
      backdrop: `linear-gradient(${t.wall} 0 ${w1}%, ${t.wainscot} ${w1}% ${w2}%, ${t.floor} ${w2}%)`,
    };
  }

  // 2. 四合院：北正房（管家）、东西厢房（匠人分居各厢）、中庭院心（石桌会诊）、南北功能房
  function computeSiheyuanLayout(artisanCount) {
    const wingRows = Math.max(3, Math.ceil((artisanCount || 0) / 2));
    const wingStep = 116;
    const extraH = (wingRows - 3) * wingStep;
    const width = 1080, height = 740 + extraH;
    const slots = [];
    const wingRooms = [];
    for (let r = 0; r < wingRows; r++) {
      const top = 232 + r * wingStep;
      const west = { side: "west", name: `西厢${r + 1}院`, x: 34, y: top, w: 204, h: 102 };
      const east = { side: "east", name: `东厢${r + 1}院`, x: 842, y: top, w: 204, h: 102 };
      wingRooms.push(west, east);
      slots.push({
        x: 136, y: top + 76, room: west,
        briefAt: { x: 266, y: top + 72 },
      });
      slots.push({
        x: 944, y: top + 76, room: east,
        briefAt: { x: 814, y: top + 72 },
      });
    }
    const desk = { x: 540, y: 168 };
    const tableY = Math.round(456 + extraH / 2);
    const table = { x: 540, y: tableY + 34 };
    const southTop = height - 146;
    const bench = { x: 222, y: southTop + 82 };
    const stations = {
      cabinet: { x: 186, y: 180 },
      books: { x: 894, y: 180 },
      bed: { x: 358, y: 312 },
      tv: { x: 722, y: 340 },
      bench,
      computer: { x: 858, y: southTop + 96 },
    };
    const seats = [
      { x: 440, y: tableY + 6 }, { x: 640, y: tableY + 6 },
      { x: 472, y: tableY + 46 }, { x: 608, y: tableY + 46 }, { x: 540, y: tableY - 26 },
    ];
    return {
      themeId: "siheyuan", width, height, rows: wingRows, extraH, southTop,
      actorScale: 0.9, furnitureScale: 0.72,
      slots, wingRooms, desk, submit: { x: 540, y: 222 }, table, bench, stations, seats,
      gate: { x: 540, y: height - 48 },
      backdrop: "#c6cfd3",
    };
  }

  // 3. 故宫：中轴线（午门-太和殿-中和殿-养心轩）+ 东西六宫（独立宫殿院落）+ 外朝诸署（皇史宬/文渊阁/军机处/造办处/钦天监）
  function computePalaceLayout(artisanCount) {
    const palaceRows = Math.max(3, Math.ceil((artisanCount || 0) / 4));
    const rowStep = 134;
    const extraH = (palaceRows - 3) * rowStep;
    const width = 1320, height = 880 + extraH;
    const slots = [];
    const compounds = [];
    const cols = [
      { x: 928, side: "east", briefX: 902 },   // 东内列（近中轴）
      { x: 222, side: "west", briefX: 418 },   // 西内列（近中轴）
      { x: 1116, side: "east", briefX: 1106 }, // 东外列
      { x: 34, side: "west", briefX: 214 },    // 西外列
    ];
    let idx = 0;
    for (let r = 0; r < palaceRows; r++) {
      const top = 198 + r * rowStep;
      for (const col of cols) {
        const name = palaceHallName(idx++);
        const box = { name, side: col.side, x: col.x, y: top, w: 170, h: 120 };
        compounds.push(box);
        slots.push({
          x: col.x + 85, y: top + 95, compound: box,
          briefAt: { x: col.briefX, y: top + 96 },
        });
      }
    }
    const desk = { x: 660, y: 318 };
    const tableCenterY = Math.round(492 + extraH / 2);
    const table = { x: 660, y: tableCenterY + 34 };
    const southTop = 640 + extraH;
    const bench = { x: 344, y: southTop + 98 };
    const stations = {
      cabinet: { x: 214, y: 166 },
      bed: { x: 660, y: 146 },
      books: { x: 1106, y: 166 },
      computer: { x: 146, y: southTop + 114 },
      bench,
      tv: { x: 1106, y: southTop + 118 },
    };
    const seats = [
      { x: 562, y: tableCenterY + 4 }, { x: 758, y: tableCenterY + 4 },
      { x: 594, y: tableCenterY + 44 }, { x: 726, y: tableCenterY + 44 }, { x: 660, y: tableCenterY - 26 },
    ];
    return {
      themeId: "palace", width, height, rows: palaceRows, extraH, southTop,
      actorScale: 0.78, furnitureScale: 0.7,
      slots, compounds, desk, submit: { x: 660, y: 376 }, table, bench, stations, seats,
      gate: { x: 660, y: height - 44 },
      backdrop: "#ded3bc",
    };
  }

  function seatFor(layout, index) {
    if (index < layout.seats.length) return layout.seats[index];
    const angle = ((index - layout.seats.length) * 0.85) + 0.35;
    return {
      x: Math.round(layout.table.x + Math.cos(angle) * 102),
      y: Math.round(layout.table.y - 30 + Math.sin(angle) * 28),
    };
  }

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
      Object.assign(this, {
        scene, key, x, y, home: { x, y }, briefAt: { x: x - 62, y: y + 16 },
        chain: Promise.resolve(), bw: 0, slot: null, seat: null, station: null, atTable: false, heldItem: null,
      });
      this.g = make("g", { class: "actor", "data-face": "open", role: "button", tabindex: "0" }, scene.layers.actors);
      this.g._y = () => this.y;
      this.g.addEventListener("click", () => scene.onSelect(key));
      this.shadow = make("ellipse", { cx: 0, cy: 1, rx: 26, ry: 6, fill: "#2b2f3a", opacity: ".13" }, this.g);
      this.ring = make("ellipse", { class: "ring", cx: 0, cy: 1, rx: 33, ry: 10, fill: "none", stroke: "#2f6f5e", "stroke-width": 2.6, "stroke-dasharray": "6 4" }, this.g);
      const bob = make("g", { class: "bob" }, this.g);
      this.scaleNode = make("g", { transform: `scale(${scene.layout.actorScale})` }, bob);
      this.skinNode = make("g", {}, this.scaleNode);
      this.propNode = make("g", {}, this.scaleNode);
      this.zzNode = make("text", { class: "zz", x: 26, y: -74 }, bob); this.zzNode.textContent = "z";
      this.tag = make("text", { class: "tag", x: 0, y: 19, "text-anchor": "middle" }, this.g);
      this.bubbleNode = make("g", {}, scene.layers.bubbles);
      this.applyScale(scene.layout.actorScale);
    }
    applyScale(scale) {
      const ratio = scale / 1.2;
      this.scaleNode.setAttribute("transform", `scale(${scale.toFixed(2)})`);
      this.shadow.setAttribute("rx", (26 * ratio).toFixed(1));
      this.shadow.setAttribute("ry", (6 * ratio).toFixed(1));
      this.ring.setAttribute("rx", (33 * ratio).toFixed(1));
      this.ring.setAttribute("ry", (10 * ratio).toFixed(1));
      this.zzNode.setAttribute("x", (26 * ratio).toFixed(1));
      this.zzNode.setAttribute("y", (-74 * ratio).toFixed(1));
      this.tag.setAttribute("y", (15 + 4 * ratio).toFixed(1));
    }
    skin(look) {
      this.look = look;
      this.skinNode.innerHTML = art().charMarkup(look);
      const fullName = String(look.name || "");
      const compact = fullName.length > 6 ? fullName.replace(/^通用/, "").slice(0, 6) + "…" : fullName;
      this.tag.textContent = compact;
      this.g.setAttribute("aria-label", fullName);
      this.place();
    }
    place() {
      if (!Number.isFinite(this.x) || !Number.isFinite(this.y)) return;
      this.g.setAttribute("transform", `translate(${this.x.toFixed(1)},${this.y.toFixed(1)})`);
      // 贴边时只挪气泡框，尖角仍指着角色
      const half = this.bw / 2, maxX = this.scene.layout.width - 4;
      const shift = Math.max(half + 4 - this.x, Math.min(0, maxX - half - this.x));
      const baseTop = art().TOP[this.look ? this.look.species : ""] || 100;
      const top = baseTop * (this.scene.layout.actorScale / 1.2);
      this.bubbleNode.setAttribute("transform", `translate(${this.x.toFixed(1)},${(this.y - top).toFixed(1)})`);
      for (const n of this.bubbleNode.querySelectorAll(".bx,.bt")) n.setAttribute("transform", `translate(${shift.toFixed(1)},0)`);
    }
    do(fn) {
      this.chain = this.chain.then(() => fn(this)).catch(e => { if (e && e.name !== "AbortError") console.error(e); });
      return this.chain;
    }
    async walk(x, y) {
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      const sx = this.x, sy = this.y, dx = x - sx, dy = y - sy, dist = Math.hypot(dx, dy);
      if (dist < 2) return;
      this.g.classList.add("walking");
      // 更大尺度的四合院/故宫适当提速，避免长途穿宫耗时过久
      const speed = 0.27 * (this.scene.layout.width / 900);
      await this.scene.clock.run(dist / speed, p => { const e = ease(p); this.x = sx + dx * e; this.y = sy + dy * e; this.place(); });
      this.g.classList.remove("walking");
    }
    face(f) { this.g.dataset.face = f || "open"; }
    pose(p) { this.g.classList.remove("working", "asking", "sleeping"); if (p) this.g.classList.add(p); }
    hold(k) { this.propNode.innerHTML = k ? art().PROPS[k] : ""; }
    bubble(text, kind) { this.bw = bubbleNodes(this.bubbleNode, text ? clip(text, 20) : "", kind); this.place(); }
    async say(text, ms, kind) { this.bubble(text, kind); await this.scene.clock.run(ms); this.bubble(null); }
    jump(x, y) { if (!Number.isFinite(x) || !Number.isFinite(y)) return; this.x = x; this.y = y; this.place(); }
    remove() { this.g.remove(); this.bubbleNode.remove(); }
  }

  function stationPos(scene, name) {
    return scene.layout.stations[name];
  }

  const STATION_TIPS = {
    books: "点击打开书架资料室（通讯录、公司、项目、记录与文件）",
    tv: "点击打开看板（待办、日程与提醒）",
    cabinet: "点击打开档案柜（项目与协作记录）",
  };

  function renderEnvironment(scene) {
    const theme = THEMES[scene.themeId] || THEMES.courtyard;
    const layout = scene.layout;
    scene.svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    scene.svg.setAttribute("aria-label", `${theme.title}：管家与匠人正在办这件事，文字经过见下方`);
    if (scene.host) {
      if (scene.host.dataset) scene.host.dataset.theme = theme.id;
      if (scene.host.style && typeof scene.host.style.setProperty === "function") {
        scene.host.style.setProperty("--cy-backdrop", layout.backdrop);
      }
    }
    scene.layers.back.innerHTML = sceneMarkup(theme, layout);
    // 重建家具节点（保留原有激活态，热切换小院/四合院/故宫时不打断正在进行的工作）
    for (const node of scene.furnitureNodes) node.remove();
    scene.furnitureNodes = [];
    scene.stationNodes = {};
    scene.boardLayer = null;
    scene.booksBadgeLayer = null;
    for (const item of furniture(theme, layout)) {
      const g = make("g", { transform: `translate(${item.x},${item.y})` }, scene.layers.actors);
      g.classList.add("station");
      if ((scene.stationUse[item.key] || 0) > 0) g.classList.add("on");
      g.dataset.station = item.key;
      g._y = () => item.y;
      const inner = item.scale && item.scale !== 1 ? make("g", { transform: `scale(${item.scale})` }, g) : g;
      inner.innerHTML = item.markup;
      if (item.key === "tv") scene.boardLayer = make("g", { class: "board-live" }, inner);
      if (item.key === "books") scene.booksBadgeLayer = make("g", { class: "books-badge" }, inner);
      if (scene.onStationClick && STATION_TIPS[item.key]) {
        const tipText = STATION_TIPS[item.key];
        g.classList.add("clickable");
        g.setAttribute("role", "button");
        g.setAttribute("tabindex", "0");
        g.setAttribute("aria-label", tipText);
        const tip = make("title", {}, g);
        tip.textContent = tipText;
        g.addEventListener("click", () => scene.onStationClick(item.key));
        g.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); scene.onStationClick(item.key); }
        });
      }
      scene.stationNodes[item.key] = g;
      scene.furnitureNodes.push(g);
    }
    // 同步管家和所有匠人在新建筑布局中的位置与缩放
    const butler = scene.actors.get("butler");
    if (butler) {
      butler.applyScale(layout.actorScale);
      butler.home = { x: layout.desk.x, y: layout.desk.y };
      const target = butler.station && layout.stations[butler.station]
        ? layout.stations[butler.station]
        : butler.atTable ? layout.table : butler.home;
      butler.jump(target.x, target.y);
    }
    let idx = 0;
    for (const [k, actor] of scene.actors) {
      if (k === "butler") continue;
      actor.applyScale(layout.actorScale);
      const slot = layout.slots[idx] || layout.slots[layout.slots.length - 1];
      actor.slot = idx;
      actor.home = { x: slot.x, y: slot.y };
      actor.briefAt = slot.briefAt || { x: slot.x - 54, y: slot.y + 12 };
      actor.seat = seatFor(layout, idx);
      if (actor.station && layout.stations[actor.station]) {
        const s = layout.stations[actor.station];
        actor.jump(s.x, s.y);
      } else if (actor.atTable) {
        actor.jump(actor.seat.x, actor.seat.y);
      } else {
        actor.jump(actor.home.x, actor.home.y);
      }
      idx += 1;
    }
    renderWorld(scene);
    syncTakenClasses(scene);
    sortActors(scene);
  }

  function renderWorld(scene) {
    const world = scene.world || {};
    // 1. 看板真实待办 / 日程 / 提醒渲染（纯 SVG 节点 + textContent，不拼 HTML）
    if (scene.boardLayer) {
      scene.boardLayer.replaceChildren();
      const todos = (world.todos || []).filter(t => t && !t.done);
      const reminders = world.reminders || [];
      const events = world.events || [];
      const entries = [];
      for (const t of todos) entries.push({ kind: "todo", title: t.title || "待办", color: "#f6dc7a" });
      for (const r of reminders) entries.push({ kind: "reminder", title: "提醒·" + (r.title || "提醒"), color: "#f4a9a3" });
      for (const ev of events) entries.push({ kind: "event", title: ev.title || "日程", color: "#7cc48f" });
      const total = entries.length;

      // 顶部标题条与角标：按实际内容准确显示标题，避免提醒被误标成“待办”
      const titleNode = make("text", { class: "board-hd", x: -41, y: -136, "font-size": "8.5", fill: "#d7e3ee" }, scene.boardLayer);
      titleNode.textContent = !total ? "今日看板"
        : (todos.length && !reminders.length && !events.length) ? "今日待办"
        : (reminders.length && !todos.length && !events.length) ? "今日提醒"
        : (events.length && !todos.length && !reminders.length) ? "今日日程"
        : "今日看板";
      make("rect", {
        x: 24, y: -146, width: 20, height: 13, rx: 6.5,
        fill: total > 0 ? "#e5a93c" : "#5e9c76",
      }, scene.boardLayer);
      const badge = make("text", {
        class: "board-badge", x: 34, y: -136.5, "text-anchor": "middle",
        "font-size": "8.5", "font-weight": "700", fill: "#1f2430",
      }, scene.boardLayer);
      badge.textContent = total > 0 ? (total > 9 ? "9+" : String(total)) : "✓";

      if (!total) {
        const empty = make("text", {
          class: "board-empty", x: 0, y: -112, "text-anchor": "middle",
          "font-size": "9.5", fill: "#a9d9b7",
        }, scene.boardLayer);
        empty.textContent = "今日已清空 ✓";
      } else {
        const shown = entries.slice(0, 3);
        shown.forEach((item, i) => {
          const y = -123 + i * 13;
          make("rect", { x: -42, y: y - 6.5, width: 6.5, height: 6.5, rx: 1.5, fill: "none", stroke: item.color, "stroke-width": 1.3 }, scene.boardLayer);
          const line = make("text", { class: "board-line", x: -32, y: y - 0.5, "font-size": "8.8", fill: "#f7f4ec" }, scene.boardLayer);
          line.textContent = clip(item.title, 9);
        });
      }
    }

    // 2. 书架资料数微标
    if (scene.booksBadgeLayer) {
      scene.booksBadgeLayer.replaceChildren();
      const count = Number(world.booksCount || 0);
      if (count > 0) {
        make("rect", { x: 25, y: -136, width: 24, height: 14, rx: 7, fill: "#2f6f5e", stroke: "#2b2f3a", "stroke-width": 1.4 }, scene.booksBadgeLayer);
        const t = make("text", { x: 37, y: -126, "text-anchor": "middle", "font-size": "8.8", "font-weight": "700", fill: "#fffaf0" }, scene.booksBadgeLayer);
        t.textContent = count > 99 ? "99+" : String(count);
      }
    }

    // 3. 动态工具架能力高亮：依据当前启用匠人的连接器与能力点亮对应实体道具
    const artisans = world.artisans || [];
    const caps = {
      compass: artisans.some(a => (a.connectors || []).some(c => /amap|map|geo|weather/i.test(String(c))) || /driver|travel|route|出行|派车/i.test(String((a.package || "") + " " + (a.name || "")))),
      wrench: artisans.some(a => a.capabilities && a.capabilities.shell),
      stamp: artisans.some(a => /expense|finance|reimburse|approval|报销|财务/i.test(String((a.package || "") + " " + (a.name || "")))),
    };
    if (scene.svg && typeof scene.svg.querySelectorAll === "function") {
      for (const [key, active] of Object.entries(caps)) {
        for (const node of scene.svg.querySelectorAll(`[data-tool-item="${key}"]`)) {
          if (node.classList) node.classList.toggle("cap-ready", Boolean(active));
        }
      }
    }
  }

  function setWorld(scene, patch) {
    scene.world = { ...(scene.world || {}), ...(patch || {}) };
    renderWorld(scene);
  }

  function syncTakenClasses(scene) {
    if (!scene.svg || typeof scene.svg.querySelectorAll !== "function") return;
    for (const node of scene.svg.querySelectorAll("[data-tool-item]")) {
      const k = node.getAttribute ? node.getAttribute("data-tool-item") : null;
      if (k && node.classList) node.classList.toggle("taken", (scene.itemTaken[k] || 0) > 0);
    }
  }

  function setItemTaken(scene, itemKey, delta) {
    if (!itemKey) return;
    scene.itemTaken[itemKey] = Math.max(0, (scene.itemTaken[itemKey] || 0) + delta);
    if (!scene.svg || typeof scene.svg.querySelectorAll !== "function") return;
    for (const node of scene.svg.querySelectorAll(`[data-tool-item="${itemKey}"]`)) {
      if (node.classList) node.classList.toggle("taken", scene.itemTaken[itemKey] > 0);
    }
  }

  function takeTool(scene, actor, itemKey, prop) {
    releaseTool(scene, actor);
    if (itemKey) {
      setItemTaken(scene, itemKey, 1);
      actor.heldItem = itemKey;
    }
    actor.hold(prop || null);
  }

  function releaseTool(scene, actor) {
    if (actor.heldItem) {
      setItemTaken(scene, actor.heldItem, -1);
      actor.heldItem = null;
    }
    actor.hold(null);
  }

  function create(host, options) {
    const initialTheme = THEMES[options.theme] ? options.theme : loadTheme();
    const layout = computeLayout(initialTheme, 0);
    const svg = make("svg", { viewBox: `0 0 ${layout.width} ${layout.height}`, role: "img", "aria-label": "小院：管家与匠人正在办这件事，文字经过见下方" });
    svg.classList.add("courtyard-svg");
    host.appendChild(svg);
    const layers = {};
    for (const name of ["back", "actors", "fx", "bubbles"]) layers[name] = make("g", {}, svg);
    const scene = {
      host, svg, layers, actors: new Map(), furnitureNodes: [], stationNodes: {}, clock: clock(), signals: new Map(),
      onSelect: options.onSelect || (() => {}), onStationClick: options.onStationClick || null,
      stationUse: {}, itemTaken: {}, world: { todos: [], events: [], reminders: [], booksCount: 0, artisans: [] },
      boardLayer: null, booksBadgeLayer: null,
      themeId: initialTheme, layout,
    };
    scene.clock.onFrame = () => sortActors(scene);
    renderEnvironment(scene);
    scene.api = {
      cast: keys => cast(scene, keys, options.look),
      settle: poses => settle(scene, poses),
      setWorld: patch => setWorld(scene, patch),
      play: beats => { for (const beat of beats) direct(scene, beat); },
      idle: async () => {
        for (;;) {
          const chains = [...scene.actors.values()].map(a => a.chain);
          await Promise.all(chains);
          if ([...scene.actors.values()].every((a, i) => a.chain === chains[i])) return;
        }
      },
      restyle: () => { for (const [key, actor] of scene.actors) actor.skin(options.look(key)); },
      select: key => { for (const [k, a] of scene.actors) a.g.classList.toggle("sel", k === key); },
      theme: () => scene.themeId,
      setTheme: id => {
        if (!THEMES[id] || scene.themeId === id) return scene.themeId;
        scene.themeId = id;
        saveTheme(id);
        const artisanCount = Math.max(0, scene.actors.size - 1);
        scene.layout = computeLayout(id, artisanCount);
        renderEnvironment(scene);
        return scene.themeId;
      },
      destroy: () => { scene.clock.stop(); svg.remove(); },
    };
    return scene.api;
  }

  function sortActors(scene) {
    const kids = [...scene.layers.actors.children], sorted = kids.slice().sort((a, b) => a._y() - b._y());
    if (kids.some((k, i) => k !== sorted[i])) for (const n of sorted) scene.layers.actors.appendChild(n);
  }

  // 出场：管家固定在主位（管家台/正房总管案/太和殿内务府）；所有匠人皆有独立驻地，超出当前规模时院落/宫殿自动扩容。
  function cast(scene, keys, look) {
    if (!scene.actors.has("butler")) {
      const butler = new Actor(scene, "butler", scene.layout.desk.x, scene.layout.desk.y);
      scene.actors.set("butler", butler);
      butler.skin(look("butler"));
    }
    const existingArtisans = Math.max(0, scene.actors.size - 1);
    const additions = keys.filter(k => k && k !== "butler" && !scene.actors.has(k));
    const totalNeeded = existingArtisans + additions.length;
    const nextLayout = computeLayout(scene.themeId, totalNeeded);
    if (totalNeeded > scene.layout.slots.length || nextLayout.rows !== scene.layout.rows) {
      scene.layout = nextLayout;
      renderEnvironment(scene);
    }
    for (const key of additions) {
      const idx = scene.actors.size - 1;
      const slot = scene.layout.slots[idx] || scene.layout.slots[scene.layout.slots.length - 1];
      const actor = new Actor(scene, key, slot.x, slot.y);
      actor.slot = idx;
      actor.briefAt = slot.briefAt || { x: slot.x - 54, y: slot.y + 12 };
      actor.seat = seatFor(scene.layout, idx);
      scene.actors.set(key, actor);
      actor.skin(look(key));
      actor.pose("sleeping"); actor.face("closed");
    }
    sortActors(scene);
    return 0;
  }

  function stationOn(scene, key, on) {
    const node = scene.svg.querySelector(`[data-station="${key}"]`);
    if (!node) return;
    scene.stationUse[key] = Math.max(0, (scene.stationUse[key] || 0) + (on ? 1 : -1));
    node.classList.toggle("on", scene.stationUse[key] > 0);
  }

  function spot(scene, actor, poseItem) {
    if (poseItem.at === "station" && stationPos(scene, poseItem.station)) return stationPos(scene, poseItem.station);
    if (poseItem.at === "table") return actor.key === "butler" ? scene.layout.table : actor.seat || scene.layout.table;
    return actor.home;
  }

  // 终态：直接把每个角色放到该在的地方，不播动画（刷新、已结束、减少动效）。
  function settle(scene, poses) {
    for (const key of Object.keys(scene.stationUse)) { scene.stationUse[key] = 1; stationOn(scene, key, false); }
    scene.itemTaken = {};
    for (const actor of scene.actors.values()) actor.heldItem = null;
    for (const [key, actor] of scene.actors) {
      const p = poses[key] || (key === "butler" ? { at: "home", face: "open" } : { at: "home", pose: "sleeping", face: "closed" });
      const where = spot(scene, actor, p);
      actor.jump(where.x, where.y);
      actor.pose(p.pose); actor.face(p.face);
      if (p.itemKey) takeTool(scene, actor, p.itemKey, p.prop);
      else actor.hold(p.prop);
      actor.bubble(p.bubble, p.red ? "red" : null);
      if (p.at === "station" && p.station) stationOn(scene, p.station, true);
      actor.station = p.at === "station" ? p.station : null;
      actor.atTable = p.at === "table";
    }
    syncTakenClasses(scene);
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
    releaseTool(scene, actor);
    if (actor.station) { stationOn(scene, actor.station, false); actor.station = null; }
    actor.atTable = false;
    actor.pose(null); actor.face("open"); actor.bubble(null);
  }

  function direct(scene, beat) {
    const butler = scene.actors.get("butler"), actor = scene.actors.get(beat.actor), handler = BEATS[beat.type];
    if (!actor || !butler || !handler) return;
    handler({ scene, beat, butler, actor, key: beat.dispatch || beat.actor, run: ms => scene.clock.run(ms), F: art().FLY });
  }

  async function briefLetter(c, b) {
    b.hold("quill"); await b.say("写信：" + c.beat.text, 2200); b.hold(null);
    fire(c.scene, "call:" + c.key);
    await fly(c.scene, c.F.letter, { x: b.x + 24, y: b.y - 50 }, { x: c.actor.x, y: c.actor.y - 46 }, 900, 110);
  }

  async function briefInPerson(c, b) {
    if (c.beat.how === "table") {
      b.atTable = true;
      await b.walk(c.scene.layout.table.x, c.scene.layout.table.y); fire(c.scene, "call:" + c.key);
      await waitFor(c.scene, "seated:" + c.key);
    } else {
      const spotAt = c.actor.briefAt || { x: c.actor.home.x - 54, y: c.actor.home.y + 12 };
      await b.walk(spotAt.x, spotAt.y); fire(c.scene, "call:" + c.key);
      await b.say(c.beat.text ? "醒醒，有活了" : "交代任务", 900);
    }
    await b.say(c.beat.text || "这件事交给你了", 2600);
  }

  async function returnHome(c, a) {
    const F = c.F, butler = c.butler, submit = c.scene.layout.submit;
    if (!c.beat.ok) {
      a.face("dizzy"); await a.say("没办成", 1100, "bad");
      await fly(c.scene, F.note, { x: a.x, y: a.y - 50 }, { x: butler.x + 24, y: butler.y - 50 }, 800, 100);
    } else if (c.beat.how === "table") {
      a.atTable = true;
      a.face("happy"); a.hold("scroll"); await a.walk(a.seat.x, a.seat.y); a.hold(null); await a.say("说完了", 1200);
    } else if (c.beat.how === "letter") {
      a.face("happy"); await fly(c.scene, F.letter, { x: a.x, y: a.y - 50 }, { x: butler.x + 24, y: butler.y - 50 }, 800, 100);
    } else {
      a.face("happy"); a.hold("scroll"); await a.walk(submit.x, submit.y); a.hold(null);
      await fly(c.scene, F.scroll, { x: a.x - 10, y: a.y - 44 }, { x: butler.x + 24, y: butler.y - 36 }, 450, 24);
    }
  }

  const BEATS = {
    butler_tool: c => c.butler.do(async b => {
      const st = c.beat.station && stationPos(c.scene, c.beat.station);
      if (st) {
        leave(c.scene, b);
        const n = c.scene.stationUse[c.beat.station] || 0, off = [0, -24, 24][n % 3];
        await b.walk(st.x + off, st.y);
        stationOn(c.scene, c.beat.station, true);
        b.station = c.beat.station;
        takeTool(c.scene, b, c.beat.itemKey, c.beat.prop);
        b.pose("working");
        await b.say(SHORT[c.beat.tool] || clip(c.beat.text, 10), 1300);
        leave(c.scene, b);
      } else {
        takeTool(c.scene, b, c.beat.itemKey, c.beat.prop);
        await b.say(SHORT[c.beat.tool] || clip(c.beat.text, 10), 1300);
        releaseTool(c.scene, b);
      }
    }),
    brief: c => {
      c.butler.do(async b => {
        leave(c.scene, b);
        if (c.beat.how === "letter") { await b.walk(c.scene.layout.desk.x, c.scene.layout.desk.y); await briefLetter(c, b); }
        else await briefInPerson(c, b);
        fire(c.scene, "briefed:" + c.key);
        if (c.beat.how !== "letter") { b.atTable = false; await b.walk(c.scene.layout.desk.x, c.scene.layout.desk.y); }
        b.hold("tea"); b.face("happy");
      });
      c.actor.do(async a => {
        await waitFor(c.scene, "call:" + c.key);
        a.pose(null); a.face("open");
        if (c.beat.how === "table") { a.atTable = true; await a.walk(a.seat.x, a.seat.y); fire(c.scene, "seated:" + c.key); }
        await waitFor(c.scene, "briefed:" + c.key);
        if (c.beat.how === "letter") { a.hold("letter"); await a.say("读信…", 1200); a.hold(null); }
        a.face("happy"); await a.say("收到！我这就去办", 1400); a.face("open");
        a.bubble("收到，开始办…");
      });
    },
    wake: c => c.actor.do(async a => { a.pose(null); a.face("open"); }),
    work: c => c.actor.do(async a => {
      leave(c.scene, a);
      const s = stationPos(c.scene, c.beat.station), n = c.scene.stationUse[c.beat.station] || 0, off = [0, 28, -28][n % 3];
      await a.walk(s.x + off, s.y);
      stationOn(c.scene, c.beat.station, true); a.station = c.beat.station;
      if (c.beat.station === "bed") { a.pose("sleeping"); a.face("closed"); }
      else {
        a.face("open"); a.pose("working");
        if (c.beat.prop || c.beat.itemKey) takeTool(c.scene, a, c.beat.itemKey, c.beat.prop);
      }
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
      leave(c.scene, a);
      await returnHome(c, a);
      fire(c.scene, "returned:" + c.key);
      if (c.beat.how !== "table") { await a.walk(a.home.x, a.home.y); if (c.beat.ok) { a.pose("sleeping"); a.face("closed"); } }
    }),
    check: c => c.butler.do(async b => {
      await waitFor(c.scene, "returned:" + c.key);
      leave(c.scene, b);
      await b.walk(c.scene.layout.desk.x, c.scene.layout.desk.y);
      const passed = c.beat.checks ? Object.values(c.beat.checks).filter(Boolean).length : 5;
      b.hold("magnifier"); b.face("open");
      if (c.beat.ok) for (let i = 1; i <= passed; i++) { b.bubble("验收 " + "✓".repeat(i)); await c.run(220); }
      else { b.bubble("验收 ✗", "bad"); await c.run(900); }
      await c.run(400); b.bubble(null); b.hold(null);
    }),
    wait: c => c.butler.do(async b => { leave(c.scene, b); await b.walk(c.scene.layout.desk.x, c.scene.layout.desk.y); b.hold("tea"); b.face("happy"); }),
    ruling: c => c.butler.do(async b => { leave(c.scene, b); b.atTable = true; await b.walk(c.scene.layout.table.x, c.scene.layout.table.y); b.bubble("等你裁决", "red"); }),
    reply: c => c.butler.do(async b => {
      leave(c.scene, b);
      await b.walk(c.scene.layout.desk.x, c.scene.layout.desk.y);
      b.face("happy"); b.hold("scroll"); await b.say("这就回话", 900); b.hold(null);
      await fly(c.scene, c.F.scroll, { x: b.x + 20, y: b.y - 50 }, c.scene.layout.gate, 800, 50);
      b.bubble("已回话，看下方");
    }),
  };

  // ---------- 建筑背景与家具（三套尺度各自独立构图） ----------
  function shelfMarkup(theme) {
    const cols = ["#f4a9a3", "#f2c14e", "#7fa99b", "#a4c9e3", "#c9b8e6", "#fffaf0", "#5e9c76"], ink = art().INK;
    let m = `<rect class="ik" x="-50" y="-130" width="100" height="130" rx="6" fill="${theme.woodDark}"/>`, k = 0;
    for (let r = 0; r < 3; r++) {
      const y = -123 + r * 41;
      m += `<rect x="-43" y="${y}" width="86" height="34" rx="2" fill="#5e3e29"/>`;
      let x = -40;
      while (x < 33) {
        const w = 6 + ((k * 5) % 4) * 1.5, h = 21 + ((k * 7) % 4) * 3;
        const pick = r === 0 && k === 4 ? " pick" : "";
        const itemAttr = r === 0 && k === 2 ? ' data-tool-item="contacts"'
          : r === 0 && k === 4 ? ' data-tool-item="roster"'
          : r === 1 && k === 11 ? ' data-tool-item="knowledge"'
          : "";
        m += `<rect class="tool-prop${pick}"${itemAttr} x="${x}" y="${y + 34 - h}" width="${w}" height="${h}" rx="1.5" fill="${cols[k % cols.length]}" stroke="${ink}" stroke-width="1.4"/>`;
        x += w + 1.5; k++;
      }
    }
    return m;
  }

  function sceneMarkup(theme, layout) {
    if (layout.themeId === "siheyuan") return siheyuanMarkup(theme, layout);
    if (layout.themeId === "palace") return palaceMarkup(theme, layout);
    return courtyardMarkup(theme, layout);
  }

  // 风格 1：玄圃小院（单间温馨工作室）
  function courtyardMarkup(theme, layout) {
    const ink = art().INK;
    const plankYs = [];
    for (let y = 244; y < layout.height; y += 54) plankYs.push(y);
    const planks = plankYs.map(y => `<path d="M0,${y} H900" stroke="${theme.plank}" stroke-width="2"/>`).join("");
    const cushions = layout.slots.map(s => `<ellipse cx="${s.x}" cy="${s.y + 4}" rx="33" ry="9.5" fill="${theme.cushionFill}" stroke="${theme.cushionStroke}" stroke-width="2"/>`).join("");
    const wLabel = (x, y, t) => `<text x="${x}" y="${y}" text-anchor="middle" font-size="12.5" fill="${theme.labelColor}">${t}</text>`;
    const rugY = layout.desk.y + 50;
    return `
  <rect width="900" height="200" fill="${theme.wall}"/><rect y="166" width="900" height="34" fill="${theme.wainscot}"/><path d="M0,166 H900" stroke="${theme.wainscotLine}" stroke-width="2"/>
  <rect y="200" width="900" height="${layout.height - 200}" fill="${theme.floor}"/>${planks}<path class="ik" d="M0,200 H900" fill="none"/>
  <g transform="translate(80,98)"><circle r="54" fill="#bfe0d0" stroke="${theme.woodDark}" stroke-width="7"/><circle r="50.5" fill="none" stroke="${ink}" stroke-width="2"/><path d="M-30,40 q6,-44 -4,-78 M-18,44 q10,-40 4,-70" fill="none" stroke="#5e9c76" stroke-width="3" stroke-linecap="round"/><path d="M-33,-8 q-14,-4 -16,-16 q13,2 16,16Z M-30,-24 q10,-10 22,-8 q-8,10 -22,8Z M-14,6 q12,-8 22,-2 q-10,8 -22,2Z" fill="#5e9c76"/><circle cx="14" cy="6" r="13" fill="${ink}"/><path d="M-9,50 C-9,26 37,26 37,48 Q14,58 -9,50Z" fill="${ink}"/></g>
  ${wLabel(80, 180, theme.windowLabel)}
  <g transform="translate(868,0)"><path class="ik" d="M0,0 V24" fill="none"/><ellipse class="ik" cx="0" cy="44" rx="15" ry="20" fill="#f2c14e"/><path class="ik" d="M-9,27 h18 M-9,61 h18 M0,64 v12" fill="none"/></g>
  <g transform="translate(520,28)"><rect class="ik" x="-64" y="-16" width="128" height="32" rx="5" fill="${theme.plaqueFill}"/><text x="3" y="7" text-anchor="middle" font-size="19" letter-spacing="5" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">${theme.title}</text></g>
  <ellipse cx="170" cy="${rugY}" rx="152" ry="50" fill="${theme.rugFill}" stroke="${theme.rugStroke}" stroke-width="2"/><ellipse cx="170" cy="${rugY}" rx="138" ry="41" fill="none" stroke="${theme.rugStroke}" stroke-width="2" stroke-dasharray="3 7"/>
  ${cushions}
  ${wLabel(345, 88, theme.stations.cabinet)}${wLabel(455, 78, theme.stations.books)}${wLabel(580, 90, theme.stations.computer)}${wLabel(715, 52, theme.stations.tv)}${wLabel(835, 124, theme.stations.bed)}`;
  }

  // 风格 2：玄圃四合院（正房、东西各厢房、抄手游廊、中庭石板院心、南倒座房与垂花门）
  function siheyuanMarkup(theme, layout) {
    const ink = art().INK, W = layout.width, H = layout.height;
    const courtTop = 216, courtBot = H - 156, courtH = courtBot - courtTop;
    const midY = Math.round((courtTop + courtBot) / 2);
    const roofCap = (x, y, w, label, fill = "#536169") =>
      `<g><path class="ik" d="M${x - 6},${y + 22} L${x + 12},${y} H${x + w - 12} L${x + w + 6},${y + 22} Z" fill="${fill}"/><line x1="${x + 10}" y1="${y + 8}" x2="${x + w - 10}" y2="${y + 8}" stroke="#7f8f97" stroke-width="1.5"/>${label ? `<rect x="${x + w / 2 - 42}" y="${y + 3}" width="84" height="17" rx="3" fill="${theme.plaqueFill}" stroke="${ink}" stroke-width="1.2"/><text x="${x + w / 2}" y="${y + 15}" text-anchor="middle" font-size="11.5" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">${label}</text>` : ""}</g>`;
    const wings = layout.wingRooms.map((r, i) => {
      const slot = layout.slots[i];
      const doorX = r.side === "west" ? r.x + r.w - 3 : r.x - 3;
      const winX = r.side === "west" ? r.x + 12 : r.x + r.w - 42;
      return `<g>
        <rect class="ik" x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}" rx="5" fill="#ebe3d5"/>
        <rect x="${r.x + 4}" y="${r.y + 24}" width="${r.w - 8}" height="${r.h - 28}" fill="#f3ecdf"/>
        ${roofCap(r.x, r.y, r.w, r.name, "#5d6b73")}
        <rect x="${winX}" y="${r.y + 34}" width="30" height="22" rx="2" fill="#d3e3dc" stroke="${theme.woodDark}" stroke-width="1.8"/>
        <path d="M${winX + 15},${r.y + 34} v22 M${winX},${r.y + 45} h30" stroke="${theme.woodDark}" stroke-width="1.2"/>
        <rect x="${doorX}" y="${r.y + 52}" width="6" height="36" rx="2" fill="#efe6d5"/>
        <ellipse cx="${slot.x}" cy="${slot.y + 4}" rx="28" ry="8" fill="${theme.cushionFill}" stroke="${theme.cushionStroke}" stroke-width="1.8"/>
      </g>`;
    }).join("");
    const tree = (x, y) => `<g transform="translate(${x},${y})"><ellipse cx="0" cy="14" rx="18" ry="6" fill="#9fb1b5" opacity=".45"/><rect x="-4" y="-6" width="8" height="20" rx="2" fill="#7c563a"/><circle cx="-10" cy="-12" r="14" fill="#689f78" stroke="${ink}" stroke-width="1.6"/><circle cx="10" cy="-10" r="13" fill="#588e68" stroke="${ink}" stroke-width="1.6"/><circle cx="0" cy="-22" r="14" fill="#7ab389" stroke="${ink}" stroke-width="1.6"/><circle cx="-6" cy="-18" r="2.8" fill="#e05a47"/><circle cx="8" cy="-14" r="2.6" fill="#e05a47"/></g>`;
    const southTop = layout.southTop;
    return `
  <!-- 院墙与青砖外廊 -->
  <rect width="${W}" height="${H}" fill="#c6cfd3"/>
  <rect class="ik" x="18" y="18" width="${W - 36}" height="${H - 36}" rx="10" fill="#d8dfe2"/>
  <!-- 抄手游廊 -->
  <rect x="242" y="${courtTop - 10}" width="596" height="${courtH + 20}" rx="8" fill="#e4d6c2" stroke="${theme.woodDark}" stroke-width="2.4"/>
  <!-- 中庭石板院心 -->
  <rect x="268" y="${courtTop + 6}" width="544" height="${courtH - 12}" rx="8" fill="#d9e0e2" stroke="#9aa8ae" stroke-width="2"/>
  <path d="M540,${courtTop + 6} V${courtBot - 6} M268,${midY} H812" stroke="#b5c0c4" stroke-width="18" opacity=".55"/>
  <path d="M540,${courtTop + 6} V${courtBot - 6} M268,${midY} H812" stroke="#eef2f3" stroke-width="2" stroke-dasharray="10 8"/>
  <circle cx="540" cy="${layout.table.y - 26}" r="70" fill="#e6ecee" stroke="#a6b4ba" stroke-width="2" stroke-dasharray="6 5"/>
  ${tree(304, courtBot - 36)}${tree(776, courtBot - 36)}
  <!-- 北·正房（总管厅）与东西耳房 -->
  <rect class="ik" x="40" y="34" width="286" height="164" rx="6" fill="#ebe3d5"/>
  <rect class="ik" x="342" y="28" width="396" height="174" rx="6" fill="#f3ebd9"/>
  <rect class="ik" x="754" y="34" width="286" height="164" rx="6" fill="#ebe3d5"/>
  ${roofCap(40, 34, 286, theme.stations.cabinet, "#54636b")}
  ${roofCap(342, 26, 396, "", "#46555e")}
  ${roofCap(754, 34, 286, theme.stations.books, "#54636b")}
  <g transform="translate(540,42)"><rect class="ik" x="-78" y="-15" width="156" height="30" rx="4" fill="${theme.plaqueFill}"/><text x="2" y="6" text-anchor="middle" font-size="18" letter-spacing="4" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">${theme.title}</text></g>
  <ellipse cx="540" cy="172" rx="126" ry="30" fill="${theme.rugFill}" stroke="${theme.rugStroke}" stroke-width="1.8"/>
  <!-- 庭院北角：歇息轩与榜文壁 -->
  <rect x="284" y="226" width="148" height="132" rx="8" fill="#e9e1d2" stroke="${theme.woodDark}" stroke-width="1.6" stroke-dasharray="5 4"/>
  <text x="358" y="242" text-anchor="middle" font-size="11.5" fill="${theme.labelColor}">${theme.stations.bed}</text>
  <rect x="648" y="226" width="148" height="132" rx="8" fill="#e9e1d2" stroke="${theme.woodDark}" stroke-width="1.6" stroke-dasharray="5 4"/>
  <text x="722" y="242" text-anchor="middle" font-size="11.5" fill="${theme.labelColor}">${theme.stations.tv}</text>
  <!-- 东西厢房（自动按匠人数增建） -->
  ${wings}
  <!-- 南·倒座房与垂花门 -->
  <rect class="ik" x="40" y="${southTop}" width="364" height="122" rx="6" fill="#ebe3d5"/>
  <rect class="ik" x="676" y="${southTop}" width="364" height="122" rx="6" fill="#ebe3d5"/>
  ${roofCap(40, southTop, 364, theme.benchLabel, "#54636b")}
  ${roofCap(676, southTop, 364, theme.stations.computer, "#54636b")}
  <g transform="translate(540,${southTop + 62})">
    <rect class="ik" x="-92" y="-44" width="184" height="88" rx="6" fill="#f0e5d2"/>
    <path class="ik" d="M-104,-24 L-84,-52 H84 L104,-24 Z" fill="#8f3f36"/>
    <rect x="-40" y="-46" width="80" height="19" rx="3" fill="${theme.plaqueFill}" stroke="${ink}" stroke-width="1.2"/>
    <text x="0" y="-33" text-anchor="middle" font-size="12" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">垂花门</text>
    <circle cx="0" cy="4" r="10" fill="${ink}"/><path d="M-16,32 C-16,14 16,14 16,32 Z" fill="${ink}"/>
    <text x="0" y="42" text-anchor="middle" font-size="11.5" fill="${theme.labelColor}">${theme.windowLabel}</text>
  </g>`;
  }

  // 风格 3：玄圃紫禁城（故宫：四角楼 + 中轴御道 + 太和殿/中和殿/养心轩 + 东西六宫独立院落 + 外朝诸署 + 内金水河 + 午门）
  function palaceMarkup(theme, layout) {
    const ink = art().INK, W = layout.width, H = layout.height;
    const riverY = 602 + layout.extraH;
    const southTop = 642 + layout.extraH;
    const goldRoof = (x, y, w, h, label, plaqueW = 74) =>
      `<g><path class="ik" d="M${x - 7},${y + h} L${x + 12},${y} H${x + w - 12} L${x + w + 7},${y + h} Z" fill="#e5b33d"/>
      <line x1="${x + 12}" y1="${y + h * 0.45}" x2="${x + w - 12}" y2="${y + h * 0.45}" stroke="#c28b1e" stroke-width="1.5"/>
      <circle cx="${x + 10}" cy="${y + 1}" r="3" fill="#f7d47c" stroke="${ink}" stroke-width="1.1"/>
      <circle cx="${x + w - 10}" cy="${y + 1}" r="3" fill="#f7d47c" stroke="${ink}" stroke-width="1.1"/>
      ${label ? `<rect x="${x + w / 2 - plaqueW / 2}" y="${y + 3}" width="${plaqueW}" height="17" rx="3" fill="${theme.plaqueFill}" stroke="#d9a94b" stroke-width="1.2"/><text x="${x + w / 2}" y="${y + 15}" text-anchor="middle" font-size="11.5" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">${label}</text>` : ""}</g>`;
    const turret = (x, y) =>
      `<g transform="translate(${x},${y})"><rect class="ik" x="-16" y="-12" width="32" height="24" rx="3" fill="#9c2c24"/><path class="ik" d="M-20,-10 L0,-26 L20,-10 Z" fill="#e5b33d"/></g>`;
    const compounds = layout.compounds.map((box, i) => {
      const slot = layout.slots[i];
      const gateX = box.side === "west" ? box.x + box.w - 4 : box.x - 4;
      return `<g>
        <!-- 独立宫院围墙 -->
        <rect class="ik" x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" rx="5" fill="#f3e8d2"/>
        <rect x="${box.x + 3}" y="${box.y + 3}" width="${box.w - 6}" height="${box.h - 6}" rx="3" fill="none" stroke="#9e2f26" stroke-width="3"/>
        <rect x="${box.x + 8}" y="${box.y + 24}" width="${box.w - 16}" height="${box.h - 30}" rx="2" fill="#f8f0df"/>
        ${goldRoof(box.x + 4, box.y + 2, box.w - 8, 22, box.name, 70)}
        <!-- 宫院门 -->
        <rect x="${gateX}" y="${box.y + 72}" width="8" height="32" rx="2" fill="#f8f0df"/>
        <ellipse cx="${slot.x}" cy="${slot.y + 4}" rx="25" ry="7.5" fill="${theme.cushionFill}" stroke="${theme.cushionStroke}" stroke-width="1.6"/>
      </g>`;
    }).join("");
    const bridge = x => `<g transform="translate(${x},${riverY})"><rect class="ik" x="-26" y="-12" width="52" height="32" rx="4" fill="#f6f1e6"/><path d="M-22,-8 H22 M-22,16 H22" stroke="#d4c7ad" stroke-width="2.2"/></g>`;
    return `
  <!-- 紫禁城外廓与宫道 -->
  <rect width="${W}" height="${H}" fill="#ded3bc"/>
  <rect class="ik" x="16" y="16" width="${W - 32}" height="${H - 32}" rx="8" fill="#eadfcd"/>
  <rect x="20" y="20" width="${W - 40}" height="${H - 40}" rx="6" fill="none" stroke="#9b2b23" stroke-width="5"/>
  <!-- 东西宫巷与南北中轴御道 -->
  <rect x="424" y="28" width="472" height="${H - 56}" fill="#f1e8d6" stroke="#d6c5a3" stroke-width="1.8"/>
  <rect x="626" y="34" width="68" height="${H - 68}" fill="#f8f3e8" stroke="#d9c69e" stroke-width="1.6"/>
  <path d="M660,34 V${H - 34}" stroke="#e3d2af" stroke-width="2" stroke-dasharray="10 8"/>
  <!-- 东西六宫区标识 -->
  <text x="214" y="191" text-anchor="middle" font-size="11.5" letter-spacing="4" fill="#8b3a2e">西 六 宫</text>
  <text x="1106" y="191" text-anchor="middle" font-size="11.5" letter-spacing="4" fill="#8b3a2e">东 六 宫</text>
  <!-- 北·外朝两阁与养心轩 -->
  <rect class="ik" x="46" y="36" width="336" height="138" rx="6" fill="#f3e9d6"/>
  ${goldRoof(46, 36, 336, 26, theme.stations.cabinet, 82)}
  <rect class="ik" x="454" y="32" width="412" height="142" rx="6" fill="#f5ecd9"/>
  ${goldRoof(454, 30, 412, 28, "", 120)}
  <g transform="translate(660,44)"><rect class="ik" x="-88" y="-14" width="176" height="28" rx="4" fill="${theme.plaqueFill}" stroke="#d9a94b" stroke-width="1.5"/><text x="2" y="6" text-anchor="middle" font-size="18" letter-spacing="4" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">${theme.title}</text></g>
  <text x="660" y="76" text-anchor="middle" font-size="12" fill="${theme.labelColor}">${theme.stations.bed}</text>
  <rect class="ik" x="938" y="36" width="336" height="138" rx="6" fill="#f3e9d6"/>
  ${goldRoof(938, 36, 336, 26, theme.stations.books, 82)}
  <!-- 中轴·太和殿（内务府） -->
  <g>
    <rect x="454" y="200" width="412" height="168" rx="8" fill="#f7f2e7" stroke="#d4c4a2" stroke-width="2.2"/>
    <rect x="468" y="212" width="384" height="144" rx="6" fill="#f2e6cf" stroke="#b83c30" stroke-width="2.2"/>
    ${goldRoof(464, 206, 392, 28, "太和殿", 88)}
    <ellipse cx="660" cy="316" rx="132" ry="30" fill="${theme.rugFill}" stroke="${theme.rugStroke}" stroke-width="1.8"/>
  </g>
  <!-- 中轴·中和殿（议政台） -->
  <g>
    <rect x="478" y="${layout.table.y - 102}" width="364" height="140" rx="12" fill="#f7f2e7" stroke="#d4c4a2" stroke-width="2.2"/>
    <circle cx="660" cy="${layout.table.y - 30}" r="78" fill="#f2e7d2" stroke="#d4a442" stroke-width="2" stroke-dasharray="6 5"/>
    <text x="660" y="${layout.table.y - 84}" text-anchor="middle" font-size="12" fill="${theme.labelColor}" font-family="Kaiti SC,STKaiti,KaiTi,serif">中和殿·议政广场</text>
  </g>
  <!-- 东西六宫（独立宫院，超出 12 位自动向外东/西路扩展） -->
  ${compounds}
  <!-- 内金水河与汉白玉桥 -->
  <path d="M26,${riverY} Q330,${riverY - 12} 660,${riverY} T1294,${riverY}" fill="none" stroke="#85b7c2" stroke-width="22"/>
  <path d="M26,${riverY} Q330,${riverY - 12} 660,${riverY} T1294,${riverY}" fill="none" stroke="#cbe3e8" stroke-width="4" stroke-dasharray="14 10"/>
  ${bridge(214)}${bridge(660)}${bridge(1106)}
  <!-- 南·外朝三署（军机处 / 造办处 / 钦天监） -->
  <rect class="ik" x="42" y="${southTop}" width="206" height="142" rx="6" fill="#f3e9d6"/>
  ${goldRoof(42, southTop, 206, 24, theme.stations.computer, 74)}
  <rect class="ik" x="262" y="${southTop}" width="164" height="142" rx="6" fill="#f3e9d6"/>
  ${goldRoof(262, southTop, 164, 24, theme.benchLabel, 74)}
  <rect class="ik" x="938" y="${southTop}" width="336" height="142" rx="6" fill="#f3e9d6"/>
  ${goldRoof(938, southTop, 336, 24, theme.stations.tv, 82)}
  <!-- 南·午门（用户来信与回话之门） -->
  <g transform="translate(660,${H - 64})">
    <rect class="ik" x="-164" y="-36" width="328" height="72" rx="6" fill="#a33329"/>
    <path class="ik" d="M-176,-14 L-152,-44 H152 L176,-14 Z" fill="#e5b33d"/>
    <rect x="-48" y="-38" width="96" height="20" rx="3" fill="${theme.plaqueFill}" stroke="#d9a94b" stroke-width="1.2"/>
    <text x="0" y="-24" text-anchor="middle" font-size="13" letter-spacing="3" fill="${theme.plaqueText}" font-family="Kaiti SC,STKaiti,KaiTi,serif">午 门</text>
    <path d="M-24,36 V6 A24,20 0 0 1 24,6 V36 Z" fill="#f3ead8" stroke="${ink}" stroke-width="1.8"/>
    <circle cx="0" cy="14" r="7.5" fill="${ink}"/><path d="M-12,35 C-12,22 12,22 12,35 Z" fill="${ink}"/>
    <text x="0" y="48" text-anchor="middle" font-size="11.5" fill="${theme.labelColor}">${theme.windowLabel}</text>
  </g>
  <!-- 四角楼 -->
  ${turret(28, 30)}${turret(W - 28, 30)}${turret(28, H - 24)}${turret(W - 28, H - 24)}`;
  }

  function furniture(theme, layout) {
    const ink = art().INK;
    const s = layout.furnitureScale || 1;
    const drawer = (y, mid) => `<g>${mid ? `<g class="peek"><rect class="ik" x="-22" y="${y - 11}" width="14" height="14" rx="2" fill="#f2c14e"/><rect class="ik" x="-4" y="${y - 14}" width="14" height="17" rx="2" fill="#fffaf0"/><rect class="ik" x="13" y="${y - 9}" width="12" height="12" rx="2" fill="#f4a9a3"/></g>` : ""}<rect class="ik" x="-30" y="${y}" width="60" height="31" rx="4" fill="${theme.drawerFill}"/><rect x="-9" y="${y + 12}" width="18" height="6" rx="3" fill="${ink}"/></g>`;
    const name = (t, y = 16) => `<text x="0" y="${y}" text-anchor="middle" font-size="12.5" fill="${theme.labelColor}">${t}</text>`;
    const st = layout.stations;
    const isSingle = layout.themeId === "courtyard";
    const isSiheyuan = layout.themeId === "siheyuan";
    const cabY = isSingle ? 215 : isSiheyuan ? 176 : 162;
    const booksY = isSingle ? 215 : isSiheyuan ? 178 : 166;
    const compY = isSingle ? 215 : isSiheyuan ? layout.southTop + 112 : layout.southTop + 124;
    const tvY = isSingle ? 215 : isSiheyuan ? 352 : layout.southTop + 134;
    const bedY = isSingle ? 215 : isSiheyuan ? 304 : 132;
    const deskY = layout.desk.y + Math.round(32 * s);
    const tableY = layout.table.y - Math.round(41 * s);
    const benchY = isSingle ? layout.bench.y + 33 : isSiheyuan ? layout.southTop + 98 : layout.southTop + 108;
    return [
      { key: "cabinet", x: st.cabinet.x, y: cabY, scale: s, markup: `<rect class="ik" x="-37" y="-120" width="74" height="120" rx="7" fill="${theme.cabinetFill}"/>${drawer(-112)}${drawer(-75, true)}${drawer(-38)}<g class="tool-prop" data-tool-item="folder" transform="translate(-18,-126)"><path d="M-10,-4 h6 l2,2.5 h11 v9.5 h-19 Z" fill="#e8c56a" stroke="${ink}" stroke-width="1.5"/></g><g class="tool-prop" data-tool-item="stamp" transform="translate(18,-124)"><rect x="-7" y="0" width="14" height="4.5" rx="1.5" fill="#d2432f" stroke="${ink}" stroke-width="1.4"/><path d="M-3,0 L-2.2,-7 a2.2,2.2 0 0 1 4.4,0 L3,0 Z" fill="${theme.woodDark}" stroke="${ink}" stroke-width="1.4"/></g>` },
      { key: "books", x: st.books.x, y: booksY, scale: s, markup: shelfMarkup(theme) },
      { key: "computer", x: st.computer.x, y: compY, scale: s, markup: `<rect class="ik" x="-37" y="-118" width="74" height="52" rx="6" fill="${ink}"/><rect class="scr" x="-31" y="-112" width="62" height="40" rx="3" fill="#9fb8ae"/><g><rect class="ln" x="-25" y="-106" width="30" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-98" width="46" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-90" width="22" height="4" rx="2" fill="#2f6f5e"/><rect class="ln" x="-25" y="-82" width="38" height="4" rx="2" fill="#2f6f5e"/></g><path class="ik" d="M-7,-66 h14 l3,8 h-20Z" fill="#5a6072"/><rect class="ik" x="-61" y="-58" width="122" height="11" rx="4" fill="${theme.woodLight}"/><rect class="ik" x="-55" y="-47" width="9" height="47" fill="${theme.woodDark}"/><rect class="ik" x="46" y="-47" width="9" height="47" fill="${theme.woodDark}"/><rect class="tool-prop" data-tool-item="terminal" x="-21" y="-65" width="42" height="7" rx="2" fill="#fffaf0" stroke="${ink}" stroke-width="1.4"/><g class="tool-prop" data-tool-item="compass" transform="translate(46,-67)"><circle r="8" fill="#fffaf0" stroke="${ink}" stroke-width="1.6"/><path d="M0,-5 L2.4,0 L0,5 L-2.4,0 Z" fill="#d2432f"/></g>` },
      { key: "tv", x: st.tv.x, y: tvY, scale: s, markup: `<rect class="ik" x="-55" y="-156" width="110" height="72" rx="7" fill="${ink}"/><rect class="scr" x="-49" y="-150" width="98" height="60" rx="3.5" fill="#32414e"/><rect class="ik" x="-47" y="-42" width="94" height="42" rx="5" fill="${theme.woodLight}"/><path class="ik" d="M0,-42 V0" fill="none"/><circle cx="-7" cy="-21" r="2.4" fill="${ink}"/><circle cx="7" cy="-21" r="2.4" fill="${ink}"/><g class="tool-prop" data-tool-item="sticky" transform="translate(32,-52) rotate(-5)"><rect x="-8" y="-8" width="16" height="16" rx="2" fill="#f6dc7a" stroke="${ink}" stroke-width="1.5"/><path d="M-4,-2 h8 M-4,2.5 h5" stroke="${ink}" stroke-width="1.3" stroke-linecap="round"/></g>` },
      { key: "bed", x: st.bed.x, y: bedY, scale: s, markup: `<rect class="ik" x="-49" y="-82" width="98" height="66" rx="13" fill="${theme.woodDark}"/><rect class="ik" x="-45" y="-32" width="90" height="72" rx="8" fill="#fffaf0"/><ellipse class="ik" cx="0" cy="-22" rx="29" ry="10.5" fill="#ffffff"/>` },
      { key: "blanket", x: st.bed.x, y: bedY + Math.round(47 * s), scale: s, markup: `<g transform="translate(0,-47)"><rect class="ik" x="-47" y="-8" width="94" height="52" rx="9" fill="${theme.cushionFill}"/><path d="M-47,5 h94" stroke="#fff" stroke-width="3" opacity=".7"/><rect class="ik" x="-50" y="38" width="100" height="12" rx="5" fill="${theme.woodDark}"/></g>` },
      { key: "desk", x: layout.desk.x, y: deskY, scale: s, markup: `<rect class="ik" x="-80" y="-36" width="160" height="36" rx="5" fill="${theme.woodDark}"/><rect class="ik" x="-87" y="-48" width="174" height="13" rx="5" fill="${theme.woodLight}"/><g transform="translate(-60,-48)"><g class="deskphone"><rect class="ik" x="-14" y="-11" width="28" height="11" rx="3.5" fill="${ink}"/><path d="M-16,-17 q16,-10 32,0" fill="none" stroke="${ink}" stroke-width="6" stroke-linecap="round"/><circle cx="0" cy="-5.5" r="2.6" fill="#f2c14e"/></g></g><g transform="translate(62,-48)"><path class="ik" d="M-10,0 a10,10 0 0 1 20,0Z" fill="#f2c14e"/><rect x="-12" y="-1.5" width="24" height="3.5" rx="1.7" fill="${ink}"/></g><text x="0" y="-13" text-anchor="middle" font-size="12.5" fill="${theme.plaqueText}">${theme.deskLabel}</text>` },
      { key: "table", x: layout.table.x, y: tableY, scale: s, markup: `<path class="ik" d="M-8,-12 h16 v10 h12 v5 h-40 v-5 h12Z" fill="${theme.woodDark}"/><ellipse class="ik" cx="0" cy="-30" rx="64" ry="20" fill="${theme.woodLight}"/><g transform="translate(0,-38)"><ellipse class="ik" rx="12" ry="8.5" fill="${theme.cabinetFill}"/><circle class="ik" cx="0" cy="-9" r="2.6" fill="${theme.cabinetFill}"/></g>${name(theme.tableLabel)}` },
      { key: "bench", x: layout.bench.x, y: benchY, scale: s, markup: `<rect class="ik" x="-62" y="-30" width="10" height="30" fill="${theme.woodDark}"/><rect class="ik" x="52" y="-30" width="10" height="30" fill="${theme.woodDark}"/><rect class="ik" x="-70" y="-42" width="140" height="13" rx="4" fill="${theme.woodLight}"/><rect class="ik" x="-54" y="-57" width="20" height="15" rx="2" fill="#5a6072"/><g class="gear"><circle class="ik" cx="0" cy="-53" r="10" fill="#f2c14e" stroke-dasharray="5 3.4"/><circle cx="0" cy="-53" r="3.4" fill="${ink}"/></g><g class="tool-prop" data-tool-item="wrench" transform="translate(-26,-52) rotate(-25)"><path d="M-2,-5 h4 v14 h-4 Z" fill="#b4c4d0" stroke="${ink}" stroke-width="1.4"/><circle cx="0" cy="-7" r="4.5" fill="#b4c4d0" stroke="${ink}" stroke-width="1.4"/></g><rect class="ik" x="30" y="-59" width="24" height="17" rx="2" fill="${theme.cabinetFill}"/>${name(theme.benchLabel)}` },
    ];
  }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.scene = {
    create,
    clip,
    THEMES,
    THEME_ORDER,
    loadTheme,
    saveTheme,
    computeLayout,
  };
})();
