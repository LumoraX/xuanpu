// 面向终端用户的助手气泡排版：纯 DOM textContent，无原始 JSON、自动拆分来源头、Markdown 表格/代码/Mermaid 与可折叠复核说明。
"use strict";

// 过滤面向终端用户回复里意外出现的原始 JSON 对象或代码块，提取其中的自然语言说明（保留声明式 xuanpu-ui 插件块）。
function cleanUserText(raw) {
  let text = String(raw || "");
  const keptBlocks = [];
  text = text.replace(/```xuanpu-ui\b[\s\S]*?```/gi, (block) => {
    const token = "@@XUANPU_UI_BLOCK_" + keptBlocks.length + "@@";
    keptBlocks.push(block);
    return token;
  });
  text = text.replace(/```(?:json)?\s*\{[\s\S]*?\}\s*```/gi, "").trim();
  let out = "", i = 0;
  while (i < text.length) {
    const start = text.indexOf("{", i);
    if (start < 0) { out += text.slice(i); break; }
    out += text.slice(i, start);
    let depth = 0, inStr = false, esc = false, end = -1;
    for (let j = start; j < text.length; j++) {
      const ch = text[j];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
      } else {
        if (ch === '"') inStr = true;
        else if (ch === "{") depth++;
        else if (ch === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
      }
    }
    if (end < 0) { out += text.slice(start); break; }
    const candidate = text.slice(start, end);
    let parsed = null;
    try { parsed = JSON.parse(candidate); } catch { parsed = null; }
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const before = out.trim(), after = text.slice(end).trim();
      if (!before && !after) {
        const picked = ["message", "summary", "answer", "reason"]
          .map(k => (typeof parsed[k] === "string" ? parsed[k].trim() : ""))
          .filter(Boolean);
        if (picked.length) out += [...new Set(picked)].join("；");
      }
      i = end;
    } else {
      out += "{";
      i = start + 1;
    }
  }
  let restored = out.replace(/\n{3,}/g, "\n\n").trim();
  for (let k = 0; k < keptBlocks.length; k++) {
    restored = restored.replace("@@XUANPU_UI_BLOCK_" + k + "@@", keptBlocks[k]);
  }
  return restored;
}

// 安全渲染单行内的 **加粗**，全部走 textContent/DOM 节点，不拼 HTML。
function appendFormattedInline(parent, text) {
  const cleaned = String(text || "").replace(/^#{1,6}\s+/, "");
  const parts = cleaned.split(/(\*\*[^*]+\*\*)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      parent.appendChild(el("strong", part.slice(2, -2)));
    } else {
      parent.appendChild(el("span", part));
    }
  }
}

