// 运行真实提醒脚本，以可控请求和时钟覆盖编辑器竞争及首次断线恢复。
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element(tag = "div") {
  const classes = new Set();
  return {
    tag, children: [], listeners: {}, attributes: {}, dataset: {}, value: "",
    textContent: "", disabled: false, checked: false,
    classList: {add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value), toggle: (value, yes) => yes ? classes.add(value) : classes.delete(value)},
    set innerHTML(value) {assert.equal(value, ""); this.children = [];},
    appendChild(child) {
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(node => node !== child);
      this.children.push(child); child.parentNode = this; return child;
    },
    prepend(child) {this.children.unshift(child);},
    setAttribute(name, value) {this.attributes[name] = value;},
    addEventListener(name, callback) {this.listeners[name] = callback;},
  };
}

function harness() {
  const nodes = new Map(), timers = new Map(), streams = [], errors = [], messages = [];
  let timerId = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const context = vm.createContext({
    console, crypto, AbortController, DOMException,
    document: {createElement: element}, $: node,
    conn: {me: {user_id: "employee"}},
    api: async () => ({items: []}),
    append: (role, text) => messages.push({role, text}),
    fail: error => {if (error.name !== "AbortError") errors.push(error);},
    when: seconds => String(seconds), url: value => value,
    requestOptions: () => ({}), headers: () => ({}),
    setTimeout: (callback, delay) => {
      const id = ++timerId; timers.set(id, {callback, delay}); return id;
    },
    clearTimeout: id => timers.delete(id),
    // 保持连接待定，退出通过真实 AbortController 中止；没有实际网络请求。
    fetch: (url, options) => new Promise((resolve, reject) => {
      streams.push({url, signal: options.signal});
      options.signal.addEventListener("abort", () => reject(new DOMException("退出", "AbortError")));
    }),
  });
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], "reminders.js"), "utf8"), context);
  return {context, node, timers, streams, errors, messages,
    run: code => vm.runInContext(code, context)};
}

function newDraft(title) {
  return {title, due_at: 2000000000, timezone: "UTC", repeat: "once", channels: ["inbox"]};
}

function showDraft(h, title) {
  h.context.nextDraft = newDraft(title);
  h.run("showReminderDraft(nextDraft)");
}

function findChild(node, predicate) {
  for (const child of node.children) {
    if (predicate(child)) return child;
    const nested = findChild(child, predicate);
    if (nested) return nested;
  }
  return null;
}

async function olderSubmissionKeepsNewDraft(mode, changedConversation = false) {
  const h = harness();
  const posted = [];
  let finish;
  h.context.api = async (method, url, options) => {
    if (method === "POST" || method === "PUT") {
      posted.push({method, url, body: options.json});
      return await new Promise(resolve => {finish = resolve;});
    }
    return {items: []};
  };
  let pending;
  if (mode === "draft") {
    showDraft(h, "较早草稿 A");
    pending = findChild(h.node("reminder-preview"), node => node.id === "confirm-reminder").listeners.click();
  } else {
    h.context.existing = {...newDraft("较早提醒 A"), reminder_id: "a", version: 1};
    h.run(mode === 'manual' ? 'editReminder()' : 'editReminder(existing)');
    const form = h.node("reminder-preview").children[0];
    findChild(form, node => node.attributes["aria-label"] === "提醒内容").value = "较早提醒 A";
    pending = form.listeners.submit({preventDefault() {}});
  }
  assert.equal(posted.length, 1, `${mode} 必须已发送旧提交`);
  if (changedConversation) h.run('dismissReminderContext()');
  showDraft(h, "新的草稿 B");
  const laterCard = h.node("reminder-preview").children[0];
  const laterRequest = h.run("reminderState.draft.request_id");
  finish({reminder_id: "a"});
  await pending;
  assert.equal(h.run("reminderState.draft?.title"), "新的草稿 B", `${mode} 旧提交不能吞掉新草稿`);
  assert.equal(h.run("reminderState.draft.request_id"), laterRequest);
  assert.equal(h.node("reminder-preview").children[0], laterCard);
  assert.equal(h.node("reminder-preview").classList.contains("hidden"), false);
  assert.equal(h.errors.length, 0);
  if (changedConversation) assert.equal(h.messages.length, 0, '旧对话提交结果不能插入新对话');
  else if (mode === "draft") assert(h.messages.some(item => item.text.includes("较早草稿 A")));
}

