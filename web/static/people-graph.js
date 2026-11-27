// 玄圃小院「书架 · 资料室」个人关系图谱模块：原生 SVG 径向 / 力导向布局、拖拽、缩放、筛选与聚焦穿透。
// 复用 /v1/people/graph 与 /v1/people/graph/{kind}/{id}；100% 原生 DOM/SVG + textContent，零外部依赖。
"use strict";

const AB_GRAPH_SVG_NS = "http://www.w3.org/2000/svg";
const AB_GRAPH_KIND_LABELS = { person: "人物", org: "公司", matter: "项目" };
const AB_GRAPH_EXTRA_REL_LABELS = {
  owner: "负责人",
  co_creator: "共创人",
  creator: "创建人",
  project_introducer: "项目介绍人",
  introducer: "介绍人",
  lead: "负责人",
  member: "成员",
  participant: "参与",
  decision_maker: "决策人",
  contact: "对接人",
  sponsor: "发起人",
  investor: "投资方",
  mentor: "导师",
  boss: "上级",
  affiliated_with: "关联机构",
  works_for: "任职于",
};

const abGraphState = {
  mode: "full", // "full" | "focus"
  focus: null, // { kind: "person"|"org"|"matter", id: string }
  depth: 2,
  includeIsolated: false,
  autoFallbackDone: false,
  inferredOrgEdges: true,
  filters: { person: true, org: true, matter: true },
  query: "",
  loading: false,
  error: "",
  gen: 0,
  raw: null,
  selectedNodeKey: null,
  selectedEdgeId: null,
  highlightActive: false, // 仅用户主动点击选中某节点/边时才淡化其余节点，避免聚焦默认态把 2 跳变暗
  layoutKey: "",
  positions: new Map(),
  camera: { x: 340, y: 250, scale: 1 },
};

function abGraphSvgEl(tag, attrs, text) {
  const node = typeof document.createElementNS === "function"
    ? document.createElementNS(AB_GRAPH_SVG_NS, tag)
    : document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v !== undefined && v !== null) node.setAttribute(k, String(v));
    }
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function abGraphNodeKey(kind, id) {
  return String(kind || "person") + ":" + String(id || "");
}

function abGraphRelText(relType, role) {
  const rawType = String(relType || "").trim();
  const cleanRole = String(role || "").trim();
  const mapped = (typeof AB_REL_LABELS === "object" && AB_REL_LABELS && AB_REL_LABELS[rawType])
    || AB_GRAPH_EXTRA_REL_LABELS[rawType.toLowerCase()]
    || "";
  const isAsciiSlug = /^[a-z0-9_ -]+$/i.test(rawType);
  if (cleanRole) {
    if (!mapped && isAsciiSlug) return cleanRole;
    if (mapped && (cleanRole.includes(mapped) || mapped.includes(cleanRole))) return cleanRole;
    if (mapped === "参与事项" || mapped === "关联事项") return cleanRole;
    return mapped ? mapped + " · " + cleanRole : cleanRole;
  }
  return mapped || rawType || "关联";
}

function abGraphHashUnit(str) {
  const s = String(str || "");
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10000) / 10000;
}

function abGraphShort(text, maxLen) {
  const s = String(text || "");
  return s.length > maxLen ? s.slice(0, maxLen - 1) + "…" : s;
}

function abGraphTextWidth(text) {
  let w = 0;
  for (const ch of String(text || "")) {
    w += ch.charCodeAt(0) > 127 ? 11.2 : 6.6;
  }
  return Math.max(28, Math.ceil(w));
}

function resetPeopleGraphState() {
  abGraphState.mode = "full";
  abGraphState.focus = null;
  abGraphState.depth = 2;
  abGraphState.includeIsolated = false;
  abGraphState.autoFallbackDone = false;
  abGraphState.inferredOrgEdges = true;
  abGraphState.filters = { person: true, org: true, matter: true };
  abGraphState.query = "";
  abGraphState.loading = false;
  abGraphState.error = "";
  abGraphState.gen += 1;
  abGraphState.raw = null;
  abGraphState.selectedNodeKey = null;
  abGraphState.selectedEdgeId = null;
  abGraphState.highlightActive = false;
  abGraphState.layoutKey = "";
  abGraphState.positions = new Map();
  abGraphState.camera = { x: 340, y: 250, scale: 1 };
}

async function openEntityInGraph(kind, id) {
  if (!kind || !id) return;
  abState.tab = "graph";
  abState.editing = null;
  abState.addingNote = false;
  abState.status = "";
  abState.error = "";
  await loadPeopleGraph({ focus: { kind, id } });
}

async function loadPeopleGraph(options = {}) {
  const epoch = entry.epoch;
  const gen = ++abGraphState.gen;
  if (options.mode === "full") {
    abGraphState.mode = "full";
    abGraphState.focus = null;
    abGraphState.highlightActive = false;
  } else if (options.focus && options.focus.kind && options.focus.id) {
    abGraphState.mode = "focus";
    abGraphState.focus = { kind: options.focus.kind, id: options.focus.id };
    abGraphState.selectedNodeKey = abGraphNodeKey(options.focus.kind, options.focus.id);
    abGraphState.selectedEdgeId = null;
    abGraphState.highlightActive = false;
  }
  if (typeof options.depth === "number") {
    abGraphState.depth = Math.max(1, Math.min(3, options.depth));
  }
  if (typeof options.includeIsolated === "boolean") {
    abGraphState.includeIsolated = options.includeIsolated;
    abGraphState.autoFallbackDone = true;
  }
  abGraphState.loading = true;
  abGraphState.error = "";
  abGraphState.layoutKey = "";
  renderContactsDrawer();

  try {
    let url = "";
    if (abGraphState.mode === "focus" && abGraphState.focus) {
      url = "/v1/people/graph/"
        + encodeURIComponent(abGraphState.focus.kind) + "/"
        + encodeURIComponent(abGraphState.focus.id)
        + "?depth=" + encodeURIComponent(String(abGraphState.depth));
    } else {
      const params = new URLSearchParams({ limit: "240" });
      if (abGraphState.includeIsolated) params.set("include_isolated", "true");
      url = "/v1/people/graph?" + params.toString();
    }
    const body = await entryApi("GET", url);
    if (isStale(epoch) || !abState.open || gen !== abGraphState.gen) return;

    // 若全景模式尚未建立显式关系，但本人已有联系人/公司/项目，自动切到含孤立点展示以便按同公司聚类浏览
    const st = body && body.stats;
    const totalEntities = st ? (st.total_persons || 0) + (st.total_orgs || 0) + (st.total_matters || 0) : 0;
    if (
      abGraphState.mode === "full"
      && !abGraphState.includeIsolated
      && !abGraphState.autoFallbackDone
      && (body.edges || []).length === 0
      && totalEntities > 0
    ) {
      abGraphState.autoFallbackDone = true;
      abGraphState.includeIsolated = true;
      return loadPeopleGraph({ mode: "full", includeIsolated: true });
    }

    abGraphState.raw = body || { nodes: [], edges: [] };
    abGraphState.loading = false;
    if (abGraphState.mode === "focus" && abGraphState.focus) {
      abGraphState.selectedNodeKey = abGraphNodeKey(abGraphState.focus.kind, abGraphState.focus.id);
      abGraphState.highlightActive = false;
    } else if (abGraphState.selectedNodeKey) {
      const exists = (abGraphState.raw.nodes || []).some(
        (n) => abGraphNodeKey(n.kind, n.id) === abGraphState.selectedNodeKey
      );
      if (!exists) {
        abGraphState.selectedNodeKey = null;
        abGraphState.highlightActive = false;
      }
    }
    renderContactsDrawer();
  } catch (error) {
    if (isStale(epoch) || (error && error.name === "AbortError") || gen !== abGraphState.gen) return;
    abGraphState.loading = false;
    abGraphState.error = (error && error.message) || "关系图加载失败";
    renderContactsDrawer();
  }
}

