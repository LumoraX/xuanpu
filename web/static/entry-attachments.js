// 对话附件交互组件：把消息与输入区里的附件渲染为可查看、可下载的交互标签，并提供安全的页内预览弹窗。
// 所有文本一律走 textContent / 纯 DOM 节点，不拼 HTML，不内联执行脚本或 HTML/SVG 文档。
"use strict";

const ATT_MAX_TEXT_PREVIEW_BYTES = 512 * 1024;
const ATT_TEXT_EXTS = new Set([
  "md", "markdown", "txt", "json", "csv", "tsv", "log", "yaml", "yml",
  "xml", "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx", "jsx",
  "py", "sh", "bash", "zsh", "sql", "ini", "conf", "cfg", "toml",
  "vcf", "ics", "svg", "go", "rs", "java", "c", "h", "cpp", "hpp",
]);
const ATT_IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif"]);
const ATT_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const ATT_BINARY_EXTS = new Set(["docx", "xlsx", "pptx", "doc", "xls", "ppt", "zip", "tar", "gz", "7z", "rar", "mp3", "mp4", "mov", "wav"]);

const attViewer = {
  open: false,
  epoch: 0,
  controller: null,
  blobUrl: null,
  file: null,
  mode: "rendered",
  loading: false,
  error: "",
  kind: "binary",
  text: "",
  truncated: false,
  rawSize: 0,
  keyBound: false,
};

function attExtension(name) {
  const clean = String(name || "").trim().toLowerCase();
  const idx = clean.lastIndexOf(".");
  return idx > 0 && idx < clean.length - 1 ? clean.slice(idx + 1) : "";
}

function attFormatSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

function attContentUrl(itemId, revision) {
  const base = "/v1/workspace/files/" + encodeURIComponent(String(itemId || "")) + "/content";
  return revision ? base + "?revision=" + encodeURIComponent(String(revision)) : base;
}

