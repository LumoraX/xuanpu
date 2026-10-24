// 到人共享的辅助入口；确认卡固定操作号与版本，切换身份立即丢弃旧响应。
"use strict";
const sharedState={generation:0,matter:null,context:null,enabled:false};
function sharedLive(epoch,generation){return epoch===conn.epoch&&generation===sharedState.generation;}
function sharedNode(tag,text=""){return connectionText(tag,text);}
function sharedButton(text,action){return connectionButton(text,action);}
function sharedPath(id){return "/v1/shared-matters/"+encodeURIComponent(id);}
function sharedNotice(text){$("shared-feedback").textContent=text;}
function sharedInvalidate(message){
 sharedState.generation++;sharedState.matter=null;sharedState.context=null;sharedState.materials=[];
 $("shared-list").replaceChildren();sharedNotice(message);
}
function resetShared(){
 sharedState.generation++;sharedState.matter=null;sharedState.context=null;
 $("shared-list").replaceChildren();$("shared-feedback").textContent="";$("shared-panel").classList.add("hidden");
}
function sharedField(form,label,value="",options=null){
 const wrapper=sharedNode("label",label),input=document.createElement(options?"select":"textarea");
 if(options)for(const [id,text]of options){const option=sharedNode("option",text);option.value=id;input.append(option);}
 input.setAttribute("aria-label",label);input.value=value;input.maxLength=20000;wrapper.append(input);form.append(wrapper);return input;
}
function sharedForm(host,title,setup){
 const section=sharedNode("article"),form=document.createElement("form");section.className="function-card";form.className="organization-form";
 section.append(sharedNode("h3",title),form);host.append(section);setup(form,section);
}
function sharedSubmit(form,label,action){
 const button=sharedNode("button",label);button.type="submit";form.append(button);
 form.addEventListener("submit",event=>{event.preventDefault();action();});return button;
}
function sharedReview(host,label,method,path,body,lines,done=null,send=null){
 const epoch=conn.epoch,generation=sharedState.generation,request=JSON.parse(JSON.stringify(body));
 const card=sharedNode("section");card.className="reminder-preview";card.append(sharedNode("h4","请核对本次操作"));
 for(const line of lines)card.append(sharedNode("p",line));let pending=false;
 const submit=sharedButton("确认"+label,async()=>{
  if(pending||submit.disabled||!sharedLive(epoch,generation))return;pending=true;submit.disabled=true;
  try{
   const result=send?await send(request):await api(method,path,{json:request});
   if(!sharedLive(epoch,generation))return;
   if(done)await done(result);else if(sharedState.matter)await openSharedMatter(sharedState.matter.id);else await openShared();
  }catch(error){
   if(!sharedLive(epoch,generation))return;
   sharedNotice(error.message);
   if(error.status===403||error.status===404)sharedInvalidate(error.message);
   else if(error.status===409)card.append(sharedButton("刷新后重新核对",()=>sharedState.matter?openSharedMatter(sharedState.matter.id):openShared()));
   else{submit.disabled=false;submit.textContent="重试本次提交";}
  }finally{pending=false;}
 });card.append(submit,sharedButton("暂不提交",()=>card.remove()));host.append(card);return card;
}
async function openShared(search="",cursor=null,appendPage=false){
 const epoch=conn.epoch,generation=appendPage?sharedState.generation:++sharedState.generation;
 if(!appendPage){sharedState.matter=null;sharedState.context=null;$("shared-list").replaceChildren();}
 $("shared-panel").classList.remove("hidden");$("shared-panel").scrollIntoView?.({block:"start"});sharedNotice("正在读取可见共享事项…");
 try{
  const query=new URLSearchParams({search,limit:"50"});if(cursor)query.set("cursor",cursor);
  const page=await api("GET","/v1/shared-matters?"+query);if(!sharedLive(epoch,generation))return;
  sharedState.enabled=page.enabled;const host=$("shared-list");
  if(!appendPage){
   host.append(sharedNode("h3","共享事项（"+page.total+"）"));
   sharedForm(host,"查找共享事项",form=>{const text=sharedField(form,"关键词",search);sharedSubmit(form,"搜索",()=>openShared(text.value));});
   host.append(sharedButton("查看我的共享阅读",()=>sharedReadingList()));
   if(page.enabled)sharedCreateForm(host);else sharedNotice("新建和发布暂未开放，已有事项仍按当前权限可读和撤销。");
  }
  for(const item of page.items){const row=sharedNode("article");row.className="function-card";
   row.append(sharedNode("h3",item.title),sharedNode("p",item.scope==="packet"?"仅限已明确共享的资料":"共享事项"),sharedButton("查看资料与进展",()=>openSharedMatter(item.id)));host.append(row);}
  if(page.next_cursor)host.append(sharedButton("查看更多共享事项",()=>openShared(search,page.next_cursor,true)));
  if(page.enabled)sharedNotice("选择具体员工即可协作，部门与岗位可选。个人资料需另行明确发布。");
 }catch(error){if(sharedLive(epoch,generation))sharedNotice(error.message);}
}
function sharedCreateForm(host,draft={},proposalId=null){
 sharedForm(host,"新建共享事项",(form,card)=>{
  const title=sharedField(form,"名称",draft.title||""),content=sharedField(form,"共享正文",draft.content||"");title.maxLength=500;title.required=true;
  sharedSubmit(form,"核对新建",async()=>{
   const epoch=conn.epoch,generation=sharedState.generation,values={title:title.value,content:content.value};
   const requestId=await sharedDraftRequest(proposalId,values);if(!sharedLive(epoch,generation))return;
   sharedReview(card,"新建","POST","/v1/shared-matters",{request_id:requestId,confirmed:true,...values},["名称："+values.title,"正文："+values.content,"只创建共享事项。后续由你明确邀请具体员工。"],result=>openSharedMatter(result.id));
  });
 });
}
async function openSharedMatter(id){
 const epoch=conn.epoch,generation=++sharedState.generation;sharedState.matter=null;sharedState.context=null;
 $("shared-panel").classList.remove("hidden");const host=$("shared-list");host.replaceChildren();sharedNotice("正在核对当前权限…");
 try{
  const matter=await api("GET",sharedPath(id));if(!sharedLive(epoch,generation))return;
  sharedState.matter=matter;host.append(sharedButton("返回共享事项",()=>openShared()),sharedNode("h3",matter.title),sharedNode("p",matter.content||"暂无共享正文"));
  const actions=new Set(matter.allowed_actions);sharedNotice(matter.scope==="packet"?"当前仅可见选定资料。":"资料与操作以当前授权为准。");
  if(matter.owner_id===conn.me?.user_id&&typeof handoffCreate==="function")host.append(sharedButton("将本事项资料交给具体员工",()=>handoffCreate(matter)));
  if(actions.has("edit"))sharedEditForm(host,matter);
  if(actions.has("archive"))for(const [status,label]of [["closed","关闭事项"],["archived","归档事项"]])host.append(sharedButton(label,()=>sharedReview(host,label,"PATCH",sharedPath(id),
   {request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,status},[label+"不代表完成交付、审批或签收。"] )));
  await sharedMaterials(host,matter,epoch,generation);if(!sharedLive(epoch,generation))return;
  if(actions.has("read_members"))await sharedMembers(host,matter,epoch,generation);
  if(!sharedLive(epoch,generation))return;
  if(actions.has("read_history"))await sharedEvents(host,matter,epoch,generation);
 }catch(error){if(sharedLive(epoch,generation)){sharedInvalidate(error.message);}}
}
function sharedEditForm(host,matter){
 sharedForm(host,"修改共享正文",(form,card)=>{const title=sharedField(form,"名称",matter.title),content=sharedField(form,"正文",matter.content);title.maxLength=500;
  sharedSubmit(form,"核对修改",()=>sharedReview(card,"保存正文","PATCH",sharedPath(matter.id),{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,title:title.value,content:content.value},["名称："+title.value,"正文："+content.value]));});
}
async function sharedPages(path,epoch,generation){
 const items=[];let cursor=null;
 do{const page=await api("GET",path+(path.includes("?")?"&":"?")+"limit=100"+(cursor?"&cursor="+encodeURIComponent(cursor):""));
  if(!sharedLive(epoch,generation))return null;items.push(...page.items);cursor=page.next_cursor;
 }while(cursor);return items;
}
async function sharedMembers(host,matter,epoch,generation){
 const rows=await sharedPages(sharedPath(matter.id)+"/members",epoch,generation);if(!rows)return;
 const section=sharedNode("article");section.className="function-card";section.append(sharedNode("h3","参与授权（"+rows.length+"）"));host.append(section);
 for(const row of rows){const card=sharedNode("p",(row.subject_kind==="user"?"员工 ":"部门 ")+row.subject_id+" · "+row.role+" · "+row.status);section.append(card);
  if(matter.allowed_actions.includes("revoke_member")&&row.role!=="owner"&&row.status==="active")card.append(sharedButton("撤销授权",()=>sharedReview(section,"撤销授权","POST",sharedPath(matter.id)+"/members/"+encodeURIComponent(row.id)+"/revoke",
   {request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version},["撤销："+row.subject_id,"旧来源阅读将失效，再授权需重新选择资料。"] )));
 }
 if(!matter.allowed_actions.includes("manage_members"))return;
 const people=await sharedPages("/v1/organization/people",epoch,generation);if(!people)return;
 const choices=people.map(p=>[p.user_id,p.display_name+" · 员工编号 "+p.user_id.slice(-6)]);
 sharedInviteForm(section,matter,choices);
 if(matter.allowed_actions.includes("transfer_owner"))sharedForm(section,"转交负责人",(form,card)=>{const person=sharedField(form,"新负责人",choices[0]?.[0]||"",choices);
  sharedSubmit(form,"核对转交",()=>sharedReview(card,"转交负责人","POST",sharedPath(matter.id)+"/transfer-owner",{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,user_id:person.value},["新负责人："+choices.find(p=>p[0]===person.value)?.[1],"转交后原负责人的授权撤销；不代表业务签收。"]));});
}
async function sharedEvents(host,matter,epoch,generation){
 const page=await api("GET",sharedPath(matter.id)+"/events?limit=50");if(!sharedLive(epoch,generation))return;
 const card=sharedNode("details");card.append(sharedNode("summary","事项历史（"+page.total+"）"));
 for(const row of page.items)card.append(sharedNode("p",row.type||row.action||row.kind||"事项变更"));host.append(card);
}
async function handleSharedRoute(route,text){
 if(!route.shared_draft&&!route.open_shared)return false;
 $("welcome").classList.add("hidden");append("user",text);append("assistant",route.message||"请核对共享事项。");$("message").value="";chat.routeContext=[];
 const epoch=conn.epoch;await openShared();if(epoch===conn.epoch&&sharedState.enabled&&route.shared_draft)sharedCreateForm($("shared-list"),route.shared_draft,route.proposal_id);return true;
}
$("open-shared").addEventListener("click",()=>openShared());$("close-shared").addEventListener("click",resetShared);

async function sharedDraftRequest(proposalId,values){
 if(!proposalId)return crypto.randomUUID();
 const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(JSON.stringify(values)));
 const suffix=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("").slice(0,32);
 return proposalId+":shared:"+suffix;
}
async function restoreSharedDraft(draft,proposalId){
 const epoch=conn.epoch;await openShared();if(epoch===conn.epoch&&sharedState.enabled)sharedCreateForm($("shared-list"),draft,proposalId);
}