function abGraphBuildModel() {
  const raw = abGraphState.raw || { nodes: [], edges: [] };
  const nodeMap = new Map();
  for (const n of raw.nodes || []) {
    const key = abGraphNodeKey(n.kind, n.id);
    const cachedPerson = n.kind === "person" ? abState.peopleById.get(n.id) : null;
    const orgName = n.organization || (cachedPerson && (cachedPerson.organization || (cachedPerson.profile && cachedPerson.profile.organization))) || "";
    nodeMap.set(key, { ...n, key, organization: orgName });
  }

  const edges = [];
  const explicitPersonOrg = new Set();
  for (const e of raw.edges || []) {
    const srcKey = abGraphNodeKey(e.from_kind || "person", e.from_person_id);
    const dstKey = abGraphNodeKey(e.target_kind, e.target_id);
    if (!nodeMap.has(srcKey) || !nodeMap.has(dstKey)) continue;
    if ((e.from_kind || "person") === "person" && e.target_kind === "org") {
      explicitPersonOrg.add(e.from_person_id + "|" + e.target_id);
    } else if ((e.from_kind || "person") === "org" && e.target_kind === "person") {
      explicitPersonOrg.add(e.target_id + "|" + e.from_person_id);
    }
    edges.push({
      id: e.id,
      srcKey,
      dstKey,
      relation_type: e.relation_type,
      label: abGraphRelText(e.relation_type, e.role),
      role: e.role || "",
      note: e.note || "",
      valid_from: e.valid_from || null,
      valid_to: e.valid_to || null,
      is_symmetric: Boolean(e.is_symmetric),
      inferred: false,
    });
  }

  // 可选：根据人物档案中的「公司/机构」文本自动补齐同公司虚线聚合边
  if (abGraphState.inferredOrgEdges) {
    const orgByNorm = new Map();
    for (const n of nodeMap.values()) {
      if (n.kind === "org") orgByNorm.set(n.name.trim().toLowerCase(), n);
    }
    for (const o of abState.orgs || []) {
      const norm = String(o.name || "").trim().toLowerCase();
      if (norm && !orgByNorm.has(norm)) {
        const key = abGraphNodeKey("org", o.id);
        orgByNorm.set(norm, {
          kind: "org",
          id: o.id,
          key,
          name: o.name,
          subtitle: [o.profile && o.profile.industry, o.profile && o.profile.city].filter(Boolean).join(" · "),
          summary: (o.profile && o.profile.summary) || "",
          fromDrawer: true,
        });
      }
    }
    const textGroups = new Map();
    for (const n of nodeMap.values()) {
      if (n.kind !== "person") continue;
      const orgText = String(n.organization || "").trim();
      if (!orgText) continue;
      const norm = orgText.toLowerCase();
      if (orgByNorm.has(norm)) {
        const orgNode = orgByNorm.get(norm);
        if (!nodeMap.has(orgNode.key)) nodeMap.set(orgNode.key, orgNode);
        if (!explicitPersonOrg.has(n.id + "|" + orgNode.id)) {
          edges.push({
            id: "inf_org_" + n.id + "_" + orgNode.id,
            srcKey: n.key,
            dstKey: orgNode.key,
            relation_type: "同公司",
            label: "同公司",
            role: "",
            note: "由名片中的公司字段自动聚合",
            is_symmetric: false,
            inferred: true,
          });
        }
      } else {
        if (!textGroups.has(norm)) textGroups.set(norm, { name: orgText, persons: [] });
        textGroups.get(norm).persons.push(n);
      }
    }
    for (const [norm, group] of textGroups.entries()) {
      if (group.persons.length < 2) continue;
      const vid = "v_org_" + norm;
      const vkey = abGraphNodeKey("org", vid);
      nodeMap.set(vkey, {
        kind: "org",
        id: vid,
        key: vkey,
        name: group.name,
        subtitle: "按公司名自动聚合（" + group.persons.length + " 人）",
        summary: "",
        virtual: true,
      });
      for (const p of group.persons) {
        edges.push({
          id: "inf_vorg_" + p.id + "_" + norm,
          srcKey: p.key,
          dstKey: vkey,
          relation_type: "同公司",
          label: "同公司",
          role: "",
          note: "由名片中的公司字段自动聚合",
          is_symmetric: false,
          inferred: true,
        });
      }
    }
  }

  const centerKey = abGraphState.mode === "focus" && abGraphState.focus
    ? abGraphNodeKey(abGraphState.focus.kind, abGraphState.focus.id)
    : null;
  const visibleNodes = [];
  const visibleMap = new Map();
  for (const n of nodeMap.values()) {
    if (n.key === centerKey || abGraphState.filters[n.kind]) {
      visibleNodes.push(n);
      visibleMap.set(n.key, n);
    }
  }
  const visibleEdges = edges.filter((e) => visibleMap.has(e.srcKey) && visibleMap.has(e.dstKey));

  // 为同一对端点之间的多条边分配曲线偏移量，防止重叠
  const pairGroups = new Map();
  for (const e of visibleEdges) {
    const pairKey = e.srcKey < e.dstKey ? e.srcKey + "|" + e.dstKey : e.dstKey + "|" + e.srcKey;
    if (!pairGroups.has(pairKey)) pairGroups.set(pairKey, []);
    pairGroups.get(pairKey).push(e);
  }
  for (const group of pairGroups.values()) {
    group.forEach((e, idx) => {
      e.curveOffset = group.length === 1 ? 0 : (idx - (group.length - 1) / 2) * 44;
    });
  }

  const adj = new Map();
  for (const n of visibleNodes) adj.set(n.key, []);
  for (const e of visibleEdges) {
    adj.get(e.srcKey).push({ edge: e, otherKey: e.dstKey, outgoing: true });
    adj.get(e.dstKey).push({ edge: e, otherKey: e.srcKey, outgoing: false });
  }

  const q = abGraphState.query.trim().toLowerCase();
  const matchedKeys = new Set();
  if (q) {
    for (const n of visibleNodes) {
      const hay = [n.name, n.subtitle, n.organization, n.summary].filter(Boolean).join(" ").toLowerCase();
      if (hay.includes(q)) matchedKeys.add(n.key);
    }
  }

  return {
    nodes: visibleNodes,
    nodeMap: visibleMap,
    edges: visibleEdges,
    adj,
    centerKey,
    matchedKeys,
    records: raw.records || [],
    todos: raw.todos || [],
    events: raw.events || [],
    reminders: raw.reminders || [],
    stats: raw.stats || null,
    truncated: Boolean(raw.truncated),
  };
}