function attSafeExternalUrl(raw) {
  if (!raw) return null;
  try {
    const parsed = new URL(String(raw));
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function attPreviewKind(name, mimeType) {
  const ext = attExtension(name);
  const mime = String(mimeType || "").split(";")[0].trim().toLowerCase();
  if (ext === "md" || ext === "markdown" || mime === "text/markdown") return "markdown";
  if (ATT_IMAGE_EXTS.has(ext) || ATT_IMAGE_MIMES.has(mime)) return "image";
  if (ext === "pdf" || mime === "application/pdf") return "pdf";
  if (ATT_BINARY_EXTS.has(ext)) return "binary";
  if (ATT_TEXT_EXTS.has(ext) || mime.startsWith("text/") || mime === "application/json" || mime === "application/xml") return "text";
  return "binary";
}

function attCacheMeta(item) {
  if (!item) return null;
  const data = item.data || item;
  const itemId = item.item_id || item.id;
  if (!itemId) return null;
  const prev = entry.fileMeta ? entry.fileMeta.get(itemId) : null;
  const merged = {
    item_id: itemId,
    revision: item.revision || data.current_revision || (prev && prev.revision) || null,
    name: data.name || item.name || (prev && prev.name) || "",
    mime_type: data.mime_type || item.mime_type || (prev && prev.mime_type) || "",
    size: typeof data.size === "number" ? data.size : (typeof item.size === "number" ? item.size : (prev ? prev.size : 0)),
    source_url: data.source_url !== undefined ? data.source_url : (prev ? prev.source_url : null),
    trashed_at: item.trashed_at !== undefined ? item.trashed_at : (prev ? prev.trashed_at : null),
  };
  if (entry.fileMeta) entry.fileMeta.set(itemId, merged);
  return merged;
}

function attNormalizeRef(ref) {
  if (!ref) return null;
  if (typeof ref === "string") {
    const cached = entry.fileMeta && entry.fileMeta.get(ref);
    return cached ? { ...cached } : { item_id: ref, revision: null, name: "", mime_type: "", size: 0, source_url: null };
  }
  const itemId = ref.item_id || ref.id;
  if (!itemId) return null;
  const cached = entry.fileMeta && entry.fileMeta.get(itemId);
  return {
    item_id: itemId,
    revision: ref.revision || (ref.data && ref.data.current_revision) || (cached && cached.revision) || null,
    name: ref.name || (ref.data && ref.data.name) || (cached && cached.name) || "",
    mime_type: ref.mime_type || (ref.data && ref.data.mime_type) || (cached && cached.mime_type) || "",
    size: typeof ref.size === "number" ? ref.size : (ref.data && typeof ref.data.size === "number" ? ref.data.size : (cached ? cached.size : 0)),
    source_url: ref.source_url || (ref.data && ref.data.source_url) || (cached && cached.source_url) || null,
  };
}

// 懒加载历史消息中缺失的附件元数据（名称、大小、类型），按 item_id 去重并受身份代际保护。
async function fetchAttachmentMeta(itemId) {
  if (!itemId || !entry.me) return null;
  if (entry.fileMeta && entry.fileMeta.has(itemId)) return entry.fileMeta.get(itemId);
  const epoch = entry.epoch;
  if (entry.fileMetaPending && entry.fileMetaPending.has(itemId)) {
    return entry.fileMetaPending.get(itemId);
  }
  const task = entryApi("GET", "/v1/workspace/items/" + encodeURIComponent(itemId))
    .then((item) => (isStale(epoch) ? null : attCacheMeta(item)))
    .catch(() => null)
    .finally(() => {
      if (entry.fileMetaPending && !isStale(epoch)) entry.fileMetaPending.delete(itemId);
    });
  if (entry.fileMetaPending) entry.fileMetaPending.set(itemId, task);
  return task;
}

function buildDownloadLink(meta, className, label) {
  const external = attSafeExternalUrl(meta.source_url);
  if (external) {
    const link = el("a", label || "打开原文", className);
    link.href = external;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }
  const link = el("a", label || "下载", className);
  link.href = attContentUrl(meta.item_id, meta.revision);
  link.rel = "noopener noreferrer";
  if (meta.name) link.setAttribute("download", meta.name);
  else link.setAttribute("download", "");
  link.title = "下载附件原件";
  return link;
}

function fillAttachmentChip(chip, meta) {
  const nameText = meta.name || "附件";
  const sizeText = attFormatSize(meta.size);
  const viewBtn = button("", () => { openAttachmentViewer(meta).catch(report); }, "msg-att-view");
  viewBtn.title = "点击查看附件内容";
  viewBtn.append(
    el("span", "▤", "msg-att-icon"),
    el("span", nameText, "msg-att-name")
  );
  if (sizeText) viewBtn.appendChild(el("span", sizeText, "msg-att-size"));
  const dlLink = buildDownloadLink(meta, "msg-att-dl", meta.source_url ? "打开" : "下载");
  chip.replaceChildren(viewBtn, dlLink);
}

// 在用户消息气泡内渲染可查看、可下载的附件条；缺少名称时异步补齐元数据。
function renderMessageAttachments(container, refs) {
  if (!container || !Array.isArray(refs) || !refs.length) return;
  const list = el("div", null, "msg-attachments");
  const epoch = entry.epoch;
  for (const raw of refs) {
    const meta = attNormalizeRef(raw);
    if (!meta) continue;
    const chip = el("div", null, "msg-att-item");
    chip.dataset.itemId = meta.item_id;
    if (meta.name) {
      fillAttachmentChip(chip, meta);
    } else {
      fillAttachmentChip(chip, { ...meta, name: "加载附件…" });
      fetchAttachmentMeta(meta.item_id).then((loaded) => {
        if (isStale(epoch) || !chip.parentNode) return;
        if (loaded) {
          fillAttachmentChip(chip, { ...loaded, revision: meta.revision || loaded.revision });
        } else {
          fillAttachmentChip(chip, { ...meta, name: "附件（" + meta.item_id + "）" });
        }
      });
    }
    list.appendChild(chip);
  }
  if (list.children.length) container.appendChild(list);
}

function revokeViewerBlob() {
  if (attViewer.blobUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
    try { URL.revokeObjectURL(attViewer.blobUrl); } catch { /* 忽略释放异常 */ }
  }
  attViewer.blobUrl = null;
}

function closeAttachmentViewer() {
  attViewer.open = false;
  if (attViewer.controller) {
    attViewer.controller.abort();
    attViewer.controller = null;
  }
  revokeViewerBlob();
  attViewer.file = null;
  attViewer.loading = false;
  attViewer.error = "";
  attViewer.text = "";
  attViewer.truncated = false;
  const overlay = $("attachment-overlay");
  if (overlay) {
    overlay.classList.add("hidden");
    overlay.replaceChildren();
  }
}

function ensureViewerKeyBinding() {
  if (attViewer.keyBound || typeof document === "undefined" || typeof document.addEventListener !== "function") return;
  attViewer.keyBound = true;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && attViewer.open) closeAttachmentViewer();
  });
}