async function initialFailureRecovers(failingPath) {
  const h = harness();
  let failed = false;
  const calls = [];
  h.context.api = async (method, url) => {
    calls.push(url);
    if (url === failingPath && !failed) {
      failed = true; throw new Error("模拟首次读取失败");
    }
    if (url === "/v1/reminders/channels") return {channels: ["inbox", "feishu"]};
    if (url === "/v1/reminders?status=active") return {items: [{...newDraft("恢复后的提醒"), reminder_id: "restored", status: "completed"}]};
    return {items: [{notification_id: 1, title: "恢复后的通知", due_at: 2000000000, read_at: 2000000001, deliveries: [{channel: "feishu", status: "sent"}]}]};
  };
  await h.run("startReminders()");
  assert(failed, `必须命中 ${failingPath} 的首次错误`);
  assert.equal(h.streams.length, 1, "首次读取失败不能阻止 SSE 启动");
  const timer = [...h.timers.entries()].find(([, value]) => value.delay === 15000);
  assert(timer, "首次读取失败后仍须安排补查");
  h.timers.delete(timer[0]);
  await timer[1].callback();
  assert.equal(h.run('reminderState.channels.includes("feishu")'), true);
  assert(findChild(h.node("reminder-list"), node => node.textContent === "恢复后的提醒"));
  assert(findChild(h.node("notification-list"), node => node.textContent === "飞书：已发送"));
  const nextPoll = [...h.timers.values()].find(value => value.delay === 15000);
  assert(nextPoll, "恢复后应继续检查外部送达状态");
  h.run("resetReminders()");
  const callCount = calls.length;
  await nextPoll.callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, callCount, "退出后已经排队的旧回调不能继续请求");
  assert.equal(h.timers.size, 0, "退出应取消所有补查和重连定时器");
  assert.equal(h.streams[0].signal.aborted, true);
  assert.equal(h.errors.length, 0);
}

async function capabilitiesStayInTheBackground() {
  const h = harness();
  let unread = true;
  h.context.api = async (method, url) => url.endsWith('/channels') ? {channels:['inbox']} :
    url.startsWith('/v1/notifications') ? {items:[{notification_id:1,title:'到期消息',due_at:2000000000,read_at:unread ? null : 2000000001}]} : {items:[]};
  h.run('resetReminders()');
  await h.run('startReminders()');
  assert(h.node('reminders-panel').classList.contains('hidden'), '登录后台启动不应展开提醒管理');
  assert(h.node('notifications-panel').classList.contains('hidden'), '后台通知不应强行打开列表');
  assert.equal(h.node('notification-toggle').textContent, '通知 · 有未读');
  showDraft(h, '对话确认');
  assert(h.node('reminders-panel').classList.contains('hidden'), '提出提醒只出现对话确认卡');
  await h.run('showReminderManager()');
  assert.equal(h.node('reminders-panel').classList.contains('hidden'), false);
  h.run('dismissReminderContext()');
  assert(h.node('reminders-panel').classList.contains('hidden'));
  assert(h.node('reminder-preview').classList.contains('hidden'));
  await h.node('notification-toggle').listeners.click();
  assert.equal(h.node('notifications-panel').classList.contains('hidden'), false);
  unread = false;
  await h.run('refreshNotifications()');
  assert.equal(h.node('notification-toggle').textContent, '通知');
  h.node('close-notifications').listeners.click();
  await h.run('refreshNotifications()');
  assert(h.node('notifications-panel').classList.contains('hidden'));
  let release;
  h.context.api = async () => new Promise(resolve => {release=resolve;});
  const opening=h.run('showReminderManager()');
  h.run('dismissReminderContext()');
  release({items:[]});await opening;
  assert(h.node('reminders-panel').classList.contains('hidden'), '晚回来的列表不能重新展开');
  h.run('resetReminders()');
  assert(h.node('notification-toggle').classList.contains('hidden'));
  assert.equal(h.node('notification-toggle').textContent,'通知');
}

