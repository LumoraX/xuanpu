// 组织规则由管理员预配；员工申请不接收客户端另传的审批人。
"use strict";
async function openApprovals(cursor=null,append=false){
 if(!append)collabClear();collabOpen();const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");
 try{const page=await api("GET","/v1/approval-requests?limit=50"+(cursor?"&cursor="+encodeURIComponent(cursor):""));if(!collabLive(epoch,generation))return;
  if(!Number.isInteger(page.total))throw new Error("审批数量格式无效，请刷新。");
  if(!append)host.append(cButton("返回工作交接",()=>openCollaboration()),cNode("h3","我的组织审批（"+page.total+"）"));
  for(const item of page.items){const card=cNode("article");card.className="function-card";card.append(cNode("h4",collabLabels[item.status]||"请核对状态"),cNode("p","顺序步骤 "+(item.current_step+1)+" / "+item.steps.length),cButton("查看本次审批",()=>openApproval(item.id)));host.append(card);}
  if(page.next_cursor)host.append(cButton("查看更多审批",()=>openApprovals(page.next_cursor,true)));collabNotice("组织决定只适用于本次固定成果，不代表签收或外部写入授权。");
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
async function openApproval(id){
 collabClear();collabOpen();const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");
 try{const record=await api("GET","/v1/approval-requests/"+encodeURIComponent(id));if(!collabLive(epoch,generation))return;
  collabState.current=record;collabState.kind="approval";
  host.append(cButton("返回我的审批",()=>openApprovals()),cNode("h3","组织审批 · "+(collabLabels[record.status]||"请核对状态")),cNode("p","顺序步骤 "+(record.current_step+1)+" / "+record.steps.length));
  collabNotice(record.blocked_reason?"责任、目标或成果权限已失效，须明确重提。":"请先下载核对本次固定成果，再作本人的组织决定。");
  // handoff详情不授予额外权限，审批人只通过其临时成果scope读取。
  for(const ref of record.can_read_results?(record.result_refs||[]):[])host.append(cNode("p","固定成果版本 "+ref.revision),cButton("下载并核验固定成果",async()=>{
   try{const handoffRef={shared_id:null};const notePage=await api("GET","/v1/approval-requests/"+encodeURIComponent(record.id));if(!collabLive(epoch,generation))return;
    if(!notePage.shared_id)throw new Error("审批来源信息不可用，请刷新。");handoffRef.shared_id=notePage.shared_id;await collabDownload(handoffRef,ref,"待审批成果-"+ref.revision);
   }catch(error){if(collabLive(epoch,generation))collabNotice(error.message);}
  }));
  for(const action of record.allowed_actions){const label={approve:"批准本次固定成果",reject:"驳回",return:"退回补充",withdraw:"撤销本人申请"}[action];if(!label)continue;
   sharedForm(host,label,(form,card)=>{const reason=sharedField(form,"决定说明（可选）");reason.maxLength=4000;
    sharedSubmit(form,"核对"+label,()=>collabReview(card,label,"/v1/approval-requests/"+encodeURIComponent(record.id)+"/"+action,{request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,reason:reason.value},[label,"只针对当前固定成果与当前步骤。",reason.value,"不代替承办交付或发起人签收。"],()=>openApproval(record.id)));
   });
  }
  for(const d of record.decisions)host.append(cNode("p",({approve:"已批准",reject:"已驳回",return:"已退回",withdraw:"已撤销"}[d.decision]||"已记录决定")+" · 第"+(d.step_index+1)+"步"+(d.reason?" · "+d.reason:"")));
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
async function approvalPolicies(){
 collabClear();collabOpen();const epoch=conn.epoch,generation=collabState.generation,host=$("collaboration-list");
 try{const people=await collabPages("/v1/organization/people",epoch,generation),policies=await collabPages("/v1/admin/approval-policies",epoch,generation);if(!collabLive(epoch,generation))return;
  host.append(cButton("返回协作",()=>openCollaboration()),cNode("h3","审批规则（"+(policies||[]).length+"）"));
  const choices=(people||[]).map(p=>[p.user_id,p.display_name+"（"+p.user_id+"）"]);if(!choices.length)throw new Error("请先准备启用员工。");
  approvalPolicyForm(host,null,choices);for(const p of policies||[])approvalPolicyForm(host,p,choices);
 }catch(error){if(collabLive(epoch,generation))collabInvalidate(error.message);}
}
function approvalPolicyForm(host,policy,choices){
 sharedForm(host,policy?policy.name+" · 修订 "+policy.version:"配置新的到人审批规则",(form,card)=>{
  const name=sharedField(form,"规则名称",policy?.name||"");name.required=true;name.maxLength=500;
  const current=policy?.revisions.at(-1),steps=[];
  for(let i=0;i<3;i++)steps.push(sharedField(form,"第"+(i+1)+"步审批人"+(i?"（可选）":""),current?.steps[i]?.user_id||(i?"":choices[0][0]),i?[["","不设此步"],...choices]:choices));
  const status=policy?sharedField(form,"新申请是否可用",policy.status,[["active","启用"],["disabled","停用新申请"]]):null;
  sharedSubmit(form,"核对规则",()=>{if((!steps[1].value&&steps[2].value)){collabNotice("审批步骤需按顺序填写。");return;}
   const body={request_id:crypto.randomUUID(),confirmed:true,name:name.value,steps:steps.filter(s=>s.value).map(s=>({selector:"user",user_id:s.value})),...(policy?{expected_version:policy.version,status:status.value}:{})};
   collabReview(card,policy?"修改规则":"配置规则","/v1/admin/approval-policies"+(policy?"/"+encodeURIComponent(policy.id):""),body,["规则："+name.value,...steps.filter(s=>s.value).map((s,i)=>"第"+(i+1)+"步："+choices.find(p=>p[0]===s.value)?.[1]),"不得由申请人审批本人请求。已有合法决定及策略旧修订会保留。"],()=>approvalPolicies(),policy?request=>api("PATCH","/v1/admin/approval-policies/"+encodeURIComponent(policy.id),{json:request}):null);
  });
 });
}
