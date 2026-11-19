// 能力分配、隔离评测与按员工发布（R4-B3/C2，仅管理员）：每一步都先核对确认卡再提交，
// 员工名单按「员工 + 代次」固定；发布先取只读预览，确认时带预览摘要与要停止的重叠发布。
"use strict";
const releaseState={evaluations:new Map()};
async function capabilityEmployees(epoch,generation){
  const body=await api("GET","/v1/users?status=active&limit=100");if(!capabilityLive(epoch,generation))return null;
  if(!Array.isArray(body.users))throw new Error("员工列表响应不完整，请重新读取。");
  return body.users.filter(u=>Number.isInteger(u.lifecycle_epoch));
}
function capabilityPicker(host,users,multiple){
  const box=connectionText("fieldset","");box.append(connectionText("legend",multiple?"选择员工（可多选）":"选择员工"));
  const inputs=[];
  for(const user of users){
    const label=connectionText("label",""),input=connectionText("input","");input.type=multiple?"checkbox":"radio";input.name="capability-employee";
    input.value=user.user_id;label.append(input,connectionText("span"," "+(user.display_name||user.user_id)));box.append(label);inputs.push({input,user});
  }
  host.append(box);
  return ()=>inputs.filter(x=>x.input.checked).map(x=>x.user);
}
async function capabilityAssign(card,item,epoch,generation){
  try{
    const users=await capabilityEmployees(epoch,generation);if(!users)return;
    const form=connectionText("section","");form.className="reminder-preview";form.append(connectionText("h4","分配给员工 · 说明修订 "+item.revision));
    const chosen=capabilityPicker(form,users,false);card.append(form);
    form.append(connectionButton("核对分配",()=>{
      const [user]=chosen();if(!user){capabilityNotice("请选择一名员工。");return;}
      capabilityReview(form,"分配","/v1/capabilities/"+encodeURIComponent(item.capability_id)+"/assignments",
        {request_id:crypto.randomUUID(),selector:{kind:"user",id:user.user_id,user_epoch:user.lifecycle_epoch},allowed_revisions:[item.revision]},
        ["员工："+(user.display_name||user.user_id),"可用修订："+item.revision,"只授权使用这项能力，不授予资料读取或审批权；发布后才能实际运行。"]);
    }));
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
function capabilityEvaluate(card,item,installation,epoch,generation){
  const form=connectionText("section","");form.className="reminder-preview";
  form.append(connectionText("h4","隔离评测"),connectionText("p","选择交付方提供的评测套件文件（suite.json）。套件摘要须与登记的交付说明一致；评测在隔离环境运行，不用生产凭据。"));
  const label=connectionText("label","评测套件 "),file=connectionText("input","");file.type="file";file.accept=".json,application/json";label.append(file);form.append(label);card.append(form);
  file.addEventListener("change",async()=>{
    try{
      const selected=file.files?.[0];if(!selected)return;
      if(selected.size>262144)throw new Error("套件文件不能超过256KiB。");
      const suite=JSON.parse(await selected.text());if(!capabilityLive(epoch,generation))return;
      const request={request_id:crypto.randomUUID(),capability_id:item.capability_id,installation_id:installation.id,suite};
      let pending=false;
      const submit=connectionButton("确认发起评测",async()=>{
        if(pending||!capabilityLive(epoch,generation))return;pending=true;submit.disabled=true;
        try{
          const result=await api("POST","/v1/capability-evaluations",{json:request});if(!capabilityLive(epoch,generation))return;
          releaseState.evaluations.set(item.capability_id,result.id);
          form.append(connectionText("p","评测已开始："+result.id+"（"+result.status+"）。结束后可据此发布。"),
            connectionButton("查看评测结果",()=>capabilityEvaluationStatus(form,result.id,epoch,generation)));
        }catch(error){if(capabilityLive(epoch,generation)){capabilityNotice(error.message);submit.disabled=false;submit.textContent="重试本次提交";}}
        finally{pending=false;}
      });
      form.append(connectionText("p","用例 "+(Array.isArray(suite.cases)?suite.cases.length:0)+" 个 · 总 token 上限 100000"),submit);
    }catch(error){if(capabilityLive(epoch,generation))capabilityNotice("无法读取评测套件，请检查文件格式。");}
  });
}
async function capabilityEvaluationStatus(host,id,epoch,generation){
  try{
    const result=await api("GET","/v1/capability-evaluations/"+encodeURIComponent(id));if(!capabilityLive(epoch,generation))return;
    const labels={running:"进行中",passed:"通过",failed:"未通过",interrupted:"已中断",blocked:"被阻止"};
    host.append(connectionText("p","评测 "+id+"："+(labels[result.status]||result.status)+(result.reason?"（"+result.reason+"）":"")));
    for(const c of result.cases||[])host.append(connectionText("p",(c.passed?"通过":"未通过")+" · "+c.name+(c.reasons?.length?"："+c.reasons.join("；"):"")));
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
async function capabilityRelease(card,item,installation,epoch,generation){
  try{
    const users=await capabilityEmployees(epoch,generation);if(!users)return;
    const form=connectionText("section","");form.className="reminder-preview";form.append(connectionText("h4","发布给员工"));
    const label=connectionText("label","评测编号 "),evaluation=connectionText("input","");evaluation.type="text";evaluation.maxLength=128;
    evaluation.value=releaseState.evaluations.get(item.capability_id)||"";label.append(evaluation);form.append(label);
    const chosen=capabilityPicker(form,users,true);card.append(form);
    form.append(connectionButton("预览发布",async()=>{
      const targets=chosen().map(u=>({user_id:u.user_id,user_epoch:u.lifecycle_epoch}));
      if(!evaluation.value.trim()||!targets.length){capabilityNotice("请填写已通过的评测编号并至少选择一名员工。");return;}
      const body={installation_id:installation.id,evaluation_id:evaluation.value.trim(),targets};
      try{
        const preview=await api("POST","/v1/capabilities/"+encodeURIComponent(item.capability_id)+"/release-previews",{json:body});
        if(!capabilityLive(epoch,generation))return;
        const names=new Map(users.map(u=>[u.user_id,u.display_name||u.user_id]));
        const lines=preview.changes.map(c=>names.get(c.user_id)+"："+(c.from_revision===null?"新开通":"修订 "+c.from_revision)+" → 修订 "+c.to_revision);
        if(preview.supersedes.length)lines.push("将停止重叠的发布："+preview.supersedes.join("、"));
        lines.push("确认后这些员工可以新受理这项能力；名单外的员工不受影响。");
        capabilityReview(form,"发布","/v1/capabilities/"+encodeURIComponent(item.capability_id)+"/releases",
          {...body,request_id:crypto.randomUUID(),preview_digest:preview.digest,supersedes:preview.supersedes,confirmed:true},lines);
      }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.status===409?"评测证据或名单不符合发布条件："+error.message:error.message);}
    }));
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
