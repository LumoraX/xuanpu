// 纯 Web 辅助工作台抽屉（Aux Pane）与顶部 Live Focus 实时焦点胶囊：
// 覆盖 4 大标签：跨轮任务板（Task Boards）、过程产物预览与转存（Artifacts）、
// 跨节点脱敏 Transcript 诊断（Diagnostics）及三层发布节点回退（Releases，管理员可见）。
"use strict";

const workbenchState = {
  open: false,
  tab: "tasks",
  traceId: null,
  liveFocus: null,
  boards: [],
  artifacts: [],
  preview: null,
  failedTranscripts: [],
  transcriptPage: null,
  releasePackage: "",
  releases: [],
};

function updateLiveFocusPill(snapshot) {
  if (snapshot) workbenchState.liveFocus = snapshot;
  const pill = $("entry-live-focus");
  if (!pill) return;
  const snap = workbenchState.liveFocus;
  if (!snap || snap.status !== "active") {
    pill.classList.add("hidden");
    pill.textContent = "";
    return;
  }
  const steps = Array.isArray(snap.steps) ? snap.steps : [];
  const done = steps.filter((s) => s.status === "done").length;
  const focusText = snap.focus || snap.title || "推进中";
  const progress = steps.length ? ` (${done}/${steps.length})` : "";
  pill.textContent = "焦点：" + focusText + progress;
  pill.title = "点击打开辅助工作台查看跨轮任务板与产物";
  pill.classList.remove("hidden");
  if (snap.trace_id) workbenchState.traceId = snap.trace_id;
  if (workbenchState.open && workbenchState.tab === "tasks") {
    renderWorkbenchDrawer();
  }
}

function closeWorkbenchDrawer() {
  workbenchState.open = false;
  const overlay = $("workbench-overlay");
  if (overlay) {
    overlay.classList.add("hidden");
    overlay.replaceChildren();
  }
  if (document.body && document.body.classList) {
    document.body.classList.remove("workbench-open");
  }
  const btn = $("entry-workbench");
  if (btn) btn.setAttribute("aria-pressed", "false");
}

async function openWorkbenchDrawer(options = {}) {
  if (!entry.me) return;
  if (options.tab) workbenchState.tab = options.tab;
  if (options.traceId) workbenchState.traceId = options.traceId;
  workbenchState.open = true;
  if (document.body && document.body.classList) {
    document.body.classList.add("workbench-open");
  }
  const btn = $("entry-workbench");
  if (btn) btn.setAttribute("aria-pressed", "true");
  renderWorkbenchDrawer();
  await refreshWorkbenchTab();
}

async function refreshWorkbenchTab() {
  const epoch = entry.epoch;
  try {
    if (workbenchState.tab === "tasks") {
      const res = await entryApi("GET", "/v1/assistant/task-boards?limit=20");
      if (isStale(epoch)) return;
      workbenchState.boards = Array.isArray(res.items) ? res.items : [];
      const active = workbenchState.boards.find((b) => b.status === "active") || null;
      updateLiveFocusPill(active);
    } else if (workbenchState.tab === "artifacts") {
      const traceId = workbenchState.traceId || (workbenchState.boards[0] && workbenchState.boards[0].trace_id);
      if (traceId) {
        const res = await entryApi("GET", "/v1/traces/" + encodeURIComponent(traceId) + "/artifacts");
        if (isStale(epoch)) return;
        workbenchState.artifacts = Array.isArray(res.items) ? res.items : [];
      } else {
        workbenchState.artifacts = [];
      }
    } else if (workbenchState.tab === "diagnostics") {
      const res = await entryApi("GET", "/v1/diagnostics/failed-transcripts?limit=20");
      if (isStale(epoch)) return;
      workbenchState.failedTranscripts = Array.isArray(res.items) ? res.items : [];
    } else if (workbenchState.tab === "releases" && workbenchState.releasePackage) {
      const pkg = workbenchState.releasePackage.trim();
      const res = await entryApi("GET", "/v1/releases/artisan/" + encodeURIComponent(pkg));
      if (isStale(epoch)) return;
      workbenchState.releases = Array.isArray(res.items) ? res.items : [];
    }
  } catch (error) {
    if (!isStale(epoch)) report(error);
  }
  if (!isStale(epoch) && workbenchState.open) renderWorkbenchDrawer();
}