function abGraphSpreadAngles(items, minGap) {
  // 对同环节点按期望角度排序并推开过近的角度，避免节点重叠或共线
  if (items.length <= 1) return items;
  items.sort((a, b) => a.angle - b.angle);
  for (let pass = 0; pass < 4; pass += 1) {
    for (let i = 1; i < items.length; i += 1) {
      if (items[i].angle - items[i - 1].angle < minGap) {
        const mid = (items[i].angle + items[i - 1].angle) / 2;
        items[i - 1].angle = mid - minGap / 2;
        items[i].angle = mid + minGap / 2;
      }
    }
  }
  return items;
}

function abGraphLayoutFocus(model) {
  const pos = new Map();
  const dist = new Map([[model.centerKey, 0]]);
  const parent = new Map();
  const queue = [model.centerKey];
  while (queue.length) {
    const cur = queue.shift();
    const d = dist.get(cur);
    for (const item of model.adj.get(cur) || []) {
      if (!dist.has(item.otherKey)) {
        dist.set(item.otherKey, d + 1);
        parent.set(item.otherKey, cur);
        queue.push(item.otherKey);
      }
    }
  }

  const layers = new Map();
  for (const n of model.nodes) {
    const h = dist.has(n.key) ? dist.get(n.key) : 3;
    if (!layers.has(h)) layers.set(h, []);
    layers.get(h).push(n);
  }

  pos.set(model.centerKey, { x: 0, y: 0, pinned: true });
  const angles = new Map([[model.centerKey, 0]]);

  // 第 1 跳：横向展开，避免 2 个邻居落到正上/正下形成共线竖排
  const ring1 = layers.get(1) || [];
  const r1 = Math.max(185, ring1.length * 36);
  if (ring1.length === 1) {
    angles.set(ring1[0].key, 0);
    pos.set(ring1[0].key, { x: r1, y: 0 });
  } else if (ring1.length === 2) {
    // 左上与右下/右偏展布，既水平舒展，又保证两节点之间若有连线也不会穿过中心节点
    const preset = [-Math.PI * 0.78, Math.PI * 0.08];
    ring1.forEach((n, idx) => {
      const a = preset[idx];
      angles.set(n.key, a);
      pos.set(n.key, { x: Math.cos(a) * r1, y: Math.sin(a) * r1 });
    });
  } else if (ring1.length > 2) {
    const startAngle = -Math.PI * 0.18;
    ring1.forEach((n, idx) => {
      const a = startAngle + (2 * Math.PI * idx) / ring1.length;
      angles.set(n.key, a);
      pos.set(n.key, { x: Math.cos(a) * r1, y: Math.sin(a) * r1 });
    });
  }

  // 第 2、3 跳：严格沿各自父节点向外扇形延展，绝不跨过中心节点连线
  for (const h of [2, 3]) {
    const ring = layers.get(h) || [];
    if (!ring.length) continue;
    const byParent = new Map();
    for (const n of ring) {
      const pk = parent.get(n.key) || model.centerKey;
      if (!byParent.has(pk)) byParent.set(pk, []);
      byParent.get(pk).push(n);
    }
    const desired = [];
    for (const [pk, children] of byParent.entries()) {
      const baseAngle = angles.has(pk) ? angles.get(pk) : 0;
      const spread = Math.min(Math.PI * 0.65, Math.max(0.42, (children.length - 1) * 0.42));
      children.forEach((n, idx) => {
        const offset = children.length === 1
          ? 0
          : (idx / (children.length - 1) - 0.5) * spread;
        desired.push({ node: n, angle: baseAngle + offset });
      });
    }
    const r = r1 + (h - 1) * 185;
    abGraphSpreadAngles(desired, Math.min(0.48, 130 / r));
    for (const item of desired) {
      angles.set(item.node.key, item.angle);
      pos.set(item.node.key, {
        x: Math.cos(item.angle) * r,
        y: Math.sin(item.angle) * r,
      });
    }
  }

  abGraphResolveOverlaps(model.nodes, pos, 132, model.centerKey);
  return pos;
}

