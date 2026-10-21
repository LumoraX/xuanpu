// 执行安排只产生草稿；服务端复核范围后，员工明确确认才授权。
"use strict";
const definitionState={generation:0,cards:new Set(),records:new Map(),enabled:false};
const definitionBase="/v1/task-definitions";
function definitionToken(){return {epoch:conn.epoch,generation:definitionState.generation};}
function definitionCurrent(token){return conn.ready && Boolean(conn.me?.user_id) && token.epoch===conn.epoch && token.generation===definitionState.generation;}
function resetTaskDefinitions(){definitionState.generation++;definitionState.enabled=false;for(const card of definitionState.cards)card.remove();definitionState.cards.clear();definitionState.records.clear();}
function definitionControls(card,disabled){for(const node of card.querySelectorAll("input,textarea,select,button"))node.disabled=disabled;}
function definitionSelect(form,label,options,value){
  const wrapper=wsText("label",label),input=document.createElement("select");input.setAttribute("aria-label",label);
  for(const [key,text] of options){const option=wsText("option",text);option.value=key;input.append(option);}input.value=value;wrapper.append(input);form.append(wrapper);return input;
}
function definitionField(form,label,type,value){
  const wrapper=wsText("label",label),input=document.createElement(type==="textarea"?"textarea":"input");
  if(type!=="textarea")input.type=type;input.value=value;input.required=true;input.setAttribute("aria-label",label);wrapper.append(input);form.append(wrapper);return input;
}
function definitionISO(wall,zone){
  const stamp=reminderTimestamp(wall,zone),offset=Math.round((Date.parse(wall+"Z")-stamp*1000)/60000);
  return wall+":00"+(offset>=0?"+":"-")+String(Math.floor(Math.abs(offset)/60)).padStart(2,"0")+":"+String(Math.abs(offset)%60).padStart(2,"0");
}
function definitionSummary(draft,card){
  const r=draft.request,display=draft.display||{},trigger=r.trigger;
  card.append(wsText("h3",r.title),wsText("p",r.template.text));
  card.append(wsText("p",trigger.kind==="time"?({once:"执行一次",daily:"每天",weekly:"每周"}[trigger.repeat]+" · 首次 "+trigger.at):"当本人"+({"workspace.file_revision_committed":"文件提交新版本","workspace.todo_completed":"待办完成","work_task.succeeded":"交办工作完成"}[trigger.event_type]||"业务发生变化")));
  if(trigger.kind==="event")card.append(wsText("p","事件范围："+(display.event_subject||"本人全部此类事件")));
  card.append(wsText("p","时区："+r.timezone+"；错过时"+(r.misfire_policy==="coalesce_latest"?"合并为最近一次":"跳过")+"；上次尚未结束时跳过重叠执行。"));
  const files=display.files||[];card.append(wsText("p",files.length?"材料："+files.map(f=>f.name+"（版本 "+f.revision+"）").join("、"):"无附件；仅调用已开通的只读业务能力。"));
  card.append(wsText("p",r.input_policy==="latest_at_occurrence"?"每次使用上述文件的最新版本，并在受理时固定内容。":"使用上述固定文件版本。"));
  if(display.capability)card.append(wsText("p","处理能力："+display.capability));
  card.append(wsText("p","最多执行 "+r.max_executions+" 次，每次最多尝试 "+r.max_attempts+" 次；累计模型额度 "+r.max_tokens+" tokens。"));
  if(display.task_token_limit)card.append(wsText("p","单项工作另受平台上限 "+display.task_token_limit+" tokens 限制；每次请求须预留完整上界。"));
  card.append(wsText("p","授权截止："+r.valid_until+"。额度或次数耗尽、文件失效或授权撤销后停止；不会因此扩大权限。"));
}
function showTaskDefinitionDraft(draft,target=$("transcript"),editing=null){
  const token=definitionToken(),card=wsText("article","","workspace-record definition-card");
  definitionState.cards.add(card);definitionSummary(draft,card);
  const feedback=wsText("p","尚未启用，请确认范围。","status");feedback.setAttribute("role","status");
  const payload={...structuredCloneDefinition(draft.request),confirmed:true};
  if(editing){payload.expected_version=editing.state_version;payload.stop_running=Boolean(draft.stop_running);}
  let sending=false;
  const confirm=wsButton(editing?"确认修改安排":"确认启用安排",async()=>{
    if(sending || !definitionCurrent(token))return;sending=true;definitionControls(card,true);feedback.textContent="正在确认…";
    try{
      const record=await api(editing?"PUT":"POST",definitionBase+(editing?"/"+encodeURIComponent(editing.definition_id):""),{json:payload});
      if(!definitionCurrent(token))return;
      definitionState.records.set(record.definition_id,record);card.replaceChildren(wsText("p",(editing?"已修改安排：":"已启用安排：")+record.title),wsButton("查看执行安排",()=>openWorkspace("definition")));
    }catch(error){
      if(!definitionCurrent(token))return;
      if(error.status && error.status<500){
        feedback.textContent=error.status===409?"记录或范围已改变，请读取最新状态并重新确认。":error.message;
        card.append(wsButton("重新核对范围",async()=>{if(!definitionCurrent(token))return;if(editing){await openWorkspace("definition");return;}await restoreTaskDefinitionDraft(draft,target);card.remove();}));
      }else{feedback.textContent="确认结果尚未查明，范围已固定；请重试同一确认。";confirm.disabled=false;confirm.textContent="重试同一确认";}
    }finally{sending=false;}
  });card.append(feedback,confirm);target.append(card);confirm.focus();return card;
}
function structuredCloneDefinition(value){return JSON.parse(JSON.stringify(value));}
async function restoreTaskDefinitionDraft(saved,target=$("transcript")){
  const token=definitionToken(),chatGeneration=chat.generation,loading=workspaceChat.loading;
  try{
    const draft=await api("POST",definitionBase+"/preview",{json:{...saved.request,confirmed:false}});
    if(definitionCurrent(token)&&chatGeneration===chat.generation&&loading===workspaceChat.loading)showTaskDefinitionDraft(draft,target);
  }catch(error){if(definitionCurrent(token)&&chatGeneration===chat.generation&&loading===workspaceChat.loading)target.append(wsText("p","这份安排草稿需要重新核对时间、材料或权限，请重新提出安排。","status"));}
}
async function handleTaskDefinitionRoute(route,text){
  if(!route.task_definition_draft&&!route.open_task_definitions)return false;
  $("welcome").classList.add("hidden");append("user",text);renderMessageFileCards(workspaceChat.attachments);append("assistant",route.message||"请确认执行安排。");
  chat.routeContext=[];$("message").value="";workspaceRouteAccepted();
  if(route.task_definition_draft)showTaskDefinitionDraft(route.task_definition_draft);else await openWorkspace("definition");return true;
}
async function renderTaskDefinitionList(generation,epoch,after=null){
  const body=await api("GET",definitionBase+"?limit=50"+(after?"&after_id="+encodeURIComponent(after):""));
  const options=after?{enabled:definitionState.enabled}:await api("GET",definitionBase+"/options");if(!workspaceCurrent(generation,epoch))return;
  const box=$("workspace-content");definitionState.enabled=options.enabled;
  if(!after){
    const create=wsButton("新建执行安排",()=>showTaskDefinitionCreate());create.disabled=!options.enabled;
    $("workspace-toolbar").append(create,wsButton("刷新",refreshWorkspace));
    if(!options.enabled)box.append(wsText("p","执行安排暂未启用。已有记录仍可查看、暂停和取消。","status"));
    if(!body.items.length)box.append(wsText("p","暂无执行安排。你也可以在对话中说明何时替你处理什么。"));
  }
  for(const record of body.items){definitionState.records.set(record.definition_id,record);box.append(taskDefinitionCard(record,options?.enabled));}
  if(body.items.length===50){const more=wsButton("加载更多",async()=>{await renderTaskDefinitionList(generation,epoch,body.items.at(-1).definition_id);more.remove();});box.append(more);}
}
function taskDefinitionCard(record,enabled=definitionState.enabled){
  const card=wsText("article","","workspace-record definition-card");renderTaskDefinition(card,record,enabled);return card;
}
function renderTaskDefinition(card,record,enabled){
  card.replaceChildren();card.append(wsText("h3",record.title),wsText("p",record.cancelled?"已取消":record.enabled?"已启用":"已暂停","tag"),wsText("p",record.confirmed.template.text),wsText("p","已受理 "+record.executions+" 次 · "+record.confirmed.timezone));
  if(record.next_fire_at)card.append(wsText("p","下次计划："+workspaceTime(record.next_fire_at)));
  if(record.pause_reason&&!record.cancelled)card.append(wsText("p","当前安排已暂停，请检查材料、权限、额度和有效期。"));
  if(!record.cancelled){
    if(record.enabled)card.append(definitionActionButton(record,"pause","暂停未来执行",false,card),definitionActionButton(record,"pause","暂停并停止在途",true,card));
    else if(enabled)card.append(definitionActionButton(record,"resume","恢复原安排",false,card));
    if(enabled)card.append(wsButton("修改并重新确认",()=>showTaskDefinitionCreate(record)));
    card.append(definitionActionButton(record,"cancel","取消安排并停止在途",true,card));
  }
  card.append(wsButton("查看每次执行",()=>showDefinitionOccurrences(record,card)));
}
function definitionActionButton(record,action,label,stop,card){
  const token=definitionToken(),payload={request_id:crypto.randomUUID(),expected_version:record.state_version,stop_running:stop};let sending=false;
  const button=wsButton(label,async()=>{
    if(!definitionCurrent(token)||sending)return;sending=true;definitionControls(card,true);
    try{const fresh=await api("POST",definitionBase+"/"+encodeURIComponent(record.definition_id)+"/"+action,{json:payload});
      if(definitionCurrent(token)){definitionState.records.set(fresh.definition_id,fresh);renderTaskDefinition(card,fresh,definitionState.enabled);}
    }catch(error){if(!definitionCurrent(token))return;
      if(error.status&&error.status<500){card.append(wsText("p",error.status===409?"安排已更新，请刷新后重新选择操作。":error.message),wsButton("读取最新状态",refreshWorkspace));}
      else{button.disabled=false;button.textContent="重试同一操作";card.append(wsText("p","操作结果待确认，已固定原请求。","status"));}
    }finally{sending=false;}
  });return button;
}
async function showDefinitionOccurrences(record,target,after=null){
  const token=definitionToken(),generation=workspace.generation;
  const body=await api("GET",definitionBase+"/"+encodeURIComponent(record.definition_id)+"/occurrences?limit=50"+(after?"&after_id="+encodeURIComponent(after):""));
  if(!definitionCurrent(token)||!workspaceCurrent(generation,token.epoch))return;
  const box=wsText("section","","definition-occurrences");box.append(wsText("h4","每次执行"));
  for(const item of body.items){box.append(wsText("p",workspaceTime(item.scheduled_at)+" · "+({accepted:"已受理",pending:"待执行",missed:"已错过",skipped_overlap:"跳过重叠",blocked:"等待核对",cancelled:"已取消"}[item.status]||"待核实")));if(item.task_id)await showWorkTaskCard(item.task_id,box);}
  if(!definitionCurrent(token)||!workspaceCurrent(generation,token.epoch))return;
  if(!body.items.length)box.append(wsText("p","尚无执行记录。"));
  if(body.items.length===50)box.append(wsButton("加载更多执行记录",()=>showDefinitionOccurrences(record,box,body.items.at(-1).occurrence_id)));
  target.append(box);
}
async function showTaskDefinitionCreate(editing=null){
  const token=definitionToken(),generation=workspace.generation;
  const [options,body]=await Promise.all([api("GET",definitionBase+"/options"),api("GET",workspaceBase+"/items")]);
  if(!definitionCurrent(token)||!workspaceCurrent(generation,token.epoch))return;
  if(!options.enabled)throw new Error("执行安排暂未启用。");
  const box=$("workspace-content"),form=wsText("form","","workspace-form definition-form"),r=editing?.confirmed;
  form.append(wsText("h3",editing?"修改执行安排":"新建执行安排"));
  const fields=definitionFormFields(form,r,options,body.items);
  const feedback=wsText("p","先核对范围，再由你明确确认。","status"),submit=wsButton("核对并预览",()=>{});submit.type="submit";feedback.setAttribute("role","status");form.append(feedback,submit);box.replaceChildren(form);
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!definitionCurrent(token)||submit.disabled)return;
    try{
      const request=definitionFormRequest(fields);submit.disabled=true;
      const draft=await api("POST",definitionBase+"/preview",{json:request});
      if(definitionCurrent(token)&&workspaceCurrent(generation,token.epoch)){draft.stop_running=fields.stop?.checked||false;box.replaceChildren();showTaskDefinitionDraft(draft,box,editing);}
    }catch(error){if(definitionCurrent(token)&&workspaceCurrent(generation,token.epoch)){feedback.textContent=error.message;submit.disabled=false;}}
  });fields.title.focus();
}
function definitionFormFields(form,r,options,items){
  const zone=r?.timezone||workspace.settings?.timezone||"Asia/Shanghai",f={};
  f.title=definitionField(form,"安排名称","text",r?.title||"");f.title.maxLength=500;
  f.goal=definitionField(form,"执行目标","textarea",r?.template.text||"");f.goal.maxLength=8000;
  const cap=options.capabilities.map((c,i)=>[String(i),c.label]);
  const initial=r?.template.kind==="capability"?String(options.capabilities.findIndex(c=>c.package===r.template.capability?.package&&c.entry===r.template.capability?.entry)):"office";
  f.capability=definitionSelect(form,"处理方式",[["office","处理材料并保存成果"],...cap],initial);f.capabilities=options.capabilities;
  f.files=[];const selected=new Map((r?.template.input_files||[]).map(file=>[file.item_id,file.revision]));
  for(const item of items.filter(i=>i.kind==="file"&&!i.trashed_at&&i.data.current_revision>0&&!i.data.source_url).slice(0,100)){
    const label=wsText("label",item.data.name+" · 版本 "+(selected.get(item.id)||item.data.current_revision)),check=document.createElement("input");check.type="checkbox";check.checked=selected.has(item.id);label.append(check);form.append(label);f.files.push({check,item,revision:selected.get(item.id)||item.data.current_revision});
  }
  f.zone=definitionField(form,"时区","text",zone);
  const t=r?.trigger;
  f.trigger=definitionSelect(form,"何时执行",[["once","指定时间一次"],["daily","每天"],["weekly","每周"],["workspace.file_revision_committed","本人文件提交新版本时"],["workspace.todo_completed","本人待办完成时"],["work_task.succeeded","本人交办工作完成时"]],t?.kind==="event"?t.event_type:t?.repeat||"once");
  f.at=definitionField(form,"首次执行时间（时间安排必填）","datetime-local",reminderWallTime(t?.kind==="time"?Date.parse(t.at)/1000:Date.now()/1000+3600,zone));f.at.required=false;
  const subjects=items.filter(i=>["file","todo"].includes(i.kind)&&!i.trashed_at).map(i=>[i.id,(i.kind==="file"?"文件：":"待办：")+(i.data.name||i.data.title)]);
  f.subject=definitionSelect(form,"事件范围（仅事件安排使用）",[["","本人全部此类事件"],...subjects],t?.kind==="event"?t.subject_id||"":"");
  f.policy=definitionSelect(form,"文件版本",[["fixed_revision","固定当前选中版本"],["latest_at_occurrence","每次采用选中文件最新版本"]],r?.input_policy||"fixed_revision");
  f.misfire=definitionSelect(form,"停机错过时",[["skip","跳过错过的执行"],["coalesce_latest","合并执行最近一次"]],r?.misfire_policy||"skip");
  f.count=definitionField(form,"最多执行次数","number",String(r?.max_executions||3));f.count.min=1;f.count.max=100;
  f.attempts=definitionField(form,"每次最多尝试次数","number",String(r?.max_attempts||3));f.attempts.min=1;f.attempts.max=3;
  f.tokens=definitionField(form,"累计模型额度（tokens）","number",String(r?.max_tokens||300000));f.tokens.min=1;f.tokens.max=15000000;
  f.expiry=definitionField(form,"授权截止时间","datetime-local",reminderWallTime(r?Date.parse(r.valid_until)/1000:Date.now()/1000+30*86400,zone));
  if(r){const label=wsText("label","同时停止旧版在途工作"),check=document.createElement("input");check.type="checkbox";label.append(check);form.append(label);f.stop=check;}
  return f;
}
function definitionFormRequest(f){
  const template={kind:"office",text:f.goal.value.trim(),input_files:f.files.filter(row=>row.check.checked).map(row=>({item_id:row.item.id,revision:row.revision}))};
  if(f.capability.value!=="office"){const cap=f.capabilities[Number(f.capability.value)];if(!cap)throw new Error("请选择当前已开通的处理方式。");template.kind="capability";template.capability={package:cap.package,entry:cap.entry};}
  const event=f.trigger.value.startsWith("workspace.")||f.trigger.value==="work_task.succeeded";
  const trigger=event?{kind:"event",event_type:f.trigger.value,subject_id:f.subject.value||null}:{kind:"time",repeat:f.trigger.value,at:definitionISO(f.at.value,f.zone.value)};
  return {request_id:crypto.randomUUID(),confirmed:false,title:f.title.value.trim(),template,trigger,timezone:f.zone.value,input_policy:f.policy.value,misfire_policy:f.misfire.value,overlap_policy:"skip",max_executions:Number(f.count.value),max_attempts:Number(f.attempts.value),max_tokens:Number(f.tokens.value),valid_until:definitionISO(f.expiry.value,f.zone.value)};
}