function renderWorkbenchDrawer() {
  const overlay = $("workbench-overlay");
  if (!overlay || !workbenchState.open) return;
  overlay.classList.remove("hidden");

  const panel = el("section", null, "ab-drawer wb-drawer");
  // 桌面端右侧常驻分屏拖拽调宽手柄
  const resizer = el("div", null, "wb-resizer");
  resizer.setAttribute("role", "separator");
  resizer.setAttribute("aria-orientation", "vertical");
  resizer.title = "拖动调整工作台分屏宽度";
  resizer.addEventListener("mousedown", (startEv) => {
    if (typeof startEv.preventDefault === "function") startEv.preventDefault();
    const onMove = (ev) => {
      const vw = globalThis.innerWidth || 1280;
      const next = Math.max(320, Math.min(Math.round(vw * 0.72), vw - (ev.clientX || 0)));
      if (document.documentElement && document.documentElement.style && document.documentElement.style.setProperty) {
        document.documentElement.style.setProperty("--wb-width", next + "px");
      }
    };
    const onUp = () => {
      if (globalThis.removeEventListener) {
        globalThis.removeEventListener("mousemove", onMove);
        globalThis.removeEventListener("mouseup", onUp);
      }
    };
    if (globalThis.addEventListener) {
      globalThis.addEventListener("mousemove", onMove);
      globalThis.addEventListener("mouseup", onUp);
    }
  });
  panel.appendChild(resizer);
  const head = el("header", null, "ab-head");
  const titleWrap = el("div");
  titleWrap.append(el("h2", "辅助工作台"), el("p", "跨轮任务板、过程产物预览、跨节点诊断与发布回退", "meta"));
  head.append(titleWrap, button("关闭", closeWorkbenchDrawer, "ab-close"));
  panel.appendChild(head);

  const tabs = el("div", null, "wb-tabs");
  const tabDefs = [
    ["tasks", "任务板"],
    ["artifacts", "过程产物"],
    ["diagnostics", "跨节点诊断"],
  ];
  if (entry.me && (entry.me.role === "tenant_admin" || entry.me.role === "platform_admin")) {
    tabDefs.push(["releases", "发布与回退"]);
  }
  if (globalThis.XuanpuPluginHost) {
    for (const plugin of XuanpuPluginHost.list("aux-tab")) {
      tabDefs.push(["plugin:" + plugin.plugin_id, plugin.title]);
    }
  }
  for (const [id, label] of tabDefs) {
    const tBtn = button(label, () => {
      workbenchState.tab = id;
      renderWorkbenchDrawer();
      refreshWorkbenchTab().catch(report);
    }, "wb-tab" + (workbenchState.tab === id ? " active" : ""));
    tabs.appendChild(tBtn);
  }
  panel.appendChild(tabs);

  const body = el("div", null, "wb-body");
  if (workbenchState.tab === "tasks") renderTaskBoardsTab(body);
  else if (workbenchState.tab === "artifacts") renderArtifactsTab(body);
  else if (workbenchState.tab === "diagnostics") renderDiagnosticsTab(body);
  else if (workbenchState.tab === "releases") renderReleasesTab(body);
  else if (workbenchState.tab.startsWith("plugin:") && globalThis.XuanpuPluginHost) {
    const pid = workbenchState.tab.slice("plugin:".length);
    const plugin = XuanpuPluginHost.get(pid);
    if (plugin) {
      if (plugin.mode === "sandbox_iframe") {
        XuanpuPluginHost.mountSandboxIframe(body, plugin, { slot: "aux-tab" });
      } else if (plugin.render) {
        plugin.render(body, { slot: "aux-tab", plugin });
      }
    } else {
      body.appendChild(el("p", "该插件已卸载或不再可用。", "meta"));
    }
  }
  panel.appendChild(body);

  overlay.replaceChildren(panel);
}