function abGraphResolveOverlaps(nodes, pos, minDist, fixedKey) {
  // 仅分离距离过近的节点，保留径向分层结构不被力导向拉乱
  for (let step = 0; step < 24; step += 1) {
    let moved = false;
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i], pa = pos.get(a.key);
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j], pb = pos.get(b.key);
        let dx = pb.x - pa.x;
        let dy = pb.y - pa.y;
        let d = Math.hypot(dx, dy);
        if (d >= minDist) continue;
        moved = true;
        if (d < 1) { dx = 1; dy = 0.6; d = 1.17; }
        const push = (minDist - d) * 0.52;
        const ux = dx / d, uy = dy / d;
        if (a.key !== fixedKey && b.key !== fixedKey) {
          pa.x -= ux * push * 0.5; pa.y -= uy * push * 0.5;
          pb.x += ux * push * 0.5; pb.y += uy * push * 0.5;
        } else if (a.key === fixedKey) {
          pb.x += ux * push; pb.y += uy * push;
        } else {
          pa.x -= ux * push; pa.y -= uy * push;
        }
      }
    }
    if (!moved) break;
  }
}

function abGraphEnsureLayout(model) {
  const sig = [
    abGraphState.mode,
    model.centerKey || "",
    model.nodes.map((n) => n.key).sort().join(","),
    model.edges.map((e) => e.id).sort().join(","),
  ].join("#");
  if (sig === abGraphState.layoutKey && abGraphState.positions.size > 0) return;

  let pos;
  if (model.centerKey && model.nodeMap.has(model.centerKey)) {
    pos = abGraphLayoutFocus(model);
  } else {
    pos = new Map();
    const connected = model.nodes.filter((n) => (model.adj.get(n.key) || []).length > 0);
    const isolated = model.nodes.filter((n) => (model.adj.get(n.key) || []).length === 0);
    const spread = Math.max(155, Math.sqrt(Math.max(1, connected.length)) * 96);
    // 起始角偏离 ±π/2，确保即使只有 2-4 个节点也不会退化为纯竖排共线
    connected.forEach((n, idx) => {
      const angle = -0.35 + (2 * Math.PI * idx) / Math.max(1, connected.length) + (abGraphHashUnit(n.key) - 0.5) * 0.28;
      const rad = spread * (0.55 + 0.45 * abGraphHashUnit(n.key + ":r"));
      pos.set(n.key, { x: Math.cos(angle) * rad, y: Math.sin(angle) * rad });
    });
    abGraphRelaxPositions(connected, model.edges, pos, connected.length > 80 ? 80 : 120, null);
    abGraphResolveOverlaps(connected, pos, 130, null);

    let baseY = 0;
    if (connected.length) {
      let maxY = -Infinity;
      for (const n of connected) maxY = Math.max(maxY, pos.get(n.key).y);
      baseY = maxY + 135;
    }
    const cols = Math.max(3, Math.min(7, Math.ceil(Math.sqrt(Math.max(1, isolated.length)) * 1.4)));
    isolated.forEach((n, idx) => {
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      pos.set(n.key, {
        x: (col - (cols - 1) / 2) * 130,
        y: baseY + row * 98,
      });
    });
  }

  abGraphState.positions = pos;
  abGraphState.layoutKey = sig;
  abGraphFitCamera(model.nodes, pos);
}

function abGraphRelaxPositions(nodes, edges, pos, iterations, fixedKey) {
  if (nodes.length <= 1) return;
  const k = Math.max(145, Math.min(200, 155 + Math.sqrt(nodes.length) * 6));
  const repulse = k * k * 1.45;
  const disp = new Map();
  for (let step = 0; step < iterations; step += 1) {
    for (const n of nodes) disp.set(n.key, { x: 0, y: 0 });
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i], pa = pos.get(a.key), da = disp.get(a.key);
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j], pb = pos.get(b.key), db = disp.get(b.key);
        let dx = pa.x - pb.x;
        let dy = pa.y - pb.y;
        let dist = Math.hypot(dx, dy);
        if (dist < 1) { dx = 1.2; dy = 0.7; dist = 1.39; }
        const force = repulse / (dist * dist);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        da.x += fx; da.y += fy;
        db.x -= fx; db.y -= fy;
      }
    }
    for (const e of edges) {
      const pa = pos.get(e.srcKey), pb = pos.get(e.dstKey);
      const da = disp.get(e.srcKey), db = disp.get(e.dstKey);
      if (!pa || !pb || !da || !db) continue;
      const dx = pb.x - pa.x;
      const dy = pb.y - pa.y;
      const dist = Math.max(1, Math.hypot(dx, dy));
      const ideal = e.inferred ? k * 1.08 : k;
      const force = (dist - ideal) * (e.inferred ? 0.08 : 0.14);
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      da.x += fx; da.y += fy;
      db.x -= fx; db.y -= fy;
    }
    const maxStep = Math.max(6, 48 * (1 - step / (iterations + 10)));
    for (const n of nodes) {
      if (n.key === fixedKey) continue;
      const p = pos.get(n.key);
      const d = disp.get(n.key);
      d.x -= p.x * 0.02;
      d.y -= p.y * 0.02;
      const len = Math.hypot(d.x, d.y);
      if (len > 0.01) {
        const move = Math.min(len, maxStep);
        p.x += (d.x / len) * move;
        p.y += (d.y / len) * move;
      }
    }
  }
}

function abGraphFitCamera(nodes, pos) {
  const vw = 680, vh = 500;
  if (!nodes.length) {
    abGraphState.camera = { x: vw / 2, y: vh / 2, scale: 1 };
    return;
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of nodes) {
    const p = pos.get(n.key);
    if (!p) continue;
    minX = Math.min(minX, p.x - 76);
    maxX = Math.max(maxX, p.x + 76);
    minY = Math.min(minY, p.y - 54);
    maxY = Math.max(maxY, p.y + 66);
  }
  const spanX = Math.max(300, maxX - minX);
  const spanY = Math.max(240, maxY - minY);
  const scale = Math.max(0.42, Math.min(1.08, Math.min((vw - 64) / spanX, (vh - 64) / spanY)));
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  abGraphState.camera = {
    x: vw / 2 - cx * scale,
    y: vh / 2 - cy * scale,
    scale,
  };
}

