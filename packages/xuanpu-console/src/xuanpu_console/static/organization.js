// 员工默认仍从对话进入；组织辅助入口不依赖模型，所有写入须原生确认。
"use strict";
const organizationState={generation:0,data:null};
function organizationLive(epoch,generation){return epoch===conn.epoch&&generation===organizationState.generation;}
function resetOrganization(){
  organizationState.generation++;organizationState.data=null;
  $("organization-list").replaceChildren();$("organization-feedback").textContent="";
  $("organization-panel").classList.add("hidden");
}
function organizationNotice(message){$("organization-feedback").textContent=message;}
function organizationNode(tag,text){return connectionText(tag,text);}
function organizationButton(text,action){return connectionButton(text,action);}
function organizationReview(host,label,method,path,request,lines){
  const epoch=conn.epoch,generation=organizationState.generation;
  if(!organizationLive(epoch,generation))return;
  const review=organizationNode("section","");review.className="reminder-preview";
  review.append(organizationNode("h4","请核对本次变更"));
  for(const line of lines)review.append(organizationNode("p",line));
  let pending=false;
  const submit=organizationButton("确认"+label,async()=>{
    if(pending||!organizationLive(epoch,generation))return;
    pending=true;submit.disabled=true;
    try{
      await api(method,path,{json:request});
      if(!organizationLive(epoch,generation))return;
      await openOrganization();organizationNotice("已"+label+"。");
    }catch(error){
      if(!organizationLive(epoch,generation))return;
      if(error.status===409){
        organizationNotice("记录已改变，请刷新后重新核对。原确认未自动覆盖新记录。");
        review.append(organizationButton("刷新并重新核对",()=>openOrganization()));
      }else{
        organizationNotice(error.message);submit.disabled=false;submit.textContent="重试本次提交";
      }
    }finally{pending=false;}
  });
  review.append(submit,organizationButton("暂不提交",()=>review.remove()));host.append(review);
}
async function openOrganization(){
  const epoch=conn.epoch,generation=++organizationState.generation;
  $("organization-panel").classList.remove("hidden");$("organization-panel").scrollIntoView?.({block:"start"});
  $("organization-list").replaceChildren();organizationNotice("正在读取本人的部门与职责…");
  try{
    const me=await api("GET","/v1/organization/me");if(!organizationLive(epoch,generation))return;
    const directory=await api("GET","/v1/organization/directory");if(!organizationLive(epoch,generation))return;
    organizationState.data={me,directory};
    organizationRenderMe(me,directory);
    organizationNotice(me.enabled===false?"组织配置暂未开放新变更，已有记录仍可查看和撤销。":"任职与业务职责由企业明确配置。");
    if(isAdmin()||me.roles.some(role=>role.role==="org_manager"))await organizationManage(epoch,generation,me);
  }catch(error){if(organizationLive(epoch,generation))organizationNotice(error.message);}
}
function organizationRenderMe(me,directory){
  const host=$("organization-list"),roles={org_manager:"组织管理",unit_operator:"部门承办",unit_approver:"部门审批"};
  const card=organizationNode("article","");card.className="function-card";
  card.append(organizationNode("h3","我的部门与职责"));
  for(const unit of me.units||[])card.append(organizationNode("p",unit.name));
  for(const role of me.roles||[])card.append(organizationNode("p",roles[role.role]||"企业职责"));
  if(!(me.memberships||[]).length)card.append(organizationNode("p","尚未配置本人的部门任职。"));
  host.append(card);
  organizationRenderDirectory(directory);
}
function organizationRenderDirectory(directory){
  const host=$("organization-list");
  const listing=organizationNode("article","");listing.className="function-card";
  listing.append(organizationNode("h3","可见任职（"+directory.total+"）"));
  for(const member of directory.items||[])listing.append(organizationNode("p",
    (member.display_name||"员工")+" · "+(member.unit_name||"部门")+(member.position_name?" · "+member.position_name:"")));
  host.append(listing);
  if(directory.next_cursor)listing.append(organizationButton("查看更多任职",async()=>{
    const epoch=conn.epoch,generation=organizationState.generation;
    try{
      const next=await api("GET","/v1/organization/directory?cursor="+encodeURIComponent(directory.next_cursor));
      if(!organizationLive(epoch,generation))return;
      organizationRenderDirectory(next);
    }catch(error){if(organizationLive(epoch,generation))organizationNotice(error.message);}
  }));
}
async function organizationManage(epoch,generation,me){
  const data={};
  for(const kind of ["units","positions","memberships","roles"]){
    data[kind]=await api("GET","/v1/admin/organization/"+kind+"?limit=100");
    if(!organizationLive(epoch,generation))return;
  }
  const people=await api("GET","/v1/organization/people?limit=100");
  if(!organizationLive(epoch,generation))return;
  data.people=people.items;data.peoplePage=people;data.me=me;
  organizationPeople($("organization-list"),data);
  organizationManagementForms($("organization-list"),data);
  if(isAdmin()){
    const connections=await api("GET","/v1/admin/connections");
    if(!organizationLive(epoch,generation))return;
    organizationSourceForms($("organization-list"),connections.connections||[]);
  }
}
function organizationImportPreview(host,preview){
  const card=organizationNode("section","");card.className="reminder-preview";
  card.append(organizationNode("h4","请核对目录差异"),organizationNode("p","仅为应用可见范围，未出现的员工不会被停用。"),
    organizationNode("p","已采集 "+preview.pages.length+" 页；明确错误 "+preview.errors.length+" 项；待核对冲突 "+preview.conflicts.length+" 项。"));
  const selected=[];
  for(const change of preview.changes){
    if(preview.applied_changes.includes(change.id))continue;
    const label=organizationNode("label",""),checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=false;
    label.append(checkbox,organizationNode("span",(change.action==="create"?"新增":"更新")+" · "+
      (change.summary||(change.object_type==="unit"?(change.data.name||"部门"):change.object_type==="user"?"员工姓名":"员工任职"))));
    card.append(label);selected.push({change,checkbox});
  }
  card.append(organizationButton("核对所选变更",()=>{
    const changes=selected.filter(v=>v.checkbox.checked).map(v=>({change_id:v.change.id,action:v.change.action}));
    if(!changes.length){organizationNotice("请先明确选择本次要应用的变更。");return;}
    organizationReview(card,"应用所选变更","POST","/v1/admin/organization/import-previews/"+encodeURIComponent(preview.id)+"/apply",
      {request_id:crypto.randomUUID(),expected_version:preview.version,source_revision:preview.source_revision,selected_changes:changes},
      ["本批明确选择 "+changes.length+" 项。","应用前会再次核对来源与本地版本；不会自动选择其他变更。"]);
  }));
  for(const conflict of preview.conflicts)card.append(organizationNode("p",
    conflict.code==="unmapped_user"?"有企微成员尚未明确关联玄圃员工，请先核对身份映射。":"有来源、结构或字段归属冲突，请先核对后重新预览。"));
  host.append(card);
}
if(typeof document!=="undefined"){
  $("open-organization").addEventListener("click",()=>openOrganization());
  $("close-organization").addEventListener("click",()=>resetOrganization());
}
