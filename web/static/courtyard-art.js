// 小院角色画法（R7）：样子 × 颜色 × 配饰拼成一个角色。只拼常量 SVG 片段，不含任何用户文本；
// 名字、气泡等文字一律由场景用 textContent 写入。
"use strict";

(function () {
  const INK = "#2b2f3a";
  const S = `stroke="${INK}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"`;
  const COLORS = {
    white: { n: "米白", b: "#f7f2e8", d: "#d9cfbd" }, apricot: { n: "杏", b: "#f2b872", d: "#cf8f45" },
    peach: { n: "桃", b: "#f4a9a3", d: "#d47f78" }, yellow: { n: "鹅黄", b: "#f6dc7a", d: "#d6b548" },
    green: { n: "竹青", b: "#a3d3b0", d: "#74ab85" }, blue: { n: "天青", b: "#a4c9e3", d: "#74a2c2" },
    purple: { n: "藕紫", b: "#c9b8e6", d: "#9f8ac4" },
  };
  const SPECIES = { fox: "狐", cat: "猫", rabbit: "兔", bear: "熊", chick: "小鸡", sprout: "芽" };
  const ACCS = { none: "不戴", bowtie: "领结", monocle: "单片镜", glasses: "眼镜", headset: "耳机", apron: "围裙", hat: "草帽" };
  // 头顶高度：气泡放在这之上
  const TOP = { rabbit: 120, fox: 108, sprout: 108, chick: 104 };
  const BUTLER = { species: "fox", color: "apricot", acc: "bowtie", name: "管家" };
  const STORE_KEY = "xuanpu-courtyard-looks";
  let artisanNames = {};

  function speciesMarkup(species, b, d) {
    if (species === "cat") return [`<path d="M-23,-40 L-20,-63 L-4,-50Z" fill="${b}" ${S}/><path d="M23,-40 L20,-63 L4,-50Z" fill="${b}" ${S}/><path d="M-18.5,-48 L-17.5,-57 L-10,-51Z" fill="#f7b5ae"/><path d="M18.5,-48 L17.5,-57 L10,-51Z" fill="#f7b5ae"/>`, ""];
    if (species === "rabbit") return [`<ellipse cx="-11" cy="-64" rx="6.5" ry="17" transform="rotate(-9 -11 -64)" fill="${b}" ${S}/><ellipse cx="11" cy="-64" rx="6.5" ry="17" transform="rotate(9 11 -64)" fill="${b}" ${S}/><ellipse cx="-11" cy="-65" rx="2.6" ry="10" transform="rotate(-9 -11 -64)" fill="#f7b5ae"/><ellipse cx="11" cy="-65" rx="2.6" ry="10" transform="rotate(9 11 -64)" fill="#f7b5ae"/>`, ""];
    if (species === "bear") return [`<circle cx="-18" cy="-50" r="9" fill="${b}" ${S}/><circle cx="18" cy="-50" r="9" fill="${b}" ${S}/><circle cx="-18" cy="-50" r="4" fill="${d}"/><circle cx="18" cy="-50" r="4" fill="${d}"/>`, ""];
    if (species === "fox") return [`<path d="M20,-6 C42,-2 48,-24 37,-36 C39,-22 31,-17 20,-19Z" fill="${b}" ${S}/><path d="M37,-36 C40,-30 39,-25 36,-22 C36,-27 35,-30 33,-32Z" fill="#fff" ${S}/><path d="M-25,-38 L-24,-67 L-4,-51Z" fill="${b}" ${S}/><path d="M25,-38 L24,-67 L4,-51Z" fill="${b}" ${S}/><path d="M-24.4,-58 L-24,-67 L-17.5,-61.5Z" fill="${INK}"/><path d="M24.4,-58 L24,-67 L17.5,-61.5Z" fill="${INK}"/>`, `<ellipse cx="0" cy="-26" rx="10" ry="6.5" fill="#fff" opacity=".85"/>`];
    if (species === "chick") return [`<path d="M-4,-50 q-6,-12 2,-15 q-1,6 2,8 q0,-8 7,-9 q-3,8 -1,16Z" fill="${b}" ${S}/>`, ""];
    if (species === "sprout") return [`<path d="M0,-48 V-62" fill="none" stroke="#3f8a5a" stroke-width="3" stroke-linecap="round"/><path d="M0,-62 C-13,-75 -21,-62 -1,-60Z" fill="#7cc48f" ${S}/><path d="M0,-62 C13,-77 23,-64 1,-60Z" fill="#9bd6a8" ${S}/>`, ""];
    return ["", ""];
  }

  const ACC_MARKUP = {
    none: "",
    bowtie: `<path d="M0,-17 L-10,-22.5 L-10,-11.5Z M0,-17 L10,-22.5 L10,-11.5Z" fill="${INK}" ${S}/><circle cx="0" cy="-17" r="2.6" fill="#f2c14e" stroke="${INK}" stroke-width="1.5"/>`,
    monocle: `<circle cx="9" cy="-34" r="7" fill="rgba(255,255,255,.4)" stroke="#a87a22" stroke-width="2"/><path d="M15.5,-31 q6,9 2,18" fill="none" stroke="#a87a22" stroke-width="1.5" stroke-linecap="round"/>`,
    glasses: `<circle cx="-9" cy="-34" r="6.5" fill="rgba(255,255,255,.4)" stroke="${INK}" stroke-width="1.8"/><circle cx="9" cy="-34" r="6.5" fill="rgba(255,255,255,.4)" stroke="${INK}" stroke-width="1.8"/><path d="M-2.5,-34 h5" stroke="${INK}" stroke-width="1.8"/>`,
    headset: `<path d="M-24,-36 C-24,-64 24,-64 24,-36" fill="none" stroke="${INK}" stroke-width="3.2" stroke-linecap="round"/><rect x="-30" y="-42" width="9" height="14" rx="3.5" fill="#5a6072" ${S}/><rect x="21" y="-42" width="9" height="14" rx="3.5" fill="#5a6072" ${S}/><path d="M-25,-28 q1,9 13,7" fill="none" stroke="${INK}" stroke-width="1.8" stroke-linecap="round"/><circle cx="-11" cy="-21" r="2.2" fill="${INK}"/>`,
    apron: `<path d="M-14,-21 h28 v14 q0,6 -6,6 h-16 q-6,0 -6,-6Z" fill="#fffaf0" ${S}/><rect x="-5.5" y="-14" width="11" height="8" rx="2" fill="none" stroke="${INK}" stroke-width="1.5"/>`,
    hat: `<ellipse cx="0" cy="-49" rx="27" ry="6.5" fill="#e8c56a" ${S}/><path d="M-14,-50 C-14,-66 14,-66 14,-50Z" fill="#e8c56a" ${S}/><path d="M-13.5,-53 q13.5,4 27,0" fill="none" stroke="#2f6f5e" stroke-width="3"/>`,
  };

  // 一个角色（站立原点在脚底）。表情四种都画上，由外层 data-face 决定显示哪一种。
  function charMarkup(look) {
    const col = COLORS[look.color] || COLORS.white, b = col.b, d = col.d;
    const [back, front] = speciesMarkup(look.species, b, d);
    const mouth = look.species === "chick"
      ? `<path d="M-4.5,-28.5 L4.5,-28.5 L0,-22.5Z" fill="#f2a03b" ${S}/>`
      : `<path d="M-3,-27 q3,3.2 6,0" fill="none" ${S}/>`;
    return `${back}
  <ellipse cx="-10" cy="-1.5" rx="7.5" ry="4" fill="${d}" ${S}/><ellipse cx="10" cy="-1.5" rx="7.5" ry="4" fill="${d}" ${S}/>
  <path d="M-26,-27 C-26,-57 26,-57 26,-27 C26,-7 17,-1 0,-1 C-17,-1 -26,-7 -26,-27Z" fill="${b}" ${S}/>
  <ellipse cx="0" cy="-13.5" rx="14" ry="10.5" fill="#fff" opacity=".42"/>
  <ellipse class="arm arm-l" cx="-24.5" cy="-19" rx="5.5" ry="9" fill="${b}" ${S}/><ellipse class="arm arm-r" cx="24.5" cy="-19" rx="5.5" ry="9" fill="${b}" ${S}/>
  ${front}
  <ellipse cx="-17" cy="-27" rx="4.2" ry="2.6" fill="#f08a8a" opacity=".5"/><ellipse cx="17" cy="-27" rx="4.2" ry="2.6" fill="#f08a8a" opacity=".5"/>
  <g class="eyes eyes-open"><circle cx="-9" cy="-34" r="3.1" fill="${INK}"/><circle cx="9" cy="-34" r="3.1" fill="${INK}"/><circle cx="-8" cy="-35.2" r="1.1" fill="#fff"/><circle cx="10" cy="-35.2" r="1.1" fill="#fff"/></g>
  <g class="eyes eyes-closed"><path d="M-12.5,-34 q3.5,3.4 7,0 M5.5,-34 q3.5,3.4 7,0" fill="none" ${S}/></g>
  <g class="eyes eyes-happy"><path d="M-12.5,-32.5 q3.5,-5 7,0 M5.5,-32.5 q3.5,-5 7,0" fill="none" ${S}/></g>
  <g class="eyes eyes-dizzy"><path d="M-12,-37 l6,6 m0,-6 l-6,6 M6,-37 l6,6 m0,-6 l-6,6" fill="none" ${S}/></g>
  ${mouth}${ACC_MARKUP[look.acc] || ""}`;
  }

  // 手里的道具（相对角色原点）。
  const PROPS = {
    letter: `<g transform="translate(30,-22) rotate(-10)"><rect x="-10" y="-7" width="20" height="14" rx="2" fill="#fffaf0" ${S}/><path d="M-10,-6 L0,2 L10,-6" fill="none" ${S}/></g>`,
    scroll: `<g transform="translate(31,-24) rotate(12)"><rect x="-5" y="-12" width="10" height="24" rx="4.5" fill="#fffaf0" ${S}/><path d="M-5,-3 h10 M-5,3 h10" stroke="#5e9c76" stroke-width="2"/></g>`,
    phone: `<path d="M25,-45 c-6,3 -6,18 0,21 l4,-3 c-3,-3 -3,-12 0,-15Z" fill="${INK}" ${S}/>`,
    tea: `<g transform="translate(30,-19)"><path d="M-7,-5 h14 v5 a7,6 0 0 1 -14,0Z" fill="#fffaf0" ${S}/><path d="M7,-3 q5,0 4,4 q-1,3 -5,2" fill="none" ${S}/><path class="steam" d="M-2,-9 q-3,-4 0,-7 M3,-9 q-3,-4 0,-7" fill="none" stroke="#8b92a1" stroke-width="1.6" stroke-linecap="round"/></g>`,
    magnifier: `<g transform="translate(31,-30)"><path d="M6,6 l8,9" fill="none" stroke="${INK}" stroke-width="4" stroke-linecap="round"/><circle r="8.5" fill="rgba(255,255,255,.6)" ${S}/></g>`,
    quill: `<g transform="translate(30,-24) rotate(20)"><path d="M0,10 L-1,-4 C-6,-12 2,-22 6,-22 C8,-14 5,-8 1,-4Z" fill="#fffaf0" ${S}/></g>`,
    book: `<g transform="translate(30,-22)"><rect x="-10" y="-8" width="20" height="15" rx="2" fill="#7fa99b" ${S}/><path d="M0,-8 v15" fill="none" ${S}/></g>`,
    paper: `<g transform="translate(32,-34)"><rect x="-11" y="-14" width="22" height="28" rx="2.5" fill="#fffaf0" ${S}/><path d="M-6,-8 h12 M-6,-3 h12 M-6,2 h6" stroke="#9aa0ad" stroke-width="1.6" stroke-linecap="round"/><circle cx="4" cy="8" r="4.4" fill="none" stroke="#d2432f" stroke-width="1.7" stroke-dasharray="2.2 2"/></g>`,
  };
  // 飞行中的信与卷轴
  const FLY = {
    letter: `<rect x="-12" y="-8" width="24" height="16" rx="2" fill="#fffaf0" ${S}/><path d="M-12,-7 L0,2 L12,-7" fill="none" ${S}/>`,
    note: `<rect x="-12" y="-8" width="24" height="16" rx="2" fill="#d9dce3" ${S}/><path d="M-12,-7 L0,2 L12,-7" fill="none" ${S}/>`,
    scroll: `<rect x="-6" y="-13" width="12" height="26" rx="5" fill="#fffaf0" ${S}/><path d="M-6,-3 h12 M-6,3 h12" stroke="#5e9c76" stroke-width="2"/>`,
  };

  // 匠人默认形象：按包名稳定推导（FNV-1a），同一匠人对谁都长一样。
  function hash(text) {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  }
  function lookFor(pkg) {
    const h = hash(String(pkg)), pick = (obj, shift) => { const keys = Object.keys(obj); return keys[(h >>> shift) % keys.length]; };
    const species = ["cat", "rabbit", "bear", "chick", "sprout"][h % 5];
    const accs = Object.keys(ACCS).filter(k => k !== "bowtie");
    return { species, color: pick(COLORS, 8), acc: accs[(h >>> 16) % accs.length], name: String(pkg) };
  }

  // 本设备上的角色设置：{ butler: {...}, "<包名>": {...} }。存储不可用时只是不记住。
  function loadLooks() {
    try {
      const saved = JSON.parse(globalThis.localStorage.getItem(STORE_KEY) || "null");
      return saved && typeof saved === "object" ? saved : {};
    } catch { return {}; }
  }
  function saveLooks(looks) {
    try { globalThis.localStorage.setItem(STORE_KEY, JSON.stringify(looks)); } catch { /* 存储不可用：本次有效，不记住 */ }
  }
  function setNames(items) {
    artisanNames = Object.fromEntries((items || []).filter(item => item && item.package).map(item => [item.package,
      typeof item.name === "string" && item.name ? item.name : item.package]));
  }
  // 某个角色的最终形象：本设备上的设置覆盖默认，服务端提供本地化工匠名。
  function resolveLook(looks, key) {
    const base = key === "butler" ? BUTLER : { ...lookFor(key), name: artisanNames[key] || key }, own = looks[key] || {};
    const look = {
      species: SPECIES[own.species] ? own.species : base.species,
      color: COLORS[own.color] ? own.color : base.color,
      acc: ACCS[own.acc] !== undefined ? own.acc : base.acc,
      name: base.name,
    };
    if (key === "butler" && typeof own.name === "string" && own.name.trim()) look.name = own.name.trim().slice(0, 6);
    return look;
  }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.art = { INK, COLORS, SPECIES, ACCS, TOP, PROPS, FLY, charMarkup, lookFor,
    loadLooks, saveLooks, setNames, resolveLook };
})();