function abGraphEdgeGeometry(e, pa, pb, srcCenter, dstCenter) {
  const dx = pb.x - pa.x;
  const dy = pb.y - pa.y;
  const dist = Math.max(1, Math.hypot(dx, dy));
  const ux = dx / dist, uy = dy / dist;
  const startPad = srcCenter ? 27 : 22;
  const endPad = (dstCenter ? 27 : 22) + (e.is_symmetric || e.inferred ? 0 : 5);
  const sx = pa.x + ux * startPad;
  const sy = pa.y + uy * startPad;
  const ex = pb.x - ux * endPad;
  const ey = pb.y - uy * endPad;
  if (!e.curveOffset) {
    return {
      d: `M ${sx.toFixed(1)} ${sy.toFixed(1)} L ${ex.toFixed(1)} ${ey.toFixed(1)}`,
      lx: (sx + ex) / 2,
      ly: (sy + ey) / 2,
    };
  }
  const nx = -uy, ny = ux;
  const cx = (sx + ex) / 2 + nx * e.curveOffset;
  const cy = (sy + ey) / 2 + ny * e.curveOffset;
  return {
    d: `M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}`,
    lx: (sx + 2 * cx + ex) / 4,
    ly: (sy + 2 * cy + ey) / 4,
  };
}

function buildPeopleGraphPane() {
  const pane = el("div", null, "ab-graph-pane");
  const model = abGraphBuildModel();
  if (!abGraphState.loading && model.nodes.length) abGraphEnsureLayout(model);

  // 顶部工具栏：模式切换、跳数、类型过滤、开关与搜索定位
  const toolbar = el("div", null, "abg-toolbar");
  const modeGroup = el("div", null, "abg-group");
  modeGroup.appendChild(button("全景", () => loadPeopleGraph({ mode: "full" }), "abg-pill" + (abGraphState.mode === "full" ? " active" : "")));
  if (abGraphState.focus) {
    const centerNode = model.nodeMap.get(abGraphNodeKey(abGraphState.focus.kind, abGraphState.focus.id));
    const focusLabel = "聚焦：" + abGraphShort(centerNode ? centerNode.name : abGraphState.focus.id, 8);
    modeGroup.appendChild(button(focusLabel, () => loadPeopleGraph({ focus: abGraphState.focus }), "abg-pill" + (abGraphState.mode === "focus" ? " active" : "")));
    for (const d of [1, 2, 3]) {
      modeGroup.appendChild(button(d + "跳", () => loadPeopleGraph({ focus: abGraphState.focus, depth: d }), "abg-pill sm" + (abGraphState.depth === d ? " active" : "")));
    }
  }
  toolbar.appendChild(modeGroup);

  const filterGroup = el("div", null, "abg-group");
  for (const kind of ["person", "org", "matter"]) {
    const active = Boolean(abGraphState.filters[kind]);
    const btn = button(AB_GRAPH_KIND_LABELS[kind], () => {
      abGraphState.filters[kind] = !abGraphState.filters[kind];
      abGraphState.layoutKey = "";
      renderContactsDrawer();
    }, "abg-pill kind-" + kind + (active ? " active" : ""));
    filterGroup.appendChild(btn);
  }
  filterGroup.appendChild(button("同公司连线", () => {
    abGraphState.inferredOrgEdges = !abGraphState.inferredOrgEdges;
    abGraphState.layoutKey = "";
    renderContactsDrawer();
  }, "abg-pill" + (abGraphState.inferredOrgEdges ? " active" : "")));
  if (abGraphState.mode === "full") {
    filterGroup.appendChild(button("含孤立点", () => {
      loadPeopleGraph({ mode: "full", includeIsolated: !abGraphState.includeIsolated });
    }, "abg-pill" + (abGraphState.includeIsolated ? " active" : "")));
  }
  toolbar.appendChild(filterGroup);

  const searchGroup = el("div", null, "abg-group abg-search-group");
  const search = document.createElement("input");
  search.id = "ab-graph-search";
  search.type = "search";
  search.className = "abg-search";
  search.placeholder = "定位人物 / 公司 / 项目…";
  search.value = abGraphState.query;
  search.addEventListener("input", () => {
    abGraphState.query = search.value;
    renderContactsDrawer();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && model.matchedKeys.size > 0) {
      event.preventDefault();
      abGraphState.selectedNodeKey = [...model.matchedKeys][0];
      abGraphState.selectedEdgeId = null;
      abGraphState.highlightActive = true;
      renderContactsDrawer();
    }
  });
  searchGroup.appendChild(search);
  toolbar.appendChild(searchGroup);
  pane.appendChild(toolbar);

  if (abGraphState.error) pane.appendChild(el("p", abGraphState.error, "ab-banner error"));

  // 主体：左侧 SVG 关系画布 + 右侧详情/图例面板
  const workspace = el("div", null, "abg-workspace");
  const stage = el("div", null, "abg-stage");

  if (abGraphState.loading) {
    stage.appendChild(el("p", "正在绘制个人关系图谱…", "ab-empty"));
  } else if (!model.nodes.length) {
    const emptyMsg = abGraphState.mode === "full" && !abGraphState.includeIsolated
      ? "暂无显式关系边。可点上方「含孤立点」查看全部联系人，或直接告诉管家「张三和李四是同事、负责壁仞项目」自动建图。"
      : "还没有可展示的人物、公司或项目节点。直接在对话里记录联系人、会议或导入通讯录后即可生成关系图。";
    stage.appendChild(el("p", emptyMsg, "ab-empty"));
  } else {
    stage.appendChild(abGraphBuildSvg(model));
    const controls = el("div", null, "abg-zoom-bar");
    controls.append(
      button("+", () => abGraphZoomBy(1.22), "abg-zoom-btn"),
      button("−", () => abGraphZoomBy(0.82), "abg-zoom-btn"),
      button("适配", () => { abGraphFitCamera(model.nodes, abGraphState.positions); renderContactsDrawer(); }, "abg-zoom-btn")
    );
    stage.appendChild(controls);
  }

  workspace.append(stage, abGraphBuildInspector(model));
  pane.appendChild(workspace);
  return pane;
}

function abGraphZoomBy(factor) {
  const cam = abGraphState.camera;
  const cx = 340, cy = 250;
  const nextScale = Math.max(0.35, Math.min(2.8, cam.scale * factor));
  const ratio = nextScale / cam.scale;
  cam.x = cx - (cx - cam.x) * ratio;
  cam.y = cy - (cy - cam.y) * ratio;
  cam.scale = nextScale;
  renderContactsDrawer();
}