async function openAttachmentViewer(rawRef) {
  if (!entry.me) return;
  ensureViewerKeyBinding();
  if (attViewer.controller) attViewer.controller.abort();
  revokeViewerBlob();

  const epoch = entry.epoch;
  const initial = attNormalizeRef(rawRef);
  if (!initial) return;
  const controller = new AbortController();
  attViewer.open = true;
  attViewer.epoch = epoch;
  attViewer.controller = controller;
  attViewer.file = initial;
  attViewer.loading = true;
  attViewer.error = "";
  attViewer.text = "";
  attViewer.truncated = false;
  attViewer.rawSize = initial.size || 0;
  attViewer.mode = "rendered";
  attViewer.kind = attPreviewKind(initial.name, initial.mime_type);
  renderAttachmentViewer();

  // 先补齐元数据（确保历史附件拿到真实文件名、MIME 和大小）
  let meta = initial;
  if (!meta.name || !meta.mime_type) {
    const loaded = await fetchAttachmentMeta(initial.item_id);
    if (isStale(epoch) || attViewer.controller !== controller) return;
    if (loaded) meta = { ...loaded, revision: initial.revision || loaded.revision };
    attViewer.file = meta;
    attViewer.rawSize = meta.size || 0;
    attViewer.kind = attPreviewKind(meta.name, meta.mime_type);
  }

  if (attSafeExternalUrl(meta.source_url)) {
    attViewer.kind = "external";
    attViewer.loading = false;
    renderAttachmentViewer();
    return;
  }

  if (attViewer.kind === "binary") {
    attViewer.loading = false;
    renderAttachmentViewer();
    return;
  }

  try {
    const response = await fetch(attContentUrl(meta.item_id, meta.revision), {
      method: "GET",
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "X-Requested-By": "xuanpu", "Accept-Language": preferredLanguages() },
    });
    if (isStale(epoch) || attViewer.controller !== controller) return;
    if (!response.ok) {
      let msg = "附件读取失败（" + response.status + "）";
      try {
        const errBody = JSON.parse(await response.text());
        if (errBody && errBody.error && errBody.error.message) msg = errBody.error.message;
      } catch { /* 非 JSON 错误体使用默认文案 */ }
      if (response.status === 401 && entry.me) resetEntry();
      throw new Error(msg);
    }

    const respType = (response.headers && typeof response.headers.get === "function" && response.headers.get("content-type")) || meta.mime_type || "";
    const resolvedKind = attPreviewKind(meta.name, respType);
    attViewer.kind = resolvedKind;

    if (resolvedKind === "image" || resolvedKind === "pdf") {
      const blob = typeof response.blob === "function"
        ? await response.blob()
        : new Blob([await response.arrayBuffer()], { type: resolvedKind === "pdf" ? "application/pdf" : respType });
      if (isStale(epoch) || attViewer.controller !== controller) return;
      attViewer.rawSize = blob.size || attViewer.rawSize;
      if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
        const typedBlob = resolvedKind === "pdf" && blob.type !== "application/pdf"
          ? new Blob([blob], { type: "application/pdf" })
          : blob;
        attViewer.blobUrl = URL.createObjectURL(typedBlob);
      }
      attViewer.loading = false;
      renderAttachmentViewer();
      return;
    }

    let rawText = "";
    if (typeof response.arrayBuffer === "function") {
      const buf = await response.arrayBuffer();
      if (isStale(epoch) || attViewer.controller !== controller) return;
      const bytes = new Uint8Array(buf);
      attViewer.rawSize = bytes.byteLength;
      const slice = bytes.byteLength > ATT_MAX_TEXT_PREVIEW_BYTES ? bytes.subarray(0, ATT_MAX_TEXT_PREVIEW_BYTES) : bytes;
      attViewer.truncated = bytes.byteLength > ATT_MAX_TEXT_PREVIEW_BYTES;
      if (slice.includes(0)) {
        attViewer.kind = "binary";
        attViewer.loading = false;
        renderAttachmentViewer();
        return;
      }
      rawText = new TextDecoder("utf-8", { fatal: false }).decode(slice);
    } else {
      rawText = await response.text();
      if (isStale(epoch) || attViewer.controller !== controller) return;
      attViewer.rawSize = rawText.length;
      if (rawText.length > ATT_MAX_TEXT_PREVIEW_BYTES) {
        rawText = rawText.slice(0, ATT_MAX_TEXT_PREVIEW_BYTES);
        attViewer.truncated = true;
      }
    }

    if (attExtension(meta.name) === "json" && !attViewer.truncated) {
      try { rawText = JSON.stringify(JSON.parse(rawText), null, 2); } catch { /* 保持原文 */ }
    }
    attViewer.text = rawText;
    attViewer.loading = false;
    renderAttachmentViewer();
  } catch (error) {
    if (isStale(epoch) || attViewer.controller !== controller || (error && error.name === "AbortError")) return;
    attViewer.loading = false;
    attViewer.error = (error && error.message) || "附件读取失败";
    renderAttachmentViewer();
  }
}

