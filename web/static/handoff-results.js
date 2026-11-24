// 成果只发布到本次交接；组织批准、承办交付和发起签收是三次独立确认。
"use strict";
function handoffUpload(host,record){
 sharedForm(host,"上传本次新成果",(form,card)=>{const input=document.createElement("input");input.type="file";input.required=true;const label=cNode("label","选择成果文件");label.append(input);form.append(label);
  sharedSubmit(form,"核对发布成果",async()=>{
   const epoch=conn.epoch,generation=collabState.generation,file=input.files?.[0];if(!file)return;
   if(file.size>20*1024*1024){collabNotice("成果最大20MiB。");return;}
   try{const matter=await api("GET",sharedPath(record.shared_id));if(!collabLive(epoch,generation))return;
    const body={request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,shared_version:matter.version,name:file.name};
    collabReview(card,"发布本次新成果","",body,["成果："+file.name,"大小："+file.size+" 字节","只创建本次成果，不修改原输入。发布后仍需独立审批、交付和签收。"],result=>handoffPublication(record,result),async request=>{
     const response=await sharedBinary("/v1/handoffs/"+encodeURIComponent(record.id)+"/result-uploads?"+new URLSearchParams(request),{method:"POST",headers:{"Content-Type":file.type||"application/octet-stream"},body:file});return response.json();
    });
   }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
  });
 });
}
async function handoffPublication(record,result){
 const epoch=conn.epoch,generation=collabState.generation;
 try{const publication=await api("GET","/v1/handoffs/"+encodeURIComponent(record.id)+"/result-publications/"+encodeURIComponent(result.id));if(!collabLive(epoch,generation))return;
  if(publication.status==="ready"){await openHandoff(record.id);collabNotice("固定成果已发布，请再明确选择成果提交审批或交付。");}
  else{collabNotice(publication.status==="failed"?"成果发布未完成，请核对来源与当前责任后重新选择。":"发布结果待确认，请补读状态。");$("collaboration-list").append(cButton("补读成果发布状态",()=>handoffPublication(record,result)));}
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
function handoffTextPublication(host,matter,identifier,row){
 const record=collabState.current;
 if(collabState.kind!=="handoff"||record?.shared_id!==matter.id||!record.allowed_actions.includes("publish_result"))return;
 const epoch=conn.epoch,generation=sharedState.generation,collaborationGeneration=collabState.generation;
 host.append(sharedButton("将此草稿发布为本次交接成果",()=>{
  sharedForm(host,"核对本次成果草稿",(form,card)=>{const name=sharedField(form,"成果名称","本次核对成果.txt");name.maxLength=255;name.required=true;
   sharedSubmit(form,"核对固定成果草稿",async()=>{
    try{const current=await api("GET",sharedPath(record.shared_id));
     if(!sharedLive(epoch,generation)||!collabLive(epoch,collaborationGeneration))return;
     sharedReview(card,"发布本次成果草稿","POST","/v1/handoffs/"+encodeURIComponent(record.id)+"/result-texts",
      {request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,shared_version:current.version,name:name.value,source_context_id:identifier,draft_message_id:row.id},
      ["交接："+record.title,"草稿原文："+row.text,"发布固定原文并保留来源；仍需独立审批、交付和签收。"],result=>handoffPublication(record,result));
    }catch(error){if(sharedLive(epoch,generation))sharedNotice(error.message);}
   });
  });
 }));
}
async function handoffResults(host,record,actions,epoch,generation){
 const materials=await collabPages(sharedPath(record.shared_id)+"/materials",epoch,generation),requests=await collabPages("/v1/approval-requests",epoch,generation);if(!collabLive(epoch,generation))return;
 sharedForm(host,"明确选择固定成果",(form,card)=>{
  const selected=collabMaterialChecks(form,materials||[]),approved=(requests||[]).filter(r=>r.handoff_id===record.id&&r.status==="approved"&&r.target_version===record.version);
  const chosen=record.policy_id?sharedField(form,"本版本已通过的审批",approved[0]?.id||"",approved.length?approved.map((r,i)=>[r.id,"已批准记录 "+(i+1)]):[["","请先提交成果审批并等候决定"]]):null;
  const refs=()=>selected.filter(v=>v.check.checked).map(v=>({material_id:v.material.id,revision:v.revision.revision,sha256:v.revision.sha256}));
  const valid=()=>{const rows=refs();if(!rows.length||rows.length>8||new Set(rows.map(v=>v.material_id)).size!==rows.length){collabNotice("请选1至8项成果，每项材料只选一个固定版本。");return false;}return true;};
  if(actions.has("submit_approval"))sharedSubmit(form,"核对申请组织审批",()=>{if(!valid())return;const body={request_id:crypto.randomUUID(),confirmed:true,policy_id:record.policy_id,target_type:"handoff",target_id:record.id,target_version:record.version,result_refs:refs()};
   collabReview(card,"申请组织审批","/v1/approval-requests",body,["工作："+record.title,...selected.filter(v=>v.check.checked).map(v=>v.material.name+" · 版本 "+v.revision.revision),"审批人由已配置规则决定；组织批准后仍需承办交付、发起人签收。"],result=>openApproval(result.id));
  });
  if(actions.has("deliver")){const button=cButton("核对交付固定成果",()=>{if(!valid())return;if(record.policy_id&&!chosen?.value){collabNotice("请先完成本版本的组织审批。");return;}
    collabReview(card,"交付已选固定成果","/v1/handoffs/"+encodeURIComponent(record.id)+"/deliver",{request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,result_refs:refs(),...(chosen?.value?{approval_id:chosen.value}:{})},
     ["工作："+record.title,...selected.filter(v=>v.check.checked).map(v=>v.material.name+" · 固定版本 "+v.revision.revision),"交付后仍需发起人独立签收。"],()=>openHandoff(record.id));
   });form.append(button);}
 });
}
