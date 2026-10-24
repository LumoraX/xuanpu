// 玄圃提醒：持久状态在服务端，页面只负责确认、管理与读取通知。
"use strict";
const reminderState = {cursor:0, generation:0, context:0, editor:0, draft:null, controller:null, channels:["inbox"], retry:null, poll:null, notificationRequest:0, notificationApplied:0};
const repeatLabels = {once:"一次", daily:"每天", weekly:"每周"};
const deliveryLabels = {pending:"待发送", sending:"发送中", sent:"已发送", failed:"发送失败", skipped:"已停止发送"};

function browserTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
}
function reminderTime(item) {
  return new Date(item.due_at * 1000).toLocaleString("zh-CN", {timeZone:item.timezone || browserTimezone(), hour12:false});
}
// 编辑使用提醒原来的时区，不能把浏览器所在地区偷偷写回重复规则。
function reminderWallTime(stamp, zone) {
  const fields=Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:zone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(new Date(stamp*1000)).map(x=>[x.type,x.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}
function reminderTimestamp(wall, zone) {
  const wanted=Date.parse(wall+"Z"); const matches=[];
  for(const offset of [-86400000,0,86400000]) {
    let guess=wanted+offset;
    for(let i=0;i<4;i++) guess += wanted-Date.parse(reminderWallTime(guess/1000,zone)+"Z");
    if(Number.isFinite(guess) && reminderWallTime(guess/1000,zone)===wall) matches.push(guess/1000);
  }
  if(!matches.length) throw new Error("该时间在所选时区不存在，请重新选择。");
  return Math.min(...matches);
}
function reminderNode(tag, text, className = "") {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
}
// 界面按入口挂载；后台调度和通知连接保持独立。
function dismissReminderContext() {
  reminderState.context++; reminderState.editor++; reminderState.draft=null;
  $("reminders-panel").classList.add("hidden");
  $("reminder-preview").classList.add("hidden");
  $("reminder-preview").innerHTML="";
  $("reminder-feedback").textContent="";
}
function mountReminderWorkspace(surface) {
  $(surface === "module" ? "reminder-module-host" : "reminder-chat-host").appendChild($("reminder-workspace"));
  $("new-reminder").classList.toggle("hidden", surface !== "module");
}
async function showReminderManager(surface = "conversation") {
  if (!conn.me?.user_id) return;
  dismissReminderContext();
  mountReminderWorkspace(surface);
  $("reminder-status").value="active";
  $("reminders-panel").classList.remove("hidden");
  await refreshReminders();
}
function resetReminders() {
  reminderState.generation++; reminderState.editor++;
  reminderState.notificationRequest=0;reminderState.notificationApplied=0;
  resetNotificationAlerts();
  if (reminderState.controller) reminderState.controller.abort();
  clearTimeout(reminderState.retry); clearTimeout(reminderState.poll);
  reminderState.controller = null; reminderState.retry = null; reminderState.poll=null; reminderState.cursor = 0;
  reminderState.draft = null; reminderState.channels = ["inbox"];
  dismissReminderContext(); setNotificationsOpen(false);
  mountReminderWorkspace("conversation");
  $("notification-toggle").classList.add("hidden");
  $("notification-toggle").textContent="通知";
  $("reminder-status").value = "active";
  for (const id of ["reminder-list", "notification-list", "reminder-preview"]) $(id).innerHTML = "";
  $("reminders-panel").classList.add("hidden");
  $("reminder-preview").classList.add("hidden");
}
async function startReminders() {
  if (!conn.me?.user_id) return;
  const generation = reminderState.generation;
  $("notification-toggle").classList.remove("hidden");
  try {
    await Promise.all([refreshReminders(), refreshNotifications()]);
    const body = await api("GET", "/v1/reminders/channels");
    if (generation === reminderState.generation) reminderState.channels = body.channels;
  } catch(error) {
    if(generation===reminderState.generation) $("notification-status").textContent="连接恢复后自动读取提醒";
  } finally {
    if(generation===reminderState.generation && conn.me?.user_id) {
      startNotificationStream(generation);
      pollDeliveryStatus(generation);
    }
  }
}
// 状态变化不产生新通知ID；有界补查保证外部发送终态不会停在“待发送”。
function pollDeliveryStatus(generation) {
  reminderState.poll=setTimeout(async()=>{
    if(generation!==reminderState.generation || !conn.me?.user_id) return;
    try {await Promise.all([refreshNotifications(),refreshReminders()]);const body=await api("GET","/v1/reminders/channels");if(generation===reminderState.generation) reminderState.channels=body.channels;} catch(error) {if(error.name!=="AbortError" && generation===reminderState.generation) $("notification-status").textContent="稍后自动更新发送状态";}
    if(generation===reminderState.generation) pollDeliveryStatus(generation);
  },15000);
}
async function refreshReminders() {
  const generation = reminderState.generation;
  const context = reminderState.context;
  const status = $("reminder-status").value || "active";
  const body = await api("GET", "/v1/reminders?status=" + encodeURIComponent(status));
  if (generation !== reminderState.generation || context !== reminderState.context || status !== ($("reminder-status").value || "active")) return;
  const list = $("reminder-list"); list.innerHTML = "";
  for (const item of body.items) {
    const row = reminderNode("li", "", "reminder-item");
    row.appendChild(reminderNode("strong", item.title));
    row.appendChild(reminderNode("div", `${reminderTime(item)} · ${item.timezone} · ${repeatLabels[item.repeat]}`, "meta"));
    row.appendChild(reminderNode("div", {active:"等待提醒",completed:"已提醒",cancelled:"已取消"}[item.status], "meta"));
    row.dataset.id = item.reminder_id;
    if (item.status === "active") {
      const edit = reminderNode("button", "修改", "ghost"); edit.type = "button";
      edit.addEventListener("click", () => editReminder(item)); row.appendChild(edit);
      const cancel = reminderNode("button", "取消提醒", "ghost"); cancel.type = "button";
      cancel.addEventListener("click", async () => {
        const generation=reminderState.generation, context=reminderState.context;
        cancel.disabled = true;
        try {
          await api("DELETE", `/v1/reminders/${encodeURIComponent(item.reminder_id)}?version=${item.version}`);
          if(generation===reminderState.generation && context===reminderState.context) await refreshReminders();
        } catch (error) {
          if(generation===reminderState.generation && context===reminderState.context) {
            cancel.disabled=false;fail(error);
            try { await refreshReminders(); }
            catch (refreshError) {if(generation===reminderState.generation && context===reminderState.context) fail(refreshError);}
          }
        }
      }); row.appendChild(cancel);
    }
    if(item.source==="work_task" && item.work_task_id){
      const taskButton=reminderNode("button","查看工作进展","ghost");taskButton.type="button";
      taskButton.addEventListener("click",async()=>{if(generation!==reminderState.generation)return;await openWorkspace("task");if(generation===reminderState.generation)await showWorkTaskCard(item.work_task_id,$("workspace-content"));});row.appendChild(taskButton);
    }
    list.appendChild(row);
  }
  if (!body.items.length) list.appendChild(reminderNode("li", status === "active" ? "暂无待办提醒。可以在对话里告诉助手，也可以从功能页新建。" : "还没有此类提醒。", "meta"));
}
async function refreshNotifications() {
  const generation = reminderState.generation;
  const request = ++reminderState.notificationRequest;
  const body = await api("GET", "/v1/notifications?limit=100&latest=true");
  if (generation !== reminderState.generation || request < reminderState.notificationApplied) return;
  reminderState.notificationApplied=request;
  observeNotificationAlerts(body.items,{snapshot:true});
  if(typeof renderSharedNotifications==="function"&&body.shared)renderSharedNotifications(body.shared);
  if(typeof renderCollaborationNotifications==="function"&&body.collaboration)renderCollaborationNotifications(body.collaboration);
  const unread = body.items.some(item => !item.read_at) || ((body.shared?.unread_total||body.collaboration?.unread_total)>0);
  $("notification-toggle").textContent = unread ? "通知 · 有未读" : "通知";
  $("notification-toggle").title = "查看最近100条通知";
  const list = $("notification-list"); list.innerHTML = "";
  const associations=[];
  for (const item of body.items.slice().reverse()) {
    reminderState.cursor=Math.max(reminderState.cursor,item.notification_id);
    const row = reminderNode("li", "", "reminder-item");
    row.appendChild(reminderNode("strong", item.title));
    if(item.due_at!=null)row.appendChild(reminderNode("div", `原定 ${when(item.due_at)}`, "meta"));
    if(item.body)row.appendChild(reminderNode("p",item.body,"notification-reason"));
    for (const delivery of item.deliveries || []) {
      const status = delivery.last_error_code === "delivery_unknown" ? "送达结果未确认，请检查飞书消息" : deliveryLabels[delivery.status] || delivery.status;
      row.appendChild(reminderNode("div", `${delivery.channel === "feishu" ? "飞书" : delivery.channel}：${status}`, "meta"));
    }
    if (!item.read_at) {
      const read = reminderNode("button", "标为已读", "ghost"); read.type="button";
      read.addEventListener("click", async () => { if(generation!==reminderState.generation)return;read.disabled=true; try { await api("POST", `/v1/notifications/${item.notification_id}/read`); if(generation===reminderState.generation)await refreshNotifications(); } catch(error) {if(generation===reminderState.generation){read.disabled=false; fail(error);}} });
      row.appendChild(read);
    } else row.appendChild(reminderNode("span", "已读", "meta"));
    if(item.source==="work_task" && item.work_task_id){
      const taskButton=reminderNode("button","查看工作进展","ghost");taskButton.type="button";
      taskButton.addEventListener("click",async()=>{if(generation!==reminderState.generation)return;await openWorkspace("task");if(generation===reminderState.generation)await showWorkTaskCard(item.work_task_id,$("workspace-content"));});row.appendChild(taskButton);
    }
    list.appendChild(row);
    if(typeof appendProactiveNotificationAction==="function")associations.push(appendProactiveNotificationAction(item,row,
      ()=>generation===reminderState.generation && request===reminderState.notificationApplied && row.parentNode===list));
  }
  if (!body.items.length) list.appendChild(reminderNode("li", "暂无通知。离线期间的提醒会保存在这里。", "meta"));
  await Promise.all(associations);
}
function showReminderDraft(draft,proposalId=null) {
  mountReminderWorkspace("conversation");
  $("reminders-panel").classList.add("hidden");
  const editor=++reminderState.editor;
  reminderState.draft = {...draft, request_id:crypto.randomUUID()};
  const box = $("reminder-preview"); box.innerHTML=""; box.classList.remove("hidden");
  box.appendChild(reminderNode("strong", draft.title));
  box.appendChild(reminderNode("p", `${reminderTime(draft)} · ${draft.timezone} · ${repeatLabels[draft.repeat]}`));
  box.appendChild(reminderNode("p", "到点保存在玄圃通知中心。关闭网页后，可在下次打开时查看。", "meta"));
  const extra = reminderNode("input", ""); extra.type="checkbox"; extra.id="reminder-feishu";
  if (reminderState.channels.includes("feishu")) {
    const label = reminderNode("label", " 同时通过飞书提醒我");
    label.prepend(extra); box.appendChild(label);
  }
  const confirm = reminderNode("button", "确认创建提醒"); confirm.id="confirm-reminder"; confirm.type="button";
  confirm.addEventListener("click", async () => {
    const current = reminderState.draft; if (!current) return;
    const generation = reminderState.generation, context = reminderState.context; confirm.disabled=true;
    try {
      // 首次点击即冻结正文与幂等ID；失败重试不能悄悄改变同一请求的内容。
      if (!current.submitted) current.submitted = {title:current.title,due_at:current.due_at,timezone:current.timezone,repeat:current.repeat,
        channels:extra.checked ? ["inbox","feishu"] : ["inbox"],request_id:current.request_id};
      extra.disabled=true;
      if(proposalId) {const {request_id,...data}=current.submitted;await api("POST", `/v1/assistant/proposals/${encodeURIComponent(proposalId)}/confirm`, {json:{data}});}
      else await api("POST", "/v1/reminders", {json:current.submitted});
      if (generation !== reminderState.generation || context !== reminderState.context) return;
      if(editor===reminderState.editor) {reminderState.draft=null; box.innerHTML=""; box.classList.add("hidden");}
      append("assistant", `提醒已创建：${current.title}，${reminderTime(current)}（${current.timezone}）。`);
      await refreshReminders();
    } catch(error) {if (generation === reminderState.generation && context === reminderState.context) {confirm.disabled=false; fail(error);}}
  }); box.appendChild(confirm);
  const discard = reminderNode("button", "暂不创建", "ghost"); discard.type="button";
  discard.addEventListener("click", async () => {if(confirm.disabled) return; try {if(proposalId)await api("POST", `/v1/assistant/proposals/${encodeURIComponent(proposalId)}/cancel`);reminderState.draft=null;box.innerHTML="";box.classList.add("hidden");}catch(error){fail(error);}});
  box.appendChild(discard);
}
function editReminder(item = null) {
  if (!conn.me?.user_id) return;
  const editor=++reminderState.editor;
  $("reminder-feedback").textContent="";
  const box=$("reminder-preview"); box.innerHTML="";box.classList.remove("hidden"); reminderState.draft=null;
  const form=document.createElement("form"); form.className="reminder-form";
  const title=document.createElement("input"); title.required=true;title.maxLength=500;title.value=item?.title || "";title.placeholder="例如：准备复盘材料";title.setAttribute("aria-label","提醒内容");
  const due=document.createElement("input"); due.type="datetime-local";due.required=true;due.setAttribute("aria-label","提醒时间");
  const zone=item?.timezone || browserTimezone();
  const originalWall=item ? reminderWallTime(item.due_at,zone) : "";
  due.value=originalWall || reminderWallTime(Date.now()/1000+3600,zone);
  const repeat=document.createElement("select");repeat.setAttribute("aria-label","重复方式");
  for(const [value,label] of Object.entries(repeatLabels)) {const option=reminderNode("option",label);option.value=value;repeat.appendChild(option);} repeat.value=item?.repeat || "once";
  form.appendChild(reminderNode("strong",item ? "修改提醒" : "新建提醒"));
  for(const [name,field] of [["提醒内容",title],["提醒时间",due],["重复方式",repeat]]) {const label=reminderNode("label",name);label.appendChild(field);form.appendChild(label);}
  form.appendChild(reminderNode("p",`时间按 ${zone} 显示。通知保存在玄圃，关闭网页也不影响后台提醒。`,"meta"));
  const feishu=document.createElement("input");feishu.type="checkbox";feishu.checked=item?.channels.includes("feishu") || false;
  if(reminderState.channels.includes("feishu") || feishu.checked) {const label=reminderNode("label"," 同时通过飞书提醒我","channel-choice");label.prepend(feishu);form.appendChild(label);}
  const submit=reminderNode("button","保存提醒");submit.type="submit";form.appendChild(submit);
  const discard=reminderNode("button","返回","ghost");discard.type="button";discard.addEventListener("click",()=>{box.innerHTML="";box.classList.add("hidden");});form.appendChild(discard);
  let submitted=null;
  const requestId=crypto.randomUUID();
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(submit.disabled) return; submit.disabled=true;
    const generation=reminderState.generation, context=reminderState.context;
    try {
      if(!submitted) submitted={title:title.value.trim(),due_at:item && due.value===originalWall ? item.due_at : reminderTimestamp(due.value,zone),timezone:zone,repeat:repeat.value,channels:feishu.checked ? ["inbox","feishu"] : ["inbox"],...(item ? {version:item.version} : {request_id:requestId})};
      for(const field of [title,due,repeat,feishu,discard]) field.disabled=true;
      await api(item ? "PUT" : "POST",item ? `/v1/reminders/${encodeURIComponent(item.reminder_id)}` : "/v1/reminders",{json:submitted});
      if(generation !== reminderState.generation || context !== reminderState.context) return;
      if(editor===reminderState.editor) {box.innerHTML="";box.classList.add("hidden");$("reminder-feedback").textContent=item ? "提醒已更新。" : "提醒已创建。";}
      if(!item && editor===reminderState.editor) $("reminder-status").value="active";
      // 写入已成功；列表读取失败只提示刷新，不能把成功操作当成提交失败。
      await refreshReminders().catch(error=>{if(generation===reminderState.generation && context===reminderState.context) fail(error);});
    } catch(error){if(generation===reminderState.generation && context===reminderState.context){
      submit.disabled=false;
      if(!submitted || error.status===400 || error.status===422) {submitted=null;for(const field of [title,due,repeat,feishu,discard]) field.disabled=false;}
      else {submit.textContent="重试保存";discard.disabled=false;}
      fail(error);
    }}
  });box.appendChild(form);
}
async function startNotificationStream(generation) {
  if(generation !== reminderState.generation || !conn.me?.user_id) return;
  const controller=new AbortController();reminderState.controller=controller;
  try {
    const response=await fetch(url(`/v1/notifications/events?after_id=${reminderState.cursor}&shared_after_id=${typeof sharedInbox!=="undefined"?sharedInbox.cursor:0}&collaboration_after_id=${typeof collabInbox!=="undefined"?collabInbox.cursor:0}`),{
      ...requestOptions(),headers:headers(),signal:controller.signal,
    });
    if(!response.ok) {if(response.status===401){resetIdentity();return;}throw new Error("通知连接暂不可用");}
    $("notification-status").textContent="实时更新中";
    for await(const [name,data] of sse(response)) {
      if(generation !== reminderState.generation) return;
      if(name==="shared_notification" || name==="shared_notifications_changed"){
        if(typeof sharedNotificationEvent==="function")sharedNotificationEvent(name,data);
        await refreshNotifications();
      }else if(name==="collaboration_notification"||name==="collaboration_notifications_changed"){
        if(typeof collaborationNotificationEvent==="function")collaborationNotificationEvent(name,data);
        await refreshNotifications();
      }else if(name==="notification") {
        reminderState.cursor=Math.max(reminderState.cursor, Number(data.notification_id || data.id || 0));
        observeNotificationAlerts([data]);
        await Promise.all([refreshNotifications(),refreshReminders()]);
      }
    }
  } catch(error) {
    if(error.name !== "AbortError" && generation===reminderState.generation) $("notification-status").textContent="连接恢复后自动补读";
  } finally {
    if(generation===reminderState.generation && conn.me?.user_id) {
      reminderState.retry=setTimeout(async()=>{await refreshNotifications().catch(fail);if(generation===reminderState.generation) startNotificationStream(generation);},3000);
    }
  }
}
$("reminder-status").addEventListener("change",()=>refreshReminders().catch(fail));
$("new-reminder").addEventListener("click",()=>editReminder());
$("refresh-reminders").addEventListener("click",()=>refreshReminders().catch(fail));
$("open-reminder-module").addEventListener("click",()=>showReminderManager("module").catch(fail));
$("close-reminders").addEventListener("click",dismissReminderContext);
