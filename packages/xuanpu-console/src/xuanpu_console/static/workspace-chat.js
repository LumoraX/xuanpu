// 主对话与个人空间的连接：附件、事项上下文与持久历史。
"use strict";
const workspaceChat={attachments:[],matter:null,request:null,loading:0};
function resetWorkspaceChat() {
  closeAttachmentMenu();
  workspaceChat.attachments=[];workspaceChat.matter=null;workspaceChat.request=null;workspaceChat.loading++;
  $("attachment-chips").replaceChildren();$("attachment-input").value="";$("attach-file").disabled=true;
}
function stageWorkspaceFile(item) {
  if(chat.routing || chat.streaming)throw new Error("请等待当前回复完成后再添加附件。");
  if(workspaceChat.attachments.some(x=>x.id===item.id))return;
  if(workspaceChat.attachments.length>=8)throw new Error("一次最多添加 8 个附件。");
  workspaceChat.attachments.push(item);workspaceChat.request=null;renderAttachmentChips();
}
function renderAttachmentChips() {
  const box=$("attachment-chips");box.replaceChildren();
  if(workspaceChat.matter) {
    const chip=wsText("span","事项："+workspaceChat.matter.data.title,"attachment-chip");
    chip.append(wsButton("×",()=>{workspaceChat.matter=null;workspaceChat.request=null;renderAttachmentChips();}));box.append(chip);
  }
  for(const item of workspaceChat.attachments) {
    const chip=wsText("span",item.data.name,"attachment-chip");
    const remove=wsButton("×",()=>{if(chat.routing)return;workspaceChat.attachments=workspaceChat.attachments.filter(x=>x.id!==item.id);workspaceChat.request=null;renderAttachmentChips();});
    remove.setAttribute("aria-label","移除附件 "+item.data.name);chip.append(remove);box.append(chip);
  }
}
function workspaceRouteExtras(text) {
  closeAttachmentMenu();
  if(!conn.me?.user_id)return {};
  if(typeof dismissProactiveContext==="function")dismissProactiveContext();
  workspaceChat.loading++; // 新轮次使所有未完成的历史恢复失效，避免迟到快照覆盖回复。
  const input_files=workspaceChat.attachments.filter(x=>x.data.current_revision>0 && !x.data.source_url).map(x=>({item_id:x.id,revision:x.data.current_revision}));
  const attachments=workspaceChat.attachments.map(x=>x.id),matter_id=workspaceChat.matter?.id || null;
  const signature=JSON.stringify({text,attachments,input_files,matter_id,session:chat.sessionId});
  if(workspaceChat.request?.signature!==signature)workspaceChat.request={signature,id:crypto.randomUUID()};
  return {request_id:workspaceChat.request.id,attachments,input_files,matter_id,...(workspace.settings?{timezone:workspace.settings.timezone}:{})};
}
function workspaceRouteAccepted() {
  workspaceChat.attachments=[];workspaceChat.request=null;renderAttachmentChips();
}
async function loadMainConversation() {
  if(!conn.ready || !conn.me?.user_id || chat.sessionId)return;
  const epoch=conn.epoch,generation=chat.generation,loading=++workspaceChat.loading;
  const {items:loaded}=await api("GET",workspaceBase+"/messages?private_only=true&latest=true&limit=100");
  if(epoch!==conn.epoch || generation!==chat.generation || loading!==workspaceChat.loading || chat.sessionId)return;
  const records=loaded.filter(personalHistoryRecord);
  const {items:files}=await api("GET",workspaceBase+"/items?kind=file");
  if(epoch!==conn.epoch || generation!==chat.generation || loading!==workspaceChat.loading || chat.sessionId)return;
  $("transcript").replaceChildren();
  renderWorkspaceHistory(records,files,$("transcript"));
  if(records.length===100)prependOlderHistory(records[0].id,files,{epoch,generation,loading});
  $("transcript").scrollTop=$("transcript").scrollHeight;
  $("welcome").classList.toggle("hidden",records.length>0);
  $("chat-head").textContent=(workspace.settings?.assistant_name || "玄圃助手")+" · 主对话";
  const proactive=records.slice().reverse().find(x=>x.metadata?.route?.proactive_draft);
  if(proactive && !chat.routing)await restoreProactiveDraft(proactive.metadata.route.proactive_draft);
  const scheduled=records.slice().reverse().find(x=>x.metadata?.route?.task_definition_draft);
  if(scheduled && !chat.routing)await restoreTaskDefinitionDraft(scheduled.metadata.route.task_definition_draft);
  const pending=records.slice().reverse().find(x=>x.metadata?.route?.proposal_id && !x.metadata.route.proactive_draft && !x.metadata.route.task_definition_draft && !x.metadata.route.shared_draft && !x.metadata.route.open_shared);
  if(pending && !chat.routing)await restoreWorkspaceProposal(pending.metadata.route.proposal_id);
}
function personalHistoryRecord(item){return !item.source_context_id&&!item.source_blocked&&!item.sources?.length;}
function renderWorkspaceHistory(records,files,target) {
  for(const item of records) {
    if(!personalHistoryRecord(item))continue;
    const node=wsText("div",item.text,"msg "+item.role);
    if(item.metadata?.proposal_id)node.dataset.proposalId=item.metadata.proposal_id;
    target.append(node);
    if(item.metadata?.route?.shared_draft)target.append(wsButton("核对共享事项草稿",()=>restoreSharedDraft(item.metadata.route.shared_draft,item.metadata.route.proposal_id)));
    else if(item.metadata?.route?.open_shared)target.append(wsButton("查看共享事项",()=>openShared()));
    if(item.attachments?.length)renderMessageFileCards(files.filter(file=>item.attachments.includes(file.id)),target);
    if(item.metadata?.route?.work_task_id)showWorkTaskCard(item.metadata.route.work_task_id,target).catch(fail);
    if(item.metadata?.route?.session_id)target.append(wsButton("查看业务对话",async()=>openSession(await api("GET","/v1/sessions/"+encodeURIComponent(item.metadata.route.session_id)))));
  }
}
function prependOlderHistory(before,files,token) {
  const current=()=>token.epoch===conn.epoch && token.generation===chat.generation && !chat.sessionId;
  const more=wsButton("加载更早记录",async()=>{
    if(!current() || chat.routing || chat.streaming)return;
    const loading=workspaceChat.loading;
    more.disabled=true;more.textContent="正在加载…";
    try {
      const {items}=await api("GET",workspaceBase+"/messages?private_only=true&latest=true&limit=100&before_id="+before);
      if(!current() || loading!==workspaceChat.loading)return;
      const transcript=$("transcript"),height=transcript.scrollHeight,scroll=transcript.scrollTop;
      const fragment=document.createDocumentFragment();renderWorkspaceHistory(items,files,fragment);
      more.remove();transcript.prepend(fragment);
      if(items.length===100)prependOlderHistory(items[0].id,files,token);
      transcript.scrollTop=scroll+transcript.scrollHeight-height;
    } finally {
      if(more.isConnected){more.disabled=false;more.textContent="加载更早记录";}
    }
  });
  more.classList.add("history-more");$("transcript").prepend(more);
}
function talkAboutMatter(item) {
  if(chat.routing || chat.streaming){toast("请等待当前请求完成。","info");return;}
  document.querySelector('[data-tab="sessions"]').click();startConversation();
  workspaceChat.matter=item;renderAttachmentChips();closeWorkspace();
  $("message").value="请帮我继续处理这件事："+item.data.title;$("message").focus();
}
async function handleWorkspaceRoute(route,text) {
  if(!route.workspace_draft && !route.open_workspace)return false;
  $("welcome").classList.add("hidden");append("user",text);append("assistant",route.message || "请确认下面的内容。");
  renderMessageFileCards(workspaceChat.attachments);
  chat.routeContext=[];$("message").value="";workspaceRouteAccepted();
  if(route.workspace_draft)await showWorkspaceDraft(route.workspace_draft,route.proposal_id);
  else await openWorkspace(route.open_workspace);
  return true;
}
function closeAttachmentMenu(restoreFocus=false) {
  const menu=$("attachment-menu"),button=$("attach-file");
  const wasOpen=!menu.classList.contains("hidden");
  menu.classList.add("hidden");button.setAttribute("aria-expanded","false");
  if(wasOpen && restoreFocus && !button.disabled)button.focus();
}
function openAttachmentMenu(last=false) {
  if(!conn.ready || $("attach-file").disabled || chat.routing || chat.streaming)return;
  $("attachment-menu").classList.remove("hidden");$("attach-file").setAttribute("aria-expanded","true");
  const items=$("attachment-menu").querySelectorAll('[role="menuitem"]');
  items[last?items.length-1:0].focus();
}
$("attach-file").addEventListener("click",()=>{
  if($("attachment-menu").classList.contains("hidden"))openAttachmentMenu();else closeAttachmentMenu(true);
});
$("attach-file").addEventListener("keydown",event=>{
  if(["ArrowDown","ArrowUp"].includes(event.key)){event.preventDefault();openAttachmentMenu(event.key==="ArrowUp");}
});
$("attachment-upload").addEventListener("click",()=>{closeAttachmentMenu(true);$("attachment-input").click();});
$("attachment-pick").addEventListener("click",()=>{closeAttachmentMenu();openWorkspace("file",{picking:true}).catch(fail);});
// 菜单与业务草稿独立；移开焦点不删除已编辑的确认内容，也不抢外部输入焦点。
for(const name of ["pointerdown","focusin"])document.addEventListener(name,event=>{
  if(!$("attachment-menu").contains(event.target) && !$("attach-file").contains(event.target))closeAttachmentMenu();
},true);
document.addEventListener("keydown",event=>{
  if($("attachment-menu").classList.contains("hidden"))return;
  if(event.key==="Escape"){event.preventDefault();event.stopPropagation();closeAttachmentMenu(true);return;}
  if(event.key==="Tab"){closeAttachmentMenu(true);return;}
  if(!["ArrowDown","ArrowUp","Home","End"].includes(event.key))return;
  event.preventDefault();event.stopPropagation();
  const items=Array.from($("attachment-menu").querySelectorAll('[role="menuitem"]'));
  const index=items.indexOf(document.activeElement);
  const next=event.key==="Home"?0:event.key==="End"?items.length-1:(index+(event.key==="ArrowDown"?1:-1)+items.length)%items.length;
  items[next].focus();
},true);
$("attachment-input").addEventListener("change",async()=>{
  try{await uploadWorkspaceFiles($("attachment-input").files,{attach:true});}catch(error){fail(error);}finally{$("attachment-input").value="";}
});
const chatPane=document.querySelector(".chat-pane");
chatPane.addEventListener("dragover",event=>{if(conn.ready){event.preventDefault();chatPane.classList.add("file-drop-active");}});
chatPane.addEventListener("dragleave",()=>chatPane.classList.remove("file-drop-active"));
chatPane.addEventListener("drop",event=>{
  if(!conn.ready)return;event.preventDefault();chatPane.classList.remove("file-drop-active");
  if(chat.routing || chat.streaming){toast("请等待当前回复完成后再添加文件。","info");return;}
  uploadWorkspaceFiles(event.dataTransfer.files,{attach:true}).catch(fail);
});
$("message").addEventListener("paste",event=>{
  const files=Array.from(event.clipboardData?.files || []);if(!files.length)return;
  event.preventDefault();uploadWorkspaceFiles(files,{attach:true}).catch(fail);
});
function clearWorkspaceConversationContext() {
  closeAttachmentMenu();
  workspaceChat.matter=null;workspaceChat.request=null;
  $("workspace-draft").replaceChildren();$("workspace-draft").classList.add("hidden");renderAttachmentChips();
}
function renderMessageFileCards(items,target=$("transcript")) {
  if(!items.length)return;
  const card=wsText("div","","msg user attachment-chips");
  for(const item of items) {
    const button=wsButton("▤ "+item.data.name,()=>item.data.source_url?openWorkspace("file"):downloadWorkspaceFile(item));
    card.append(button);
  }target.append(card);
}
async function appendSentFiles(ids) {
  if(!ids?.length)return;
  const epoch=conn.epoch,generation=chat.generation;
  const items=await Promise.all(ids.map(id=>api("GET",`${workspaceBase}/items/${id}`)));
  if(epoch===conn.epoch && generation===chat.generation)renderMessageFileCards(items);
}
async function restoreWorkspaceProposal(proposalId) {
  const epoch=conn.epoch,generation=chat.generation,loading=workspaceChat.loading;
  const saved=await api("GET","/v1/assistant/proposals/"+encodeURIComponent(proposalId));
  if(epoch!==conn.epoch || generation!==chat.generation || loading!==workspaceChat.loading)return;
  if(saved.state!=="pending") {
    if(!Array.from($("transcript").querySelectorAll("[data-proposal-id]")).some(node=>node.dataset.proposalId===proposalId)) {
      const title=saved.result?.data?.title || saved.result?.data?.name || "提醒";
      const node=append("assistant",saved.state==="confirmed"?"已保存："+title:"已取消这项待确认操作。");
      node.dataset.proposalId=proposalId;
    }
    return;
  }
  if(saved.route.workspace_draft)await showWorkspaceDraft(saved.route.workspace_draft,proposalId);
  else showReminderDraft(saved.route.reminder_draft,proposalId);
}
async function appendWorkspaceProposals(generation,epoch) {
  const {items}=await api("GET","/v1/assistant/proposals");
  if(!workspaceCurrent(generation,epoch))return;
  if(items.length===100)$("workspace-content").prepend(wsText("p","当前显示最近 100 项待确认操作；处理后刷新可继续查看。","meta"));
  for(const item of items) {
    const data=item.route.workspace_draft?.data || item.route.reminder_draft;
    const card=wsText("article","","workspace-record");
    card.append(wsText("h3","等你确认 · "+(data.title || data.name)),wsText("p",workspaceTime(item.created_at),"meta"),wsButton("查看并确认",async()=>{
      if(chat.routing || chat.streaming){toast("请等待当前请求完成。","info");return;}
      document.querySelector('[data-tab="sessions"]').click();closeWorkspace();await restoreWorkspaceProposal(item.proposal_id);
    }));$("workspace-content").prepend(card);
  }
}