function abGraphBuildSvg(model) {
  const svg = abGraphSvgEl("svg", {
    class: "abg-svg",
    viewBox: "0 0 680 500",
    role: "img",
    "aria-label": "个人人物与关系图谱",
  });

  const defs = abGraphSvgEl("defs");
  const mkArrow = (id, cls) => {
    const m = abGraphSvgEl("marker", {
      id,
      viewBox: "0 0 10 10",
      refX: "8.5",
      refY: "5",
      markerWidth: "6",
      markerHeight: "6",
      orient: "auto-start-reverse",
    });
    m.appendChild(abGraphSvgEl("path", { d: "M 0 1.5 L 9 5 L 0 8.5 z", class: cls }));
    return m;
  };
  defs.append(mkArrow("abg-arrow", "abg-arrow-fill"), mkArrow("abg-arrow-active", "abg-arrow-fill active"));
  svg.appendChild(defs);

  const cam = abGraphState.camera;
  const viewport = abGraphSvgEl("g", {
    class: "abg-viewport",
    transform: `translate(${cam.x.toFixed(1)} ${cam.y.toFixed(1)}) scale(${cam.scale.toFixed(3)})`,
  });

  const selNode = abGraphState.selectedNodeKey;
  const selEdge = abGraphState.selectedEdgeId;
  const neighborKeys = new Set();
  const activeEdgeIds = new Set();
  if (abGraphState.highlightActive && selNode && model.adj.has(selNode)) {
    neighborKeys.add(selNode);
    for (const item of model.adj.get(selNode)) {
      neighborKeys.add(item.otherKey);
      activeEdgeIds.add(item.edge.id);
    }
  } else if (selEdge) {
    const found = model.edges.find((e) => e.id === selEdge);
    if (found) {
      activeEdgeIds.add(found.id);
      neighborKeys.add(found.srcKey);
      neighborKeys.add(found.dstKey);
    }
  }
  const hasFocusHighlight = neighborKeys.size > 0;
  const hasSearch = model.matchedKeys.size > 0;

  const edgeLayer = abGraphSvgEl("g", { class: "abg-edges" });
  const edgeRefs = new Map();
  for (const e of model.edges) {
    const pa = abGraphState.positions.get(e.srcKey);
    const pb = abGraphState.positions.get(e.dstKey);
    if (!pa || !pb) continue;
    const geom = abGraphEdgeGeometry(e, pa, pb, e.srcKey === model.centerKey, e.dstKey === model.centerKey);
    const isActive = activeEdgeIds.has(e.id);
    const isDim = hasFocusHighlight && !isActive;
    const g = abGraphSvgEl("g", {
      class: "abg-edge" + (e.inferred ? " inferred" : "") + (isActive ? " active" : "") + (isDim ? " dimmed" : ""),
    });
    const pathAttrs = { d: geom.d, class: "abg-edge-line" };
    if (!e.is_symmetric && !e.inferred) {
      pathAttrs["marker-end"] = isActive ? "url(#abg-arrow-active)" : "url(#abg-arrow)";
    }
    const line = abGraphSvgEl("path", pathAttrs);
    const hit = abGraphSvgEl("path", { d: geom.d, class: "abg-edge-hit" });

    // 边标签采用圆角胶囊底座 + 居中文字，确保深浅主题下都清晰不与连线互相干扰
    const shortLabel = abGraphShort(e.label, 10);
    const pillW = abGraphTextWidth(shortLabel) + 14;
    const pillH = 20;
    const badge = abGraphSvgEl("g", {
      class: "abg-edge-badge",
      transform: `translate(${geom.lx.toFixed(1)} ${geom.ly.toFixed(1)})`,
    });
    badge.append(
      abGraphSvgEl("rect", {
        x: (-pillW / 2).toFixed(1),
        y: (-pillH / 2).toFixed(1),
        width: pillW.toFixed(1),
        height: String(pillH),
        rx: "10",
        class: "abg-edge-pill",
      }),
      abGraphSvgEl("text", {
        y: "3.5",
        class: "abg-edge-label",
        "text-anchor": "middle",
      }, shortLabel)
    );
    g.append(line, hit, badge);
    g.addEventListener("click", (ev) => {
      ev.stopPropagation();
      abGraphState.selectedEdgeId = e.id;
      abGraphState.selectedNodeKey = null;
      abGraphState.highlightActive = true;
      renderContactsDrawer();
    });
    edgeLayer.appendChild(g);
    edgeRefs.set(e.id, { edge: e, line, hit, badge });
  }
  viewport.appendChild(edgeLayer);

  const nodeLayer = abGraphSvgEl("g", { class: "abg-nodes" });
  for (const n of model.nodes) {
    const p = abGraphState.positions.get(n.key);
    if (!p) continue;
    const isCenter = n.key === model.centerKey;
    const isSelected = n.key === selNode;
    const isMatch = model.matchedKeys.has(n.key);
    const isDim = (hasFocusHighlight && !neighborKeys.has(n.key)) || (hasSearch && !isMatch && !isSelected);
    const cls = [
      "abg-node",
      "kind-" + n.kind,
      n.virtual ? "virtual" : "",
      isCenter ? "center" : "",
      isSelected ? "selected" : "",
      isMatch ? "matched" : "",
      isDim ? "dimmed" : "",
    ].filter(Boolean).join(" ");

    const g = abGraphSvgEl("g", {
      class: cls,
      transform: `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`,
    });
    g.dataset.nodeKey = n.key;
    const radius = isCenter ? 24 : 19;
    if (n.kind === "org") {
      const sz = radius * 1.9;
      g.appendChild(abGraphSvgEl("rect", {
        x: (-sz / 2).toFixed(1),
        y: (-sz / 2).toFixed(1),
        width: sz.toFixed(1),
        height: sz.toFixed(1),
        rx: "9",
        class: "abg-shape",
      }));
    } else if (n.kind === "matter") {
      const s = radius * 1.48;
      g.appendChild(abGraphSvgEl("rect", {
        x: (-s / 2).toFixed(1),
        y: (-s / 2).toFixed(1),
        width: s.toFixed(1),
        height: s.toFixed(1),
        rx: "6",
        transform: "rotate(45)",
        class: "abg-shape",
      }));
    } else {
      g.appendChild(abGraphSvgEl("circle", { r: String(radius), class: "abg-shape" }));
    }

    const glyph = (n.name || "?").trim().slice(0, 1);
    g.appendChild(abGraphSvgEl("text", { y: "5", "text-anchor": "middle", class: "abg-glyph" }, glyph));

    const labelText = abGraphShort(n.name, 12);
    const labelW = abGraphTextWidth(labelText) + 12;
    const labelY = radius + 16;
    g.append(
      abGraphSvgEl("rect", {
        x: (-labelW / 2).toFixed(1),
        y: (labelY - 12).toFixed(1),
        width: labelW.toFixed(1),
        height: "18",
        rx: "6",
        class: "abg-label-bg",
      }),
      abGraphSvgEl("text", {
        y: String(labelY),
        "text-anchor": "middle",
        class: "abg-label",
      }, labelText)
    );
    g.appendChild(abGraphSvgEl("title", null, n.name + (n.subtitle ? " · " + n.subtitle : "")));

    abGraphBindNodeDrag(svg, g, n, model, edgeRefs);
    nodeLayer.appendChild(g);
  }
  viewport.appendChild(nodeLayer);
  svg.appendChild(viewport);
  abGraphBindCanvasPanZoom(svg, viewport);
  return svg;
}

