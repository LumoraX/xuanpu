// 主动业务只在对话按需展示；所有决定经当前身份、会话与版本核验。
"use strict";
const proactiveState={context:0,loading:0,noticeCache:null};
const proactiveActions={complete:"确认完成",postpone:"确认延后",ignore_stage:"本阶段不用跟进",set_reminder:"确认设置提醒",progress:"确认有新进展"};
function proactiveToken() { return {epoch:conn.epoch,generation:chat.generation,context:proactiveState.context,loading:workspaceChat.loading}; }
function proactiveCurrent(token) {
  return token.epoch===conn.epoch && token.generation===chat.generation && token.context===proactiveState.context && token.loading===workspaceChat.loading && conn.ready;
}
function dismissProactiveContext() {
  proactiveState.context++;proactiveState.loading++;
  $("proactive-draft").replaceChildren();$("proactive-draft").classList.add("hidden");
}
function resetProactive() { proactiveState.noticeCache=null;dismissProactiveContext(); }
function proactiveClosed(item) { return ["resolved","dismissed","ignored","failed"].includes(item.status); }
async function openProactive(id) {
  if(!conn.ready || !conn.me?.user_id || chat.routing || chat.streaming)return;
  const token=proactiveToken(),loading=++proactiveState.loading;
  try {
    const item=await api("GET","/v1/proactive/"+encodeURIComponent(id));
    if(!proactiveCurrent(token) || loading!==proactiveState.loading)return;
    document.querySelector('[data-tab="sessions"]').click();closeWorkspace();setNotificationsOpen(false);
    showProactiveCard(item);
  } catch(error) {if(proactiveCurrent(token) && error.name!=="AbortError")fail(error);}
}
// 原生提醒复用业务关联，不创建另一条通知；短缓存合并同批通知的查询。
async function proactiveNotificationItems() {
  const epoch=conn.epoch,previous=proactiveState.noticeCache;
  if(previous?.epoch===epoch) {
    if(previous.pending)return previous.pending;
    if(previous.expires>Date.now())return previous.items;
  }
  const cache={epoch,items:[],expires:0,pending:null};proactiveState.noticeCache=cache;
  cache.pending=api("GET","/v1/proactive").then(body=>{
    if(epoch!==conn.epoch || proactiveState.noticeCache!==cache)return [];
    cache.items=body.items;cache.expires=Date.now()+15000;cache.pending=null;return cache.items;
  }).catch(error=>{
    if(proactiveState.noticeCache===cache)proactiveState.noticeCache=null;
    throw error;
  });
  return cache.pending;
}
async function appendProactiveNotificationAction(item,container,current,onHandle=()=>{}) {
  const epoch=conn.epoch;
  try {
    let id=item.proactive_followup_id || item.proactive_check_id;
    if(!id && item.source==="reminder" && item.reminder_id) {
      const items=await proactiveNotificationItems();
      id=items.find(record=>record.reminder_id===item.reminder_id)?.id;
    }
    if(!id || epoch!==conn.epoch || !current())return;
    const button=reminderNode("button","处理","ghost");button.type="button";
    button.addEventListener("click",()=>{if(epoch!==conn.epoch || !current())return;onHandle();return openProactive(id);});
    container.appendChild(button);
  } catch(error) {
    if(epoch===conn.epoch && current() && error.name!=="AbortError")$("notification-status").textContent="业务关联暂不可用，稍后自动重试。";
  }
}
function showProactiveCard(item,draft=null) {
  dismissProactiveContext();
  const box=$("proactive-draft");box.classList.remove("hidden");
  box.append(wsText("strong",item.title),wsButton("收起",dismissProactiveContext));
  const certainty=["inferred","possible","uncertain","推测"].includes(item.certainty)?"待核实的建议":"根据现有记录跟进";
  box.append(wsText("p",certainty,"tag"));
  if(item.reason)box.append(wsText("p",item.reason));
  if(item.body && item.body!==item.reason)box.append(wsText("p",item.body));
  if(item.evidence_details?.length || item.evidence_refs?.length) {
    const evidence=wsText("details",""),summary=wsText("summary","查看依据");evidence.append(summary);
    if(item.evidence_details?.length) {
      for(const detail of item.evidence_details) {
        evidence.append(wsText("p",detail.text));
        if(detail.at!=null)evidence.append(wsText("p",workspaceTime(detail.at),"meta"));
      }
    } else for(const ref of item.evidence_refs) evidence.append(wsText("p",typeof ref==="string"?ref:[ref.kind || ref.type,ref.id || ref.message_id || ref.item_id,ref.version!=null?"版本 "+ref.version:""].filter(Boolean).join(" · "),"meta"));
    box.append(evidence);
  }
  if(proactiveClosed(item)) {box.append(wsText("p","这项跟进已处理。","status"));return;}
  const token=proactiveToken();
  if(draft?.action && proactiveActions[draft.action]) {
    if(["postpone","set_reminder"].includes(draft.action))renderProactiveTime(item,draft.action,box,token,draft);
    else appendProactiveDecision(item,draft.action,box,token);
  } else {
    const actions=wsText("div","","row");
    for(const [action,label] of [["complete","完成了"],["postpone","延后"],["set_reminder","设置提醒"],["progress","有新进展"],["ignore_stage","本阶段不用跟进"]]) {
      actions.append(wsButton(label,()=>{
        if(!proactiveCurrent(token))return;
        if(["postpone","set_reminder"].includes(action)) {actions.replaceChildren();renderProactiveTime(item,action,actions,token);}
        else return submitProactiveDecision(item,action,token,actions);
      }));
    }box.append(actions);
  }
  box.scrollIntoView({block:"nearest",behavior:"smooth"});
}
function appendProactiveDecision(item,action,box,token) {
  const actions=wsText("div","","row");
  const confirm=wsButton(proactiveActions[action],()=>submitProactiveDecision(item,action,token,actions));
  actions.append(confirm);box.append(actions);
}
function renderProactiveTime(item,action,box,token,draft={}) {
  const zone=draft.timezone || workspace.settings?.timezone || "Asia/Shanghai";
  const form=wsText("form","","workspace-form"),label=wsText("label",action==="postpone"?"新的目标时间":"提醒时间");
  const due=document.createElement("input");due.type="datetime-local";due.required=true;due.setAttribute("aria-label",label.textContent);
  const suggested=draft.due_at || item.proposed_time;
  if(typeof suggested==="number" && Number.isFinite(suggested))due.value=reminderWallTime(suggested,zone);
  label.append(due);form.append(label,wsText("p","请确认具体时间，按 "+zone+" 解释。提醒保存在玄圃通知中心。","meta"));
  const channelFields=[];
  for(const channel of reminderState.channels) {
    if(channel==="inbox")continue;
    const label=wsText("label",channel==="feishu"?"同时通过飞书提醒我":channel),input=document.createElement("input");input.type="checkbox";
    input.checked=(draft.channels || workspace.settings?.proactive_channels || ["inbox"]).includes(channel);label.prepend(input);form.append(label);channelFields.push({channel,input});
  }
  const status=wsText("p","","status");status.setAttribute("role","status");
  const confirm=wsButton(proactiveActions[action],()=>{},"");confirm.type="submit";form.append(status,confirm);box.append(form);
  let submitted=null;
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!proactiveCurrent(token) || confirm.disabled)return;
    try {
      if(!submitted) {
        if(!due.value)throw new Error("请先选择并确认具体时间。");
        const dueAt=reminderTimestamp(due.value,zone);
        if(!Number.isFinite(dueAt) || dueAt<=Date.now()/1000)throw new Error("请选择将来的具体时间。");
        submitted={request_id:crypto.randomUUID(),version:item.version,...(item.item_version?{item_version:item.item_version}:{}),action,due_at:dueAt,timezone:zone,channels:["inbox",...channelFields.filter(x=>x.input.checked).map(x=>x.channel)]};
      }
      due.disabled=true;for(const x of channelFields)x.input.disabled=true;confirm.disabled=true;status.textContent="正在保存…";
      await performProactiveAction(item,submitted,token);
    } catch(error) {
      if(!proactiveCurrent(token))return;
      if(error.status===409) {await refreshConflictedProactive(item,token);return;}
      status.textContent=error.message;confirm.disabled=false;
      if(!submitted || [400,422].includes(error.status)) {submitted=null;due.disabled=false;for(const x of channelFields)x.input.disabled=false;}
      else confirm.textContent="重试确认";
    }
  });
}
async function submitProactiveDecision(item,action,token,actions) {
  if(!proactiveCurrent(token) || actions.dataset.saving==="true")return;
  // 相同卡片重试复用冻结请求，避免网络中断后重复处理。
  if(!actions.proactiveRequest)actions.proactiveRequest={request_id:crypto.randomUUID(),version:item.version,...(item.item_version?{item_version:item.item_version}:{}),action,due_at:null,timezone:workspace.settings?.timezone || "Asia/Shanghai",channels:["inbox"]};
  const submitted=actions.proactiveRequest;
  if(submitted.action!==action)return;
  actions.dataset.saving="true";
  for(const button of actions.children)button.disabled=true;
  try {await performProactiveAction(item,submitted,token);}
  catch(error) {
    if(!proactiveCurrent(token))return;
    if(error.status===409){await refreshConflictedProactive(item,token);return;}
    actions.dataset.saving="false";
    if([400,422].includes(error.status)) {
      actions.proactiveRequest=null;for(const button of actions.children)button.disabled=false;
    } else actions.replaceChildren(wsButton("重试"+proactiveActions[action],()=>submitProactiveDecision(item,action,token,actions)));
    fail(error);
  }
}
async function performProactiveAction(item,payload,token) {
  await api("POST","/v1/proactive/"+encodeURIComponent(item.id)+"/actions",{json:payload});
  if(!proactiveCurrent(token))return;
  proactiveState.noticeCache=null;
  dismissProactiveContext();append("assistant",{complete:"已确认完成",postpone:"已延后",ignore_stage:"本阶段不再跟进",set_reminder:"已设置提醒",progress:"已记录新进展"}[payload.action]+"："+item.title+(payload.due_at?"，"+reminderTime(payload)+"（"+payload.timezone+"）":"")+"。");
}
async function refreshConflictedProactive(item,token) {
  try {
    const fresh=await api("GET","/v1/proactive/"+encodeURIComponent(item.id));
    if(!proactiveCurrent(token))return;
    showProactiveCard(fresh);$("proactive-draft").append(wsText("p","记录已改变，已读取最新状态。请重新选择操作。","status"));
  } catch(error) {if(proactiveCurrent(token))fail(error);}
}
async function restoreProactiveDraft(draft) {
  const token=proactiveToken(),loading=++proactiveState.loading;
  const item=await api("GET","/v1/proactive/"+encodeURIComponent(draft.id));
  if(!proactiveCurrent(token) || loading!==proactiveState.loading)return;
  if(proactiveClosed(item)){dismissProactiveContext();return;}
  showProactiveCard(item,item.version===draft.version?draft:null);
  if(item.version!==draft.version)$("proactive-draft").append(wsText("p","这项跟进已有更新，请按最新状态选择操作。","status"));
}
async function handleProactiveRoute(route,text) {
  if(!route.proactive_draft && !route.open_proactive)return false;
  $("welcome").classList.add("hidden");append("user",text);append("assistant",route.message || "请确认这项业务跟进。");
  renderMessageFileCards(workspaceChat.attachments);chat.routeContext=[];$("message").value="";workspaceRouteAccepted();
  if(route.proactive_draft)await restoreProactiveDraft(route.proactive_draft);
  else await showProactiveList();
  return true;
}
async function showProactiveList() {
  if(!conn.ready || !conn.me?.user_id)return;
  await openWorkspace("proactive");
}
async function renderProactiveList(generation,epoch) {
  const body=await api("GET","/v1/proactive");
  if(!workspaceCurrent(generation,epoch))return;
  $("workspace-toolbar").append(wsButton("刷新",refreshWorkspace),wsButton("跟进设置",()=>openWorkspace("settings")));
  const box=$("workspace-content");
  box.append(wsText("p",body.enabled?"助手会根据现有记录提出跟进，处理时在对话中确认。":"主动跟进已关闭，可在助手设置中开启。","status"));
  if(!body.items.length)box.append(wsText("p","暂无需要处理的业务跟进。","workspace-empty"));
  for(const item of body.items) {
    const card=wsText("article","","workspace-record");card.append(wsText("h3",item.title),wsText("p",item.reason || item.body || "待核实最新进展"));
    if(item.reminder_id)card.append(wsText("p","已设置提醒"+(item.next_review_at?" · "+workspaceTime(item.next_review_at):""),"meta"));
    card.append(wsButton(proactiveClosed(item)?"查看已处理记录":"处理",()=>openProactive(item.id)));box.append(card);
  }
}
$("open-proactive-module").addEventListener("click",()=>showProactiveList().catch(fail));
// 配置只展示已有通知渠道；无需模型或连接器密钥。
function renderProactiveSettings(form,settings) {
  const current=settings.proactive_checks || {enabled:true,quiet_hours:{enabled:false,start:"22:00",end:"08:00"},ignored_types:[]};
  const quiet=current.quiet_hours;
  form.append(wsText("h3","业务跟进"));
  const field=(label,type,value)=>{
    const wrap=wsText("label",label),input=document.createElement("input");input.type=type;
    if(type==="checkbox")input.checked=Boolean(value);else input.value=value;
    input.setAttribute("aria-label",label);wrap.append(input);form.append(wrap);return input;
  };
  const enabled=field("主动关注我的业务进展","checkbox",current.enabled);
  const quietEnabled=field("开启免打扰时段","checkbox",quiet.enabled);
  const start=field("免打扰开始","time",quiet.start),end=field("免打扰结束","time",quiet.end);
  form.append(wsText("p","免打扰按上方时区解释，可跨午夜。通知仍保存在通知中心，浮层在时段结束后提示。","meta"));
  const ignored=new Map((current.ignored_types || []).map(item=>[item.type,item]));
  const types=[["overdue","逾期待办"],["stale","长期未处理待办"],["commitment","承诺跟进"],["waiting_result","等待结果"],["stalled","可能停滞"],["missing_schedule","缺少时间安排"]];
  const controls=types.map(([type,label])=>({type,input:field("暂停"+label,"checkbox",ignored.has(type))}));
  const channels=[];
  for(const channel of reminderState.channels) {
    const input=field(channel==="inbox"?"玄圃通知中心":channel==="feishu"?"飞书通知":channel,"checkbox",(settings.proactive_channels || ["inbox"]).includes(channel));
    if(channel==="inbox"){input.checked=true;input.disabled=true;}
    channels.push({channel,input});
  }
  return ()=>{
    if(quietEnabled.checked && (!start.value || !end.value || start.value===end.value))throw new Error("请设置不同的免打扰开始和结束时间。");
    for(const {type,input} of controls) {
      if(input.checked && !ignored.has(type))ignored.set(type,{type,until:null});
      if(!input.checked)ignored.delete(type);
    }
    return {proactive_checks:{enabled:enabled.checked,quiet_hours:{enabled:quietEnabled.checked,start:start.value,end:end.value},ignored_types:[...ignored.values()]},
      proactive_channels:channels.filter(x=>x.input.checked).map(x=>x.channel)};
  };
}
