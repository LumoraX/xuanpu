// 模块表单与对话确认共用字段和提交规则；永远不把模型建议当成已执行。
"use strict";
function wsField(form,name,label,value="",type="text",{required=false,maxLength=500,options=null}={}) {
  const wrapper=wsText("label",label),input=document.createElement(options?"select":type==="textarea"?"textarea":"input");
  input.name=name;if(!options && type!=="textarea")input.type=type;
  if(options)for(const [key,text] of options){const option=wsText("option",text);option.value=key;input.append(option);}
  input.value=value ?? "";input.required=required;input.maxLength=maxLength;input.setAttribute("aria-label",label);
  if(type==="checkbox")input.checked=Boolean(value);
  wrapper.append(input);form.append(wrapper);return input;
}
function localDateTime(stamp) {
  if(!stamp)return "";
  const date=new Date(stamp*1000);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
function formTimestamp(value) { return value?new Date(value).getTime()/1000:null; }
async function recordForm(kind,item,onSave,onCancel) {
  const data=item?.data || {},form=document.createElement("form");form.className="workspace-form";
  const refs=await api("GET",workspaceBase+"/items");
  const folders=refs.items.filter(x=>x.kind==="folder" && x.id!==item?.id),matters=refs.items.filter(x=>x.kind==="matter");
  const relational=(name,label,records,current)=>wsField(form,name,label,current || "","text",{options:[["","不关联"],...records.map(x=>[x.id,x.data.name || x.data.title])]});
  if(kind==="folder" || kind==="file") {
    wsField(form,"name","名称",data.name || "","text",{required:true,maxLength:255});
    relational("parent_id","所在文件夹",folders,item?.id ? data.parent_id : data.parent_id ?? workspace.parent);
    if(kind==="file") {
      relational("matter_id","关联事项",matters,data.matter_id);
      wsField(form,"tags","标签（用逗号分隔）",(data.tags || []).join("，"));
      if(!item?.id)wsField(form,"source_url","在线文档链接",data.source_url || "","url",{required:true,maxLength:2048});
    }
  } else {
    wsField(form,"title","标题",data.title || "","text",{required:true,maxLength:200});
    wsField(form,"description","说明",data.description || "","textarea",{maxLength:4000});
    if(kind==="matter")wsField(form,"status","进展",data.status || "active","text",{options:[["active","进行中"],["completed","已完成"],["archived","已归档"]]});
    if(kind==="todo") {
      wsField(form,"status","状态",data.status || "open","text",{options:[["open","待完成"],["done","已完成"]]});
      wsField(form,"due_at","截止时间（当前设备时区，可留空）",localDateTime(data.due_at),"datetime-local");
    }
    if(kind==="event") {
      wsField(form,"start_at","开始（当前设备时区）",localDateTime(data.start_at),"datetime-local",{required:true});
      wsField(form,"end_at","结束（当前设备时区）",localDateTime(data.end_at),"datetime-local",{required:true});
    }
    if(kind==="todo" || kind==="event")relational("matter_id","关联事项",matters,data.matter_id || workspace.matter);
    if(kind==="skill") {
      wsField(form,"instructions","工作指令",data.instructions || "","textarea",{required:true,maxLength:8000});
      wsField(form,"enabled","在我的对话中启用",data.enabled!==false,"checkbox");
      form.append(wsText("p","指令只影响你的助手，不会增加企业权限，也不会自动批准业务操作。","status"));
    }
  }
  const status=wsText("p","","status");status.setAttribute("role","status");
  const actions=wsText("div","","row"),submit=wsButton("确认保存",()=>{},"");submit.type="submit";
  actions.append(submit,wsButton("取消",onCancel));form.append(status,actions);
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(submit.disabled)return;
    const values=new FormData(form),payload={};
    for(const [key,value] of values)payload[key]=String(value).trim();
    for(const name of ["parent_id","matter_id"])if(name in payload)payload[name]=payload[name] || null;
    for(const name of ["due_at","start_at","end_at"])if(name in payload)payload[name]=formTimestamp(payload[name]);
    if(kind==="event") {
      if(payload.end_at<=payload.start_at){status.textContent="结束时间必须晚于开始时间。";return;}
      payload.timezone=browserTimezone();
    }
    if(kind==="file")payload.tags=payload.tags.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
    if(kind==="skill")payload.enabled=form.elements.enabled.checked;
    submit.disabled=true;status.textContent="正在保存…";
    try{await onSave(payload);}
    catch(error){if(error.name!=="AbortError")status.textContent=error.status===409?"记录已更新。请返回列表刷新，再重新编辑。":error.message;}
    finally{submit.disabled=false;}
  });return form;
}
async function editWorkspaceRecord(kind,item=null) {
  const generation=++workspace.generation,epoch=conn.epoch,requestId=crypto.randomUUID();
  $("workspace-toolbar").replaceChildren(wsButton("返回列表",refreshWorkspace));
  $("workspace-feedback").textContent="";$("workspace-content").textContent="正在准备表单…";
  const form=await recordForm(kind,item, data=>workspaceMutation(()=>item?.id?
    api("PUT",`${workspaceBase}/items/${item.id}`,{json:{data,version:item.version}}):
    api("POST",workspaceBase+"/items",{json:{kind,data,request_id:requestId}})),refreshWorkspace);
  if(workspaceCurrent(generation,epoch))$("workspace-content").replaceChildren(form);
}
function renderWorkspaceSettings(settings) {
  const form=document.createElement("form");form.className="workspace-form";
  wsField(form,"assistant_name","助手名字",settings.assistant_name,"text",{required:true,maxLength:60});
  wsField(form,"user_title","怎么称呼你",settings.user_title,"text",{maxLength:60});
  wsField(form,"style","回复风格",settings.style,"textarea",{maxLength:1000});
  wsField(form,"timezone","时间显示时区",settings.timezone,"text",{required:true,maxLength:64});
  form.append(wsText("p","例如 Asia/Shanghai。日程表单使用当前设备时间；列表按此时区显示。服务、模型和渠道由企业管理员准备。","status"));
  const status=wsText("p","","status"),submit=wsButton("保存设置",()=>{},"");submit.type="submit";form.append(status,submit);
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(submit.disabled)return;
    const data=Object.fromEntries(new FormData(form));submit.disabled=true;
    try{
      try{new Intl.DateTimeFormat("zh-CN",{timeZone:data.timezone});}
      catch(error){if(!(error instanceof RangeError))throw error;throw new Error("当前浏览器不支持这个时区，请选择有效时区，例如 Asia/Shanghai。");}
      await workspaceMutation(()=>api("PUT",workspaceBase+"/settings",{json:{...data,version:settings.version}}));
    }
    catch(error){status.textContent=error.message;}finally{submit.disabled=false;}
  });$("workspace-content").replaceChildren(form);
}
async function showWorkspaceDraft(draft,proposalId) {
  const epoch=conn.epoch,generation=chat.generation,loading=workspaceChat.loading,box=$("workspace-draft"),requestId=crypto.randomUUID();
  box.classList.remove("hidden");box.replaceChildren(wsText("strong","确认保存到"+workspaceNames[draft.kind]));
  const form=await recordForm(draft.kind,{data:draft.data},async data=>{
    const response=proposalId?await api("POST",`/v1/assistant/proposals/${encodeURIComponent(proposalId)}/confirm`,{json:{data}}):null;
    const result=response?.result || await api("POST",workspaceBase+"/items",{json:{kind:draft.kind,data,request_id:requestId}});
    if(epoch!==conn.epoch)return;
    if(generation===chat.generation) {box.replaceChildren();box.classList.add("hidden");append("assistant","已保存："+(result.data.title || result.data.name));}
    await refreshWorkspaceSummary();
    if(workspace.kind===draft.kind)await refreshWorkspace();
  },async()=>{if(proposalId)await api("POST",`/v1/assistant/proposals/${encodeURIComponent(proposalId)}/cancel`);box.replaceChildren();box.classList.add("hidden");});
  if(epoch===conn.epoch && generation===chat.generation && loading===workspaceChat.loading)box.append(form);
}
