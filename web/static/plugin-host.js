// Web 主壳微内核前端插件宿主（PluginHost）、4 类标准挂载槽与白名单 postMessage Bridge（ADR 0027）。
// 铁律：沙盒 iframe 插件永远运行在无 allow-same-origin 的 sandbox="allow-scripts" 环境中，
// 不可触碰主站 Cookie、CSRF 与 DOM，所有能力必须经本宿主白名单 RPC 转发与后端权威鉴权。
"use strict";

(function initXuanpuPluginHost() {
  const VALID_SLOTS = new Set(["header-pill", "message-renderer", "aux-tab", "artifact-viewer"]);
  const registry = new Map();
  const mountedFrames = new Map();
  let bridgeListenerBound = false;

  function normalizeDef(def) {
    if (!def || typeof def.plugin_id !== "string" || !def.plugin_id.trim()) {
      throw new Error("前端插件缺少合法 plugin_id");
    }
    const slots = Array.isArray(def.slots) ? def.slots : (def.slot ? [def.slot] : []);
    const validSlots = slots.filter((s) => VALID_SLOTS.has(s));
    if (!validSlots.length) {
      throw new Error("前端插件必须声明至少一个合法槽位：" + def.plugin_id);
    }
    return {
      plugin_id: def.plugin_id.trim(),
      title: String(def.title || def.plugin_id),
      slots: validSlots,
      mode: def.mode === "sandbox_iframe" ? "sandbox_iframe" : "declarative",
      source_layer: def.source_layer === "L1_artisan" ? "L1_artisan" : "L2_builtin",
      package: def.package || null,
      version: String(def.version || "1.0.0"),
      entry_url: def.entry_url || null,
      mime_types: Array.isArray(def.mime_types) ? [...def.mime_types] : [],
      bridge_permissions: Array.isArray(def.bridge_permissions) ? [...def.bridge_permissions] : [],
      match: typeof def.match === "function" ? def.match : null,
      render: typeof def.render === "function" ? def.render : null,
      onUnmount: typeof def.onUnmount === "function" ? def.onUnmount : null,
      mountedNodes: new Set(),
    };
  }

  function register(def) {
    const item = normalizeDef(def);
    if (registry.has(item.plugin_id)) unregister(item.plugin_id);
    registry.set(item.plugin_id, item);
    return item;
  }

  // Clean Unmounting：彻底清理插件挂载 DOM、注销沙盒 iframe Bridge 句柄并执行自定义卸载回调。
  function unregister(pluginId) {
    const existing = registry.get(pluginId);
    if (!existing) return false;
    if (existing.onUnmount) {
      try { existing.onUnmount(); } catch (err) { console.error("插件卸载钩子异常", pluginId, err); }
    }
    for (const node of existing.mountedNodes) {
      if (node && typeof node.remove === "function") node.remove();
    }
    existing.mountedNodes.clear();
    for (const [win, meta] of mountedFrames.entries()) {
      if (meta.plugin_id === pluginId) mountedFrames.delete(win);
    }
    registry.delete(pluginId);
    return true;
  }

  function list(slot) {
    const out = [];
    for (const item of registry.values()) {
      if (!slot || item.slots.includes(slot)) out.push(item);
    }
    return out;
  }

  function get(pluginId) {
    return registry.get(pluginId) || null;
  }

  function resetTenantPlugins() {
    for (const item of [...registry.values()]) {
      if (item.source_layer === "L1_artisan") unregister(item.plugin_id);
    }
    mountedFrames.clear();
    const pillHost = typeof $ === "function" ? $("entry-plugin-pills") : null;
    if (pillHost) pillHost.replaceChildren();
  }

  function renderHeaderPills(container) {
    const target = container || (typeof $ === "function" ? $("entry-plugin-pills") : null);
    if (!target) return;
    target.replaceChildren();
    for (const plugin of list("header-pill")) {
      if (plugin.render) {
        const node = plugin.render(target, { slot: "header-pill", plugin });
        if (node) plugin.mountedNodes.add(node);
      } else if (plugin.entry_url) {
        const pill = button(plugin.title, () => {
          if (typeof openWorkbenchDrawer === "function") {
            openWorkbenchDrawer({ tab: "plugin:" + plugin.plugin_id }).catch(report);
          }
        }, "nav-pill plugin-slot-pill");
        target.appendChild(pill);
        plugin.mountedNodes.add(pill);
      }
    }
  }

  function renderMessageBlock(container, context) {
    for (const plugin of list("message-renderer")) {
      if (!plugin.render) continue;
      const matched = plugin.match
        ? plugin.match(context)
        : (context.mimeType && plugin.mime_types.includes(context.mimeType));
      if (!matched) continue;
      const rendered = plugin.render(container, context);
      if (rendered) {
        if (rendered !== true) plugin.mountedNodes.add(rendered);
        return true;
      }
    }
    return false;
  }

  function renderArtifact(container, artifact, previewData) {
    const mime = String((artifact && artifact.mime_type) || "").toLowerCase();
    const context = {
      slot: "artifact-viewer",
      artifact,
      preview: previewData,
      mimeType: mime,
      text: String((previewData && previewData.text) || ""),
    };
    for (const plugin of list("artifact-viewer")) {
      if (plugin.source_layer === "L1_artisan" && plugin.mode === "sandbox_iframe") {
        if (plugin.mime_types.length && plugin.mime_types.includes(mime)) {
          mountSandboxIframe(container, plugin, context);
          return true;
        }
        continue;
      }
      if (!plugin.render) continue;
      const matched = plugin.match
        ? plugin.match(context)
        : (mime && plugin.mime_types.some((m) => mime.includes(m)));
      if (!matched) continue;
      const rendered = plugin.render(container, context);
      if (rendered) {
        if (rendered !== true) plugin.mountedNodes.add(rendered);
        return true;
      }
    }
    return false;
  }

  // 安全挂载沙盒 iframe（严禁 allow-same-origin），并登记到 postMessage Bridge 句柄表。
  function mountSandboxIframe(container, pluginMeta, initContext = {}) {
    ensureBridgeListener();
    const frame = el("iframe", null, "wb-sandbox-frame");
    // 核心安全边界：仅允许沙盒内脚本运行，严禁同源权限（永不可读 Cookie/CSRF/主页面 DOM）
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    if (pluginMeta.entry_url) {
      frame.setAttribute("src", pluginMeta.entry_url);
    } else if (typeof initContext.text === "string") {
      frame.setAttribute("srcdoc", initContext.text);
    }
    container.appendChild(frame);
    const meta = {
      plugin_id: pluginMeta.plugin_id,
      package: pluginMeta.package || null,
      version: pluginMeta.version || "1.0.0",
      bridge_permissions: Array.isArray(pluginMeta.bridge_permissions) ? [...pluginMeta.bridge_permissions] : [],
      slot: initContext.slot || "artifact-viewer",
      frame,
      initContext,
    };
    const registerWindow = () => {
      if (frame.contentWindow) mountedFrames.set(frame.contentWindow, meta);
    };
    registerWindow();
    if (typeof frame.addEventListener === "function") {
      frame.addEventListener("load", () => {
        registerWindow();
        if (frame.contentWindow && typeof frame.contentWindow.postMessage === "function") {
          frame.contentWindow.postMessage({
            type: "xuanpu:init",
            plugin_id: meta.plugin_id,
            slot: meta.slot,
            artifact: initContext.artifact || null,
          }, "*");
        }
      });
    }
    const existing = registry.get(pluginMeta.plugin_id);
    if (existing) existing.mountedNodes.add(frame);
    return { frame, meta, registerWindow };
  }

  // 白名单 postMessage Bridge：严格校验来源句柄与插件声明权限，由宿主代调后端 RPC 网关。
  async function handleBridgeMessage(event) {
    const data = event && event.data;
    if (!data || typeof data !== "object" || typeof data.type !== "string") return false;
    const meta = event.source ? mountedFrames.get(event.source) : null;
    if (!meta) return false;

    if (data.type === "xuanpu:ready") {
      if (event.source && typeof event.source.postMessage === "function") {
        event.source.postMessage({
          type: "xuanpu:init",
          plugin_id: meta.plugin_id,
          slot: meta.slot,
          permissions: meta.bridge_permissions,
          artifact: meta.initContext ? meta.initContext.artifact || null : null,
        }, "*");
      }
      return true;
    }

    if (data.type !== "xuanpu:rpc") return false;
    const reqId = data.reqId || data.req_id || null;
    const method = String(data.method || "").trim();
    const reply = (payload) => {
      if (event.source && typeof event.source.postMessage === "function") {
        event.source.postMessage({ type: "xuanpu:rpc_result", reqId, ...payload }, "*");
      }
    };
    if (!meta.bridge_permissions.includes(method)) {
      reply({ ok: false, error: "permission_denied", message: "插件未获授权限：" + method });
      return true;
    }
    try {
      const res = await entryApi("POST", "/v1/plugins/bridge/rpc", {
        json: {
          plugin_id: meta.plugin_id,
          package: meta.package,
          version: meta.version,
          method,
          params: (data.params && typeof data.params === "object") ? data.params : {},
        },
      });
      reply({ ok: true, result: res.result });
      if (method === "workspace.propose" && res.result && res.result.summary && typeof announce === "function") {
        announce("插件建议已提交待确认：" + res.result.summary);
      }
    } catch (error) {
      reply({ ok: false, error: (error && error.code) || "rpc_failed", message: (error && error.message) || "RPC 调用失败" });
    }
    return true;
  }

  function ensureBridgeListener() {
    if (bridgeListenerBound) return;
    if (typeof globalThis.addEventListener === "function") {
      globalThis.addEventListener("message", (ev) => { handleBridgeMessage(ev).catch(() => {}); });
      bridgeListenerBound = true;
    }
  }

  // 登录后从后端 /v1/plugins/ui 动态发现并注册本租户生效的 L1 全栈匠人前端插件。
  async function loadRemotePlugins(epoch) {
    const res = await entryApi("GET", "/v1/plugins/ui");
    if (typeof isStale === "function" && isStale(epoch)) return [];
    const items = Array.isArray(res && res.items) ? res.items : [];
    const registered = [];
    for (const item of items) {
      if (item.source_layer !== "L1_artisan") continue;
      register(item);
      registered.push(item.plugin_id);
    }
    renderHeaderPills();
    if (typeof workbenchState !== "undefined" && workbenchState.open && typeof renderWorkbenchDrawer === "function") {
      renderWorkbenchDrawer();
    }
    return registered;
  }

  globalThis.XuanpuPluginHost = {
    register,
    unregister,
    list,
    get,
    resetTenantPlugins,
    renderHeaderPills,
    renderMessageBlock,
    renderArtifact,
    mountSandboxIframe,
    handleBridgeMessage,
    loadRemotePlugins,
    _mountedFrames: mountedFrames,
  };
})();