async function lateCancellationStaysInItsConversation(rejected) {
  const h = harness();
  let complete;
  let reads = 0;
  h.context.api = async method => {
    if (method === 'DELETE') return await new Promise((resolve, reject) => {
      complete = () => rejected ? reject(new Error('旧取消冲突')) : resolve({});
    });
    reads++;
    return {items:[{...newDraft('旧提醒'),reminder_id:'old',version:1,status:'active'}]};
  };
  await h.run('showReminderManager()');
  const cancelling=findChild(h.node('reminder-list'),node=>node.textContent==='取消提醒').listeners.click();
  h.run('dismissReminderContext()');
  complete();await cancelling;
  assert.equal(h.errors.length,0,'旧取消请求不能向新对话显示错误');
  assert.equal(reads,1,'旧取消结果不能替新对话刷新清单');
  assert(h.node('reminders-panel').classList.contains('hidden'));
}

async function manualCreationRetriesSafely(status) {
  const h=harness(), posted=[];
  h.node('reminder-status').value='cancelled';
  h.run('editReminder()');
  const form=h.node('reminder-preview').children[0];
  assert(form, '手动兜底必须提供可提交的新建表单');
  const title=findChild(form,node=>node.attributes['aria-label']==='提醒内容');
  const due=findChild(form,node=>node.attributes['aria-label']==='提醒时间');
  title.value='手动创建'; due.value='2033-05-18T10:00';
  h.context.api=async(method,url,options)=>{
    assert(!url.includes('/assistant/'), '手动功能不得依赖模型');
    if(method==='POST') {
      posted.push(JSON.parse(JSON.stringify(options.json)));
      if(posted.length===1) throw Object.assign(new Error('提交失败'),{status});
    }
    return {items:[]};
  };
  await form.listeners.submit({preventDefault(){}});
  assert.equal(posted.length,1);
  assert(posted[0].request_id);
  assert.equal(title.disabled, status!==422, '明确拒绝可编辑；结果不确定时冻结重试');
  title.value='修正内容';
  await form.listeners.submit({preventDefault(){}});
  assert.equal(posted.length,2);
  if(status===422) assert.equal(posted[1].title,'修正内容');
  else assert.deepEqual(posted[1],posted[0], '网络失败重试必须同正文同ID');
  assert.equal(h.messages.length,0,'手动保存不向对话插入消息');
  assert(h.node('reminder-preview').classList.contains('hidden'));
  assert.equal(h.node('reminder-status').value,'active','新建完成应显示等待提醒列表');
}

async function manualModuleIsAnIndependentSurface() {
  const h=harness();
  await h.run('showReminderManager("module")');
  assert.equal(h.node('reminder-workspace').parentNode,h.node('reminder-module-host'));
  assert.equal(h.node('new-reminder').classList.contains('hidden'),false);
  h.node('new-reminder').listeners.click();
  assert(h.node('reminder-preview').children.length);
  h.run('dismissReminderContext()');
  await h.run('showReminderManager()');
  assert.equal(h.node('reminder-workspace').parentNode,h.node('reminder-chat-host'));
  assert(h.node('new-reminder').classList.contains('hidden'));
}

(async () => {
  await manualCreationRetriesSafely(503);
  await manualCreationRetriesSafely(422);
  await manualModuleIsAnIndependentSurface();
  for(const rejected of [true,false]) await lateCancellationStaysInItsConversation(rejected);
  await capabilitiesStayInTheBackground();
  for (const mode of ["draft", "edit", "manual"]) await olderSubmissionKeepsNewDraft(mode);
  for (const mode of ["draft", "edit", "manual"]) await olderSubmissionKeepsNewDraft(mode, true);
  for (const endpoint of ["/v1/reminders?status=active", "/v1/notifications?limit=100&latest=true", "/v1/reminders/channels"]) {
    await initialFailureRecovers(endpoint);
  }
  console.log("reminder behavior passed: 2 editor races, 3 initial failures and logout cleanup");
})().catch(error => {console.error(error); process.exitCode = 1;});
