// 共享材料仅选择确切版本，下载校验SHA，发布不自动追随私人正本更新。
"use strict";
async function sharedMaterials(host,matter,epoch,generation){
 const materials=await sharedPages(sharedPath(matter.id)+"/materials",epoch,generation);if(!materials)return;
 sharedState.materials=materials;const section=sharedNode("article");section.className="function-card";
 section.append(sharedNode("h3","可见材料（"+materials.length+"）"));host.append(section);const selected=[];
 for(const material of materials){const row=sharedNode("section");row.append(sharedNode("h4",material.name));section.append(row);
  for(const revision of material.revisions){const label=sharedNode("label"),check=document.createElement("input");check.type="checkbox";check.checked=false;
   label.append(check,sharedNode("span","版本 "+revision.revision+" · "+revision.size+" 字节 · SHA256 "+revision.sha256));row.append(label);
   selected.push({check,revision,material});row.append(sharedButton("下载版本 "+revision.revision,()=>sharedDownload(matter,material,revision,epoch,generation)));
  }
  if(matter.allowed_actions.includes("publish_material"))row.append(sharedButton("撤回共享材料",()=>sharedReview(section,"撤回材料","POST",sharedPath(matter.id)+"/materials/"+encodeURIComponent(material.id)+"/withdraw",{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version},["材料："+material.name,"撤回所有共享版本，私人原件保留。已下载的文件无法追回。"] )));
 }
 if(matter.allowed_actions.includes("start_context")){
  const label=sharedNode("label"),include=document.createElement("input");include.type="checkbox";include.checked=true;label.append(include,sharedNode("span","包括当前可见的共享正文"));section.append(label);
  section.append(sharedButton("核对并开始阅读",()=>{const revisions=selected.filter(v=>v.check.checked);
   if(revisions.length>8){sharedNotice("每次最多明确选择8个材料版本。");return;}
   sharedReview(section,"开始阅读","POST","/v1/shared-contexts",{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,shared_id:matter.id,kind:"assistant_conversation",include_content:include.checked,material_revision_ids:revisions.map(v=>v.revision.id)},
    ["事项："+matter.title,"正文："+(include.checked?"包括当前可见正文":"不包括正文"),...revisions.map(v=>v.material.name+" · 版本 "+v.revision.revision+" · "+v.revision.sha256),"旧授权失效后，本次阅读也会停止；重新授权需要重新选择。"],result=>openSharedConversation(result.id,matter.id));
  }));
 }
 if(matter.allowed_actions.includes("publish_material")){sharedUploadForm(section,matter);await sharedPrivatePublish(section,matter,epoch,generation);}
}
async function sharedBinary(path,options={}){
 const response=await fetch(url(path),{...requestOptions(),...options,headers:{...headers(),...(options.headers||{})}});
 if(!response.ok){const body=await response.json();const error=new Error(body.error?.message||"材料操作失败");error.status=response.status;throw error;}
 return response;
}
async function sharedDownload(matter,material,revision,epoch,generation){
 try{
  const response=await sharedBinary(sharedPath(matter.id)+"/materials/"+encodeURIComponent(material.id)+"/content?revision="+revision.revision);
  const data=await response.arrayBuffer();if(!sharedLive(epoch,generation))return;
  const sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",data)),x=>x.toString(16).padStart(2,"0")).join("");
  if(sha!==revision.sha256||data.byteLength!==revision.size)throw new Error("材料版本校验不一致，请重新读取。");
  if(!sharedLive(epoch,generation))return;
  const blob=URL.createObjectURL(new Blob([data],{type:"application/octet-stream"})),link=sharedNode("a");link.href=blob;link.download=material.name;link.click();URL.revokeObjectURL(blob);
 }catch(error){if(sharedLive(epoch,generation)){sharedNotice(error.message);if(error.status===403||error.status===404){sharedInvalidate(error.message);}}}
}
function sharedUploadForm(host,matter){
 sharedForm(host,"上传新的共享副本",(form,card)=>{const field=sharedNode("label","选择文件"),input=document.createElement("input");input.type="file";input.required=true;field.append(input);form.append(field);
  sharedSubmit(form,"核对上传",()=>{const file=input.files?.[0];if(!file)return;if(file.size>20*1024*1024){sharedNotice("共享材料最大20MiB。");return;}
   const body={request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,name:file.name};
   sharedReview(card,"发布共享副本","POST","",body,["文件："+file.name,"大小："+file.size+" 字节","上传后事项当前参与者可按各自权限读取。"],result=>sharedPublication(matter,result),async request=>{
    const response=await sharedBinary(sharedPath(matter.id)+"/material-uploads?"+new URLSearchParams(request),{method:"POST",headers:{"Content-Type":file.type||"application/octet-stream"},body:file});return response.json();
   });
  });
 });
}
async function sharedPrivatePublish(host,matter,epoch,generation){
 const {items:files}=await api("GET","/v1/workspace/items?kind=file");if(!sharedLive(epoch,generation))return;
 const options=files.filter(f=>!f.trashed_at&&!f.data.source_url&&f.data.current_revision).map(f=>[f.id,f.data.name]);
 if(!options.length)return;
 sharedForm(host,"发布本人文件的固定版本",(form,card)=>{const chosen=sharedField(form,"本人文件",options[0][0],options);
  sharedSubmit(form,"选择固定版本",async()=>{
   try{const itemId=chosen.value,versions=await api("GET","/v1/workspace/files/"+encodeURIComponent(itemId)+"/versions");if(!sharedLive(epoch,generation))return;
    const rows=versions.items||versions,version=sharedField(card,"明确版本",String(rows[0].revision),rows.map(v=>[String(v.revision),"版本 "+v.revision+" · "+v.sha256]));
    card.append(sharedButton("核对发布该版本",()=>sharedReview(card,"发布固定版本","POST",sharedPath(matter.id)+"/materials",{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,source_item_id:itemId,source_revision:Number(version.value)},["文件："+options.find(v=>v[0]===itemId)?.[1],"固定版本："+version.value,"只发布此版本的共享副本，不会自动跟随个人文件更新。"],result=>sharedPublication(matter,result))));
   }catch(error){if(sharedLive(epoch,generation))sharedNotice(error.message);}
  });
 });
}
async function sharedPublication(matter,result){
 const epoch=conn.epoch,generation=sharedState.generation;
 const publication=await api("GET",sharedPath(matter.id)+"/publications/"+encodeURIComponent(result.id));if(!sharedLive(epoch,generation))return;
 if(publication.status==="ready"){await openSharedMatter(matter.id);sharedNotice("共享副本已发布。");return;}
 sharedNotice(publication.status==="failed"?"副本发布未完成，请核对状态后重新选择。":"发布结果待确认，请稍后补读状态。");
 $("shared-list").append(sharedButton("补读发布状态",()=>sharedPublication(matter,result)));
}
function sharedInviteForm(host,matter,choices){
 sharedForm(host,"邀请具体员工",(form,card)=>{
  const person=sharedField(form,"员工",choices[0]?.[0]||"",choices),scope=sharedField(form,"资料范围","packet",[["packet","只读选定资料包"],["full","整事项正文及全部共享材料"]]);
  const role=sharedField(form,"整事项权限","viewer",[["viewer","只读"],["editor","可编辑整事项"]]),title=sharedField(form,"限定资料包名称","待核对资料");title.maxLength=500;
  const label=sharedNode("label"),include=document.createElement("input");include.type="checkbox";include.checked=false;label.append(include,sharedNode("span","资料包包括当前正文的固定副本"));form.append(label);
  const selected=[];for(const material of sharedState.materials||[])for(const revision of material.revisions){
   const line=sharedNode("label"),check=document.createElement("input");check.type="checkbox";check.checked=false;line.append(check,sharedNode("span",material.name+" · 固定版本 "+revision.revision));form.append(line);selected.push({check,revision,material});
  }
  sharedSubmit(form,"核对邀请",()=>{
   const packet=scope.value==="packet",revisions=selected.filter(v=>v.check.checked),body={request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,subject_kind:"user",subject_id:person.value,role:packet?"viewer":role.value,scope:scope.value};
   if(packet)Object.assign(body,{packet_title:title.value,include_content:include.checked,material_revisions:revisions.map(v=>v.revision.id)});
   sharedReview(card,"邀请员工","POST",sharedPath(matter.id)+"/members",body,["员工："+choices.find(p=>p[0]===person.value)?.[1],
    packet?"范围：选定资料包，仅包含下面明确核对的正文副本和材料版本。":"范围：整事项正文和全部共享材料。",
    packet?"资料包名称："+title.value:"权限："+(role.value==="editor"?"可编辑":"只读"),
    ...(packet?["正文副本："+(include.checked?"包含":"不包含"),...revisions.map(v=>v.material.name+" · 版本 "+v.revision.revision+" · "+v.revision.sha256)]:[])]);
  });
 });
}
