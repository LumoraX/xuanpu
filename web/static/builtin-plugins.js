// 内置 L2 前端富交互插件包（ADR 0027）：离线 Mermaid 矢量图、行级彩色 Diff、声明式 UI 卡片与沙盒 iframe 预览。
// 所有非内核 UI 渲染能力均通过 XuanpuPluginHost 标准契约注册，支持独立替换与干净卸载。
"use strict";

(function registerBuiltinFrontendPlugins() {
  if (!globalThis.XuanpuPluginHost) return;
  const SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs = {}, text = null) {
    const node = document.createElementNS ? document.createElementNS(SVG_NS, tag) : el(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (typeof node.setAttribute === "function") node.setAttribute(k, String(v));
    }
    if (text !== null) node.textContent = String(text);
    return node;
  }

  // 1. 离线零依赖 Mermaid 矢量图渲染器（支持 flowchart/graph 与 sequenceDiagram，纯 DOM/SVG 零 innerHTML）
  function parseFlowchart(source) {
    const nodes = new Map();
    const edges = [];
    const ensureNode = (id, label) => {
      const cleanId = id.trim();
      if (!cleanId) return null;
      const cleanLabel = (label || cleanId).replace(/^["']|["']$/g, "").trim();
      if (!nodes.has(cleanId)) nodes.set(cleanId, { id: cleanId, label: cleanLabel });
      else if (label) nodes.get(cleanId).label = cleanLabel;
      return cleanId;
    };
    const tokenRe = /([A-Za-z0-9_-]+)(?:\[([^\]]+)\]|\(([^)]+)\)|\{([^}]+)\})?/;
    for (const raw of source.split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("%%") || /^(?:flowchart|graph|subgraph|end)\b/i.test(line)) continue;
      const arrow = line.match(/^(.+?)\s*(?:-->|-\.->|==>)\s*(?:\|([^|]+)\|\s*)?(.+)$/);
      if (arrow) {
        const left = arrow[1].trim().match(tokenRe);
        const right = arrow[3].trim().match(tokenRe);
        if (left && right) {
          const from = ensureNode(left[1], left[2] || left[3] || left[4]);
          const to = ensureNode(right[1], right[2] || right[3] || right[4]);
          if (from && to) edges.push({ from, to, label: (arrow[2] || "").trim() });
        }
      }
    }
    return { nodes: [...nodes.values()], edges };
  }

  function parseSequenceDiagram(source) {
    const participants = [];
    const ensurePart = (name) => {
      const clean = name.trim();
      if (clean && !participants.includes(clean)) participants.push(clean);
      return clean;
    };
    const msgs = [];
    for (const raw of source.split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("%%") || /^sequenceDiagram\b/i.test(line)) continue;
      const pDecl = line.match(/^participant\s+(\S+)(?:\s+as\s+(.+))?$/i);
      if (pDecl) { ensurePart(pDecl[1]); continue; }
      const m = line.match(/^([A-Za-z0-9_\u4e00-\u9fa5-]+)\s*(--?>>?)\s*([A-Za-z0-9_\u4e00-\u9fa5-]+)\s*:\s*(.*)$/);
      if (m) {
        msgs.push({ from: ensurePart(m[1]), to: ensurePart(m[3]), dashed: m[2].startsWith("--"), label: m[4].trim() });
      }
    }
    return { participants, msgs };
  }

  function renderMermaidSvg(container, source) {
    const wrap = el("div", null, "msg-mermaid-block plugin-mermaid-card");
    const header = el("div", null, "plugin-card-bar");
    header.append(el("span", "Mermaid 矢量图示（离线渲染）", "msg-code-label"));
    wrap.appendChild(header);

    const isSeq = /^\s*sequenceDiagram\b/im.test(source);
    if (isSeq) {
      const seq = parseSequenceDiagram(source);
      if (seq.participants.length) {
        const colW = 140, padX = 24, topY = 20, rowH = 40;
        const width = Math.max(360, padX * 2 + seq.participants.length * colW);
        const height = topY + 60 + Math.max(1, seq.msgs.length) * rowH + 24;
        const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "mermaid-svg", role: "img", "aria-label": "Mermaid 时序图" });
        const xMap = new Map();
        seq.participants.forEach((p, idx) => {
          const cx = padX + idx * colW + colW / 2;
          xMap.set(p, cx);
          svg.appendChild(svgEl("rect", { x: cx - 54, y: topY, width: 108, height: 30, rx: 6, fill: "#1e293b", stroke: "#38bdf8", "stroke-width": 1.5 }));
          svg.appendChild(svgEl("text", { x: cx, y: topY + 19, fill: "#f8fafc", "font-size": 12, "text-anchor": "middle" }, p.slice(0, 14)));
          svg.appendChild(svgEl("line", { x1: cx, y1: topY + 30, x2: cx, y2: height - 16, stroke: "#475569", "stroke-dasharray": "4,4", "stroke-width": 1.2 }));
        });
        seq.msgs.forEach((m, idx) => {
          const x1 = xMap.get(m.from), x2 = xMap.get(m.to);
          if (x1 == null || x2 == null) return;
          const y = topY + 58 + idx * rowH;
          const attrs = { x1, y1: y, x2, y2: y, stroke: m.dashed ? "#94a3b8" : "#60a5fa", "stroke-width": 1.8 };
          if (m.dashed) attrs["stroke-dasharray"] = "5,3";
          svg.appendChild(svgEl("line", attrs));
          svg.appendChild(svgEl("circle", { cx: x2, cy: y, r: 3, fill: "#38bdf8" }));
          svg.appendChild(svgEl("text", { x: (x1 + x2) / 2, y: y - 6, fill: "#e2e8f0", "font-size": 11, "text-anchor": "middle" }, m.label.slice(0, 28)));
        });
        wrap.appendChild(svg);
      }
    } else {
      const parsed = parseFlowchart(source);
      if (parsed.nodes.length) {
        const boxW = 148, boxH = 42, gapX = 48, padX = 20, padY = 24;
        const cols = Math.min(4, parsed.nodes.length);
        const rows = Math.ceil(parsed.nodes.length / cols);
        const width = padX * 2 + cols * boxW + (cols - 1) * gapX;
        const height = padY * 2 + rows * boxH + Math.max(0, rows - 1) * 56;
        const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "mermaid-svg", role: "img", "aria-label": "Mermaid 图示" });
        const coords = new Map();
        parsed.nodes.forEach((n, idx) => {
          const col = idx % cols, row = Math.floor(idx / cols);
          const x = padX + col * (boxW + gapX), y = padY + row * (boxH + 56);
          coords.set(n.id, { x, y, cx: x + boxW / 2, cy: y + boxH / 2 });
        });
        for (const edge of parsed.edges) {
          const a = coords.get(edge.from), b = coords.get(edge.to);
          if (!a || !b) continue;
          svg.appendChild(svgEl("line", { x1: a.cx, y1: a.cy, x2: b.cx, y2: b.cy, stroke: "#60a5fa", "stroke-width": 2 }));
          if (edge.label) {
            svg.appendChild(svgEl("text", { x: (a.cx + b.cx) / 2, y: (a.cy + b.cy) / 2 - 6, fill: "#cbd5e1", "font-size": 11, "text-anchor": "middle" }, edge.label));
          }
        }
        for (const n of parsed.nodes) {
          const c = coords.get(n.id);
          svg.appendChild(svgEl("rect", { x: c.x, y: c.y, width: boxW, height: boxH, rx: 8, fill: "#1e293b", stroke: "#38bdf8", "stroke-width": 1.5 }));
          svg.appendChild(svgEl("text", { x: c.cx, y: c.cy + 4, fill: "#f8fafc", "font-size": 12, "text-anchor": "middle" }, n.label.slice(0, 18)));
        }
        wrap.appendChild(svg);
      }
    }

    const srcDetails = el("details", null, "mermaid-src-toggle");
    srcDetails.append(el("summary", "查看 Mermaid 源码"), el("pre", source));
    wrap.appendChild(srcDetails);
    container.appendChild(wrap);
    return wrap;
  }

  XuanpuPluginHost.register({
    plugin_id: "builtin:mermaid-svg",
    title: "Mermaid 离线矢量图渲染",
    slots: ["message-renderer", "artifact-viewer"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    mime_types: ["text/vnd.mermaid"],
    match: (ctx) => ctx.lang === "mermaid" || ctx.mimeType === "text/vnd.mermaid",
    render: (container, ctx) => renderMermaidSvg(container, ctx.text || ""),
  });

  // 2. 行级彩色 Unified Diff 审阅器插件
  function renderUnifiedDiff(container, text) {
    const lines = String(text || "").split("\n");
    let added = 0, removed = 0;
    const pre = el("div", null, "diff-lines plugin-diff-view");
    for (const line of lines) {
      let cls = "diff-ctx";
      if (line.startsWith("+") && !line.startsWith("+++")) { cls = "diff-add"; added++; }
      else if (line.startsWith("-") && !line.startsWith("---")) { cls = "diff-del"; removed++; }
      else if (line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")) { cls = "diff-hunk"; }
      pre.appendChild(el("div", line || " ", cls));
    }
    const wrap = el("div", null, "plugin-diff-card");
    const bar = el("div", null, "plugin-card-bar");
    bar.append(el("span", "变更对照（Unified Diff）", "msg-code-label"), el("span", `+${added} / -${removed}`, "wb-badge"));
    wrap.append(bar, pre);
    container.appendChild(wrap);
    return wrap;
  }

  XuanpuPluginHost.register({
    plugin_id: "builtin:unified-diff",
    title: "行级彩色 Diff 审阅器",
    slots: ["message-renderer", "artifact-viewer"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    mime_types: ["text/x-diff"],
    match: (ctx) => ctx.lang === "diff" || ctx.mimeType === "text/x-diff",
    render: (container, ctx) => renderUnifiedDiff(container, ctx.text || ""),
  });

  // 3. 声明式业务卡片与指标看板插件（Level A 零 JS 风险）
  function renderDeclarativeUi(container, rawText) {
    let spec = null;
    try { spec = JSON.parse(rawText); } catch { return null; }
    if (!spec || typeof spec !== "object" || Array.isArray(spec)) return null;
    const wrap = el("section", null, "plugin-decl-card");
    if (spec.title) wrap.appendChild(el("h4", String(spec.title), "plugin-decl-title"));
    if (Array.isArray(spec.metrics) && spec.metrics.length) {
      const grid = el("div", null, "plugin-metric-grid");
      for (const m of spec.metrics) {
        const tile = el("div", null, "plugin-metric-tile");
        tile.append(el("span", String(m.label || ""), "plugin-metric-label"), el("strong", String(m.value ?? ""), "plugin-metric-value"));
        if (m.delta) tile.appendChild(el("span", String(m.delta), "plugin-metric-delta"));
        grid.appendChild(tile);
      }
      wrap.appendChild(grid);
    }
    if (spec.table && Array.isArray(spec.table.headers) && Array.isArray(spec.table.rows)) {
      const tbl = el("table", null, "msg-table");
      const headTr = el("tr");
      for (const h of spec.table.headers) headTr.appendChild(el("th", String(h)));
      tbl.appendChild(headTr);
      for (const row of spec.table.rows) {
        const tr = el("tr");
        for (const cell of (Array.isArray(row) ? row : [])) tr.appendChild(el("td", String(cell)));
        tbl.appendChild(tr);
      }
      wrap.appendChild(tbl);
    }
    if (Array.isArray(spec.callout) || (spec.callout && typeof spec.callout === "object")) {
      const list = Array.isArray(spec.callout) ? spec.callout : [spec.callout];
      for (const c of list) {
        wrap.appendChild(el("p", String(c.text || ""), "plugin-callout " + String(c.tone || "info")));
      }
    }
    container.appendChild(wrap);
    return wrap;
  }

  XuanpuPluginHost.register({
    plugin_id: "builtin:declarative-ui",
    title: "声明式业务卡片与指标看板",
    slots: ["message-renderer", "artifact-viewer"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    mime_types: ["application/vnd.xuanpu.ui+json"],
    match: (ctx) => ctx.lang === "xuanpu-ui" || ctx.mimeType === "application/vnd.xuanpu.ui+json",
    render: (container, ctx) => renderDeclarativeUi(container, ctx.text || ""),
  });

  // 4. 沙盒交互式 HTML/SVG 产物渲染插件（经安全 sandbox="allow-scripts" 与 Bridge 挂载）
  XuanpuPluginHost.register({
    plugin_id: "builtin:sandbox-iframe",
    title: "沙盒交互式产物视图",
    slots: ["artifact-viewer"],
    mode: "sandbox_iframe",
    source_layer: "L2_builtin",
    version: "1.0.0",
    mime_types: ["text/html", "image/svg+xml"],
    bridge_permissions: ["artifact.read", "collection.read", "collection.write", "workspace.propose"],
    match: (ctx) => (ctx.mimeType || "").includes("html") || (ctx.mimeType || "").includes("svg"),
    render: (container, ctx) => {
      const mounted = XuanpuPluginHost.mountSandboxIframe(container, {
        plugin_id: "builtin:sandbox-iframe",
        version: "1.0.0",
        bridge_permissions: ["artifact.read", "collection.read", "collection.write", "workspace.propose"],
      }, ctx);
      return mounted.frame;
    },
  });

  // 5. 插件中心 L2 工作台插件：实时展示前后端所有已挂载插件、健康状态、版本与路由
  XuanpuPluginHost.register({
    plugin_id: "builtin:plugin-manager",
    title: "插件中心",
    slots: ["aux-tab"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    render: (container) => {
      const box = el("div", null, "wb-plugin-manager");
      const statusLine = el("p", "正在加载微内核前后端插件清单…", "meta");
      box.appendChild(statusLine);
      container.appendChild(box);
      const isAdmin = Boolean(entry.me && (entry.me.role === "tenant_admin" || entry.me.role === "platform_admin"));
      Promise.all([
        entryApi("GET", "/v1/plugins").catch(() => ({ items: [] })),
        entryApi("GET", "/v1/plugins/ui").catch(() => ({ items: [] })),
      ]).then(([be, fe]) => {
        box.replaceChildren();
        box.appendChild(el("h3", "后端微内核插件（L2 BackendPlugin）"));
        const beItems = Array.isArray(be && be.items) ? be.items : [];
        if (!beItems.length) {
          box.appendChild(el("p", "暂无已挂载后端插件。", "meta"));
        } else {
          for (const p of beItems) {
            const card = el("article", null, "wb-card");
            const head = el("div", null, "wb-card-head");
            const ok = Boolean(p.health && p.health.ok);
            head.append(
              el("strong", `${p.display_name || p.plugin_id} (${p.plugin_id}@${p.version})`),
              el("span", ok ? "healthy" : (p.state || "unmounted"), "wb-badge " + (ok ? "active" : "failed")),
            );
            card.appendChild(head);
            if (p.description) card.appendChild(el("p", p.description, "meta"));
            if (Array.isArray(p.routes) && p.routes.length) {
              card.appendChild(el("p", "动态路由：" + p.routes.join(" · "), "meta"));
            }
            if (isAdmin && p.state === "mounted") {
              const actions = el("div", null, "wb-actions");
              actions.appendChild(button("干净卸载", async () => {
                await entryApi("POST", "/v1/plugins/" + encodeURIComponent(p.plugin_id) + "/unmount");
                announce("已干净卸载插件：" + p.plugin_id);
                if (typeof renderWorkbenchDrawer === "function") renderWorkbenchDrawer();
              }));
              card.appendChild(actions);
            }
            box.appendChild(card);
          }
        }
        box.appendChild(el("h3", "前端微内核插件（L2 内置 + L1 匠人同包 UI）"));
        const feItems = Array.isArray(fe && fe.items) ? fe.items : [];
        for (const u of feItems) {
          const card = el("article", null, "wb-card");
          const head = el("div", null, "wb-card-head");
          head.append(
            el("strong", `${u.title} (${u.plugin_id}@${u.version})`),
            el("span", `${u.source_layer} · ${u.slot}`, "wb-badge active"),
          );
          card.appendChild(head);
          if (Array.isArray(u.bridge_permissions) && u.bridge_permissions.length) {
            card.appendChild(el("p", "Bridge 权限：" + u.bridge_permissions.join(", "), "meta"));
          }
          box.appendChild(card);
        }
      }).catch((err) => {
        statusLine.textContent = (err && err.message) || "加载插件清单失败";
      });
      return box;
    },
  });

  // 6. 离线零依赖数学公式渲染插件（纯 DOM 排版分式、上下标与希腊/数学符号，零 XSS 风险）
  const MATH_SYMBOLS = {
    "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ", "\\Delta": "Δ",
    "\\sigma": "σ", "\\Sigma": "Σ", "\\pi": "π", "\\mu": "μ", "\\lambda": "λ",
    "\\sum": "∑", "\\prod": "∏", "\\int": "∫", "\\infty": "∞", "\\times": "×",
    "\\cdot": "·", "\\pm": "±", "\\le": "≤", "\\ge": "≥", "\\ne": "≠",
    "\\approx": "≈", "\\rightarrow": "→", "\\to": "→", "\\Rightarrow": "⇒", "\\sqrt": "√",
  };

  function appendMathExpr(parent, expr) {
    let s = String(expr || "").trim();
    for (const [k, v] of Object.entries(MATH_SYMBOLS)) s = s.split(k).join(v);
    let i = 0;
    while (i < s.length) {
      if (s.startsWith("\\frac{", i)) {
        const numEnd = s.indexOf("}", i + 6);
        if (numEnd > 0 && s[numEnd + 1] === "{") {
          const denEnd = s.indexOf("}", numEnd + 2);
          if (denEnd > 0) {
            const frac = el("span", null, "math-frac");
            const num = el("span", null, "math-num");
            const den = el("span", null, "math-den");
            appendMathExpr(num, s.slice(i + 6, numEnd));
            appendMathExpr(den, s.slice(numEnd + 2, denEnd));
            frac.append(num, den);
            parent.appendChild(frac);
            i = denEnd + 1;
            continue;
          }
        }
      }
      if ((s[i] === "^" || s[i] === "_") && i + 1 < s.length) {
        const tag = s[i] === "^" ? "sup" : "sub";
        if (s[i + 1] === "{") {
          const close = s.indexOf("}", i + 2);
          if (close > 0) {
            parent.appendChild(el(tag, s.slice(i + 2, close)));
            i = close + 1;
            continue;
          }
        } else {
          parent.appendChild(el(tag, s[i + 1]));
          i += 2;
          continue;
        }
      }
      const nextSpecial = s.slice(i).search(/\\frac\{|[\^_]/);
      const end = nextSpecial <= 0 ? (nextSpecial === 0 ? i + 1 : s.length) : i + nextSpecial;
      parent.appendChild(el("span", s.slice(i, end)));
      i = end;
    }
  }

  XuanpuPluginHost.register({
    plugin_id: "builtin:math-formula",
    title: "数学公式离线排版",
    slots: ["message-renderer", "artifact-viewer"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    mime_types: ["text/x-latex"],
    match: (ctx) => ctx.lang === "math" || ctx.lang === "latex" || ctx.mimeType === "text/x-latex",
    render: (container, ctx) => {
      const wrap = el("div", null, "plugin-math-block");
      const formula = el("div", null, "plugin-math-expr");
      appendMathExpr(formula, ctx.text || "");
      wrap.appendChild(formula);
      container.appendChild(wrap);
      return wrap;
    },
  });

  // 7. 离线多语言代码语法高亮插件（Python / JS / TS / SQL / JSON / YAML / Bash）
  const CODE_LANGS = new Set(["python", "py", "javascript", "js", "typescript", "ts", "sql", "json", "yaml", "yml", "bash", "sh"]);
  const CODE_KW = new Set([
    "def", "async", "await", "class", "return", "if", "elif", "else", "for", "while", "import", "from", "as", "try", "except", "with", "yield", "True", "False", "None",
    "function", "const", "let", "var", "new", "throw", "catch", "export", "default", "true", "false", "null",
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "UPDATE", "DELETE", "CREATE", "TABLE", "ALTER", "JOIN", "ORDER", "BY", "LIMIT",
  ]);

  XuanpuPluginHost.register({
    plugin_id: "builtin:code-highlight",
    title: "离线代码语法高亮",
    slots: ["message-renderer"],
    mode: "declarative",
    source_layer: "L2_builtin",
    version: "1.0.0",
    match: (ctx) => CODE_LANGS.has((ctx.lang || "").toLowerCase()),
    render: (container, ctx) => {
      const wrap = el("div", null, "msg-code-block plugin-code-card");
      const bar = el("div", null, "plugin-card-bar");
      bar.append(el("span", (ctx.lang || "code").toUpperCase(), "msg-code-label"));
      wrap.appendChild(bar);
      const pre = el("pre", null, "plugin-code-pre");
      const tokRe = /(#[^\n]*|\/\/[^\n]*|--[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
      const lines = String(ctx.text || "").split("\n");
      lines.forEach((line, idx) => {
        let last = 0;
        let m;
        tokRe.lastIndex = 0;
        while ((m = tokRe.exec(line)) !== null) {
          if (m.index > last) pre.appendChild(el("span", line.slice(last, m.index)));
          const tok = m[0];
          let cls = "";
          if (tok.startsWith("#") || tok.startsWith("//") || tok.startsWith("--")) cls = "tok-com";
          else if (tok.startsWith('"') || tok.startsWith("'")) cls = "tok-str";
          else if (/^\d/.test(tok)) cls = "tok-num";
          else if (CODE_KW.has(tok) || CODE_KW.has(tok.toUpperCase())) cls = "tok-kw";
          pre.appendChild(el("span", tok, cls || null));
          last = m.index + tok.length;
        }
        if (last < line.length) pre.appendChild(el("span", line.slice(last)));
        if (idx < lines.length - 1) pre.appendChild(el("span", "\n"));
      });
      wrap.appendChild(pre);
      container.appendChild(wrap);
      return wrap;
    },
  });
})();