function renderTaskBoardsTab(container) {
  if (!workbenchState.boards.length) {
    container.appendChild(el("p", "暂无跨轮任务板记录。复杂多步任务进行时会自动在此实时同步焦点与 DAG 步骤。", "meta"));
    return;
  }
  for (const board of workbenchState.boards) {
    const cardNode = el("article", null, "wb-card");
    const top = el("div", null, "wb-card-head");
    top.append(el("strong", board.title || "任务板"), el("span", board.status || "active", "wb-badge " + (board.status || "active")));
    cardNode.appendChild(top);
    if (board.focus) cardNode.appendChild(el("p", "当前焦点：" + board.focus, "wb-focus-line"));
    const ul = el("ul", null, "wb-steps");
    for (const st of board.steps || []) {
      const li = el("li", null, "wb-step " + (st.status || "pending"));
      const icon = st.status === "done" ? "✓" : st.status === "in_progress" ? "▸" : st.status === "failed" ? "✕" : "○";
      const dep = Array.isArray(st.blocked_by) && st.blocked_by.length ? "（等待：" + st.blocked_by.join("、") + "）" : "";
      li.append(el("span", icon, "wb-step-icon"), el("span", (st.title || st.id) + dep));
      ul.appendChild(li);
    }
    cardNode.appendChild(ul);
    if (board.trace_id) {
      const actions = el("div", null, "wb-actions");
      actions.appendChild(button("查看本轮过程产物", () => {
        workbenchState.traceId = board.trace_id;
        workbenchState.tab = "artifacts";
        renderWorkbenchDrawer();
        refreshWorkbenchTab().catch(report);
      }));
      cardNode.appendChild(actions);
    }
    container.appendChild(cardNode);
  }
}

function renderArtifactsTab(container) {
  const bar = el("div", null, "wb-search-row");
  const input = el("input");
  input.placeholder = "输入 trace_id 查看过程产物…";
  input.value = workbenchState.traceId || "";
  const goBtn = button("查询", () => {
    workbenchState.traceId = input.value.trim() || null;
    workbenchState.preview = null;
    refreshWorkbenchTab().catch(report);
  }, "primary");
  bar.append(input, goBtn);
  container.appendChild(bar);

  if (!workbenchState.artifacts.length) {
    container.appendChild(el("p", "当前 trace 暂无过程产物。", "meta"));
  } else {
    for (const art of workbenchState.artifacts) {
      const cardNode = el("article", null, "wb-card");
      const head = el("div", null, "wb-card-head");
      head.append(el("strong", art.name), el("span", art.mime_type || "text/plain", "wb-badge"));
      cardNode.appendChild(head);
      if (art.contains_external_input) {
        cardNode.appendChild(el("p", "含外部联网输入（已做污点标记，仅只读展示）", "wb-taint"));
      }
      const actions = el("div", null, "wb-actions");
      actions.appendChild(button("预览正文", async () => {
        const prev = await entryApi("GET", "/v1/artifacts/" + encodeURIComponent(art.artifact_id) + "/preview");
        workbenchState.preview = { artifact: art, data: prev };
        renderWorkbenchDrawer();
      }));
      if (!art.promoted_item_id) {
        actions.appendChild(button("转存个人空间", async () => {
          await entryApi("POST", "/v1/artifacts/" + encodeURIComponent(art.artifact_id) + "/promote", {
            json: { request_id: requestId() },
          });
          announce("已转存到个人工作台");
          refreshWorkbenchTab().catch(report);
        }, "primary"));
      } else {
        actions.appendChild(el("span", "已转存正式文件", "meta"));
      }
      cardNode.appendChild(actions);
      container.appendChild(cardNode);
    }
  }

  if (workbenchState.preview) {
    const pv = workbenchState.preview;
    const box = el("section", null, "wb-preview");
    box.appendChild(el("h3", "产物预览：" + pv.artifact.name));
    const handled = globalThis.XuanpuPluginHost && XuanpuPluginHost.renderArtifact(box, pv.artifact, pv.data);
    if (!handled) {
      const pre = el("pre", String((pv.data && pv.data.text) || ""), "wb-code");
      box.appendChild(pre);
    }
    container.appendChild(box);
  }
}

