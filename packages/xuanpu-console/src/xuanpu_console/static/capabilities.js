// 能力管理不依赖模型；登记、预检和明确确认互相独立，绝不自动运行。
"use strict";
const capabilityState={generation:0,adminView:false};
function capabilityLive(epoch,generation){return epoch===conn.epoch&&generation===capabilityState.generation;}
function capabilityNotice(text){$("capability-feedback").textContent=text;}
function resetCapabilities(){
  capabilityState.generation++;$("capability-list").replaceChildren();capabilityNotice("");
  $("capability-panel").classList.add("hidden");
}
async function openCapabilities(adminView=false){
  const epoch=conn.epoch,generation=++capabilityState.generation;
  capabilityState.adminView=adminView;
  $(adminView?"capability-admin-host":"capability-employee-host").append($("capability-panel"));
  $("capability-panel").classList.remove("hidden");$("capability-list").replaceChildren();
  $("capability-panel").scrollIntoView?.({block:"start"});capabilityNotice("正在读取能力配置…");
  try{
    if(conn.me?.user_id){
      const me=await api("GET","/v1/me/capabilities");if(!capabilityLive(epoch,generation))return;
      if(!Array.isArray(me.capabilities)||!Number.isInteger(me.total))throw new Error("能力目录响应不完整，请重新读取。");
      $("capability-list").append(connectionText("p",me.capabilities.length?"已分配能力（"+me.total+"）":"尚未分配可运行能力。个人工作方法不等于企业执行授权。"));
    }
    if(isAdmin())await capabilityManage(epoch,generation);
    if(capabilityLive(epoch,generation))capabilityNotice("登记和保存配置不会启用运行；分配及发布检查尚未完成。");
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
function capabilityReview(host,label,path,request,lines){
  const epoch=conn.epoch,generation=capabilityState.generation;
  if(!capabilityLive(epoch,generation))return;
  const review=connectionText("section","");review.className="reminder-preview";
  review.append(connectionText("h4","请核对本次"+label));
  for(const line of lines)review.append(connectionText("p",line));
  let pending=false;
  const submit=connectionButton("确认"+label,async()=>{
    if(pending||!capabilityLive(epoch,generation))return;
    pending=true;submit.disabled=true;
    try{
      await api("POST",path,{json:request});
      if(capabilityLive(epoch,generation)){const next=capabilityState.generation+1;await openCapabilities(capabilityState.adminView);if(capabilityLive(epoch,next))capabilityNotice("已"+label+"。当前仍不能运行。");}
    }catch(error){
      if(!capabilityLive(epoch,generation))return;
      capabilityNotice(error.status===409?"记录或条件已变化，请刷新并重新预检；没有覆盖旧配置。":error.message);
      if(error.status!==409){submit.disabled=false;submit.textContent="重试本次提交";}
    }finally{pending=false;}
  });
  review.append(submit,connectionButton("暂不提交",()=>review.remove()));host.append(review);
}
async function capabilityManage(epoch,generation,cursor=null){
  const body=await api("GET","/v1/capabilities?limit=50"+(cursor?"&cursor="+encodeURIComponent(cursor):""));
  if(!capabilityLive(epoch,generation))return;
  if(!Array.isArray(body.items)||!Array.isArray(body.installations)||!Number.isInteger(body.total))throw new Error("能力管理响应不完整，请重新读取。");
  const host=$("capability-list");
  if(!cursor){
    host.append(connectionText("h3","登记能力（"+body.total+"）"));
    if(body.configuration_enabled===true)capabilityRegistration(host,epoch,generation);
    else host.append(connectionText("p","企业尚未开启新能力配置，历史仍可查看和停止。"));
  }
  for(const item of body.items){
    const installation=body.installations.find(i=>i.capability_id===item.capability_id);
    capabilityCard(host,item,installation,body.configuration_enabled===true,epoch,generation);
  }
  if(body.next_cursor)host.append(connectionButton("查看更多修订",async()=>{
    try{await capabilityManage(epoch,generation,body.next_cursor);}catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
  }));
}
function capabilityRegistration(host,epoch,generation){
  const card=connectionText("article","");card.className="function-card";
  card.append(connectionText("h4","登记能力说明"),connectionText("p","选择交付方提供的能力说明文件。这里只登记说明，不安装、不分配或执行。"));
  const label=connectionText("label","交付说明文件 "),file=connectionText("input","");file.type="file";file.accept=".json,application/json";
  label.append(file);card.append(label);host.append(card);
  file.addEventListener("change",async()=>{
    try{
      const selected=file.files?.[0];if(!selected)return;
      if(selected.size>262144)throw new Error("说明文件不能超过256KiB。");
      const delivery=JSON.parse(await selected.text());
      if(!capabilityLive(epoch,generation))return;
      if(!delivery||typeof delivery!=="object"||Array.isArray(delivery))throw new Error("请选择有效的能力说明文件。");
      capabilityReview(card,"登记说明","/v1/capabilities",{request_id:crypto.randomUUID(),delivery},
        ["能力："+String(delivery.title||"未填写"),"版本："+String(delivery.package?.version||"未填写"),"登记后仍需独立预检、配置和发布。"]);
    }catch(error){if(capabilityLive(epoch,generation))capabilityNotice("无法读取能力说明，请检查文件格式；不要在说明中填写密钥。");}
  });
}
function capabilityCard(host,item,installation,enabled,epoch,generation){
  const card=connectionText("article","");card.className="function-card";
  const d=item.delivery;
  card.append(connectionText("h4",d.title+" · 说明修订 "+item.revision),connectionText("p",d.summary));
  card.append(connectionText("p","业务版本 "+d.package.version+" · 当前未通过分配和发布检查"));
  if(installation){
    const labels={staged:"待发布",ready:"已发布",draining:"停止新受理",disabled:"已停用"};
    if(!labels[installation.status])throw new Error("安装状态响应不完整，请重新读取。");
    card.append(connectionText("p","安装配置："+labels[installation.status]+" · 配置修订 "+installation.config_revision));
    if(installation.status!=="disabled")card.append(connectionButton("停止此安装",()=>capabilityReview(card,"停止安装",
      "/v1/installations/"+encodeURIComponent(installation.id)+"/state",
      {request_id:crypto.randomUUID(),expected_version:installation.version,status:"disabled"},["保留说明、旧配置与历史记录，不删除任何工作。"] )));
  }
  card.append(connectionButton("导出交付说明",()=>{
    if(!capabilityLive(epoch,generation))return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:"application/json"}));
    const link=connectionText("a","");link.href=url;link.download="delivery.json";link.click();URL.revokeObjectURL(url);
  }));
  if(enabled)card.append(connectionButton("配置并预检",()=>capabilityConfigure(card,item,epoch,generation)));
  host.append(card);
}
function capabilityField(host,title,definition,choices=null){
  const label=connectionText("label",title+" ");
  const values=choices||definition.choices||[];
  const selection=choices!==null||values.length||definition.kind==="boolean";
  const input=connectionText(selection?"select":"input","");
  if(selection){
    const empty=connectionText("option","请选择");empty.value="";input.append(empty);
    for(const value of choices!==null?values:values.length?values:["true","false"]){const option=connectionText("option",String(value));option.value=String(value);input.append(option);}
      if(choices!==null&&!values.length)input.disabled=true;
  }else{input.type=definition.kind==="integer"?"number":"text";input.maxLength=500;}
  label.append(input);host.append(label);return input;
}
async function capabilityConfigure(card,item,epoch,generation){
  try{
    const d=item.delivery,form=connectionText("section","");form.className="reminder-preview";
    const publicFields={},secretFields={},connectionFields={};
    for(const [name,definition] of Object.entries(d.requirements.public_config))publicFields[name]={definition,input:capabilityField(form,name,definition)};
    if(d.requirements.secret_refs.length){
      const names=await api("GET","/v1/secrets");if(!capabilityLive(epoch,generation))return;
      if(!Array.isArray(names.names))throw new Error("凭据目录响应不完整。");
      for(const slot of d.requirements.secret_refs)secretFields[slot]=capabilityField(form,"已配置凭据："+slot,{},names.names);
    }
    if(d.permissions.connections.length){
      const records=await api("GET","/v1/admin/connections");if(!capabilityLive(epoch,generation))return;
      if(!Array.isArray(records.connections))throw new Error("连接目录响应不完整。");
      for(const required of d.permissions.connections)connectionFields[required.connector]=capabilityField(form,"企业连接："+required.connector,{},records.connections.filter(c=>c.status==="active").map(c=>c.id));
    }
    let request=null,pending=false;
    const controls=[...Object.values(publicFields).map(f=>f.input),...Object.values(secretFields),...Object.values(connectionFields)];
    const submit=connectionButton("检查安装条件",async()=>{
      if(pending||!capabilityLive(epoch,generation))return;
      try{
        pending=true;submit.disabled=true;
        if(!request){
          const public_config={};for(const [name,field] of Object.entries(publicFields)){
            if(field.input.value!=="")public_config[name]=field.definition.kind==="integer"?Number(field.input.value):field.definition.kind==="boolean"?field.input.value==="true":field.input.value;
          }
          const selected=fields=>Object.fromEntries(Object.entries(fields).filter(([,field])=>field.value!=="").map(([name,field])=>[name,field.value]));
          request={request_id:crypto.randomUUID(),revision:item.revision,public_config,secret_refs:selected(secretFields),connection_refs:selected(connectionFields)};
          for(const control of controls)control.disabled=true;
        }
        const result=await api("POST","/v1/capabilities/"+encodeURIComponent(item.capability_id)+"/install-previews",{json:request});
        if(!capabilityLive(epoch,generation))return;capabilityPreview(form,item,result);submit.disabled=true;
      }catch(error){if(capabilityLive(epoch,generation)){capabilityNotice(error.message);submit.disabled=false;submit.textContent="重试同一次检查";}}
      finally{pending=false;}
    });
    form.append(connectionText("p","这里只选择已配置的凭据引用，不填写密钥值；检查可能连接声明的工具服务，但不会调用业务工具。"),submit);card.append(form);
  }catch(error){if(capabilityLive(epoch,generation))capabilityNotice(error.message);}
}
function capabilityPreview(host,item,result){
  const card=connectionText("section","");card.className="reminder-preview";
  if(!Array.isArray(result.requirements)||typeof result.can_install!=="boolean")throw new Error("预检响应不完整，请重新检查。");
  card.append(connectionText("h4",result.can_install?"安装条件已核对":"尚有条件未满足"));
  for(const missing of result.requirements)card.append(connectionText("p",capabilityRequirement(missing)));
  const p=item.delivery.permissions,d=item.delivery;
  const contract={office_files:"本次选择的办公文件",capability_receipt:"业务结果回执",read_only_answer:"只读答复"};
  const output={table_summary:"表格汇总",meeting_minutes:"会议纪要",capability_result:"业务结果",answer:"答复"};
  const lines=["输入范围："+contract[d.inputs.contract]+"，最多 "+d.inputs.max_files+" 个文件；输出："+output[d.outputs.output_kind],
    ...capabilityPermissions(p),"预检到期："+new Date(result.expires_at*1000).toLocaleString(),
    "保存后只进入待发布，不分配人员、不执行工作。"];
  const previous=result.diff?.previous_permissions;
  if(previous){
    const before=capabilityPermissions(previous),after=capabilityPermissions(p);
    lines.push("与上一安装配置比较：");
    const removed=before.filter(line=>!after.includes(line)),added=after.filter(line=>!before.includes(line));
    lines.push(...removed.map(line=>"此前："+line),...added.map(line=>"本次："+line));
    if(!removed.length&&!added.length)lines.push("权限相同。");
  }else lines.push("首次配置，没有已安装权限。");
  if(!result.can_install)for(const line of lines)card.append(connectionText("p",line));
  if(result.can_install)capabilityReview(card,"保存安装配置","/v1/capabilities/"+encodeURIComponent(item.capability_id)+"/installations",
    {request_id:crypto.randomUUID(),preview_id:result.id,expected_version:result.expected_version,confirmed:true},lines);
  host.append(card);
}
function capabilityPermissions(p){
  const tier={fast:"快速",balanced:"均衡",deep:"深入"};
  return ["脚本命令："+(p.shell?"需要":"不需要"),"脚本联网："+(p.network?"需要":"不需要"),
    "预算上限："+p.max_tokens+" tokens，最多 "+p.max_iterations+" 轮；模型层级："+(tier[p.model_tier]||p.model_tier),
    "操作确认："+({strict:"逐项确认",standard:"标准确认",relaxed:"较少确认"}[p.approval]),
    ...(p.tools.length?p.tools.map(t=>"工具："+t.name+"（"+({read:"读取",write:"写入",external:"外部效应"}[t.effect]||t.effect)+"；参数校验摘要 "+t.schema_sha256+"）"):["外部工具：不需要"]),
    ...(p.connections.length?p.connections.map(c=>"企业连接："+c.connector+"；范围："+c.operations.map(o=>({"self_profile.read":"本人资料只读","directory_preview.read":"管理员通讯录预览"}[o]||o)).join("、")):["企业连接：不需要"])];
}
function capabilityRequirement(code){
  const [kind,slot]=String(code).split(":",2);
  const labels={config_missing:"公开配置尚未填写",secret_unconfigured:"尚未选择已配置凭据",secret_missing:"所选凭据不存在",
    connection_unavailable:"企业连接未启用或不存在",connection_scope_missing:"企业连接范围不足",connection_credential_missing:"企业连接凭据缺失",
    budget_exceeds_platform:"预算超出当前平台上限",package_digest_mismatch:"业务包内容与交付说明不一致",
    tool_inventory_changed:"实际工具权限与交付说明不同",dependency_unavailable:"依赖服务暂时无法核对"};
  return (labels[kind]||"安装条件尚未满足")+(slot?"："+slot:"");
}
if(typeof document!=="undefined"){
  $("open-capabilities").addEventListener("click",()=>openCapabilities(false));
  $("close-capabilities").addEventListener("click",()=>resetCapabilities());
}
