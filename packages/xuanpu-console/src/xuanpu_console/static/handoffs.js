// 到人接收与交付均走原生接口；部门队列只是同一责任规则的可选视图。
"use strict";
async function openCollaboration(view="received",draft=null,proposalId=null,cursor=null,append=false){
 if(!append)collabClear();collabOpen();collabState.view=view;
 const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");collabNotice("正在核对本人的协作记录…");
 try{const page=await api("GET","/v1/handoffs?"+new URLSearchParams({view,limit:"50",...(cursor?{cursor}:{})}));if(!collabLive(epoch,generation))return;
  if(!Number.isInteger(page.total))throw new Error("工作数量格式无效，请刷新。");
  if(!append){for(const [value,label]of [["received","我收到的"],["sent","我发出的"],["assigned","我承办的"],["department_queue","可选部门队列"]])host.append(cButton(label,()=>openCollaboration(value)));
   host.append(cButton("我的组织审批",()=>openApprovals()),cNode("h3","工作交接（"+page.total+"）"));
   if(page.enabled)host.append(cButton(draft?"核对交接说明草稿":"交给具体员工",()=>handoffCreate(null,draft,proposalId)));
   if(isAdmin())host.append(cButton("配置审批规则",()=>approvalPolicies()));
  }
  for(const item of page.items){const card=cNode("article");card.className="function-card";
   card.append(cNode("h3",item.title),cNode("p",item.blocked_reason&&!["signed_off","returned","cancelled"].includes(item.status)?"需要重新安排":collabLabels[item.status]||"请核对当前记录"),cButton("查看与处理",()=>openHandoff(item.id)));host.append(card);}
  if(page.next_cursor)host.append(cButton("查看更多工作",()=>openCollaboration(view,null,null,page.next_cursor,true)));
  collabNotice(page.enabled?"可直接指定同事，部门可选。所有处理均需本人确认。":"新交接暂未开放，既有工作仍可按当前权限处理。");
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
async function openHandoff(id){
 collabClear();collabOpen();const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");collabNotice("正在核对当前责任与资料范围…");
 try{const record=await api("GET","/v1/handoffs/"+encodeURIComponent(id));if(!collabLive(epoch,generation))return;collabState.current=record;collabState.kind="handoff";
  host.append(cButton("返回我的协作",()=>openCollaboration(collabState.view)),cNode("h3",record.title),cNode("p",collabLabels[record.status]||"请核对状态"),cNode("p",record.summary));
  if(record.content_snapshot)host.append(cNode("p",record.content_snapshot));
  if(record.due_at)host.append(cNode("p","截止时间："+new Date(record.due_at*1000).toLocaleString()));
  collabNotice(record.blocked_reason?(["signed_off","returned","cancelled"].includes(record.status)?"工作已结束；部分历史资料当前不可读取。":"资料或责任已失效，需要负责人明确重新安排。"):"操作由当前责任及资料权限决定。");
  const actions=new Set(record.allowed_actions);
  if(!record.blocked_reason)host.append(cButton("查看本次可见资料并起草建议",()=>openSharedMatter(record.shared_id)));
  for(const [action,label]of [["accept","接收这项工作"],["start","开始处理"],["cancel","取消尚未接收的工作"],["confirm_cancel","确认取消"],["reopen","确认重新办理"]])if(actions.has(action))host.append(cButton(label,()=>collabAction(host,record,action.replaceAll("_","-"),label)));
  for(const [action,label]of [["return","退回工作"],["request_information","请求补充"],["request_cancel","请求取消"],["dispute","对本次交付提出争议"]])if(actions.has(action))collabReason(host,label,(card,reason)=>collabAction(card,record,action.replaceAll("_","-"),label,{},reason));
  if(actions.has("transfer")||actions.has("provide_information"))await handoffNewInput(host,record,actions,epoch,generation);if(!collabLive(epoch,generation))return;
  if(actions.has("publish_result"))handoffUpload(host,record);
  if(actions.has("deliver")||actions.has("submit_approval"))await handoffResults(host,record,actions,epoch,generation);if(!collabLive(epoch,generation))return;
  for(const delivery of record.deliveries||[]){const card=cNode("article");card.className="function-card";card.append(cNode("h4","固定交付 · 第"+delivery.delivery_version+"次"));host.append(card);
   for(const ref of delivery.result_refs)card.append(cNode("p","固定成果版本 "+ref.revision),cButton("下载并核验该成果",()=>collabDownload(record,ref,"交付成果-"+ref.revision)));
  }
  if(actions.has("sign_off"))host.append(cButton("核对本次成果后签收",()=>collabAction(host,record,"sign-off","签收本次固定交付",{delivery_version:record.delivery_version})));
  const history=cNode("details");history.append(cNode("summary","责任与处理历史（"+record.history.length+"）"));
  const names={submit:"提交",accept:"接收",start:"开始处理",return:"退回",request_information:"请求补充",provide_information:"明确新输入",transfer:"明确转交",deliver:"固定交付",sign_off:"签收",dispute:"提出争议",reopen:"重新办理",blocked:"需要重新安排",request_cancel:"请求取消",confirm_cancel:"确认取消",cancel:"取消"};
  for(const event of record.history)history.append(cNode("p",(names[event.action]||"状态更新")+" · "+(event.actor_id?"员工操作":"系统核对")+(event.reason?" · "+event.reason:"")));host.append(history);
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
async function handoffCreate(matter=null,draft=null,proposalId=null){
 collabClear();collabOpen();const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");
 try{const [people,matters,policies]=await Promise.all([collabPages("/v1/organization/people",epoch,generation),collabPages("/v1/shared-matters",epoch,generation),collabPages("/v1/approval-policies",epoch,generation)]);
  if(!collabLive(epoch,generation))return;const own=(matters||[]).filter(m=>m.owner_id===conn.me.user_id&&m.status==="active");
  if(!own.length){host.append(cNode("p","请先创建一项由你负责的共享事项，并明确发布可交接资料。"),cButton("查看共享事项",()=>openShared()));return;}
  const choices=(people||[]).filter(p=>p.user_id!==conn.me.user_id).map(p=>[p.user_id,p.display_name+"（"+p.user_id+"）"]);
  if(!choices.length){collabNotice("请管理员先准备另一名启用员工，无需部门。");return;}
  sharedForm(host,"明确交接来源",(form,card)=>{const source=sharedField(form,"由我负责的共享事项",matter?.id||own[0].id,own.map(m=>[m.id,m.title]));
   sharedSubmit(form,"选择本次资料",async()=>{try{
    const current=await api("GET",sharedPath(source.value)),materials=await collabPages(sharedPath(source.value)+"/materials",epoch,generation);if(!collabLive(epoch,generation))return;
    handoffInputForm(card,current,materials||[],choices,policies||[],draft,proposalId);
   }catch(error){if(collabLive(epoch,generation))collabNotice(error.message);}});
  });collabNotice("只选定本次交接需要的正文与固定材料；接收人不会获得整事项编辑权。");
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
function handoffInputForm(host,matter,materials,people,policies,draft,proposalId){
 const epoch=conn.epoch,generation=collabState.generation;
 sharedForm(host,"交给具体员工",(form,card)=>{
  const title=sharedField(form,"工作名称",draft?.title||matter.title),summary=sharedField(form,"交接说明",draft?.summary||""),person=sharedField(form,"接收员工",people[0][0],people),policy=sharedField(form,"成果审批规则","",[["","无需组织审批"],...policies.map(p=>[p.id,p.name])]);
  title.required=true;title.maxLength=500;summary.required=true;summary.maxLength=4000;
  const include=document.createElement("input");include.type="checkbox";const label=cNode("label","包括本次正文固定副本");label.append(include);form.append(label);
  const selected=collabMaterialChecks(form,materials),due=document.createElement("input");due.type="datetime-local";const dateLabel=cNode("label","截止时间（可选，按本机时区）");dateLabel.append(due);form.append(dateLabel);
  sharedSubmit(form,"核对交接",async()=>{
   const revisions=selected.filter(v=>v.check.checked);if(revisions.length>8){collabNotice("每次最多选择8个固定版本。");return;}
   const body={confirmed:true,expected_version:matter.version,shared_id:matter.id,target_kind:"user",target_id:person.value,title:title.value,summary:summary.value,include_content:include.checked,material_revision_ids:revisions.map(v=>v.revision.id),...(policy.value?{policy_id:policy.value}:{}),...(due.value?{due_at:new Date(due.value).getTime()/1000}:{})};
   const key=proposalId?proposalId+":"+await collabFingerprint(body):crypto.randomUUID();if(!collabLive(epoch,generation))return;body.request_id="handoff:"+key;
   collabReview(card,"提交交接","/v1/handoffs",body,["来源："+matter.title,"接收人："+people.find(p=>p[0]===person.value)?.[1],"成果审批规则："+(policies.find(p=>p.id===policy.value)?.name||"无需组织审批"),"截止时间："+(due.value?new Date(due.value).toLocaleString():"未设置"),"说明："+summary.value,"正文："+(include.checked?matter.content:"不包括"),...revisions.map(v=>v.material.name+" · 版本 "+v.revision.revision),"接收人需本人接收，只获本次选定资料。"],result=>openHandoff(result.id));
  });
 });
}
function collabMaterialChecks(host,materials){const selected=[];for(const material of materials)for(const revision of material.revisions){const label=cNode("label"),check=document.createElement("input");check.type="checkbox";label.append(check,cNode("span",material.name+" · 固定版本 "+revision.revision));host.append(label);selected.push({check,material,revision});}return selected;}
async function collabFingerprint(value){return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(JSON.stringify(value)))),n=>n.toString(16).padStart(2,"0")).join("");}
async function handoffNewInput(host,record,actions,epoch,generation){
 const source=await api("GET",sharedPath(record.shared_id)),materials=await collabPages(sharedPath(record.shared_id)+"/materials",epoch,generation),people=await collabPages("/v1/organization/people",epoch,generation);if(!collabLive(epoch,generation))return;
 for(const [action,label]of [["transfer","明确转交"],["provide_information","补充后重新提交"]])if(actions.has(action))sharedForm(host,label,(form,card)=>{
  const reason=sharedField(form,"原因"),summary=sharedField(form,"新的交接说明",record.summary);reason.required=true;reason.maxLength=4000;summary.required=true;summary.maxLength=4000;
  const choices=(people||[]).filter(p=>p.user_id!==record.requester_id).map(p=>[p.user_id,p.display_name+"（"+p.user_id+"）"]),person=action==="transfer"?sharedField(form,"新接收员工",choices[0]?.[0]||"",choices):null;
  const include=document.createElement("input");include.type="checkbox";const checkbox=cNode("label","明确包括当前正文副本");checkbox.append(include);form.append(checkbox);const selected=collabMaterialChecks(form,materials||[]);
  sharedSubmit(form,"核对"+label,()=>{const revisions=selected.filter(v=>v.check.checked);if(revisions.length>8){collabNotice("每次最多选择8个版本。");return;}
   const body={request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,shared_version:source.version,reason:reason.value,summary:summary.value,include_content:include.checked,material_revision_ids:revisions.map(v=>v.revision.id),...(person?{user_id:person.value}:{})};
   collabReview(card,label,"/v1/handoffs/"+encodeURIComponent(record.id)+"/"+action.replaceAll("_","-"),body,[reason.value,summary.value,...revisions.map(v=>v.material.name+" · 版本 "+v.revision.revision),"原承办权限收回，旧审批不沿用，新接收人需重新接受。"],()=>openHandoff(record.id));
  });
 });
}