function renderDiagnosticsTab(container) {
  const rows = workbenchState.failedTranscripts;
  if (!rows.length) {
    container.appendChild(el("p", "近期未发现失败或中断的执行 Transcript。", "meta"));
  } else {
    for (const item of rows) {
      const cardNode = el("article", null, "wb-card");
      const head = el("div", null, "wb-card-head");
      head.append(el("strong", item.span_id), el("span", item.status + (item.error_code ? ":" + item.error_code : ""), "wb-badge failed"));
      cardNode.appendChild(head);
      cardNode.appendChild(el("p", `trace=${item.trace_id} · 节点=${item.node_id} · 行数=${item.line_count}`, "meta"));
      const actions = el("div", null, "wb-actions");
      for (const mode of ["compact", "full"]) {
        actions.appendChild(button(mode === "compact" ? "读紧凑版" : "读完整脱敏版", async () => {
          const page = await entryApi(
            "GET",
            "/v1/traces/" + encodeURIComponent(item.trace_id) + "/transcripts/" + encodeURIComponent(item.span_id) + "?mode=" + mode
          );
          workbenchState.transcriptPage = page;
          renderWorkbenchDrawer();
        }));
      }
      cardNode.appendChild(actions);
      container.appendChild(cardNode);
    }
  }
  if (workbenchState.transcriptPage) {
    const pg = workbenchState.transcriptPage;
    const box = el("section", null, "wb-preview");
    box.appendChild(el("h3", `Transcript（${pg.mode} · 来源：${pg.source}）`));
    const text = (pg.lines || []).map((l, i) => `${i + 1}. [${l.kind}/${l.status}] ${JSON.stringify(l.payload)}`).join("\n");
    box.appendChild(el("pre", text || "（空）", "wb-code"));
    container.appendChild(box);
  }
}

function renderReleasesTab(container) {
  const bar = el("div", null, "wb-search-row");
  const input = el("input");
  input.placeholder = "输入匠人包名（如 support-desk）查看发布代次…";
  input.value = workbenchState.releasePackage || "";
  const goBtn = button("查询代次", () => {
    workbenchState.releasePackage = input.value.trim();
    refreshWorkbenchTab().catch(report);
  }, "primary");
  bar.append(input, goBtn);
  container.appendChild(bar);

  if (!workbenchState.releases.length) {
    container.appendChild(el("p", "输入匠人包名后可查看 L1 追加式不可变发布节点历史并执行活性核验回退。", "meta"));
    return;
  }
  for (const node of workbenchState.releases) {
    const cardNode = el("article", null, "wb-card");
    const head = el("div", null, "wb-card-head");
    head.append(el("strong", `R${node.generation} · ${node.target_name}@${node.version}`), el("span", node.state, "wb-badge " + node.state));
    cardNode.appendChild(head);
    const lineage = node.built_by ? ` · built_by forge=${node.built_by.forge_version}` : "";
    cardNode.appendChild(el("p", `release_id=${node.release_id}${lineage}`, "meta"));
    if (node.state !== "active") {
      const actions = el("div", null, "wb-actions");
      actions.appendChild(button("回退到此版本", async () => {
        await entryApi("POST", "/v1/releases/artisan/" + encodeURIComponent(node.target_name) + "/rollback", {
          json: { to_release_id: node.release_id, mode: "retired" },
        });
        announce("已追加新发布节点回退至 " + node.version);
        refreshWorkbenchTab().catch(report);
      }));
      cardNode.appendChild(actions);
    }
    container.appendChild(cardNode);
  }
}
