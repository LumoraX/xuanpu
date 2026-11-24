// 能力运营（R4-B/C/F，仅管理员）：按能力看运行结果与用量，查看发布与分配并经确认停止或撤销。
// 统计只有计数与用量，不含任务正文或具体员工；未知不当 0，没有价格表时费用显示未配置。
// 员工在「我的能力」里看本人结果并导出；导出与页面同一权限与口径（CSV）。
"use strict";
function capabilityRate(value){return value===null||value===undefined?"暂无":Math.round(value*1000)/10+"%";}
function capabilitySummaryLines(summary){
  const t=summary.totals||{},tokens=summary.tokens||{};
  return ["任务 "+t.tasks+"：成功 "+t.succeeded+" · 失败 "+t.failed+" · 结果未知 "+t.outcome_unknown+" · 进行中 "+((t.accepted||0)+(t.running||0)+(t.waiting_user||0)),
    "技术完成率 "+capabilityRate(summary.technical_completion?.rate)+"（成功 / 全部受理，业务是否正确另行核实）",
    "token：已结算 "+tokens.settled+" · 仍占用上界 "+tokens.held_upper_bound+" · 无用量记录的任务 "+tokens.tasks_without_records,
    summary.cost?.status==="price_unavailable"?"费用：未配置价格表，不估算":"费用："+summary.cost?.amount+" "+summary.cost?.currency];
}
async function capabilityOperations(epoch,generation){
  const body=await api("GET","/v1/admin/capability-outcomes");if(!capabilityLive(epoch,generation))return;
  if(!body.totals||!Array.isArray(body.capabilities))throw new Error("运行结果响应不完整，请重新读取。");
  const host=$("capability-list"),section=connectionText("section","");section.className="reminder-preview";
  section.append(connectionText("h3","运行结果与发布"));
  for(const line of capabilitySummaryLines(body))section.append(connectionText("p",line));
  section.append(connectionButton("导出汇总（CSV）",()=>capabilityExport("/v1/admin/capability-outcomes/export","能力运行汇总.csv",epoch,generation)));
  for(const item of body.capabilities){
    const card=connectionText("article","");card.className="function-card";
    card.append(connectionText("h4","能力 "+item.capability_id+" · 说明修订 "+item.capability_revision));
    for(const line of capabilitySummaryLines(item))card.append(connectionText("p",line));
    card.append(connectionButton("查看发布与分配",()=>capabilityGovernance(card,item.capability_id,epoch,generation)));
    section.append(card);
  }
  if(!body.capabilities.length)section.append(connectionText("p","还没有经能力受理的任务。"));
  host.append(section);
}
async function capabilityGovernance(card,capabilityId,epoch,generation){
  try{
    const id=encodeURIComponent(capabilityId);
    const [releases,assignments]=await Promise.all([api("GET","/v1/capabilities/"+id+"/releases"),api("GET","/v1/capabilities/"+id+"/assignments")]);
    if(!capabilityLive(epoch,generation))return;
    if(!Array.isArray(releases.releases)||!Array.isArray(assignments.assignments))throw new Error("发布或分配响应不完整，请重新读取。");
    const labels={active:"生效中",retired:"已停止新受理",revoked:"已撤销"};
    for(const release of releases.releases){
      const line=connectionText("p","发布 "+release.id+" · 修订 "+release.capability_revision+" · "+(labels[release.status]||release.status)+" · 员工 "+release.targets.length+" 人");
      card.append(line);
      if(release.status==="active"){
        for(const [action,label,note] of [["retire","停止发布","已受理的工作照常完成，之后不再受理新工作。"],["revoke","撤销发布","立即拒绝新调用，已受理的工作也不能继续；旧任务的包、配置与输入不改。"]])
          card.append(connectionButton(label,()=>capabilityReview(card,label,"/v1/capability-releases/"+encodeURIComponent(release.id)+"/"+action,
            {request_id:crypto.randomUUID(),expected_version:release.version},["发布 "+release.id,note])));
      }
    }
    for(const assignment of assignments.assignments){
      const who=assignment.selector.kind==="user"?"员工 "+assignment.selector.id:assignment.selector.kind+" "+assignment.selector.id;
      card.append(connectionText("p","分配 "+who+" · 修订 "+assignment.allowed_revisions.join("、")+" · "+(assignment.status==="active"?"生效中":"已撤销")));
      if(assignment.status==="active")card.append(connectionButton("撤销分配",()=>capabilityReview(card,"撤销分配",
        "/v1/capability-assignments/"+encodeURIComponent(assignment.id)+"/revoke",
        {request_id:crypto.randomUUID(),expected_version:assignment.version},[who,"只撤销能力使用授权，不删除任何工作或资料。"])));
    }
    if(!releases.releases.length&&!assignments.assignments.length)card.append(connectionText("p","尚无发布或分配。"));
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
async function capabilityMine(epoch,generation){
  const body=await api("GET","/v1/me/capability-outcomes?limit=20");if(!capabilityLive(epoch,generation))return;
  if(!body.totals||!Array.isArray(body.items))throw new Error("本人运行结果响应不完整，请重新读取。");
  if(!body.totals.tasks)return;
  const section=connectionText("section","");section.className="reminder-preview";
  section.append(connectionText("h3","我的能力工作"));
  for(const line of capabilitySummaryLines(body))section.append(connectionText("p",line));
  const labels={accepted:"已受理",running:"进行中",waiting_user:"等你处理",succeeded:"已完成",failed:"失败",cancelled:"已取消",outcome_unknown:"结果未知",interrupted:"已中断",unreadable:"暂时读不到",missing:"记录缺失"};
  for(const item of body.items)section.append(connectionText("p",(labels[item.status]||item.status)+" · 能力 "+item.capability_id+" 修订 "+item.capability_revision+(item.tokens_settled===null?" · 无用量记录":" · token "+item.tokens_settled)));
  section.append(connectionButton("导出本人记录（CSV）",()=>capabilityExport("/v1/me/capability-outcomes/export","我的能力工作.csv",epoch,generation)));
  $("capability-list").append(section);
}
async function capabilityExport(path,name,epoch,generation){
  try{
    const response=await fetch(url(path),{...requestOptions(),headers:headers()});
    if(!response.ok)throw new Error((await response.json()).error?.message||"导出失败");
    const blob=await response.blob();if(!capabilityLive(epoch,generation))return;
    const href=URL.createObjectURL(blob),link=document.createElement("a");link.href=href;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