// 忠实排版 Markdown 附件（不删减原文 JSON 或特定前缀，纯 DOM textContent，无 HTML 注入）。
function appendAttInline(parent, text) {
  const parts = String(text || "").split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      parent.appendChild(el("strong", part.slice(2, -2)));
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      parent.appendChild(el("code", part.slice(1, -1), "att-inline-code"));
    } else {
      parent.appendChild(el("span", part));
    }
  }
}

function renderMarkdownPreview(container, rawText) {
  const lines = String(rawText || "").replace(/\r\n/g, "\n").split("\n");
  let list = null, listTag = "", codeLines = null, tableRows = null;
  const flushList = () => { if (list) { container.appendChild(list); list = null; listTag = ""; } };
  const flushTable = () => {
    if (!tableRows || !tableRows.length) { tableRows = null; return; }
    const tbl = el("table", null, "msg-table");
    tableRows.forEach((cells, rIdx) => {
      const tr = el("tr");
      cells.forEach((cell) => {
        const td = el(rIdx === 0 ? "th" : "td");
        appendAttInline(td, cell);
        tr.appendChild(td);
      });
      tbl.appendChild(tr);
    });
    container.appendChild(tbl);
    tableRows = null;
  };
  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    if (trimmed.startsWith("```")) {
      flushList();
      flushTable();
      if (codeLines === null) codeLines = [];
      else {
        container.appendChild(el("pre", codeLines.join("\n"), "att-md-code"));
        codeLines = null;
      }
      continue;
    }
    if (codeLines !== null) { codeLines.push(rawLine); continue; }
    if (!trimmed) { flushList(); flushTable(); continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushList(); flushTable();
      container.appendChild(el("hr", null, "att-md-hr"));
      continue;
    }
    if (trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2) {
      flushList();
      if (/^\|[\s:|-]+\|$/.test(trimmed)) continue;
      if (!tableRows) tableRows = [];
      tableRows.push(trimmed.slice(1, -1).split("|").map((c) => c.trim()));
      continue;
    }
    flushTable();
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushList();
      const level = Math.min(4, heading[1].length);
      const h = el("h" + level, null, "att-md-h att-md-h" + level);
      appendAttInline(h, heading[2]);
      container.appendChild(h);
      continue;
    }
    if (trimmed.startsWith("> ")) {
      flushList();
      const bq = el("blockquote", null, "att-md-quote");
      appendAttInline(bq, trimmed.slice(2));
      container.appendChild(bq);
      continue;
    }
    const ulMatch = trimmed.match(/^[-*•]\s+(.+)$/);
    const olMatch = !ulMatch && trimmed.match(/^\d+\.\s+(.+)$/);
    if (ulMatch || olMatch) {
      const wanted = ulMatch ? "ul" : "ol";
      if (!list || listTag !== wanted) { flushList(); list = el(wanted, null, "msg-list"); listTag = wanted; }
      const li = el("li");
      appendAttInline(li, (ulMatch || olMatch)[1]);
      list.appendChild(li);
      continue;
    }
    flushList();
    const para = el("p", null, "msg-para");
    appendAttInline(para, trimmed);
    container.appendChild(para);
  }
  if (codeLines !== null) container.appendChild(el("pre", codeLines.join("\n"), "att-md-code"));
  flushList();
  flushTable();
}

