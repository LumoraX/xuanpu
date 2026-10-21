// 原生个人工作区：所有模块使用同一权限、数据与版本规则。
"use strict";
const workspace = {kind:null, generation:0, parent:null, trashed:false, search:"", items:[], settings:null, summary:null, matter:null, picking:false};
const workspaceNames = {file:"我的文件",folder:"文件夹",matter:"我的事项",todo:"我的待办",event:"我的日程",skill:"个人能力",settings:"助手设置",proactive:"业务跟进",task:"我的工作",finance:"财务记录与审批"};
const workspaceBase = "/v1/workspace";
function wsButton(text, action, kind="ghost") {
  const button=document.createElement("button"); button.type="button"; button.className=kind; button.textContent=text;
  button.addEventListener("click",()=>Promise.resolve().then(action).catch(fail)); return button;
}
function wsText(tag,text,className="") {
  const node=document.createElement(tag); node.textContent=text; node.className=className; return node;
}
function workspaceCurrent(generation,epoch) { return generation===workspace.generation && epoch===conn.epoch && conn.ready; }
function resetWorkspace() {
  workspace.generation++; workspace.kind=null; workspace.settings=null; workspace.summary=null; workspace.items=[]; workspace.parent=null; workspace.matter=null;
  workspace.trashed=false; workspace.picking=false; workspace.search="";
  $("workspace-window").classList.add("hidden"); $("workspace-nav").classList.add("hidden");
  $("workspace-content").replaceChildren(); $("workspace-toolbar").replaceChildren(); $("workspace-draft").replaceChildren();
  $("workspace-feedback").textContent=""; $("assistant-name").textContent="玄圃助手";
  document.body.classList.remove("workspace-open"); resetWorkspaceChat();
  if(typeof resetWorkTasks==="function")resetWorkTasks();
  if(typeof resetProactive==="function")resetProactive();
  if(typeof resetFinance==="function")resetFinance();
}
async function startWorkspace() {
  if (!conn.me?.user_id || !conn.ready) return;
  $("workspace-nav").classList.remove("hidden"); $("attach-file").disabled=false;
  await refreshWorkspaceSummary();
  if (!chat.sessionId) await loadMainConversation();
}
async function refreshWorkspaceSummary() {
  if(!conn.ready || !conn.me?.user_id)return;
  const epoch=conn.epoch;
  const [summary,settings]=await Promise.all([api("GET",workspaceBase+"/summary"),api("GET",workspaceBase+"/settings")]);
  if(epoch!==conn.epoch)return;
  workspace.summary=summary;workspace.settings=settings; $("assistant-name").textContent=settings.assistant_name;
  for(const node of document.querySelectorAll("[data-count]")) node.textContent=summary.counts[node.dataset.count] || "";
}
async function openWorkspace(kind,{picking=false, matter=null}={}) {
  if(!conn.ready || !conn.me?.user_id)return;
  workspace.kind=kind; workspace.parent=null; workspace.search=""; workspace.trashed=false; workspace.picking=picking; workspace.matter=matter;
  $("workspace-window").classList.remove("hidden"); document.body.classList.add("workspace-open");
  for(const node of document.querySelectorAll("[data-workspace]"))node.setAttribute("aria-expanded",String(node.dataset.workspace===kind));
  await refreshWorkspace();
}
function closeWorkspace() {
  workspace.generation++; workspace.kind=null; workspace.picking=false;
  $("workspace-window").classList.add("hidden"); document.body.classList.remove("workspace-open");
  for(const node of document.querySelectorAll("[data-workspace]"))node.setAttribute("aria-expanded","false");
}
async function refreshWorkspace() {
  const generation=++workspace.generation,epoch=conn.epoch,kind=workspace.kind;
  if(!kind)return;
  $("workspace-title").textContent=workspaceNames[kind]+(workspace.picking?" · 选择附件":"");
  $("workspace-feedback").textContent="正在加载…";
  $("workspace-content").replaceChildren(); $("workspace-toolbar").replaceChildren();
  try {
    if(kind==="finance") {
      await renderFinanceList(generation,epoch);
    } else if(kind==="task") {
      await renderWorkTaskList(generation,epoch);
    } else if(kind==="proactive") {
      await renderProactiveList(generation,epoch);
    } else if(kind==="settings") {
      const settings=await api("GET",workspaceBase+"/settings");
      if(!workspaceCurrent(generation,epoch))return;
      workspace.settings=settings; renderWorkspaceSettings(settings);
    } else {
      const params=new URLSearchParams({trashed:String(workspace.trashed)});
      if(kind!=="file")params.set("kind",kind);
      if(workspace.search)params.set("search",workspace.search);
      const {items}=await api("GET",workspaceBase+"/items?"+params);
      if(!workspaceCurrent(generation,epoch))return;
      workspace.items=items;
      renderWorkspaceToolbar(kind);
      if(kind==="file")renderWorkspaceFiles(items);
      else renderWorkspaceRecords(items.filter(x=>!workspace.matter || x.data.matter_id===workspace.matter));
      if(kind==="event" && !workspace.trashed)await appendCalendarReminders(generation,epoch);
      if(kind==="todo" && !workspace.trashed)await appendWorkspaceApprovals(generation,epoch);
    }
    if(kind!=="finance" && workspaceCurrent(generation,epoch))$("workspace-feedback").textContent="";
  } catch(error) {
    if(!workspaceCurrent(generation,epoch)||error.name==="AbortError")return;
    $("workspace-feedback").textContent=error.message;
    $("workspace-content").append(wsButton("重新加载",refreshWorkspace));
  }
}
function renderWorkspaceToolbar(kind) {
  const box=$("workspace-toolbar");
  if(kind==="file") { renderFileToolbar(box); return; }
  box.append(wsButton("新建"+{matter:"事项",todo:"待办",event:"日程",skill:"能力"}[kind],()=>editWorkspaceRecord(kind),""));
  const search=document.createElement("input");search.type="search";search.placeholder="搜索"+workspaceNames[kind];search.setAttribute("aria-label",search.placeholder);search.value=workspace.search;
  search.addEventListener("change",()=>{workspace.search=search.value.trim();refreshWorkspace();});
  box.append(search,wsButton(workspace.trashed?"返回列表":"回收站",()=>{workspace.trashed=!workspace.trashed;return refreshWorkspace();}),wsButton("刷新",refreshWorkspace));
}
function renderWorkspaceRecords(items) {
  const box=$("workspace-content");
  if(!items.length)box.append(wsText("p",workspace.trashed?"回收站是空的。":"这里还没有记录。直接在对话中告诉助手，或点击新建。","workspace-empty"));
  const sorted=[...items].sort((a,b)=>(a.data.start_at || a.data.due_at || Infinity)-(b.data.start_at || b.data.due_at || Infinity));
  for(const item of sorted)box.append(workspaceRecordCard(item));
}
function workspaceRecordCard(item) {
  const card=wsText("article","","workspace-record"),data=item.data;
  card.append(wsText("h3",data.title || data.name));
  if(data.description)card.append(wsText("p",data.description));
  const status={active:"进行中",completed:"已完成",archived:"已归档",open:"待完成",done:"已完成"};
  if(data.status)card.append(wsText("span",status[data.status] || data.status,"tag"));
  if(item.kind==="skill")card.append(wsText("span",data.enabled?"对话中启用":"已停用","tag"));
  if(data.due_at)card.append(wsText("p","截止 "+workspaceTime(data.due_at),"meta"));
  if(data.start_at)card.append(wsText("p",workspaceTime(data.start_at)+" — "+workspaceTime(data.end_at)+" · "+data.timezone,"meta"));
  card.append(wsText("p","更新于 "+workspaceTime(item.updated_at),"meta"));
  const actions=wsText("div","","row");
  if(item.trashed_at)actions.append(wsButton("恢复",()=>workspaceMutation(()=>api("POST",`${workspaceBase}/items/${item.id}/restore?version=${item.version}`))));
  else {
    actions.append(wsButton("编辑",()=>editWorkspaceRecord(item.kind,item)));
    if(item.kind==="matter")actions.append(wsButton("查看事项",()=>showMatter(item)),wsButton("聊这件事",()=>talkAboutMatter(item)));
    if(item.kind==="todo")actions.append(wsButton(data.status==="done"?"重新打开":"完成",()=>patchWorkspaceItem(item,{status:data.status==="done"?"open":"done"})));
    if(item.kind==="skill")actions.append(wsButton(data.enabled?"停用":"启用",()=>patchWorkspaceItem(item,{enabled:!data.enabled})));
    actions.append(wsButton("移入回收站",()=>trashWorkspaceItem(item)));
  }
  card.append(actions);return card;
}
async function workspaceMutation(work,success="已保存") {
  const generation=workspace.generation,epoch=conn.epoch;
  $("workspace-feedback").textContent="正在保存…";
  try {
    const result=await work();
    if(epoch!==conn.epoch)return;
    if(workspaceCurrent(generation,epoch))await refreshWorkspace();
    await refreshWorkspaceSummary();toast(success,"info");return result;
  } catch(error) {
    if(workspaceCurrent(generation,epoch))$("workspace-feedback").textContent=error.status===409?"记录已改变，请刷新后重试。" : error.message;
    throw error;
  }
}
function patchWorkspaceItem(item,data) { return workspaceMutation(()=>api("PUT",`${workspaceBase}/items/${item.id}`,{json:{data,version:item.version}})); }
function trashWorkspaceItem(item) {
  if(!confirm(`将“${item.data.title || item.data.name}”移入回收站？${item.kind==="folder"?"文件夹内的内容也会移入，可在回收站恢复。":""}`))return;
  return workspaceMutation(()=>api("DELETE",`${workspaceBase}/items/${item.id}?version=${item.version}`),"已移入回收站");
}
function workspaceTime(value) {
  if(!value)return "";
  try {
    return new Date(value*1000).toLocaleString("zh-CN",{timeZone:workspace.settings?.timezone || browserTimezone()});
  } catch(error) {
    if(!(error instanceof RangeError))throw error;
    return new Date(value*1000).toLocaleString("zh-CN",{timeZone:"UTC"})+" UTC（所选时区暂不受浏览器支持）";
  }
}
async function showMatter(item) {
  const generation=++workspace.generation,epoch=conn.epoch;
  const {items}=await api("GET",workspaceBase+"/items");
  if(!workspaceCurrent(generation,epoch))return;
  $("workspace-toolbar").replaceChildren(wsButton("返回事项",refreshWorkspace),wsButton("聊这件事",()=>talkAboutMatter(item)));
  const box=$("workspace-content");box.replaceChildren(workspaceRecordCard(item));
  box.append(wsText("h3","关联资料与工作"));
  const linked=items.filter(x=>x.data.matter_id===item.id);
  if(!linked.length)box.append(wsText("p","尚无关联。上传文件或创建待办时可以选择此事项。","status"));
  for(const record of linked)box.append(record.kind==="file"?workspaceFileCard(record):workspaceRecordCard(record));
  const {items:messages}=await api("GET",workspaceBase+"/messages?latest=true&limit=100&matter_id="+encodeURIComponent(item.id));
  if(!workspaceCurrent(generation,epoch))return;
  box.append(wsText("h3","事项对话与进展"));
  if(!messages.length)box.append(wsText("p","点击“聊这件事”，在此事项下继续工作。","status"));
  for(const message of messages) {
    const entry=wsText("article","","workspace-record");entry.append(wsText("p",(message.role==="user"?"你":"助手")+" · "+workspaceTime(message.created_at),"meta"),wsText("p",message.text));box.append(entry);
  }
  if(messages.length===100)box.append(wsText("p","显示此事项最近100条对话。","status"));
}
async function appendCalendarReminders(generation,epoch) {
  const {items}=await api("GET","/v1/reminders?status=active");
  if(!workspaceCurrent(generation,epoch)||!items.length)return;
  const box=$("workspace-content");box.append(wsText("h3","等待提醒"));
  for(const item of items) {
    const card=wsText("article","","workspace-record");card.append(wsText("h3",item.title),wsText("p",workspaceTime(item.next_due_at || item.due_at),"meta"),wsButton("管理提醒",()=>{document.querySelector('[data-tab="sessions"]').click();closeWorkspace();return showReminderManager();}));box.append(card);
  }
}
async function appendWorkspaceApprovals(generation,epoch) {
  await appendWorkspaceProposals(generation,epoch);
  const {sessions}=await api("GET","/v1/sessions?status=active&mine=true&limit=100");
  if(sessions.length===100)$("workspace-content").append(wsText("p","当前显示最近 100 个业务对话中的确认任务；更早对话可在业务记录中查看。","status"));
  for(const session of sessions) {
    if(!workspaceCurrent(generation,epoch))return;
    const {pending}=await api("GET",`/v1/sessions/${session.session_id}/pending`);
    if(!workspaceCurrent(generation,epoch))return;
    for(const item of pending) {
      const card=wsText("article","","workspace-record");card.append(wsText("h3","等你确认 · "+businessName(session.package)),wsText("p",item.detail?.target || item.reason),wsButton("查看并处理",()=>{closeWorkspace();document.querySelector('[data-tab="sessions"]').click();return openSession(session);}));$("workspace-content").prepend(card);
    }
  }
}
for(const button of document.querySelectorAll("[data-workspace]"))button.addEventListener("click",()=>openWorkspace(button.dataset.workspace).catch(fail));
$("workspace-close").addEventListener("click",closeWorkspace);
document.addEventListener("keydown",event=>{if(event.key==="Escape" && workspace.kind)closeWorkspace();});