function abGraphClientToSvg(svg, clientX, clientY) {
  if (typeof svg.getBoundingClientRect !== "function") return { x: clientX, y: clientY };
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return { x: clientX, y: clientY };
  return {
    x: ((clientX - rect.left) / rect.width) * 680,
    y: ((clientY - rect.top) / rect.height) * 500,
  };
}

function abGraphBindNodeDrag(svg, g, node, model, edgeRefs) {
  let dragging = false;
  let moved = false;
  let startClient = null;
  let startPos = null;

  g.addEventListener("pointerdown", (ev) => {
    ev.stopPropagation();
    dragging = true;
    moved = false;
    startClient = { x: ev.clientX, y: ev.clientY };
    const p = abGraphState.positions.get(node.key);
    startPos = { x: p.x, y: p.y };
    if (typeof g.setPointerCapture === "function") g.setPointerCapture(ev.pointerId);
  });

  g.addEventListener("pointermove", (ev) => {
    if (!dragging || !startClient || !startPos) return;
    const dxPx = ev.clientX - startClient.x;
    const dyPx = ev.clientY - startClient.y;
    if (Math.hypot(dxPx, dyPx) > 4) moved = true;
    if (!moved) return;
    const p0 = abGraphClientToSvg(svg, startClient.x, startClient.y);
    const p1 = abGraphClientToSvg(svg, ev.clientX, ev.clientY);
    const nextX = startPos.x + (p1.x - p0.x) / abGraphState.camera.scale;
    const nextY = startPos.y + (p1.y - p0.y) / abGraphState.camera.scale;
    const cur = abGraphState.positions.get(node.key);
    cur.x = nextX;
    cur.y = nextY;
    g.setAttribute("transform", `translate(${nextX.toFixed(1)} ${nextY.toFixed(1)})`);
    for (const item of model.adj.get(node.key) || []) {
      const ref = edgeRefs.get(item.edge.id);
      if (!ref) continue;
      const pa = abGraphState.positions.get(item.edge.srcKey);
      const pb = abGraphState.positions.get(item.edge.dstKey);
      if (!pa || !pb) continue;
      const geom = abGraphEdgeGeometry(
        item.edge,
        pa,
        pb,
        item.edge.srcKey === model.centerKey,
        item.edge.dstKey === model.centerKey
      );
      ref.line.setAttribute("d", geom.d);
      ref.hit.setAttribute("d", geom.d);
      ref.badge.setAttribute("transform", `translate(${geom.lx.toFixed(1)} ${geom.ly.toFixed(1)})`);
    }
  });

  const finish = () => {
    if (!dragging) return;
    dragging = false;
    if (!moved) {
      abGraphState.selectedNodeKey = node.key;
      abGraphState.selectedEdgeId = null;
      abGraphState.highlightActive = true;
      renderContactsDrawer();
    }
  };
  g.addEventListener("pointerup", finish);
  g.addEventListener("pointercancel", () => { dragging = false; });
  g.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!moved && (abGraphState.selectedNodeKey !== node.key || !abGraphState.highlightActive)) {
      abGraphState.selectedNodeKey = node.key;
      abGraphState.selectedEdgeId = null;
      abGraphState.highlightActive = true;
      renderContactsDrawer();
    }
  });
  g.addEventListener("dblclick", (ev) => {
    ev.stopPropagation();
    if (!node.virtual) loadPeopleGraph({ focus: { kind: node.kind, id: node.id } });
  });
}

function abGraphBindCanvasPanZoom(svg, viewport) {
  let panning = false;
  let moved = false;
  let startClient = null;
  let startCam = null;

  svg.addEventListener("pointerdown", (ev) => {
    panning = true;
    moved = false;
    startClient = { x: ev.clientX, y: ev.clientY };
    startCam = { x: abGraphState.camera.x, y: abGraphState.camera.y };
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!panning || !startClient || !startCam) return;
    if (Math.hypot(ev.clientX - startClient.x, ev.clientY - startClient.y) > 4) moved = true;
    if (!moved) return;
    const p0 = abGraphClientToSvg(svg, startClient.x, startClient.y);
    const p1 = abGraphClientToSvg(svg, ev.clientX, ev.clientY);
    abGraphState.camera.x = startCam.x + (p1.x - p0.x);
    abGraphState.camera.y = startCam.y + (p1.y - p0.y);
    const cam = abGraphState.camera;
    viewport.setAttribute("transform", `translate(${cam.x.toFixed(1)} ${cam.y.toFixed(1)}) scale(${cam.scale.toFixed(3)})`);
  });
  svg.addEventListener("pointerup", () => {
    if (!panning) return;
    panning = false;
    if (!moved && (abGraphState.selectedNodeKey || abGraphState.selectedEdgeId || abGraphState.highlightActive)) {
      abGraphState.selectedNodeKey = null;
      abGraphState.selectedEdgeId = null;
      abGraphState.highlightActive = false;
      renderContactsDrawer();
    }
  });
  svg.addEventListener("pointercancel", () => { panning = false; });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 0.89;
    const pt = abGraphClientToSvg(svg, ev.clientX, ev.clientY);
    const cam = abGraphState.camera;
    const nextScale = Math.max(0.35, Math.min(2.8, cam.scale * factor));
    const ratio = nextScale / cam.scale;
    cam.x = pt.x - (pt.x - cam.x) * ratio;
    cam.y = pt.y - (pt.y - cam.y) * ratio;
    cam.scale = nextScale;
    viewport.setAttribute("transform", `translate(${cam.x.toFixed(1)} ${cam.y.toFixed(1)}) scale(${cam.scale.toFixed(3)})`);
  }, { passive: false });
}