function renderAttachmentViewer() {
  const overlay = $("attachment-overlay");
  if (!overlay || !attViewer.open || !attViewer.file) return;
  overlay.classList.remove("hidden");

  const meta = attViewer.file;
  const sheet = el("section", null, "ab-sheet att-viewer-sheet");
  const header = el("header", null, "ab-header");
  const titleWrap = el("div", null, "att-viewer-title-wrap");
  titleWrap.appendChild(el("h2", meta.name || "附件预览", "ab-title"));
  const subParts = [
    meta.mime_type || "",
    attFormatSize(attViewer.rawSize || meta.size),
    meta.revision ? "版本 v" + meta.revision : "",
  ].filter(Boolean);
  if (subParts.length) titleWrap.appendChild(el("span", subParts.join(" · "), "ab-count"));

  const actions = el("div", null, "ab-head-actions");
  if (!attViewer.loading && !attViewer.error && attViewer.kind === "markdown") {
    const toggleLabel = attViewer.mode === "rendered" ? "查看原文" : "排版阅读";
    actions.appendChild(button(toggleLabel, () => {
      attViewer.mode = attViewer.mode === "rendered" ? "raw" : "rendered";
      renderAttachmentViewer();
    }, "ab-act-btn"));
  }
  if (!attViewer.loading && !attViewer.error && (attViewer.kind === "markdown" || attViewer.kind === "text") && attViewer.text) {
    const copyBtn = button("复制全文", () => {
      if (globalThis.navigator && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        navigator.clipboard.writeText(attViewer.text).then(() => {
          copyBtn.textContent = "已复制";
          setTimeout(() => { if (copyBtn.parentNode) copyBtn.textContent = "复制全文"; }, 1500);
        }).catch(() => {});
      }
    }, "ab-act-btn");
    actions.appendChild(copyBtn);
  }
  actions.appendChild(buildDownloadLink(meta, "ab-act-btn primary-soft att-dl-btn", meta.source_url ? "打开原文" : "下载原件"));
  actions.appendChild(button("关闭", closeAttachmentViewer, "ab-close-btn"));
  header.append(titleWrap, actions);
  sheet.appendChild(header);

  const body = el("div", null, "att-viewer-body");
  if (attViewer.loading) {
    body.appendChild(el("p", "正在读取附件内容…", "att-viewer-status"));
  } else if (attViewer.error) {
    body.appendChild(el("p", attViewer.error, "att-viewer-error"));
  } else if (attViewer.kind === "external") {
    const wrap = el("div", null, "att-viewer-fallback");
    wrap.append(
      el("p", "该附件为外部在线文档链接，不存储本地正本。"),
      buildDownloadLink(meta, "ab-act-btn primary-soft att-dl-btn", "打开原文")
    );
    body.appendChild(wrap);
  } else if (attViewer.kind === "image") {
    if (attViewer.blobUrl) {
      const img = el("img", null, "att-viewer-img");
      img.src = attViewer.blobUrl;
      img.alt = meta.name || "附件图片";
      body.appendChild(img);
    } else {
      body.appendChild(el("p", "当前环境无法内联显示图片，请点击右上角下载原件。", "att-viewer-status"));
    }
  } else if (attViewer.kind === "pdf") {
    if (attViewer.blobUrl) {
      const frame = el("iframe", null, "att-viewer-pdf");
      frame.src = attViewer.blobUrl;
      frame.title = meta.name || "PDF 预览";
      body.appendChild(frame);
    } else {
      body.appendChild(el("p", "当前环境无法内联显示 PDF，请点击右上角下载原件。", "att-viewer-status"));
    }
  } else if (attViewer.kind === "markdown" || attViewer.kind === "text") {
    if (attViewer.truncated) {
      body.appendChild(el("p", "文件较大，以下仅预览前 512 KB；完整内容请点击右上角「下载原件」。", "att-viewer-notice"));
    }
    if (!attViewer.text) {
      body.appendChild(el("p", "（空文件）", "att-viewer-status"));
    } else if (attViewer.kind === "markdown" && attViewer.mode === "rendered") {
      const article = el("div", null, "att-viewer-markdown");
      renderMarkdownPreview(article, attViewer.text);
      body.appendChild(article);
    } else {
      body.appendChild(el("pre", attViewer.text, "att-viewer-pre"));
    }
  } else {
    const wrap = el("div", null, "att-viewer-fallback");
    wrap.append(
      el("p", "该文件格式暂不支持网页内直接预览正文，请下载原件查看。"),
      buildDownloadLink(meta, "ab-act-btn primary-soft att-dl-btn", "下载原件")
    );
    body.appendChild(wrap);
  }
  sheet.appendChild(body);

  overlay.replaceChildren(sheet);
  if (!overlay.dataset.boundClose) {
    overlay.dataset.boundClose = "1";
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeAttachmentViewer();
    });
  }
}
