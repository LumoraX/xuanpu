// 概览：今天、等我处理、进行中、未读通知。只读 /v1/dashboard，不调模型；
// 某一块读取失败只在该块显示失败；点击摘要在对话里展开对应卡片。
"use strict";

const BLOCK_NAMES = { today: "今天", waiting: "等我处理", in_progress: "进行中", unread: "未读通知" };
let overviewGeneration = 0;

async function refreshOverview() {
  const epoch = entry.epoch, generation = ++overviewGeneration;
  const body = await entryApi("GET", "/v1/dashboard?timezone=" + encodeURIComponent(timezone()));
  // 后发的刷新先回来时，旧结果不覆盖新结果
  if (isStale(epoch) || generation !== overviewGeneration) return body;
  $("overview").replaceChildren(...(body.blocks || []).map(blockNode));
  if (typeof renderTips === "function") renderTips(body);
  return body;
}

function blockNode(block) {
  const node = el("article", null, "block" + (block.status === "ok" ? "" : " error"));
  node.dataset.key = block.key;
  node.appendChild(el("h2", BLOCK_NAMES[block.key] || block.key));
  if (block.status !== "ok") {
    node.appendChild(el("div", "暂时读不出", "count"));
    return node;
  }
  node.appendChild(el("div", block.count, "count"));
  const list = el("ul");
  for (const item of block.items || []) {
    const row = el("li");
    const title = item.title === "butler" ? "管家" : item.title;
    row.appendChild(button(title + (item.waiting_me ? "（等你）" : ""), () => openSummary(item)));
    list.appendChild(row);
  }
  node.appendChild(list);
  if (block.key === "unread") node.appendChild(button("全部通知", () => appendCard(notificationCard()), "link"));
  return node;
}

// 点击摘要：在对话里展开对应卡片。
function openSummary(item) {
  const handlers = {
    reminder: () => appendCard(listCard("reminder")),
    event: () => appendCard(listCard("event")),
    todo: () => appendCard(listCard("todo")),
    proposal: () => openProposal(item.ref),
    question: () => openPending(item.ref),
    approval: () => openPending(item.ref),
    ruling: () => openPending(item.ref),
    work_task: () => appendCard(workTaskCard(item.ref)),
    approval_request: () => appendCard(moreCard("collaboration")),
    trace: () => openProgressCard(item.ref, { follow: true }),
    notification: () => appendCard(notificationCard()),
  };
  const handler = handlers[item.kind];
  if (handler) handler();
}

async function openProposal(proposalId) {
  const existing = entry.cards.get("proposal:" + proposalId);
  if (existing) { existing.scrollIntoView({ block: "center" }); return; }
  const epoch = entry.epoch;
  try {
    const saved = await entryApi("GET", "/v1/assistant/proposals/" + encodeURIComponent(proposalId));
    if (isStale(epoch) || saved.state !== "pending") return;
    renderRouteCards({ ...saved.route, proposal_id: proposalId });
  } catch (error) { report(error); }
}

async function openPending(pendingId) {
  const epoch = entry.epoch;
  try {
    const body = await entryApi("GET", "/v1/assistant/pending");
    if (isStale(epoch)) return;
    const item = (body.items || []).find(i => i.pending_id === pendingId);
    if (item) appendCard(answerCard(item));
    else announce("这件事已经处理完了");
  } catch (error) { report(error); }
}
