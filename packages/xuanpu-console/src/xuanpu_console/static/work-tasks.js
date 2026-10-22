// 员工交办的持久状态与确认卡；网页只订阅，执行由服务端持有。
"use strict";
const workTaskState={generation:0,records:new Map(),cards:new Map(),timer:null};
const taskStates={accepted:"已受理",running:"正在处理",waiting_user:"等待你确认",succeeded:"已完成",failed:"处理失败",cancelled:"已停止",interrupted:"已中断",outcome_unknown:"结果待核实"};
const taskTerminal=new Set(["succeeded","failed","cancelled","interrupted","outcome_unknown"]);
function taskCurrent(epoch,generation){return conn.ready && conn.me?.user_id && epoch===conn.epoch && generation===workTaskState.generation;}
function resetWorkTasks(){
  workTaskState.generation++;clearTimeout(workTaskState.timer);workTaskState.timer=null;
  for(const cards of workTaskState.cards.values())for(const card of cards)card.remove();
  workTaskState.records.clear();workTaskState.cards.clear();
}
function registerTaskCard(id,card){
  if(!workTaskState.cards.has(id))workTaskState.cards.set(id,new Set());
  workTaskState.cards.get(id).add(card);card.dataset.workTask=id;
}
function workTaskCard(task){
  const card=wsText("article","","workspace-record work-task-card");registerTaskCard(task.task_id,card);renderWorkTask(card,task);return card;
}
function updateWorkTask(task){
  if(!task || !Number.isInteger(task.state_version) || typeof task.task_id!=="string")throw new Error("任务响应无效。");
  const old=workTaskState.records.get(task.task_id);if(old && (old.state_version>task.state_version || (old.activity_cursor||0)>(task.activity_cursor||0)))return;
  workTaskState.records.set(task.task_id,task);
  for(const card of workTaskState.cards.get(task.task_id)||[]){if(card.isConnected)renderWorkTask(card,task);else workTaskState.cards.get(task.task_id).delete(card);}
  scheduleWorkTaskPoll();
}
async function refreshWorkTask(id){
  const epoch=conn.epoch,generation=workTaskState.generation;
  try{
    const task=await api("GET","/v1/work-tasks/"+encodeURIComponent(id));
    if(taskCurrent(epoch,generation))updateWorkTask(task);
    return taskCurrent(epoch,generation)?task:null;
  }catch(error){
    if(taskCurrent(epoch,generation) && [403,404].includes(error.status)){
      workTaskState.records.delete(id);
      for(const card of workTaskState.cards.get(id)||[])card.replaceChildren(wsText("p","此任务不可访问。"));
    }
    throw error;
  }
}
function scheduleWorkTaskPoll(){
  if(workTaskState.timer || !conn.ready || !conn.me?.user_id)return;
  const active=[...workTaskState.cards.keys()].some(id=>!taskTerminal.has(workTaskState.records.get(id)?.status));
  if(!active)return;
  const epoch=conn.epoch,generation=workTaskState.generation;
  workTaskState.timer=setTimeout(async()=>{
    workTaskState.timer=null;if(!taskCurrent(epoch,generation))return;
    const ids=[...workTaskState.cards.keys()].filter(id=>{
      const cards=workTaskState.cards.get(id);for(const card of cards)if(!card.isConnected)cards.delete(card);
      if(!cards.size){workTaskState.cards.delete(id);return false;}
      return !taskTerminal.has(workTaskState.records.get(id)?.status);
    });
    for(const id of ids){if(!taskCurrent(epoch,generation))return;try{await refreshWorkTask(id);}catch(error){if(error.status===401)break;}}
    if(taskCurrent(epoch,generation))scheduleWorkTaskPoll();
  },2000);
}
function renderWorkTask(card,task){
  if(card.taskVersion===task.state_version)return;card.taskVersion=task.state_version;card.replaceChildren();
  card.append(wsText("h3",task.text),wsText("p",task.block_reason?.includes("budget")?"模型额度不足":taskStates[task.status]||"状态待核实","tag"));
  if(task.result_summary)card.append(wsText("p",task.result_summary,"work-task-result"));
  if(task.status==="accepted")card.append(wsText("p","已保存交办，等待开始。关闭页面后仍会继续。"));
  if(task.status==="running")card.append(wsText("p",task.cancel_requested?"已请求停止，正在核实退出与执行结果。":"正在处理固定版本的材料，完成后校验并保存成果。"));
  if(task.status==="outcome_unknown")card.append(wsText("p","执行结果尚待核实，请勿重复发起同一操作。"));
  if(task.status==="interrupted")card.append(wsText("p","任务已中断，原执行不会自动重跑。已保存成果仍可下载。"));
  if(task.status==="failed")card.append(wsText("p",task.error_message||"本次处理未完成。请检查材料格式、时间和文件权限后再交办。"));
  appendWorkTaskExecution(card,task);
  const epoch=conn.epoch,generation=workTaskState.generation;
  for(const artifact of task.artifacts||[]){
    const button=wsButton("下载 "+artifact.name,async()=>{
      const item=await api("GET",workspaceBase+"/items/"+encodeURIComponent(artifact.item_id));
      if(taskCurrent(epoch,generation))await downloadWorkspaceFile(item,artifact.revision);
    });card.append(button);
  }
  if(task.status==="waiting_user"){
    if(task.action_candidates?.length)card.append(workTaskActionsForm(task));
    else if(task.pending?.length)card.append(workTaskApprovalForm(task));
    else card.append(wsText("p","正在同步需要确认的内容，请刷新查看。"));
  }
  for(const result of task.action_results||[])card.append(wsText("p",result.accepted?"已创建"+({todo:"待办",event:"日程"}[result.kind]||"记录")+"："+(result.title||"已确认行动项"):"该行动项未创建。"));
  const row=wsText("div","","row");row.append(wsButton("刷新进展",()=>refreshWorkTask(task.task_id)));
  if(!taskTerminal.has(task.status)){
    const stop=wsButton("停止这项工作",async()=>{
      stop.disabled=true;
      try{await api("POST","/v1/work-tasks/"+encodeURIComponent(task.task_id)+"/cancel");if(taskCurrent(epoch,generation))await refreshWorkTask(task.task_id);}
      finally{if(taskCurrent(epoch,generation))stop.disabled=false;}
    });row.append(stop);
  }
  card.append(row);scheduleWorkTaskPoll();
}
function taskField(label,type,value=""){
  const wrapper=wsText("label",label),input=document.createElement("input");input.type=type;input.value=value;input.setAttribute("aria-label",label);wrapper.append(input);return {wrapper,input};
}
function workTaskActionsForm(task){
  const form=document.createElement("form"),rows=[];form.className="work-task-actions";
  form.append(wsText("p","只创建你选中的本人记录。可编辑标题和时间，也可以全部取消。时区："+task.timezone));
  for(const entry of task.action_candidates){
    const candidate=entry.candidate,section=document.createElement("fieldset"),check=document.createElement("input");check.type="checkbox";
    check.disabled=candidate.owner!=="self";check.dataset.taskLocked=check.disabled?"yes":"no";const legend=wsText("label",candidate.owner==="self"?"创建这条本人记录":"负责人待核实，此项暂不能创建");legend.append(check);section.append(legend);
    const title=taskField("行动项标题","text",candidate.title);section.append(title.wrapper);
    const fields={};for(const key of candidate.kind==="event"?["start_at","end_at"]:["due_at"]){
      const label={start_at:"开始时间",end_at:"结束时间",due_at:"截止时间（可留空）"}[key];
      fields[key]=taskField(label,"datetime-local",candidate[key]?reminderWallTime(candidate[key],task.timezone):"").input;
      const wrapper=wsText("label",label);wrapper.append(fields[key]);section.append(wrapper);
    }
    section.append(wsText("blockquote",candidate.source.quote));
    if(candidate.remind_at)section.append(wsText("p","所选记录同时创建站内提醒："+new Date(candidate.remind_at*1000).toLocaleString("zh-CN",{timeZone:task.timezone})));
    rows.push({entry,check,title:title.input,fields});form.append(section);
  }
  const submit=document.createElement("button");submit.type="submit";submit.textContent="确认所选行动项";form.append(submit);
  const feedback=wsText("p","","status");form.append(feedback);
  form.addEventListener("submit",async event=>{
    event.preventDefault();let choices;try{choices=rows.filter(row=>row.check.checked).map(row=>{
      const choice={action_id:row.entry.action_id,accepted:true,title:row.title.value.trim()};
      for(const [key,input] of Object.entries(row.fields))choice[key]=input.value?reminderTimestamp(input.value,task.timezone):null;
      return choice;
    });}catch(error){feedback.textContent=error.message;return;}
    await postWorkTaskDecision(task,form,submit,feedback,{kind:"office_actions",choices});
  });
  const cancel=wsButton("仅保存纪要，取消全部行动项",()=>postWorkTaskDecision(task,form,submit,feedback,{kind:"office_actions",choices:[]}));form.append(cancel);
  return form;
}
function workTaskApprovalForm(task){
  const form=document.createElement("form"),rows=[];
  form.append(wsText("p","这一步需要你的明确决定。未决定的操作继续等待，不会自动批准。"));
  for(const pending of task.pending){
    const label=wsText("label",pending.summary||{tool_use:"调用业务服务",shell_exec:"执行处理命令",script_exec:"运行材料处理",file_read:"读取材料",file_write:"保存处理结果"}[pending.detail?.scope]||"待确认操作");
    const select=document.createElement("select");select.setAttribute("aria-label",label.textContent);
    for(const [value,text] of [["","暂不决定"],["yes","允许"],["no","拒绝"]]){const option=wsText("option",text);option.value=value;if(value==="yes"&&pending.can_approve===false)option.disabled=true;select.append(option);}label.append(select);form.append(label);
    rows.push({id:pending.request_id,select});
  }
  const submit=document.createElement("button");submit.type="submit";submit.textContent="提交所选决定";form.append(submit);
  const feedback=wsText("p","","status");form.append(feedback);
  form.addEventListener("submit",async event=>{event.preventDefault();const decisions={};for(const row of rows)if(row.select.value)decisions[row.id]={granted:row.select.value==="yes"};
    if(!Object.keys(decisions).length){feedback.textContent="请至少决定一项操作。";return;}
    await postWorkTaskDecision(task,form,submit,feedback,{kind:"tool_approval",decisions});
  });return form;
}
async function postWorkTaskDecision(task,form,button,feedback,value){
  const epoch=conn.epoch,generation=workTaskState.generation;
  if(form.sending)return;
  if(!form.attempt)form.attempt={...value,request_id:crypto.randomUUID(),state_version:task.state_version};
  form.sending=true;for(const input of form.querySelectorAll("input,select,textarea,button"))input.disabled=true;
  feedback.textContent="正在提交…";
  try{
    const updated=await api("POST","/v1/work-tasks/"+encodeURIComponent(task.task_id)+"/decisions",{json:form.attempt});
    if(taskCurrent(epoch,generation)){updateWorkTask(updated);await refreshWorkspaceSummary();}
  }catch(error){
    if(!taskCurrent(epoch,generation))return;
    if(error.status && error.status<500){feedback.textContent=error.message;form.attempt=null;for(const input of form.querySelectorAll("input,select,textarea,button"))input.disabled=input.dataset?.taskLocked==="yes";if(error.status===409)await refreshWorkTask(task.task_id);}
    else feedback.textContent="提交结果尚未确认，内容已固定；请重试同一决定。";
    button.disabled=false;button.textContent="重试提交";
  }finally{form.sending=false;}
}
async function showWorkTaskCard(id,target){
  const cached=workTaskState.records.get(id),card=cached?workTaskCard(cached):wsText("article","正在读取工作进展…","workspace-record work-task-card");
  if(!cached)registerTaskCard(id,card);target.append(card);
  try{await refreshWorkTask(id);}catch(error){if(conn.ready && card.isConnected)card.replaceChildren(wsText("p",error.message));}
  return card;
}
async function handleWorkTaskRoute(route,text){
  if(!route.work_task_id && !route.preserve_input)return false;
  $("welcome").classList.add("hidden");append("user",text);renderMessageFileCards(workspaceChat.attachments);append("assistant",route.message||"工作已受理。");
  if(route.preserve_input)return true;
  chat.routeContext=[];$("message").value="";workspaceRouteAccepted();
  await showWorkTaskCard(route.work_task_id,$("transcript"));return true;
}
async function renderWorkTaskList(generation,epoch,after=null){
  const body=await api("GET","/v1/work-tasks?limit=50"+(after?"&after_id="+encodeURIComponent(after):""));
  if(!workspaceCurrent(generation,epoch))return;
  const box=$("workspace-content");if(!after){$("workspace-toolbar").append(wsButton("交办工作",()=>showWorkTaskCreate()),wsButton("刷新",refreshWorkspace));if(!body.items.length)box.append(wsText("p","这里还没有交办记录。可以在对话中添加材料并交办，也可以直接交办工作。"));}
  for(const task of body.items){updateWorkTask(task);box.append(workTaskCard(task));}
  if(body.items.length===50){const more=wsButton("加载更多",async()=>{more.disabled=true;try{await renderWorkTaskList(generation,epoch,body.items.at(-1).task_id);more.remove();}finally{more.disabled=false;}});box.append(more);}
}
async function showWorkTaskCreate(){
  const epoch=conn.epoch,generation=workTaskState.generation,workspaceGeneration=workspace.generation;
  const {items}=await api("GET",workspaceBase+"/items?kind=file");
  if(!taskCurrent(epoch,generation)||!workspaceCurrent(workspaceGeneration,epoch))return;
  const box=$("workspace-content"),form=document.createElement("form"),goal=document.createElement("textarea"),checks=[];
  goal.required=true;goal.maxLength=8000;goal.setAttribute("aria-label","交办目标");goal.placeholder="例如：整理会议纪要，提出我的行动项；或按部门汇总表格金额。";form.append(wsText("h3","交办工作"),goal);
  for(const item of items.filter(item=>!item.trashed_at && item.data.current_revision>0 && !item.data.source_url).slice(0,100)){
    const label=wsText("label",item.data.name+" · 版本 "+item.data.current_revision),check=document.createElement("input");check.type="checkbox";label.append(check);form.append(label);checks.push({item,check});
  }
  form.append(wsText("p","选择1至8份本人文件；会议支持文本、DOCX与正文PDF，表格支持CSV与无公式XLSX。"));
  const button=document.createElement("button");button.type="submit";button.textContent="交办并开始处理";form.append(button);const feedback=wsText("p","","status");form.append(feedback);
  form.addEventListener("submit",async event=>{event.preventDefault();if(form.sending)return;
    if(!form.attempt){const selected=checks.filter(row=>row.check.checked);if(!goal.value.trim()||selected.length<1||selected.length>8){feedback.textContent="请填写目标，并选择1至8份文件。";return;}
      form.attempt={text:goal.value.trim(),request_id:crypto.randomUUID(),timezone:workspace.settings?.timezone||browserTimezone(),input_files:selected.map(row=>({item_id:row.item.id,revision:row.item.data.current_revision}))};}
    form.sending=true;for(const node of form.querySelectorAll("input,textarea,button"))node.disabled=true;
    try{const task=await api("POST","/v1/work-tasks",{json:form.attempt});if(taskCurrent(epoch,generation)&&workspaceCurrent(workspaceGeneration,epoch)){updateWorkTask(task);box.replaceChildren(workTaskCard(task));}}
    catch(error){if(!taskCurrent(epoch,generation))return;feedback.textContent=error.message;if(error.status && error.status<500){form.attempt=null;for(const node of form.querySelectorAll("input,textarea,button"))node.disabled=false;}else{feedback.textContent="受理结果尚未确认，材料和目标已固定；请重试同一交办。";button.disabled=false;button.textContent="重试同一交办";}}
    finally{form.sending=false;}
  });box.replaceChildren(form);goal.focus();
}