// 面向终端用户的助手气泡排版：纯 DOM textContent，无原始 JSON、自动拆分来源头、条目列表、表格、代码块与可折叠复核说明。
function renderAssistantBubble(node, rawText) {
  const cleaned = cleanUserText(rawText) || String(rawText || "").trim();
  const reviewMatch = cleaned.match(/\n*\s*辅助判断（[^）]*）[：:]\s*([\s\S]+)$/);
  const mainBody = reviewMatch ? cleaned.slice(0, reviewMatch.index).trim() : cleaned;
  const reviewBody = reviewMatch ? reviewMatch[1].trim() : "";

  node.textContent = "";
  let bodyText = mainBody;
  const headerMatch = bodyText.match(/^「([^」]+)」已完成，结果如下[：:]\s*\n?/);
  if (headerMatch) {
    const sourceTag = el("div", null, "msg-source");
    sourceTag.append(el("span", headerMatch[1], "msg-source-name"), el("span", "已完成", "msg-source-state"));
    node.appendChild(sourceTag);
    bodyText = bodyText.slice(headerMatch[0].length).trim();
  }

  const lines = bodyText.split("\n");
  let list = null;
  let codeLines = null;
  let codeLang = "";
  let mathLines = null;
  let tableRows = null;
  const flushList = () => { if (list) { node.appendChild(list); list = null; } };
  const flushTable = () => {
    if (!tableRows || !tableRows.length) { tableRows = null; return; }
    const tbl = el("table", null, "msg-table");
    tableRows.forEach((cells, rIdx) => {
      const tr = el("tr");
      cells.forEach((cell) => {
        const td = el(rIdx === 0 ? "th" : "td");
        appendFormattedInline(td, cell);
        tr.appendChild(td);
      });
      tbl.appendChild(tr);
    });
    node.appendChild(tbl);
    tableRows = null;
  };
  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    if (trimmed.startsWith("```")) {
      flushList();
      flushTable();
      if (codeLines === null) {
        codeLines = [];
        codeLang = trimmed.slice(3).trim().toLowerCase();
      } else {
        const blockText = codeLines.join("\n");
        const handled = globalThis.XuanpuPluginHost && XuanpuPluginHost.renderMessageBlock(node, {
          slot: "message-renderer",
          lang: codeLang,
          text: blockText,
        });
        if (!handled) {
          const wrap = el("div", null, codeLang === "mermaid" ? "msg-mermaid-block" : "msg-code-block");
          if (codeLang === "mermaid") wrap.appendChild(el("div", "Mermaid 图示源码（可在工作台预览器展开）", "msg-code-label"));
          wrap.appendChild(el("pre", blockText));
          node.appendChild(wrap);
        }
        codeLines = null;
        codeLang = "";
      }
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(rawLine);
      continue;
    }
    if (trimmed === "$$") {
      flushList();
      flushTable();
      if (mathLines === null) {
        mathLines = [];
      } else {
        const expr = mathLines.join("\n").trim();
        const handled = globalThis.XuanpuPluginHost && XuanpuPluginHost.renderMessageBlock(node, {
          slot: "message-renderer", lang: "math", text: expr,
        });
        if (!handled) node.appendChild(el("pre", expr, "msg-code-block"));
        mathLines = null;
      }
      continue;
    }
    if (mathLines !== null) {
      mathLines.push(rawLine);
      continue;
    }
    if (trimmed.startsWith("$$") && trimmed.endsWith("$$") && trimmed.length > 4) {
      flushList();
      flushTable();
      const expr = trimmed.slice(2, -2).trim();
      const handled = globalThis.XuanpuPluginHost && XuanpuPluginHost.renderMessageBlock(node, {
        slot: "message-renderer", lang: "math", text: expr,
      });
      if (!handled) node.appendChild(el("pre", expr, "msg-code-block"));
      continue;
    }
    if (!trimmed) { flushList(); flushTable(); continue; }
    if (trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2) {
      flushList();
      if (/^\|[\s:|-]+\|$/.test(trimmed)) continue;
      if (!tableRows) tableRows = [];
      tableRows.push(trimmed.slice(1, -1).split("|").map((c) => c.trim()));
      continue;
    }
    flushTable();
    if (/^[-•*]\s+/.test(trimmed)) {
      if (!list) list = el("ul", null, "msg-list");
      const li = el("li");
      appendFormattedInline(li, trimmed.replace(/^[-•*]\s+/, ""));
      list.appendChild(li);
    } else {
      flushList();
      const para = el("p", null, /^#{1,6}\s+/.test(trimmed) ? "msg-para msg-heading" : "msg-para");
      appendFormattedInline(para, trimmed);
      node.appendChild(para);
    }
  }
  if (codeLines !== null) {
    const wrap = el("div", null, "msg-code-block");
    wrap.appendChild(el("pre", codeLines.join("\n")));
    node.appendChild(wrap);
  }
  flushList();
  flushTable();

  if (reviewBody) {
    const details = el("details", null, "msg-review");
    details.append(el("summary", "管家复核说明"), el("p", reviewBody));
    node.appendChild(details);
  }
  return mainBody || cleaned;
}
