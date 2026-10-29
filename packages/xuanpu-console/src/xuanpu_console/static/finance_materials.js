// 财务材料选择与有限恢复：浏览器只保留请求线索，独立财务回执始终是事实来源。
"use strict";
const financeMaterialsPrefix="xuanpu.finance.materials.v1.";
function financeMaterialsScope(){
  const {tenant_id,user_id}=conn.me||{};
  if(!tenant_id||!user_id||!financeState.entity||conn.apiKey)throw new Error("材料恢复需要当前员工和公司，请重新登录并选择公司。");
  return {tenant_id,user_id,legal_entity_id:financeState.entity};
}
function financeMaterialsStorageKey(scope=financeMaterialsScope()){
  return financeMaterialsPrefix+[scope.tenant_id,scope.user_id,scope.legal_entity_id].map(value=>encodeURIComponent(value).replaceAll(".","%2E")).join(".");
}
function financeMaterialsIdentifier(value){return typeof value==="string"&&!!value.trim()&&value.length<=128;}
function financeMaterialsValidate(value,scope){
  const fields=["tenant_id","user_id","legal_entity_id","filename","size_bytes","media_type","sha256","request_id","file_ref","idempotency_key","operation_id"];
  if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).some(k=>!fields.includes(k)))throw new Error("材料恢复线索格式损坏，请放弃本地线索后重试。");
  for(const key of ["tenant_id","user_id","legal_entity_id"])if(value[key]!==scope[key])throw new Error("材料恢复身份不匹配，请放弃本地线索后重试。");
  for(const key of ["filename","media_type"])if(typeof value[key]!=="string"||!value[key].trim()||value[key].length>1024)throw new Error("材料恢复信息不完整。");
  if(!financeMaterialsIdentifier(value.request_id))throw new Error("材料恢复请求标识无效，须为1至128字符的非空白标识。");
  if(!Number.isSafeInteger(value.size_bytes)||value.size_bytes<1||value.size_bytes>50*1024*1024||typeof value.sha256!=="string"||!/^[a-f0-9]{64}$/.test(value.sha256))throw new Error("材料恢复文件信息无效。");
  if("file_ref" in value){const ref=value.file_ref;if(!ref||typeof ref!=="object"||Object.keys(ref).sort().join(",")!=="file_id,revision,sha256"||!financeMaterialsIdentifier(ref.file_id)||!Number.isSafeInteger(ref.revision)||ref.revision<1||ref.sha256!==value.sha256||!financeMaterialsIdentifier(value.idempotency_key)||!/^[A-Za-z0-9._:-]+$/.test(value.idempotency_key))throw new Error("材料恢复固定版本不完整。");}
  else if("idempotency_key" in value||"operation_id" in value)throw new Error("材料恢复阶段不完整。");
  if("operation_id" in value&&!financeMaterialsIdentifier(value.operation_id))throw new Error("材料恢复操作无效。");
  return value;
}
function financeMaterialsRead(scope=financeMaterialsScope()){
  try{const raw=localStorage.getItem(financeMaterialsStorageKey(scope));if(raw===null)return null;if(raw.length>8192)throw new Error("恢复信息超过上限");return financeMaterialsValidate(JSON.parse(raw),scope);}
  catch(error){throw new Error("无法读取材料恢复线索："+error.message);}
}
function financeMaterialsWrite(pointer,scope=financeMaterialsScope()){
  try{
    const current=financeMaterialsScope();if(JSON.stringify(current)!==JSON.stringify(scope))throw new Error("当前身份已变化");
    const key=financeMaterialsStorageKey(scope),value=financeMaterialsValidate({...pointer,...scope},scope),raw=JSON.stringify(value);
    if(raw.length>8192)throw new Error("恢复线索超过8192字符");
    const prior=financeMaterialsRead(scope);
    if(prior){
      for(const field of ["filename","size_bytes","media_type","sha256","request_id"])if(prior[field]!==value[field])throw new Error("已有未决材料，请恢复或主动放弃原线索");
      if(prior.file_ref&&(JSON.stringify(prior.file_ref)!==JSON.stringify(value.file_ref)||prior.idempotency_key!==value.idempotency_key))throw new Error("原材料固定版本和幂等键不能更换，请恢复原请求");
      if(prior.operation_id&&prior.operation_id!==value.operation_id)throw new Error("已受理材料只能查询原操作");
    }
    const prefix=financeMaterialsPrefix+[scope.tenant_id,scope.user_id].map(value=>encodeURIComponent(value).replaceAll(".","%2E")).join(".")+".";
    let count=0;for(let i=0;i<localStorage.length;i++)if(localStorage.key(i)?.startsWith(prefix))count++;
    if(localStorage.getItem(key)===null&&count>=10)throw new Error("同一员工最多保留10个公司的未决材料，请先处理或放弃旧线索");
    localStorage.setItem(key,raw);if(localStorage.getItem(key)!==raw)throw new Error("恢复线索写入未保全");return value;
  }catch(error){throw new Error("无法保存材料恢复线索，已阻止新请求："+error.message);}
}
function financeMaterialsForget(scope=financeMaterialsScope(),expected=null){
  try{
    if(JSON.stringify(financeMaterialsScope())!==JSON.stringify(scope))throw new Error("当前身份已变化，保留原恢复链");
    if(expected!==null&&JSON.stringify(financeMaterialsRead(scope))!==JSON.stringify(expected))throw new Error("所查材料线索已变化，保留当前恢复链，请刷新后恢复");
    localStorage.removeItem(financeMaterialsStorageKey(scope));if(localStorage.getItem(financeMaterialsStorageKey(scope))!==null)throw new Error("未删除");
  }catch(error){throw new Error("无法放弃本地恢复线索："+error.message);}
}
// 每次实际发送前重读原线索；另一窗口放弃或修改后，旧确认与未知结果重试均失效。
function financeMaterialsSendGuard(args){
  const scope=financeMaterialsScope(),pointer=financeMaterialsRead(scope),captured=JSON.stringify(pointer);
  if(!pointer?.file_ref||pointer.operation_id||args.body?.legal_entity_id!==scope.legal_entity_id||args["Idempotency-Key"]!==pointer.idempotency_key||args.body.files?.length!==1||JSON.stringify(args.body.files[0])!==JSON.stringify(pointer.file_ref))throw new Error("材料恢复原请求不匹配，请重新核对原收件线索。");
  return ()=>{
    try{
      if(JSON.stringify(financeMaterialsScope())!==JSON.stringify(scope)||JSON.stringify(financeMaterialsRead(scope))!==captured)throw new Error("原线索已变化或已被放弃");
    }catch(error){throw Object.assign(new Error("材料恢复确认已失效，已阻止发送。请核对原收件记录后重新恢复："+error.message),{status:400,code:"MATERIAL_RECOVERY_INVALID"});}
  };
}
function financeMaterialsToolbar(toolbar){toolbar.append(wsButton("我的收件记录",()=>financeMaterialsInbox()));}
function financeMaterialsStatus(value){return ({accepted:"已受理",queued:"等待处理",running:"处理中",succeeded:"接收完成",partial:"部分接收",failed:"处理失败",cancelled:"已取消",pending:"等待接收",material_received:"原件已保全",received:"原件已保全"})[value]||"待核对状态";}
function financeMaterialsEligible(item){return item?.selectable===true&&item.state==="material_received"&&typeof item.receipt_id==="string"&&!!item.receipt_id;}
async function financeMaterialsList(box,token,onSelect=null,cursor="",guard=()=>true,onCancel=null){
  const page=await financeCall("listSupportingMaterialImports",{legal_entity_id:financeState.entity,limit:20,...(cursor?{cursor}:{})});
  if(!financeCurrent(token)||!guard())return;
  if(!Array.isArray(page.items)||!Number.isSafeInteger(page.total)||page.total<0)throw new Error("材料目录响应不完整。");
  box.replaceChildren(wsText("p","我的收件记录（"+page.total+"）"));
  if(!page.items.length)box.append(wsText("p","尚无已受理的材料，可先上传支持材料。"));
  for(const operation of page.items){const card=wsText("article","","workspace-record");
    card.append(wsText("p","材料收件 · "+financeMaterialsStatus(operation.status)+" · "+(operation.created_at||"")+" · "+operation.total+"份"),wsButton("查看收件材料",async()=>{
      const detail=await financeCall("getSupportingMaterialImport",{id:operation.id});if(!financeCurrent(token)||!guard())return;
      if(detail.operation?.id!==operation.id||!Array.isArray(detail.materials))throw new Error("材料回执响应不完整。");
      const rows=wsText("div","");
      for(const item of detail.materials){const name=item.filename||"附件 "+(item.position+1),line=wsText("article","","workspace-record");line.append(wsText("p",name+" · "+financeMaterialsStatus(item.state)+" · 未查验"));
        if(financeMaterialsEligible(item))line.append(wsButton(onSelect?"选择附件 "+name:"用于新建报销",()=>onSelect?onSelect(item):financeEdit(null,[item])));
        rows.append(line);}
      card.replaceChildren(wsText("p","收件状态："+detail.operation.status),rows);
    }));box.append(card);}
  if(page.next_cursor)box.append(wsButton("下一页收件记录",()=>financeMaterialsList(box,token,onSelect,page.next_cursor,guard,onCancel)));
  if(onSelect)box.append(wsButton("取消选择附件",()=>onCancel?onCancel():box.replaceChildren()));
}
async function financeMaterialsInbox(){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,box=$("workspace-content");box.replaceChildren(wsText("h3","我的收件记录"));
  const form=wsText("form","","workspace-form"),state={ids:[],pending:null},save=wsButton("新建报销使用已收件附件",()=>financeEdit(null,state.selected||[]));form.append(save);box.append(form);financeMaterialsUpload(form,state,save,token,{required:true,onReceived:(_ids,items)=>{state.selected=items;}});
  const list=wsText("div","");box.append(list);await financeMaterialsList(list,token,null,"",()=>revision===financeState.revision&&financeState.materialForm===form);
}
function financeMaterialsAttachmentField(form,claim,token,initial=[]){
  let pickerGeneration=0;
  const section=wsText("section",""),cards=wsText("div",""),picker=wsText("div",""),details=wsText("details","");details.append(wsText("summary","附件技术信息（可选）"));
  const evidence=financeField(details,"附件记录编号（可选，多个用逗号分隔）","text",[...new Set([...(claim?.content.evidence_ids||[]),...initial.map(x=>x.receipt_id)])].join(","),false),names=new Map(initial.map(x=>[x.receipt_id,x.filename||"已收件附件"]));
  const read=()=>[...new Set(evidence.value.split(/[，,\s]+/).filter(Boolean))];
  const render=()=>{cards.replaceChildren();const ids=read();if(!ids.length)cards.append(wsText("p","尚未选择附件，可上传或选择已收件附件。"));ids.forEach((id,i)=>{const name=names.get(id)||"已有附件 "+(i+1),row=wsText("article","","workspace-record");row.append(wsText("p",name+" · 已关联材料，待人工核对"),wsButton("移除附件 "+name,()=>{evidence.value=read().filter(x=>x!==id).join(",");financeInvalidate(true);render();}));cards.append(row);});};
  const add=(ids,items=[])=>{for(const item of items)names.set(item.receipt_id,item.filename||"已收件附件");evidence.value=[...new Set([...read(),...ids])].join(",");financeInvalidate(true);render();};
  section.append(wsText("h4","报销附件"),cards,wsButton("选择已收件附件",async()=>{const generation=++pickerGeneration,guard=()=>financeState.materialForm===form&&generation===pickerGeneration,close=()=>{pickerGeneration++;picker.replaceChildren();};await financeMaterialsList(picker,token,item=>{if(!financeCurrent(token)||!guard())return;add([item.receipt_id],[item]);close();},"",guard,close);}),picker,details);form.append(section);evidence.addEventListener("input",render);render();return {evidence,add};
}
function financeMaterialsUpload(form,state,save,token,{required=true,onReceived=null}={}){
  financeState.materialForm=form;const scope=financeMaterialsScope(),input=financeField(form,"支持材料文件","file","",false),status=wsText("p","材料接收回执以独立财务服务为准。"),progress=wsText("div","");input.accept=".pdf,.png,.jpg,.jpeg,.xml";
  const active=()=>financeCurrent(token)&&financeState.materialForm===form&&JSON.stringify(scope)===JSON.stringify(financeMaterialsScope());
  const update=()=>{save.disabled=(required&&!state.ids.length)||!!(state.pending||state.uploading||state.confirming);};
  const report=error=>{status.textContent=error.message;fail(error);};
  const query=async()=>{
    const pointer=financeMaterialsRead(scope);if(!pointer?.operation_id)throw new Error("未取得原收件操作，请先恢复原请求。");const revision=financeState.revision;
    const detail=await financeCall("getSupportingMaterialImport",{id:pointer.operation_id});if(!active()||revision!==financeState.revision)return;
    if(JSON.stringify(financeMaterialsRead(scope))!==JSON.stringify(pointer))throw new Error("所查材料恢复线索已变化，已保留当前恢复链，请刷新后恢复；本次旧结果未关联附件。");
    if(detail.operation?.id!==pointer.operation_id||!Array.isArray(detail.materials))throw new Error("材料回执响应不完整。");
    const items=detail.materials.filter(financeMaterialsEligible),ids=items.map(x=>x.receipt_id);
    if(!ids.length){status.textContent="材料状态："+financeMaterialsStatus(detail.operation.status)+"，尚无可选真实接收回执，请保留原操作查询。";return;}
    financeMaterialsForget(scope,pointer);
    state.ids=[...new Set([...state.ids,...ids])];if(onReceived)onReceived(ids,items);state.pending=null;state.confirming=false;update();progress.replaceChildren();status.textContent="已取得真实材料接收回执 "+ids.length+" 份，待人工核对。";
  };
  const showQuery=()=>{progress.replaceChildren(wsButton("读取材料接收结果",()=>query().catch(report)));};
  const confirm=async pointer=>{
    if(!active())return;if(pointer.operation_id){state.pending=pointer.operation_id;showQuery();update();return;}
    state.confirming=true;update();await financeConfirmWrite("importSupportingMaterials",{"Idempotency-Key":pointer.idempotency_key,body:{legal_entity_id:scope.legal_entity_id,files:[pointer.file_ref]}},"确认将“"+pointer.filename+"”固定版本导入当前公司独立财务存储？",op=>{
      if(!op?.id)throw new Error("材料受理响应不完整，请恢复原请求。");financeMaterialsWrite({...pointer,operation_id:op.id},scope);
      if(!active())return;state.confirming=false;state.pending=op.id;status.textContent="材料已受理，尚未取得接收回执。";if(form.isConnected===false)$("workspace-content").replaceChildren(form);showQuery();update();
    },form);
  };
  const recover=async()=>{try{if(!active()||financeState.pending)return;const pointer=financeMaterialsRead(scope);if(!pointer)throw new Error("当前公司没有未决材料恢复线索。");financeInvalidate(true);if(pointer.file_ref)await confirm(pointer);else status.textContent="上次平台上传结果未核实，请重选同名、同大小、同摘要文件，再点击上传，继续原请求。";}catch(error){report(error);}};
  const upload=async()=>{
    const file=input.files?.[0];if(!file||state.pending||state.uploading||state.confirming||financeState.pending)return;
    financeInvalidate(true);state.uploading=true;update();const revision=financeState.revision;
    try{
      let pointer=financeMaterialsRead(scope);
      const bytes=await file.arrayBuffer(),sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),n=>n.toString(16).padStart(2,"0")).join("");if(!active()||revision!==financeState.revision)return;
      const metadata={filename:file.name,size_bytes:file.size,media_type:file.type||"application/octet-stream",sha256:sha};
      if(pointer&&(pointer.filename!==metadata.filename||pointer.size_bytes!==metadata.size_bytes||pointer.media_type!==metadata.media_type||pointer.sha256!==sha))throw new Error("材料恢复文件不匹配，请重选原文件或放弃本地线索。");
      if(pointer?.file_ref){await confirm(pointer);return;}
      pointer=financeMaterialsWrite(pointer||{...metadata,request_id:crypto.randomUUID()},scope);
      const item=await api("POST",workspaceBase+"/files?"+new URLSearchParams({name:pointer.filename,request_id:pointer.request_id}),{body:file,headers:{"Content-Type":pointer.media_type},signal:financeState.controller.signal});
      if(!active())return;pointer=financeMaterialsWrite({...pointer,file_ref:{file_id:item.id,revision:item.data?.current_revision,sha256:sha},idempotency_key:crypto.randomUUID()},scope);
      if(revision!==financeState.revision){status.textContent="文件已上传，请恢复上次材料收件继续确认。";return;}await confirm(pointer);
    }catch(error){if(active()){state.confirming=false;report(error);}}
    finally{state.uploading=false;if(active())update();}
  };
  form.append(status,progress,wsButton("上传支持材料",upload),wsButton("恢复上次材料收件",recover),wsButton("放弃本地材料恢复线索",()=>{if(state.uploading||financeState.pending){status.textContent="请求在途，先核对原请求再放弃线索。";return;}try{financeMaterialsForget(scope);financeInvalidate(true);state.pending=null;state.confirming=false;progress.replaceChildren();update();status.textContent="已放弃本地线索；平台文件和财务历史未删除。";}catch(error){report(error);}}));
  for(const event of ["input","change"])form.addEventListener(event,()=>{if(!financeState.pending&&!state.pending){state.confirming=false;update();}});
  try{const pointer=financeMaterialsRead(scope);if(pointer){status.textContent="发现上次材料恢复线索，请主动恢复；不会自动重发。";if(pointer.operation_id){state.pending=pointer.operation_id;showQuery();}}}catch(error){report(error);}update();
}