function abGraphOpenEntityDetail(node) {
  if (!node || node.virtual) return;
  if (node.kind === "person") {
    abState.tab = "people";
    loadPersonDetail(node.id);
  } else if (node.kind === "org") {
    abState.tab = "orgs";
    loadOrgDetail(node.id);
  } else if (node.kind === "matter") {
    abState.tab = "matters";
    loadMatterDetail(node.id);
  }
}

function abGraphBuildInspector(model) {
  const side = el("aside", null, "abg-inspector");
  const node = abGraphState.selectedNodeKey ? model.nodeMap.get(abGraphState.selectedNodeKey) : null;
  const edge = abGraphState.selectedEdgeId ? model.edges.find((e) => e.id === abGraphState.selectedEdgeId) : null;

  if (node) {
    const head = el("div", null, "abg-insp-head");
    head.append(
      el("span", AB_GRAPH_KIND_LABELS[node.kind] || node.kind, "abg-kind-badge kind-" + node.kind),
      el("h4", node.name, "abg-insp-title")
    );
    side.appendChild(head);
    if (node.subtitle) side.appendChild(el("p", node.subtitle, "ab-muted"));
    if (node.summary) side.appendChild(el("p", node.summary, "abg-insp-summary"));

    const act = el("div", null, "abg-insp-actions");
    if (!node.virtual && node.key !== model.centerKey) {
      act.appendChild(button("以它为中心", () => loadPeopleGraph({ focus: { kind: node.kind, id: node.id } }), "ab-mini-btn primary"));
    }
    if (!node.virtual) {
      act.appendChild(button("查看档案", () => abGraphOpenEntityDetail(node), "ab-mini-btn"));
      act.appendChild(button("问管家", () => {
        closeContactsDrawer();
        const box = $("message");
        if (box) { box.value = `梳理一下「${node.name}」的关联人物、公司和项目脉络`; box.focus(); }
      }, "ab-mini-btn"));
    }
    if (act.children.length) side.appendChild(act);

    const rels = model.adj.get(node.key) || [];
    side.appendChild(el("h5", `关联关系（${rels.length}）`, "ab-sec-title"));
    if (!rels.length) {
      side.appendChild(el("p", "当前视图下暂无连线关系。", "ab-muted"));
    } else {
      const list = el("div", null, "abg-rel-list");
      for (const item of rels) {
        const peer = model.nodeMap.get(item.otherKey);
        if (!peer) continue;
        const row = button("", () => {
          abGraphState.selectedNodeKey = peer.key;
          abGraphState.selectedEdgeId = null;
          abGraphState.highlightActive = true;
          renderContactsDrawer();
        }, "abg-rel-item");
        row.append(
          el("span", item.edge.label, "abg-rel-tag" + (item.edge.inferred ? " inferred" : "")),
          el("strong", peer.name, "abg-rel-name")
        );
        list.appendChild(row);
      }
      side.appendChild(list);
    }
  } else if (edge) {
    const src = model.nodeMap.get(edge.srcKey);
    const dst = model.nodeMap.get(edge.dstKey);
    side.append(
      el("span", edge.inferred ? "自动聚合" : "关系边", "abg-kind-badge"),
      el("h4", `${src ? src.name : edge.srcKey} ${edge.is_symmetric ? "↔" : "→"} ${dst ? dst.name : edge.dstKey}`, "abg-insp-title"),
      el("p", "关系：" + edge.label, "ab-muted")
    );
    if (edge.valid_from || edge.valid_to) {
      side.appendChild(el("p", `时间：${edge.valid_from || "早先"} ~ ${edge.valid_to || "至今"}`, "ab-muted"));
    }
    if (edge.note) side.appendChild(el("p", edge.note, "abg-insp-summary"));
  } else {
    side.appendChild(el("h4", abGraphState.mode === "focus" ? "局部关系图" : "个人关系全景", "abg-insp-title"));
    const explicitCount = model.edges.filter((e) => !e.inferred).length;
    const inferredCount = model.edges.length - explicitCount;
    side.appendChild(el(
      "p",
      `当前展示 ${model.nodes.length} 个节点、${explicitCount} 条关系` + (inferredCount ? `（含 ${inferredCount} 条同公司聚合）` : ""),
      "ab-muted"
    ));
    if (model.truncated) {
      side.appendChild(el("p", "节点较多，已按关系紧密度优先展示前 240 个，可用搜索或从人物详情点击「聚焦」深入查看。", "ab-muted"));
    }
    const legend = el("div", null, "abg-legend");
    legend.append(
      el("span", "● 人物", "abg-leg person"),
      el("span", "■ 公司", "abg-leg org"),
      el("span", "◆ 项目", "abg-leg matter"),
      el("span", "┄ 同公司", "abg-leg inferred")
    );
    side.appendChild(legend);
    side.appendChild(el("p", "点击节点查看关系详情，双击节点以它为中心展开 1–3 跳，拖拽节点可微调布局，滚轮缩放画布。", "ab-muted"));
  }

  if (abGraphState.mode === "focus" && model.records.length) {
    side.appendChild(el("h5", `溯源记录（${model.records.length}）`, "ab-sec-title"));
    const recBox = el("div", null, "abg-rec-list");
    for (const r of model.records.slice(0, 4)) {
      const card = el("div", null, "abg-rec-item");
      const dt = typeof abFormatDate === "function" ? abFormatDate(r.occurred_at) : "";
      card.append(
        el("strong", r.title || dt || "原始记录", "abg-rec-title"),
        el("p", abGraphShort(r.summary || r.content || "", 72), "ab-muted")
      );
      recBox.appendChild(card);
    }
    side.appendChild(recBox);
  }

  return side;
}