function appendWorkTaskExecution(card,task){
  if(task.task_token_limit)card.append(wsText("p","单项工作模型额度上限："+task.task_token_limit+" tokens；未知用量仍按完整预留计入。","meta"));
  const blocked={connection_reauthorization_required:"企业连接需要本人重新授权。请确认连接后，再明确继续这项工作。",budget:"模型额度不足，等待核实或补充授权。",block_budget:"模型额度不足，等待核实或补充授权。",definition_paused:"执行安排已暂停。",source_invalid:"原材料或授权已失效。",block_resume:"原操作无法安全恢复，请先核实。"};
  if(task.recovering){card.append(wsText("p","正在恢复 · 第 "+task.attempt_no+" 次尝试","tag"),wsText("p",task.recovery_mode==="save_artifacts"?"继续保存成果":task.recovery_mode==="await_decision"?"等待批准或核实":"重新执行"));}
  if(task.block_reason)card.append(wsText("p",blocked[task.block_reason]||"执行已暂停，等待核对材料、权限或结果。","status"));
  else if(task.next_run_at&&!taskTerminal.has(task.status))card.append(wsText("p","排队等待执行 · "+workspaceTime(task.next_run_at)));
  if(task.block_reason==="connection_reauthorization_required"){
    card.append(wsButton("查看本人连接",()=>typeof openConnections==="function"?openConnections():toast("请打开功能中的企业连接")));
    if(!task.source_occurrence)card.append(workTaskConnectionButton(task));
  }
  if(task.source_occurrence)card.append(wsButton("查看原执行安排",()=>openWorkspace("definition")));
  if(task.status==="failed"&&task.available_actions?.includes("create_followup"))card.append(wsButton("重新交办",()=>openWorkspace("task").then(()=>showWorkTaskCreate())));
  for(const effect of task.effects||[])if(effect.can_reconcile)card.append(workTaskReconcileButton(task,effect));
}
function workTaskReconcileButton(task,effect){
  const epoch=conn.epoch,generation=workTaskState.generation,payload={request_id:crypto.randomUUID(),expected_version:task.state_version};let sending=false;
  const button=wsButton("查询权威回执",async()=>{
    if(sending||!taskCurrent(epoch,generation))return;sending=true;button.disabled=true;
    try{await api("POST","/v1/work-tasks/"+encodeURIComponent(task.task_id)+"/effects/"+encodeURIComponent(effect.action_id)+"/reconcile",{json:payload});if(taskCurrent(epoch,generation))await refreshWorkTask(task.task_id);}
    catch(error){if(taskCurrent(epoch,generation)){button.textContent=error.status===409?"状态已变化，请刷新进展":"重试同一回执查询";if(!error.status||error.status>=500)button.disabled=false;}}
    finally{sending=false;}
  });return button;
}

function workTaskConnectionButton(task){
  const epoch=conn.epoch,generation=workTaskState.generation;
  const payload={request_id:crypto.randomUUID(),expected_version:task.state_version};let sending=false;
  const button=wsButton("已重新授权，继续原工作",async()=>{
    if(sending||!taskCurrent(epoch,generation))return;sending=true;button.disabled=true;
    try{const result=await api("POST","/v1/work-tasks/"+encodeURIComponent(task.task_id)+"/connection-authorization",{json:payload});if(taskCurrent(epoch,generation))updateWorkTask(result);}
    catch(error){if(taskCurrent(epoch,generation)){button.textContent=error.status===409?"状态已变化，请刷新进展":"重试同一次继续确认";if(!error.status||error.status>=500)button.disabled=false;toast(error.message);}}
    finally{sending=false;}
  });return button;
}
